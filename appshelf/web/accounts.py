"""«Аккаунты» (spec 2026-10-08 §10.4, 2026-10-12 §6, 2026-10-28 §5, §6.4, §10): Apple ID и Google-аккаунты людей,
вошедших в этом браузере, — по человеку: состояние, место, вход заново, добавить, удалить полку или Google-аккаунт,
подключить Google-аккаунт к Google Play и отключить, убрать человека с устройства, «Связать» двух вошедших, если хотя бы
у одного нет Apple ID. oauth_token подключения — только в памяти обработки формы: не в ответе, журнале, базе."""
from __future__ import annotations

import sys

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import jobs, people, store, webauth
from ..android import db as android_db, net, play
from ..config import GB
from .common import ACCT_COOKIE, WHO_COOKIE, WebCtx, back, relogin_query
from .signin import login_page, submit

ARCHIVE_DOWN = "Архив недоступен — удалить сейчас нельзя"
ACCOUNT_BUSY = "Идёт скачивание или проверка этого Apple ID — удалить можно, когда закончится"
LAST_OWNER_ID = "Последний Apple ID владельца удалить нельзя"
ANDROID_BUSY = "Идёт скачивание на Android-полку — попробуйте через минуту"
BOTH_APPLE = "У обоих есть Apple ID — их не связать"
LINK_ARCHIVE_DOWN = "Архив недоступен — связать сейчас нельзя"
NO_TOKEN = "Вставьте значение cookie oauth_token со страницы Google"
NO_PROXY = "Такого прокси нет в APPSHELF_PROXIES"
OWNER_PROXY = "Прокси выбирает владелец сервера"


