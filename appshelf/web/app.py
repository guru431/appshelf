"""Страницы appshelf (spec §8). Пароль проверяет само приложение (webauth: к сокету ходят все процессы
www-data): форма /login с cookie или заголовок Basic; слушает только unix-сокет /run/appshelf/web.sock,
один процесс uvicorn."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import ipatool, jobs, notify, removed, store, webauth
from ..config import GB, Config, from_env
from ..ipatool import IpatoolError
from . import pwa
from .login import LoginExpired, LoginFlow

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LOGIN_ERRORS = {
    "edge_rejected": "Apple отклонил вход с этого адреса — попробуйте позже",
    "invalid_credentials": "Неверный пароль или код",
    "busy": "Идёт скачивание или ночная проверка — попробуйте через несколько минут",
    "timeout": "Apple не ответил за 2 минуты — попробуйте позже",
}


def login_error_text(e: IpatoolError) -> str:
    return LOGIN_ERRORS.get(e.error, f"Ошибка входа: {e.message}")


THEMES = ("light", "dark")
FAIL_LIMIT, FAIL_WINDOW = 10, 60.0  # неудачных проверок пароля за секунд


def version_key(v: str) -> tuple:
    """Части версии сравниваются как числа: 10.0 > 2.10 > 2.9."""
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-_ ]+", v or "") if p)


HISTORY_SORTS = {  # ключ → (подпись, направление по умолчанию, ключ сортировки)
    "name": ("Название", "asc", lambda r: (r["name"].casefold(),)),
    "date": ("Дата покупки", "desc", lambda r: (r["purchase_date"],)),
    "version": ("Версия", "desc", lambda r: version_key(r["version"])),
    "bundle": ("Bundle ID", "asc", lambda r: (r["bundle_id"].casefold(),)),
    "status": ("В каталоге", "desc", lambda r: (bool(r["published"]),)),
}

APP_ID_RE = re.compile(r"^\s*(?:id)?(\d{5,12})\s*$|/id(\d{5,12})(?:[/?#]|$)")


def parse_app_id(ref: str) -> int | None:
    """ID App Store из ввода: 492224193, id492224193 или ссылка apps.apple.com/…/id492224193."""
    m = APP_ID_RE.search(ref.strip())
    return int(m.group(1) or m.group(2)) if m else None


def create_app(cfg: Config, tool, *, now=jobs.now_iso, clock=time.monotonic, send=notify.send_mail,
               disk_free=jobs.free_bytes, start_worker: bool = True) -> FastAPI:
    env = jobs.Env(cfg, tool, now=now, send=send, disk_free=disk_free)
    flow = LoginFlow(tool, clock=clock)
    worker = jobs.Worker(env, lambda: store.connect(cfg.db), purge=flow.purge)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield

    app = FastAPI(title="appshelf", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.env, app.state.login, app.state.worker = env, flow, worker

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # Apache Basic Auth браузер подставляет и в межсайтовые POST: сверяем Origin с хостом запроса
        origin = request.headers.get("origin")
        if origin == "null" and request.headers.get("sec-fetch-site") == "same-origin":
            # Referrer-Policy: no-referrer (глобально в Apache) превращает Origin своего POST в null;
            # Sec-Fetch-Site ставит сам браузер, со страницы его не подделать
            origin = None
        if request.method == "POST" and origin is not None:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            host = host.split(",")[0].strip().lower()
            if urlsplit(origin).netloc.lower() != host:
                return PlainTextResponse("Forbidden: cross-origin POST", status_code=403)
        return await call_next(request)

    auth_seen: set[bytes] = set()
    key_box: list[bytes] = []
    fails: deque[float] = deque()  # время неудачных проверок пароля (monotonic)

    def too_many_fails() -> bool:
        """Не больше FAIL_LIMIT неудач за FAIL_WINDOW: дальше пароль не проверяем (PBKDF2 ~0,2 с CPU)."""
        t = clock()
        while fails and t - fails[0] > FAIL_WINDOW:
            fails.popleft()
        return len(fails) >= FAIL_LIMIT

    async def password_ok(check, *args) -> bool | None:
        """None — лимит неудач исчерпан, пароль не проверялся."""
        if too_many_fails():
            return None
        ok = await run_in_threadpool(check, *args)  # PBKDF2 — не в цикле событий
        if not ok:
            fails.append(clock())
        return ok

    def cookie_key() -> bytes:
        if not key_box:
            key_box.append(webauth.load_key(cfg.cookie_key))
        return key_box[0]

    def epoch() -> int:
        return int(datetime.fromisoformat(now()).timestamp())

    def web_users() -> dict[str, str]:
        try:
            return webauth.read_users(cfg.web_auth)
        except OSError:
            return {}

    @app.middleware("http")
    async def auth(request: Request, call_next):
        # добавлен последним — выполняется первым. Без пароля: /healthz (мониторинг), /pwa/ (манифест и
        # иконка — iOS берёт их без авторизации), /login. Вход — cookie формы /login или заголовок Basic.
        path = request.url.path
        if path in ("/healthz", "/login") or path.startswith("/pwa/"):
            return await call_next(request)
        users = web_users()
        if not users:  # fail closed
            return PlainTextResponse("503: пароль страниц не задан (appshelf set-web-password)", status_code=503)
        header = request.headers.get("authorization", "")
        if webauth.check_cookie(cookie_key(), users, request.cookies.get(webauth.COOKIE, ""), epoch()) or (
                header and await password_ok(webauth.check, users, header, auth_seen)):
            return await call_next(request)
        if request.method == "GET" and not path.startswith("/api/"):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)
        # без WWW-Authenticate: окно Basic Auth в браузере не нужно, вход — формой
        return PlainTextResponse("401: нужен вход — /login", status_code=401)

    def conn():
        return closing(store.connect(cfg.db))

    def session_info(c) -> dict:
        return {k: store.get_state(c, k) for k in ("session", "session_since", "account_name", "storefront")}

    def page(request: Request, name: str, nav: str, status_code: int = 200, **ctx):
        with conn() as c:
            ctx.setdefault("session", session_info(c))
        theme = request.cookies.get("theme", "")
        ctx.update(cfg=cfg, nav=nav, theme=theme if theme in THEMES else "")
        return TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)

    def back(path: str = "/"):
        return RedirectResponse(path, status_code=303)

    def apple_page(request: Request, step: int, error: str = "", status_code: int = 200, email: str | None = None):
        return page(request, "apple.html", "apple", status_code, step=step, error=error,
                    email=flow.pending_email if email is None else email)

    @app.get("/healthz")
    def healthz():
        with conn() as c:
            c.execute("SELECT 1")
        return PlainTextResponse("ok")

    def login_page(request: Request, next_: str, error: str = "", status_code: int = 200):
        return TEMPLATES.TemplateResponse(request, "login.html", {"nav": "login", "theme": "", "session": {},
                                          "next": next_, "error": error}, status_code=status_code)

    def safe_next(target: str) -> str:
        """Только свой путь: //host и https://host увели бы после входа на чужой сайт."""
        return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return login_page(request, safe_next(next))

    @app.post("/login")
    async def login(request: Request, user: str = Form(""), password: str = Form(""), next: str = Form("/")):
        users = web_users()
        ok = await password_ok(webauth.verify, users[user], password) if user in users else False
        if ok is None:
            return login_page(request, safe_next(next), "Слишком много неудачных попыток — подождите минуту",
                              status_code=429)
        if not ok:
            return login_page(request, safe_next(next), "Неверный логин или пароль", status_code=401)
        resp = back(safe_next(next))
        resp.set_cookie(webauth.COOKIE, webauth.make_cookie(cookie_key(), users, user, epoch()),
                        max_age=webauth.COOKIE_AGE, httponly=True, secure=cfg.public_base.startswith("https://"),
                        samesite="lax", path="/")
        return resp

    @app.get("/pwa/manifest.webmanifest")
    def pwa_manifest():
        return JSONResponse(pwa.MANIFEST, media_type="application/manifest+json")

    @app.get("/pwa/icon-{size:int}.png")
    def pwa_icon(size: int):
        if size not in pwa.SIZES:
            return PlainTextResponse("not found", status_code=404)
        return Response(pwa.icon_png(size), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/", response_class=HTMLResponse)
    def catalog(request: Request):
        with conn() as c:
            apps = store.list_apps(c)
            info = session_info(c)
            nightly = (store.get_state(c, "nightly_last"), store.get_state(c, "nightly_result"))
            busy = store.has_active_jobs(c)
        banners = []
        if info["session"] == "expired":
            banners.append("Вход в Apple ID истёк — войдите на странице «Apple ID», задания ждут.")
        try:
            free = jobs.free_space(env)
        except jobs.ArchiveUnavailable:
            banners.append(f"Архив недоступен ({cfg.pub_root.parent}): установка и скачивание не работают — "
                           "смонтирован ли он?")
        else:
            if free < jobs.BANNER_FREE:
                banners.append(f"Мало места: свободно {free / GB:.1f} ГБ.")
        statuses = json.dumps({str(a.app_id): a.status for a in apps})
        return page(request, "catalog.html", "catalog", apps=apps, session=info, banners=banners,
                    nightly=nightly, busy=busy, statuses=statuses)

    @app.get("/theme")
    def theme(request: Request, set: str = ""):
        """Тема: light / dark / auto (как в системе). Возврат на ту же страницу."""
        ref = urlsplit(request.headers.get("referer", ""))
        target = (ref.path or "/") + (f"?{ref.query}" if ref.query else "")
        resp = back(target if target.startswith("/") else "/")
        if set in THEMES:
            resp.set_cookie("theme", set, max_age=10 * 365 * 86400, samesite="lax", secure=True)
        else:
            resp.delete_cookie("theme")
        return resp

    def matches(needle: str, alias: dict, app_id: int, *texts: str) -> bool:
        return any(needle in t.casefold() for t in texts) or needle in alias.get(app_id, "")

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, q: str = "", sort: str = "name", dir: str = ""):
        with conn() as c:
            rows = store.list_purchases(c)
            refreshed = store.history_refreshed_at(c)
            refreshing, refresh_error, refresh_error_at = store.refresh_status(c)
            busy = store.has_active_jobs(c)
        needle = q.strip().casefold()  # в Python: lower()/LIKE SQLite кириллицу не понимают
        if needle:
            alias = removed.aliases()  # «vk» найдёт «ВКонтакте», «max» — «МАКС»
            rows = [r for r in rows if matches(needle, alias, r["app_id"], r["name"], r["bundle_id"])]
        sort = sort if sort in HISTORY_SORTS else "name"
        label, default_dir, key = HISTORY_SORTS[sort]
        dir = dir if dir in ("asc", "desc") else default_dir
        rows = sorted(rows, key=lambda r: (r["name"].casefold(),))       # стабильный второй ключ — название
        rows = sorted(rows, key=key, reverse=dir == "desc")
        links = []
        for k, (lbl, kdir, _) in HISTORY_SORTS.items():
            ndir = ("asc" if dir == "desc" else "desc") if k == sort else kdir
            links.append((lbl, "/history?" + urlencode({"q": q, "sort": k, "dir": ndir}),
                          ("↓" if dir == "desc" else "↑") if k == sort else ""))
        return page(request, "history.html", "history", rows=rows, q=q, refreshed=refreshed, busy=busy,
                    refreshing=refreshing, refresh_error=refresh_error, refresh_error_at=refresh_error_at,
                    sort=sort, dir=dir, sort_links=links)

    def blocked_page(request: Request, q: str = "", status_code: int = 200, add_error: str = "", add_ref: str = ""):
        catalog = removed.load()
        with conn() as c:
            rows = {r["app_id"]: r for r in store.list_purchases(c)}
            no_license = store.no_license_ids(c)
            published = {a.app_id for a in store.list_apps(c)}
            refreshing, _, _ = store.refresh_status(c)
            busy = store.has_active_jobs(c)
        items = []
        for e in catalog:
            row = rows.get(e["id"])
            status = "owned" if row is not None else ("missing" if e["id"] in no_license else "unknown")
            items.append({"id": e["id"], "name": row["name"] if row is not None else e["name"],
                          "catalog_name": e["name"], "aliases": " ".join(e.get("aliases", [])),
                          "version": row["version"] if row is not None else "", "status": status,
                          "published": e["id"] in published})
        counts = {s: sum(1 for i in items if i["status"] == s) for s in ("owned", "missing", "unknown")}
        needle = q.strip().casefold()
        if needle:
            items = [i for i in items if needle in i["name"].casefold() or needle in i["catalog_name"].casefold()
                     or needle in i["aliases"]]
        order = {"owned": 0, "unknown": 1, "missing": 2}
        items.sort(key=lambda i: (order[i["status"]], i["name"].casefold()))
        return page(request, "blocked.html", "blocked", status_code, items=items, counts=counts, q=q,
                    checking=refreshing == "check_removed", busy=busy, add_error=add_error, add_ref=add_ref)

    @app.get("/blocked", response_class=HTMLResponse)
    def blocked(request: Request, q: str = ""):
        return blocked_page(request, q)

    @app.post("/blocked/check")
    def blocked_check():
        with conn() as c:
            store.enqueue_once(c, "check_removed", None, now())
        return back("/blocked")

    @app.post("/blocked/add", response_class=HTMLResponse)
    def add_by_link(request: Request, ref: str = Form("")):
        app_id = parse_app_id(ref)
        if app_id is None:
            return blocked_page(request, "", 400, add_ref=ref,
                                add_error="Не похоже на ссылку App Store или ID — пример: apps.apple.com/ru/app/…/id492224193")
        with conn() as c:
            store.add_manual(c, app_id, now())
        return back("/")

    @app.post("/history/refresh")
    def refresh():
        with conn() as c:
            store.enqueue_once(c, "refresh_history", None, now())
            store.enqueue_once(c, "check_removed", None, now())  # удалённые из App Store — по справочнику
        return back("/history")

    @app.post("/apps/{app_id}/publish")
    def publish(app_id: int):
        with conn() as c:
            store.publish(c, app_id, now())
        return back("/")

    @app.post("/apps/{app_id}/retry")
    def retry(app_id: int):
        with conn() as c:
            store.retry(c, app_id, now())
        return back("/")

    @app.post("/apps/{app_id}/unpublish")
    def unpublish(app_id: int):
        with conn() as c:
            store.unpublish(c, cfg.pub_root, app_id)
        return back("/")

    @app.get("/api/status")
    def api_status():
        with conn() as c:
            return JSONResponse({"busy": store.has_active_jobs(c), "session": store.get_state(c, "session"),
                                 "apps": {str(a.app_id): a.status for a in store.list_apps(c)}})

    @app.get("/apple", response_class=HTMLResponse)
    def apple(request: Request):
        return apple_page(request, 2 if flow.waiting_code else 1)

    @app.post("/apple/login", response_class=HTMLResponse)
    def apple_login(request: Request, email: str = Form(""), password: str = Form("")):
        email = email.strip()
        if not email or not password:
            return apple_page(request, 1, "Нужны Apple ID и пароль", 400, email=email)
        try:
            account = flow.start(email, password)
        except IpatoolError as e:
            return apple_page(request, 1, login_error_text(e), 400, email=email)
        if account is None:
            return apple_page(request, 2)
        with conn() as c:
            jobs.mark_ok(c, account, now())
        return back("/")

    @app.post("/apple/code", response_class=HTMLResponse)
    def apple_code(request: Request, code: str = Form("")):
        code = "".join(code.split())
        if not (code.isdigit() and len(code) == 6):  # опечатка в форме — не попытка входа, шаг не расходуем
            return apple_page(request, 2, "Код — 6 цифр", 400)
        try:
            account = flow.finish(code)
        except LoginExpired:
            return apple_page(request, 1, "Срок шага истёк — введите Apple ID и пароль заново", 400)
        except IpatoolError as e:
            return apple_page(request, 1, login_error_text(e), 400)
        with conn() as c:
            jobs.mark_ok(c, account, now())
        return back("/")

    return app


def main() -> FastAPI:
    """uvicorn --factory appshelf.web.app:main — окружение читается при старте службы, не при импорте."""
    cfg = from_env()
    return create_app(cfg, ipatool.from_config(cfg))
