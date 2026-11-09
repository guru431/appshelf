"""Каталог и «История» Apple: занятая полка, действия со старой вкладки, метки и фильтры состояния."""
import re

from appshelf import ipatool, jobs, store
from helpers import make_account, seed_app, sign_in


def test_unpublish_while_shelf_busy_says_so(web, conn, cfg, clock, monkeypatch):
    seed_app(conn, cfg, clock, versions=("1.0",))
    monkeypatch.setattr(jobs, "REMOVE_LOCK_WAIT", 0)
    with ipatool.hold(cfg.download_lock):                  # чьё-то скачивание IPA или APK
        r = web.client.post("/apps/123/unpublish")
    assert r.status_code == 503 and "Полка занята скачиванием" in r.text
    assert store.list_apps(conn, 1) and store.current_version(conn, 1, 123)


def test_history_keeps_removed_mark_after_first_download(web, conn, clock):
    store.add_known(conn, 1, 11, "Банк", "license")        # нашла проверка справочника
    store.add_known(conn, 1, 12, "App Store 12", "manual")  # по ссылке, лицензия не подтверждена
    store.upsert_purchases(conn, 1, [{"id": 13, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    for app_id in (11, 12):                                # первое скачивание подставило имя и bundle id
        store.fill_names(conn, 1, app_id, f"Приложение {app_id}", f"ru.app{app_id}")
    rows = {m.group(1): m.group(2) for m in re.finditer(
        r"<b>([^<]+)</b><div class=\"meta\">(.*?)</div></div>", web.client.get("/history").text, re.S)}
    assert "удалено из App Store" in rows["Приложение 11"] and "ru.app11" in rows["Приложение 11"]
    assert "по ссылке, не подтверждено" in rows["Приложение 12"]
    assert "tag" not in rows["VK"]


def test_action_from_stale_tab_shows_its_shelf(web, conn, cfg, clock):
    store.upsert_purchases(conn, 1, [{"id": 7, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    other = make_account(conn, clock, "other@example", role="member", legacy=False)
    sign_in(web.client, cfg, conn, clock, other.user_id)
    web.client.get(f"/acct/{other.id}", follow_redirects=False)   # другая вкладка переключила полку
    r = web.client.post("/apps/7/publish", params={"account": 1})  # форма вкладки «Истории» полки №1
    assert r.status_code == 200 and r.context["acct"].id == 1 and "VK" in r.text
    assert web.client.get("/").context["acct"].id == 1


def test_stale_version_confirmation_asks_again(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    old = conn.execute("SELECT * FROM versions WHERE role='previous'").fetchone()
    url = f"/apps/123/versions/{old['id']}/remove"
    snapshot = re.search(r'name="snapshot" value="([a-f0-9]{64})"', web.client.get(url).text).group(1)
    conn.execute("UPDATE versions SET pinned=1 WHERE id=?", (old["id"],))   # закрепили в другой вкладке
    r = web.client.post(url, data={"snapshot": snapshot})
    assert r.status_code == 409 and "Версия изменилась" in r.text and "Закрепление будет снято" in r.text
    fresh = re.search(r'name="snapshot" value="([a-f0-9]{64})"', r.text).group(1)
    assert fresh != snapshot and store.old_version(conn, 1, 123, old["id"])
    assert web.client.post(url, data={"snapshot": fresh}, follow_redirects=False).status_code == 303
    gone = web.client.post(url, data={"snapshot": fresh})                   # уже удалена: страница, не голый текст
    assert gone.status_code == 404 and gone.headers["content-type"].startswith("text/html")
    assert "Версия не найдена" in gone.text and 'href="/"' in gone.text
    assert web.client.get(url).status_code == 404 and "Версия не найдена" in web.client.get(url).text


def updated_app(conn, cfg, clock):
    """Приложение скачано впервые (задание publish завершено), через час — обновление: previous и current."""
    seed_app(conn, cfg, clock, versions=("1.0",), make_dirs=True)
    finish_publish(conn, clock)
    clock.advance(3600)
    root = cfg.shelf_root(store_acct(conn))
    (root / "123-1.1").mkdir()
    store.add_version(conn, 1, root, 123, store.NewVersion("1.1", "1", "901", "13.0", "iphone", 1, "123-1.1"),
                      clock.iso())


def finish_publish(conn, clock):
    """Задание publish выполнено обработчиком: в тот же миг, что и запись версии."""
    conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE kind='publish'", (clock.iso(),))


def store_acct(conn):
    from appshelf import people
    return people.get_account(conn, 1)


def test_new_version_mark_survives_removal_of_previous(web, conn, cfg, clock):
    updated_app(conn, cfg, clock)
    assert "новая версия ·" in web.client.get("/").text
    old = conn.execute("SELECT * FROM versions WHERE role='previous'").fetchone()
    assert store.remove_version(conn, 1, cfg.shelf_root(store_acct(conn)), 123, old["id"], store.version_snapshot(old))
    text = web.client.get("/").text
    assert "новая версия ·" in text and "123-1.0" not in text
    assert "СберБанк" in web.client.get("/", params={"os": "apple", "filter": "recent"}).text


def test_first_download_is_not_new_version(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0",))
    finish_publish(conn, clock)
    clock.advance(60)
    assert "новая версия" not in web.client.get("/").text


def test_catalog_state_links_only_nonzero(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, app_id=1, name="Работает", bundle_id="b1", versions=("1.0",))
    seed_app(conn, cfg, clock, app_id=2, name="Ошибка", bundle_id="b2")
    store.set_app_status(conn, 1, 2, "error", "ipatool: timeout")
    seed_app(conn, cfg, clock, app_id=3, name="Ждёт", bundle_id="b3")       # queued
    text = web.client.get("/").text
    links = dict(re.findall(r'<a[^>]*href="/\?os=apple&amp;filter=(\w+)"[^>]*>([^<]+)</a>', text))
    assert set(links) == {"errors", "active"} and "1" in links["errors"] and "1" in links["active"]
    for mode, name in (("errors", "Ошибка"), ("active", "Ждёт")):
        page = web.client.get("/", params={"os": "apple", "filter": mode}).text
        assert name in page and "Работает" not in page


def test_check_all_updates_once(web, conn, cfg, clock):
    for app_id in (1, 2):
        seed_app(conn, cfg, clock, app_id=app_id, name=f"A{app_id}", bundle_id=f"b{app_id}", versions=("1.0",))
    seed_app(conn, cfg, clock, app_id=3, name="Без версии", bundle_id="b3")
    conn.execute("UPDATE jobs SET status='done'")
    other = make_account(conn, clock, "other@example", role="member", legacy=False)
    seed_app(conn, cfg, clock, app_id=4, name="Чужое", bundle_id="b4", versions=("1.0",), acct=other)
    conn.execute("UPDATE jobs SET status='done'")
    assert "Проверить обновления всех" in web.client.get("/").text
    for _ in range(2):                                     # двойной клик — по заданию на приложение
        assert web.client.post("/apps/check", follow_redirects=False).status_code == 303
    queued = conn.execute("SELECT account_id, app_id FROM jobs WHERE status='queued' ORDER BY app_id").fetchall()
    assert [tuple(r) for r in queued] == [(1, 1), (1, 2)]
    assert {a.app_id: a.status for a in store.list_apps(conn, 1)} == {1: "queued", 2: "queued", 3: "queued"}


def test_action_on_missing_app_is_a_page_with_way_back(web, conn, cfg, clock):
    r = web.client.post("/apps/999/unpublish")              # сняли в другой вкладке
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Приложение не найдено" in r.text and 'href="/"' in r.text


def test_app_gone_during_action_is_a_page(web, conn, cfg, clock, monkeypatch):
    seed_app(conn, cfg, clock, versions=("1.0",))
    def gone(*args, **kwargs):
        raise store.AppGone(123)
    monkeypatch.setattr(store, "check_update", gone)
    r = web.client.post("/apps/123/check")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "больше не существует" in r.text


def test_pin_when_share_fails_during_move_is_503(web, conn, cfg, clock, monkeypatch):
    seed_app(conn, cfg, clock, versions=("1.0", "1.1"))
    old = conn.execute("SELECT id FROM versions WHERE app_id=123 AND role='previous'").fetchone()[0]
    def unavailable(*args, **kwargs):
        raise jobs.ArchiveUnavailable("архив недоступен")
    monkeypatch.setattr(jobs, "pin_version", unavailable)
    r = web.client.post(f"/apps/123/versions/{old}/pin")
    assert r.status_code == 503 and "Архив недоступен" in r.text
