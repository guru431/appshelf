import pytest

from apk_factory import make_apk
from appshelf import store
from appshelf.android import db, fetch, pin, rustore
from helpers import make_account, sign_in

PKG = "ru.example.app"
URL = "https://bank.example/app.apk"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    """Сто неверных PIN по 200 000 итераций — десятки секунд."""
    monkeypatch.setattr(pin, "ITERATIONS", 1000)


@pytest.fixture
def member(make_web, cfg, conn, clock, acct):
    """Другой браузер: вошёл только участник, владелец — нет."""
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    wb = make_web(cfg, conn)
    sign_in(wb.client, cfg, conn, clock, petr.user_id)
    return wb


def work(web, conn):
    job = db.take_job(conn)
    assert job is not None
    fetch.run_job(web.app.state.w.env, conn, job)


def add_public(web, conn, tmp_path, url=URL, **apk):
    """Владелец добавил APK по ссылке на публичную полку, обработчик скачал."""
    web.sources.apks[url] = make_apk(tmp_path / f"{len(web.sources.apks)}.apk", **apk)
    r = web.client.post("/android/public/add/link", data={"url": url, "name": ""}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android/public")
    work(web, conn)
    return db.public_shelf(conn)


def test_member_sees_public_shelf_read_only(web, member, conn, tmp_path):
    assert member.client.get("/android/public/add?q=example").status_code == 403
    assert member.client.post("/android/public/add/source", data={"source": "github", "ref": "example/app"}).status_code == 403
    shelf = add_public(web, conn, tmp_path)
    r = member.client.get("/android/public")
    assert r.status_code == 200 and f"/d/{shelf.shelf}/{PKG}-1/{PKG}-1.0.apk" in r.text
    assert "Действия" not in r.text and "/android/public/add" not in r.text and "Гостевой доступ" not in r.text
    assert 'href="/android/public"' in member.client.get("/android").text                 # вкладка «Публичная»
    for path, data in (("/android/public/add/rustore", {"package": "ru.other", "name": ""}),
                       ("/android/public/add/link", {"url": URL, "name": ""}),
                       (f"/android/public/apps/{PKG}/remove", {}), (f"/android/public/apps/{PKG}/check", {}),
                       (f"/android/public/apps/{PKG}/source", {"source": "rustore", "ref": ""}),
                       ("/android/public/pin", {"pin": "123456"}), ("/android/public/pin/reopen", {})):
        assert member.client.post(path, data=data).status_code == 403, path
    r = member.client.post("/android/public/add/upload", data={"name": ""}, files={"file": ("a.apk", b"x")})
    assert r.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE status='queued'").fetchone()[0] == 0
    assert db.get_app(conn, shelf.id, PKG) is not None and not pin.is_set(conn)


def test_owner_adds_to_public_and_worker_fills_it(web, conn, tmp_path):
    web.sources.found = [rustore.StoreApp(1, PKG, "Пример", "", 1, "", 0, "", 0)]
    assert 'action="/android/public/add/source"' in web.client.get("/android/public/add", params={"q": "пример"}).text
    web.sources.apks[PKG] = make_apk(tmp_path / "a.apk")
    r = web.client.post("/android/public/add/rustore", data={"package": PKG, "name": "Пример"}, follow_redirects=False)
    assert r.headers["location"] == "/android/public"
    shelf = db.public_shelf(conn)
    assert shelf is not None and db.get_app(conn, shelf.id, PKG)["status"] == "queued"
    assert web.client.get("/api/android/status", params={"public": 1}).json() == {"busy": True, "apps": {PKG: "queued"}}
    assert web.client.get("/api/android/status").json() == {"busy": False, "apps": {}}   # своя полка пуста
    work(web, conn)
    page = web.client.get("/android/public").text
    assert f"/d/{shelf.shelf}/{PKG}-1/" in page and f'action="/android/public/apps/{PKG}/check"' in page
    assert 'href="/android/public/add"' in page                                          # «Добавить в публичную»


def test_personal_and_public_are_separate(web, conn, tmp_path):
    add_public(web, conn, tmp_path, label="Публичное")
    web.sources.apks["https://bank.example/mine.apk"] = make_apk(tmp_path / "m.apk", package="ru.mine", label="Своё")
    web.client.post("/android/add/link", data={"url": "https://bank.example/mine.apk", "name": ""})
    work(web, conn)
    mine, public = web.client.get("/android").text, web.client.get("/android/public").text
    assert "Своё" in mine and "Публичное" not in mine and "Публичное" in public and "Своё" not in public
    assert web.client.post(f"/android/apps/{PKG}/remove").status_code == 404
    assert web.client.post("/android/public/apps/ru.mine/remove").status_code == 404


def test_owner_sets_pin_then_change_rotates_token(web, cfg, conn, clock, tmp_path):
    shelf = add_public(web, conn, tmp_path)
    page = web.client.get("/android/public").text
    assert "Гостевой доступ" in page and "https://apps.example/p" in page and "Задайте PIN" in page
    r = web.client.post("/android/public/pin", data={"pin": "482913"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android/public")
    assert db.public_shelf(conn) == shelf and pin.epoch(conn) == 1                  # первая установка — токен прежний
    assert pin.try_pin(conn, "ip:x", "482913", clock.iso()).result == "ok"
    assert "Задайте PIN" not in web.client.get("/android/public").text
    web.client.post("/android/public/pin", data={"pin": "111111"})
    new = db.public_shelf(conn)
    assert new.shelf != shelf.shelf and pin.epoch(conn) == 2
    assert (cfg.archive / new.shelf / f"{PKG}-1").is_dir() and not (cfg.archive / shelf.shelf).exists()
    assert f"/d/{new.shelf}/{PKG}-1/{PKG}-1.0.apk" in web.client.get("/android/public").text


def test_pin_change_refused_while_downloading(web, cfg, conn, clock, tmp_path):
    shelf = add_public(web, conn, tmp_path)
    web.client.post("/android/public/pin", data={"pin": "482913"})
    web.client.post(f"/android/public/apps/{PKG}/check")
    db.take_job(conn)                                                              # обработчик качает
    r = web.client.post("/android/public/pin", data={"pin": "111111"})
    assert r.status_code == 409 and "Идёт скачивание" in r.text
    assert db.public_shelf(conn) == shelf and pin.try_pin(conn, "ip:x", "482913", clock.iso()).result == "ok"
    conn.execute("UPDATE android_jobs SET status='done'")
    cfg.archive.rename(cfg.archive.with_name("gone"))                              # шара не смонтирована
    assert web.client.post("/android/public/pin", data={"pin": "111111"}).status_code == 503
    assert pin.epoch(conn) == 1


def test_bad_pin_format_on_owner_form(web, conn):
    for bad in ("12a456", "12345", "１２３４５６"):
        r = web.client.post("/android/public/pin", data={"pin": bad})
        assert r.status_code == 400 and "6 цифр" in r.text, bad
    assert not pin.is_set(conn) and store.get_state(conn, pin.HASH_KEY) == ""


def test_reopen_button_only_for_owner(web, member, conn, clock):
    web.client.post("/android/public/pin", data={"pin": "482913"})
    for i in range(pin.ALL_LIMIT):
        pin.try_pin(conn, f"ip:198.51.100.{i}", "000000", clock.iso())
    page = web.client.get("/android/public").text
    assert "Ввод PIN закрыт до" in page and 'action="/android/public/pin/reopen"' in page
    assert "/android/public/pin/reopen" not in member.client.get("/android/public").text
    assert member.client.post("/android/public/pin/reopen").status_code == 403 and pin.closed_until(conn, clock.iso())
    r = web.client.post("/android/public/pin/reopen", follow_redirects=False)
    assert r.status_code == 303 and pin.closed_until(conn, clock.iso()) == ""
