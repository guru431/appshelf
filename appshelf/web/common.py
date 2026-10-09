"""Общее для страниц appshelf (spec 2026-10-08 §4, §10): контекст приложения, шаблоны, имена cookie."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import jobs, people, store, webauth
from ..config import Config
from .login import Limiter, LoginFlow

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
THEMES = ("light", "dark")
ACCT_COOKIE = "appshelf_acct"      # активная полка: id Apple ID вошедшего человека
LOGIN_COOKIE = "appshelf_login"    # незавершённый вход (шаг кода 2FA): id в памяти LoginFlow
INVITE_COOKIE = "appshelf_invite"  # принятое приглашение — до первого входа нового Apple ID


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

    def person(self, request: Request):
        """(человек, его Apple ID, активный) по cookie или None. Активный — из cookie выбора полки, если этот Apple ID
        принадлежит человеку, иначе первый."""
        with self.conn() as c:
            user = webauth.check_cookie(self.key, request.cookies.get(webauth.COOKIE, ""), self.epoch(),
                                        lambda uid: people.get_user(c, uid))
            accounts = people.accounts_of(c, user.id) if user is not None else []
        if not accounts:
            return None
        want = request.cookies.get(ACCT_COOKIE, "")
        return user, accounts, next((a for a in accounts if str(a.id) == want), accounts[0])

    @property
    def secure(self) -> bool:
        return self.cfg.public_base.startswith("https://")

    def set_cookie(self, resp, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=self.secure, samesite="lax", path="/")

    def page(self, request: Request, name: str, nav: str, status_code: int = 200, **ctx):
        """Шаблон; вошедший человек, его Apple ID и активный — из request.state (на страницах входа их нет).
        here — адрес страницы для возврата (тема, выбор полки, «войти заново»); страница ответа на POST — каталог."""
        for key in ("user", "accounts", "acct"):
            ctx.setdefault(key, getattr(request.state, key, None))
        here = "/"
        if request.method == "GET":
            here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if ctx["acct"] is not None:
            ctx.setdefault("relogin", relogin_query(ctx["acct"], here))
        theme = request.cookies.get("theme", "")
        ctx.update(cfg=self.cfg, nav=nav, here=here, theme=theme if theme in THEMES else "")
        return TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)


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
    """Куда вернуться: путь из ссылки (back=…), иначе Referer — его браузер не шлёт, раз Apache отдаёт
    Referrer-Policy: no-referrer."""
    return safe_next(target) if target else referer_path(request)


def relogin_query(acct, next_: str) -> str:
    """?email=…&next=… для «Войти заново»: та же форма входа с подставленным адресом."""
    return urlencode({"email": acct.email, "next": next_})
