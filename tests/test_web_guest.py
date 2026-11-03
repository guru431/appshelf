import io

import pytest
import zxingcpp
from PIL import Image

from apk_factory import make_apk
from appshelf.android import db, fetch, pin
from appshelf.android.sources import Latest
from helpers import HOST, make_account, sign_in

PKG = "ru.example.app"
URL = "https://bank.example/app.apk"
PIN = "482913"


def test_public_qr_decodes_to_current_apk_link(web, anon, cfg, conn, shelf, tmp_path):
    link = fetch.app_url(cfg, shelf, PKG)
    qr_path = link + "/qr.png"
    assert qr_path in web.client.get("/android/public").text
    assert qr_path not in anon.client.get("/p").text
    enter(anon.client, PIN)
    assert qr_path in anon.client.get("/p").text
    response = anon.client.get(qr_path)
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    image = Image.open(io.BytesIO(response.content))
    decoded = zxingcpp.read_barcode(image)
    assert decoded is not None and decoded.text == link
    redirect = anon.client.get(decoded.text, follow_redirects=False)
    current = db.current_version(conn, shelf.id, PKG)
    assert redirect.headers["location"] == fetch.file_url(cfg, shelf, current["dir"], fetch.apk_name(PKG, "1.0"))
    # QR постоянный: после обновления тот же адрес выдаёт новый APK.
    web.sources.apks[URL] = make_apk(tmp_path / "new.apk", version_code=2, version_name="2.0")
    web.sources._latest[URL] = Latest(None, "v2", True)
    web.client.post(f"/android/public/apps/{PKG}/check")
    fetch.run_job(web.app.state.w.env, conn, db.take_job(conn))
    assert anon.client.get(qr_path).content == response.content
    assert anon.client.get(link, follow_redirects=False).headers["location"].endswith(f"/{PKG}-2.0.apk")
    web.client.post("/android/public/pin", data={"pin": "111111"})
    assert anon.client.get(qr_path).status_code == 404
    assert anon.client.get(link, follow_redirects=False).status_code == 404
    new_shelf = db.public_shelf(conn)
    assert anon.client.get(fetch.app_url(cfg, new_shelf, PKG) + "/qr.png").status_code == 200


def test_qr_unavailable_for_private_missing_and_rotating_shelves(web, anon, cfg, conn, clock, shelf):
    mine = db.ensure_shelf(conn, 1, clock.iso())
    db.put_app(conn, shelf.id, "ru.queued", "Ожидает", "upload", "", clock.iso())
    for token, package in ((mine.shelf, PKG), ("0" * 32, PKG), (shelf.shelf, "ru.missing"),
                           (shelf.shelf, "ru.queued"), (shelf.shelf[:-1], PKG)):
        assert anon.client.get(f"/apk/{token}/{package}.apk/qr.png").status_code == 404
    db.set_shelf_next(conn, shelf.id, "1" * 32)
    assert anon.client.get(fetch.app_url(cfg, shelf, PKG) + "/qr.png").status_code == 404


