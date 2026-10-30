"""GitHub Releases: публичные репозитории с одним APK для ARM, без учётной записи и токена.

Репозиторий задаёт участник или ссылка автора в описании приложения. Выбор по имени файла — подсказка: настоящий
пакет, ABI и подпись проверяет общий конвейер после скачивания. Наборы APK и сборки только для x86 не предлагаются.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser

from .net import FetchError, Http, SourceError

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
UNKNOWN = "GitHub ответил в неизвестном формате"
NO_APK = "в последнем выпуске GitHub нет одного APK для ARM"
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")


class _RateLimited(FetchError):
    pass


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
        raise _RateLimited("GitHub ограничил запросы — повторите позже")
    if status != 200:
        raise FetchError(f"GitHub ответил {status}")
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        raise SourceError(UNKNOWN) from None


def info(http: Http, ref: str, *, wait=lambda: None) -> GithubApp:
    """Последний опубликованный стабильный выпуск; release/latest не включает draft и prerelease."""
    ref = parse_ref(ref)
    if not ref:
        raise SourceError("нужна ссылка на репозиторий GitHub или owner/repo")
    try:
        d = _json(http, f"/repos/{ref}/releases/latest")
    except _RateLimited:
        return _html_info(http, ref, wait)
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


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fragments, self.links = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "include-fragment" and attrs.get("src"):
            self.fragments.append(attrs["src"])
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


def _page(http: Http, url: str) -> _Links:
    status, _, body = http.request("GET", url, headers={"Accept": "text/html"}, max_bytes=2 << 20)
    if status == 404:
        raise SourceError("в GitHub нет публичного репозитория или опубликованного стабильного выпуска")
    if status in (403, 429):
        raise FetchError("GitHub ограничил запросы — повторите позже")
    if status != 200:
        raise FetchError(f"GitHub ответил {status}")
    parser = _Links()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser


def _html_info(http: Http, ref: str, wait) -> GithubApp:
    """При исчерпанном лимите API — та же публичная страница последнего выпуска и только её список файлов."""
    page_url = f"https://github.com/{ref}/releases/latest"
    wait()
    page = _page(http, page_url)
    prefix = f"/{ref}/releases/expanded_assets/"
    fragment = next((urllib.parse.urljoin(page_url, u) for u in page.fragments
                     if _fragment_url(urllib.parse.urljoin(page_url, u), prefix)), "")
    if not fragment:
        raise SourceError("не удалось прочитать файлы последнего выпуска GitHub")
    raw_tag = urllib.parse.urlsplit(fragment).path[len(prefix):]
    wait()
    assets = _page(http, fragment)
    candidates = []
    for href in assets.links:
        url = urllib.parse.urljoin(fragment, href)
        if not _asset_url(url, ref):
            continue
        u = urllib.parse.urlsplit(url)
        # Фрагмент и файл относятся к одному выпуску; посторонние ссылки страницы не кандидаты.
        if not u.path.lower().startswith(f"/{ref}/releases/download/{raw_tag}/".lower()):
            continue
        name = urllib.parse.unquote(u.path.rsplit("/", 1)[-1])
        rank = _rank(name)
        if rank is not None:
            candidates.append((rank, name.lower(), name, url))
    if not candidates:
        raise SourceError(NO_APK)
    _, _, name, url = min(candidates)
    tag = urllib.parse.unquote(raw_tag)
    return GithubApp(repository=ref, name=ref.rsplit("/", 1)[1], version_name=tag,
                     asset_url=url, asset_name=name, mark=f"gh-web:{tag}:{url}")


def _fragment_url(url: str, prefix: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        return (u.scheme == "https" and u.hostname == "github.com" and u.port in (None, 443)
                and u.username is None and u.password is None and not u.query and not u.fragment
                and u.path.lower().startswith(prefix.lower()) and len(u.path) > len(prefix))
    except ValueError:
        return False
