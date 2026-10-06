from appshelf import jobs, nightly, store
from appshelf.config import GB
from appshelf.ipatool import SessionExpired
from helpers import seed_app
from ipa_factory import make_ipa


def stamp(cfg) -> str:
    p = cfg.status_dir / nightly.STAMP
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def ready(conn, cfg, clock, versions=("1.0",)):
    seed_app(conn, cfg, clock, versions=versions, make_dirs=True)
    for j in conn.execute("SELECT id FROM jobs").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    store.set_state(conn, "session", "ok")


def test_new_version_becomes_current_previous_kept_older_removed(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock, versions=("1.0", "1.1"))  # 1.1 — current (ext 901), 1.0 — previous
    ctx.tool.latest[123] = "902"
    ctx.tool.ipas[123] = make_ipa(tmp_path / "n.ipa", item_id=123, version="1.2", external_id="902")
    result = nightly.run(ctx.env, conn)
    assert {r["role"]: r["version"] for r in conn.execute("SELECT * FROM versions")} == \
        {"current": "1.2", "previous": "1.1"}
    assert not (cfg.pub_root / "123-1.0").exists()
    assert result.startswith("обновлено 1, без изменений 0, ошибок 0")
    assert stamp(cfg) == clock.iso() and store.get_state(conn, "nightly_result") == result
    assert conn.execute("SELECT status FROM jobs WHERE kind='update'").fetchone()["status"] == "done"


def test_same_version_downloads_nothing(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)  # ext 900
    ctx.tool.latest[123] = "900"
    assert nightly.run(ctx.env, conn).startswith("обновлено 0, без изменений 1")
    assert [c[0] for c in ctx.tool.calls] == ["list_purchases", "latest_version_id"]


def test_without_session_calls_nothing_but_stamps(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    store.set_state(conn, "session", "expired")
    assert nightly.run(ctx.env, conn) == "пропущена: нет входа в Apple ID"
    assert ctx.tool.calls == [] and stamp(cfg) == clock.iso()


def test_session_expiry_mails_once(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    ctx.tool.errors["list_purchases"] = SessionExpired("session_expired", "")
    assert nightly.run(ctx.env, conn).startswith("остановлена")
    clock.advance(86400)
    nightly.run(ctx.env, conn)
    assert ctx.sent == ["appshelf: нужен вход в Apple ID"]


def test_low_space_skips_download(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    ctx.tool.latest[123] = "901"
    ctx.free["bytes"] = 2 * GB
    assert "ошибок 1" in nightly.run(ctx.env, conn)
    assert store.list_apps(conn)[0].status == "error"
    assert store.current_version(conn, 123)["external_version_id"] == "900"
    assert ctx.sent == ["appshelf: мало места на debian"]


def test_failed_publish_is_retried(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock, versions=())
    store.set_app_status(conn, 123, "error", "сбой")
    ctx.tool.latest[123] = "900"
    ctx.tool.ipas[123] = make_ipa(tmp_path / "r.ipa", item_id=123, version="1.0", external_id="900")
    nightly.run(ctx.env, conn)
    assert store.list_apps(conn)[0].status == "ok"


def test_error_app_with_same_version_back_to_ok(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    store.set_app_status(conn, 123, "error", "временный сбой")
    ctx.tool.latest[123] = "900"
    nightly.run(ctx.env, conn)
    assert store.list_apps(conn)[0].status == "ok"


def test_second_nightly_does_nothing_while_first_runs(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    with jobs.nightly_lock(cfg) as got:
        assert got
        assert nightly.run(ctx.env, conn) == "уже идёт другая ночная проверка"
    assert ctx.tool.calls == [] and stamp(cfg) == ""
