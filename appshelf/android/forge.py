"""Выпуски Codeberg (Forgejo) и GitLab.com: публичные репозитории с одним APK для ARM, без учётной записи и токена.

Как github.py: только последний стабильный выпуск; выбор файла по имени — подсказка, пакет, ABI и подпись проверяет
общий конвейер. Файлы — только с того же сервера и того же проекта, сеть — android/net.py. Самостоятельно размещённые
Forgejo и GitLab не поддерживаются: по адресу их не отличить от любого сайта. Ответы — tests/data/codeberg, gitlab.
"""
from __future__ import annotations

import json
import re
import urllib.parse

from .github import GithubApp, _rank
from .net import FetchError, Http, SourceError

SOURCES = ("codeberg", "gitlab")
HOSTS = {"codeberg": "codeberg.org", "gitlab": "gitlab.com"}
NAMES = {"codeberg": "Codeberg", "gitlab": "GitLab"}
API = {"codeberg": "https://codeberg.org/api/v1", "gitlab": "https://gitlab.com/api/v4"}
SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z")
MAX_DEPTH = 20   # GitLab: группа/подгруппы/проект
# Файл выпуска GitLab, загруженный в описание: [имя](/uploads/<32 hex>/<файл>)
UPLOAD_RE = re.compile(r"\]\((/uploads/[0-9a-f]{32}/[^)\s]+)\)")


def parse_ref(source: str, text: str) -> str:
    """owner/repo (GitLab — и с подгруппами) либо страница проекта на своём сервере → путь; иначе пустая строка."""
    text = text.strip()
    if "://" in text:
        try:
            u = urllib.parse.urlsplit(text)
            if (u.scheme != "https" or u.hostname != HOSTS[source] or u.username is not None
                    or u.password is not None or u.port not in (None, 443)):
                return ""
        except ValueError:
            return ""
        parts = u.path.strip("/").split("/")
        if source == "gitlab":
            parts = parts[:parts.index("-")] if "-" in parts else parts
        elif len(parts) > 2 and parts[2] not in ("releases", "src"):
            return ""
        else:
            parts = parts[:2]
        text = "/".join(parts)
    text = text.removesuffix(".git")
    parts = text.split("/")
    if not 2 <= len(parts) <= (MAX_DEPTH if source == "gitlab" else 2):
        return ""
    if any(not SEGMENT_RE.match(p) or p.endswith(".") for p in parts):
        return ""
    return text


def info(http: Http, source: str, ref: str, *, wait=lambda: None) -> GithubApp:
    """Последний опубликованный стабильный выпуск; текст выпуска — «Что нового» (как есть, только текст). Пауза
    перед первым запросом — у вызывающего, wait — перед следующими."""
    ref = parse_ref(source, ref)
    if not ref:
        raise SourceError(f"нужна ссылка на репозиторий {NAMES[source]}")
    try:
        app = _codeberg(http, ref) if source == "codeberg" else _gitlab(http, ref, wait)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SourceError(f"{NAMES[source]} ответил в неизвестном формате") from None
    if app is None:
        raise SourceError(f"в последнем выпуске {NAMES[source]} нет одного APK для ARM")
    return app


def _json(http: Http, source: str, url: str):
    status, _, body = http.request("GET", url, headers={"Accept": "application/json"})
    if status == 404:
        raise SourceError(f"в {NAMES[source]} нет публичного репозитория или опубликованного стабильного выпуска")
    if status in (403, 429):
        raise FetchError(f"{NAMES[source]} ограничил запросы — повторите позже")
    if status != 200:
        raise FetchError(f"{NAMES[source]} ответил {status}")
    return json.loads(body)


def _own(url, host: str, prefixes) -> bool:
    """Ссылка на файл того же проекта на том же сервере; prefixes — начала пути без учёта регистра."""
    if not isinstance(url, str):
        return False
    try:
        u = urllib.parse.urlsplit(url)
        path = u.path.lower()
        return (u.scheme == "https" and u.hostname == host and u.port in (None, 443) and u.username is None
                and u.password is None and any(path.startswith(p.lower()) and len(path) > len(p) for p in prefixes))
    except ValueError:
        return False


def _best(candidates) -> tuple[str, str] | None:
    """(имя, адрес) лучшего APK для ARM по имени файла; нет — None."""
    ranked = [(rank, name.lower(), name, url) for name, url in candidates
              if isinstance(name, str) and (rank := _rank(name)) is not None]
    return min(ranked)[2:] if ranked else None


def _codeberg(http: Http, ref: str) -> GithubApp | None:
    d = _json(http, "codeberg", f"{API['codeberg']}/repos/{ref}/releases/latest")
    if (not isinstance(d["tag_name"], str) or not d["tag_name"] or d.get("draft") or d.get("prerelease")
            or not isinstance(d["assets"], list)):
        raise ValueError
    assets = {a["browser_download_url"]: a for a in d["assets"] if isinstance(a, dict)
              and _own(a.get("browser_download_url"), HOSTS["codeberg"], (f"/{ref}/releases/download/",))}
    best = _best((a.get("name"), url) for url, a in assets.items())
    if best is None:
        return None
    name, url = best
    a = assets[url]
    return GithubApp(repository=ref, name=ref.rsplit("/", 1)[1], version_name=d["tag_name"], asset_url=url,
                     asset_name=name, mark=f"cb:{int(d['id'])}:{int(a['id'])}:{a.get('uuid') or ''}",
                     whats_new=d.get("body") if isinstance(d.get("body"), str) else "")


def _gitlab(http: Http, ref: str, wait) -> GithubApp | None:
    """Проект по пути (его номер нужен для адресов файлов), затем последний вышедший выпуск. Файлы — ссылки выпуска
    и загрузки, на которые ссылается его описание (так выкладывает большинство проектов)."""
    api = API["gitlab"]
    project = _json(http, "gitlab", f"{api}/projects/{urllib.parse.quote(ref, safe='')}")
    pid, path = int(project["id"]), str(project["path_with_namespace"])
    if parse_ref("gitlab", path) != path:
        raise ValueError
    wait()
    releases = _json(http, "gitlab", f"{api}/projects/{pid}/releases?per_page=5")
    d = next((r for r in releases if isinstance(r, dict) and not r.get("upcoming_release")), None)
    if d is None:
        raise SourceError("в GitLab нет опубликованного выпуска")
    if not isinstance(d["tag_name"], str) or not d["tag_name"]:
        raise ValueError
    text = d.get("description") if isinstance(d.get("description"), str) else ""
    uploads = f"/-/project/{pid}/uploads/"
    candidates = [(link.get("name"), link.get("url")) for link in d["assets"]["links"] if isinstance(link, dict)]
    for upload in UPLOAD_RE.findall(text):
        candidates.append((urllib.parse.unquote(upload.rsplit("/", 1)[1]), f"https://gitlab.com/-/project/{pid}{upload}"))
    best = _best((n, u) for n, u in candidates
                 if _own(u, HOSTS["gitlab"], (uploads, f"/{path}/-/package_files/")))
    if best is None:
        return None
    name, url = best
    # Ссылки на загруженные файлы и «\» в конце строки (перенос Markdown GitLab) в тексте «Что нового» — шум.
    notes = re.sub(r"\\$", "", re.sub(r"\[[^\]\n]*\]\(/uploads/[^)\s]*\)", "", text), flags=re.M)
    return GithubApp(repository=path, name=path.rsplit("/", 1)[1], version_name=d["tag_name"], asset_url=url,
                     asset_name=name, mark=f"gl:{pid}:{d['tag_name']}:{url}", whats_new=notes)
