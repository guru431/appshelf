"""Справочник удалённых из App Store приложений (data/removed_apps.json).

Apple не отдаёт их в истории покупок DAAP, хотя по id они скачиваются, а банки (Сбер, ВТБ, Т-Банк,
Альфа…) выпускали десятки клонов под чужими названиями. Проверка по каждому Apple ID: list-versions
отвечает только на свои лицензии, остальные — license_not_found. Свои добавляются в «Покупки»
строкой без bundle id (метка «удалено из App Store»); имя поправит первое скачивание (store.fill_names)."""
from __future__ import annotations

import json
import sys
from functools import cache
from importlib import resources

from . import people, store
from .ipatool import IpatoolError, LicenseNotFound, SessionExpired


class Interrupted(Exception):
    """Проверка уступила очередь (stop() — в очереди публикация); проверенное запомнено, продолжит с места."""


@cache  # справочник — в пакете: до перезапуска службы не меняется, а поиск «Покупок» зовёт его на каждый запрос
def load() -> list[dict]:
    doc = json.loads(resources.files("appshelf").joinpath("data/removed_apps.json").read_text(encoding="utf-8"))
    return doc["apps"]


def search_terms() -> dict[int, str]:
    """id → поисковые слова одной строкой: название в справочнике и синонимы («vk вк» для ВКонтакте, «max макс» для
    МАКС). Название держит бренд клона («Банк Зенит (DriveFlow)»), а строка «Покупок» после скачивания — имя из IPA."""
    return {a["id"]: " ".join([a["name"].casefold(), *a.get("aliases", [])]) for a in load()}


def check(env, c, acct, catalog: list[dict] | None = None, full: bool = False,
          stop=lambda: False, progress=None, cursor: int = 0) -> tuple[int, int, int]:
    """(добавлено своих, без лицензии, ошибок) для Apple ID acct. Уже известные истории id не проверяются, кроме
    добавленных по ссылке и не подтверждённых (manual): найдена лицензия — подтверждаются, нет — «нет лицензии».
    «Нет лицензии» запоминается, и кнопка их пропускает (~480 проверок — около восьми минут), полную перепроверку
    делает ночь — раз в неделю на Apple ID. Она же обновляет версию (и version_at) известных строк не из DAAP:
    +2 запроса в неделю на каждое своё удалённое приложение. stop() перед каждым запросом к Apple: True —
    Interrupted. Apple ID удалили посреди обхода — запись итога бросает store.AccountGone, и обход прекращается.
    progress(ход) — в начале и после ответа на каждый id: done из total (сколько id этот проход спросит у Apple),
    added, missing, errors и failed — id с технической ошибкой. cursor сохраняет позицию при уступке очереди;
    ошибки до неё повторяются после оставшихся ID. Неопределённый отказ входа хранится отдельно от «нет лицензии»:
    одна попытка на новый вход, после двух отказов — недельный карантин только этого ID."""
    aid = acct.id
    acct = people.get_account(c, aid)
    if acct is None:
        raise store.AccountGone(aid)
    tool = env.tools(acct)
    rows = {r["app_id"]: r for r in store.list_purchases(c, acct.id)}
    skip = set() if full else store.no_license_ids(c, acct.id)
    apps = load() if catalog is None else catalog
    def eligible(a):
        return (a["id"] not in rows or rows[a["id"]]["source"] == "manual") and a["id"] not in skip and \
            store.removed_retry_allowed(c, acct.id, a["id"], acct.last_login_at, env.now())

    total = sum(1 for a in apps if eligible(a))
    added = missing = errors = done = 0
    failed = []

    position = min(max(cursor, 0), len(apps))

    def report(finished=False):
        if progress is not None:
            progress({"done": done, "total": total, "added": added, "missing": missing, "errors": errors,
                      "failed": failed, **({} if finished else {"cursor": position})})

    report()
    # Ошибки до курсора повторяются после оставшихся ID: длинный обход приоритет не теряет и не начинает сначала.
    order = list(range(position, len(apps))) + list(range(position))
    for index in order:
        app = apps[index]
        row = rows.get(app["id"])
        if row is not None and row["source"] != "manual":
            if full and row["source"] != "daap" and store.removed_retry_allowed(
                    c, acct.id, app["id"], acct.last_login_at, env.now()):
                _fill_version(env, c, acct, app["id"])
            continue
        if not eligible(app):
            continue
        if stop():
            position = index
            report()
            raise Interrupted
        done += 1
        position = index + 1
        try:
            latest = tool.latest_version_id(app["id"])
        except SessionExpired as e:
            # list-versions считает «вход истёк» и 2042 AuthTokenResumeFreeBuy на одно приложение — вход проверяем
            # списком покупок. Такой ответ никогда не подтверждает отсутствие лицензии.
            alive = _session_alive(tool)
            state = "отдаётся" if alive else "тоже нет" if alive is False else "не удалось проверить"
            print(f"appshelf: Apple ID #{acct.id}: list-versions {app['id']} — {e.message}; "
                  f"список покупок {state}", file=sys.stderr)
            store.mark_removed_retry(c, acct.id, app["id"], acct.last_login_at, env.now())
            errors += 1
            failed.append(app["id"])
            report()
            if alive is False:
                raise
            continue
        except LicenseNotFound:
            store.mark_no_license(c, acct.id, app["id"], env.now())
            missing += 1
            report()
            continue
        except IpatoolError:  # сбой одного id не останавливает проверку остальных
            errors += 1
            failed.append(app["id"])
            report()
            continue
        if row is None:
            store.add_known(c, acct.id, app["id"], app["name"], "license")
            added += 1
        else:
            store.confirm_license(c, acct.id, app["id"], env.now())
        store.clear_no_license(c, acct.id, app["id"])
        report()  # до версии: лицензия уже записана, а за версией может истечь вход
        _fill_version(env, c, acct, app["id"], latest)
    report(finished=True)
    return added, missing, errors


def _session_alive(tool) -> bool | None:
    try:
        tool.list_purchases()
    except SessionExpired:
        return False
    except IpatoolError:  # сбой связи не доказывает истечение входа
        return None
    return True


def _fill_version(env, c, acct, app_id: int, latest: str = "") -> None:
    """Последняя версия: external id последней версии → displayVersion (get-version-metadata). «Вход истёк» на одном
    приложении (2042) — вход проверяем списком покупок, как в check: жив — версию пропускаем."""
    tool = env.tools(acct)
    try:
        version = tool.display_version(app_id, latest or tool.latest_version_id(app_id))
    except SessionExpired:
        store.mark_removed_retry(c, acct.id, app_id, acct.last_login_at, env.now())
        if _session_alive(tool) is False:
            raise
        return
    except IpatoolError:  # без версии строка всё равно полезна
        return
    if version:
        store.set_purchase_version(c, acct.id, app_id, version, env.now())
    store.clear_removed_retry(c, acct.id, app_id)
