"""F-Droid (spec 2026-10-28 §3, §6.5): официальный репозиторий f-droid.org — пакетный API (рекомендуемая версия и
список версий) и APK по HTTPS. Подпись индекса не проверяется (решение плана этапа 5: index-v2 — десятки МБ JSON);
подпись приложения задаёт первая версия на полке, как у ссылки. У одной версии бывает несколько сборок по ABI, и
рекомендуемая — не обязательно ARM (VLC 3.7.2 — x86_64): сборку выбирает Sources.fetch по ABI самого APK. Живая проверка
2026-10-29 — tests/data/fdroid/README.md."""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass

from .net import FetchError, Http, SourceError

API = "https://f-droid.org/api/v1/packages/"
REPO = "https://f-droid.org/repo/"
MAX_CANDIDATES = 4
UNKNOWN = "F-Droid ответил в неизвестном формате"


@dataclass(frozen=True)
class FdPackage:
    package: str
    suggested: int                              # рекомендуемый versionCode
    versions: tuple[tuple[str, int], ...]       # (versionName, versionCode), как в ответе — новые сверху


def info(http: Http, package: str) -> FdPackage:
    status, _, body = http.request("GET", API + urllib.parse.quote(package), headers={"Accept": "application/json"})
    if status == 404:
        raise SourceError("нет в F-Droid")
    if status != 200:
        raise FetchError(f"f-droid.org ответил {status}")
    try:
        d = json.loads(body)
        return FdPackage(package=package, suggested=int(d["suggestedVersionCode"]),
                         versions=tuple((str(v["versionName"]), int(v["versionCode"])) for v in d["packages"]))
    except (KeyError, TypeError, ValueError):
        raise SourceError(UNKNOWN) from None


def candidates(pkg: FdPackage) -> list[int]:
    """versionCode для скачивания по порядку: рекомендуемый, затем другие сборки той же версии (не больше 4)."""
    name = next((n for n, code in pkg.versions if code == pkg.suggested), None)
    same = [code for n, code in pkg.versions if n == name and code != pkg.suggested]
    return ([pkg.suggested] + same)[:MAX_CANDIDATES]


def apk_url(package: str, version_code: int) -> str:
    return f"{REPO}{urllib.parse.quote(package)}_{version_code}.apk"
