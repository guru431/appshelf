"""Confirmed file deletion on Apple, personal Android and public shelves."""
import html
import re
import sqlite3
from types import SimpleNamespace

import pytest

from appshelf import store
from appshelf.android import db, fetch
from helpers import make_account, seed_app, sign_in


@pytest.fixture(params=["apple", "android", "public"])
def saved_versions(request, web, conn, cfg, clock, acct):
    platform = request.param
    if platform == "apple":
        seed_app(conn, cfg, clock, versions=("1.0", "2.0"), make_dirs=True)
        table, root, prefix = "versions", cfg.shelf_root(acct), "/apps/123"
        shelf = None
    else:
        shelf = (db.ensure_public_shelf(conn, clock.iso()) if platform == "public"
                 else db.ensure_shelf(conn, acct.user_id, clock.iso()))
        db.put_app(conn, shelf.id, "ru.example.app", "Example", "upload", "", clock.iso())
        root = fetch.shelf_root(cfg, shelf)
        for n in (1, 2):
            name = f"ru.example.app-{n}"
            (root / name).mkdir(parents=True)
            db.add_version(conn, root, shelf.id, "ru.example.app", db.NewApk(
                f"{n}.0", n, 26, 34, "", "a" * 64, "upload", 10, str(n) * 64, name), clock.iso())
        table = "android_versions"
        prefix = ("/android/public" if platform == "public" else "/android") + "/apps/ru.example.app"
    rows = {r["role"]: r for r in conn.execute(f"SELECT * FROM {table}")}
    for row in rows.values():
        (root / row["dir"] / "payload").write_bytes(b"saved version")
    return SimpleNamespace(platform=platform, table=table, root=root, prefix=prefix, shelf=shelf,
                           old=rows["previous"], current=rows["current"])


def remove_url(saved, row=None):
    row = saved.old if row is None else row
    return f"{saved.prefix}/versions/{row['id']}/remove"


def snapshot_from(page):
    assert page.status_code == 200
    return re.search(r'name="snapshot" value="([a-f0-9]{64})"', page.text).group(1)


def test_confirmed_removal_of_pinned_old_version(web, conn, cfg, saved_versions):
    s = saved_versions
    conn.execute(f"UPDATE {s.table} SET pinned=1 WHERE id=?", (s.old["id"],))
    page = web.client.get(remove_url(s))
    snapshot = snapshot_from(page)
    assert "Закрепление будет снято" in page.text and "Удалить версию" in page.text
    assert (s.root / s.old["dir"] / "payload").is_file()  # GET only asks.
    assert web.client.post(remove_url(s)).status_code == 400
    result = web.client.post(remove_url(s), data={"snapshot": snapshot}, follow_redirects=False)
    assert result.status_code == 303
    assert conn.execute(f"SELECT 1 FROM {s.table} WHERE id=?", (s.old["id"],)).fetchone() is None
    assert not (s.root / s.old["dir"]).exists()
    assert (s.root / s.current["dir"] / "payload").is_file()
    assert conn.execute(f"SELECT role FROM {s.table} WHERE id=?", (s.current["id"],)).fetchone()[0] == "current"
    if s.shelf:
        link = fetch.app_url(cfg, s.shelf, "ru.example.app")
        assert web.client.get(link + f"?version={s.old['id']}", follow_redirects=False).status_code == 404
        assert web.client.get(link, follow_redirects=False).status_code == 302


def test_current_version_and_wrong_app_are_protected(web, conn, saved_versions):
    s = saved_versions
    urls = [remove_url(s, s.current), remove_url(s).replace("/apps/123/", "/apps/456/")
            if s.platform == "apple" else remove_url(s).replace("ru.example.app/", "ru.other.app/")]
    for url in urls:
        assert web.client.get(url).status_code == 404
        assert web.client.post(url, data={"snapshot": store.version_snapshot(s.current)}).status_code == 404
    assert all((s.root / r["dir"] / "payload").is_file() for r in (s.old, s.current))
    assert conn.execute(f"SELECT COUNT(*) FROM {s.table}").fetchone()[0] == 2


