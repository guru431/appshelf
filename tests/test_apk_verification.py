"""Actual AOSP verifier contract, separate from synthetic pipeline fixtures.

Run with APPSHELF_APKSIGNER pointing to the SDK apksigner.jar (or apksigner on PATH).
"""
import shutil
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from appshelf import apk
from appshelf.android import db, fetch


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_resumable_chunks_preserve_real_signature_and_reject_tampering(web, conn, tmp_path):
    from test_web_android import work
    from test_uploads import payload, post_part
    raw = (DATA / "v2-5.apk").read_bytes()
    item = web.client.post("/android/uploads", json=payload(raw)).json()
    endpoint = "/android/uploads/" + item["id"]
    assert post_part(web.client, endpoint, 1, 0, raw).status_code == 200
    assert web.client.post(endpoint + "/finish").status_code == 200
    work(web, conn)
    current = conn.execute("SELECT * FROM android_versions WHERE role='current'").fetchone()
    assert current["signature_verified"] == 1 and current["signer"] == OLD
    candidate = tmp_path / "bad.apk"
    shutil.copyfile(DATA / "v2-6.apk", candidate)
    damaged(candidate, "v2")
    raw = candidate.read_bytes()
    item = web.client.post("/android/uploads", json=payload(raw, client_id="b"*32)).json()
    endpoint = "/android/uploads/" + item["id"]
    assert post_part(web.client, endpoint, 1, 0, raw).status_code == 200
    assert web.client.post(endpoint + "/finish").status_code == 200
    work(web, conn)
    assert conn.execute("SELECT COUNT(*) FROM android_versions").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM android_jobs ORDER BY id DESC LIMIT 1").fetchone()[0] == "error"

DATA = Path(__file__).parent / "data" / "apk-signatures"
OLD = "6cb0d638923096bfd175cfa91e16404980f5f706eb8dfde5ba37e5528040f186"
NEW = "e08de9a9f6987978aafa11b24c4276f7146e72c29306bf16dd99bebbbe561d65"
PACKAGE = "ru.example.app"


def damaged(path: Path, scheme: str) -> None:
    if scheme == "v1":
        with zipfile.ZipFile(path) as z:
            entries = [(entry, z.read(entry)) for entry in z.infolist()]
        with zipfile.ZipFile(path, "w") as z:
            for entry, contents in entries:
                z.writestr(entry, b"changed-dex" if entry.filename == "classes.dex" else contents)
    else:
        raw = bytearray(path.read_bytes())
        with zipfile.ZipFile(path) as z:
            start = z.getinfo("classes.dex").header_offset
        name_len, extra_len = struct.unpack_from("<HH", raw, start + 26)
        raw[start + 30 + name_len + extra_len] ^= 1
        path.write_bytes(raw)


