"""Участники (spec 2026-10-08 §10.5): только владельцу — приглашения, люди и их Apple ID, запасные ссылки входа,
«выйти везде», удаление. Новая ссылка показывается один раз: в базе только хэш."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import jobs, people, store
from .accounts import ACCOUNT_BUSY, ARCHIVE_DOWN
from .common import WebCtx, back

SELF = "Себя удалить нельзя"
CREATED = "appshelf_created"  # новая ссылка до показа на /admin: ответ на POST — переход, «обновить» её не повторит
CREATED_AGE = 300


def owner_only(request: Request) -> None:
    """Владелец — среди вошедших в этом браузере; активной может быть и чужая полка (spec 2026-10-12 §6)."""
    if not any(u.role == people.OWNER for u in request.state.users):
        raise HTTPException(403, "только владельцу")


def register_routes(app: FastAPI, w: WebCtx) -> None:
    router = APIRouter(prefix="/admin", dependencies=[Depends(owner_only)])

    def admin_page(request: Request, created_link: str = ""):
        with w.conn() as c:
            invites = people.list_invites(c)
            persons = [{"user": u, "accounts": [{"a": a, "size": store.shelf_size(c, a.id)}
                                                for a in people.accounts_of(c, u.id)],
                        "google": people.google_of(c, u.id)}
                       for u in people.list_users(c)]
        return w.page(request, "admin.html", "admin", invites=invites, persons=persons, created_link=created_link)

    def confirm(request: Request, uid: int, error: str = "", status_code: int = 200, refused: bool = False):
        """Подтверждение удаления. Приглашение, по которому человек пришёл, бессрочное: не отключи его —
        удалённый зарегистрируется по той же ссылке снова. Пришёл им один — галочка «отключить» стоит сразу."""
        with w.conn() as c:
            user = people.get_user(c, uid)
            emails = ", ".join([a.email for a in people.accounts_of(c, uid)] +
                               [f"Google {g.email}" for g in people.google_of(c, uid)])
            invite = people.invite_of(c, uid)
        option = None
        if invite is not None and not invite["disabled_at"]:
            others = invite["joined"] - 1
            option = {"name": "disable_invite", "checked": others == 0,
                      "text": f"Отключить приглашение «{invite['label']}» — по нему можно зарегистрироваться снова"
                              + (f" (им пришли ещё {others}: для новых участников создайте другое)" if others else "")}
        return w.page(request, "confirm.html", "admin", status_code,
                      question=f"Удалить участника «{user.name}» ({emails})? Полки, история, вход этих аккаунтов "
                               "на сервере, Android-полка и доступ к панели удалятся.",
                      action=f"/admin/users/{uid}/delete", button="Удалить", cancel="/admin", error=error,
                      refused=refused, option=option)

    def exists(uid: int) -> bool:
        with w.conn() as c:
            return people.get_user(c, uid) is not None

    def is_me(request: Request, uid: int) -> bool:
        """Себя — значит владельца: сюда пускают только его, а человек активной полки может быть и другим."""
        return any(u.id == uid and u.role == people.OWNER for u in request.state.users)

    def created(prefix: str, token: str):
        resp = back("/admin")
        w.set_cookie(resp, CREATED, f"{prefix}:{token}", CREATED_AGE)
        return resp

    @router.get("", response_class=HTMLResponse)
    def admin(request: Request):
        prefix, _, token = request.cookies.get(CREATED, "").partition(":")
        ok = prefix in ("join", "l") and re.fullmatch(r"[\w-]+", token)
        resp = admin_page(request, f"{w.cfg.public_base}/{prefix}/{token}" if ok else "")
        if CREATED in request.cookies:
            resp.delete_cookie(CREATED, path="/")
        return resp

    @router.post("/invites")
    def create_invite(label: str = Form("")):
        with w.conn() as c:
            token = people.create_link(c, people.INVITE, label.strip() or "приглашение", None, w.now())
        return created("join", token)

    @router.post("/invites/{lid}/disable")
    def disable_invite(lid: int):
        with w.conn() as c:
            people.disable_invite(c, lid, w.now())
        return back("/admin")

    @router.post("/invites/{lid}/delete")
    def delete_invite(lid: int):
        with w.conn() as c, store.tx(c):
            people.delete_invite(c, lid)
        return back("/admin")

    @router.post("/users/{uid}/login-link")
    def login_link(uid: int):
        if not exists(uid):
            return PlainTextResponse("not found", status_code=404)
        with w.conn() as c:
            token = people.create_link(c, people.LOGIN, "", uid, w.now())
        return created("l", token)

    @router.post("/users/{uid}/logout-all")
    def logout_all(request: Request, uid: int):
        with w.conn() as c:
            people.bump_epoch(c, uid)
        return back("/" if is_me(request, uid) else "/admin")  # погасил себя — «Участники» этому браузеру закрыты

    @router.get("/users/{uid}/delete", response_class=HTMLResponse)
    def delete_form(request: Request, uid: int):
        if not exists(uid):
            return PlainTextResponse("not found", status_code=404)
        me = is_me(request, uid)
        return confirm(request, uid, SELF if me else "", refused=me)

    @router.post("/users/{uid}/delete", response_class=HTMLResponse)
    def delete(request: Request, uid: int, disable_invite: str = Form("")):
        if not exists(uid):
            return PlainTextResponse("not found", status_code=404)
        if is_me(request, uid):
            return confirm(request, uid, SELF, 400, refused=True)
        try:
            with w.conn() as c:
                invite = people.invite_of(c, uid)  # после удаления человека его приглашение уже не найти
                jobs.remove_user(w.cfg, c, uid)
                if disable_invite and invite is not None:
                    people.disable_invite(c, invite["id"], w.now())
        except jobs.ArchiveUnavailable:
            return confirm(request, uid, ARCHIVE_DOWN, 503)
        except jobs.AccountBusy:
            return confirm(request, uid, ACCOUNT_BUSY, 409)
        return back("/admin")

    app.include_router(router)
