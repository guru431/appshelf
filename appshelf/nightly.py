"""Ночная проверка (spec 2026-10-05 §7.4, 2026-10-08 §8, 2026-10-28 §9): по каждому Apple ID с входом — история,
справочник удалённых, новые версии опубликованного; затем Android-полки, еженедельные письма участникам о новых
версиях (digests) и сводка appshelf check без удаления (_integrity, только владельцу); отметка для мониторинга.

Apple ID без входа пропускаются: о входе пишет письмо (владельцу), а мониторинг срабатывает, лишь если сама
проверка не отработала (нет nightly.stamp > 30 ч: сутки + TimeoutStartSec=4h + запас — отметка пишется в конце
прогона). Проблемы целостности — письмо владельцу, одно на эпизод."""
from __future__ import annotations

import shutil
import traceback
from collections import Counter
from datetime import datetime, timedelta

from . import integrity, notify, people, removed, store
from .android import db as android_db, fetch as android_fetch
from .ipatool import SessionExpired
from .jobs import (ACCOUNT_GONE, Env, LowSpace, app_failed, describe, fetch_version, hold, low_space, mark_expired,
                   move_shelves, nightly_lock, refresh_history)

STAMP = "nightly.stamp"
INTEGRITY = "Целостность: "  # часть итога со сводкой appshelf check — только владельцу (_integrity)
INTEGRITY_KEY = "integrity_mail_sent"
INTEGRITY_SUBJECT = "appshelf: проверка целостности нашла проблемы"
EXPIRED = "остановлена: истёк вход в Apple ID"
GONE = "остановлена: Apple ID удалён"
NO_LOGIN = "пропущена: нет входа ни в один Apple ID"
NOT_IN_RUN = "пропущена: не было входа в этот Apple ID"
SEP = " | "
DIGEST_SUBJECT = "appshelf: что обновилось на полках"
DIGEST_DAYS = 7       # письмо участнику — не чаще раза в 7 дней (по датам UTC: время конца проверки плавает)
DIGEST_BACK_DAYS = 14  # прошлое письмо давно (ночи не было, писать было нечего) — новые версии не старше этого
# новые версии полки: текущая скачана позже since и не первая с публикации — как метка «новая версия» в каталоге
# (есть предыдущая или, если её удалили вручную, до текущей уже было скачивание — store.updated_ids)
APPLE_NEW = """SELECT a.name, v.version FROM versions v JOIN apps a ON a.account_id = v.account_id
                   AND a.app_id = v.app_id WHERE v.account_id = ? AND v.role = 'current' AND v.downloaded_at > ?
                   AND (EXISTS (SELECT 1 FROM versions p WHERE p.account_id = v.account_id AND p.app_id = v.app_id
                       AND p.role = 'previous') OR EXISTS (SELECT 1 FROM jobs j WHERE j.account_id = v.account_id
                       AND j.app_id = v.app_id AND j.kind IN ('publish', 'update') AND j.status = 'done'
                       AND j.created_at >= a.published_at AND j.finished_at < v.downloaded_at))"""
ANDROID_NEW = """SELECT a.name, v.version_name AS version FROM android_versions v JOIN android_apps a
                     ON a.shelf_id = v.shelf_id AND a.package = v.package WHERE v.shelf_id = ? AND v.role = 'current'
                     AND v.downloaded_at > ? AND (EXISTS (SELECT 1 FROM android_versions p
                         WHERE p.shelf_id = v.shelf_id AND p.package = v.package AND p.role = 'previous')
                     OR EXISTS (SELECT 1 FROM state s WHERE s.key = 'android_updated:' || v.shelf_id || ':' || v.package
                         AND s.value = CAST(v.version_code AS TEXT)))"""


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
        apple = NO_LOGIN
    else:
        # справочник удалённых целиком (~480 запросов к Apple) — раз в неделю на Apple ID, в «свой» день недели
        weekday = datetime.fromisoformat(env.now()).weekday()
        apple = SEP.join(f"{a.email}: {_run_account(env, c, a, work, full=a.id % 7 == weekday)}" for a in accounts)
    # Android-полкам вход в Apple ID не нужен — их проверяем и без него (spec 2026-10-28 §9)
    android = android_fetch.check_all(env, c, work / "android")
    try:
        digests(env, c)
    except Exception:  # письма не должны оставить проверку без отметки для Zabbix
        traceback.print_exc()
    summary = _integrity(env, c)
    return _finish(env, c, SEP.join(p for p in (apple, android, summary) if p))


