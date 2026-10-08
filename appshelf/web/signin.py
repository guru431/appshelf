"""Вход в панель Apple ID (spec 2026-10-08 §4). Известный Apple ID — сразу в Apple; новый — только по приглашению
(cookie после /join/), как APPSHELF_OWNER, пока владельца нет, или вторым Apple ID уже вошедшего человека
(«Мои Apple ID» → «Добавить»). Пока не решено «пускать», к Apple не обращаемся. Пароль — в памяти LoginFlow
не дольше 10 минут."""
from __future__ import annotations

import os
import secrets
import sqlite3
import sys
from math import ceil
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from .. import notify, people, store, webauth
from ..ipatool import IpatoolError
from .common import ACCT_COOKIE, INVITE_COOKIE, LOGIN_COOKIE, WebCtx, back, referer_path, safe_next
from .login import TTL, Intent, LoginBusy, LoginExpired, drop_home

LOGIN_ERRORS = {
    "edge_rejected": "Apple отклонил вход с этого адреса — попробуйте позже",
    "invalid_credentials": "Неверный пароль или код",
    "busy": "Идёт скачивание для этого Apple ID — попробуйте через пару минут",
    "timeout": "Apple не ответил за 2 минуты — попробуйте позже",
    "bad_device_mac": "Сбой настройки сервера — сообщите владельцу",
}
NOT_REGISTERED = "Этот Apple ID на сервере не зарегистрирован — нужна ссылка-приглашение от владельца"
NOT_CONFIGURED = "Сервер не настроен: не задан APPSHELF_OWNER"
INVITE_GONE = "Ссылка-приглашение недействительна — попросите новую"
LINK_GONE = "Ссылка недействительна — попросите новую"
TAKEN = "Этот Apple ID уже зарегистрирован — войдите ещё раз"
EXPIRED_STEP = "Срок шага истёк — введите Apple ID и пароль заново"
TOO_MANY = "Слишком много неудачных попыток — подождите минуту"
JOINED_SUBJECT = "appshelf: новый участник"
INVITE_AGE = 86400  # cookie принятого приглашения: сутки на первый вход


class Refused(Exception):
    """Новый Apple ID не регистрируется: приглашение отключили, владелец уже есть, адрес заняли."""


def login_error_text(e: IpatoolError) -> str:
    return LOGIN_ERRORS.get(e.error, f"Ошибка входа: {e.message}")


def relogin_query(acct, next_: str) -> str:
    """?email=…&next=… для «Войти заново»: та же форма входа с подставленным адресом."""
    return urlencode({"email": acct.email, "next": next_})


def login_page(w: WebCtx, request: Request, *, step: int = 1, email: str = "", next_: str = "/", error: str = "",
               status_code: int = 200, action: str = "/login", add: bool = False):
    return w.page(request, "login.html", "apple" if add else "login", status_code, step=step, email=email,
                  next=safe_next(next_), error=error, action=action, add=add,
                  invited=bool(request.cookies.get(INVITE_COOKIE)))


def signed_in(w: WebCtx, resp, user, aid: int):
    """cookie человека на год и выбранная полка."""
    w.set_cookie(resp, webauth.COOKIE, webauth.make_cookie(w.cookie_key(), user, w.epoch()), webauth.COOKIE_AGE)
    w.set_cookie(resp, ACCT_COOKIE, str(aid), webauth.COOKIE_AGE)
    return resp


def new_intent(w: WebCtx, email: str, **kw) -> Intent:
    """Новый Apple ID: временный HOME ipatool (accounts/.new-*) и свой MAC."""
    home = w.cfg.accounts_dir / f".new-{secrets.token_hex(8)}"
    home.mkdir(mode=0o700, parents=True)
    return Intent(email, home=home, device_mac=people.new_device_mac(), **kw)


