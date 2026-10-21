"""Ход и итог проверки справочника удалённых (jobs.progress): видно, сколько проверено и что не удалось."""
import json

from appshelf import jobs, removed, store
from appshelf.ipatool import IpatoolError
from helpers import make_account, owner, set_session
from ipa_factory import make_ipa

CATALOG = [{"id": 1, "name": "СберБанк Онлайн"}, {"id": 2, "name": "Т-Банк"}, {"id": 3, "name": "ВКонтакте"}]
JOB = "SELECT status, progress FROM jobs WHERE kind='check_removed'"


def progress(conn) -> dict:
    return json.loads(conn.execute(JOB).fetchone()["progress"])


def fail_on(tool, app_id: int):
    """Хук ipatool: list-versions для app_id — технический сбой (не «нет лицензии»)."""
    def hook(name, *args):
        if name == "latest_version_id" and args[0] == app_id:
            tool.errors["latest_version_id"] = IpatoolError("network", "сбой связи")
    return hook


def test_progress_is_written_while_check_goes(ctx, cfg, conn, clock, monkeypatch):
    monkeypatch.setattr(removed, "load", lambda: CATALOG)
    owner(conn, clock)
    set_session(conn, 1, "ok")
    ctx.tool.latest[1] = "900"
    ctx.tool.no_license.add(2)
    seen = []

    def hook(name, *args):
        if name == "latest_version_id":
            p = progress(conn)
            seen.append((p["done"], p["total"], p["added"], p["missing"]))
            fail_on(ctx.tool, 3)(name, *args)

    ctx.tool.on_call = hook
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    jobs.Worker(ctx.env, lambda: store.connect(cfg.db)).tick()
    assert seen == [(0, 3, 0, 0), (1, 3, 1, 0), (2, 3, 1, 1)]               # опрос видит движение посреди обхода
    assert conn.execute(JOB).fetchone()["status"] == "done"
    assert progress(conn) == {"done": 3, "total": 3, "added": 1, "missing": 1, "errors": 1, "failed": [3]}


def test_interrupted_check_keeps_progress_and_goes_on(ctx, cfg, conn, clock, tmp_path, monkeypatch):
    # уступила публикации: ход сохранён, следующий проход продолжает счёт; id с ошибкой спрашивает снова
    monkeypatch.setattr(removed, "load", lambda: CATALOG)
    owner(conn, clock)
    set_session(conn, 1, "ok")
    ctx.tool.no_license.update({1, 2, 3})
    store.upsert_purchases(conn, 1, [{"id": 7, "bundleId": "ru.example.app", "name": "Seven"}], clock.iso())
    ctx.tool.ipas[7] = make_ipa(tmp_path / "7.ipa", item_id=7)
    ctx.tool.errors["latest_version_id"] = IpatoolError("network", "сбой связи")     # id 1 — сбой
    ctx.tool.on_call = lambda name, *args: name == "latest_version_id" and args[0] == 1 and \
        not store.has_app(conn, 1, 7) and store.publish(conn, 1, 7, clock.iso())    # нажали «Опубликовать»
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    w = jobs.Worker(ctx.env, lambda: store.connect(cfg.db))
    w.tick()
    assert conn.execute(JOB).fetchone()["status"] == "queued"
    assert progress(conn) == {"done": 1, "total": 3, "added": 0, "missing": 0, "errors": 1, "failed": [1]}
    w.tick()                                                                         # публикация
    w.tick()
    assert conn.execute(JOB).fetchone()["status"] == "done"
    assert progress(conn) == {"done": 3, "total": 3, "added": 0, "missing": 3, "errors": 0, "failed": []}


def test_merge_progress_counts_across_passes():
    first = {"done": 10, "total": 480, "added": 1, "missing": 7, "errors": 2, "failed": [5, 6]}
    second = {"done": 4, "total": 472, "added": 0, "missing": 4, "errors": 0, "failed": []}
    assert jobs.merge_progress(first, second) == \
        {"done": 12, "total": 480, "added": 1, "missing": 11, "errors": 0, "failed": []}
    many = {"done": 60, "total": 60, "added": 0, "missing": 0, "errors": 60, "failed": list(range(60))}
    assert len(jobs.merge_progress(None, many)["failed"]) == jobs.FAILED_MAX


def test_result_stays_after_check_and_separates_errors(web, conn, monkeypatch):
    monkeypatch.setattr(removed, "load", lambda: CATALOG)
    set_session(conn, 1, "ok")
    web.tool.latest[1] = "900"
    web.tool.no_license.add(2)
    web.tool.on_call = fail_on(web.tool, 3)
    web.client.post("/blocked/check")
    web.app.state.worker.tick()
    text = web.client.get("/blocked").text                                  # страницу открыли после окончания
    at = conn.execute("SELECT finished_at FROM jobs").fetchone()["finished_at"]
    assert f"последняя проверка {at[8:10]}.{at[5:7]} {at[11:16]} UTC: найдено 1, нет лицензии 1, ошибок 1" in text
    assert "ID 3 · ошибка проверки" in text and "ID 2 · нет в аккаунте" in text
    assert "ошибка проверки: 1" in text and "не проверено: 0" in text
    assert "progress" not in web.client.get("/api/status").json()


def test_pages_show_progress_of_running_check(web, conn, clock):
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    job = conn.execute("SELECT id FROM jobs").fetchone()["id"]
    store.set_progress(conn, job, {"done": 5, "total": 480, "added": 1, "missing": 3, "errors": 1, "failed": [9]})
    line = "проверено 5 из 480: найдено 1, нет лицензии 3, ошибок 1"
    assert f'id="progress">{line}</span>' in web.client.get("/blocked").text
    assert f"проверяю справочник удалённых… <span id=\"progress\">{line}</span>" in web.client.get("/history").text
    assert web.client.get("/api/status").json()["progress"] == \
        {"done": 5, "total": 480, "added": 1, "missing": 3, "errors": 1, "text": line}
    assert "s.progress.text" in web.client.get("/blocked").text             # опрос обновляет строку без перезагрузки
    set_session(conn, 1, "expired")
    assert "ждёт входа в Apple ID" in web.client.get("/blocked").text


def test_api_status_shows_progress_of_active_shelf_only(web, conn, cfg, clock):
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1, session="ok")
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, session="ok")
    for a, done in ((second, 1), (petr, 7)):
        store.enqueue_once(conn, a.id, "check_removed", None, clock.iso())
        job = conn.execute("SELECT MAX(id) AS id FROM jobs").fetchone()["id"]
        store.set_progress(conn, job, {"done": done, "total": 9, "added": 0, "missing": done, "errors": 0,
                                       "failed": []})
    assert "progress" not in web.client.get("/api/status").json()          # полка №1: ни вторая своя, ни чужая
    assert "из 9" not in web.client.get("/blocked").text
    web.client.get(f"/acct/{second.id}")
    assert web.client.get("/api/status").json()["progress"]["done"] == 1
    web.client.get(f"/acct/{petr.id}")                                       # чужую полку не выбрать
    assert web.client.get("/api/status").json()["progress"]["done"] == 1
    assert "проверено 1 из 9" in web.client.get("/blocked").text
