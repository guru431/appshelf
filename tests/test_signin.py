from dataclasses import replace

from starlette.testclient import TestClient

from appshelf import people, store
from appshelf.ipatool import AuthCodeRequired, IpatoolError
from helpers import HOST, FakeTool, make_account, seed_app, sign_in

BASE = f"https://{HOST}"


def cookies(r) -> str:
    return "; ".join(r.headers.get_list("set-cookie")).lower()


def names_in(html: str) -> list[str]:
    import re
    return re.findall(r"<b>([^<]*)</b>", html)


def test_pages_need_login(make_web, cfg, conn, acct):
    wb = make_web(cfg, conn)
    r = wb.client.get("/history?sort=date", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fhistory%3Fsort%3Ddate"
    r = wb.client.post("/apps/1/publish")
    assert r.status_code == 401 and "www-authenticate" not in r.headers   # без окна Basic Auth
    wb.client.cookies.set("appshelf_auth", "admin:1890000000:" + "0" * 64, domain=HOST)   # Review Focus 3
    assert wb.client.get("/", follow_redirects=False).status_code == 303


def test_known_apple_id_logs_in_with_2fa_code(make_web, cfg, conn, acct):  # Review Focus 2
    wb = make_web(cfg, conn)
    tool = wb.tools.setdefault(1, FakeTool())
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    r = wb.client.post("/login", data={"email": "  Owner@Example ", "password": "S3cret-pw", "next": "/history"})
    assert r.status_code == 200 and 'name="code"' in r.text and "S3cret-pw" not in r.text
    assert "город сервера" in r.text
    r = wb.client.post("/login/code", data={"code": "123 456", "next": "/history"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/history"
    assert tool.calls[-1] == ("login", "owner@example", "S3cret-pw", "123456")
    set_cookie = cookies(r)
    assert "appshelf_auth=" in set_cookie and "appshelf_acct=1" in set_cookie
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
    assert "max-age=31536000" in set_cookie
    assert people.get_account(conn, 1).session == "ok"
    assert wb.client.get("/", follow_redirects=False).status_code == 200
    for root in (cfg.data_dir, cfg.accounts_dir):
        for f in (root.rglob("*") if root.exists() else []):
            if f.is_file():
                assert b"S3cret-pw" not in f.read_bytes(), f


def test_unknown_apple_id_without_invite_never_reaches_apple(make_web, cfg, conn, acct):
    wb = make_web(cfg, conn)
    r = wb.client.post("/login", data={"email": "stranger@example", "password": "pw"})
    assert r.status_code == 403 and "нужна ссылка-приглашение" in r.text
    assert wb.new_tools == [] and wb.tools == {}
    assert not cfg.accounts_dir.exists() or not list(cfg.accounts_dir.iterdir())


def test_server_without_owner_says_not_configured(make_web, cfg, conn):
    r = make_web(cfg, conn).client.post("/login", data={"email": "anyone@example", "password": "pw"})
    assert r.status_code == 403 and "не задан APPSHELF_OWNER" in r.text


def test_owner_from_config_registers_once(make_web, cfg, conn):
    wb = make_web(replace(cfg, owner_email="boss@example"), conn)
    r = wb.client.post("/login", data={"email": "Boss@Example", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303
    [boss] = people.list_users(conn)
    a = people.account_by_email(conn, "boss@example")
    assert boss.role == "owner" and a.user_id == boss.id and a.device_mac[:8] in people.APPLE_OUIS
    assert (cfg.accounts_dir / str(a.id)).is_dir() and not list(cfg.accounts_dir.glob(".new-*"))
    assert wb.sent == []                                      # о себе владельцу не пишем
    other = TestClient(wb.app, base_url=BASE)
    assert other.post("/login", data={"email": "boss@example", "password": "pw"},
                      follow_redirects=False).status_code == 303
    assert len(people.list_users(conn)) == 1                  # второй вход — известный Apple ID


def test_invite_registers_member_and_mails_owner(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    token = people.create_link(conn, people.INVITE, "коллеги", None, clock.iso())
    r = wb.client.get(f"/join/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Приглашение принято" in wb.client.get("/login").text
    t = FakeTool()
    t.account = {"name": "Пётр", "storefront": "RU"}
    wb.queue.append(t)
    r = wb.client.post("/login", data={"email": "petr@example", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303
    petr = people.account_by_email(conn, "petr@example")
    user = people.get_user(conn, petr.user_id)
    assert (user.name, user.role) == ("Пётр", "member") and petr.user_id != acct.user_id
    assert t.home.name.startswith(".new-") and not t.home.exists() and (cfg.accounts_dir / str(petr.id)).is_dir()
    assert t.mac == petr.device_mac
    [(subject, body)] = wb.sent
    assert subject == "appshelf: новый участник" and "petr@example" in body and "коллеги" in body
    assert "petr@example" in wb.client.get("/").text            # вошёл: в шапке — его Apple ID
    assert people.list_invites(conn)[0]["joined"] == 1


def test_bad_invite_link(make_web, cfg, conn, acct):
    r = make_web(cfg, conn).client.get("/join/nope")
    assert r.status_code == 400 and "Ссылка-приглашение недействительна" in r.text


def test_invite_disabled_between_steps(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    token = people.create_link(conn, people.INVITE, "все", None, clock.iso())
    wb.client.get(f"/join/{token}")
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    wb.queue.append(t)
    assert 'name="code"' in wb.client.post("/login", data={"email": "petr@example", "password": "pw"}).text
    people.disable_invite(conn, people.list_invites(conn)[0]["id"], clock.iso())
    r = wb.client.post("/login/code", data={"code": "123456"})
    assert r.status_code == 403 and "Ссылка-приглашение недействительна" in r.text
    assert people.account_by_email(conn, "petr@example") is None and not t.home.exists()


def test_code_after_ttl_asks_to_start_over(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    wb.client.get(f"/join/{people.create_link(conn, people.INVITE, 'все', None, clock.iso())}")
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    wb.queue.append(t)
    wb.client.post("/login", data={"email": "petr@example", "password": "pw1"})
    clock.advance(601)
    assert "Срок шага истёк" in wb.client.post("/login/code", data={"code": "123456"}).text
    assert len(t.calls) == 1 and not t.home.exists()


def test_bad_code_format_keeps_step(make_web, cfg, conn, acct):
    wb = make_web(cfg, conn)
    wb.tools.setdefault(1, FakeTool()).errors["login"] = AuthCodeRequired("auth_code_required", "")
    wb.client.post("/login", data={"email": "owner@example", "password": "pw1"})
    r = wb.client.post("/login/code", data={"code": "12"})
    assert r.status_code == 400 and "Код — 6 цифр" in r.text
    assert wb.client.post("/login/code", data={"code": "123456"}, follow_redirects=False).status_code == 303


def test_edge_rejection_text_not_counted_as_wrong_password(make_web, cfg, conn, acct):
    wb = make_web(cfg, conn)
    tool = wb.tools.setdefault(1, FakeTool())
    for _ in range(6):
        tool.errors["login"] = IpatoolError("edge_rejected", "HTTP 301")
        r = wb.client.post("/login", data={"email": "owner@example", "password": "Xyzzy-42"})
        assert r.status_code == 400 and "Apple отклонил вход с этого адреса" in r.text and "Xyzzy-42" not in r.text
    assert wb.client.post("/login", data={"email": "owner@example", "password": "pw"},
                          follow_redirects=False).status_code == 303


def test_wrong_password_closes_apple_id_for_an_hour(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    tool = wb.tools.setdefault(1, FakeTool())
    for _ in range(5):
        tool.errors["login"] = IpatoolError("invalid_credentials", "")
        assert wb.client.post("/login", data={"email": "owner@example", "password": "bad"}).status_code == 400
    calls = len(tool.calls)
    r = wb.client.post("/login", data={"email": "owner@example", "password": "good"})
    assert r.status_code == 429 and "закрыт" in r.text and len(tool.calls) == calls   # к Apple не ходили
    clock.advance(3601)
    assert wb.client.post("/login", data={"email": "owner@example", "password": "good"},
                          follow_redirects=False).status_code == 303


def test_failed_logins_limited_for_everyone(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    for i in range(10):
        assert wb.client.post("/login", data={"email": f"x{i}@example", "password": "pw"}).status_code == 403
    r = wb.client.post("/login", data={"email": "owner@example", "password": "pw"})
    assert r.status_code == 429 and "подождите минуту" in r.text           # даже верный — до конца минуты
    clock.advance(61)
    assert wb.client.post("/login", data={"email": "owner@example", "password": "pw"},
                          follow_redirects=False).status_code == 303


def test_busy_apple_id_text(make_web, cfg, conn, acct):
    wb = make_web(cfg, conn)
    wb.tools.setdefault(1, FakeTool()).errors["login"] = IpatoolError("busy", "ipatool этого Apple ID занят")
    r = wb.client.post("/login", data={"email": "owner@example", "password": "pw"})
    assert r.status_code == 400 and "Идёт скачивание для этого Apple ID" in r.text


def test_login_link_once_and_expires(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    token = people.create_link(conn, people.LOGIN, "", acct.user_id, clock.iso())
    r = wb.client.get(f"/l/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and "appshelf_auth=" in cookies(r)
    assert wb.client.get("/", follow_redirects=False).status_code == 200
    other = TestClient(wb.app, base_url=BASE)
    assert other.get(f"/l/{token}").status_code == 400                    # второй раз — нет
    late = people.create_link(conn, people.LOGIN, "", acct.user_id, clock.iso())
    clock.advance(24 * 3600 + 1)
    r = other.get(f"/l/{late}")
    assert r.status_code == 400 and "Ссылка недействительна" in r.text


def test_logout_and_logout_everywhere(web, conn, cfg, clock):
    old = web.client.cookies.get("appshelf_auth")
    r = web.client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert any(h.startswith("appshelf_auth=") and "max-age=0" in h.lower() for h in r.headers.get_list("set-cookie"))
    other = TestClient(web.app, base_url=BASE)
    sign_in(other, cfg, conn, clock)
    assert other.post("/logout-all", follow_redirects=False).status_code == 303
    assert people.get_user(conn, 1).epoch == 1
    web.client.cookies.set("appshelf_auth", old, domain=HOST)
    assert web.client.get("/", follow_redirects=False).status_code == 303   # старая cookie после «выйти везде»


def test_switch_between_own_shelves(web, conn, cfg, clock):
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1)
    seed_app(conn, cfg, clock, app_id=5, name="Второе", acct=second)
    seed_app(conn, cfg, clock, app_id=6, name="Первое")
    assert names_in(web.client.get("/").text) == ["Первое"]
    r = web.client.get(f"/acct/{second.id}", headers={"referer": f"{BASE}/history"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/history"
    assert names_in(web.client.get("/").text) == ["Второе"]
    stranger = make_account(conn, clock, email="x@example", role="member", legacy=False)
    web.client.get(f"/acct/{stranger.id}")
    assert names_in(web.client.get("/").text) == ["Второе"]           # чужую полку не выбрать


def test_shelves_of_other_people_are_invisible(web, conn, cfg, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    seed_app(conn, cfg, clock, app_id=7, name="Чужое", acct=petr, versions=("1.0",))
    seed_app(conn, cfg, clock, app_id=8, name="Своё")
    text = web.client.get("/").text
    assert "Своё" in text and "Чужое" not in text and cfg.shelf_token(petr) not in text
    assert "Чужое" not in web.client.get("/history").text
    for action in ("publish", "retry", "unpublish"):
        assert web.client.post(f"/apps/7/{action}", follow_redirects=False).status_code == 404
    assert [a.app_id for a in store.list_apps(conn, petr.id)] == [7] and store.current_version(conn, petr.id, 7)
    assert web.client.get("/api/status").json()["apps"] == {"8": "queued"}