def decide(w: WebCtx, c, request: Request, email: str, user_id: int | None) -> Intent | None:
    """До обращения к Apple: кого пускаем. None — неизвестный Apple ID без права на регистрацию."""
    acct = people.account_by_email(c, email)
    if acct is not None:
        return Intent(email, account_id=acct.id)
    if user_id is not None:
        return new_intent(w, email, user_id=user_id)
    invite = people.active_invite(c, request.cookies.get(INVITE_COOKIE, ""))
    if invite is not None:
        return new_intent(w, email, invite_id=invite["id"])
    if w.cfg.owner_email and email == w.cfg.owner_email and not people.owner_exists(c):
        return new_intent(w, email, owner=True)
    return None


def register(w: WebCtx, c, intent: Intent, info: dict, now: str) -> tuple[int, int]:
    """Новый Apple ID после успешного входа: строки users/accounts одной транзакцией, затем временный HOME →
    accounts/<id>; о новом участнике — письмо владельцу."""
    joined = None
    try:
        with store.tx(c):
            if intent.invite_id is not None and not people.invite_active(c, intent.invite_id):
                raise Refused(INVITE_GONE)
            if intent.owner and people.owner_exists(c):
                raise Refused(NOT_REGISTERED)
            if intent.user_id is not None and people.get_user(c, intent.user_id) is None:
                raise Refused(NOT_REGISTERED)  # человека удалили, пока он добавлял Apple ID
            uid = intent.user_id
            if uid is None:
                role = people.OWNER if intent.owner else people.MEMBER
                uid = people.create_user(c, str(info.get("name") or intent.email), role, intent.invite_id, now)
                joined = intent.invite_id
            aid = people.create_account(c, uid, intent.email, info, intent.device_mac, now)
    except sqlite3.IntegrityError:  # тот же новый Apple ID успел войти из другого браузера
        raise Refused(TAKEN) from None
    os.replace(intent.home, w.cfg.accounts_dir / str(aid))
    if joined is not None:
        notify.send_quietly(w.env.send, JOINED_SUBJECT,
                            f"{info.get('name') or intent.email}, Apple ID {intent.email}, приглашение "
                            f"«{people.invite_label(c, joined)}».\nУчастники: {w.cfg.public_base}/admin\n",
                            w.cfg.mail_to)
    return aid, uid


def complete(w: WebCtx, request: Request, intent: Intent, info: dict, next_: str):
    """Apple впустил: известному Apple ID — свежий токен, новому — регистрация; устройству — cookie на год."""
    now = w.now()
    with w.conn() as c:
        if intent.account_id is not None:
            acct = people.get_account(c, intent.account_id)
            if acct is None:  # Apple ID удалили, пока ждали код
                return login_page(w, request, email=intent.email, next_=next_, error=NOT_REGISTERED, status_code=403)
            people.mark_login(c, acct.id, info, now)
            aid, uid = acct.id, acct.user_id
        else:
            try:
                aid, uid = register(w, c, intent, info, now)
            except Refused as e:
                drop_home(intent)
                return login_page(w, request, email=intent.email, next_=next_, error=str(e), status_code=403)
        user = people.get_user(c, uid)
    resp = signed_in(w, back(safe_next(next_)), user, aid)
    resp.delete_cookie(LOGIN_COOKIE, path="/")
    resp.delete_cookie(INVITE_COOKIE, path="/")
    return resp


def submit(w: WebCtx, request: Request, email: str, password: str, next_: str, *, user_id: int | None = None,
           action: str = "/login"):
    """Шаг 1 входа (и «Добавить Apple ID» — с user_id вошедшего человека)."""
    add = user_id is not None
    email = people.normalize_email(email)

    def again(error: str, status_code: int):
        return login_page(w, request, email=email, next_=next_, error=error, status_code=status_code,
                          action=action, add=add)

    if not email or not password:
        return again("Нужны Apple ID и пароль", 400)
    if w.limiter.blocked():
        return again(TOO_MANY, 429)
    wait = w.limiter.email_wait(email)
    if wait:
        return again(f"Слишком много неверных паролей — вход этим Apple ID закрыт, попробуйте через "
                     f"{ceil(wait / 60)} мин", 429)
    with w.conn() as c:
        intent = decide(w, c, request, email, user_id)
        if intent is None:
            w.limiter.fail()
            configured = bool(w.cfg.owner_email) or people.owner_exists(c)
            return again(NOT_REGISTERED if configured else NOT_CONFIGURED, 403)
        tool = (w.env.tools(people.get_account(c, intent.account_id)) if intent.account_id is not None
                else w.new_tool(intent.home, intent.device_mac))
    try:
        flow_id, info = w.flow.start(tool, intent, password)
    except LoginBusy:
        return again("Сейчас входят слишком многие — попробуйте через минуту", 503)
    except IpatoolError as e:
        if e.error == "invalid_credentials":
            w.limiter.fail()
            w.limiter.email_fail(email)
        elif e.error == "bad_device_mac":
            print(f"appshelf: ipatool {e}", file=sys.stderr)
        return again(login_error_text(e), 400)
    if info is None:
        resp = login_page(w, request, step=2, email=email, next_=next_, add=add)
        w.set_cookie(resp, LOGIN_COOKIE, flow_id, int(TTL))
        return resp
    return complete(w, request, intent, info, next_)