def test_version_links_and_qr_select_exact_build_within_shelf(web, anon, cfg, conn, clock, shelf, tmp_path):
    old = db.current_version(conn, shelf.id, PKG)
    for code in (2, 3):
        web.sources.apks[URL] = make_apk(tmp_path / f"v{code}.apk", version_code=code, version_name=f"{code}.0")
        web.client.post("/android/public/add/link", data={"url": URL})
        fetch.run_job(web.app.state.w.env, conn, db.take_job(conn))
    link = fetch.app_url(cfg, shelf, PKG)
    selected = f"{link}?version={old['id']}"
    response = anon.client.get(f"{link}/qr.png?version={old['id']}")
    decoded = zxingcpp.read_barcode(Image.open(io.BytesIO(response.content)))
    assert decoded is not None and decoded.text == selected
    assert anon.client.get(selected, follow_redirects=False).headers["location"] == \
        fetch.file_url(cfg, shelf, old["dir"], fetch.apk_name(PKG, "1.0"))
    assert anon.client.get(link, follow_redirects=False).headers["location"].endswith(f"/{PKG}-3.0.apk")

    # A version ID from another shelf or a pending signature never grants access.
    web.client.post("/android/add/link", data={"url": URL})
    fetch.run_job(web.app.state.w.env, conn, db.take_job(conn))
    foreign = db.current_version(conn, db.shelf_for_user(conn, 1).id, PKG)
    web.sources.apks[URL] = make_apk(tmp_path / "pending.apk", version_code=4, cert=b"CERT-B")
    web.client.post("/android/public/add/link", data={"url": URL})
    fetch.run_job(web.app.state.w.env, conn, db.take_job(conn))
    pending = db.list_apps(conn, shelf.id)[0].pending
    for vid in (foreign["id"], pending["id"], -1, 999999, 1 << 100):
        for suffix in ("", "/qr.png"):
            assert anon.client.get(f"{link}{suffix}?version={vid}", follow_redirects=False).status_code == 404
    other = fetch.app_url(cfg, shelf, "ru.other")
    assert anon.client.get(f"{other}?version={old['id']}", follow_redirects=False).status_code == 404
    db.set_shelf_next(conn, shelf.id, "f" * 32)
    assert anon.client.get(selected, follow_redirects=False).status_code == 404


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    """Сто неверных PIN по 200 000 итераций — десятки секунд."""
    monkeypatch.setattr(pin, "ITERATIONS", 1000)


@pytest.fixture
def fewer_for_all(monkeypatch):
    monkeypatch.setattr(pin, "ALL_LIMIT", 3)


@pytest.fixture
def shelf(web, conn, tmp_path):
    """Владелец положил приложение на публичную полку и задал PIN."""
    web.sources.apks[URL] = make_apk(tmp_path / "a.apk", label="Банк")
    web.client.post("/android/public/add/link", data={"url": URL, "name": ""})
    fetch.run_job(web.app.state.w.env, conn, db.take_job(conn))
    web.client.post("/android/public/pin", data={"pin": PIN})
    return db.public_shelf(conn)


@pytest.fixture
def anon(make_web, cfg, conn):
    """Браузер гостя: в панель никто не входил."""
    return make_web(cfg, conn)


def enter(client, value, ip="198.51.100.7"):
    """POST /p через Apache: адрес клиента — последний элемент X-Forwarded-For."""
    return client.post("/p", data={"pin": value}, headers={"X-Forwarded-For": ip}, follow_redirects=False)


def close_for_all(client):
    """Общий предел — в тестах страницы 3 (fewer_for_all): сто запросов через веб дольше секунды, счёт до 100 —
    в test_android_pin."""
    for i in range(pin.ALL_LIMIT):
        r = enter(client, "000000", ip=f"198.51.100.{i}")
    return r


def test_guest_without_pin_sees_only_form(shelf, anon):
    r = anon.client.get("/p")
    assert r.status_code == 200 and 'name="pin"' in r.text and 'inputmode="numeric"' in r.text
    assert "Скачать APK" not in r.text and shelf.shelf not in r.text
    assert "/admin" not in r.text and "/android" not in r.text and "Выйти" not in r.text


def test_guest_right_pin_lists_apps_without_actions(shelf, anon):
    r = enter(anon.client, PIN)
    assert (r.status_code, r.headers["location"]) == (303, "/p")
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(f"{pin.COOKIE}=") and "httponly" in cookie and "secure" in cookie
    assert "samesite=lax" in cookie and f"max-age={pin.GUEST_DAYS * 86400}" in cookie
    page = anon.client.get("/p").text
    assert "Банк" in page and "Скачать APK" in page and f"/d/{shelf.shelf}/{PKG}-1/{PKG}-1.0.apk" in page
    assert '<form method="post"' not in page and "Действия" not in page and "/admin" not in page and "/android" not in page
    assert "Установка неизвестных приложений" in page                          # подсказки по установке


