import pytest

from appshelf import store
from helpers import seed_app


def test_rotation_keeps_current_and_previous_and_removes_older(conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0", "1.1", "1.2"), make_dirs=True)
    roles = {r["role"]: r["version"] for r in conn.execute("SELECT * FROM versions")}
    assert roles == {"current": "1.2", "previous": "1.1"}
    assert not (cfg.pub_root / "123-1.0").exists()
    assert (cfg.pub_root / "123-1.1").exists() and (cfg.pub_root / "123-1.2").exists()
    assert store.list_apps(conn)[0].status == "ok"


def test_add_version_after_unpublish_raises(conn, cfg, clock):
    seed_app(conn, cfg, clock)
    store.unpublish(conn, cfg.pub_root, 123)
    with pytest.raises(store.AppGone):
        store.add_version(conn, cfg.pub_root, 123, store.NewVersion(
            "1.0", "1", "900", "13.0", "iphone", 1, "123-1.0"), clock.iso())
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0


def test_unpublish_removes_dirs_cancels_jobs_keeps_history(conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0", "1.1"), make_dirs=True)
    store.unpublish(conn, cfg.pub_root, 123)
    assert list(cfg.pub_root.iterdir()) == []
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "cancelled"
    assert [r["published"] for r in store.list_purchases(conn)] == [0]


def test_double_click_creates_one_job(conn, cfg, clock):  # Review Focus 1
    seed_app(conn, cfg, clock)
    assert store.publish(conn, 123, clock.iso()) is False
    job = store.take_job(conn)
    store.finish_job(conn, job["id"], "error", clock.iso(), "сбой")
    store.set_app_status(conn, 123, "error", "сбой")
    assert store.retry(conn, 123, clock.iso()) is True
    assert store.retry(conn, 123, clock.iso()) is False
    assert store.enqueue_once(conn, "refresh_history", None, clock.iso()) is True
    assert store.enqueue_once(conn, "refresh_history", None, clock.iso()) is False
    queued = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='queued' ORDER BY id")]
    assert queued == ["publish", "refresh_history"]


def test_requeue_after_web_restart(conn, cfg, clock):  # Review Focus 4
    seed_app(conn, cfg, clock)
    store.enqueue_once(conn, "refresh_history", None, clock.iso())
    job = store.take_job(conn)
    assert job["kind"] == "publish"
    store.set_app_status(conn, 123, "downloading")
    store.requeue_running(conn)
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()["status"] == "queued"
    assert store.list_apps(conn)[0].status == "queued"


def test_publish_goes_before_earlier_background_jobs(conn, cfg, clock):
    store.enqueue_once(conn, "check_removed", None, clock.iso())
    store.enqueue_once(conn, "refresh_history", None, clock.iso())
    seed_app(conn, cfg, clock)  # «Опубликовать» нажато позже
    assert [store.take_job(conn)["kind"] for _ in range(3)] == ["publish", "refresh_history", "check_removed"]


def test_fail_stale_updates_after_nightly_crash(conn, cfg, clock):  # Review Focus 4
    seed_app(conn, cfg, clock, app_id=1, versions=("1.0",))
    seed_app(conn, cfg, clock, app_id=2)
    for j in conn.execute("SELECT id FROM jobs").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    for app_id in (1, 2):
        store.start_job(conn, "update", app_id, clock.iso())
        store.set_app_status(conn, app_id, "downloading")
    store.fail_stale_updates(conn, clock.iso())
    assert {a.app_id: a.status for a in store.list_apps(conn)} == {1: "ok", 2: "error"}
    assert {r["status"] for r in conn.execute("SELECT status FROM jobs WHERE kind='update'")} == {"error"}


def test_active_jobs_ignore_queue_while_logged_out(conn, cfg, clock):
    seed_app(conn, cfg, clock)
    assert store.has_active_jobs(conn) is False
    store.set_state(conn, "session", "ok")
    assert store.has_active_jobs(conn) is True


def test_claim_state_only_once(conn):
    assert store.claim_state(conn, "expired_mail_sent", "t1") is True
    assert store.claim_state(conn, "expired_mail_sent", "t2") is False
    assert store.get_state(conn, "expired_mail_sent") == "t1"


def test_remove_dirs_ignores_unsafe_names(cfg):
    victim = cfg.data_dir / "victim"
    victim.mkdir(parents=True)
    cfg.pub_root.mkdir(parents=True)
    store.remove_dirs(cfg.pub_root, ["..", "../../victim", "", "abc"])
    assert victim.exists()


def test_upsert_purchases_and_published_flag(conn, cfg, clock):
    store.upsert_purchases(conn, [{"id": 1, "bundleId": "a", "name": "Старое", "purchaseDate": "2020-01-01T00:00:00Z"},
                                  {"id": 0, "name": "без id"}], clock.iso())
    store.upsert_purchases(conn, [{"id": 1, "bundleId": "a", "name": "Новое", "purchaseDate": "2020-01-01T00:00:00Z"}],
                           clock.iso())
    seed_app(conn, cfg, clock, app_id=2, name="Опубликованное")
    rows = {r["app_id"]: (r["name"], r["published"]) for r in store.list_purchases(conn)}
    assert rows == {1: ("Новое", 0), 2: ("Опубликованное", 1)}
    assert store.history_refreshed_at(conn) == clock.iso()


def test_add_manual_creates_purchase_and_publish_job_once(conn, cfg, clock):  # удалённые из истории Apple
    assert store.add_manual(conn, 492224193, clock.iso()) is True
    assert store.add_manual(conn, 492224193, clock.iso()) is False
    [row] = store.list_purchases(conn)
    assert row["app_id"] == 492224193 and row["published"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish'").fetchone()[0] == 1
    assert store.list_apps(conn)[0].status == "queued"


def test_add_manual_keeps_history_row(conn, cfg, clock):
    store.upsert_purchases(conn, [{"id": 7, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    assert store.add_manual(conn, 7, clock.iso()) is True
    assert [(r["name"], r["bundle_id"]) for r in store.list_purchases(conn)] == [("VK", "com.vk.vkclient")]


def test_old_database_gets_version_column(tmp_path, clock):
    import sqlite3
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE purchases (app_id INTEGER PRIMARY KEY, bundle_id TEXT NOT NULL DEFAULT '', "
                "name TEXT NOT NULL DEFAULT '', purchase_date TEXT NOT NULL DEFAULT '', refreshed_at TEXT NOT NULL)")
    old.execute("INSERT INTO purchases VALUES (1, 'a', 'Старое', '', '')")
    old.commit()
    old.close()
    c = store.connect(db)
    store.upsert_purchases(c, [{"id": 2, "bundleId": "b", "name": "Новое", "version": "1.2.3"}], clock.iso())
    assert {r["app_id"]: r["version"] for r in store.list_purchases(c)} == {1: "", 2: "1.2.3"}
