import hashlib
import io
import json
import zipfile

import pytest

from android_fakes import FakeHttp
from apk_factory import make_apk
from appshelf import apk
from appshelf.android import db, fdroid, fdroid_index as index, net, sources

PKG = "org.telegram.messenger"
ARCHIVE = "https://f-droid.org/archive/"


def catalog(builds=(), package=PKG, name="Telegram FOSS", upstream="https://github.com/example/telegram"):
    data = {"apps": [{"packageName": package, "suggestedVersionCode": "2", "sourceCode": upstream,
                      "localized": {"en-US": {"name": name}}}], "packages": {package: list(builds)}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index-v1.json", json.dumps(data))
    return out.getvalue()


def build(code, name, body=b"APK", abis=()):
    return {"versionCode": code, "versionName": name, "apkName": f"{PKG}_{code}.apk",
            "hashType": "sha256", "hash": hashlib.sha256(body).hexdigest(), "nativecode": abis}


def test_archive_search_versions_abi_and_cache():
    raw = catalog([build(1, "old"), build(2, "new", abis=["x86_64"]), build(3, "new", abis=["arm64-v8a"])])
    http = FakeHttp(raw={ARCHIVE + "index-v1.jar": (200, {}, raw)})
    now = [10.0]
    archive = index.Catalog(http, archive=True, clock=lambda: now[0])
    app = archive.search("telegram")[0]
    assert app.select().code == 3 and [b.code for b in app.choices()] == [3, 1]
    assert archive.info(PKG + "@1")[1].name == "old"
    assert app.upstream == "https://github.com/example/telegram"
    assert len(http.calls) == 1
    now[0] += index.TTL
    archive.search("telegram")
    assert len(http.calls) == 2
    with pytest.raises(net.SourceError, match="для ARM"):
        archive.info(PKG + "@2")


def test_archive_fallback_download_and_nightly_mark(tmp_path, conn, clock, acct):
    path = make_apk(tmp_path / "test.apk", package=PKG, version_code=2, version_name="new")
    body = path.read_bytes()
    raw = catalog([build(2, "new", body)])
    http = FakeHttp(raw={fdroid.API + PKG: (404, {}, b""), ARCHIVE + "index-v1.jar": (200, {}, raw)},
                    files={ARCHIVE + PKG + "_2.apk": body})
    src = sources.Sources(http, sleep=lambda s: None)
    dest = tmp_path / "out.apk"
    got = src.fetch("fdroid", PKG, dest)
    assert apk.read_apk(dest).package == PKG and got.etag == "fdroid_archive:2"
    assert not src.latest("fdroid", PKG, got.etag).changed
    pinned = src.fetch("fdroid_archive", PKG + "@2", dest)
    assert not src.latest("fdroid_archive", PKG + "@2", pinned.etag).changed
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    for package, source in ((PKG, "fdroid_archive"), ("com.example.app", "github")):
        db.put_app(conn, shelf.id, package, "Example", source, package, clock.iso())
        db.set_status(conn, shelf.id, package, "ok")
    assert {a["source"] for a in db.apps_to_check(conn)} == {"fdroid_archive", "github"}


def test_archive_hash_failure_removes_apk(tmp_path):
    raw = catalog([build(2, "new")])
    http = FakeHttp(raw={ARCHIVE + "index-v1.jar": (200, {}, raw)}, files={ARCHIVE + PKG + "_2.apk": b"wrong"})
    src = sources.Sources(http, sleep=lambda s: None)
    with pytest.raises(net.SourceError, match="SHA-256"):
        src.fetch("fdroid_archive", PKG, tmp_path / "out.apk")
    assert not (tmp_path / "out.apk").exists()


def test_catalog_rejects_bad_index_and_does_not_use_partial_cache(monkeypatch):
    for raw in (b"not zip", catalog([{**build(1, "old"), "apkName": "../evil.apk"}])):
        if raw == b"not zip":
            with pytest.raises(net.SourceError, match="формате"):
                index.parse(raw)
        else:
            assert index.parse(raw)[PKG].builds == ()
    monkeypatch.setattr(index, "MAX_INDEX", 10)
    with pytest.raises(net.SourceError, match="слишком большой"):
        index.parse(catalog())


def test_bad_pinned_refs():
    for ref in (PKG + "@-1", PKG + "@../../x", PKG + "@" + "1" * 100, "../x@1"):
        with pytest.raises(net.SourceError):
            index.split_ref(ref)
