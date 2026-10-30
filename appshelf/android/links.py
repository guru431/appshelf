"""Поле «Ссылка на приложение или APK» (план этапа 5, Task 1): страница приложения в магазине → (источник, ref),
иначе — прямая ссылка на APK. Страницы не скачиваются — пакет или ID берётся из самого адреса. ref: пакет (play,
fdroid, apkpure, rustore), ID AppGallery (C…: пакет по нему узнаёт android/appgallery.py), адрес (link)."""
from __future__ import annotations

import re
import urllib.parse

from ..apk import PACKAGE_RE
from .net import FetchError, check_url
from . import github

APPGALLERY_ID_RE = re.compile(r"^C\d{6,12}$")
PLAY_HOSTS = ("play.google.com",)
APPGALLERY_HOSTS = ("appgallery.huawei.com", "appgallery.cloud.huawei.com")
FDROID_HOSTS = ("f-droid.org", "www.f-droid.org")
APKPURE_HOSTS = ("apkpure.com", "www.apkpure.com", "m.apkpure.com", "apkpure.net", "m.apkpure.net")
RUSTORE_HOSTS = ("rustore.ru", "www.rustore.ru", "apps.rustore.ru")
APPGALLERY_MARKS = ("app", "appdl", "outGoingApp")   # …/app/C…, /appdl/C…, /outGoingApp/C…
APKPURE_TAILS = ("download", "versions")
NAMES = {"play": "Google Play", "appgallery": "AppGallery", "fdroid": "F-Droid", "apkpure": "APKPure",
         "rustore": "RuStore", "fdroid_archive": "архива F-Droid", "github": "GitHub"}


def parse(url: str) -> tuple[str, str]:
    """(источник, ref); страница магазина с негодным пакетом или ID — FetchError, чужой адрес — net.check_url."""
    text = url.strip()
    if APPGALLERY_ID_RE.match(text):
        return "appgallery", text
    try:
        u = urllib.parse.urlsplit(text)
    except ValueError:
        u = None
    page = _page(u) if u is not None and u.scheme in ("http", "https") else None
    if page is None:
        check_url(text)
        return "link", text
    source, ref = page
    if source == "github":
        if not github.parse_ref(ref):
            raise FetchError("не похоже на репозиторий GitHub")
        return source, ref
    if source in ("fdroid", "fdroid_archive") and "@" in ref:
        from .fdroid_index import split_ref
        from .net import SourceError
        try:
            split_ref(ref)
        except SourceError:
            raise FetchError(f"не похоже на страницу приложения {NAMES[source]}") from None
        return source, ref
    if not (APPGALLERY_ID_RE if source == "appgallery" else PACKAGE_RE).match(ref):
        raise FetchError(f"не похоже на страницу приложения {NAMES[source]}: «{ref[:100]}» — не "
                         + ("ID приложения (C и цифры)" if source == "appgallery" else "имя пакета"))
    return source, ref


def _page(u: urllib.parse.SplitResult) -> tuple[str, str] | None:
    """Адрес — страница приложения в магазине: (источник, то, что стоит на месте пакета или ID); нет — None."""
    host = (u.hostname or "").lower()
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    if host == "github.com" and (len(parts) == 2 or len(parts) == 3 and parts[2] == "releases"
                                 or len(parts) == 4 and parts[2:] == ["releases", "latest"]):
        return "github", "/".join(parts[:2])
    if host in FDROID_HOSTS and len(parts) == 2 and parts[0] == "archive":
        match = re.fullmatch(r"(.+)_(\d+)\.apk", parts[1])
        if match:
            return "fdroid_archive", f"{match[1]}@{match[2]}"
    if host in PLAY_HOSTS and parts == ["store", "apps", "details"]:
        return "play", urllib.parse.parse_qs(u.query).get("id", [""])[0]
    if host in APPGALLERY_HOSTS:
        parts += [p for p in u.fragment.split("/") if p]  # прежний вид: /#/app/C…
        for mark, value in zip(parts, parts[1:]):
            if mark in APPGALLERY_MARKS:
                return "appgallery", value
        return None
    if host in FDROID_HOSTS and len(parts) in (2, 3) and parts[-2] in ("packages", "app"):  # /app/ — «Поделиться»
        return "fdroid", parts[-1]
    if host in APKPURE_HOSTS:
        if parts and parts[-1] in APKPURE_TAILS:
            parts = parts[:-1]
        return ("apkpure", parts[-1]) if len(parts) >= 2 and "." in parts[-1] else None
    if host in RUSTORE_HOSTS and (parts[:2] == ["catalog", "app"] and len(parts) == 3
                                  or host == "apps.rustore.ru" and parts[:1] == ["app"] and len(parts) == 2):
        return "rustore", parts[-1]
    return None
