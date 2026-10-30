"""Поиск имён пакетов на публичной странице Google Play, без входа и токенов.

Это только подсказки для общего поиска: наличие и версию в каждом магазине Sources проверяет отдельно.
Страница отдаёт обычные ссылки /store/apps/details?id=…; сгенерированные имена CSS не используются.
"""
from __future__ import annotations

import urllib.parse
from html.parser import HTMLParser

from ..apk import PACKAGE_RE
from .net import FetchError, Http, SourceError

SEARCH = "https://play.google.com/store/search"
LIMIT = 5
UNKNOWN = "Google Play не отдал страницу поиска — результаты могут быть неполными"


def search_url(query: str) -> str:
    return SEARCH + "?" + urllib.parse.urlencode({"q": query, "c": "apps", "hl": "en"})


def _package(href: str) -> str:
    try:
        u = urllib.parse.urlsplit(href)
    except ValueError:
        return ""
    if u.scheme and u.scheme != "https" or u.netloc and u.netloc != "play.google.com":
        return ""
    if u.path != "/store/apps/details":
        return ""
    package = urllib.parse.parse_qs(u.query).get("id", [""])[0]
    return package if PACKAGE_RE.fullmatch(package) else ""


class _Results(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}
        self.package = ""
        self.name = ""
        self.title: list[str] = []
        self.in_title = False
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag in ("script", "style"):
            self.skip += 1
        elif tag == "a":
            self.package = _package(a.get("href") or "")
            self.name = (a.get("aria-label") or a.get("title") or "").strip()

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.package and not self.name and not self.skip and data.strip():
            self.name = data.strip()

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a":
            if self.package and self.package not in self.found and len(self.found) < LIMIT:
                self.found[self.package] = self.name[:200] or self.package
            self.package = self.name = ""


def discover(http: Http, query: str, *, wait=lambda: None) -> list[tuple[str, str]]:
    """До пяти (пакет, название), в порядке выдачи Play; сбой/страница согласия — явная ошибка."""
    query = query.strip()
    if not query:
        return []
    if len(query) > 200:
        raise SourceError("Поисковый запрос длиннее 200 символов")
    wait()
    status, _, body = http.request("GET", search_url(query), headers={"Accept": "text/html"})
    if status != 200:
        raise FetchError(f"поиск Google Play ответил {status}")
    try:
        parser = _Results()
        parser.feed(body.decode("utf-8"))
        parser.close()
    except (UnicodeDecodeError, ValueError):
        raise SourceError(UNKNOWN) from None
    if "Google Play" not in "".join(parser.title):
        raise SourceError(UNKNOWN)
    return list(parser.found.items())
