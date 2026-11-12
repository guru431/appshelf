"""Общее для страниц appshelf (spec 2026-10-08 §4, §10): контекст приложения, шаблоны, имена cookie."""
from __future__ import annotations

import io
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit

import qrcode
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import google, jobs, people, store, webauth
from ..android import pin
from ..config import GB, Config
from .login import Limiter, LoginFlow

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["gb"] = lambda size: f"{size / GB:.1f}"  # размер полки в ГБ — как в подтверждении удаления
# размер IPA/APK в МБ на карточке: маленький APK — «< 1», а не «0»
TEMPLATES.env.filters["mb"] = lambda size: "< 1" if size < 1048576 else str(int(size / 1048576 + .5))
THEMES = ("light", "dark")
# браузеры iOS, из которых itms-services не запускает установку: Chrome, Firefox, Edge, Opera, Яндекс
IOS_OTHER_BROWSERS = ("CriOS", "FxiOS", "EdgiOS", "OPiOS", "YaBrowser")
ACCT_COOKIE = "appshelf_acct"      # активная полка: id Apple ID одного из вошедших
WHO_COOKIE = "appshelf_who"        # активный участник Android-раздела: id одного из вошедших
LOGIN_COOKIE = "appshelf_login"    # незавершённый вход (шаг кода 2FA): id в памяти LoginFlow
INVITE_COOKIE = "appshelf_invite"  # принятое приглашение — до первого входа нового Apple ID
CREATED_COOKIE = "appshelf_created"  # новая ссылка до показа на /admin (web/admin.py)
# Свои cookie с состоянием (вход и его поток, выбор полки, поток Google, приглашение, новая ссылка, гость
# публичной полки) на https — с префиксом __Host- (браузер требует Secure, Path=/ и не
# принимает Domain): соседний поддомен не подложит их на родительский домен. Имена в коде — без префикса, его ставит
# WebCtx. Старые имена (без префикса) WebCtx.cookie читает только у долгоживущих (LEGACY_COOKIES) — год после
# выкладки префикса, столько живёт cookie входа, а новых со старым именем уже не ставят: до LEGACY_UNTIL (выкладка —
# до 2027-01-01; позже — сдвинуть), после него не читает, а код чтения можно убрать. Потоки входа и приглашения живут
# минуты и сутки — их старые имена не читаются: иначе подложенное с соседнего поддомена значение сработало бы до 2028 года.
HOST_PREFIX = "__Host-"
HOST_COOKIES = frozenset((webauth.COOKIE, ACCT_COOKIE, WHO_COOKIE, google.COOKIE, LOGIN_COOKIE, INVITE_COOKIE,
                          CREATED_COOKIE, pin.COOKIE))
LEGACY_COOKIES = frozenset((webauth.COOKIE, ACCT_COOKIE, WHO_COOKIE, pin.COOKIE))
LEGACY_UNTIL = datetime(2028, 1, 1, tzinfo=timezone.utc)


@lru_cache(maxsize=128)
def qr_png(url: str) -> bytes:
    """QR ссылки — PNG: установка с компьютера, ссылки APK, приглашения в админке."""
    data = io.BytesIO()
    qrcode.make(url, box_size=4, border=4).save(data, format="PNG")
    return data.getvalue()


