"""Страницы appshelf (spec 2026-10-05 §8, 2026-10-08 §10). Вход — Apple ID (signin.py); cookie человека проверяет
само приложение (webauth: к сокету ходят все процессы www-data). Страницы полки — активного Apple ID.
Слушает только unix-сокет /run/appshelf/web.sock, один процесс uvicorn."""
from __future__ import annotations

import json
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import ipatool, jobs, notify, people, removed, store, webauth
from ..config import GB, Config, from_env
from . import accounts, admin, pwa, signin
from .common import ACCT_COOKIE, THEMES, WebCtx, back, referer_path
from .login import Limiter, LoginFlow

PUBLIC = ("/healthz", "/login", "/login/code")
PUBLIC_PREFIXES = ("/pwa/", "/join/", "/l/")


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


def create_app(cfg: Config, tools, *, new_tool=None, now=jobs.now_iso, clock=time.monotonic, send=notify.send_mail,
               disk_free=jobs.free_bytes, start_worker: bool = True) -> FastAPI:
    """tools(acct) — ipatool Apple ID; new_tool(home, device_mac) — ipatool первого входа нового Apple ID."""
    env = jobs.Env(cfg, tools, now=now, send=send, disk_free=disk_free)
    flow = LoginFlow(clock=clock)
    w = WebCtx(cfg, env, new_tool or (lambda home, mac: ipatool.for_new(cfg, home, mac)), flow, Limiter(clock), now)
    worker = jobs.Worker(env, lambda: store.connect(cfg.db, cfg.owner_email), purge=flow.purge)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield

    app = FastAPI(title="appshelf", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.w, app.state.worker = w, worker

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # cookie браузер подставляет и в межсайтовые запросы: POST сверяем по Origin с хостом запроса
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

    def who(request: Request):
        """(человек, его Apple ID, активный) по cookie или None. Активный — из cookie выбора полки, если этот Apple ID
        принадлежит человеку, иначе первый."""
        with w.conn() as c:
            user = webauth.check_cookie(w.cookie_key(), request.cookies.get(webauth.COOKIE, ""), w.epoch(),
                                        lambda uid: people.get_user(c, uid))
            accounts = people.accounts_of(c, user.id) if user is not None else []
        if not accounts:
            return None
        want = request.cookies.get(ACCT_COOKIE, "")
        return user, accounts, next((a for a in accounts if str(a.id) == want), accounts[0])

    @app.middleware("http")
    async def auth(request: Request, call_next):
        # добавлен последним — выполняется первым. Без входа: /healthz (мониторинг), /pwa/ (манифест и иконку iOS
        # берёт без cookie), сам вход (/login, /join/, /l/).
        path = request.url.path
        if path in PUBLIC or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        found = await run_in_threadpool(who, request)
        if found is None:
            if request.method == "GET" and not path.startswith("/api/"):
                target = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)
            # без WWW-Authenticate: окно Basic Auth в браузере не нужно, вход — формой
            return PlainTextResponse("401: нужен вход — /login", status_code=401)
        request.state.user, request.state.accounts, request.state.acct = found
        return await call_next(request)

    def not_found():
        return PlainTextResponse("not found", status_code=404)

    @app.get("/healthz")
    def healthz():
        with w.conn() as c:
            c.execute("SELECT 1")
        return PlainTextResponse("ok")

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
        a = request.state.acct
        with w.conn() as c:
            apps = store.list_apps(c, a.id)
            nightly = (store.get_state(c, "nightly_last"), store.get_state(c, "nightly_result"))
            busy = store.has_active_jobs(c, a.id)
        banners = []
        try:
            free = jobs.free_space(env)
        except jobs.ArchiveUnavailable:
            banners.append(f"Архив недоступен ({cfg.archive}): установка и скачивание не работают — "
                           "смонтирован ли он?")
        else:
            if free < jobs.BANNER_FREE:
                banners.append(f"Мало места: свободно {free / GB:.1f} ГБ.")
        statuses = json.dumps({str(x.app_id): x.status for x in apps})
        return w.page(request, "catalog.html", "catalog", apps=apps, banners=banners, nightly=nightly, busy=busy,
                      statuses=statuses, public_url=lambda d, f: cfg.shelf_url(a, d, f),
                      relogin=signin.relogin_query(a, "/"))

    @app.get("/theme")
    def theme(request: Request, set: str = ""):
        """Тема: light / dark / auto (как в системе). Возврат на ту же страницу."""
        resp = back(referer_path(request))
        if set in THEMES:
            resp.set_cookie("theme", set, max_age=10 * 365 * 86400, samesite="lax", secure=True)
        else:
            resp.delete_cookie("theme")
        return resp

    def matches(needle: str, alias: dict, app_id: int, *texts: str) -> bool:
        return any(needle in t.casefold() for t in texts) or needle in alias.get(app_id, "")

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, q: str = "", sort: str = "name", dir: str = ""):
        aid = request.state.acct.id
        with w.conn() as c:
            rows = store.list_purchases(c, aid)
            refreshed = store.history_refreshed_at(c, aid)
            refreshing, refresh_error, refresh_error_at = store.refresh_status(c, aid)
            busy = store.has_active_jobs(c, aid)
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
        return w.page(request, "history.html", "history", rows=rows, q=q, refreshed=refreshed, busy=busy,
                      refreshing=refreshing, refresh_error=refresh_error, refresh_error_at=refresh_error_at,
                      sort=sort, dir=dir, sort_links=links)

    def blocked_page(request: Request, q: str = "", status_code: int = 200, add_error: str = "", add_ref: str = ""):
        aid = request.state.acct.id
        catalog = removed.load()
        with w.conn() as c:
            rows = {r["app_id"]: r for r in store.list_purchases(c, aid)}
            no_license = store.no_license_ids(c, aid)
            published = {x.app_id for x in store.list_apps(c, aid)}
            refreshing, _, _ = store.refresh_status(c, aid)
            busy = store.has_active_jobs(c, aid)
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
        return w.page(request, "blocked.html", "blocked", status_code, items=items, counts=counts, q=q,
                      checking=refreshing == "check_removed", busy=busy, add_error=add_error, add_ref=add_ref)

    @app.get("/blocked", response_class=HTMLResponse)
    def blocked(request: Request, q: str = ""):
        return blocked_page(request, q)

    @app.post("/blocked/check")
    def blocked_check(request: Request):
        with w.conn() as c:
            store.enqueue_once(c, request.state.acct.id, "check_removed", None, now())
        return back("/blocked")

    @app.post("/blocked/add", response_class=HTMLResponse)
    def add_by_link(request: Request, ref: str = Form("")):
        app_id = parse_app_id(ref)
        if app_id is None:
            return blocked_page(request, "", 400, add_ref=ref,
                                add_error="Не похоже на ссылку App Store или ID — пример: apps.apple.com/ru/app/…/id492224193")
        with w.conn() as c:
            store.add_manual(c, request.state.acct.id, app_id, now())
        return back("/")

    @app.post("/history/refresh")
    def refresh(request: Request):
        aid = request.state.acct.id
        with w.conn() as c:
            store.enqueue_once(c, aid, "refresh_history", None, now())
            store.enqueue_once(c, aid, "check_removed", None, now())  # удалённые из App Store — по справочнику
        return back("/history")

    @app.post("/apps/{app_id}/publish")
    def publish(request: Request, app_id: int):
        aid = request.state.acct.id
        with w.conn() as c:
            if not store.has_purchase(c, aid, app_id):  # чужое или несуществующее — как нет
                return not_found()
            store.publish(c, aid, app_id, now())
        return back("/")

    @app.post("/apps/{app_id}/retry")
    def retry(request: Request, app_id: int):
        aid = request.state.acct.id
        with w.conn() as c:
            if not store.has_app(c, aid, app_id):
                return not_found()
            store.retry(c, aid, app_id, now())
        return back("/")

    @app.post("/apps/{app_id}/unpublish")
    def unpublish(request: Request, app_id: int):
        a = request.state.acct
        with w.conn() as c:
            if not store.has_app(c, a.id, app_id):
                return not_found()
            store.unpublish(c, a.id, cfg.shelf_root(a), app_id)
        return back("/")

    @app.get("/api/status")
    def api_status(request: Request):
        aid = request.state.acct.id
        with w.conn() as c:
            return JSONResponse({"busy": store.has_active_jobs(c, aid), "session": people.get_account(c, aid).session,
                                 "apps": {str(x.app_id): x.status for x in store.list_apps(c, aid)}})

    signin.register_routes(app, w)
    accounts.register_routes(app, w)
    admin.register_routes(app, w)
    return app


def main() -> FastAPI:
    """uvicorn --factory appshelf.web.app:main — окружение читается при старте службы, не при импорте. Переход схемы —
    тоже при старте: данные версии 1 без APPSHELF_OWNER не дают службе подняться (MigrationError в журнале)."""
    cfg = from_env()
    store.connect(cfg.db, cfg.owner_email).close()
    return create_app(cfg, lambda acct: ipatool.for_account(cfg, acct))
