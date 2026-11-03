"""Durable upload boundaries: retries, restart, write failure, owner isolation and exactly-once handoff."""
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from appshelf import jobs, store
from appshelf.android import db, uploads
from helpers import make_account, sign_in
from apk_factory import make_apk
from test_web_android import work


def payload(raw, **extra):
    return {"client_id": "a" * 32, "size": len(raw), "fingerprint": hashlib.sha256(raw).hexdigest(),
            "filename": "example.apk", **extra}


def post_part(client, endpoint, uid, index, raw):
    return client.post(f"{endpoint}/parts/{index}?person={uid}", content=raw,
                       headers={"Content-Type": "application/octet-stream", "X-Chunk-SHA256": hashlib.sha256(raw).hexdigest()})


@pytest.mark.parametrize("prefix", ["/android", "/android/public"])
def test_parts_out_of_order_retry_restart_and_finish_once(web, conn, cfg, clock, acct, tmp_path, prefix):
    raw = make_apk(tmp_path / "good.apk").read_bytes()
    create = prefix + "/uploads"
    response = web.client.post(create, json=payload(raw))
    assert response.status_code == 200
    transfer = response.json()
    endpoint = create + "/" + transfer["id"]
    assert web.client.post(create, json=payload(raw)).json()["id"] == transfer["id"]
    assert post_part(web.client, endpoint, acct.user_id, 0, raw).status_code == 200
    assert post_part(web.client, endpoint, acct.user_id, 0, raw).status_code == 200
    web.app.state.worker.recover()
    state = web.client.get(endpoint + f"?person={acct.user_id}").json()
    assert state["parts"] == {"0": hashlib.sha256(raw).hexdigest()}
    completed = web.client.post(endpoint + "/finish").json()
    assert web.client.post(endpoint + "/finish").json()["job_id"] == completed["job_id"]
    assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='upload'").fetchone()[0] == 1
    web.app.state.worker.recover()
    assert conn.execute("SELECT status FROM android_jobs").fetchone()[0] == "queued"
    work(web, conn)
    assert conn.execute("SELECT COUNT(*) FROM android_versions").fetchone()[0] == 1
    assert web.client.post(endpoint + "/finish").json()["job_id"] == completed["job_id"]
    assert not (uploads.Uploads(cfg).folder(transfer["id"]) / "complete.apk").exists()


def test_part_boundaries_hashes_and_partial_finalize(web, conn, cfg, acct, monkeypatch):
    raw = b"firstlast"
    monkeypatch.setattr(uploads, "CHUNK_SIZE", 5)
    transfer = web.client.post("/android/uploads", json=payload(raw)).json()
    endpoint = "/android/uploads/" + transfer["id"]
    assert post_part(web.client, endpoint, acct.user_id, 1, raw[5:]).status_code == 200
    assert web.client.post(endpoint + "/finish").status_code == 409
    assert post_part(web.client, endpoint, acct.user_id, 2, b"abc").status_code == 400
    assert post_part(web.client, endpoint, acct.user_id, 0, b"tiny").status_code == 400
    bad = web.client.post(endpoint + f"/parts/0?person={acct.user_id}", content=raw[:5],
                          headers={"X-Chunk-SHA256": "0" * 64})
    assert bad.status_code == 422
    assert post_part(web.client, endpoint, acct.user_id, 0, raw[:5]).status_code == 200
    assert post_part(web.client, endpoint, acct.user_id, 0, b"other").status_code == 409
    result = web.client.post(endpoint + "/finish")
    assert result.status_code == 200
    job = conn.execute("SELECT payload FROM android_jobs").fetchone()
    from pathlib import Path
    assert Path(json.loads(job[0])["path"]).read_bytes() == raw


def test_chunk_and_finalize_disk_failures_are_recoverable(conn, cfg, clock, acct, monkeypatch):
    manager = uploads.Uploads(cfg)
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    raw = b"example"
    state = manager.create(conn, acct.user_id, shelf.id, payload(raw), clock.iso())
    row = manager.get(conn, state["id"], acct.user_id, shelf.id, clock.iso())
    digest = hashlib.sha256(raw).hexdigest()
    with monkeypatch.context() as patch:
        patch.setattr(uploads.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("forced rename failure")))
        with pytest.raises(OSError):
            manager.part(conn, row, 0, raw, digest, clock.iso())
    assert manager.view(manager.get(conn, row["id"], acct.user_id, shelf.id, clock.iso()))["parts"] == {}
    manager.part(conn, row, 0, raw, digest, clock.iso())
    with monkeypatch.context() as patch:
        patch.setattr(uploads.db, "enqueue", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("forced commit failure")))
        with pytest.raises(sqlite3.OperationalError):
            manager.finish(conn, row, clock.iso())
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 0
    assert manager.get(conn, row["id"], acct.user_id, shelf.id, clock.iso())["job_id"] is None
    manager.finish(conn, row, clock.iso())
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 1


