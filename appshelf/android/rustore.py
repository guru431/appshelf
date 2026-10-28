"""RuStore (spec 2026-10-28 §3, §6.1): неофициальный API backapi.rustore.ru — поиск, сведения, ссылка на
универсальный APK. Без заголовка версии клиента — 400 (с июля 2026); когда VK закроет v1/v2, источник ломается до
переделки, приложения на полке остаются. Ответы — {"code": "OK", "body": …}; записанные — tests/data/rustore/."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .net import Http, SourceError

BASE = "https://backapi.rustore.ru"
VER_CODE = "1105002"
HEADERS = {"ruStoreVerCode": VER_CODE}
UNKNOWN = "RuStore ответил в неизвестном формате"


@dataclass(frozen=True)
class StoreApp:
    app_id: int
    package: str
    name: str
    version_name: str      # в поиске нет — ""
    version_code: int
    signature: str         # SHA-256 сертификата, 64 hex нижним регистром; в поиске нет — ""
    price: int             # > 0 — платное: без входа в RuStore не скачать
    icon_url: str
    size: int


def search(http: Http, query: str, limit: int = 20) -> list[StoreApp]:
    q = urllib.parse.urlencode({"query": query, "pageSize": limit, "pageNumber": 0})
    content = _body(http.json(f"{BASE}/applicationData/apps?{q}", headers=HEADERS)).get("content")
    if not isinstance(content, list):
        raise SourceError(UNKNOWN)
    return [_app(a) for a in content]


def info(http: Http, package: str) -> StoreApp:
    return _app(_body(http.json(f"{BASE}/applicationData/overallInfo/{urllib.parse.quote(package)}",
                                headers=HEADERS)))


def apk_url(http: Http, app_id: int) -> str:
    """v2/download-link с withoutSplits — один файл; по ссылке .zip-обёртка, тот же путь с .apk — сам APK
    (живая проверка 2026-10-28)."""
    body = _body(http.json(f"{BASE}/applicationData/v2/download-link", headers=HEADERS, body={
        "appId": app_id, "firstInstall": True, "supportedAbis": ["arm64-v8a", "armeabi-v7a"], "screenDensity": 420,
        "sdkVersion": 34, "withoutSplits": True}))
    urls = body.get("downloadUrls")
    if isinstance(urls, list) and len(urls) > 1:
        raise SourceError("RuStore отдаёт набор файлов, а не один APK")
    if not isinstance(urls, list) or not urls or not isinstance(urls[0], dict) or not isinstance(urls[0].get("url"), str):
        raise SourceError(UNKNOWN)
    url = urls[0]["url"]
    return url[:-len(".zip")] + ".apk" if url.endswith(".zip") else url


def _body(resp) -> dict:
    if not isinstance(resp, dict) or resp.get("code") != "OK" or not isinstance(resp.get("body"), dict):
        raise SourceError(UNKNOWN)
    return resp["body"]


def _app(a) -> StoreApp:
    try:
        sig = re.sub(r"[^0-9a-f]", "", str(a.get("signature") or "").lower())
        return StoreApp(app_id=int(a["appId"]), package=str(a["packageName"]), name=str(a.get("appName") or ""),
                        version_name=str(a.get("versionName") or ""), version_code=int(a.get("versionCode") or 0),
                        signature=sig if len(sig) == 64 else "", price=int(a.get("price") or 0),
                        icon_url=str(a.get("iconUrl") or ""), size=int(a.get("fileSize") or 0))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SourceError(UNKNOWN) from None
