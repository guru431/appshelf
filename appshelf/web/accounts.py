"""«Мои Apple ID» (spec 2026-10-08 §10.4): Apple ID вошедшего человека — состояние, место, вход заново,
добавить, удалить полку."""
from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import jobs, people, store, webauth
from ..config import GB
from .common import ACCT_COOKIE, WebCtx, back
from .signin import login_page, relogin_query, submit

ARCHIVE_DOWN = "Архив недоступен — удалить сейчас нельзя"
ACCOUNT_BUSY = "Идёт скачивание или проверка этого Apple ID — удалить можно, когда закончится"
LAST_OWNER_ID = "Последний Apple ID владельца удалить нельзя"


def register_routes(app: FastAPI, w: WebCtx) -> None:
    def own(request: Request, aid: int):
        return next((a for a in request.state.accounts if a.id == aid), None)

    def refusal(request: Request) -> str:
        if request.state.user.role == people.OWNER and len(request.state.accounts) == 1:
            return LAST_OWNER_ID
        return ""

    def confirm(request: Request, a, error: str = "", status_code: int = 200, refused: bool = False):
        with w.conn() as c:
            size = store.shelf_size(c, a.id)
        return w.page(request, "confirm.html", "apple", status_code,
                      question=f"Удалить полку {a.email} ({size / GB:.1f} ГБ)? Приложения, история и вход этого "
                               "Apple ID на сервере удалятся.",
                      action=f"/apple/{a.id}/delete", button="Удалить", cancel="/apple", error=error, refused=refused)

    @app.get("/apple", response_class=HTMLResponse)
    def my_apple_ids(request: Request):
        with w.conn() as c:
            rows = [{"a": a, "size": store.shelf_size(c, a.id), "relogin": relogin_query(a, "/apple")}
                    for a in request.state.accounts]
        return w.page(request, "apple.html", "apple", rows=rows)

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
        why = refusal(request)
        return confirm(request, a, why, refused=bool(why))

    @app.post("/apple/{aid}/delete", response_class=HTMLResponse)
    def delete(request: Request, aid: int):
        a = own(request, aid)
        if a is None:
            return PlainTextResponse("not found", status_code=404)
        why = refusal(request)
        if why:
            return confirm(request, a, why, 400, refused=True)
        try:
            with w.conn() as c:
                jobs.remove_account(w.cfg, c, a)
        except jobs.ArchiveUnavailable:
            return confirm(request, a, ARCHIVE_DOWN, 503)
        except jobs.AccountBusy:
            return confirm(request, a, ACCOUNT_BUSY, 409)
        if len(request.state.accounts) > 1:
            return back("/apple")
        resp = back("/login")  # был последний Apple ID — человека больше нет
        resp.delete_cookie(webauth.COOKIE, path="/")
        resp.delete_cookie(ACCT_COOKIE, path="/")
        return resp
