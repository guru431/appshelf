"""Справочник удалённых из App Store приложений (data/removed_apps.json).

Apple не отдаёт их в истории покупок DAAP, хотя по id они скачиваются, а банки (Сбер, ВТБ, Т-Банк,
Альфа…) выпускали десятки клонов под чужими названиями. Проверка по каждому Apple ID: list-versions
отвечает только на свои лицензии, остальные — license_not_found. Свои добавляются в «Историю»
строкой без bundle id (метка «удалено из App Store»); имя поправит первое скачивание (store.fill_names)."""
from __future__ import annotations

import json
from importlib import resources

from . import store
from .ipatool import IpatoolError, LicenseNotFound, SessionExpired


class Interrupted(Exception):
    """Проверка уступила очередь (stop() — в очереди публикация); проверенное запомнено, продолжит с места."""


def load() -> list[dict]:
    doc = json.loads(resources.files("appshelf").joinpath("data/removed_apps.json").read_text(encoding="utf-8"))
    return doc["apps"]


def aliases() -> dict[int, str]:
    """id → поисковые синонимы одной строкой («vk вк» для ВКонтакте, «max макс» для МАКС)."""
    return {a["id"]: " ".join(a.get("aliases", [])) for a in load() if a.get("aliases")}


def ids() -> set[int]:
    return {a["id"] for a in load()}


def check(env, c, acct, catalog: list[dict] | None = None, full: bool = False,
          stop=lambda: False) -> tuple[int, int, int]:
    """(добавлено своих, без лицензии, ошибок) для Apple ID acct. Уже известные истории id не проверяются;
    «нет лицензии» запоминается, и кнопка их пропускает (~480 проверок — около восьми минут), полную перепроверку
    делает ночь — раз в неделю на Apple ID. stop() перед каждым запросом к Apple: True — Interrupted."""
    tool = env.tools(acct)
    rows = {r["app_id"]: r for r in store.list_purchases(c, acct.id)}
    skip = set() if full else store.no_license_ids(c, acct.id)
    added = missing = errors = 0
    for app in load() if catalog is None else catalog:
        row = rows.get(app["id"])
        if row is not None:
            if full and row["bundle_id"] == "" and row["version"] == "":  # ночью: своё удалённое без версии
                _fill_version(env, c, acct, app["id"])
            continue
        if app["id"] in skip:
            continue
        if stop():
            raise Interrupted
        try:
            latest = tool.latest_version_id(app["id"])
        except SessionExpired:
            raise
        except LicenseNotFound:
            store.mark_no_license(c, acct.id, app["id"], env.now())
            missing += 1
            continue
        except IpatoolError:  # сбой одного id не останавливает проверку остальных
            errors += 1
            continue
        store.add_known(c, acct.id, app["id"], app["name"])
        store.clear_no_license(c, acct.id, app["id"])
        _fill_version(env, c, acct, app["id"], latest)
        added += 1
    return added, missing, errors


def _fill_version(env, c, acct, app_id: int, latest: str = "") -> None:
    """Версия до публикации: external id последней версии → displayVersion (get-version-metadata)."""
    tool = env.tools(acct)
    try:
        version = tool.display_version(app_id, latest or tool.latest_version_id(app_id))
    except SessionExpired:
        raise
    except IpatoolError:  # без версии строка всё равно полезна
        return
    if version:
        store.set_purchase_version(c, acct.id, app_id, version)
