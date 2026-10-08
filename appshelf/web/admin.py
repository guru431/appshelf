"""Участники (spec 2026-10-08 §10.5): только владельцу — приглашения, люди и их Apple ID, запасные ссылки входа,
«выйти везде», удаление. Новая ссылка показывается один раз: в базе только хэш."""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import jobs, people, store
from .accounts import ARCHIVE_DOWN
from .common import WebCtx, back

SELF = "Себя удалить нельзя"


def owner_only(request: Request) -> None:
    if request.state.user.role != people.OWNER:
        raise HTTPException(403, "только владельцу")


def register_routes(app: FastAPI, w: WebCtx) -> None:
    router = APIRouter(prefix="/admin", dependencies=[Depends(owner_only)])

    def admin_page(request: Request, created_link: str = ""):
        with w.conn() as c:
            invites = people.list_invites(c)
            persons = [{"user": u, "accounts": [{"a": a, "size": store.shelf_size(c, a.id)}
                                                for a in people.accounts_of(c, u.id)]}
                       for u in people.list_users(c)]
        return w.page(request, "admin.html", "admin", invites=invites, persons=persons, created_link=created_link)

    def confirm(request: Request, uid: int, error: str = "", status_code: int = 200, refused: bool = False):
        with w.conn() as c:
            user = people.get_user(c, uid)
            emails = ", ".join(a.email for a in people.accounts_of(c, uid))
        return w.page(request, "confirm.html", "admin", status_code,
                      question=f"Удалить участника «{user.name}» ({emails})? Полки, история, вход этих Apple ID "
                               "на сервере и доступ к панели удалятся.",
                      action=f"/admin/users/{uid}/delete", button="Удалить", cancel="/admin", error=error,
                      refused=refused)

    def exists(uid: int) -> bool:
        with w.conn() as c:
            return people.get_user(c, uid) is not None

    @router.get("", response_class=HTMLResponse)
    def admin(request: Request):
        return admin_page(request)

    @router.post("/invites", response_class=HTMLResponse)
    def create_invite(request: Request, label: str = Form("")):
        with w.conn() as c:
            token = people.create_link(c, people.INVITE, label.strip() or "приглашение", None, w.now())
        return admin_page(request, f"{w.cfg.public_base}/join/{token}")

    @router.post("/invites/{lid}/disable")
    def disable_invite(lid: int):
        with w.conn() as c:
            people.disable_invite(c, lid, w.now())
        return back("/admin")

    @router.post("/users/{uid}/login-link", response_class=HTMLResponse)
    def login_link(request: Request, uid: int):
        if not exists(uid):
            return PlainTextResponse("not found", status_code=404)
        with w.conn() as c:
            token = people.create_link(c, people.LOGIN, "", uid, w.now())
        return admin_page(request, f"{w.cfg.public_base}/l/{token}")

    @router.post("/users/{uid}/logout-all")
    def logout_all(uid: int):
        with w.conn() as c:
            people.bump_epoch(c, uid)
        return back("/admin")

    @router.get("/users/{uid}/delete", response_class=HTMLResponse)
    def delete_form(request: Request, uid: int):
        if not exists(uid):
            return PlainTextResponse("not found", status_code=404)
        me = uid == request.state.user.id
        return confirm(request, uid, SELF if me else "", refused=me)

    @router.post("/users/{uid}/delete", response_class=HTMLResponse)
    def delete(request: Request, uid: int):
        if not exists(uid):
            return PlainTextResponse("not found", status_code=404)
        if uid == request.state.user.id:
            return confirm(request, uid, SELF, 400, refused=True)
        try:
            with w.conn() as c:
                jobs.remove_user(w.cfg, c, uid)
        except jobs.ArchiveUnavailable:
            return confirm(request, uid, ARCHIVE_DOWN, 503)
        return back("/admin")

    app.include_router(router)
