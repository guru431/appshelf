import io
import re

import pytest
from PIL import Image
from starlette.testclient import TestClient

from appshelf import store
from appshelf.config import GB
from appshelf.web.app import parse_app_id
from helpers import HOST, TOKEN, make_account, seed_app, set_session, sign_in


def test_healthz_without_login(web):
    r = TestClient(web.app).get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_pwa_manifest_and_icons_without_login(web):
    # iOS «На экран „Домой“» и браузеры берут манифест и иконку без cookie
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


def test_catalog_banner_when_archive_unavailable(web, cfg):
    cfg.archive.rmdir()  # шара не смонтирована
    r = web.client.get("/")
    assert r.status_code == 200 and "Архив недоступен" in r.text and TOKEN not in r.text.split("<main>")[1]


def test_cross_origin_post_forbidden(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0",))
    r = web.client.post("/apps/123/unpublish", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and store.list_apps(conn, 1)


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
    set_session(conn, 1, "expired")
    web.free["bytes"] = 2 * GB
    r = web.client.get("/")
    assert "Вход в Apple ID owner@example истёк" in r.text and "Мало места" in r.text
    assert "/login?email=owner%40example&amp;next=%2F" in r.text


def test_catalog_shows_only_own_nightly_result(web, conn, cfg, clock):  # адреса чужих Apple ID участнику не видны
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    store.set_state(conn, "nightly_last", clock.iso())
    store.set_state(conn, "nightly_result", "owner@example: обновлено 2, без изменений 0, ошибок 0 | "
                                            "petr@example: обновлено 0, без изменений 1, ошибок 0")
    member = TestClient(web.app, base_url=f"https://{HOST}")
    sign_in(member, cfg, conn, clock, petr.user_id)
    text = member.get("/").text
    assert "обновлено 0, без изменений 1" in text and "owner@example" not in text
    assert "обновлено 2, без изменений 0" in web.client.get("/").text


def test_catalog_explains_app_store_apple_id(web):
    assert "На iPhone в App Store должен быть выполнен вход этим же Apple ID" in web.client.get("/").text


def test_history_search_cyrillic_case_insensitive(web, conn, clock):
    store.upsert_purchases(conn, 1, [{"id": 1, "bundleId": "ru.sberbank.onlineiphone", "name": "СберБанк Онлайн"},
                                     {"id": 2, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    r = web.client.get("/history", params={"q": "сбер"})
    assert "СберБанк Онлайн" in r.text and "com.vk.vkclient" not in r.text


def test_publish_and_unpublish(web, conn, cfg, clock, acct):
    store.upsert_purchases(conn, 1, [{"id": 7, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    assert web.client.post("/apps/7/publish", follow_redirects=False).status_code == 303
    assert web.client.post("/apps/7/publish", follow_redirects=False).status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish'").fetchone()[0] == 1
    assert "в каталоге" in web.client.get("/history").text
    root = cfg.shelf_root(acct)
    (root / "7-1.0").mkdir(parents=True)
    store.add_version(conn, 1, root, 7, store.NewVersion("1.0", "1", "900", "13.0", "iphone", 1, "7-1.0"), clock.iso())
    web.client.post("/apps/7/unpublish")
    assert store.list_apps(conn, 1) == [] and not (root / "7-1.0").exists()


def test_api_status(web, conn, cfg, clock):
    seed_app(conn, cfg, clock)
    set_session(conn, 1, "ok")
    assert web.client.get("/api/status").json() == {"busy": True, "session": "ok", "apps": {"123": "queued"}}


def test_history_shows_failed_refresh(web, conn, clock):
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "refresh_history", None, clock.iso())
    job = store.take_job(conn)
    store.finish_job(conn, job["id"], "error", clock.iso(),
                     "ipatool: purchase history update response returned DAAP status 500")
    r = web.client.get("/history")
    assert "не удалось обновить" in r.text and "DAAP status 500" in r.text


def test_history_refreshing_only_for_refresh_jobs(web, conn, cfg, clock):
    seed_app(conn, cfg, clock)  # задание publish в очереди
    set_session(conn, 1, "ok")
    assert "обновляется" not in web.client.get("/history").text
    store.enqueue_once(conn, 1, "refresh_history", None, clock.iso())
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
    assert [a.app_id for a in store.list_apps(conn, 1)] == [492224193]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish'").fetchone()[0] == 1


def test_add_by_bad_link_explains(web, conn):
    r = web.client.post("/blocked/add", data={"ref": "сбербанк"})
    assert r.status_code == 400 and "ссылку App Store или ID" in r.text and store.list_apps(conn, 1) == []


def test_refresh_also_checks_removed_and_marks_them(web, conn, clock):
    web.client.post("/history/refresh")
    assert sorted(r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='queued'")) == \
        ["check_removed", "refresh_history"]
    store.add_known(conn, 1, 492224193, "СберБанк Онлайн")
    r = web.client.get("/history", params={"q": "сбер"})
    assert "СберБанк Онлайн" in r.text and "удалено из App Store" in r.text


def test_history_says_when_only_catalog_check_runs(web, conn, clock):
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    assert "проверяю справочник удалённых" in web.client.get("/history").text


def test_history_search_uses_catalog_aliases(web, conn, monkeypatch):  # «vk», «max» латиницей
    from appshelf import removed
    monkeypatch.setattr(removed, "load", lambda: [{"id": 564177498, "name": "ВКонтакте", "aliases": ["vk", "вк"]},
                                                  {"id": 6739530834, "name": "МАКС", "aliases": ["max", "макс"]}])
    store.add_known(conn, 1, 564177498, "ВКонтакте")
    store.add_known(conn, 1, 6739530834, "МАКС")
    assert "ВКонтакте" in web.client.get("/history", params={"q": "vk"}).text
    r = web.client.get("/history", params={"q": "MAX"})
    assert "МАКС" in r.text and "ВКонтакте" not in r.text


def names_in(html: str) -> list[str]:
    return re.findall(r"<b>([^<]*)</b>", html)


def test_catalog_sorted_alphabetically(web, conn, cfg, clock):
    for app_id, name in ((1, "Яндекс"), (2, "альфа"), (3, "VK")):
        seed_app(conn, cfg, clock, app_id=app_id, name=name, bundle_id=f"b{app_id}")
    assert names_in(web.client.get("/").text) == ["VK", "альфа", "Яндекс"]


def test_history_sorts_and_shows_version(web, conn, clock):
    store.upsert_purchases(conn, 1, [
        {"id": 1, "bundleId": "b.one", "name": "Бета", "version": "2.10", "purchaseDate": "2020-01-01T00:00:00Z"},
        {"id": 2, "bundleId": "a.two", "name": "Альфа", "version": "2.9", "purchaseDate": "2024-01-01T00:00:00Z"},
        {"id": 3, "bundleId": "c.three", "name": "Гамма", "version": "10.0", "purchaseDate": "2022-01-01T00:00:00Z"}],
        clock.iso())
    assert names_in(web.client.get("/history").text) == ["Альфа", "Бета", "Гамма"]
    assert names_in(web.client.get("/history", params={"sort": "date"}).text) == ["Альфа", "Гамма", "Бета"]
    assert names_in(web.client.get("/history", params={"sort": "version"}).text) == ["Гамма", "Бета", "Альфа"]
    assert names_in(web.client.get("/history", params={"sort": "name", "dir": "desc"}).text) == \
        ["Гамма", "Бета", "Альфа"]
    assert "2.10" in web.client.get("/history").text


def test_blocked_page_marks_apps_missing_from_account(web, conn, clock, monkeypatch):
    from appshelf import removed
    monkeypatch.setattr(removed, "load", lambda: [{"id": 1, "name": "СберБанк Онлайн"}, {"id": 2, "name": "ВТБ Онлайн"},
                                                  {"id": 3, "name": "Альфа-Банк"}])
    store.add_known(conn, 1, 1, "СберБанк Онлайн")
    store.mark_no_license(conn, 1, 2, clock.iso())
    r = web.client.get("/blocked")
    assert r.status_code == 200
    assert "Есть в аккаунте: 1" in r.text and "нет в аккаунте: 1" in r.text and "не проверено: 1" in r.text
    assert 'class="row inactive"' in r.text and "/apps/1/publish" in r.text and "/apps/2/publish" not in r.text


def test_theme_switch_sets_cookie(web):
    r = web.client.get("/theme", params={"set": "light"}, headers={"referer": "https://apps.example/history?q=x"},
                       follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/history?q=x" and "theme=light" in r.headers["set-cookie"]
    assert 'data-theme="light"' in web.client.get("/").text