def test_upload_owner_scope_and_stale_tab_are_enforced(web, make_web, conn, cfg, clock, acct):
    member = make_account(conn, clock, email="member@example.org", role="member", legacy=False)
    from helpers import sign_in
    sign_in(web.client, cfg, conn, clock, member.user_id)
    first = web.client.post(f"/android/uploads?person={acct.user_id}", json=payload(b"a")).json()
    url = "/android/uploads/" + first["id"]
    web.client.get(f"/who/{member.user_id}")
    assert post_part(web.client, url, acct.user_id, 0, b"a").status_code == 200
    assert web.client.get(url + f"?person={member.user_id}").status_code == 404
    assert web.client.post(url + f"/finish?person={member.user_id}").status_code == 404
    assert web.client.post(url + f"/cancel?person={member.user_id}").status_code == 404
    assert web.client.request("POST", "/android/uploads", json=payload(b"a")).status_code == 409
    other = make_web(cfg, conn)
    sign_in(other.client, cfg, conn, clock, member.user_id)
    assert other.client.post("/android/public/uploads", json=payload(b"x")).status_code == 403
    assert other.client.get(url + f"?person={acct.user_id}").status_code == 403
    assert other.client.post(url + f"/parts/0?person={member.user_id}", content=b"a").status_code == 404


def test_expiry_cancel_and_limits(web, conn, cfg, clock, acct, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_RESERVED", 16)
    assert web.client.post("/android/uploads", json=payload(b"x", size=0)).status_code == 413
    first = web.client.post("/android/uploads", json=payload(b"12345678")).json()
    second = web.client.post("/android/uploads", json=payload(b"abcdefgh", client_id="b"*32)).json()
    assert web.client.post("/android/uploads", json=payload(b"x", client_id="c"*32)).status_code == 429
    assert web.client.post("/android/uploads/" + first["id"] + "/cancel").status_code == 200
    assert not uploads.Uploads(cfg).folder(first["id"]).exists()
    old = "2025-01-01T00:00:00+00:00"
    conn.execute("UPDATE android_uploads SET updated_at=? WHERE id=?", (old, second["id"]))
    assert web.client.get("/android/uploads/" + second["id"] + f"?person={acct.user_id}").status_code == 410
    uploads.Uploads(cfg).cleanup(conn, clock.iso())
    assert conn.execute("SELECT COUNT(*) FROM android_uploads").fetchone()[0] == 0


def test_concurrent_finish_enqueues_only_one_job(cfg, conn, clock, acct):
    manager = uploads.Uploads(cfg)
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    raw = b"one APK"
    transfer = manager.create(conn, acct.user_id, shelf.id, payload(raw), clock.iso())
    row = manager.get(conn, transfer["id"], acct.user_id, shelf.id, clock.iso())
    manager.part(conn, row, 0, raw, hashlib.sha256(raw).hexdigest(), clock.iso())
    def finish():
        c = store.connect(cfg.db)
        try:
            return manager.finish(c, manager.get(c, transfer["id"], acct.user_id, shelf.id, clock.iso()), clock.iso())["job_id"]
        finally:
            c.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: finish(), range(2)))
    assert results[0] == results[1]
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 1


def test_user_deletion_cleans_private_and_public_pending_files(cfg, conn, clock):
    account = make_account(conn, clock, email="remove@example.org", role="member", legacy=False)
    manager = uploads.Uploads(cfg)
    shelves = [db.ensure_shelf(conn, account.user_id, clock.iso()), db.ensure_public_shelf(conn, clock.iso())]
    transfers = []
    for index, shelf in enumerate(shelves):
        item = manager.create(conn, account.user_id, shelf.id, payload(b"private", client_id=str(index)*32), clock.iso())
        row = manager.get(conn, item["id"], account.user_id, shelf.id, clock.iso())
        manager.part(conn, row, 0, b"private", hashlib.sha256(b"private").hexdigest(), clock.iso())
        transfers.append(item)
    jobs.remove_user(cfg, conn, account.user_id)
    assert not conn.execute("SELECT 1 FROM android_uploads WHERE user_id=?", (account.user_id,)).fetchone()
    assert all(not manager.folder(item["id"]).exists() for item in transfers)