@pytest.mark.integration
@pytest.mark.timeout(120)
@pytest.mark.parametrize("scheme", ["v1", "v2", "v3"])
def test_real_signatures_and_tamper_preserve_working_version(ctx, cfg, conn, clock, tmp_path, scheme, acct):
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    good = tmp_path / "good.apk"
    shutil.copyfile(DATA / f"{scheme}-5.apk", good)
    args = dict(source="upload", source_ref="", expected_package=None, expected_signer="", name_hint="", icon=None)
    assert apk.verify_apk(good) == OLD
    assert fetch.store_apk(ctx.env, conn, shelf, good, **args) == (PACKAGE, "updated")
    current = dict(db.current_version(conn, shelf.id, PACKAGE))
    assert current["signature_verified"] == 1
    candidate = tmp_path / "bad.apk"
    shutil.copyfile(DATA / f"{scheme}-6.apk", candidate)
    damaged(candidate, scheme)
    # The former certificate-only check accepts this same signer although the content was changed.
    assert apk.read_apk(candidate).signer == OLD
    with pytest.raises(apk.ApkError, match="криптографическая"):
        fetch.store_apk(ctx.env, conn, shelf, candidate, **args)
    assert dict(db.current_version(conn, shelf.id, PACKAGE)) == current
    assert len(list((cfg.archive / shelf.shelf).iterdir())) == 1


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_manual_old_build_is_verified_and_preserves_latest(ctx, cfg, conn, clock, tmp_path, acct):
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    args = dict(source="upload", source_ref="", expected_package=None, expected_signer="", name_hint="", icon=None,
                allow_older=True)
    candidate = tmp_path / "candidate.apk"
    shutil.copyfile(DATA / "v2-6.apk", candidate)
    assert fetch.store_apk(ctx.env, conn, shelf, candidate, **args) == (PACKAGE, "updated")
    current = dict(db.current_version(conn, shelf.id, PACKAGE))
    shutil.copyfile(DATA / "v2-5.apk", candidate)
    assert fetch.store_apk(ctx.env, conn, shelf, candidate, **args) == (PACKAGE, "saved")
    assert [v["version_code"] for v in db.list_apps(conn, shelf.id)[0].versions] == [6, 5]
    public = db.ensure_public_shelf(conn, clock.iso())
    db.put_app(conn, public.id, PACKAGE, "Signed app", "upload", "", clock.iso())
    fetch._copy_to_public(ctx.env, conn, public, PACKAGE, shelf.id, tmp_path / "copy-work")
    copies = db.list_apps(conn, public.id)[0].versions
    assert [v["version_code"] for v in copies] == [6, 5]
    for version in copies:
        copied = fetch.shelf_root(cfg, public) / version["dir"] / fetch.apk_name(PACKAGE, version["version_name"])
        assert version["signature_verified"] and apk.verify_apk(copied) == OLD
    shutil.copyfile(DATA / "v2-5.apk", candidate)
    damaged(candidate, "v2")
    with pytest.raises(apk.ApkError, match="криптографическая"):
        fetch.store_apk(ctx.env, conn, shelf, candidate, **args)
    assert dict(db.current_version(conn, shelf.id, PACKAGE)) == current
    assert len(list((cfg.archive / shelf.shelf).iterdir())) == 2


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_verified_rotation_is_manual_pending_with_android_9_compatibility(ctx, cfg, conn, clock, tmp_path, acct):
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    old, rotated = tmp_path / "old.apk", tmp_path / "rotated.apk"
    shutil.copyfile(DATA / "v3-5.apk", old)
    shutil.copyfile(DATA / "v3-rotated-6.apk", rotated)
    args = dict(source="upload", source_ref="", expected_package=None, expected_signer="", name_hint="", icon=None)
    fetch.store_apk(ctx.env, conn, shelf, old, **args)
    assert apk.verify_apk(rotated) == NEW
    assert apk.verified_rotation(rotated, OLD, NEW) == 28
    assert apk.verified_rotation(rotated, "f" * 64, NEW) == 0
    assert apk.verified_rotation(DATA / "v3-6.apk", OLD, NEW) == 0
    assert fetch.store_apk(ctx.env, conn, shelf, rotated, **args) == (PACKAGE, "pending")
    view = db.list_apps(conn, shelf.id)[0]
    assert view.current["signer"] == OLD
    assert view.pending["signer"] == NEW
    assert view.pending["rotation_min_sdk"] == 28
    assert view.pending["signature_verified"] == 1


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_corrupted_rotation_proof_never_establishes_trust(tmp_path):
    path = tmp_path / "corrupt-lineage.apk"
    raw = bytearray((DATA / "v3-rotated-6.apk").read_bytes())
    with (DATA / "v3-5.apk").open("rb") as f:
        old_cert = apk._block_cert(f)
    start = raw.index(old_cert)
    raw[start + len(old_cert) - 1] ^= 1
    path.write_bytes(raw)
    assert apk.verified_rotation(path, OLD, NEW) == 0
    with pytest.raises(apk.ApkError, match="криптографическая"):
        apk.verify_apk(path)


def test_missing_verifier_and_timeout_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("APPSHELF_APKSIGNER", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(apk.ApkError, match="установите apksigner"):
        apk.verify_apk(DATA / "v2-5.apk")
    monkeypatch.setenv("APPSHELF_APKSIGNER", "apksigner")
    import subprocess

    def timeout(*args, **kw):
        raise subprocess.TimeoutExpired("apksigner", 120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(apk.ApkError, match="не удалось проверить"):
        apk.verify_apk(DATA / "v2-5.apk")


def test_multiple_verified_signers_are_not_reduced_to_first(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("APPSHELF_APKSIGNER", "apksigner")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=
        f"Signer #1 certificate SHA-256 digest: {OLD}\nSigner #2 certificate SHA-256 digest: {NEW}\n"))
    with pytest.raises(apk.ApkError, match="ровно одного"):
        apk.verify_apk(DATA / "v2-5.apk")