@dataclass
class WebCtx:
    cfg: Config
    env: jobs.Env
    new_tool: Callable            # (home, device_mac) → ipatool первого входа нового Apple ID
    flow: LoginFlow
    limiter: Limiter
    now: Callable[[], str]
    key: bytes                    # ключ подписи cookie — читается один раз, при создании приложения

    def conn(self):
        return closing(store.connect(self.cfg.db, self.cfg.owner_email))

    def epoch(self) -> int:
        return int(datetime.fromisoformat(self.now()).timestamp())

    def signed(self, c, request: Request) -> list[tuple]:
        """Вошедшие в этом браузере люди и их записи cookie — по порядку входа (webauth.signed_users)."""
        return webauth.signed_users(self.key, self.cookie(request, webauth.COOKIE), self.epoch(),
                                    lambda uid: people.get_user(c, uid))

    def person(self, request: Request):
        """(вошедшие люди, человек активной полки, полки браузера, активная полка, участник Android-раздела) по
        cookie или None — никто не вошёл (spec 2026-10-12 §3, 2026-10-28 §4). Полки браузера — Apple ID всех
        вошедших: по порядку входа, у человека — по id. Активная — из cookie выбора полки, если она среди них, иначе
        первая; Apple ID нет ни у кого — None, и человек активной полки — участник Android-раздела. Тот — из своей
        cookie, если он среди вошедших, иначе человек активной полки, иначе первый вошедший."""
        with self.conn() as c:
            users = [u for u, _ in self.signed(c, request)]
            accounts = [a for u in users for a in people.accounts_of(c, u.id)]
        if not users:
            return None
        want = self.cookie(request, ACCT_COOKIE)
        acct = next((a for a in accounts if str(a.id) == want), accounts[0] if accounts else None)
        holder = next(u for u in users if u.id == acct.user_id) if acct is not None else None
        whom = self.cookie(request, WHO_COOKIE)
        who = next((u for u in users if str(u.id) == whom), holder or users[0])
        return users, holder or who, accounts, acct, who

    @property
    def secure(self) -> bool:
        return self.cfg.public_base.startswith("https://")

    def cookie_name(self, name: str) -> str:
        """Имя в браузере: у HOST_COOKIES на https — с префиксом __Host- (без Secure браузер такую не примет)."""
        return HOST_PREFIX + name if self.secure and name in HOST_COOKIES else name

    def cookie(self, request: Request, name: str) -> str:
        """Значение cookie; у долгоживущих нет под новым именем — под старым, до LEGACY_UNTIL (см. HOST_COOKIES)."""
        value = request.cookies.get(self.cookie_name(name))
        if value is not None or (name != self.cookie_name(name) and (
                name not in LEGACY_COOKIES or datetime.fromisoformat(self.now()) >= LEGACY_UNTIL)):
            return value or ""
        return request.cookies.get(name, "")

    def set_cookie(self, resp, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(self.cookie_name(name), value, max_age=max_age, httponly=True, secure=self.secure,
                        samesite="lax", path="/")

    def delete_cookie(self, resp, name: str) -> None:
        """Удалить cookie под новым и старым именем. Удаление __Host- без Secure браузер отбросил бы."""
        if self.cookie_name(name) != name:
            resp.delete_cookie(self.cookie_name(name), path="/", secure=True, httponly=True, samesite="lax")
        resp.delete_cookie(name, path="/")

    def page(self, request: Request, name: str, nav: str, status_code: int = 200, **ctx):
        """Шаблон; вошедшие, человек активной полки, полки браузера, активная и участник Android-раздела — из
        request.state (на страницах входа их может не быть). here — адрес страницы для возврата (тема, выбор полки,
        «войти заново»); страница ответа на POST — каталог."""
        for key in ("users", "user", "accounts", "acct", "who"):
            ctx.setdefault(key, getattr(request.state, key, None))
        here = "/"
        if request.method == "GET":
            here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if ctx["acct"] is not None:
            ctx.setdefault("relogin", relogin_query(ctx["acct"], here))
        theme = request.cookies.get("theme", "")
        section = request.cookies.get("appshelf_os", "apple")
        if nav.startswith("a_"):
            section = "android"
        elif nav in ("catalog", "history", "blocked"):
            section = "apple"
        ua = request.headers.get("user-agent", "")
        device = ("android" if "Android" in ua else
                  "apple" if any(x in ua for x in ("iPhone", "iPad", "iPod")) else "desktop")
        ctx.update(cfg=self.cfg, nav=nav, here=here, theme=theme if theme in THEMES else "",
                   section=section, post_url=lambda path: mutation_url(path, ctx["acct"], ctx["who"], ctx["users"]),
                   device=device, ios_other=device == "apple" and any(x in ua for x in IOS_OTHER_BROWSERS))
        response = TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)
        if nav.startswith("a_"):
            response.set_cookie("appshelf_os", "android", max_age=10 * 365 * 86400,
                                samesite="lax", secure=self.secure)
        return response


def mutation_kind(path: str) -> str:
    """Routes whose target otherwise comes from a shared selection cookie."""
    path = urlsplit(path).path
    if path == "/api/status" or path.startswith(("/apps/", "/history/", "/blocked/")):
        return "account"
    if (path in ("/api/android/status", "/android/public/add/from-shelf") or path.startswith("/android/public/uploads")
            or (path.startswith("/android/") and not path.startswith("/android/public"))
            or path.startswith("/fourpda/add")):
        return "person"
    return ""


def mutation_url(path: str, acct, who, users=None) -> str:
    kind = mutation_kind(path)
    target = acct if kind == "account" else who
    if urlsplit(path).path == "/android/public/add/from-shelf":
        target = next((u for u in users or [] if u.role == people.OWNER), None)
    if not kind or target is None:
        return path
    return path + ("&" if "?" in path else "?") + urlencode({kind: target.id})


def filter_shelf(apps, request: Request, fresh: set, *, android: bool = False):
    """Filter saved apps independently from remote store search."""
    q = request.query_params.get("q", "").strip()[:200]
    mode = request.query_params.get("filter", "all")
    modes = [("all", "Все"), ("recent", "Недавно обновлены"), ("errors", "Ошибки"),
             ("active", "В очереди / скачиваются")]
    if android:
        modes.append(("pending", "Требуют решения"))
    mode = mode if mode in dict(modes) else "all"
    source = request.query_params.get("source", "") if android else ""
    total = len(apps)
    def matches(a):
        ident = a.package if android else (str(a.app_id) + " " + a.bundle_id)
        key = a.package if android else a.app_id
        return ((not q or q.casefold() in (a.name + " " + ident).casefold())
                and (not source or a.source == source)
                and (mode != "recent" or key in fresh)
                and (mode != "errors" or a.status in ("error", "nolicense") or a.check_result == "error")
                and (mode != "active" or a.status in ("queued", "downloading"))
                and (mode != "pending" or bool(a.pending)))
    return [a for a in apps if matches(a)], {"q": q, "filter_mode": mode, "source_filter": source,
                                          "total": total, "filter_options": modes}


def back(path: str = "/") -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def safe_next(target: str) -> str:
    """Только свой путь: //host и https://host увели бы после входа на чужой сайт."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


def referer_path(request: Request) -> str:
    """Своя страница, с которой пришли (тема, выбор полки) — вернуться на неё."""
    ref = urlsplit(request.headers.get("referer", ""))
    return safe_next((ref.path or "/") + (f"?{ref.query}" if ref.query else ""))


def back_to(request: Request, target: str) -> str:
    """Куда вернуться: путь из ссылки (back=…), иначе Referer своей страницы (политика страниц — same-origin;
    без него — каталог)."""
    return safe_next(target) if target else referer_path(request)


def relogin_query(acct, next_: str) -> str:
    """?email=…&next=… для «Войти заново»: та же форма входа с подставленным адресом."""
    return urlencode({"email": acct.email, "next": next_})