def register_routes(app: FastAPI, w: WebCtx) -> None:
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/", email: str = ""):
        pending = w.flow.pending(request.cookies.get(LOGIN_COOKIE, ""))
        if pending is not None:  # вернулись на страницу посреди шага кода
            return login_page(w, request, step=2, email=pending.email, next_=next)
        return login_page(w, request, email=email, next_=next)

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/")):
        return submit(w, request, email, password, next)

    @app.post("/login/code", response_class=HTMLResponse)
    def login_code(request: Request, code: str = Form(""), next: str = Form("/")):
        flow_id = request.cookies.get(LOGIN_COOKIE, "")
        pending = w.flow.pending(flow_id)
        if pending is None:
            return login_page(w, request, next_=next, error=EXPIRED_STEP, status_code=400)
        code = "".join(code.split())
        if not (code.isdigit() and len(code) == 6):  # опечатка в форме — не попытка входа, шаг не расходуем
            return login_page(w, request, step=2, email=pending.email, next_=next, error="Код — 6 цифр",
                              status_code=400)
        try:
            intent, info = w.flow.finish(flow_id, code)
        except LoginExpired:
            return login_page(w, request, next_=next, error=EXPIRED_STEP, status_code=400)
        except IpatoolError as e:
            if e.error == "invalid_credentials":
                w.limiter.fail()
                w.limiter.email_fail(pending.email)
            return login_page(w, request, email=pending.email, next_=next, error=login_error_text(e),
                              status_code=400)
        return complete(w, request, intent, info, next)

    @app.get("/join/{token}", response_class=HTMLResponse)
    def join(request: Request, token: str):
        with w.conn() as c:
            invite = people.active_invite(c, token)
        if invite is None:
            w.limiter.fail()
            return login_page(w, request, error=INVITE_GONE, status_code=400)
        resp = back("/login")
        w.set_cookie(resp, INVITE_COOKIE, token, INVITE_AGE)
        return resp

    @app.get("/l/{token}", response_class=HTMLResponse)
    def login_link(request: Request, token: str):
        if w.limiter.blocked():
            return login_page(w, request, error=TOO_MANY, status_code=429)
        with w.conn() as c:
            uid = people.use_login_link(c, token, w.now())
            user = people.get_user(c, uid) if uid is not None else None
            accounts = people.accounts_of(c, uid) if user is not None else []
        if not accounts:
            w.limiter.fail()
            return login_page(w, request, error=LINK_GONE, status_code=400)
        return signed_in(w, back("/"), user, accounts[0].id)

    def logged_out():
        resp = back("/login")
        resp.delete_cookie(webauth.COOKIE, path="/")
        resp.delete_cookie(ACCT_COOKIE, path="/")
        return resp

    @app.post("/logout")
    def logout():
        return logged_out()

    @app.post("/logout-all")
    def logout_all(request: Request):
        with w.conn() as c:
            people.bump_epoch(c, request.state.user.id)
        return logged_out()

    @app.get("/acct/{aid}")
    def switch_shelf(request: Request, aid: int):
        resp = back(referer_path(request))
        if any(a.id == aid for a in request.state.accounts):
            w.set_cookie(resp, ACCT_COOKIE, str(aid), webauth.COOKIE_AGE)
        return resp
