from apk_factory import cert_sha, make_apk
from appshelf import nightly, store
from appshelf.android import db, fetch, net
from appshelf.android.sources import Latest
from helpers import owner
from test_nightly import ready, stamp

URL = "https://bank.example/a.apk"


def card(ctx, conn, clock, tmp_path, package, source, ref, code=1):
    """Карточка с текущей версией code — как после добавления."""
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    ctx.sources.apks[ref] = make_apk(tmp_path / f"{package}-{code}.apk", package=package, version_code=code)
    db.put_app(conn, shelf.id, package, package, source, ref, clock.iso())
    job = db.enqueue(conn, shelf.id, "add", clock.iso(), package=package,
                     payload={"source": source, "ref": ref, "name": ""})
    fetch.run_job(ctx.env, conn, db.take_job(conn))
    assert conn.execute("SELECT status FROM android_jobs WHERE id=?", (job,)).fetchone()["status"] == "done"
    return shelf


def two_apps(ctx, conn, clock, tmp_path):
    """ru.a из RuStore (новее нет), ru.b по ссылке (файл сменился, внутри versionCode 2)."""
    shelf = card(ctx, conn, clock, tmp_path, "ru.a", "rustore", "ru.a")
    card(ctx, conn, clock, tmp_path, "ru.b", "link", URL)
    ctx.sources._latest.update({"ru.a": Latest(1, "", True), URL: Latest(None, '"new"||5', True)})
    ctx.sources.apks[URL] = make_apk(tmp_path / "b2.apk", package="ru.b", version_code=2)
    return shelf


def codes(conn) -> dict:
    return {(r["package"], r["role"]): r["version_code"] for r in conn.execute("SELECT * FROM android_versions")}


def test_nightly_checks_android_and_appends_result(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock)
    ctx.tool.latest[123] = "900"
    shelf = two_apps(ctx, conn, clock, tmp_path)
    result = nightly.run(ctx.env, conn)
    assert result.startswith("owner@example: обновлено 0, без изменений 1")
    assert result.endswith(" | Android: обновлено 1, без изменений 1, ошибок 0")
    assert codes(conn) == {("ru.a", "current"): 1, ("ru.b", "current"): 2, ("ru.b", "previous"): 1}
    b = db.get_app(conn, shelf.id, "ru.b")
    assert (b["check_result"], b["checked_at"], b["source_etag"]) == ("updated", clock.iso(), '"new"||5')
    assert db.get_app(conn, shelf.id, "ru.a")["check_result"] == "same"
    assert store.get_state(conn, "nightly_result") == result and stamp(cfg) == clock.iso()
    assert not (cfg.tmp_dir / "nightly" / "android").exists() or \
        not list((cfg.tmp_dir / "nightly" / "android").iterdir())


def test_nightly_android_runs_without_apple_login(ctx, cfg, conn, clock, tmp_path):
    owner(conn, clock)                               # Apple ID есть, входа нет
    two_apps(ctx, conn, clock, tmp_path)
    result = nightly.run(ctx.env, conn)
    assert result == f"{nightly.NO_LOGIN} | Android: обновлено 1, без изменений 1, ошибок 0"
    assert ctx.tool.calls == [] and stamp(cfg) == clock.iso()


