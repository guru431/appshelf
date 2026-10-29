"""AppGallery (spec 2026-10-28 §3, §6.5): клиентский API Huawei без входа — client.updateCheck клиента UpdateSDK по
имени пакета: версия, размер и SHA-256 файла, прямая ссылка на APK. ID со страницы приложения (C…) → пакет:
appdl/<ID> переадресует на файл <пакет>.<число>.apk. Признака платного в ответе нет — платные не находятся.
Живая проверка 2026-10-29 — tests/data/appgallery/README.md; когда Huawei закроет updateCheck, источник ломается до
переделки, приложения на полке остаются."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .. import ipa
from .net import Http, SourceError

HOST = "store-drru.hispace.dbankcloud.ru"   # APPSHELF_APPGALLERY_HOST — другой хост того же API
APPDL = "https://appgallery.cloud.huawei.com/appdl/"
COUNTRY = "RU"
HEADERS = {"User-Agent": "UpdateSDK##4.0.1.300##Android##Pixel 2##com.huawei.appmarket##12.0.1.301"}
DEVICE = json.dumps({"abis": "arm64-v8a,armeabi-v7a", "dpi": 420, "preferLan": "ru"})
FILE_RE = re.compile(r"/([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\.\d+\.apk$")
UNKNOWN = "AppGallery ответил в неизвестном формате"


@dataclass(frozen=True)
class AgApp:
    app_id: str
    package: str
    name: str
    version_name: str
    version_code: int
    apk_url: str
    sha256: str           # файла APK, 64 hex нижним регистром
    icon_url: str
    size: int


def info(http: Http, host: str, package: str) -> AgApp:
    """Сведения о новейшей версии: updateCheck «обновить с versionCode 1»."""
    pkg_info = json.dumps({"params": [{"package": package, "versionCode": 1, "oldVersion": "1.0",
                                       "targetSdkVersion": 19, "isPre": 0, "maple": 0, "pkgMode": 0,
                                       "shellApkVer": 0}]})
    resp = http.form(f"https://{host}/hwmarket/api/clientApi", {
        "method": "client.updateCheck", "pkgInfo": pkg_info, "deviceSpecParams": DEVICE, "serviceCountry": COUNTRY},
        headers=HEADERS)
    if not isinstance(resp, dict) or resp.get("rtnCode") != 0 or not isinstance(resp.get("list"), list):
        raise SourceError(UNKNOWN)
    rec = next((r for r in resp["list"] if isinstance(r, dict) and r.get("package") == package), None)
    if rec is None:
        raise SourceError(f"AppGallery не нашёл приложение {package} (зона {COUNTRY})")
    try:
        sha = str(rec.get("sha256") or "").lower()
        return AgApp(app_id=str(rec["id"]), package=package, name=str(rec.get("name") or ""),
                     version_name=str(rec.get("version") or ""), version_code=int(rec["versionCode"]),
                     apk_url=str(rec.get("fullDownUrl") or rec["downurl"]), sha256=sha if len(sha) == 64 else "",
                     icon_url=str(rec.get("icon") or ""), size=int(rec.get("size") or 0))
    except (KeyError, TypeError, ValueError):
        raise SourceError(UNKNOWN) from None


def package_of(http: Http, app_id: str) -> str:
    """ID со страницы → пакет: appdl/<ID> ведёт на …/<пакет>.<число>.apk, неизвестный ID — на главную."""
    m = FILE_RE.search(http.head(APPDL + app_id).url.split("?")[0])
    if m is None:
        raise SourceError(f"AppGallery не нашёл приложение {app_id}")
    return m.group(1)


def download(http: Http, app: AgApp, dest: Path) -> None:
    """APK в dest; размер или SHA-256 не совпали с ответом — файл удалён, SourceError."""
    http.download(app.apk_url, dest)
    if app.size and dest.stat().st_size != app.size or app.sha256 and ipa.sha256(dest) != app.sha256:
        dest.unlink(missing_ok=True)
        raise SourceError("файл из AppGallery не совпал с контрольной суммой — скачается заново")
