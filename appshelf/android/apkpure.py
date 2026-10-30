"""APKPure (spec 2026-10-28 §3, §6.5): API клиента APKPure без входа — app_version отдаёт protobuf со списком версий
пакета, первая — новейшая под ABI из заголовка x-abis. Только одиночный APK: XAPK — набор файлов. APKPure
распространял подменённые APK (2021, 2025-10), поэтому подпись сверяется с RuStore, а без неё нужна галочка
(android/fetch.py). Номера полей и живая проверка 2026-10-29 — tests/data/apkpure/README.md."""
from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import pb
from .net import FetchError, Http, SourceError

API = "https://api.pureapk.com/m/v3/cms/app_version"
HEADERS = {"x-cv": "3172501", "x-sv": "29", "x-abis": "arm64-v8a,armeabi-v7a,armeabi", "x-gp": "1"}
KINDS = {b"APK": "apk", b"XAPK": "xapk"}
UNKNOWN = "APKPure ответил в неизвестном формате"


@dataclass(frozen=True)
class PureApp:
    package: str
    name: str
    version_name: str
    version_code: int
    kind: str             # "apk" | "xapk"
    url: str
    sha1: str
    size: int
    icon_url: str
    description: str = ""  # ссылки автора для поиска отдельного APK, если магазин отдаёт XAPK


def info_url(package: str) -> str:
    return f"{API}?{urllib.parse.urlencode({'hl': 'en-US', 'package_name': package})}"


def info(http: Http, package: str) -> PureApp:
    """Новейшая версия пакета: первая запись списка с этим пакетом; нет записей — «нет в APKPure»."""
    status, _, body = http.request("GET", info_url(package), headers=HEADERS)
    if status != 200:
        raise FetchError(f"api.pureapk.com ответил {status}")
    try:
        for entry in pb.path(body, 1, 7, 2):
            app = pb.decode(pb.path(entry, 3, 2)[0])
            if _text(app, 4) != package:
                continue
            asset = pb.decode(app[24][0])
            if asset[8][0] not in KINDS:
                raise SourceError(UNKNOWN)
            icon = pb.path(app[27][0], 1, 1) if 27 in app else []
            return PureApp(package=package, name=_text(app, 1), version_name=_text(app, 6),
                           version_code=int(_text(app, 5)), kind=KINDS[asset[8][0]], url=_text(asset, 9),
                           sha1=_text(asset, 3).lower(), size=int(asset.get(4, [0])[0]),
                           icon_url=icon[0].decode() if icon else "", description=_text(app, 10))
    except (pb.PbError, IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise SourceError(UNKNOWN) from None
    raise SourceError("нет в APKPure")


def _text(msg: dict, num: int) -> str:
    vals = msg.get(num) or [b""]
    return vals[0].decode() if isinstance(vals[0], bytes) else str(vals[0])


def download(http: Http, app: PureApp, dest: Path) -> None:
    """APK в dest; размер или SHA-1 не совпали с ответом — файл удалён, SourceError."""
    http.download(app.url, dest)
    if app.size and dest.stat().st_size != app.size or app.sha1 and _sha1(dest) != app.sha1:
        dest.unlink(missing_ok=True)
        raise SourceError("файл из APKPure не совпал с контрольной суммой — скачается заново")


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
