"""Ночная проверка (spec 2026-10-05 §7.4, 2026-10-08 §8): по каждому Apple ID с входом — история, справочник
удалённых, новые версии опубликованного; отметка для Zabbix.

Apple ID без входа пропускаются: о входе пишет письмо (владельцу), а Zabbix срабатывает, лишь если сама
проверка не отработала (нет nightly.stamp > 26 ч)."""
from __future__ import annotations

import shutil
from datetime import datetime

from . import people, removed, store
from .ipatool import SessionExpired
from .jobs import (Env, LowSpace, app_failed, describe, fetch_version, low_space, mark_expired, move_shelves,
                   nightly_lock, refresh_history)

STAMP = "nightly.stamp"
EXPIRED = "остановлена: истёк вход в Apple ID"
NO_LOGIN = "пропущена: нет входа ни в один Apple ID"
NOT_IN_RUN = "пропущена: не было входа в этот Apple ID"
SEP = " | "


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
    move_shelves(env.cfg, c)  # не перенёс старт appshelf-web (архив не был смонтирован) — пробуем каждую ночь
    accounts = [a for a in people.all_accounts(c) if a.session == "ok"]
    if not accounts:
        return _finish(env, c, NO_LOGIN)
    # справочник удалённых целиком (~480 запросов к Apple) — раз в неделю на Apple ID, в «свой» день недели
    weekday = datetime.fromisoformat(env.now()).weekday()
    return _finish(env, c, SEP.join(
        f"{a.email}: {_run_account(env, c, a, work, full=a.id % 7 == weekday)}" for a in accounts))


def _run_account(env: Env, c, acct, work, full: bool) -> str:
    notes = []
    try:
        refresh_history(env, c, acct)
    except SessionExpired:
        mark_expired(env, c, acct)
        return EXPIRED
    except Exception as e:  # история не обновилась — версии всё равно проверяем
        notes.append(f"история: {describe(e)}")
    try:
        added, _, _ = removed.check(env, c, acct, full=full)
        if added:
            notes.append(f"удалённых из App Store добавлено в историю: {added}")
    except SessionExpired:
        mark_expired(env, c, acct)
        return EXPIRED
    except Exception as e:
        notes.append(f"справочник удалённых: {describe(e)}")
    tool = env.tools(acct)
    updated = same = errors = 0
    names = []
    for app in store.apps_to_check(c, acct.id):
        app_id, job_id = app["app_id"], None
        try:
            latest = tool.latest_version_id(app_id)
            cur = store.current_version(c, acct.id, app_id)
            if cur is not None and cur["external_version_id"] == latest:
                same += 1
                store.set_app_status(c, acct.id, app_id, "ok")
            else:
                job_id = store.start_job(c, acct.id, "update", app_id, env.now())
                store.set_app_status(c, acct.id, app_id, "downloading")
                fetch_version(env, c, acct, app_id, work)
                store.finish_job(c, job_id, "done", env.now())
                updated += 1
                names.append(app["name"].replace("|", "/"))  # « | » делит итог по Apple ID (result_for)
            store.set_checked(c, acct.id, app_id, env.now())
        except SessionExpired:
            if job_id is not None:
                store.finish_job(c, job_id, "error", env.now(), "истёк вход в Apple ID")
            store.set_app_status(c, acct.id, app_id, app["status"], app["last_error"])
            mark_expired(env, c, acct)
            return f"{EXPIRED} (обновлено {updated})"
        except store.AppGone:
            if job_id is not None:
                store.finish_job(c, job_id, "cancelled", env.now(), "снято с публикации во время скачивания")
        except Exception as e:  # ошибка одного приложения не останавливает остальные
            if isinstance(e, LowSpace):
                low_space(env, c, e)
            errors += 1
            if job_id is not None:
                store.finish_job(c, job_id, "error", env.now(), describe(e))
            app_failed(c, acct.id, app_id, e)
    if names:  # удалённое из App Store сам App Store не обновит — новую версию ставят с полки
        notes.append("новые версии: " + ", ".join(names))
    return "; ".join([f"обновлено {updated}, без изменений {same}, ошибок {errors}", *notes])


def result_for(result: str, email: str) -> str:
    """Итог для полки одного Apple ID: его часть «<email>: …» без адреса — адреса других Apple ID участникам не
    показываем. Итог без адресов (пропущена целиком, прогон до перехода на несколько Apple ID) — как есть."""
    prefix = f"{email}: "
    for part in result.split(SEP):
        if part.startswith(prefix):
            return part[len(prefix):]
    return result if "@" not in result else NOT_IN_RUN


def _finish(env: Env, c, result: str) -> str:
    now = env.now()
    store.set_state(c, "nightly_last", now)
    store.set_state(c, "nightly_result", result)
    env.cfg.status_dir.mkdir(parents=True, exist_ok=True)
    (env.cfg.status_dir / STAMP).write_text(now + "\n", encoding="utf-8")
    return result
