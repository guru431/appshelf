import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from android_fakes import FakeHttp
from apk_factory import make_apk
from appshelf import apk
from appshelf.android import db, fdroid, fdroid_index as index, net, sources

PKG = "org.telegram.messenger"
ARCHIVE = "https://f-droid.org/archive/"
IZZY = "https://apt.izzysoft.de/fdroid/repo/"


def catalog(builds=(), package=PKG, name="Telegram FOSS", upstream="https://github.com/example/telegram"):
    data = {"repo": {"timestamp": 1800000000000},
            "apps": [{"packageName": package, "suggestedVersionCode": "2", "sourceCode": upstream,
                      "localized": {"en-US": {"name": name}}}], "packages": {package: list(builds)}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.comment = b"appshelf-test-index"
        z.writestr("index-v1.json", json.dumps(data))
    return out.getvalue()


def build(code, name, body=b"APK", abis=()):
    return {"versionCode": code, "versionName": name, "apkName": f"{PKG}_{code}.apk",
            "hashType": "sha256", "hash": hashlib.sha256(body).hexdigest(), "nativecode": abis}


def test_archive_search_versions_abi_and_cache():
    raw = catalog([build(1, "old"), build(2, "new", abis=["x86_64"]), build(3, "new", abis=["arm64-v8a"])])
    http = FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, raw)})
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
    assert archive.info(PKG + "@2")[1].abis == ("x86_64",)  # Explicit device-specific choice remains possible.


def test_archive_fallback_download_and_nightly_mark(tmp_path, conn, clock, acct):
    path = make_apk(tmp_path / "test.apk", package=PKG, version_code=2, version_name="new")
    body = path.read_bytes()
    raw = catalog([build(2, "new", body)])
    http = FakeHttp(raw={fdroid.API + PKG: (404, {}, b""), ARCHIVE + "entry.jar": (200, {}, raw)},
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
    http = FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, raw)}, files={ARCHIVE + PKG + "_2.apk": b"wrong"})
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


def test_repository_key_and_cache_are_isolated(tmp_path, monkeypatch):
    raw = catalog([build(2, "new")])
    http = FakeHttp(raw={IZZY + "entry.jar": (200, {}, raw),
                         "https://f-droid.org/repo/entry.jar": (200, {}, raw)})
    verified = []
    monkeypatch.setattr(index, "verify", lambda data, signer: verified.append(signer))
    official = index.Catalog(http, cache_dir=tmp_path)
    izzy = index.Catalog(http, repository="izzy", cache_dir=tmp_path)
    assert official.info(PKG)[1].code == izzy.info(PKG)[1].code == 2
    assert verified == [index.REPO_SIGNER, index.IZZY_SIGNER]
    assert official.cache != izzy.cache
    saved = json.loads(izzy.cache.read_bytes())
    assert saved["repo"] == IZZY and saved["signer"] == index.IZZY_SIGNER
    izzy.cache.write_bytes(official.cache.read_bytes())
    with pytest.raises(net.SourceError, match="кэш.*повреждён"):
        index.Catalog(http, repository="izzy", cache_dir=tmp_path).info(PKG)


def test_izzy_download_hash_versions_and_nightly(tmp_path, conn, clock, acct):
    body = make_apk(tmp_path / "test.apk", package=PKG, version_code=2, version_name="new").read_bytes()
    signer = apk.read_apk(tmp_path / "test.apk").signer
    raw = catalog([{**build(2, "new", body), "signer": signer}])
    http = FakeHttp(raw={IZZY + "entry.jar": (200, {}, raw)}, files={IZZY + PKG + "_2.apk": body})
    src = sources.Sources(http, sleep=lambda _: None, pause=0)
    dest = tmp_path / "out.apk"
    got = src.fetch("izzy", PKG + "@2", dest)
    assert apk.read_apk(dest).package == PKG and got.etag == "izzy:2"
    assert got.expected_signer == signer
    assert not src.latest("izzy", PKG, got.etag).changed
    assert src.latest("izzy", PKG, "izzy:1").changed
    assert not src.latest("izzy", PKG + "@2", got.etag).changed
    http.files[IZZY + PKG + "_2.apk"] = b"wrong"
    with pytest.raises(net.SourceError, match="SHA-256"):
        src.fetch("izzy", PKG, dest)
    assert not dest.exists()
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Example", "izzy", PKG, clock.iso())
    db.set_status(conn, shelf.id, PKG, "ok")
    assert [a["source"] for a in db.apps_to_check(conn)] == ["izzy"]


