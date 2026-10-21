"""«Проверить обновление» одного приложения из каталога и итог проверки у карточки (apps.check_result)."""
from appshelf import nightly, store
from appshelf.ipatool import IpatoolError
from helpers import TOKEN, make_account, seed_app, set_session
from ipa_factory import make_ipa

MANIFEST = f"https://apps.example/d/{TOKEN}/123-1.0/manifest.plist"


def ready(conn, cfg, clock, acct=None, app_id=123):
    """Приложение с рабочей current 1.0 (ext 900), задание публикации завершено, вход есть."""
    seed_app(conn, cfg, clock, app_id=app_id, versions=("1.0",), make_dirs=True, acct=acct)
    for j in conn.execute("SELECT id FROM jobs WHERE status='queued'").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    set_session(conn, (acct.id if acct else 1), "ok")


def app_row(conn, app_id=123):
    return conn.execute("SELECT status, last_error, checked_at, check_result FROM apps WHERE app_id=?",
                        (app_id,)).fetchone()


def queued(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish' AND status='queued'").fetchone()[0]


def test_menu_has_check_and_double_click_makes_one_job(web, conn, cfg, clock):
    ready(conn, cfg, clock)
    assert 'action="/apps/123/check"' in web.client.get("/").text
    for _ in range(2):
        assert web.client.post("/apps/123/check", follow_redirects=False).status_code == 303
    assert queued(conn) == 1 and app_row(conn)["status"] == "queued"
    assert 'action="/apps/123/check"' not in web.client.get("/").text        # уже в очереди


def test_same_version_downloads_nothing(web, conn, cfg, clock):
    ready(conn, cfg, clock)
    web.tool.latest[123] = "900"
    web.client.post("/apps/123/check")
    web.app.state.worker.tick()
    row = app_row(conn)
    assert (row["status"], row["check_result"], row["checked_at"]) == ("ok", "same", clock.iso())
    assert all(c[0] != "download" for c in web.tool.calls)
    assert store.current_version(conn, 1, 123)["version"] == "1.0"
    t = clock.iso()
    assert f"проверено {t[8:10]}.{t[5:7]} {t[11:16]} UTC — новее нет" in web.client.get("/").text


def test_newer_version_is_downloaded(web, conn, cfg, clock, tmp_path):
    ready(conn, cfg, clock)
    web.tool.latest[123] = "901"
    web.tool.ipas[123] = make_ipa(tmp_path / "n.ipa", item_id=123, version="1.1", external_id="901")
    web.client.post("/apps/123/check")
    web.app.state.worker.tick()
    assert app_row(conn)["check_result"] == "updated"
    assert store.current_version(conn, 1, 123)["version"] == "1.1"
    assert "UTC — скачана новая версия" in web.client.get("/").text


def test_refusal_keeps_current_installable(web, conn, cfg, clock):
    ready(conn, cfg, clock)
    web.tool.errors["latest_version_id"] = IpatoolError("network", "сбой связи")
    web.client.post("/apps/123/check")
    web.app.state.worker.tick()
    row = app_row(conn)
    assert (row["status"], row["check_result"], row["last_error"]) == ("ok", "error", "ipatool: сбой связи")
    assert store.current_version(conn, 1, 123)["version"] == "1.0"
    text = web.client.get("/").text
    assert "UTC — не удалось: ipatool: сбой связи" in text and MANIFEST in text
    assert "не удалось обновить" not in text and 'action="/apps/123/check"' in text   # можно нажать снова


def test_nightly_records_check_result(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock)
    ready(conn, cfg, clock, app_id=124)
    ready(conn, cfg, clock, app_id=125)
    ctx.tool.latest.update({123: "900", 124: "901"})
    ctx.tool.ipas[124] = make_ipa(tmp_path / "n.ipa", item_id=124, version="1.1", external_id="901")
    ctx.tool.on_call = lambda name, *args: name == "latest_version_id" and args[0] == 125 and \
        ctx.tool.errors.setdefault("latest_version_id", IpatoolError("network", "сбой связи"))
    nightly.run(ctx.env, conn)
    assert {a: app_row(conn, a)["check_result"] for a in (123, 124, 125)} == \
        {123: "same", 124: "updated", 125: "error"}
    assert app_row(conn, 125)["checked_at"] == clock.iso() and app_row(conn, 125)["status"] == "ok"


def test_foreign_or_unpublished_app_is_not_found(web, conn, cfg, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    ready(conn, cfg, clock, acct=petr, app_id=7)
    assert web.client.post("/apps/7/check").status_code == 404
    assert web.client.post("/apps/999/check").status_code == 404
    assert queued(conn) == 0 and app_row(conn, 7)["status"] == "ok"


def test_without_login_job_waits(web, conn, cfg, clock):
    ready(conn, cfg, clock)
    set_session(conn, 1, "expired")
    web.client.post("/apps/123/check")
    assert web.app.state.worker.tick() is False and web.tool.calls == []
    assert queued(conn) == 1 and "ждёт входа в Apple ID" in web.client.get("/").text
