"""Ночная проверка (spec §7.4): история, новые версии опубликованного, отметка для Zabbix.

Без входа в Apple ID ничего не вызывает и только обновляет отметку: о входе пишет письмо, а Zabbix
срабатывает, лишь если сама проверка не отработала (нет nightly.stamp > 26 ч)."""
from __future__ import annotations

import shutil

from . import removed, store
from .ipatool import SessionExpired
from .jobs import Env, LowSpace, describe, fetch_version, low_space, mark_expired, nightly_lock, refresh_history

STAMP = "nightly.stamp"


def run(env: Env, c) -> str:
    # замок на весь прогон: по нему appshelf-web узнаёт, что упавшая проверка уже не идёт (jobs.Worker.tick)
    with nightly_lock(env.cfg) as got:
        if not got:
            return "уже идёт другая ночная проверка"
        return _run(env, c)


def _run(env: Env, c) -> str:
    work = env.cfg.tmp_dir / "nightly"
    shutil.rmtree(work, ignore_errors=True)
    store.fail_stale_updates(c, env.now())
    if store.get_state(c, "session") != "ok":
        return _finish(env, c, "пропущена: нет входа в Apple ID")
    notes = []
    try:
        refresh_history(env, c)
    except SessionExpired:
        mark_expired(env, c)
        return _finish(env, c, "остановлена: истёк вход в Apple ID")
    except Exception as e:  # история не обновилась — версии всё равно проверяем
        notes.append(f"история: {describe(e)}")
    try:
        added, _, _ = removed.check(env, c, full=True)  # ночью — весь справочник, кнопка пропускает «нет лицензии»
        if added:
            notes.append(f"удалённых из App Store добавлено в историю: {added}")
    except SessionExpired:
        mark_expired(env, c)
        return _finish(env, c, "остановлена: истёк вход в Apple ID")
    except Exception as e:
        notes.append(f"справочник удалённых: {describe(e)}")
    updated = same = errors = 0
    for app in store.apps_to_check(c):
        app_id, job_id = app["app_id"], None
        try:
            latest = env.tool.latest_version_id(app_id)
            cur = store.current_version(c, app_id)
            if cur is not None and cur["external_version_id"] == latest:
                same += 1
                store.set_app_status(c, app_id, "ok")
            else:
                job_id = store.start_job(c, "update", app_id, env.now())
                store.set_app_status(c, app_id, "downloading")
                fetch_version(env, c, app_id, work)
                store.finish_job(c, job_id, "done", env.now())
                updated += 1
            store.set_checked(c, app_id, env.now())
        except SessionExpired:
            if job_id is not None:
                store.finish_job(c, job_id, "error", env.now(), "истёк вход в Apple ID")
            store.set_app_status(c, app_id, app["status"], app["last_error"])
            mark_expired(env, c)
            return _finish(env, c, f"остановлена: истёк вход в Apple ID (обновлено {updated})")
        except store.AppGone:
            if job_id is not None:
                store.finish_job(c, job_id, "cancelled", env.now(), "снято с публикации во время скачивания")
        except Exception as e:  # ошибка одного приложения не останавливает остальные
            if isinstance(e, LowSpace):
                low_space(env, c, e)
            errors += 1
            msg = describe(e)
            if job_id is not None:
                store.finish_job(c, job_id, "error", env.now(), msg)
            store.set_app_status(c, app_id, "error", msg)
    return _finish(env, c, "; ".join([f"обновлено {updated}, без изменений {same}, ошибок {errors}", *notes]))


def _finish(env: Env, c, result: str) -> str:
    now = env.now()
    store.set_state(c, "nightly_last", now)
    store.set_state(c, "nightly_result", result)
    env.cfg.status_dir.mkdir(parents=True, exist_ok=True)
    (env.cfg.status_dir / STAMP).write_text(now + "\n", encoding="utf-8")
    return result