def test_pending_apk_cannot_be_deleted_as_history(web, conn, saved_versions):
    s = saved_versions
    if s.platform == "apple":
        pytest.skip("Apple versions have no pending role")
    conn.execute("UPDATE android_versions SET role='pending' WHERE id=?", (s.old["id"],))
    assert web.client.get(remove_url(s)).status_code == 404
    assert web.client.post(remove_url(s), data={"snapshot": store.version_snapshot(s.old)}).status_code == 404
    assert (s.root / s.old["dir"] / "payload").is_file()


def test_stale_confirmation_cannot_remove_replacement_file(web, conn, saved_versions):
    s = saved_versions
    snapshot = snapshot_from(web.client.get(remove_url(s)))
    # A manual import can reuse a removed SQLite id for another historical APK.
    conn.execute(f"UPDATE {s.table} SET sha256=? WHERE id=?", ("b" * 64, s.old["id"]))
    # Apple asks again with the current snapshot (409); Android still answers 404.
    assert web.client.post(remove_url(s), data={"snapshot": snapshot}).status_code == (
        409 if s.platform == "apple" else 404)
    assert (s.root / s.old["dir"] / "payload").is_file()


def test_removal_failure_keeps_database_and_files(web, conn, saved_versions):
    s = saved_versions
    snapshot = snapshot_from(web.client.get(remove_url(s)))
    conn.execute(f"CREATE TRIGGER fail_delete BEFORE DELETE ON {s.table} "
                 "BEGIN SELECT RAISE(ABORT, 'delete failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="delete failed"):
        web.client.post(remove_url(s), data={"snapshot": snapshot})
    assert conn.execute(f"SELECT 1 FROM {s.table} WHERE id=?", (s.old["id"],)).fetchone()
    assert (s.root / s.old["dir"] / "payload").is_file()


def test_unavailable_archive_does_not_drop_version(web, conn, cfg, saved_versions):
    s = saved_versions
    snapshot = snapshot_from(web.client.get(remove_url(s)))
    moved = cfg.archive.with_name("unmounted")
    cfg.archive.rename(moved)
    try:
        assert web.client.post(remove_url(s), data={"snapshot": snapshot}).status_code == 503
        assert conn.execute(f"SELECT 1 FROM {s.table} WHERE id=?", (s.old["id"],)).fetchone()
    finally:
        moved.rename(cfg.archive)
    assert (s.root / s.old["dir"] / "payload").is_file()


def test_foreign_member_and_cross_origin_cannot_remove(web, make_web, conn, cfg, clock, saved_versions):
    s = saved_versions
    snapshot = snapshot_from(web.client.get(remove_url(s)))
    stranger = make_account(conn, clock, "stranger@example", role="member", legacy=False)
    other = make_web(cfg, conn)
    sign_in(other.client, cfg, conn, clock, stranger.user_id)
    assert other.client.get(remove_url(s)).status_code in (403, 404)
    assert other.client.post(remove_url(s), data={"snapshot": snapshot}).status_code in (403, 404)
    assert web.client.post(remove_url(s), data={"snapshot": snapshot},
                           headers={"Origin": "https://other.example"}).status_code == 403
    assert (s.root / s.old["dir"] / "payload").is_file()


def test_confirmation_keeps_target_after_another_tab_switches_shelf(web, conn, cfg, clock, saved_versions):
    s = saved_versions
    page = web.client.get(remove_url(s))
    snapshot = snapshot_from(page)
    action = html.unescape(re.search(r'<form method="post" action="([^"]+)"', page.text).group(1))
    other = make_account(conn, clock, "other@example", role="member", legacy=False)
    sign_in(web.client, cfg, conn, clock, other.user_id)
    web.client.get(f"/acct/{other.id}", follow_redirects=False)
    web.client.get(f"/who/{other.user_id}", follow_redirects=False)
    response = web.client.request("POST", action, data={"snapshot": snapshot}, follow_redirects=False)
    assert response.status_code == 303
    assert not (s.root / s.old["dir"]).exists() and (s.root / s.current["dir"]).is_dir()
