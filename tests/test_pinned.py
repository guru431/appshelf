"""Закреплённая версия: переживает ротацию двух слотов в роли kept; закрепление и снятие из каталога."""
from contextlib import contextmanager

import pytest

from appshelf import jobs, people, store
from helpers import make_account, seed_app


def vid(conn, version, app_id=123):
    return conn.execute("SELECT id FROM versions WHERE app_id=? AND version=?", (app_id, version)).fetchone()["id"]


def roles(conn, app_id=123):
    return {r["version"]: (r["role"], r["pinned"]) for r in
            conn.execute("SELECT version, role, pinned FROM versions WHERE app_id=?", (app_id,))}


def update(conn, cfg, clock, acct, version, app_id=123):
    """Ещё одно обновление: каталог версии и add_version, как после скачивания."""
    d = f"{app_id}-{version}"
    (cfg.shelf_root(acct) / d).mkdir(parents=True)
    store.add_version(conn, acct.id, cfg.shelf_root(acct), app_id, store.NewVersion(
        version=version, build="1", external_version_id="999", min_ios="13.0", device_family="iphone",
        size=100 * 1024 * 1024, dir=d), clock.iso())


def test_third_update_keeps_pinned_previous(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    root = cfg.shelf_root(acct)
    assert store.pin_version(conn, acct.id, root, 123, vid(conn, "1.0"))
    update(conn, cfg, clock, acct, "3.0")
    assert roles(conn) == {"1.0": ("kept", 1), "2.0": ("previous", 0), "3.0": ("current", 0)}
    update(conn, cfg, clock, acct, "4.0")                  # обычная previous уходит, kept — нет
    assert roles(conn) == {"1.0": ("kept", 1), "3.0": ("previous", 0), "4.0": ("current", 0)}
    assert (root / "123-1.0").is_dir() and not (root / "123-2.0").exists()
    (a,) = store.list_apps(conn, acct.id)
    assert a.kept["version"] == "1.0" and a.previous["version"] == "3.0"
    assert store.shelf_size(conn, acct.id) == 291 * 1024 * 1024 + 2 * 100 * 1024 * 1024


def test_pinned_current_becomes_kept_after_two_updates(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0",), make_dirs=True)
    store.pin_version(conn, acct.id, cfg.shelf_root(acct), 123, vid(conn, "1.0"))
    update(conn, cfg, clock, acct, "2.0")
    assert roles(conn)["1.0"] == ("previous", 1)
    update(conn, cfg, clock, acct, "3.0")
    assert roles(conn) == {"1.0": ("kept", 1), "2.0": ("previous", 0), "3.0": ("current", 0)}
    assert (cfg.shelf_root(acct) / "123-1.0").is_dir()


def test_pin_other_version_drops_kept(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    root = cfg.shelf_root(acct)
    store.pin_version(conn, acct.id, root, 123, vid(conn, "1.0"))
    update(conn, cfg, clock, acct, "3.0")
    assert store.pin_version(conn, acct.id, root, 123, vid(conn, "3.0"))
    assert roles(conn) == {"2.0": ("previous", 0), "3.0": ("current", 1)}
    assert not (root / "123-1.0").exists()
    store.pin_version(conn, acct.id, root, 123, vid(conn, "2.0"))   # между current и previous — только флаг
    assert roles(conn) == {"2.0": ("previous", 1), "3.0": ("current", 0)}


def test_unpin(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    root = cfg.shelf_root(acct)
    store.pin_version(conn, acct.id, root, 123, vid(conn, "1.0"))
    assert store.unpin_version(conn, acct.id, root, 123, vid(conn, "1.0"))
    assert roles(conn)["1.0"] == ("previous", 0) and (root / "123-1.0").is_dir()
    store.pin_version(conn, acct.id, root, 123, vid(conn, "1.0"))
    update(conn, cfg, clock, acct, "3.0")
    assert store.unpin_version(conn, acct.id, root, 123, vid(conn, "1.0"))
    assert "1.0" not in roles(conn) and not (root / "123-1.0").exists()


def test_unpublish_removes_kept(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    root = cfg.shelf_root(acct)
    store.pin_version(conn, acct.id, root, 123, vid(conn, "1.0"))
    update(conn, cfg, clock, acct, "3.0")
    jobs.unpublish(cfg, conn, acct, 123)
    assert roles(conn) == {} and not (root / "123-1.0").exists()


@pytest.mark.parametrize("change", ["pin", "unpin", "unpublish", "remove"])
def test_shelf_change_reloads_account_after_obtaining_lock(conn, cfg, clock, acct, monkeypatch, change):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    store.pin_version(conn, acct.id, cfg.shelf_root(acct), 123, vid(conn, "1.0"))
    update(conn, cfg, clock, acct, "3.0")
    real_hold, moved, inside = jobs.hold, [], []

    @contextmanager
    def hold(path, wait=None):
        if path == cfg.download_lock and not moved:
            moved.append(True)
            jobs.move_shelf(cfg, conn, acct)  # перенос завершился, пока действие ждало замок
        with real_hold(path, wait):
            inside.append(path)
            try:
                yield
            finally:
                inside.pop()

    monkeypatch.setattr(jobs, "hold", hold)
    real_remove = store.remove_dirs

    def remove(root, names):
        assert cfg.download_lock in inside  # перенос не может вклиниться между COMMIT и удалением файлов
        real_remove(root, names)

    monkeypatch.setattr(store, "remove_dirs", remove)
    if change == "pin":
        assert jobs.pin_version(cfg, conn, acct, 123, vid(conn, "3.0"))
    elif change == "unpin":
        assert jobs.unpin_version(cfg, conn, acct, 123, vid(conn, "1.0"))
    elif change == "remove":
        row = store.old_version(conn, acct.id, 123, vid(conn, "1.0"))
        assert jobs.remove_version(cfg, conn, acct, 123, row["id"], store.version_snapshot(row))
    else:
        jobs.unpublish(cfg, conn, acct, 123)
    fresh = people.get_account(conn, acct.id)
    assert cfg.shelf_root(fresh) != cfg.shelf_root(acct)
    assert not (cfg.shelf_root(fresh) / "123-1.0").exists()


def test_catalog_shows_kept_and_pin_menu(web, conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    v1, v2 = vid(conn, "1.0"), vid(conn, "2.0")
    r = web.client.post(f"/apps/123/versions/{v1}/pin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?os=apple"
    text = web.client.get("/").text
    assert "закреплена" in text and f"/apps/123/versions/{v1}/unpin" in text and f"/apps/123/versions/{v2}/pin" in text
    update(conn, cfg, clock, acct, "3.0")
    text = web.client.get("/").text
    kept_url = cfg.shelf_url(acct, "123-1.0", "manifest.plist")
    assert f'сохранённая: <a href="itms-services://?action=download-manifest&amp;url={kept_url}">1.0</a> · 291 МБ' in text
    assert "Снять закрепление 1.0" in text and f"/apps/123/versions/{v1}/pin" not in text
    assert web.client.post(f"/apps/123/versions/{v1}/unpin", follow_redirects=False).status_code == 303
    assert "сохранённая:" not in web.client.get("/").text and not (cfg.shelf_root(acct) / "123-1.0").exists()


def test_pin_foreign_version_not_found(web, conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0",))
    seed_app(conn, cfg, clock, app_id=456, name="ВТБ", bundle_id="ru.vtb", versions=("5.0",))
    other = make_account(conn, clock, email="other@example", role="member", legacy=False)
    seed_app(conn, cfg, clock, app_id=789, name="Альфа", bundle_id="ru.alfa", versions=("7.0",), acct=other)
    for path in (f"/apps/123/versions/{vid(conn, '5.0', 456)}/pin",      # версия другого приложения
                 f"/apps/789/versions/{vid(conn, '7.0', 789)}/pin",      # чужая полка
                 f"/apps/789/versions/{vid(conn, '7.0', 789)}/unpin",
                 "/apps/123/versions/99999/unpin"):
        assert web.client.post(path).status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM versions WHERE pinned").fetchone()[0] == 0


def test_unpin_kept_refused_while_archive_unavailable(web, conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
    store.pin_version(conn, acct.id, cfg.shelf_root(acct), 123, vid(conn, "1.0"))
    update(conn, cfg, clock, acct, "3.0")
    hidden = cfg.archive.with_name("pub-unmounted")
    cfg.archive.rename(hidden)                             # шара отвалилась: каталог остался бы без строки
    r = web.client.post(f"/apps/123/versions/{vid(conn, '1.0')}/unpin")
    assert r.status_code == 503 and "изменить закрепление сейчас нельзя" in r.text
    assert roles(conn)["1.0"] == ("kept", 1)
    hidden.rename(cfg.archive)
    assert (cfg.shelf_root(acct) / "123-1.0").is_dir()
