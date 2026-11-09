"""Поле «Ссылка на приложение или APK» (план этапа 5, Task 1): страница приложения в магазине → (источник, ref),
иначе — прямая ссылка на APK. Страницы не скачиваются — пакет или ID берётся из самого адреса. ref: пакет (play,
fdroid, apkpure, rustore), ID AppGallery (C…: пакет по нему узнаёт android/appgallery.py), адрес (link, fourpda),
путь репозитория (github, codeberg, gitlab)."""
from __future__ import annotations

import re
import urllib.parse

from ..apk import PACKAGE_RE
from .net import FetchError, SourceError, check_url
from . import forge, fourpda, github

APPGALLERY_ID_RE = re.compile(r"^C\d{6,12}$")
PLAY_HOSTS = ("play.google.com",)
APPGALLERY_HOSTS = ("appgallery.huawei.com", "appgallery.cloud.huawei.com")
FDROID_HOSTS = ("f-droid.org", "www.f-droid.org")
APKPURE_HOSTS = ("apkpure.com", "www.apkpure.com", "m.apkpure.com", "apkpure.net", "m.apkpure.net")
RUSTORE_HOSTS = ("rustore.ru", "www.rustore.ru", "apps.rustore.ru")
APPGALLERY_MARKS = ("app", "appdl", "outGoingApp")   # …/app/C…, /appdl/C…, /outGoingApp/C…
APKPURE_TAILS = ("download", "versions")
NAMES = {"play": "Google Play", "appgallery": "AppGallery", "fdroid": "F-Droid", "apkpure": "APKPure",
         "rustore": "RuStore", "fdroid_archive": "архива F-Droid", "izzy": "IzzyOnDroid", "github": "GitHub",
         **forge.NAMES}


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
    if source == "fourpda":
        try:
            return source, fourpda.file_url(ref)
        except SourceError as e:
            raise FetchError(str(e)) from None
    if source == "github":
        if not github.parse_ref(ref):
            raise FetchError("не похоже на репозиторий GitHub")
        return source, ref
    if source in forge.SOURCES:
        if not forge.parse_ref(source, ref):
            raise FetchError(f"не похоже на репозиторий {NAMES[source]}")
        return source, forge.parse_ref(source, ref)
    if source in ("fdroid", "fdroid_archive", "izzy") and "@" in ref:
        from .fdroid_index import split_ref
        try:
            split_ref(ref)
        except SourceError:
            raise FetchError(f"не похоже на страницу приложения {NAMES[source]}") from None
        return source, ref
    if not (APPGALLERY_ID_RE if source == "appgallery" else PACKAGE_RE).match(ref):
        raise FetchError(f"не похоже на страницу приложения {NAMES[source]}: «{ref[:100]}» — не "
                         + ("ID приложения (C и цифры)" if source == "appgallery" else "имя пакета"))
    return source, ref


def page_url(source: str, ref: str, saved: str = "") -> str:
    """Публичная страница источника; только проверенные адреса, без сетевых запросов."""
    if source == "fourpda":
        if not saved:
            return ""
        try:
            return fourpda.topic_url(saved)
        except SourceError:
            return ""
    if source == "github":
        repo = github.parse_ref(ref)
        return f"https://github.com/{repo}" if repo else ""
    if source in forge.SOURCES:
        repo = forge.parse_ref(source, ref)
        return f"https://{forge.HOSTS[source]}/{repo}" if repo else ""
    if source == "appgallery":
        if APPGALLERY_ID_RE.fullmatch(ref):
            return f"https://appgallery.huawei.com/app/{ref}"
        if saved:
            try:
                parsed_source, app_id = parse(saved)
                if parsed_source == source and APPGALLERY_ID_RE.fullmatch(app_id):
                    return f"https://appgallery.huawei.com/app/{app_id}"
            except FetchError:
                pass
        return ""
    package = ref.split("@", 1)[0]
    if not PACKAGE_RE.fullmatch(package):
        return ""
    return {
        "rustore": f"https://www.rustore.ru/catalog/app/{package}",
        "play": f"https://play.google.com/store/apps/details?id={package}",
        "fdroid": f"https://f-droid.org/packages/{package}/",
        "fdroid_archive": f"https://f-droid.org/packages/{package}/",
        "izzy": f"https://apt.izzysoft.de/fdroid/index/apk/{package}",
        "apkpure": f"https://apkpure.com/app/{package}",
    }.get(source, "")


def _page(u: urllib.parse.SplitResult) -> tuple[str, str] | None:
    """Адрес — страница приложения в магазине: (источник, то, что стоит на месте пакета или ID); нет — None."""
    host = (u.hostname or "").lower()
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    if host == "4pda.to":
        return "fourpda", u.geturl()
    if host == "apt.izzysoft.de":
        if len(parts) == 4 and parts[:3] == ["fdroid", "index", "apk"]:
            return "izzy", parts[3]
        if len(parts) == 3 and parts[:2] == ["fdroid", "repo"]:
            match = re.fullmatch(r"(.+)_(\d+)\.apk", parts[2])
            if match:
                return "izzy", f"{match[1]}@{match[2]}"
    if host == "github.com" and (len(parts) == 2 or len(parts) == 3 and parts[2] == "releases"
                                 or len(parts) == 4 and parts[2:] == ["releases", "latest"]):
        return "github", "/".join(parts[:2])
    # Страница проекта или его выпусков; ссылка на сам файл .apk остаётся прямой ссылкой.
    if host == forge.HOSTS["codeberg"] and (len(parts) == 2 or len(parts) == 3 and parts[2] == "releases"
                                            or len(parts) == 4 and parts[2:] == ["releases", "latest"]):
        return "codeberg", "/".join(parts[:2])
    if host == forge.HOSTS["gitlab"] and len(parts) >= 2 and parts[0] != "-" and not parts[-1].endswith(".apk") and (
            "-" not in parts or parts[parts.index("-") + 1:] in (["releases"], ["releases", "permalink", "latest"])):
        return "gitlab", "/".join(parts[:parts.index("-")] if "-" in parts else parts)
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
