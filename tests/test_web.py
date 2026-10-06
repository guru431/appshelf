import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from appshelf import store, webauth
from appshelf.config import GB
from appshelf.ipatool import AuthCodeRequired, IpatoolError
from appshelf.web.app import create_app, parse_app_id
from helpers import TOKEN, FakeTool, basic, seed_app


@pytest.fixture
def web(cfg, conn, clock):
    webauth.set_password(cfg.web_auth, "admin", "pw", iterations=1000)
    tool, sent, free = FakeTool(), [], {"bytes": 50 * GB}
    app = create_app(cfg, tool, now=clock.iso, clock=clock.monotonic,
                     send=lambda subject, body, to: sent.append(subject),
                     disk_free=lambda path: free["bytes"], start_worker=False)
    return SimpleNamespace(client=TestClient(app, headers=basic("admin", "pw")), app=app, tool=tool, free=free)


def test_healthz_without_password(web):
    r = TestClient(web.app).get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_pwa_manifest_and_icons_without_password(web):
    # iOS «На экран „Домой“» и браузеры берут манифест и иконку без Basic-авторизации
    anon = TestClient(web.app)
    m = anon.get("/pwa/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["display"] == "standalone" and data["start_url"] == "/" and data["scope"] == "/"
    for icon in data["icons"]:
        assert anon.get(icon["src"]).status_code == 200
    r = anon.get("/pwa/icon-180.png")
    img = Image.open(io.BytesIO(r.content))
    assert r.headers["content-type"] == "image/png" and img.size == (180, 180) and img.mode == "RGB"  # iOS: без прозрачности
    assert anon.get("/pwa/icon-77.png").status_code == 404


def test_pages_link_pwa_manifest(web):
    r = web.client.get("/")
    assert '<link rel="manifest" href="/pwa/manifest.webmanifest">' in r.text
    assert '<link rel="apple-touch-icon" href="/pwa/icon-180.png">' in r.text
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in r.text


def test_pages_need_password(web, cfg):
    anon = TestClient(web.app, follow_redirects=False)
    r = anon.get("/history?sort=date")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fhistory%3Fsort%3Ddate"
    r = TestClient(web.app, headers=basic("admin", "nope"), follow_redirects=False).get("/history")
    assert r.status_code == 303
    r = anon.post("/apps/1/publish")
    assert r.status_code == 401 and "www-authenticate" not in r.headers  # без окна Basic Auth
    cfg.web_auth.unlink()
    assert web.client.get("/").status_code == 503


def login(web, password="pw", next_="/"):
    return TestClient(web.app, follow_redirects=False).post(
        "/login", data={"user": "admin", "password": password, "next": next_})


def cookie_get(web, value, path="/"):
    return TestClient(web.app, follow_redirects=False).get(path, headers={"Cookie": f"appshelf_auth={value}"})


def test_login_form_sets_long_cookie_for_pwa(web):
    # PWA с экрана «Домой» не показывает окно Basic Auth (iPhone: 401 → чёрный экран) — вход формой
    form = TestClient(web.app).get("/login")
    assert form.status_code == 200 and 'type="password"' in form.text
    r = login(web, next_="/history")
    assert r.status_code == 303 and r.headers["location"] == "/history"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "max-age=31536000" in cookie
    assert cookie_get(web, r.cookies["appshelf_auth"]).status_code == 200


def test_login_wrong_password_and_foreign_next(web):
    r = login(web, password="nope")
    assert r.status_code == 401 and "Неверный" in r.text and "set-cookie" not in r.headers
    assert login(web, next_="//evil.example/x").headers["location"] == "/"
    assert login(web, next_="https://evil.example").headers["location"] == "/"


def test_failed_logins_limited(web, clock):
    # Apache больше не спрашивает пароль: подбор и нагрузку PBKDF2 сдерживает само приложение
    value = login(web).cookies["appshelf_auth"]
    for _ in range(10):
        assert login(web, password="nope").status_code == 401
    assert login(web).status_code == 429                      # даже верный — до конца минуты
    bad = TestClient(web.app, headers=basic("admin", "nope"), follow_redirects=False)
    assert bad.post("/apps/1/publish").status_code == 401
    assert cookie_get(web, value).status_code == 200          # выданная cookie работает
    clock.advance(61)
    assert login(web).status_code == 303


def test_cookie_forged_expired_or_old_password_rejected(web, cfg, clock):
    value = login(web).cookies["appshelf_auth"]
    assert cookie_get(web, value).status_code == 200
    user, exp, mac = value.split(":")
    assert cookie_get(web, f"{user}:{int(exp) + 10 ** 6}:{mac}").status_code == 303  # срок подделкой не продлить
    clock.advance(366 * 86400)
    assert cookie_get(web, value).status_code == 303                                  # истёк
    clock.advance(-366 * 86400)
    webauth.set_password(cfg.web_auth, "admin", "new", iterations=1000)
    assert cookie_get(web, value).status_code == 303                                  # смена пароля гасит cookie


def test_catalog_banner_when_archive_unavailable(web, cfg):
    cfg.pub_root.parent.rmdir()  # шара не смонтирована
    r = web.client.get("/")
    assert r.status_code == 200 and "Архив недоступен" in r.text and TOKEN not in r.text.split("<main>")[1]


def test_cross_origin_post_forbidden(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0",))
    r = web.client.post("/apps/123/unpublish", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and store.list_apps(conn)


def test_catalog_shows_install_and_previous(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("12.15.0", "12.16.0"))
    r = web.client.get("/")
    base = f"itms-services://?action=download-manifest&amp;url=https://apps.example/d/{TOKEN}"
    assert f"{base}/123-12.16.0/manifest.plist" in r.text
    assert f"{base}/123-12.15.0/manifest.plist" in r.text and "предыдущая" in r.text
    assert f"https://apps.example/d/{TOKEN}/123-12.16.0/icon.png" in r.text
    assert "iOS 13.0+" in r.text and "291 МБ" in r.text
    assert "сборка 18.06.2026" in r.text and clock.iso()[:10] not in r.text  # дата версии, не публикации


def test_banners_for_expired_session_and_low_space(web, conn):
    store.set_state(conn, "session", "expired")
    web.free["bytes"] = 2 * GB
    r = web.client.get("/")
    assert "Вход в Apple ID истёк" in r.text and "Мало места" in r.text


def test_history_search_cyrillic_case_insensitive(web, conn, clock):  # Review Focus 3
    store.upsert_purchases(conn, [{"id": 1, "bundleId": "ru.sberbank.onlineiphone", "name": "СберБанк Онлайн"},
                                  {"id": 2, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    r = web.client.get("/history", params={"q": "сбер"})
    assert "СберБанк Онлайн" in r.text and "com.vk.vkclient" not in r.text


def test_publish_and_unpublish(web, conn, cfg, clock):
    store.upsert_purchases(conn, [{"id": 7, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    assert web.client.post("/apps/7/publish", follow_redirects=False).status_code == 303
    assert web.client.post("/apps/7/publish", follow_redirects=False).status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish'").fetchone()[0] == 1
    assert "в каталоге" in web.client.get("/history").text
    (cfg.pub_root / "7-1.0").mkdir(parents=True)
    store.add_version(conn, cfg.pub_root, 7, store.NewVersion("1.0", "1", "900", "13.0", "iphone", 1, "7-1.0"),
                      clock.iso())
    web.client.post("/apps/7/unpublish")
    assert store.list_apps(conn) == [] and not (cfg.pub_root / "7-1.0").exists()


def test_login_two_steps_password_not_kept(web, conn, cfg):
    web.tool.errors["login"] = AuthCodeRequired("auth_code_required", "нужен код")
    r = web.client.post("/apple/login", data={"email": "owner@example", "password": "S3cret-pw"})
    assert r.status_code == 200 and 'name="code"' in r.text and "S3cret-pw" not in r.text
    r = web.client.post("/apple/code", data={"code": "123 456"}, follow_redirects=False)
    assert r.status_code == 303
    assert web.tool.calls[-1] == ("login", "owner@example", "S3cret-pw", "123456")
    assert not web.app.state.login.waiting_code
    assert store.get_state(conn, "session") == "ok" and store.get_state(conn, "account_name") == "Иван Петров"
    for f in cfg.data_dir.rglob("*"):
        if f.is_file():
            assert b"S3cret-pw" not in f.read_bytes(), f


def test_code_after_ttl_asks_to_start_over(web, clock):
    web.tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.client.post("/apple/login", data={"email": "owner@example", "password": "pw1"})
    clock.advance(601)
    assert "Срок шага истёк" in web.client.post("/apple/code", data={"code": "123456"}).text
    assert [c[0] for c in web.tool.calls] == ["login"]


def test_bad_code_format_keeps_step(web):
    web.tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.client.post("/apple/login", data={"email": "owner@example", "password": "pw1"})
    r = web.client.post("/apple/code", data={"code": "12"})
    assert "Код — 6 цифр" in r.text and web.app.state.login.waiting_code


def test_edge_rejection_text(web):
    web.tool.errors["login"] = IpatoolError("edge_rejected", "HTTP 301")
    r = web.client.post("/apple/login", data={"email": "owner@example", "password": "Xyzzy-42"})
    assert r.status_code == 400
    assert "Apple отклонил вход с этого адреса" in r.text and "Xyzzy-42" not in r.text


def test_api_status(web, conn, cfg, clock):
    seed_app(conn, cfg, clock)
    store.set_state(conn, "session", "ok")
    assert web.client.get("/api/status").json() == {"busy": True, "session": "ok", "apps": {"123": "queued"}}


def test_history_shows_failed_refresh(web, conn, clock):  # review #2
    store.set_state(conn, "session", "ok")
    store.enqueue_once(conn, "refresh_history", None, clock.iso())
    job = store.take_job(conn)
    store.finish_job(conn, job["id"], "error", clock.iso(), "ipatool: purchase history update response returned DAAP status 500")
    r = web.client.get("/history")
    assert "не удалось обновить" in r.text and "DAAP status 500" in r.text


def test_history_refreshing_only_for_refresh_jobs(web, conn, cfg, clock):  # review #10
    seed_app(conn, cfg, clock)  # задание publish в очереди
    store.set_state(conn, "session", "ok")
    assert "обновляется" not in web.client.get("/history").text
    store.enqueue_once(conn, "refresh_history", None, clock.iso())
    assert "обновляется" in web.client.get("/history").text


@pytest.mark.parametrize("ref, app_id", [
    ("492224193", 492224193),
    ("  id492224193 ", 492224193),
    ("https://apps.apple.com/ru/app/сбербанк-онлайн/id492224193", 492224193),
    ("https://apps.apple.com/ru/app/x/id492224193?l=en-GB", 492224193),
    ("", None), ("сбербанк", None), ("https://example.com/app", None), ("12", None),
])
def test_parse_app_id(ref, app_id):
    assert parse_app_id(ref) == app_id


def test_add_by_link_publishes(web, conn):
    r = web.client.post("/blocked/add", data={"ref": "https://apps.apple.com/ru/app/x/id492224193"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert [a.app_id for a in store.list_apps(conn)] == [492224193]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish'").fetchone()[0] == 1


def test_add_by_bad_link_explains(web, conn):
    r = web.client.post("/blocked/add", data={"ref": "сбербанк"})
    assert r.status_code == 400 and "ссылку App Store или ID" in r.text and store.list_apps(conn) == []


def test_refresh_also_checks_removed_and_marks_them(web, conn, clock):
    web.client.post("/history/refresh")
    assert sorted(r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='queued'")) == \
        ["check_removed", "refresh_history"]
    store.add_known(conn, 492224193, "СберБанк Онлайн")
    r = web.client.get("/history", params={"q": "сбер"})
    assert "СберБанк Онлайн" in r.text and "удалено из App Store" in r.text


def test_history_says_when_only_catalog_check_runs(web, conn, clock):
    store.set_state(conn, "session", "ok")
    store.enqueue_once(conn, "check_removed", None, clock.iso())
    r = web.client.get("/history")
    assert "проверяю справочник удалённых" in r.text


def test_history_search_uses_catalog_aliases(web, conn, monkeypatch):  # «vk», «max» латиницей
    from appshelf import removed
    monkeypatch.setattr(removed, "load", lambda: [{"id": 564177498, "name": "ВКонтакте", "aliases": ["vk", "вк"]},
                                                  {"id": 6739530834, "name": "МАКС", "aliases": ["max", "макс"]}])
    store.add_known(conn, 564177498, "ВКонтакте")
    store.add_known(conn, 6739530834, "МАКС")
    assert "ВКонтакте" in web.client.get("/history", params={"q": "vk"}).text
    r = web.client.get("/history", params={"q": "MAX"})
    assert "МАКС" in r.text and "ВКонтакте" not in r.text


def names_in(html: str) -> list[str]:
    import re
    return re.findall(r"<b>([^<]*)</b>", html)


def test_catalog_sorted_alphabetically(web, conn, cfg, clock):
    for app_id, name in ((1, "Яндекс"), (2, "альфа"), (3, "VK")):
        seed_app(conn, cfg, clock, app_id=app_id, name=name, bundle_id=f"b{app_id}")
    assert names_in(web.client.get("/").text) == ["VK", "альфа", "Яндекс"]


def test_history_sorts_and_shows_version(web, conn, clock):
    store.upsert_purchases(conn, [
        {"id": 1, "bundleId": "b.one", "name": "Бета", "version": "2.10", "purchaseDate": "2020-01-01T00:00:00Z"},
        {"id": 2, "bundleId": "a.two", "name": "Альфа", "version": "2.9", "purchaseDate": "2024-01-01T00:00:00Z"},
        {"id": 3, "bundleId": "c.three", "name": "Гамма", "version": "10.0", "purchaseDate": "2022-01-01T00:00:00Z"}],
        clock.iso())
    assert names_in(web.client.get("/history").text) == ["Альфа", "Бета", "Гамма"]
    assert names_in(web.client.get("/history", params={"sort": "date"}).text) == ["Альфа", "Гамма", "Бета"]
    assert names_in(web.client.get("/history", params={"sort": "version"}).text) == ["Гамма", "Бета", "Альфа"]
    assert names_in(web.client.get("/history", params={"sort": "name", "dir": "desc"}).text) == ["Гамма", "Бета", "Альфа"]
    assert "2.10" in web.client.get("/history").text


def test_blocked_page_marks_apps_missing_from_account(web, conn, clock, monkeypatch):
    from appshelf import removed
    monkeypatch.setattr(removed, "load", lambda: [{"id": 1, "name": "СберБанк Онлайн"}, {"id": 2, "name": "ВТБ Онлайн"},
                                                  {"id": 3, "name": "Альфа-Банк"}])
    store.add_known(conn, 1, "СберБанк Онлайн")
    store.mark_no_license(conn, 2, clock.iso())
    r = web.client.get("/blocked")
    assert r.status_code == 200
    assert "Есть в аккаунте: 1" in r.text and "нет в аккаунте: 1" in r.text and "не проверено: 1" in r.text
    assert 'class="row inactive"' in r.text and "/apps/1/publish" in r.text and "/apps/2/publish" not in r.text


def test_theme_switch_sets_cookie(web):
    r = web.client.get("/theme", params={"set": "light"}, headers={"referer": "https://apps.example/history?q=x"},
                       follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/history?q=x" and "theme=light" in r.headers["set-cookie"]
    web.client.cookies.set("theme", "light")
    assert 'data-theme="light"' in web.client.get("/").text