def _run_account(env: Env, c, acct, work, full: bool) -> str:
    """Apple ID удалили посреди проверки (AccountGone) — его итог не записываем, проверка этого Apple ID прекращается."""
    notes = []
    try:
        refresh_history(env, c, acct)
    except SessionExpired:
        mark_expired(env, c, acct)
        return EXPIRED
    except store.AccountGone:
        return GONE
    except Exception as e:  # история не обновилась — версии всё равно проверяем
        notes.append(f"история: {describe(e)}")
    try:
        added, _, _ = removed.check(env, c, acct, full=full)
        if added:
            notes.append(f"удалённых из App Store добавлено в историю: {added}")
    except SessionExpired:
        mark_expired(env, c, acct)
        return EXPIRED
    except store.AccountGone:
        return GONE
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
                result = "same"
            else:
                job_id = store.start_job(c, acct.id, "update", app_id, env.now())
                if job_id is None:  # строки apps уже нет: сняли с публикации или удалили Apple ID
                    raise store.AppGone(app_id) if people.get_account(c, acct.id) else store.AccountGone(acct.id)
                store.set_app_status(c, acct.id, app_id, "downloading")
                result = fetch_version(env, c, acct, app_id, work)
                store.finish_job(c, job_id, "done", env.now())
                if result == "same":  # list-versions дал другой id, а IPA — та же версия
                    same += 1
                else:
                    updated += 1
                    names.append(app["name"].replace("|", "/"))  # « | » делит итог по Apple ID (result_for)
            # ошибку записывает app_failed; первое скачивание (current не было) — не итог проверки обновления
            store.set_checked(c, acct.id, app_id, env.now(), result if cur is not None else "")
        except SessionExpired:
            if job_id is not None:
                store.finish_job(c, job_id, "error", env.now(), "истёк вход в Apple ID")
            store.set_app_status(c, acct.id, app_id, app["status"], app["last_error"])
            mark_expired(env, c, acct)
            return f"{EXPIRED} (обновлено {updated})"
        except store.AppGone:
            if job_id is not None:
                store.finish_job(c, job_id, "cancelled", env.now(), "снято с публикации во время скачивания")
        except store.AccountGone:
            if job_id is not None:
                store.finish_job(c, job_id, "cancelled", env.now(), ACCOUNT_GONE)
            return f"{GONE} (обновлено {updated})"
        except Exception as e:  # ошибка одного приложения не останавливает остальные
            if isinstance(e, LowSpace):
                low_space(env, c, e)
            errors += 1
            if job_id is not None:
                store.finish_job(c, job_id, "error", env.now(), describe(e))
            app_failed(c, acct.id, app_id, e, env.now())
    if names:  # удалённое из App Store сам App Store не обновит — новую версию ставят с полки
        notes.append("новые версии: " + ", ".join(names))
    return "; ".join([f"обновлено {updated}, без изменений {same}, ошибок {errors}", *notes])


def result_for(result: str, email: str) -> str:
    """Итог для полки одного Apple ID: его часть «<email>: …» без адреса — адреса других Apple ID участникам не
    показываем. Итог без адресов (пропущена целиком, прогон до перехода на несколько Apple ID) — как есть."""
    prefix = f"{email}: "
    apple_parts = [part for part in result.split(SEP) if not part.startswith(("Android: ", INTEGRITY))]
    for part in apple_parts:
        if part.startswith(prefix):
            return part[len(prefix):]
    apple = SEP.join(apple_parts)
    return apple if "@" not in apple else NOT_IN_RUN


