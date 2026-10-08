import pytest

from appshelf import store
from helpers import make_account, seed_app, set_session


def test_rotation_keeps_current_and_previous_and_removes_older(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "1.1", "1.2"), make_dirs=True)
    root = cfg.shelf_root(acct)
    roles = {r["role"]: r["version"] for r in conn.execute("SELECT * FROM versions")}
    assert roles == {"current": "1.2", "previous": "1.1"}
    assert not (root / "123-1.0").exists()
    assert (root / "123-1.1").exists() and (root / "123-1.2").exists()
    assert store.list_apps(conn, 1)[0].status == "ok"


def test_add_version_after_unpublish_raises(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock)
    store.unpublish(conn, 1, cfg.shelf_root(acct), 123)
    with pytest.raises(store.AppGone):
        store.add_version(conn, 1, cfg.shelf_root(acct), 123, store.NewVersion(
            "1.0", "1", "900", "13.0", "iphone", 1, "123-1.0"), clock.iso())
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0


def test_unpublish_removes_dirs_cancels_jobs_keeps_history(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, versions=("1.0", "1.1"), make_dirs=True)
    store.unpublish(conn, 1, cfg.shelf_root(acct), 123)
    assert list(cfg.shelf_root(acct).iterdir()) == []
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "cancelled"
    assert [r["published"] for r in store.list_purchases(conn, 1)] == [0]


def test_double_click_creates_one_job(conn, cfg, clock):
    seed_app(conn, cfg, clock)
    set_session(conn, 1, "ok")
    assert store.publish(conn, 1, 123, clock.iso()) is False
    job = store.take_job(conn)
    store.finish_job(conn, job["id"], "error", clock.iso(), "сбой")
    store.set_app_status(conn, 1, 123, "error", "сбой")
    assert store.retry(conn, 1, 123, clock.iso()) is True
    assert store.retry(conn, 1, 123, clock.iso()) is False
    assert store.enqueue_once(conn, 1, "refresh_history", None, clock.iso()) is True
    assert store.enqueue_once(conn, 1, "refresh_history", None, clock.iso()) is False
    queued = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='queued' ORDER BY id")]
    assert queued == ["publish", "refresh_history"]


def test_requeue_after_web_restart(conn, cfg, clock):
    seed_app(conn, cfg, clock)
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "refresh_history", None, clock.iso())
    job = store.take_job(conn)
    assert job["kind"] == "publish" and job["account_id"] == 1
    store.set_app_status(conn, 1, 123, "downloading")
    store.requeue_running(conn)
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()["status"] == "queued"
    assert store.list_apps(conn, 1)[0].status == "queued"


def test_publish_goes_before_earlier_background_jobs(conn, cfg, clock, acct):
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    store.enqueue_once(conn, 1, "refresh_history", None, clock.iso())
    seed_app(conn, cfg, clock)  # «Опубликовать» нажато позже
    assert [store.take_job(conn)["kind"] for _ in range(3)] == ["publish", "refresh_history", "check_removed"]


def test_take_job_skips_apple_ids_without_login(conn, cfg, clock, acct):  # Review Focus 1
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, session="expired")
    seed_app(conn, cfg, clock, app_id=7, acct=petr)              # публикация Петра ждёт его входа
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    assert store.has_runnable(conn, "publish") is False
    assert store.take_job(conn)["kind"] == "check_removed"
    assert store.take_job(conn) is None
    set_session(conn, petr.id, "ok")
    assert store.has_runnable(conn, "publish") is True
    job = store.take_job(conn)
    assert (job["kind"], job["account_id"], job["app_id"]) == ("publish", petr.id, 7)


def test_shelves_of_apple_ids_are_separate(conn, cfg, clock, acct):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    seed_app(conn, cfg, clock, app_id=7, versions=("1.0",), make_dirs=True)
    seed_app(conn, cfg, clock, app_id=7, acct=petr, versions=("1.0",), make_dirs=True)  # то же приложение у Петра
    assert (cfg.shelf_root(acct) / "7-1.0").is_dir() and (cfg.shelf_root(petr) / "7-1.0").is_dir()
    store.unpublish(conn, petr.id, cfg.shelf_root(petr), 7)
    assert [a.app_id for a in store.list_apps(conn, 1)] == [7] and store.list_apps(conn, petr.id) == []
    assert (cfg.shelf_root(acct) / "7-1.0").is_dir() and not (cfg.shelf_root(petr) / "7-1.0").exists()
    assert store.has_app(conn, 1, 7) and not store.has_app(conn, petr.id, 7)
    assert store.has_purchase(conn, petr.id, 7) and not store.has_purchase(conn, petr.id, 8)
    assert [r["published"] for r in store.list_purchases(conn, petr.id)] == [0]
    assert store.dir_in_use(conn, 1, "7-1.0") and not store.dir_in_use(conn, petr.id, "7-1.0")


