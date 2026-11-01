"""Личные сессии 4PDA и APK из вложений. Секреты — вне БД, URL dlsess не сохраняется.

Вложение закреплено: новая версия выбирается человеком в теме, не угадывается по
номеру версии в шапке (там бывают разные архитектуры, модификации и подписи).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .net import MAX_JSON, FetchError, Http, SourceError

BASE = "https://4pda.to"
FORUM = BASE + "/forum/index.php"
COOKIE_NAMES = frozenset(("member_id", "pass_hash", "cf_clearance", "session_id", "anonymous"))
MAX_SESSION = 32 * 1024
EXPIRED = "Вход в 4PDA истёк — подключите аккаунт заново"
BLOCKED = "4PDA требует проверку браузера — подключите аккаунт заново"


class AuthError(SourceError):
    pass


@dataclass(frozen=True)
class Session:
    name: str
    user_agent: str = field(repr=False)
    cookies: dict[str, str] = field(repr=False)

    @property
    def headers(self):
        return {"User-Agent": self.user_agent, "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}


@dataclass(frozen=True)
class Link:
    name: str
    url: str


class Document(HTMLParser):
    def __init__(self, body: bytes, content_type: str = ""):
        super().__init__(convert_charrefs=True)
        self.links, self.text, self.title = [], [], []
        self._link, self._parts, self._title, self._skip = "", [], False, 0
        charset = "utf-8" if "utf-8" in content_type.lower() or b'charset="utf-8"' in body[:2000].lower() else "cp1251"
        self.feed(body.decode(charset, errors="replace"))
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "title":
            self._title = True
        if tag == "a":
            self._link, self._parts = dict(attrs).get("href") or "", []

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)
            if self._link:
                self._parts.append(data)
            if self._title:
                self.title.append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag == "title":
            self._title = False
        if tag == "a":
            if self._link:
                self.links.append(Link(" ".join("".join(self._parts).split())[:240], urljoin(FORUM, self._link)))
            self._link, self._parts = "", []

    def identity(self) -> str:
        name = ""
        for link in self.links:
            if "showuser=" in link.url:
                name = link.name
            if "action=logout" in link.url:
                return name or "4PDA"
        raise AuthError(EXPIRED)


def topic_url(value: str) -> str:
    u = _url(value)
    query = parse_qs(u.query)
    topic = query.get("showtopic", [""])[0]
    if u.path != "/forum/index.php" or not topic.isdecimal() or len(topic) > 12:
        raise SourceError("Нужна ссылка на тему 4PDA")
    fields = {"showtopic": topic}
    post = query.get("p", [""])[0]
    if query.get("view") == ["findpost"] and post.isdecimal() and len(post) <= 12:
        fields.update(view="findpost", p=post)
    elif (offset := query.get("st", [""])[0]).isdecimal() and len(offset) <= 10:
        fields["st"] = offset
    return FORUM + "?" + urlencode(fields)


def file_url(value: str) -> str:
    u = _url(value)
    if not re.fullmatch(r"/forum/dl/post/[1-9][0-9]{0,11}/[^/]+\.apk", u.path, re.I) or u.query or u.fragment \
            or re.search(r"[/\\\x00-\x1f]", unquote(u.path.rsplit("/", 1)[-1])):
        raise SourceError("Выберите отдельный APK во вложениях темы 4PDA")
    return urlunsplit(("https", "4pda.to", quote(u.path, safe="/%:+-._~()"), "", ""))


def _url(value):
    try:
        u = urlsplit(value.strip())
        if u.scheme == "https" and u.netloc == "4pda.to" and len(value) <= 2000 and not re.search(r"[\x00-\x1f\\]", value):
            return u
    except ValueError:
        pass
    raise SourceError("Нужна ссылка https://4pda.to/…")


class Client:
    def __init__(self, http, session: Session):
        self.http, self.session = http, session

    def page(self, url: str, *, authenticated=True) -> Document:
        try:
            status, headers, body = self.http.request("GET", url, headers=self.session.headers)
        except FetchError:
            raise FetchError("4PDA не ответил — попробуйте позже") from None
        if status == 403 or b"Enable JavaScript and cookies" in body:
            raise AuthError(BLOCKED)
        if status in (401, 404):
            raise AuthError(EXPIRED) if status == 401 else SourceError("Тема или вложение 4PDA недоступны")
        if status != 200:
            raise FetchError(f"4PDA ответил {status}")
        doc = Document(body, headers.get("content-type", ""))
        if authenticated:
            doc.identity()
        return doc

    def who(self):
        return self.page(FORUM + "?act=idx").identity()

    def search(self, query: str) -> list[Link]:
        query = query.strip()[:200]
        if not query:
            return []
        if query.startswith("https://"):
            url = topic_url(query)
            doc = self.page(url)
            return [Link("".join(doc.title).removesuffix(" - 4PDA"), url)]
        # Форум принимает cp1251; source=top ищет в заголовках тем.
        url = FORUM + "?" + urlencode({"act": "search", "query": query, "source": "top",
                                       "forums": "all", "subforums": "1", "result": "topics", "noform": "1"},
                                      encoding="cp1251", errors="replace")
        doc, found = self.page(url), {}
        for link in doc.links:
            try:
                # Ссылки «1, 2, 3…» в выдаче ведут на ту же тему.
                canonical = topic_url(link.url).split("&", 1)[0]
            except SourceError:
                continue
            if link.name and canonical not in found:
                found[canonical] = Link(link.name, canonical)
        return list(found.values())[:30]

    def files(self, url: str) -> tuple[str, list[Link]]:
        doc = self.page(topic_url(url))
        found = {}
        for link in doc.links:
            try:
                canonical = file_url(link.url)
            except SourceError:
                continue
            found.setdefault(canonical, Link(link.name or canonical.rsplit("/", 1)[-1], canonical))
        return "".join(doc.title).removesuffix(" - 4PDA"), list(found.values())[:100]

    def download(self, ref: str, dest: Path):
        gateway = file_url(ref)
        valid = False
        try:
            # У вошедшего пользователя gateway может сразу перенаправить на APK.
            # Сначала пишем ответ потоком: файл не ограничен размером HTML/JSON.
            meta = self.http.download(gateway, dest, headers=self.session.headers)
            with dest.open("rb") as f:
                magic = f.read(4)
            if magic == b"PK\x03\x04":
                valid = True
                return meta
            if dest.stat().st_size > MAX_JSON:
                raise FetchError("Вместо APK получена слишком большая страница")
            doc = Document(dest.read_bytes(), meta.content_type)
            # dlsess одноразовый: получать заново, не класть его в карточку, задания и ошибки.
            attachment, target = urlsplit(gateway).path.split("/")[4], None
            for link in doc.links:
                try:
                    u = _url(link.url)
                except SourceError:
                    continue
                q = parse_qs(u.query)
                if u.path == "/forum/index.php" and q.get("act") == ["attach"] and q.get("dlsess") and q.get("id") == [attachment]:
                    target = link.url
                    break
            if target is None:
                raise AuthError(EXPIRED)
            # net.Http убирает Cookie при редиректе с 4pda.to на файловый CDN.
            meta = self.http.download(target, dest, headers={**self.session.headers, "Referer": gateway})
            with dest.open("rb") as f:
                magic = f.read(4)
            if magic != b"PK\x03\x04":
                raise AuthError(EXPIRED)
            valid = True
            return meta
        except FetchError:
            raise FetchError("4PDA не отдал APK — обновите вход и повторите") from None
        finally:
            if not valid:
                dest.unlink(missing_ok=True)


class Sessions:
    def __init__(self, cfg, http=None):
        self.root = cfg.ipatool_home / "fourpda"
        self._http, self.extra_ca = http, cfg.sources_ca

    @property
    def http(self):
        if self._http is None:
            self._http = Http(self.extra_ca, timeout=10)
        return self._http

    def path(self, uid):
        if not isinstance(uid, int) or uid <= 0:
            raise SourceError("4PDA доступен только на личной полке")
        return self.root / f"{uid}.json"

    def load(self, uid) -> Session | None:
        try:
            with self.path(uid).open("rb") as f:
                raw = f.read(MAX_SESSION + 1)
            return self.parse(raw)
        except FileNotFoundError:
            return None
        except OSError:
            raise SourceError("Не удалось прочитать подключение 4PDA") from None
        except (ValueError, TypeError, KeyError, SourceError):
            raise AuthError(EXPIRED) from None

    @staticmethod
    def parse(raw: bytes) -> Session:
        if len(raw) > MAX_SESSION:
            raise SourceError("Файл сессии 4PDA слишком большой")
        try:
            data = json.loads(raw)
            ua, cookies = data["user_agent"], data["cookies"]
            if not isinstance(ua, str) or not 1 <= len(ua) <= 512 or not ua.isascii() or any(ord(c) < 32 for c in ua):
                raise ValueError
            if not isinstance(cookies, dict):
                raise ValueError
            clean = {k: v for k, v in cookies.items() if k in COOKIE_NAMES}
            if not clean.get("member_id", "").isdecimal() or not clean.get("pass_hash"):
                raise ValueError
            if any(not isinstance(v, str) or not re.fullmatch(r"[\x21-\x7e]{1,8192}", v) or ";" in v for v in clean.values()):
                raise ValueError
            return Session(str(data.get("name", "4PDA"))[:100], ua, clean)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise SourceError("Не удалось прочитать сессию 4PDA — подключите аккаунт заново") from None

    def save(self, uid, session: Session):
        dest = self.path(uid)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        temp = self.root / ("." + secrets.token_hex(12))
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"name": session.name, "user_agent": session.user_agent, "cookies": session.cookies}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, dest)
            dest.chmod(0o600)
        finally:
            temp.unlink(missing_ok=True)

    def client(self, uid):
        session = self.load(uid)
        if session is None:
            raise AuthError("Подключите свой аккаунт 4PDA на странице «Аккаунты»")
        return Client(self.http, session)

    def forget(self, uid):
        self.path(uid).unlink(missing_ok=True)