def digests(env: Env, c) -> int:
    """Раз в неделю участнику — новые версии на его полках со времени прошлого письма (первое — за 7 дней): полки
    его Apple ID, его Android-полка, владельцу — ещё публичная. Отключено в «Настройках» или писать нечего — письма
    нет. Владельцу — на MAIL_TO, как остальные письма; участнику — на адрес его Google-аккаунта, иначе Apple ID
    (адреса — только из базы). Шлёт только ночная проверка, под своим замком, — письмо одно; не ушло — повторим
    следующей ночью. → сколько писем отправлено."""
    now = datetime.fromisoformat(env.now())
    due = (now - timedelta(days=DIGEST_DAYS)).date().isoformat()
    sent = 0
    c.execute("DELETE FROM state WHERE (key LIKE ? OR key LIKE ?) AND CAST(substr(key, instr(key, ':') + 1) "
              "AS INTEGER) NOT IN (SELECT id FROM users)", (notify.DIGEST_OFF + "%", notify.DIGEST_SENT + "%"))
    for user in people.list_users(c):
        key = notify.DIGEST_SENT + str(user.id)
        last = store.get_state(c, key)
        if not notify.digest_enabled(c, user.id) or last[:10] > due:
            continue
        since = max(last, (now - timedelta(days=DIGEST_BACK_DAYS if last else DIGEST_DAYS)).isoformat(timespec="seconds"))
        shelves = [(f"Apple ID {a.email}", APPLE_NEW, a.id) for a in people.accounts_of(c, user.id)]
        mine = android_db.shelf_for_user(c, user.id)
        if mine is not None:
            shelves.append(("Android", ANDROID_NEW, mine.id))
        public = android_db.public_shelf(c) if user.role == people.OWNER else None
        if public is not None:
            shelves.append(("Публичная Android-полка", ANDROID_NEW, public.id))
        parts = []
        for title, query, shelf_id in shelves:
            rows = sorted(c.execute(query, (shelf_id, since)).fetchall(), key=lambda r: r["name"].casefold())
            if rows:
                parts.append(title + ":\n" + "".join(f"  {r['name']} {r['version']}\n" for r in rows))
        to = env.cfg.mail_to if user.role == people.OWNER and env.cfg.mail_to else _address(c, user.id)
        if not parts or not to:
            continue
        body = (f"Новые версии на ваших полках с {since[:10]}:\n\n" + "\n".join(parts) +
                f"\nОткрыть полки: {env.cfg.public_base}/\nОтключить эти письма: {env.cfg.public_base}/settings\n")
        try:
            env.send(DIGEST_SUBJECT, body, to)
        except Exception:  # сбой почты не останавливает остальные письма
            traceback.print_exc()
            continue
        store.set_state(c, key, now.isoformat(timespec="seconds"))
        sent += 1
    return sent


def _address(c, uid: int) -> str:
    mail = [g.email for g in people.google_of(c, uid)] + [a.email for a in people.accounts_of(c, uid)]
    return next((m for m in mail if "@" in m), "")


def _integrity(env: Env, c) -> str:
    """Сводка appshelf check (без --deep) в итоге ночи — только владельцу: участникам result_for её не показывает.
    Ничего не удаляет. Под download_lock: скачивание посреди проверки дало бы ложные .partial и сироты. Проблемы —
    письмо владельцу, одно на эпизод; чистая проверка эпизод закрывает."""
    try:
        with hold(env.cfg.download_lock):
            rep = integrity.check(env.cfg, c)
        if rep.ok:
            notify.reset(c, INTEGRITY_KEY)
            return f"{INTEGRITY}проблем нет"
        notify.once(c, INTEGRITY_KEY, INTEGRITY_SUBJECT, f"{rep.text()}\n\nПодробно — appshelf check на сервере; "
                    "ночная проверка ничего не удаляет.\n", env.send, env.cfg.mail_to, env.now())
        counts = Counter(p.kind for p in rep.problems)
        return f"{INTEGRITY}проблем {len(rep.problems)}: " + \
            ", ".join(f"{label} — {counts[kind]}" for kind, label in integrity.KINDS.items() if counts[kind])
    except Exception as e:  # сводка не должна оставить проверку без отметки для Zabbix
        return f"{INTEGRITY}не проверена — {describe(e)}"


def _finish(env: Env, c, result: str) -> str:
    now = env.now()
    store.set_state(c, "nightly_last", now)
    store.set_state(c, "nightly_result", result)
    env.cfg.status_dir.mkdir(parents=True, exist_ok=True)
    (env.cfg.status_dir / STAMP).write_text(now + "\n", encoding="utf-8")
    return result
