"""Раздел Android панели (spec 2026-10-28 §5 «Раздел по умолчанию», §6, §8, §10): каталог Android-полки, «Добавить»
(RuStore, ссылка, свой файл), действия карточки. Две области с одними обработчиками (Scope): своя полка — участника
активной полки (request.state.user), публичная — одна на сервер: читают все вошедшие, меняет и задаёт PIN только
владелец. Пакет ищется только на полке области, чужой — 404. Скачивает обработчик заданий (android.fetch.run_job),
страница только ставит задания."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .. import apk, jobs, people, store
from ..android import db, fetch, net, pin
from ..config import GB
from ..nightly import SEP
from .admin import owner_only
from .common import back

OS_COOKIE = "appshelf_os"   # раздел, открытый последним: apple | android — для компьютера (spec §5)
SOURCES = {"rustore": "RuStore", "link": "сайт", "upload": "свой файл"}
NEW_VERSION_DAYS = 7
ARCHIVE_DOWN = "Архив недоступен — изменить полку сейчас нельзя"
TOO_BIG = "Файл больше 1 ГБ — такой не принимается"
MAX_URL = 2000
PIN_FORMAT = "PIN — 6 цифр, только цифры 0–9"
PIN_BUSY = "Идёт скачивание — попробуйте через минуту: PIN не изменён"


@dataclass(frozen=True)
class Scope:
    prefix: str    # адреса страниц и форм области
    nav: str       # вкладка каталога
    public: bool   # публичная полка: менять — только владельцу


MINE = Scope("/android", "a_catalog", False)
PUBLIC = Scope("/android/public", "a_public", True)


def is_owner(request: Request) -> bool:
    return any(u.role == people.OWNER for u in request.state.users)


def default_section(user_agent: str, cookie: str | None, explicit: str | None) -> str:
    """?os= → он; телефон Android → android, iPhone/iPad/iPod → apple; иначе раздел, открытый последним, или apple."""
    if explicit in ("apple", "android"):
        return explicit
    if "Android" in user_agent:
        return "android"
    if any(d in user_agent for d in ("iPhone", "iPad", "iPod")):
        return "apple"
    return cookie if cookie in ("apple", "android") else "apple"


def set_section(w, resp, section: str) -> None:
    resp.set_cookie(OS_COOKIE, section, max_age=10 * 365 * 86400, samesite="lax", secure=w.secure)


def nightly_part(result: str) -> str:
    return next((p for p in result.split(SEP) if p.startswith("Android:")), "")


def _job_view(j) -> dict:
    p = json.loads(j["payload"] or "{}")
    title = p.get("name") or (p.get("ref") if j["kind"] == "add" else "") or "Свой файл"
    return {"id": j["id"], "status": j["status"], "error": j["error"], "title": title}


def register_routes(app, w) -> None:
    _register_scope(app, w, MINE)
    public_page = _register_scope(app, w, PUBLIC)
    cfg, owner = w.cfg, [Depends(owner_only)]

    @app.post("/android/public/pin", response_class=HTMLResponse, dependencies=owner)
    def set_pin(request: Request, value: str = Form("", alias="pin")):
        """Задать или сменить PIN. Смена — сначала новый токен каталога публичной полки (spec §8): пересланные раньше
        ссылки на файлы перестают работать; не вышло — PIN прежний."""
        if not pin.valid_pin(value):
            return public_page(request, 400, PIN_FORMAT)
        error = None
        with w.conn() as c:
            shelf = db.public_shelf(c)
            try:
                if pin.is_set(c) and shelf is not None:
                    fetch.rotate_shelf(cfg, c, shelf.id)
            except jobs.AccountBusy:
                error = (409, PIN_BUSY)
            except jobs.ArchiveUnavailable:
                error = (503, ARCHIVE_DOWN)
            else:
                pin.set_pin(c, value, w.now())
        return public_page(request, *error) if error else back(PUBLIC.prefix)

    @app.post("/android/public/pin/reopen", dependencies=owner)
    def reopen(request: Request):
        with w.conn() as c:
            pin.reopen(c)
        return back(PUBLIC.prefix)

    @app.get("/api/android/status")
    def api_status(request: Request, public: int = 0):
        with w.conn() as c:
            shelf = db.public_shelf(c) if public else db.shelf_for_user(c, request.state.user.id)
            if shelf is None:
                return JSONResponse({"busy": False, "apps": {}})
            return JSONResponse({"busy": db.has_active(c, shelf.id),
                                 "apps": {a.package: a.status for a in db.list_apps(c, shelf.id)}})


def _register_scope(app, w, scope: Scope):
    """Маршруты области; возвращает страницу её каталога (для ответов с ошибкой)."""
    cfg, p = w.cfg, scope.prefix
    deps = [Depends(owner_only)] if scope.public else []  # POST публичной — только владельцу, иначе 403

    def not_found():
        return PlainTextResponse("not found", status_code=404)

    def shelf_of(c, request: Request):
        return db.public_shelf(c) if scope.public else db.shelf_for_user(c, request.state.user.id)

    def ensure(c, request: Request):
        """Полка появляется при первом добавлении."""
        if scope.public:
            return db.ensure_public_shelf(c, w.now())
        return db.ensure_shelf(c, request.state.user.id, w.now())

    def own(c, request: Request, package: str):
        """(полка области, карточка пакета на ней); полки или карточки нет — None на их месте."""
        shelf = shelf_of(c, request)
        return shelf, db.get_app(c, shelf.id, package) if shelf is not None else None

    def catalog_page(request: Request, status_code: int = 200, error: str = ""):
        edit = not scope.public or is_owner(request)
        guest = None
        with w.conn() as c:
            shelf = shelf_of(c, request)
            apps = db.list_apps(c, shelf.id) if shelf is not None else []
            cards = {a.package for a in apps}
            # задания без карточки: ссылка и свой файл, пока пакет не известен, и их ошибки — до «Скрыть»
            pending = [_job_view(j) for j in db.open_jobs(c, shelf.id) if j["package"] not in cards] if shelf else []
            busy = shelf is not None and db.has_active(c, shelf.id)
            nightly = (store.get_state(c, "nightly_last"), nightly_part(store.get_state(c, "nightly_result")))
            if scope.public and edit:  # блок «Гостевой доступ» — владельцу
                guest = {"url": f"{cfg.public_base}/p", "closed": pin.closed_until(c, w.now()),
                         "set_at": store.get_state(c, pin.SET_AT_KEY) if pin.is_set(c) else ""}
        banners = [error] if error else []
        try:
            free = jobs.free_space(w.env)
        except jobs.ArchiveUnavailable:
            banners.append("Архив недоступен: скачивание и установка не работают — смонтирован ли он?")
        else:
            if free < jobs.BANNER_FREE:
                banners.append(f"Мало места: свободно {free / GB:.1f} ГБ.")
        since = datetime.fromisoformat(w.now()) - timedelta(days=NEW_VERSION_DAYS)
        fresh = {a.package for a in apps
                 if a.previous is not None and datetime.fromisoformat(a.current["downloaded_at"]) >= since}
        resp = w.page(request, "android.html", scope.nav, status_code, apps=apps, jobs=pending, banners=banners,
                      busy=busy, nightly=nightly, fresh=fresh, statuses=json.dumps({a.package: a.status for a in apps}),
                      status_url="/api/android/status" + ("?public=1" if scope.public else ""), sources=SOURCES,
                      android_version=apk.android_version, apk_name=fetch.apk_name,
                      url=lambda d, f: fetch.file_url(cfg, shelf, d, f), prefix=p, public=scope.public, edit=edit,
                      guest=guest)
        set_section(w, resp, "android")
        return resp

    def add_page(request: Request, q: str = "", status_code: int = 200, error: str = "", link_url: str = ""):
        found, search_error = [], ""
        if q.strip():
            try:
                found = w.env.sources.search(q.strip())
            except (net.FetchError, net.SourceError) as e:
                search_error = f"Поиск в RuStore не удался: {e}"
        with w.conn() as c:
            shelf = shelf_of(c, request)
            on_shelf = {a.package for a in db.list_apps(c, shelf.id)} if shelf is not None else set()
        return w.page(request, "android_add.html", "a_public" if scope.public else "a_add", status_code, q=q,
                      found=found, search_error=search_error, on_shelf=on_shelf, error=error, link_url=link_url,
                      prefix=p, public=scope.public)

    def save_upload(file: UploadFile | None) -> tuple[Path | None, tuple[int, str] | None]:
        """Загрузка — потоком во временный файл (tmp/web, локально); (путь, None) или (None, (код, текст))."""
        if file is None:
            return None, (400, "Выберите файл APK")
        dest = cfg.tmp_dir / "web" / f"up-{secrets.token_hex(8)}.apk"
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        try:
            with open(dest, "wb") as out:
                while chunk := file.file.read(1 << 20):
                    n += len(chunk)
                    if n > net.MAX_APK:
                        break
                    out.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        if n > net.MAX_APK or n == 0:
            dest.unlink(missing_ok=True)
            return None, (413, TOO_BIG) if n else (400, "Файл пустой")
        return dest, None

    def requeue(c, shelf, app) -> None:
        """«Повторить» и «Проверить обновление»: есть текущая — проверка, нет — добавление заново; свой файл без
        источника и уже ждущее задание — ничего."""
        package = app["package"]
        if app["source"] == "upload" or db.active_for(c, shelf.id, package):
            return
        with store.tx(c):
            db.set_status(c, shelf.id, package, "queued")
            if db.has_current(c, shelf.id, package):
                db.enqueue(c, shelf.id, "check", w.now(), package=package)
            else:
                db.enqueue(c, shelf.id, "add", w.now(), package=package,
                           payload={"source": app["source"], "ref": app["source_ref"], "name": app["name"]})

    @app.get(p, response_class=HTMLResponse)
    def android_catalog(request: Request):
        return catalog_page(request)

    @app.get(f"{p}/add", response_class=HTMLResponse)
    def android_add(request: Request, q: str = ""):
        return add_page(request, q)

    @app.post(f"{p}/add/rustore", response_class=HTMLResponse, dependencies=deps)
    def add_rustore(request: Request, package: str = Form(""), name: str = Form("")):
        if not apk.PACKAGE_RE.match(package):
            return add_page(request, status_code=400, error="Не похоже на имя пакета Android")
        name = name.strip()[:200]
        with w.conn() as c:
            shelf = ensure(c, request)
            with store.tx(c):
                if db.get_app(c, shelf.id, package) is None:  # уже на полке — второго не ставим
                    db.put_app(c, shelf.id, package, name or package, "rustore", package, w.now())
                    db.enqueue(c, shelf.id, "add", w.now(), package=package,
                               payload={"source": "rustore", "ref": package, "name": name})
        return back(p)

    @app.post(f"{p}/add/link", response_class=HTMLResponse, dependencies=deps)
    def add_link(request: Request, url: str = Form(""), name: str = Form("")):
        url = url.strip()
        try:
            if len(url) > MAX_URL:
                raise net.FetchError("ссылка слишком длинная")
            net.check_url(url)
        except net.FetchError as e:
            return add_page(request, status_code=400, error=f"{url[:200]}: {e}", link_url=url[:MAX_URL])
        with w.conn() as c:
            shelf = ensure(c, request)
            db.enqueue(c, shelf.id, "add", w.now(), payload={"source": "link", "ref": url, "name": name.strip()[:200]})
        return back(p)

    @app.post(f"{p}/add/upload", response_class=HTMLResponse, dependencies=deps)
    def add_upload(request: Request, file: UploadFile | None = File(None), name: str = Form("")):
        path, err = save_upload(file)
        if err:
            return add_page(request, status_code=err[0], error=err[1])
        with w.conn() as c:
            shelf = ensure(c, request)
            db.enqueue(c, shelf.id, "upload", w.now(), payload={"path": str(path), "name": name.strip()[:200]})
        return back(p)

    @app.post(p + "/apps/{package}/upload", response_class=HTMLResponse, dependencies=deps)
    def upload_version(request: Request, package: str, file: UploadFile | None = File(None)):
        with w.conn() as c:
            shelf, app_row = own(c, request, package)
        if app_row is None:
            return not_found()
        path, err = save_upload(file)
        if err:
            return catalog_page(request, err[0], err[1])
        with w.conn() as c:
            db.enqueue(c, shelf.id, "upload", w.now(), package=package, payload={"path": str(path), "name": ""})
        return back(p)

    @app.post(p + "/apps/{package}/retry", dependencies=deps)
    @app.post(p + "/apps/{package}/check", dependencies=deps)
    def retry(request: Request, package: str):
        with w.conn() as c:
            shelf, app_row = own(c, request, package)
            if app_row is None:
                return not_found()
            requeue(c, shelf, app_row)
        return back(p)

    def change(request: Request, package: str, fn):
        """Действие, которое может удалить каталоги на полке: архив недоступен — 503 и ничего не тронуто.
        fn(c, root, shelf_id) → False — нет такой версии (404)."""
        try:
            jobs._archive(cfg)
        except jobs.ArchiveUnavailable:
            return catalog_page(request, 503, ARCHIVE_DOWN)
        with w.conn() as c:
            shelf, app_row = own(c, request, package)
            if app_row is None or not fn(c, fetch.shelf_root(cfg, shelf), shelf.id):
                return not_found()
        return back(p)

    @app.post(p + "/apps/{package}/remove", response_class=HTMLResponse, dependencies=deps)
    def remove(request: Request, package: str):
        def drop(c, root, sid):
            db.remove_app(c, root, sid, package)
            return True
        return change(request, package, drop)

    @app.post(p + "/apps/{package}/accept-signer", response_class=HTMLResponse, dependencies=deps)
    def accept_signer(request: Request, package: str):
        def accept(c, root, sid):
            db.accept_pending(c, root, sid, package)  # принимать нечего (уже приняли) — просто назад
            return True
        return change(request, package, accept)

    @app.post(p + "/apps/{package}/versions/{version_id}/pin", response_class=HTMLResponse, dependencies=deps)
    def pin_version(request: Request, package: str, version_id: int):
        return change(request, package, lambda c, root, sid: db.pin_version(c, root, sid, package, version_id))

    @app.post(p + "/apps/{package}/versions/{version_id}/unpin", response_class=HTMLResponse, dependencies=deps)
    def unpin_version(request: Request, package: str, version_id: int):
        return change(request, package, lambda c, root, sid: db.unpin_version(c, root, sid, package, version_id))

    @app.post(p + "/apps/{package}/source", response_class=HTMLResponse, dependencies=deps)
    def change_source(request: Request, package: str, source: str = Form(""), ref: str = Form("")):
        """Сменить источник: версии и закрепления остаются, первая версия из нового источника — по правилу подписи."""
        with w.conn() as c:
            shelf, app_row = own(c, request, package)
        if app_row is None:
            return not_found()
        ref = ref.strip()
        if source == "rustore":
            ref = package
        elif source == "link":
            try:
                if len(ref) > MAX_URL:
                    raise net.FetchError("ссылка слишком длинная")
                net.check_url(ref)
            except net.FetchError as e:
                return catalog_page(request, 400, f"{ref[:200]}: {e}")
        else:
            return catalog_page(request, 400, "Источник — RuStore или ссылка на APK")
        with w.conn() as c:
            db.set_source(c, shelf.id, package, source, ref)
            requeue(c, shelf, db.get_app(c, shelf.id, package))
        return back(p)

    @app.post(p + "/jobs/{job_id}/dismiss", dependencies=deps)
    def dismiss(request: Request, job_id: int):
        with w.conn() as c:
            shelf = shelf_of(c, request)
            if shelf is not None:
                db.dismiss_job(c, shelf.id, job_id)
        return back(p)

    return catalog_page
