import os
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from appshelf import jobs, people, store
from appshelf.ipatool import AuthCodeRequired, IpatoolError
from appshelf.web import signin
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


def test_foreign_next_after_login_stays_on_site(make_web, cfg, conn, acct):  # open redirect
    wb = make_web(cfg, conn)
    for target in ("https://evil.example/x", "//evil.example/x", "/\\evil.example"):
        r = wb.client.post("/login", data={"email": "owner@example", "password": "pw", "next": target},
                           follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", target


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


def test_apple_id_deleted_between_login_steps(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=acct.user_id)
    tool = FakeTool(cfg.accounts_dir / str(second.id))
    wb.tools[second.id] = tool
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    wb.client.post("/login", data={"email": "second@example", "password": "pw"})
    jobs.remove_account(cfg, conn, second)                    # удалили, пока ждали код
    r = wb.client.post("/login/code", data={"code": "123456"})
    assert r.status_code == 403 and "не зарегистрирован" in r.text and "appshelf_auth=" not in cookies(r)
    assert len(tool.calls) == 1 and not tool.home.exists()    # в Apple не ходили — HOME не появился снова


def test_apple_id_deleted_while_apple_checks_code(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=acct.user_id)
    tool = FakeTool(cfg.accounts_dir / str(second.id))
    wb.tools[second.id] = tool
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    wb.client.post("/login", data={"email": "second@example", "password": "pw"})

    def remove(email):  # удаляют, пока Apple проверяет код (запрос идёт в потоке приложения — своё соединение)
        with closing(store.connect(cfg.db)) as c:
            jobs.remove_account(cfg, c, second)

    tool.on_login = remove
    r = wb.client.post("/login/code", data={"code": "123456"})
    assert r.status_code == 403 and "appshelf_auth=" not in cookies(r)
    assert not tool.home.exists()                             # учётку, записанную после удаления, убрали


def test_person_deleted_while_adding_apple_id(web, conn, cfg, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    member = TestClient(web.app, base_url=BASE)
    sign_in(member, cfg, conn, clock, petr.user_id)
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.queue.append(t)
    member.post("/apple/add", data={"email": "petr2@example", "password": "pw"})
    jobs.remove_user(cfg, conn, petr.user_id)                 # владелец удалил Петра, пока тот вводил код
    r = member.post("/login/code", data={"code": "123456"})
    assert r.status_code == 403 and people.account_by_email(conn, "petr2@example") is None and not t.home.exists()


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


@pytest.mark.parametrize("error", ["invalid_credentials", "account_disabled"])
def test_wrong_password_closes_apple_id_for_an_hour(make_web, cfg, conn, acct, clock, error):
    # «account is disabled» Apple ответил на неверный пароль — считается так же, текст ведёт к паролю, не к учётке
    wb = make_web(cfg, conn)
    tool = wb.tools.setdefault(1, FakeTool())
    for _ in range(5):
        tool.errors["login"] = IpatoolError(error, "")
        r = wb.client.post("/login", data={"email": "owner@example", "password": "bad"})
        assert r.status_code == 400 and ("Неверный пароль" in r.text or "проверьте пароль" in r.text)
    calls = len(tool.calls)
    r = wb.client.post("/login", data={"email": "owner@example", "password": "good"})
    assert r.status_code == 429 and "закрыт" in r.text and len(tool.calls) == calls   # к Apple не ходили
    clock.advance(3601)
    assert wb.client.post("/login", data={"email": "owner@example", "password": "good"},
                          follow_redirects=False).status_code == 303


def test_failed_logins_limited_for_everyone(make_web, cfg, conn, acct, clock):
    # общий лимит — отказы Apple: неизвестные адреса и плохие ссылки его не тратят, иначе аноним закрыл бы вход всем
    wb = make_web(cfg, conn)
    for i in range(10):
        assert wb.client.post("/login", data={"email": f"x{i}@example", "password": "pw"}).status_code == 403
        assert wb.client.get(f"/join/nope{i}").status_code == 400
        assert wb.client.get(f"/l/nope{i}").status_code == 400 and wb.client.post(f"/l/nope{i}").status_code == 400
    for n in (2, 3):                                          # по 5 — больше на Apple ID закрывает его лимит
        other = make_account(conn, clock, email=f"a{n}@example", legacy=False, user_id=acct.user_id)
        tool = wb.tools[other.id] = FakeTool()
        for _ in range(5):
            tool.errors["login"] = IpatoolError("invalid_credentials", "")
            assert wb.client.post("/login", data={"email": f"a{n}@example", "password": "bad"}).status_code == 400
    r = wb.client.post("/login", data={"email": "owner@example", "password": "pw"})
    assert r.status_code == 429 and "подождите минуту" in r.text           # даже верный — до конца минуты
    clock.advance(61)
    assert wb.client.post("/login", data={"email": "owner@example", "password": "pw"},
                          follow_redirects=False).status_code == 303


def test_busy_apple_id_text(make_web, cfg, conn, acct):
    wb = make_web(cfg, conn)
    wb.tools.setdefault(1, FakeTool()).errors["login"] = IpatoolError("busy", "ipatool этого Apple ID занят")
    r = wb.client.post("/login", data={"email": "owner@example", "password": "pw"})
    assert r.status_code == 400 and "сейчас занят (вход, скачивание или проверка)" in r.text


def test_login_link_once_and_expires(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    token = people.create_link(conn, people.LOGIN, "", acct.user_id, clock.iso())
    preview = TestClient(wb.app, base_url=BASE, headers={"user-agent": "TelegramBot (like TwitterBot)"})
    for _ in range(2):                                        # робот мессенджера открывает ссылку ради превью
        r = preview.get(f"/l/{token}", follow_redirects=False)
        assert r.status_code == 200 and "Войти как Иван Петров" in r.text and "appshelf_auth=" not in cookies(r)
    assert conn.execute("SELECT used_at FROM links WHERE kind='login'").fetchone()["used_at"] == ""
    r = wb.client.post(f"/l/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and "appshelf_auth=" in cookies(r)
    assert wb.client.get("/", follow_redirects=False).status_code == 200
    other = TestClient(wb.app, base_url=BASE)
    assert other.post(f"/l/{token}").status_code == 400                   # второй раз — нет
    assert other.get(f"/l/{token}").status_code == 400
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
    r = other.post("/logout-all/1", follow_redirects=False)           # последний в браузере — как «Выйти»
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert any(h.startswith("appshelf_auth=") and "max-age=0" in h.lower() for h in r.headers.get_list("set-cookie"))
    assert people.get_user(conn, 1).epoch == 1
    web.client.cookies.set("appshelf_auth", old, domain=HOST)
    assert web.client.get("/", follow_redirects=False).status_code == 303   # старая cookie после «выйти везде»


def test_forget_one_person_keeps_the_others(web, conn, cfg, clock):  # 2026-10-12, Review Focus 3
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    sign_in(web.client, cfg, conn, clock, shared.user_id)
    web.client.get(f"/acct/{shared.id}")                              # активна полка того, кого уберём
    r = web.client.post(f"/logout/{shared.user_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    page = web.client.get("/").text
    assert "owner@example" in page and "shared@example" not in page   # активной стала оставшаяся полка
    assert people.get_user(conn, shared.user_id).epoch == 0           # в других браузерах Общий жив
    r = web.client.post("/logout/1", follow_redirects=False)          # последний — как «Выйти»
    assert r.headers["location"] == "/login"
    assert any(h.startswith("appshelf_auth=") and "max-age=0" in h.lower() for h in r.headers.get_list("set-cookie"))
    assert web.client.get("/", follow_redirects=False).status_code == 303


def test_logout_everywhere_for_one_person_spares_neighbours(web, conn, cfg, clock):
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    sign_in(web.client, cfg, conn, clock, shared.user_id)
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, name="Пётр")
    other = TestClient(web.app, base_url=BASE)                        # второй браузер: Пётр и тот же Общий
    sign_in(other, cfg, conn, clock, petr.user_id)
    sign_in(other, cfg, conn, clock, shared.user_id)
    mine, theirs = web.client.get("/").text, other.get("/").text      # критерий 3: общий — у обоих, личные — врозь
    assert "shared@example" in mine and "petr@example" not in mine
    assert "shared@example" in theirs and "owner@example" not in theirs
    r = web.client.post(f"/logout-all/{shared.user_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    assert people.get_user(conn, shared.user_id).epoch == 1 and people.get_user(conn, 1).epoch == 0
    mine, theirs = web.client.get("/").text, other.get("/").text
    assert "owner@example" in mine and "shared@example" not in mine
    assert "petr@example" in theirs and "shared@example" not in theirs   # Общий погашен и там, Пётр остался


def test_logout_of_person_not_signed_in_here_is_404(web, conn, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    for path in (f"/logout/{petr.user_id}", f"/logout-all/{petr.user_id}", "/logout/999"):
        assert web.client.post(path, follow_redirects=False).status_code == 404
    assert people.get_user(conn, petr.user_id).epoch == 0
    assert web.client.get("/", follow_redirects=False).status_code == 200


def test_switch_between_own_shelves(web, conn, cfg, clock):
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1)
    seed_app(conn, cfg, clock, app_id=5, name="Второе", acct=second)
    seed_app(conn, cfg, clock, app_id=6, name="Первое")
    assert names_in(web.client.get("/").text) == ["Первое"]
    r = web.client.get(f"/acct/{second.id}", headers={"referer": f"{BASE}/history"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/history"
    assert names_in(web.client.get("/").text) == ["Второе"]
    web.client.get("/acct/1", headers={"sec-fetch-site": "cross-site"})
    assert names_in(web.client.get("/").text) == ["Второе"]           # ссылка с чужого сайта полку не переключает
    stranger = make_account(conn, clock, email="x@example", role="member", legacy=False)
    web.client.get(f"/acct/{stranger.id}")
    assert names_in(web.client.get("/").text) == ["Второе"]           # чужую полку не выбрать


def test_switch_shelf_returns_without_referer(web, conn, cfg, clock):
    # Apache шлёт Referrer-Policy: no-referrer — путь возврата в самой ссылке
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1)
    page = web.client.get("/history", params={"q": "сбер"}).text
    assert f"/acct/{second.id}?back=/history%3Fq%3D%25D1%2581%25D0%25B1%25D0%25B5%25D1%2580" in page
    r = web.client.get(f"/acct/{second.id}", params={"back": "/history?q=сбер"}, follow_redirects=False)
    assert r.headers["location"] == "/history?q=%D1%81%D0%B1%D0%B5%D1%80"
    r = web.client.get(f"/acct/{second.id}", params={"back": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"


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


def test_two_people_in_one_browser_show_all_their_shelves(web, conn, cfg, clock):
    # spec 2026-10-12 §3: полки браузера — Apple ID всех вошедших; в другом браузере — только свои
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    seed_app(conn, cfg, clock, app_id=5, name="Общее", acct=shared)
    seed_app(conn, cfg, clock, app_id=6, name="Личное")
    sign_in(web.client, cfg, conn, clock, shared.user_id)             # владелец уже вошёл — Общий добавился
    page = web.client.get("/").text
    assert names_in(page) == ["Личное"] and f"/acct/{shared.id}" in page   # активна первая полка первого вошедшего
    web.client.get(f"/acct/{shared.id}")
    assert names_in(web.client.get("/").text) == ["Общее"]
    only = TestClient(web.app, base_url=BASE)                         # другой браузер: вошёл только Общий
    sign_in(only, cfg, conn, clock, shared.user_id)
    only.get("/acct/1")                                               # полку владельца не выбрать
    page = only.get("/").text
    assert names_in(page) == ["Общее"] and "owner@example" not in page
    assert only.post("/apps/6/retry", follow_redirects=False).status_code == 404


def test_junk_around_a_good_cookie_entry_is_ignored(web):  # 2026-10-12, Review Focus 1
    good = web.client.cookies.get("appshelf_auth")
    web.client.cookies.set("appshelf_auth", f"junk|admin:1890000000:{'0' * 64}||{good}|9:9:9:zz", domain=HOST)
    r = web.client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "owner@example" in r.text


def test_adding_apple_id_of_another_person_signs_this_browser_in_as_them(web, conn, cfg, clock):
    # spec 2026-10-12, критерии 1–2: общий Apple ID остаётся отдельным человеком, браузер входит и как он
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    seed_app(conn, cfg, clock, app_id=5, name="Общее", acct=shared)
    seed_app(conn, cfg, clock, app_id=6, name="Личное")
    assert "подключится к этому устройству" in web.client.get("/apple/add").text
    tool = web.tools.setdefault(shared.id, FakeTool(cfg.accounts_dir / str(shared.id)))
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    r = web.client.post("/apple/add", data={"email": "Shared@Example", "password": "pw"})
    assert r.status_code == 200 and 'action="/login/code"' in r.text and "Каталог" in r.text   # шаг кода — с шапкой
    r = web.client.post("/login/code", data={"code": "123456", "next": "/apple"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    assert web.new_tools == [] and tool.calls[-1] == ("login", "shared@example", "pw", "123456")  # HOME этого Apple ID
    assert people.get_account(conn, shared.id).user_id == shared.user_id and len(people.list_users(conn)) == 2
    assert names_in(web.client.get("/").text) == ["Общее"]            # активна подключённая полка
    web.client.get("/acct/1")
    assert names_in(web.client.get("/").text) == ["Личное"]           # своя — на месте, без повторного входа
    only = TestClient(web.app, base_url=BASE)                         # другой браузер: вход только общим
    assert only.post("/login", data={"email": "shared@example", "password": "pw"},
                     follow_redirects=False).status_code == 303
    page = only.get("/").text
    assert names_in(page) == ["Общее"] and "owner@example" not in page
    assert only.post("/apps/6/retry", follow_redirects=False).status_code == 404


def test_login_in_signed_in_browser_adds_the_person(web, conn, clock):  # 2026-10-12, Review Focus 5
    # обычный /login и «Войти заново» не выбрасывают остальных (spec 2026-10-12 §4)
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    r = web.client.post("/login", data={"email": "shared@example", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and f"appshelf_acct={shared.id}" in cookies(r)
    page = web.client.get("/").text
    assert "/acct/1" in page and '<span class="on">shared@example</span>' in page
    before = web.client.cookies.get("appshelf_auth")
    clock.advance(3600)
    web.client.post("/login", data={"email": "owner@example", "password": "pw"})   # владелец входит заново
    after = web.client.cookies.get("appshelf_auth")
    assert after.split("|")[1] == before.split("|")[1]                # запись Общего — как была, со своим сроком
    assert after.split("|")[0] != before.split("|")[0]                # своя — новая, на прежнем месте


def test_login_link_adds_person_to_signed_in_browser(web, conn, clock):
    # критерий 7: запасная ссылка подключает общий Apple ID без пароля и кода, остальные входы на месте
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    token = people.create_link(conn, people.LOGIN, "", shared.user_id, clock.iso())
    r = web.client.post(f"/l/{token}", follow_redirects=False)
    assert r.status_code == 303 and f"appshelf_acct={shared.id}" in cookies(r)
    page = web.client.get("/").text
    assert "/acct/1" in page and '<span class="on">shared@example</span>' in page
    assert set(web.tools) == {1}                                      # к Apple не ходили


def test_invite_in_signed_in_browser_adds_the_new_person(web, conn, clock):
    token = people.create_link(conn, people.INVITE, "общий", None, clock.iso())
    web.client.get(f"/join/{token}")
    assert "это устройство войдёт и как он" in web.client.get("/login").text
    r = web.client.post("/login", data={"email": "shared@example", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303
    shared = people.account_by_email(conn, "shared@example")
    assert shared.user_id != 1 and people.get_user(conn, shared.user_id).role == "member"
    page = web.client.get("/").text
    assert "/acct/1" in page and '<span class="on">shared@example</span>' in page   # вошли оба, активен новый


def test_new_apple_id_added_from_shared_shelf_stays_private(web, conn, cfg, clock):
    # находка 2026-10-12: при активной общей полке личный Apple ID доставался общему участнику
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    sign_in(web.client, cfg, conn, clock, shared.user_id)
    web.client.get(f"/acct/{shared.id}")
    web.client.post("/apple/add", data={"email": "new@example", "password": "pw"})
    new = people.account_by_email(conn, "new@example")
    assert new.user_id not in (1, shared.user_id)
    assert [a.id for a in people.accounts_of(conn, shared.user_id)] == [shared.id]
    assert "new@example" in web.client.get("/").text                  # в этом браузере полка подключена
    only = TestClient(web.app, base_url=BASE)                         # другой вход общим Apple ID её не видит
    sign_in(only, cfg, conn, clock, shared.user_id)
    page = only.get("/").text
    assert "shared@example" in page and "new@example" not in page


def test_wrong_code_while_adding_known_apple_id_keeps_add_form(web, conn, cfg, clock):
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    tool = web.tools.setdefault(shared.id, FakeTool(cfg.accounts_dir / str(shared.id)))
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.client.post("/apple/add", data={"email": "shared@example", "password": "pw"})
    page = web.client.get("/login").text                              # вернулись посреди шага кода
    assert 'name="code"' in page and "Каталог" in page
    tool.errors["login"] = IpatoolError("invalid_credentials", "")
    r = web.client.post("/login/code", data={"code": "123456", "next": "/apple"})
    assert r.status_code == 400 and 'action="/apple/add"' in r.text and "Неверный пароль или код" in r.text
    assert "shared@example" not in web.client.get("/").text           # не вошёл — полки нет


def test_invite_warns_signed_in_person(web, conn, clock):
    token = people.create_link(conn, people.INVITE, "все", None, clock.iso())
    web.client.get(f"/join/{token}")
    text = web.client.get("/login").text
    assert "Вы уже вошли как Иван Петров" in text and "станет отдельным участником" in text and "Safari" in text
    assert "Вы уже вошли" not in TestClient(web.app, base_url=BASE).get("/login").text


def test_wrong_code_while_adding_keeps_add_form(web, conn):
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.queue.append(t)
    web.client.post("/apple/add", data={"email": "second@example", "password": "pw"})
    t.errors["login"] = IpatoolError("invalid_credentials", "")
    r = web.client.post("/login/code", data={"code": "123456", "next": "/apple"})
    assert r.status_code == 400 and 'action="/apple/add"' in r.text and "добавить Apple ID" in r.text
    assert "Неверный пароль или код" in r.text and "Каталог" in r.text     # с шапкой, без «нужно приглашение»
    r = web.client.post("/login/code", data={"code": "123456"})          # шага больше нет — всё равно добавление
    assert 'action="/apple/add"' in r.text


def test_add_form_renders_when_browser_session_is_gone(web, conn):  # 2026-10-12, Review Focus 4
    # вход человека погашен между шагами «Добавить»: форма рисуется с шапкой, но без вошедших
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.queue.append(t)
    web.client.post("/apple/add", data={"email": "second@example", "password": "pw"})
    people.bump_epoch(conn, 1)                                        # «выйти везде» с другого устройства
    r = web.client.post("/login/code", data={"code": "12"})
    assert r.status_code == 400 and "Код — 6 цифр" in r.text and 'href="/admin"' not in r.text


def test_right_code_after_logout_everywhere_adds_nothing(web, conn):
    # человека погасили посреди «Добавить»: верный код не привязывает к нему Apple ID и не возвращает вход
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.queue.append(t)
    web.client.post("/apple/add", data={"email": "second@example", "password": "pw"})
    people.bump_epoch(conn, 1)                                        # «выйти везде» с другого устройства
    r = web.client.post("/login/code", data={"code": "123456", "next": "/apple"})
    assert r.status_code == 403 and "войдите и добавьте Apple ID заново" in r.text
    assert "appshelf_auth=" not in cookies(r)
    assert people.account_by_email(conn, "second@example") is None and not t.home.exists()
    assert web.client.get("/", follow_redirects=False).status_code == 303


def test_code_step_can_be_abandoned(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    wb.client.get(f"/join/{people.create_link(conn, people.INVITE, 'все', None, clock.iso())}")
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    wb.queue.append(t)
    r = wb.client.post("/login", data={"email": "petr@example", "password": "pw"})
    assert "Начать заново" in r.text and "Получить код проверки" in r.text
    assert 'name="code"' in wb.client.get("/login", params={"email": "other@example"}).text   # шаг держится
    r = wb.client.post("/login/cancel", data={"next": "/history"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?email=petr%40example&next=%2Fhistory"
    assert "appshelf_login=" in cookies(r) and not t.home.exists()       # пароль и временный HOME — прочь
    assert 'name="password"' in wb.client.get(r.headers["location"]).text
    assert wb.app.state.w.flow._pending == {}


def test_first_login_loads_history_right_away(make_web, cfg, conn, acct, clock):
    wb = make_web(cfg, conn)
    wb.client.get(f"/join/{people.create_link(conn, people.INVITE, 'все', None, clock.iso())}")
    wb.client.post("/login", data={"email": "petr@example", "password": "pw"})
    petr = people.account_by_email(conn, "petr@example")
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE account_id=? ORDER BY id", (petr.id,))]
    assert kinds == ["refresh_history", "check_removed"]
    assert "Загружаю историю…" in wb.client.get("/history").text
    store.upsert_purchases(conn, petr.id, [{"id": 7, "bundleId": "b", "name": "VK"}], clock.iso())
    conn.execute("UPDATE jobs SET status='done'")
    other = TestClient(wb.app, base_url=BASE)
    other.post("/login", data={"email": "petr@example", "password": "pw"})          # история уже есть
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE account_id=?", (petr.id,)).fetchone()[0] == 2


class Crash(Exception):
    """Процесс упал посреди регистрации."""


def invited_login(wb, conn, clock, monkeypatch, on_move):
    """Вход нового Apple ID petr@example по приглашению; on_move(src, dst) — вместо переноса временного HOME."""
    wb.client.get(f"/join/{people.create_link(conn, people.INVITE, 'все', None, clock.iso())}")
    real = os.replace

    def move(src, dst):
        return on_move(src, dst) if os.path.basename(src).startswith(".new-") else real(src, dst)

    monkeypatch.setattr(signin.os, "replace", move)
    return wb.client.post("/login", data={"email": "petr@example", "password": "S3cret-pw"}, follow_redirects=False)


def login_again(wb):
    """Повторный вход тем же Apple ID из другого браузера — уже как известным."""
    return TestClient(wb.app, base_url=BASE).post("/login", data={"email": "petr@example", "password": "pw"},
                                                  follow_redirects=False)


def test_new_apple_id_not_ready_until_home_moved(make_web, cfg, conn, acct, clock, monkeypatch):
    # находка аудита 2026-10-21: session=ok ставилась до переноса HOME — обработчик и ночь видели Apple ID без учётки
    wb, seen = make_web(cfg, conn), []

    def move(src, dst):
        with closing(store.connect(cfg.db)) as c:
            aid = people.account_by_email(c, "petr@example").id
            store.enqueue_once(c, aid, "refresh_history", None, clock.iso())
            seen.append((people.get_account(c, aid).session, store.take_job(c)))
        os.rename(src, dst)

    assert invited_login(wb, conn, clock, monkeypatch, move).status_code == 303
    assert seen == [("none", None)]
    petr = people.account_by_email(conn, "petr@example")
    assert petr.session == "ok" and (cfg.accounts_dir / str(petr.id) / ".ipatool" / "account").is_file()
    assert store.take_job(conn)["account_id"] == petr.id


def test_home_move_failure_rolls_registration_back(make_web, cfg, conn, acct, clock, monkeypatch, capsys):
    wb = make_web(cfg, conn)

    def move(src, dst):
        raise OSError(18, "Invalid cross-device link")

    r = invited_login(wb, conn, clock, monkeypatch, move)
    assert r.status_code == 500 and "сервер не смог сохранить вход" in r.text and "appshelf_auth=" not in cookies(r)
    assert people.account_by_email(conn, "petr@example") is None and [u.id for u in people.list_users(conn)] == [1]
    assert not list(cfg.accounts_dir.glob(".new-*")) and wb.sent == []
    assert people.list_invites(conn)[0]["joined"] == 0
    err = capsys.readouterr().err
    assert "не перенесена" in err and "petr@example" not in err and "S3cret-pw" not in err


def test_mark_ready_db_error_finishes_on_next_login(make_web, cfg, conn, acct, clock, monkeypatch):
    wb = make_web(cfg, conn)

    def broken(c, aid, now):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(people, "mark_ready", broken)
    r = invited_login(wb, conn, clock, monkeypatch, os.rename)
    assert r.status_code == 500 and "сервер не смог сохранить вход" in r.text and "appshelf_auth=" not in cookies(r)
    petr = people.account_by_email(conn, "petr@example")
    assert petr.session == "none" and (cfg.accounts_dir / str(petr.id) / ".ipatool" / "account").is_file()
    monkeypatch.undo()
    assert login_again(wb).status_code == 303 and people.get_account(conn, petr.id).session == "ok"


def test_crash_after_commit_finishes_on_next_login(make_web, cfg, conn, acct, clock, monkeypatch, ctx):
    wb = make_web(cfg, conn)

    def move(src, dst):
        raise Crash()

    with pytest.raises(Crash):
        invited_login(wb, conn, clock, monkeypatch, move)
    petr = people.account_by_email(conn, "petr@example")
    home = cfg.accounts_dir / str(petr.id)
    assert petr.session == "none" and not home.exists() and list(cfg.accounts_dir.glob(".new-*"))
    store.enqueue_once(conn, petr.id, "refresh_history", None, clock.iso())
    assert store.take_job(conn) is None                       # Apple ID без учётки обработчик не берёт
    monkeypatch.undo()
    jobs.Worker(ctx.env, lambda: store.connect(cfg.db)).recover()   # перезапуск: временный HOME прочь
    assert not list(cfg.accounts_dir.glob(".new-*"))
    assert login_again(wb).status_code == 303 and people.get_account(conn, petr.id).session == "ok"
    assert (home / ".ipatool" / "account").is_file() and store.take_job(conn)["account_id"] == petr.id


def test_apple_id_deleted_before_marked_ready(make_web, cfg, conn, acct, clock, monkeypatch):
    wb, gone = make_web(cfg, conn), []

    def move(src, dst):  # владелец удалил участника между транзакцией регистрации и переносом HOME
        with closing(store.connect(cfg.db)) as c:
            petr = people.account_by_email(c, "petr@example")
            jobs.remove_account(cfg, c, petr)
        gone.append(petr.id)
        os.rename(src, dst)

    r = invited_login(wb, conn, clock, monkeypatch, move)
    assert r.status_code == 403 and "не зарегистрирован" in r.text and "appshelf_auth=" not in cookies(r)
    assert not (cfg.accounts_dir / str(gone[0])).exists() and not list(cfg.accounts_dir.glob(".new-*"))
    assert people.account_by_email(conn, "petr@example") is None and wb.sent == []