def test_guest_cannot_reach_panel(shelf, anon):
    assert enter(anon.client, PIN).status_code == 303
    for path in ("/android", "/android/public", "/android/public/add", "/apple", "/", "/admin"):
        r = anon.client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    assert anon.client.post("/android/public/pin/reopen").status_code == 401
    assert anon.client.get("/api/android/status", params={"public": 1}).status_code == 401


def test_not_configured_without_pin(web, anon, conn, clock):
    r = anon.client.get("/p")
    assert r.status_code == 200 and "не настроена" in r.text and 'name="pin"' not in r.text
    db.ensure_public_shelf(conn, clock.iso())                                   # полка есть, PIN нет
    assert "не настроена" in anon.client.get("/p").text
    assert "не настроена" in enter(anon.client, PIN).text
    assert conn.execute("SELECT COUNT(*) FROM pin_attempts").fetchone()[0] == 0
    assert "не настроена" in web.client.get("/p").text                          # и вошедшему участнику


def test_wrong_pin_counts_and_blocks_address(shelf, anon, conn):
    r = enter(anon.client, "000000")
    assert r.status_code == 401 and "Неверный PIN" in r.text and f"осталось попыток: {pin.IP_LIMIT - 1}" in r.text
    assert 'name="pin"' in r.text
    for _ in range(pin.IP_LIMIT - 1):
        r = enter(anon.client, "000000")
    assert r.status_code == 429 and "с этого адреса" in r.text and "60 мин" in r.text
    assert enter(anon.client, PIN).status_code == 429                           # и верный — в этот час нет
    forged = anon.client.post("/p", data={"pin": PIN}, headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.7"},
                              follow_redirects=False)
    assert forged.status_code == 429                                             # подставленный слева адрес не помог
    r = enter(anon.client, "12a456", ip="192.0.2.1")
    assert r.status_code == 400 and "6 цифр" in r.text
    assert enter(anon.client, PIN, ip="203.0.113.9").status_code == 303          # другой клиент — свой счёт


def test_global_close_sends_one_mail(shelf, anon, make_web, cfg, conn, fewer_for_all):
    r = close_for_all(anon.client)
    assert r.status_code == 429 and "временно закрыт" in r.text
    assert enter(anon.client, PIN, ip="203.0.113.9").status_code == 429
    mails = [(s, b) for s, b in anon.sent if "PIN" in s]
    assert len(mails) == 1 and PIN not in mails[0][1] and "/android/public" in mails[0][1]
    page = make_web(cfg, conn).client.get("/p").text
    assert "временно закрыт" in page and 'name="pin"' not in page


def test_guest_cookie_survives_global_close_but_not_pin_change(shelf, anon, web, make_web, cfg, conn, fewer_for_all):
    assert enter(anon.client, PIN).status_code == 303
    close_for_all(make_web(cfg, conn).client)                                   # чужие браузеры закрыли ввод
    assert "Скачать APK" in anon.client.get("/p").text                           # гость с cookie — видит
    value = anon.client.cookies.get(pin.COOKIE)
    forged = make_web(cfg, conn)
    forged.client.cookies.set(pin.COOKIE, value[:-1] + ("0" if value[-1] != "0" else "1"), domain=HOST)
    assert "Скачать APK" not in forged.client.get("/p").text
    web.client.post("/android/public/pin", data={"pin": "111111"})               # смена PIN — эпоха +1
    page = anon.client.get("/p").text
    assert "Скачать APK" not in page and 'name="pin"' in page                    # и ввод снова открыт


def test_signed_in_member_sees_guest_list_without_pin(shelf, make_web, cfg, conn, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    wb = make_web(cfg, conn)
    sign_in(wb.client, cfg, conn, clock, petr.user_id)
    page = wb.client.get("/p").text
    assert "Скачать APK" in page and 'name="pin"' not in page


def test_cross_site_post_to_p_rejected(shelf, anon, conn):
    r = anon.client.post("/p", data={"pin": PIN}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and pin.COOKIE not in anon.client.cookies
    assert conn.execute("SELECT COUNT(*) FROM pin_attempts").fetchone()[0] == 0
