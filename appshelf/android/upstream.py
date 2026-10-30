"""GitHub автора приложения — только по его описанию/сайту, без поиска репозиториев по названию.

Публичная страница Google Play даёт ссылку автора и описание конкретного пакета. Разрешён один переход на сайт
автора: оттуда берутся ссылки на APK или исходный код. Ссылки других приложений и отзывы магазина не читаются.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from html.parser import HTMLParser

from ..apk import PACKAGE_RE
from . import github
from .net import FetchError, Http, SourceError, check_url

MAX_REPOS = 3
MAX_PAGE = 2 << 20
URL_RE = re.compile(r"https://[^\s<>\"']+", re.I)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
CREDITS = re.compile(r"libraries|library|credits|third.party|powered by|thanks|библиотек|благодар", re.I)
SOURCE_LINK = re.compile(r"source|github|code|android|download|исход|скачать", re.I)


def play_url(package: str) -> str:
    return "https://play.google.com/store/apps/details?" + urllib.parse.urlencode({"id": package, "hl": "en", "gl": "US"})


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.package, self.website, self.description = "", "", []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            if a.get("name") == "appstore:bundle_id":
                self.package = a.get("content", "")
            if a.get("name") == "appstore:developer_url":
                self.website = a.get("content", "")
        if self.depth:
            if tag not in VOID:
                self.depth += 1
            if tag == "a" and a.get("href"):
                self.description.append(" " + a["href"] + " ")
            if tag == "br":
                self.description.append("\n")
        elif a.get("data-g-id") == "description":
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID:
            self.depth -= 1

    def handle_data(self, text):
        if self.depth:
            self.description.append(text)


class _AuthorLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.href, self.text = [], "", []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self.href, self.text = a.get("href", ""), [a.get("title", ""), a.get("aria-label", "")]
        elif self.href and tag == "img":
            self.text.append(a.get("alt", ""))

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, " ".join(self.text)))
            self.href, self.text = "", []

    def handle_data(self, text):
        if self.href:
            self.text.append(text)


def _page(http: Http, url: str, wait) -> str | None:
    wait()
    status, _, body = http.request("GET", url, headers={"Accept": "text/html"}, max_bytes=MAX_PAGE)
    if status == 404:
        return None
    if status != 200:
        raise FetchError(f"{urllib.parse.urlsplit(url).hostname} ответил {status}")
    return body.decode("utf-8", errors="replace")


def _repo(url: str) -> str:
    # Только полная ссылка: произвольное owner/repo из текста не считается указанием автора.
    return github.parse_ref(url) if url.startswith("https://github.com/") else ""


def _urls(text: str) -> tuple[str, ...]:
    return tuple(m.rstrip(".,;:!?)\u3002") for m in URL_RE.findall(html.unescape(text)))


def repositories(http: Http, package: str, *, metadata_links=(), description: str = "", wait=lambda: None) -> tuple[str, ...]:
    """До трёх репозиториев, на которые указывает автор. metadata_links — поля sourceCode/website источника.

    Без прямого sourceCode читается описание именно package в Play, затем не более одной страницы сайта автора.
    Сетевой сбой сохраняется как ошибка, отсутствие страницы или авторских ссылок — пустой результат.
    """
    if not PACKAGE_RE.match(package):
        raise SourceError("не похоже на имя пакета")
    found, websites = [], []

    def add(ref):
        if ref and ref.lower() not in {r.lower() for r in found} and len(found) < MAX_REPOS:
            found.append(ref)

    for link in metadata_links:
        if not isinstance(link, str):
            continue
        ref = _repo(link)
        if ref:
            add(ref)
        else:
            try:
                u = check_url(link)
            except FetchError:
                continue
            if u.hostname != "github.com":
                websites.append(link)
    for line in description.splitlines():
        if not CREDITS.search(line):
            for url in _urls(line):
                add(_repo(url))
    if found:
        return tuple(found)

    if not websites:
        page = _page(http, play_url(package), wait)
        if page is None:
            return ()
        meta = _Metadata()
        meta.feed(page)
        if meta.package != package:
            raise SourceError("не удалось прочитать описание приложения в Google Play")
        add(_repo(meta.website))
        for line in "".join(meta.description).splitlines():
            if not CREDITS.search(line):
                for url in _urls(line):
                    add(_repo(url))
        if meta.website:
            websites.append(meta.website)
    if found:
        return tuple(found)

    if websites:
        website = websites[0]
        # Http проверяет адрес перед запросом и после каждого редиректа; никакого обхода сайтов второго уровня.
        page = _page(http, website, wait)
        if page is not None:
            links = _AuthorLinks()
            links.feed(page)
            ranked = []
            # Страницы автора нередко создают кнопки скачивания из JSON в inline script (например Happ).
            # Только прямые APK: ссылки библиотек из остальных скриптов не становятся репозиториями приложения.
            for url in _urls(page):
                ref = _repo(url)
                if ref and urllib.parse.urlsplit(url).path.lower().endswith(".apk"):
                    ranked.append((0, ref))
            for url, label in links.links:
                url = urllib.parse.urljoin(website, url)
                ref = _repo(url)
                if not ref or CREDITS.search(label):
                    continue
                path = urllib.parse.urlsplit(url).path.lower()
                if path.endswith(".apk"):
                    ranked.append((0, ref))
                elif SOURCE_LINK.search(label):
                    ranked.append((1, ref))
            for _, ref in sorted(ranked, key=lambda r: r[0]):
                add(ref)
    return tuple(found)