def test_identity_reselection_reuses_pending_session_and_limits_raw_chunk(web, conn, acct, monkeypatch):
    raw = b"abcdef"
    first = web.client.post("/android/uploads", json=payload(raw)).json()
    second = web.client.post("/android/uploads", json=payload(raw, client_id="b"*32, filename="renamed.apk")).json()
    assert second["id"] == first["id"]
    from appshelf.web import uploads as upload_web
    monkeypatch.setattr(upload_web, "CHUNK_SIZE", 5)
    assert post_part(web.client, "/android/uploads/" + first["id"], acct.user_id, 0, raw).status_code == 413
    assert web.client.get("/android/public/uploads/" + first["id"] + f"?person={acct.user_id}").status_code == 404


def test_removal_preserves_handed_off_public_file_but_not_reused_job_id(cfg, conn, clock):
    account = make_account(conn, clock, email="transfer@example.org", role="member", legacy=False)
    manager = uploads.Uploads(cfg)
    shelf = db.ensure_public_shelf(conn, clock.iso())
    item = manager.create(conn, account.user_id, shelf.id, payload(b"public"), clock.iso())
    row = manager.get(conn, item["id"], account.user_id, shelf.id, clock.iso())
    manager.part(conn, row, 0, b"public", hashlib.sha256(b"public").hexdigest(), clock.iso())
    done = manager.finish(conn, row, clock.iso())
    jobs.remove_user(cfg, conn, account.user_id)
    path = manager.folder(item["id"])
    assert (path / "complete.apk").exists()
    manager.cleanup(conn, clock.iso())
    assert path.exists()
    conn.execute("DELETE FROM android_jobs WHERE id=?", (done["job_id"],))
    unrelated = db.enqueue(conn, shelf.id, "check", clock.iso(), package="ru.example.other")
    assert unrelated == done["job_id"]
    manager.cleanup(conn, clock.iso())
    assert not path.exists() and conn.execute("SELECT 1 FROM android_jobs WHERE id=?", (unrelated,)).fetchone()


def test_fsync_failure_does_not_acknowledge_chunk(conn, cfg, clock, acct, monkeypatch):
    manager = uploads.Uploads(cfg)
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    transfer = manager.create(conn, acct.user_id, shelf.id, payload(b"abc"), clock.iso())
    row = manager.get(conn, transfer["id"], acct.user_id, shelf.id, clock.iso())
    with monkeypatch.context() as patch:
        patch.setattr(uploads.os, "fsync", lambda *args: (_ for _ in ()).throw(OSError("forced fsync failure")))
        with pytest.raises(OSError):
            manager.part(conn, row, 0, b"abc", hashlib.sha256(b"abc").hexdigest(), clock.iso())
    assert not manager.view(manager.get(conn, row["id"], acct.user_id, shelf.id, clock.iso()))["parts"]
    assert not list(manager.folder(row["id"]).iterdir())


def test_schema_10_migration_preserves_data_and_is_reopenable(cfg, conn, acct):
    old = [tuple(row) for row in conn.execute("SELECT * FROM users ORDER BY id")]
    conn.execute("DROP TABLE android_uploads")
    conn.execute("PRAGMA user_version=10")
    reopened = store.connect(cfg.db)
    try:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        assert [tuple(row) for row in reopened.execute("SELECT * FROM users ORDER BY id")] == old
        assert reopened.execute("SELECT COUNT(*) FROM android_uploads").fetchone()[0] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("lost", ["missing", "changed"])
def test_lost_part_is_invalidated_for_retry_without_losing_other_parts(cfg, conn, clock, acct, monkeypatch, lost):
    monkeypatch.setattr(uploads, "CHUNK_SIZE", 3)
    manager = uploads.Uploads(cfg)
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    item = manager.create(conn, acct.user_id, shelf.id, payload(b"abcdef"), clock.iso())
    row = manager.get(conn, item["id"], acct.user_id, shelf.id, clock.iso())
    for index, raw in enumerate((b"abc", b"def")):
        manager.part(conn, row, index, raw, hashlib.sha256(raw).hexdigest(), clock.iso())
    part = manager.folder(row["id"]) / "1.part"
    if lost == "missing":
        part.unlink()
    else:
        part.write_bytes(b"bad")
    with pytest.raises(uploads.UploadError, match="часть повреждена"):
        manager.finish(conn, row, clock.iso())
    state = manager.view(manager.get(conn, row["id"], acct.user_id, shelf.id, clock.iso()))
    assert state["parts"] == {"0": hashlib.sha256(b"abc").hexdigest()}
    manager.part(conn, row, 1, b"def", hashlib.sha256(b"def").hexdigest(), clock.iso())
    assert manager.finish(conn, row, clock.iso())["job_id"] is not None