def test_nightly_without_android_apps_keeps_old_result(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    ctx.tool.latest[123] = "900"
    assert nightly.run(ctx.env, conn) == "owner@example: обновлено 0, без изменений 1, ошибок 0"
    owner(conn, clock)
    conn.execute("UPDATE accounts SET session='expired'")
    assert nightly.run(ctx.env, conn) == nightly.NO_LOGIN


def test_android_error_does_not_stop_others(ctx, cfg, conn, clock, tmp_path):
    owner(conn, clock)
    shelf = two_apps(ctx, conn, clock, tmp_path)
    ctx.sources.fail["ru.a"] = net.SourceError("RuStore ответил в неизвестном формате")
    result = nightly.run(ctx.env, conn)
    assert result.endswith("Android: обновлено 1, без изменений 0, ошибок 1")
    a = db.get_app(conn, shelf.id, "ru.a")
    assert (a["status"], a["check_result"], a["last_error"]) == \
        ("ok", "error", "RuStore ответил в неизвестном формате")
    assert codes(conn)[("ru.b", "current")] == 2


def test_upload_source_not_checked(ctx, cfg, conn, clock, tmp_path):
    owner(conn, clock)
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    up = cfg.tmp_dir / "web" / "up.apk"
    up.parent.mkdir(parents=True)
    make_apk(up)
    db.enqueue(conn, shelf.id, "upload", clock.iso(), payload={"path": str(up), "name": ""})
    fetch.run_job(ctx.env, conn, db.take_job(conn))
    ctx.sources.calls.clear()
    assert nightly.run(ctx.env, conn) == nightly.NO_LOGIN
    assert ctx.sources.calls == []


def test_nightly_checks_new_sources(ctx, cfg, conn, clock, tmp_path):
    owner(conn, clock)
    ctx.sources.signatures["ru.p"] = cert_sha(b"CERT-A")                  # APKPure сверяется с RuStore
    shelf = card(ctx, conn, clock, tmp_path, "ru.g", "appgallery", "ru.g")
    card(ctx, conn, clock, tmp_path, "ru.f", "fdroid", "ru.f")
    card(ctx, conn, clock, tmp_path, "ru.p", "apkpure", "ru.p")
    ctx.sources._latest.update({"ru.g": Latest(2, "", True), "ru.f": Latest(None, "fd:1", False)})
    ctx.sources.apks["ru.g"] = make_apk(tmp_path / "g2.apk", package="ru.g", version_code=2)
    ctx.sources.fail["ru.p"] = net.SourceError("нет в APKPure")
    ctx.sources.calls.clear()
    result = nightly.run(ctx.env, conn)
    assert result.endswith("Android: обновлено 1, без изменений 1, ошибок 1")
    assert codes(conn) == {("ru.g", "current"): 2, ("ru.g", "previous"): 1, ("ru.f", "current"): 1,
                           ("ru.p", "current"): 1}
    assert ("fetch", "fdroid", "ru.f") not in ctx.sources.calls                 # метка та же — не качаем
    f, p = db.get_app(conn, shelf.id, "ru.f"), db.get_app(conn, shelf.id, "ru.p")
    assert (f["check_result"], f["source_etag"]) == ("same", "fd:1")
    assert (p["status"], p["check_result"], p["last_error"]) == ("ok", "error", "нет в APKPure")


def test_notice_survives_same_check(ctx, cfg, conn, clock, tmp_path):
    ctx.sources.signatures["ru.f"] = cert_sha(b"CERT-B")                  # в RuStore подписан иначе
    shelf = card(ctx, conn, clock, tmp_path, "ru.f", "fdroid", "ru.f")
    assert db.get_app(conn, shelf.id, "ru.f")["last_error"] == fetch.SIGNER_DIFFERS
    ctx.sources._latest["ru.f"] = Latest(None, "fd:1", False)
    assert fetch.check_all(ctx.env, conn, tmp_path / "work") == "Android: обновлено 0, без изменений 1, ошибок 0"
    app = db.get_app(conn, shelf.id, "ru.f")
    assert (app["status"], app["check_result"], app["last_error"]) == ("ok", "same", fetch.SIGNER_DIFFERS)


def test_check_all_counts_pending_as_updated(ctx, cfg, conn, clock, tmp_path):
    card(ctx, conn, clock, tmp_path, "ru.b", "link", URL)
    ctx.sources._latest[URL] = Latest(None, "e2||", True)
    ctx.sources.apks[URL] = make_apk(tmp_path / "other.apk", package="ru.b", version_code=2, cert=b"CERT-B")
    assert fetch.check_all(ctx.env, conn, tmp_path / "work") == "Android: обновлено 1, без изменений 0, ошибок 0"
    assert codes(conn) == {("ru.b", "current"): 1, ("ru.b", "pending"): 2}
    assert fetch.check_all(ctx.env, conn, tmp_path / "work") == "Android: обновлено 0, без изменений 1, ошибок 0"
    assert fetch.check_all(ctx.env, conn, tmp_path / "work") != "" and not list((tmp_path / "work").iterdir())
