"""Сессия без Apple ID и активный участник Android-раздела (spec 2026-10-28 §4, §5)."""
import pytest

from appshelf import people
from appshelf.android import db
from helpers import FakeTool, make_account, sign_in

UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0"


def google_person(conn, clock, name="Мария Гугл", sub="sub-m"):
    """Участник только с Google-аккаунтом."""
    uid = people.create_user(conn, name, people.MEMBER, None, clock.iso())
    people.create_google(conn, uid, sub, f"{sub}@gmail.example", name, clock.iso())
    return uid


@pytest.fixture
def gweb(make_web, cfg, conn, clock, acct):
    """В браузере вошёл только участник без Apple ID; владелец на сервере есть."""
    wb = make_web(cfg, conn)
    wb.uid = google_person(conn, clock)
    sign_in(wb.client, cfg, conn, clock, wb.uid)
    return wb


def shelf_app(conn, clock, uid, package, name):
    shelf = db.ensure_shelf(conn, uid, clock.iso())
    db.put_app(conn, shelf.id, package, name, "rustore", package, clock.iso())
    return shelf


def test_google_only_person_signs_in_and_sees_android(gweb, conn):
    r = gweb.client.get("/android", headers={"User-Agent": UA_DESKTOP})
    assert r.status_code == 200 and "Мария Гугл" in r.text                  # в шапке — имя участника
    r = gweb.client.get("/", headers={"User-Agent": UA_DESKTOP}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android")      # раздел, открытый последним
    r = gweb.client.get("/?os=apple", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/apple")        # Apple-раздел без Apple ID
    r = gweb.client.get("/apple")
    assert r.status_code == 200 and "Аккаунты" in r.text and "Добавить Apple ID" in r.text
    r = gweb.client.post("/android/add/link", data={"url": "https://bank.example/a.apk", "name": ""},
                         follow_redirects=False)
    assert r.status_code == 303 and db.shelf_for_user(conn, gweb.uid) is not None   # полка — его
    assert db.shelf_for_user(conn, 1) is None


@pytest.mark.parametrize("method,path", [("GET", "/history"), ("GET", "/blocked"), ("POST", "/apps/1/retry"),
                                         ("GET", "/api/status"), ("GET", "/acct/1"), ("POST", "/history/refresh"),
                                         ("POST", "/blocked/add"), ("GET", "/history?sort=date")])
def test_google_only_person_on_apple_paths(gweb, method, path):
    r = gweb.client.request(method, path, follow_redirects=False)
    if method == "GET" and not path.startswith("/api/"):
        assert (r.status_code, r.headers["location"]) == (303, "/apple")
    else:
        assert r.status_code == 409 and "Apple ID" in r.text


def test_who_switches_android_shelf_between_people(web, conn, cfg, clock):
    uid = google_person(conn, clock)
    sign_in(web.client, cfg, conn, clock, uid)
    shelf_app(conn, clock, 1, "ru.owner.app", "Приложение владельца")
    shelf_app(conn, clock, uid, "ru.guest.app", "Приложение Марии")
    r = web.client.get("/android")
    assert "Приложение владельца" in r.text and "Приложение Марии" not in r.text
    assert f'href="/who/{uid}?back=/android"' in r.text and 'href="/android/public"' in r.text      # меню шапки
    r = web.client.get(f"/who/{uid}?back=/android", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android")
    r = web.client.get("/android")
    assert "Приложение Марии" in r.text and "Приложение владельца" not in r.text
    assert web.client.get("/api/android/status").json()["apps"] == {"ru.guest.app": "queued"}
    assert web.client.post("/android/apps/ru.owner.app/remove").status_code == 404    # чужая полка — не его
    page = web.client.get("/?os=apple")                                       # Apple-раздел — полка Apple ID №1
    assert page.status_code == 200 and "owner@example" in page.text


def test_who_ignores_cross_site_and_foreign_uid(web, conn, cfg, clock):
    uid = google_person(conn, clock)
    shelf_app(conn, clock, uid, "ru.guest.app", "Приложение Марии")
    r = web.client.get(f"/who/{uid}?back=/android", follow_redirects=False)  # не вошёл в этом браузере
    assert r.status_code == 303 and "appshelf_who" not in r.cookies
    sign_in(web.client, cfg, conn, clock, uid)
    r = web.client.get(f"/who/{uid}?back=/android", headers={"Sec-Fetch-Site": "cross-site"},
                       follow_redirects=False)
    assert r.status_code == 303 and "appshelf_who" not in r.cookies
    assert "Приложение Марии" not in web.client.get("/android").text
    web.client.cookies.set("appshelf_who", "999", domain="apps.example")    # подделка — не среди вошедших
    assert "Приложение Марии" not in web.client.get("/android").text


def test_login_link_for_google_only_person(make_web, cfg, conn, clock, acct):
    wb = make_web(cfg, conn)
    uid = google_person(conn, clock)
    token = people.create_link(conn, people.LOGIN, "", uid, clock.iso())
    assert "Войти как Мария Гугл" in wb.client.get(f"/l/{token}").text
    r = wb.client.post(f"/l/{token}", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    r = wb.client.get("/android", headers={"User-Agent": UA_DESKTOP})
    assert r.status_code == 200 and "Мария Гугл" in r.text


def test_google_only_person_adds_apple_id(gweb, conn):
    t = FakeTool()
    t.account = {"name": "Мария", "storefront": "RU"}
    gweb.queue.append(t)
    r = gweb.client.post("/apple/add", data={"email": "maria@example", "password": "pw"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/apple")
    added = people.account_by_email(conn, "maria@example")
    assert added.user_id != gweb.uid                                         # новый Apple ID — отдельный участник
    [(_, body)] = gweb.sent
    assert "«Мария Гугл» (sub-m@gmail.example)" in body                      # добавивший — по Google-адресу
    assert gweb.client.get("/history").status_code == 200                    # теперь в браузере есть Apple ID


def test_deleting_apple_id_keeps_browser_signed_in_when_google_left(make_web, cfg, conn, clock, acct):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, name="Пётр")
    people.create_google(conn, petr.user_id, "sub-p", "petr@gmail.example", "Пётр", clock.iso())
    wb = make_web(cfg, conn)
    sign_in(wb.client, cfg, conn, clock, petr.user_id)
    r = wb.client.post(f"/apple/{petr.id}/delete", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/apple")
    assert people.get_account(conn, petr.id) is None and people.get_user(conn, petr.user_id) is not None
    assert wb.client.get("/apple").status_code == 200
    r = wb.client.get("/history", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/apple")
