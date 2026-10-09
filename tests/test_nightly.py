import socket

from appshelf import jobs, nightly, people, store
from appshelf.config import GB
from appshelf.ipatool import SessionExpired
from helpers import FakeTool, make_account, owner, seed_app, set_session
from ipa_factory import make_ipa


def stamp(cfg) -> str:
    p = cfg.status_dir / nightly.STAMP
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def ready(conn, cfg, clock, versions=("1.0",), app_id=123, acct=None):
    acct = acct or owner(conn, clock)
    seed_app(conn, cfg, clock, app_id=app_id, versions=versions, make_dirs=True, acct=acct)
    for j in conn.execute("SELECT id FROM jobs WHERE status='queued'").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    set_session(conn, acct.id, "ok")
    return acct


def test_new_version_becomes_current_previous_kept_older_removed(ctx, cfg, conn, clock, tmp_path):
    acct = ready(conn, cfg, clock, versions=("1.0", "1.1"))  # 1.1 — current (ext 901), 1.0 — previous
    ctx.tool.latest[123] = "902"
    ctx.tool.ipas[123] = make_ipa(tmp_path / "n.ipa", item_id=123, version="1.2", external_id="902")
    result = nightly.run(ctx.env, conn)
    assert {r["role"]: r["version"] for r in conn.execute("SELECT * FROM versions")} == \
        {"current": "1.2", "previous": "1.1"}
    assert not (cfg.shelf_root(acct) / "123-1.0").exists()
    assert result.startswith("owner@example: обновлено 1, без изменений 0, ошибок 0")
    assert stamp(cfg) == clock.iso() and store.get_state(conn, "nightly_result") == result
    assert conn.execute("SELECT status FROM jobs WHERE kind='update'").fetchone()["status"] == "done"


def test_same_version_downloads_nothing(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)  # ext 900
    ctx.tool.latest[123] = "900"
    assert nightly.run(ctx.env, conn).startswith("owner@example: обновлено 0, без изменений 1")
    assert [c[0] for c in ctx.tool.calls] == ["list_purchases", "latest_version_id"]


def test_without_session_calls_nothing_but_stamps(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    set_session(conn, 1, "expired")
    assert nightly.run(ctx.env, conn) == "пропущена: нет входа ни в один Apple ID"
    assert ctx.tool.calls == [] and stamp(cfg) == clock.iso()


def test_session_expiry_mails_once(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    ctx.tool.errors["list_purchases"] = SessionExpired("session_expired", "")
    assert nightly.run(ctx.env, conn).startswith("owner@example: остановлена")
    clock.advance(86400)
    nightly.run(ctx.env, conn)
    assert ctx.sent == ["appshelf: нужен вход в Apple ID"]


def test_expired_apple_id_does_not_stop_others(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock)
    petr = ready(conn, cfg, clock, app_id=7, acct=make_account(conn, clock, email="petr@example", role="member",
                                                                legacy=False))
    ctx.tool.errors["list_purchases"] = SessionExpired("session_expired", "")
    petr_tool = ctx.tools.setdefault(petr.id, FakeTool())
    petr_tool.latest[7] = "901"
    petr_tool.ipas[7] = make_ipa(tmp_path / "p.ipa", item_id=7, version="1.1", external_id="901")
    result = nightly.run(ctx.env, conn)
    assert result == ("owner@example: остановлена: истёк вход в Apple ID | "
                      "petr@example: обновлено 1, без изменений 0, ошибок 0; новые версии: СберБанк Онлайн")
    assert people.get_account(conn, 1).session == "expired"
    assert store.current_version(conn, petr.id, 7)["version"] == "1.1"
    assert ctx.sent == ["appshelf: нужен вход в Apple ID"]


def test_low_space_skips_download(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    ctx.tool.latest[123] = "901"
    ctx.free["bytes"] = 2 * GB
    assert "ошибок 1" in nightly.run(ctx.env, conn)
    app = store.list_apps(conn, 1)[0]
    assert app.status == "ok" and "мало места" in app.last_error   # прежняя версия ставится, «Повторить» не нужен
    assert store.current_version(conn, 1, 123)["external_version_id"] == "900"
    assert ctx.sent == [f"appshelf: мало места на {socket.gethostname()}"]


def test_failed_publish_is_retried(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock, versions=())
    store.set_app_status(conn, 1, 123, "error", "сбой")
    ctx.tool.latest[123] = "900"
    ctx.tool.ipas[123] = make_ipa(tmp_path / "r.ipa", item_id=123, version="1.0", external_id="900")
    nightly.run(ctx.env, conn)
    assert store.list_apps(conn, 1)[0].status == "ok"


def test_error_app_with_same_version_back_to_ok(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    store.set_app_status(conn, 1, 123, "error", "временный сбой")
    ctx.tool.latest[123] = "900"
    nightly.run(ctx.env, conn)
    assert store.list_apps(conn, 1)[0].status == "ok"


def test_result_for_shows_only_own_apple_id():
    both = "owner@example: обновлено 1, без изменений 0, ошибок 0 | petr@example: остановлена: истёк вход в Apple ID"
    assert nightly.result_for(both, "petr@example") == "остановлена: истёк вход в Apple ID"
    assert nightly.result_for(both, "ivan@example") == nightly.NOT_IN_RUN       # не было входа — в прогон не попал
    assert nightly.result_for(nightly.NO_LOGIN, "petr@example") == nightly.NO_LOGIN
    v1 = "обновлено 0, без изменений 3, ошибок 0"                                # итог до перехода — без адресов
    assert nightly.result_for(v1, "owner@example") == v1


def test_second_nightly_does_nothing_while_first_runs(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    with jobs.nightly_lock(cfg) as got:
        assert got
        assert nightly.run(ctx.env, conn) == "уже идёт другая ночная проверка"
    assert ctx.tool.calls == [] and stamp(cfg) == ""