def test_persistent_cache_survives_restart_and_marks_stale_search(tmp_path):
    raw = catalog([build(2, "new")])
    now = [1800000000.0]
    http = FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, raw)})
    first = index.Catalog(http, archive=True, cache_dir=tmp_path, wall_clock=lambda: now[0])
    assert first.info(PKG)[1].code == 2
    assert len(http.calls) == 1
    offline = FakeHttp(raw={ARCHIVE + "entry.jar": net.FetchError("offline")})
    second = index.Catalog(offline, archive=True, cache_dir=tmp_path, wall_clock=lambda: now[0])
    assert second.info(PKG)[1].code == 2 and offline.calls == []
    now[0] += index.TTL + 1
    third = index.Catalog(offline, archive=True, cache_dir=tmp_path, wall_clock=lambda: now[0])
    assert third.search("telegram")[0].package == PKG and third.stale
    with pytest.raises(net.FetchError, match="offline"):
        third.info(PKG)


def test_catalog_rollback_and_failed_replace_preserve_generation(tmp_path, monkeypatch):
    def stamped(timestamp):
        with zipfile.ZipFile(io.BytesIO(catalog([build(2, "new")]))) as jar:
            data = json.loads(jar.read("index-v1.json"))
        data["repo"]["timestamp"] = timestamp
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as jar:
            jar.comment = b"appshelf-test-index"
            jar.writestr("index-v1.json", json.dumps(data))
        return out.getvalue()
    now = [1800000000.0]
    http = FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, stamped(1800000000000))})
    cat = index.Catalog(http, archive=True, cache_dir=tmp_path, wall_clock=lambda: now[0])
    cat.info(PKG)
    before = cat.cache.read_bytes()
    now[0] += index.TTL + 1
    cat._until = 0
    http.raw[ARCHIVE + "entry.jar"] = (200, {}, stamped(1799999999000))
    with pytest.raises(net.SourceError, match="откат"):
        cat.info(PKG)
    assert cat.cache.read_bytes() == before
    http.raw[ARCHIVE + "entry.jar"] = (200, {}, stamped(1800000001000))
    cat._retry_at = 0
    monkeypatch.setattr(index.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(net.SourceError, match="прежнее поколение сохранено"):
        cat.info(PKG)
    assert cat.cache.read_bytes() == before
    assert list(tmp_path.glob("*.partial")) == []


def test_failed_refresh_is_not_repeated_until_retry(tmp_path, monkeypatch):
    """Каталог не прошёл проверку: следующие приложения и поиски не качают его заново до RETRY."""
    checked = []

    def verify(raw, signer):
        checked.append(signer)
        raise net.SourceError("подпись каталога F-Droid не прошла проверку доверенным ключом")

    monkeypatch.setattr(index, "verify", verify)
    now = [10.0]
    http = FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, catalog([build(2, "new")]))})
    cat = index.Catalog(http, archive=True, clock=lambda: now[0], cache_dir=tmp_path)
    for _ in range(3):
        with pytest.raises(net.SourceError, match="подпись"):
            cat.info(PKG)
    with pytest.raises(net.SourceError, match="подпись"):
        cat.search("telegram")
    assert len(http.calls) == len(checked) == 1
    now[0] += index.RETRY
    monkeypatch.setattr(index, "verify", lambda raw, signer: None)
    assert cat.info(PKG)[1].code == 2 and len(http.calls) == 2


def test_spent_search_budget_is_not_remembered_as_catalog_failure(tmp_path):
    """Поиск исчерпал свой срок до каталога: следующий вызов без этого срока обновляет каталог, а не получает отказ."""
    http = FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, catalog([build(2, "new")]))})
    cat = index.Catalog(http, archive=True, clock=lambda: 10.0, cache_dir=tmp_path)

    def spent():
        raise net.FetchError("поиск занял слишком долго")

    with pytest.raises(net.FetchError, match="слишком долго"):
        cat.apps(spent)
    assert not http.calls and cat.info(PKG)[1].code == 2


def test_catalog_rejects_unknown_cache_format_and_expired_signed_timestamp(tmp_path):
    (tmp_path / "repo.json").write_text('{"schema":0}', encoding="utf-8")
    with pytest.raises(net.SourceError, match="кэш.*повреждён"):
        index.Catalog(FakeHttp(), cache_dir=tmp_path).info(PKG)
    cat = index.Catalog(FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, catalog())}), archive=True,
                        wall_clock=lambda: 1800000000.0 + index.MAX_AGE + 1)
    with pytest.raises(net.SourceError, match="устаревший"):
        cat.search("telegram")