def test_fail_stale_updates_after_nightly_crash(conn, cfg, clock):
    seed_app(conn, cfg, clock, app_id=1, versions=("1.0",))
    seed_app(conn, cfg, clock, app_id=2)
    for j in conn.execute("SELECT id FROM jobs").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    for app_id in (1, 2):
        store.start_job(conn, 1, "update", app_id, clock.iso())
        store.set_app_status(conn, 1, app_id, "downloading")
    store.fail_stale_updates(conn, clock.iso())
    assert {a.app_id: a.status for a in store.list_apps(conn, 1)} == {1: "ok", 2: "error"}
    assert {r["status"] for r in conn.execute("SELECT status FROM jobs WHERE kind='update'")} == {"error"}


def test_active_jobs_ignore_queue_while_logged_out(conn, cfg, clock):
    seed_app(conn, cfg, clock)
    assert store.has_active_jobs(conn, 1) is False
    set_session(conn, 1, "ok")
    assert store.has_active_jobs(conn, 1) is True


def test_active_jobs_are_per_apple_id(conn, cfg, clock, acct):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, session="ok")
    seed_app(conn, cfg, clock, app_id=7, acct=petr)
    set_session(conn, 1, "ok")
    assert store.has_active_jobs(conn, petr.id) is True and store.has_active_jobs(conn, 1) is False


def test_claim_state_only_once(conn):
    assert store.claim_state(conn, "low_space_mail_sent", "t1") is True
    assert store.claim_state(conn, "low_space_mail_sent", "t2") is False
    assert store.get_state(conn, "low_space_mail_sent") == "t1"


def test_remove_dirs_ignores_unsafe_names(cfg):
    victim = cfg.data_dir / "victim"
    victim.mkdir(parents=True)
    root = cfg.archive / "shelf"
    root.mkdir(parents=True)
    store.remove_dirs(root, ["..", "../../victim", "", "abc"])
    assert victim.exists()


def test_upsert_purchases_and_published_flag(conn, cfg, clock, acct):
    store.upsert_purchases(conn, 1, [{"id": 1, "bundleId": "a", "name": "Старое",
                                      "purchaseDate": "2020-01-01T00:00:00Z"}, {"id": 0, "name": "без id"}], clock.iso())
    store.upsert_purchases(conn, 1, [{"id": 1, "bundleId": "a", "name": "Новое",
                                      "purchaseDate": "2020-01-01T00:00:00Z"}], clock.iso())
    seed_app(conn, cfg, clock, app_id=2, name="Опубликованное")
    rows = {r["app_id"]: (r["name"], r["published"]) for r in store.list_purchases(conn, 1)}
    assert rows == {1: ("Новое", 0), 2: ("Опубликованное", 1)}
    assert store.history_refreshed_at(conn, 1) == clock.iso()


def test_add_manual_creates_purchase_and_publish_job_once(conn, cfg, clock, acct):  # удалённые из истории Apple
    assert store.add_manual(conn, 1, 492224193, clock.iso()) is True
    assert store.add_manual(conn, 1, 492224193, clock.iso()) is False
    [row] = store.list_purchases(conn, 1)
    assert row["app_id"] == 492224193 and row["published"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish'").fetchone()[0] == 1
    assert store.list_apps(conn, 1)[0].status == "queued"


def test_add_manual_keeps_history_row(conn, cfg, clock, acct):
    store.upsert_purchases(conn, 1, [{"id": 7, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    assert store.add_manual(conn, 1, 7, clock.iso()) is True
    assert [(r["name"], r["bundle_id"]) for r in store.list_purchases(conn, 1)] == [("VK", "com.vk.vkclient")]


def test_shelf_size_and_delete_shelf(conn, cfg, clock, acct):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    seed_app(conn, cfg, clock, versions=("1.0", "1.1"))
    seed_app(conn, cfg, clock, app_id=7, acct=petr, versions=("1.0",))
    store.mark_no_license(conn, petr.id, 9, clock.iso())
    mb = 291 * 1024 * 1024
    assert store.shelf_size(conn, 1) == 2 * mb and store.shelf_size(conn, petr.id) == mb
    with store.tx(conn):
        store.delete_shelf(conn, petr.id)
    for t in store.SHELF_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id=?", (petr.id,)).fetchone()[0] == 0
    assert store.shelf_size(conn, 1) == 2 * mb
