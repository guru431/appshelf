"""GitHub Releases: публичные репозитории с одним APK для ARM, без учётной записи и токена.

Поиск проверяет не больше пяти репозиториев. Выбор по имени файла — подсказка: настоящий пакет, ABI и подпись
проверяет общий конвейер после скачивания. Наборы APK и сборки только для x86 не предлагаются.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass

from .net import FetchError, Http, SourceError

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
LIMIT = 5
UNKNOWN = "GitHub ответил в неизвестном формате"
NO_APK = "в последнем выпуске GitHub нет одного APK для ARM"
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")


@dataclass(frozen=True)
class GithubApp:
    repository: str
    name: str
    version_name: str
    asset_url: str
    asset_name: str
    mark: str


def parse_ref(text: str) -> str:
    """owner/repo либо страница репозитория/выпуска → owner/repo; чужой адрес или неверное имя → пустая строка."""
    text = text.strip()
    if "://" in text:
        try:
            u = urllib.parse.urlsplit(text)
            if (u.scheme != "https" or u.hostname != "github.com" or u.username is not None
                    or u.password is not None or u.port not in (None, 443)):
                return ""
        except ValueError:
            return ""
        parts = u.path.strip("/").split("/")
        if len(parts) < 2 or len(parts) > 2 and parts[2] not in ("releases", "tree", "blob"):
            return ""
        text = "/".join(parts[:2])
    text = text.removesuffix(".git")
    if not REPO_RE.fullmatch(text) or text.split("/")[1] in (".", ".."):
        return ""
    return text


def _json(http: Http, path: str):
    status, _, body = http.request("GET", API + path, headers=HEADERS)
    if status == 404:
        raise SourceError("в GitHub нет публичного репозитория или опубликованного стабильного выпуска")
    if status in (403, 429):
        raise FetchError("GitHub ограничил запросы — повторите поиск позже")
    if status != 200:
        raise FetchError(f"GitHub ответил {status}")
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        raise SourceError(UNKNOWN) from None


def info(http: Http, ref: str) -> GithubApp:
    """Последний опубликованный стабильный выпуск; release/latest не включает draft и prerelease."""
    ref = parse_ref(ref)
    if not ref:
        raise SourceError("нужна ссылка на репозиторий GitHub или owner/repo")
    d = _json(http, f"/repos/{ref}/releases/latest")
    try:
        if not isinstance(d, dict) or not isinstance(d["assets"], list) or not isinstance(d["tag_name"], str):
            raise ValueError
        if d.get("draft") or d.get("prerelease") or not d["tag_name"]:
            raise ValueError
        release_id = int(d["id"])
        assets = []
        for a in d["assets"]:
            if not isinstance(a, dict) or not isinstance(a.get("name"), str):
                continue
            rank = _rank(a["name"])
            if rank is None or a.get("state", "uploaded") != "uploaded":
                continue
            url = a.get("browser_download_url")
            if not _asset_url(url, ref):
                continue
            assets.append((rank, a["name"].lower(), a))
        if not assets:
            raise SourceError(NO_APK)
        a = min(assets, key=lambda v: v[:2])[2]
        asset_id = int(a["id"])
        return GithubApp(repository=ref, name=ref.rsplit("/", 1)[1], version_name=d["tag_name"],
                         asset_url=a["browser_download_url"], asset_name=a["name"],
                         mark=f"gh:{release_id}:{asset_id}:{a.get('updated_at') or ''}")
    except (KeyError, TypeError, ValueError):
        raise SourceError(UNKNOWN) from None


def _asset_url(url, ref: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        u = urllib.parse.urlsplit(url)
        prefix = f"/{ref}/releases/download/"
        return (u.scheme == "https" and u.hostname == "github.com" and u.port in (None, 443)
                and u.username is None and u.password is None
                and u.path.lower().startswith(prefix.lower()) and len(u.path) > len(prefix))
    except ValueError:
        return False


def _rank(name: str) -> int | None:
    text = name.lower()
    if not text.endswith(".apk") or any(v in text for v in ("x86", "x64", "amd64", "split", "config.", "debug", "unsigned")):
        return None
    if "universal" in text or "all-arch" in text:
        return 0
    if any(v in text for v in ("arm64", "aarch64", "v8a")):
        return 1
    if any(v in text for v in ("armeabi", "armv7", "v7a")):
        return 3
    return 2


def search(http: Http, query: str, *, wait=lambda: None) -> list[GithubApp]:
    """До пяти репозиториев с APK; отдельный репозиторий без APK пропускается, сетевой сбой виден вызывающему."""
    query = query.strip()
    if not query:
        return []
    direct = parse_ref(query)
    if direct:
        wait()
        return [info(http, direct)]
    params = urllib.parse.urlencode({"q": query[:160] + " android in:name,description,readme", "per_page": LIMIT})
    wait()
    d = _json(http, "/search/repositories?" + params)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise SourceError(UNKNOWN)
    found, seen = [], set()
    for item in d["items"][:LIMIT]:
        if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
            continue
        ref = parse_ref(item["full_name"])
        if not ref or ref.lower() in seen:
            continue
        seen.add(ref.lower())
        try:
            wait()
            found.append(info(http, ref))
        except SourceError:
            pass
    return found