def test_v2_signed_entry_authenticates_json_and_normalized_apk_hash(tmp_path):
    digest = hashlib.sha256(b"APK").hexdigest()
    body = json.dumps({"packages": {PKG: {"metadata": {"name": {"en-US": "Telegram FOSS"}}, "versions": {
        digest: {"file": {"name": "/test.apk", "sha256": digest}, "manifest": {"versionName": "2",
            "versionCode": 2, "signer": {"sha256": ["a" * 64]}, "nativecode": ["arm64-v8a"]}}}}}}).encode()
    entry = {"timestamp": 1800000000000, "index": {"name": "/index-v2.json", "size": len(body),
             "sha256": hashlib.sha256(body).hexdigest()}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as jar:
        jar.comment = b"appshelf-test-index"
        jar.writestr("entry.json", json.dumps(entry))
    http = FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, out.getvalue()),
                         ARCHIVE + "index-v2.json": (200, {}, body)})
    cat = index.Catalog(http, archive=True, cache_dir=tmp_path)
    app, selected = cat.info(PKG)
    assert app.name == "Telegram FOSS" and selected.sha256 == digest and selected.signer == "a" * 64
    before = cat.cache.read_bytes()
    cat._until = 0
    http.raw[ARCHIVE + "index-v2.json"] = (200, {}, body.replace(b"Telegram", b"Evilgram"))
    with pytest.raises(net.SourceError, match="SHA-256 подписанного"):
        cat.info(PKG)
    assert cat.cache.read_bytes() == before
    http.raw[ARCHIVE + "index-v2.json"] = (200, {"content-encoding": "gzip"}, b"damaged-gzip")
    cat._retry_at = 0
    with pytest.raises(net.SourceError, match="сжатый каталог.*повреждён"):
        cat.info(PKG)
    assert cat.cache.read_bytes() == before


def test_java_verifies_the_same_file_the_catalog_is_read_from(monkeypatch):
    """Подписан только index-v1.json, рядом неподписанный entry.json без "index": каталог берётся из entry.json,
    значит Java проверяет именно его — и отказывает. JSON до успешной проверки не разбирается."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(catalog([build(2, "new")]))) as signed, zipfile.ZipFile(out, "w") as jar:
        jar.writestr("index-v1.json", signed.read("index-v1.json"))
        jar.writestr("entry.json", json.dumps({"repo": {"timestamp": 1800000000000}, "apps": [], "packages": {}}))
    checked = []

    def java(args, **kw):  # подписан только index-v1.json
        checked.append(args[-1])
        return SimpleNamespace(returncode=0 if args[-1] == "index-v1.json" else 1, stdout=b"verified")

    monkeypatch.setattr(index.subprocess, "run", java)
    monkeypatch.setattr(index.json, "loads", lambda *a, **kw: pytest.fail("JSON разобран до проверки"))
    with pytest.raises(net.SourceError, match="подпись"):
        index.Catalog(FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, out.getvalue())}), archive=True).info(PKG)
    assert checked == ["entry.json"]


def test_jar_with_oversized_directory_is_refused_before_zipfile(monkeypatch):
    """В jar каталога — JSON и META-INF подписи; оглавление из тысяч записей zipfile раздул бы в память до проверки
    подписи — отказ по EOCD, ZipFile не создаётся."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(catalog([build(2, "new")]))) as signed, zipfile.ZipFile(out, "w") as jar:
        jar.writestr("index-v1.json", signed.read("index-v1.json"))
        for n in range(index.JAR_ENTRIES):
            jar.writestr(f"META-INF/{n}", b"")
    raw = out.getvalue()
    monkeypatch.undo()  # настоящий verify: подставной из conftest сам открывает ZipFile
    monkeypatch.setattr(index.zipfile, "ZipFile", lambda *a, **kw: pytest.fail("ZipFile до проверки оглавления"))
    for check in (index.verify, index.parse, index.index_timestamp):
        with pytest.raises(net.SourceError, match="слишком большой"):
            check(raw)


def test_refresh_parses_catalog_json_once(monkeypatch):
    """index-v1.json — до 192 МиБ: время, приложения и версии берутся из одного разбора."""
    loads, calls = index.json.loads, []
    monkeypatch.setattr(index.json, "loads", lambda *a, **kw: calls.append(1) or loads(*a, **kw))
    cat = index.Catalog(FakeHttp(raw={ARCHIVE + "entry.jar": (200, {}, catalog([build(2, "new")]))}), archive=True)
    assert cat.info(PKG)[1].code == 2 and len(calls) == 1