def register_routes(app: FastAPI, w: WebCtx) -> None:
    def own(request: Request, aid: int):
        return next((a for a in request.state.accounts if a.id == aid), None)

    def refusal(request: Request, a) -> str:
        """Последний Apple ID владельца: решает человек полки, а не число полок в браузере."""
        person = next(u for u in request.state.users if u.id == a.user_id)
        last = sum(1 for x in request.state.accounts if x.user_id == a.user_id) == 1
        return LAST_OWNER_ID if person.role == people.OWNER and last else ""

    def confirm(request: Request, a, error: str = "", status_code: int = 200, refused: bool = False):
        with w.conn() as c:
            size = store.shelf_size(c, a.id)
        return w.page(request, "confirm.html", "apple", status_code,
                      question=f"Удалить полку {a.email} ({size / GB:.1f} ГБ)? Приложения, история и вход этого "
                               "Apple ID на сервере удалятся.",
                      action=f"/apple/{a.id}/delete", button="Удалить", cancel="/apple", error=error, refused=refused)

    def signed_here(request: Request, uid: int):
        return next((u for u in request.state.users if u.id == uid), None)

    def without(request: Request, uid: int, resp):
        """Человека больше нет — его запись из cookie прочь; вошедших не осталось — как «Выйти»."""
        with w.conn() as c:
            value = webauth.without_user(w.signed(c, request), uid)
        if value:
            w.set_cookie(resp, webauth.COOKIE, value, webauth.COOKIE_AGE)
            return resp
        resp = back("/login")
        resp.delete_cookie(webauth.COOKIE, path="/")
        resp.delete_cookie(ACCT_COOKIE, path="/")
        return resp

    @app.get("/apple", response_class=HTMLResponse)
    def my_apple_ids(request: Request):
        users, order = request.state.users, [u.id for u in request.state.users]
        with w.conn() as c:
            groups = [{"user": u, "rows": [{"a": a, "size": store.shelf_size(c, a.id),
                                            "relogin": relogin_query(a, "/apple")}
                                           for a in request.state.accounts if a.user_id == u.id],
                       "google": people.google_of(c, u.id)}
                      for u in users]
            pairs = [people.link_roles(c, a.id, b.id, order) for i, a in enumerate(users) for b in users[i + 1:]]
        by_id = {u.id: u for u in users}
        links = [{"keep": by_id[k], "gone": by_id[g]} for k, g in filter(None, pairs)]
        return w.page(request, "apple.html", "apple", groups=groups, links=links)

    def link_page(request: Request, keep, gone, error: str = "", status_code: int = 200, refused: bool = False):
        return w.page(request, "confirm.html", "apple", status_code,
                      question=f"Связать «{gone.name}» с «{keep.name}»? Google-аккаунты и Android-полка «{gone.name}» "
                               f"перейдут к «{keep.name}», вход любым их аккаунтом откроет все полки «{keep.name}». "
                               "Связку не отменить; аккаунт, которым пользуются несколько человек, не связывайте.",
                      action="/apple/link", button="Связать", cancel="/apple", error=error, refused=refused,
                      hidden={"a": keep.id, "b": gone.id})

    def link_pair(request: Request, a: int, b: int):
        """Оба — среди вошедших в этом браузере: так человек доказал, что владеет обоими. → (A, B, роли) или None."""
        ua, ub = signed_here(request, a), signed_here(request, b)
        if ua is None or ub is None or a == b:
            return None
        with w.conn() as c:
            return ua, ub, people.link_roles(c, a, b, [u.id for u in request.state.users])

    @app.get("/apple/link", response_class=HTMLResponse)
    def link_form(request: Request, a: int = 0, b: int = 0):
        found = link_pair(request, a, b)
        if found is None:
            return PlainTextResponse("not found", status_code=404)
        ua, ub, roles = found
        if roles is None:
            return link_page(request, ua, ub, BOTH_APPLE, 400, refused=True)
        keep, gone = (ua, ub) if roles[0] == a else (ub, ua)
        return link_page(request, keep, gone)

    @app.post("/apple/link", response_class=HTMLResponse)
    def link(request: Request, a: int = Form(0), b: int = Form(0)):
        found = link_pair(request, a, b)
        if found is None:
            return PlainTextResponse("not found", status_code=404)
        ua, ub, roles = found
        if roles is None:
            return link_page(request, ua, ub, BOTH_APPLE, 400, refused=True)
        keep, gone = (ua, ub) if roles[0] == a else (ub, ua)
        try:
            with w.conn() as c:
                jobs.link_users(w.cfg, c, keep.id, gone.id)
        except jobs.LinkRefused:  # у уходящего появился Apple ID, пока шёл запрос
            return link_page(request, keep, gone, BOTH_APPLE, 400, refused=True)
        except jobs.ArchiveUnavailable:
            return link_page(request, keep, gone, LINK_ARCHIVE_DOWN, 503)
        except jobs.AccountBusy:
            return link_page(request, keep, gone, ANDROID_BUSY, 409)
        resp = without(request, gone.id, back("/apple"))
        w.set_cookie(resp, WHO_COOKIE, str(keep.id), webauth.COOKIE_AGE)
        return resp

    def own_google(request: Request, gid: int):
        with w.conn() as c:
            g = people.get_google(c, gid)
        return g if g is not None and signed_here(request, g.user_id) is not None else None

    def google_confirm(request: Request, g, error: str = "", status_code: int = 200):
        with w.conn() as c:
            last = not people.has_apple(c, g.user_id) and len(people.google_of(c, g.user_id)) == 1
            shelf = android_db.shelf_for_user(c, g.user_id)
            apps = len(android_db.list_apps(c, shelf.id)) if shelf is not None else 0
        person = signed_here(request, g.user_id)
        tail = (f" Это последний аккаунт участника «{person.name}»: участник и его Android-полка (приложений: "
                f"{apps}) удалятся." if last else "")
        return w.page(request, "confirm.html", "apple", status_code,
                      question=f"Удалить Google-аккаунт {g.email}? Входить им в панель станет нельзя.{tail}",
                      action=f"/google/{g.id}/delete", button="Удалить", cancel="/apple", error=error)

    @app.get("/google/{gid}/delete", response_class=HTMLResponse)
    def google_delete_form(request: Request, gid: int):
        g = own_google(request, gid)
        if g is None:
            return PlainTextResponse("not found", status_code=404)
        return google_confirm(request, g)

    @app.post("/google/{gid}/delete", response_class=HTMLResponse)
    def google_delete(request: Request, gid: int):
        g = own_google(request, gid)
        if g is None:
            return PlainTextResponse("not found", status_code=404)
        try:
            with w.conn() as c:
                gone = jobs.remove_google(w.cfg, c, gid)
        except jobs.ArchiveUnavailable:
            return google_confirm(request, g, ARCHIVE_DOWN, 503)
        except jobs.AccountBusy:
            return google_confirm(request, g, ANDROID_BUSY, 409)
        return without(request, g.user_id, back("/apple")) if gone else back("/apple")

    def connect_page(request: Request, g, error: str = "", status_code: int = 200):
        """Подключение к Google Play: инструкция, предупреждение, поле oauth_token (без значения — его не отдаём
        назад), прокси — если владелец среди вошедших."""
        owner = any(u.role == people.OWNER for u in request.state.users)
        return w.page(request, "play_connect.html", "apple", status_code, g=g, error=error,
                      proxies=sorted(w.cfg.proxies) if owner else [])

    @app.get("/google/{gid}/play", response_class=HTMLResponse)
    def play_form(request: Request, gid: int):
        g = own_google(request, gid)
        if g is None:
            return PlainTextResponse("not found", status_code=404)
        return connect_page(request, g)

    @app.post("/google/{gid}/play", response_class=HTMLResponse)
    def play_connect(request: Request, gid: int, oauth_token: str = Form(""), proxy: str = Form("")):
        g = own_google(request, gid)
        if g is None:
            return PlainTextResponse("not found", status_code=404)
        if any(u.role == people.OWNER for u in request.state.users):
            proxy = proxy.strip()
            if proxy and proxy not in w.cfg.proxies:
                return connect_page(request, g, NO_PROXY, 400)
        elif proxy:
            return connect_page(request, g, OWNER_PROXY, 403)
        else:
            proxy = g.proxy  # выбрал владелец раньше
        if not oauth_token.strip():
            return connect_page(request, g, NO_TOKEN, 400)
        try:
            country = play.connect(w.cfg, g.id, g.email, oauth_token.strip(), proxy)
        except play.PlayError as e:  # тексты без токенов (play.py)
            return connect_page(request, g, str(e), 400)
        except net.FetchError as e:
            print(f"appshelf: подключение Google-аккаунта #{g.id} к Play: {e}", file=sys.stderr)
            return connect_page(request, g, f"Google недоступен: {e}", 502)
        with w.conn() as c:
            with store.tx(c):
                people.set_proxy(c, g.id, proxy)
                people.set_play(c, g.id, "ok", w.now(), country)
        return back("/apple")

    @app.post("/google/{gid}/play/disconnect", response_class=HTMLResponse)
    def play_disconnect(request: Request, gid: int):
        """Мастер-токен и профиль устройства — с диска, токен Play — из памяти; задания аккаунта ждут нового."""
        g = own_google(request, gid)
        if g is None:
            return PlainTextResponse("not found", status_code=404)
        with w.conn() as c:
            people.set_play(c, g.id, "none", w.now())
        store.rmtree_logged(play.token_dir(w.cfg, g.id))
        play.forget(g.id)
        return back("/apple")

    @app.get("/apple/add", response_class=HTMLResponse)
    def add_form(request: Request):
        return login_page(w, request, next_="/apple", action="/apple/add", add=True)

    @app.post("/apple/add", response_class=HTMLResponse)
    def add(request: Request, email: str = Form(""), password: str = Form("")):
        return submit(w, request, email, password, "/apple", user_id=request.state.user.id, action="/apple/add")

    @app.get("/apple/{aid}/delete", response_class=HTMLResponse)
    def delete_form(request: Request, aid: int):
        a = own(request, aid)
        if a is None:
            return PlainTextResponse("not found", status_code=404)
        why = refusal(request, a)
        return confirm(request, a, why, refused=bool(why))

    @app.post("/apple/{aid}/delete", response_class=HTMLResponse)
    def delete(request: Request, aid: int):
        a = own(request, aid)
        if a is None:
            return PlainTextResponse("not found", status_code=404)
        why = refusal(request, a)
        if why:
            return confirm(request, a, why, 400, refused=True)
        try:
            with w.conn() as c:
                jobs.remove_account(w.cfg, c, a)
        except jobs.ArchiveUnavailable:
            return confirm(request, a, ARCHIVE_DOWN, 503)
        except jobs.AccountBusy:
            return confirm(request, a, ACCOUNT_BUSY, 409)
        except jobs.LastOwnerAccount:  # другую полку владельца удалили, пока шёл этот запрос
            return confirm(request, a, LAST_OWNER_ID, 400, refused=True)
        with w.conn() as c:
            left = w.signed(c, request)  # человек ушёл, только если у него не осталось и Google-аккаунтов
        if left:
            return back("/apple")
        resp = back("/login")  # в браузере не осталось вошедших
        resp.delete_cookie(webauth.COOKIE, path="/")
        resp.delete_cookie(ACCT_COOKIE, path="/")
        return resp
