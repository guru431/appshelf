"""Раздел Android панели (spec 2026-10-28 §5 «Раздел по умолчанию», §6, §8, §10): каталог Android-полки, «Добавить»
(RuStore, Google Play, ссылка, свой файл), действия карточки. Две области с одними обработчиками (Scope): своя полка — активного
участника раздела (request.state.who, выбор — /who/<id>), публичная — одна на сервер: читают все вошедшие, меняет и задаёт PIN только
владелец. Пакет ищется только на полке области, чужой — 404. Скачивает обработчик заданий (android.fetch.run_job),
страница проверяет доступ и состав доставки Google Play перед постановкой задания."""
from __future__ import annotations

import io
import hashlib
import json
import re
import secrets
import urllib.parse
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import qrcode
from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse

from .. import apk, ipatool, jobs, notify, people, store
from ..android import db, fdroid_index, fetch, github, links, net, pin, play
from ..config import GB
from ..nightly import SEP
from .admin import owner_only
from .common import TEMPLATES, back, filter_shelf, mutation_url

OS_COOKIE = "appshelf_os"   # раздел, открытый последним: apple | android — для компьютера (spec §5)
SOURCES = {"rustore": "RuStore", "link": "сайт", "upload": "свой файл", "play": "Google Play",
           "appgallery": "AppGallery", "fdroid": "F-Droid", "fdroid_archive": "Архив F-Droid",
           "izzy": "F-Droid · IzzyOnDroid", "github": "GitHub", "apkpure": "APKPure", "fourpda": "4PDA"}
NEW_VERSION_DAYS = 7
ARCHIVE_DOWN = "Архив недоступен — изменить полку сейчас нельзя"
TOO_BIG = "Файл больше 1 ГБ — такой не принимается"
MAX_URL = 2000
PIN_FORMAT = "PIN — 6 цифр, только цифры 0–9"
PIN_BUSY = "Идёт скачивание — попробуйте через минуту: PIN не изменён"
NEED_PLAY = "Сначала подключите Google-аккаунт к Google Play на странице «Аккаунты»"
PLAY_PACKAGE = "Не похоже на ссылку Google Play или имя пакета"
NO_PLAY_SOURCE = "Источником карточки Google Play не ставится — снимите её и добавьте приложение из Google Play"
BY_PACKAGE = ("rustore", "appgallery", "fdroid", "fdroid_archive", "izzy", "apkpure")
NOTICES = {fetch.SIGNER_DIFFERS: "подпись не как в RuStore", fetch.NOT_VERIFIED: "подпись не сверена"}
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")   # токен каталога полки (db.ensure_shelf)


@dataclass(frozen=True)
class Scope:
    prefix: str    # адреса страниц и форм области
    nav: str       # вкладка каталога
    public: bool   # публичная полка: менять — только владельцу


MINE = Scope("/android", "a_catalog", False)
PUBLIC = Scope("/android/public", "a_public", True)


def is_owner(request: Request) -> bool:
    return any(u.role == people.OWNER for u in request.state.users)


def _owner_shelf(c, request: Request):
    owner = next((u for u in request.state.users if u.role == people.OWNER), None)
    return db.shelf_for_user(c, owner.id) if owner is not None else None


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


def play_package(text: str) -> str:
    """Ссылка Google Play (https://play.google.com/store/apps/details?id=<пакет>) или само имя пакета → пакет; не
    то — ""."""
    text = text.strip()
    if "://" in text:
        u = urllib.parse.urlsplit(text)
        if u.scheme != "https" or u.hostname != "play.google.com" or u.path.rstrip("/") != "/store/apps/details":
            return ""
        text = urllib.parse.parse_qs(u.query).get("id", [""])[0]
    return text if apk.PACKAGE_RE.match(text) else ""


def nightly_part(result: str) -> str:
    return next((p for p in result.split(SEP) if p.startswith("Android:")), "")


def _job_view(j) -> dict:
    p = json.loads(j["payload"] or "{}")
    title = p.get("name") or (p.get("ref") if j["kind"] == "add" else "") or "Свой файл"
    return {"id": j["id"], "status": j["status"], "error": j["error"], "title": title,
            "query": p.get("ref", "") if j["kind"] == "add" else "",
            "split_only": p.get("split_only", False), "alternatives": p.get("alternatives", [])}


def _fresh(apps, now: str) -> set[str]:
    """Метка «новая версия»: текущая скачана за NEW_VERSION_DAYS и есть предыдущая."""
    since = datetime.fromisoformat(now) - timedelta(days=NEW_VERSION_DAYS)
    return {a.package for a in apps
            if a.previous is not None and datetime.fromisoformat(a.current["downloaded_at"]) >= since}


def _lock_mail(until: str, base: str) -> str:
    return (f"За сутки на публичной Android-полке введено {pin.ALL_LIMIT} неверных PIN с разных адресов: ввод PIN "
            f"закрыт для новых гостей до {until[:16].replace('T', ' ')} UTC. Гости, уже вводившие PIN, полку видят.\n\n"
            f"Открыть сразу — «Открыть ввод PIN» на публичной полке: Android → меню «Полка» → «Публичная» ({base}/android/public) или смена "
            f"PIN там же; если PIN мог утечь — смените его.")


@lru_cache(maxsize=128)
def _qr_png(url: str) -> bytes:
    data = io.BytesIO()
    qrcode.make(url, box_size=4, border=4).save(data, format="PNG")
    return data.getvalue()


def register_routes(app, w) -> None:
    _register_scope(app, w, MINE)
    public_page = _register_scope(app, w, PUBLIC)
    cfg, owner = w.cfg, [Depends(owner_only)]

    def copy_preview(c, source, target, package):
        original = db.get_app(c, source.id, package)
        current = db.get_app(c, target.id, package) if target else None
        incoming = [dict(v) for v in c.execute("SELECT * FROM android_versions WHERE shelf_id=? AND package=? "
                                              "AND role!='pending' ORDER BY (role='current') DESC,version_code,id",
                                              (source.id, package))]
        saved = [dict(v) for v in c.execute("SELECT * FROM android_versions WHERE shelf_id=? AND package=? ORDER BY id",
                                           (target.id, package))] if target else []
        hashes = {v['sha256'] for v in saved}
        pending = any(v['role'] == 'pending' for v in saved)
        signer = current['signer'] if current else ''
        rows, rejected = [], []
        for v in incoming:
            denied = target is not None and db.rejected_apk(c, target.id, package, v['sha256'])
            rejected.append(denied)
            different = bool(signer and signer != v['signer'])
            state = ('Уже есть по SHA-256' if v['sha256'] in hashes else 'Ранее отклонена' if denied else
                     'Ждёт решения по существующей подписи' if different and pending else
                     'Проверка новой подписи; возможно потребуется решение' if different else 'Будет добавлена')
            add = v['sha256'] not in hashes and not denied and not (different and pending)
            rows.append({'v': v, 'state': state, 'add': add})
            if add and different:
                pending = True
            if add and not signer:
                signer = v['signer']
        snapshot = {'source': dict(original), 'target': dict(current) if current else None,
                    'incoming': incoming, 'saved': saved, 'rejected': rejected}
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        return {'original': original, 'target': current, 'rows': rows, 'count': sum(r['add'] for r in rows),
                'snapshot': digest, 'saved': saved}

    def preview_page(request, package, error='', status=200):
        with w.conn() as c:
            c.execute("BEGIN")  # One read snapshot for the comparison, without creating or modifying shelves.
            source = _owner_shelf(c, request)
            original = db.get_app(c, source.id, package) if source else None
            if original is None:
                return PlainTextResponse('not found', status_code=404)
            plan = copy_preview(c, source, db.public_shelf(c), package)
        return w.page(request, 'public_copy.html', 'a_public', status, package=package, plan=plan,
                      error=error, sources=SOURCES)

    @app.get('/android/public/add/from-shelf', dependencies=owner)
    def preview_from_shelf(request: Request, package: str = ''):
        return preview_page(request, package)

    @app.post("/android/public/add/from-shelf", dependencies=owner)
    def add_from_shelf(request: Request, package: str = Form(""), sync_history: str = Form(""), snapshot: str = Form("")):
        with w.conn() as c, store.tx(c):
            source = db.shelf_for_user(c, request.state.who.id) if request.state.who.role == people.OWNER else None
            original = db.get_app(c, source.id, package) if source is not None else None
            if original is None:
                return PlainTextResponse("not found", status_code=404)
            if original["paid"]:
                return public_page(request, 400, "Купленное в Google Play нельзя добавлять на публичную полку")
            if db.current_version(c, source.id, package) is None:
                return public_page(request, 409, "Сначала дождитесь скачивания приложения на свою полку")
            target = db.ensure_public_shelf(c, w.now())
            if snapshot and copy_preview(c, source, target, package)['snapshot'] != snapshot:
                return preview_page(request, package, 'Версии изменились. Проверьте сравнение и подтвердите заново', 409)
            existing = db.get_app(c, target.id, package)
            if existing is None:
                db.put_app(c, target.id, package, original["name"], original["source"], original["source_ref"],
                           w.now(), google_account_id=original["google_account_id"])
                c.execute("UPDATE android_apps SET source_page=? WHERE shelf_id=? AND package=?",
                          (original["source_page"], target.id, package))
            if (existing is None or sync_history == "1") and not db.active_for(c, target.id, package):
                db.enqueue(c, target.id, "copy", w.now(), package=package, payload={"from_shelf": source.id})
        return back(PUBLIC.prefix)

    @app.post("/android/public/pin", response_class=HTMLResponse, dependencies=owner)
    def set_pin(request: Request, value: str = Form("", alias="pin")):
        """PIN и намерение сменить токен каталога — одной транзакцией; перенос файлов после COMMIT.
        Занята полка или архив недоступен — PIN прежний, прерванный перенос доделает запуск службы."""
        if not pin.valid_pin(value):
            return public_page(request, 400, PIN_FORMAT)
        error = None
        with w.conn() as c:
            shelf = db.public_shelf(c)
            try:
                if pin.is_set(c) and shelf is not None:
                    fetch.rotate_shelf(cfg, c, shelf.id, transaction=pin.setting(c, value, w.now()))
                else:
                    pin.set_pin(c, value, w.now())
            except jobs.AccountBusy:
                error = (409, PIN_BUSY)
            except jobs.ArchiveUnavailable:
                error = (503, ARCHIVE_DOWN)
        return public_page(request, *error) if error else back(PUBLIC.prefix)

    @app.post("/android/public/pin/reopen", dependencies=owner)
    def reopen(request: Request):
        with w.conn() as c:
            pin.reopen(c)
        return back(PUBLIC.prefix)

    def guest_page(request: Request, status_code: int = 200, error: str = ""):
        """Страница гостя (spec §8): вне входа в панель — middleware её пропускает, request.state пуст. Список видят
        гость с действующей cookie и вошедший участник; остальным — форма PIN, пока ввод не закрыт всем."""
        with w.conn() as c:
            shelf = db.public_shelf(c)
            if shelf is None or not pin.is_set(c):
                return w.page(request, "guest.html", "guest", status_code, configured=False)
            if shelf.shelf_next:
                return PlainTextResponse("Полка временно недоступна, попробуйте позже", status_code=503)
            guest = pin.guest_ok(w.key, request.cookies.get(pin.COOKIE, ""), pin.epoch(c), w.now())
            closed = pin.closed_until(c, w.now())
            apps = [a for a in db.list_apps(c, shelf.id) if a.current is not None]
        allowed = guest or w.person(request) is not None
        fresh = _fresh(apps, w.now())
        apps, filters = filter_shelf(apps, request, fresh, android=True)
        return w.page(request, "guest.html", "guest", status_code, configured=True, allowed=allowed, **filters,
                      closed=closed, error=error, apps=apps if allowed else [], fresh=fresh,
                      sources=SOURCES, notices=NOTICES, android_version=apk.android_version,
                      apk_name=fetch.apk_name,
                      app_url=lambda package: fetch.app_url(cfg, shelf, package),
                      url=lambda d, f: fetch.file_url(cfg, shelf, d, f))

    @app.get("/p", response_class=HTMLResponse)
    def guest(request: Request):
        return guest_page(request)

    @app.post("/p", response_class=HTMLResponse)
    def guest_pin(request: Request, value: str = Form("", alias="pin")):
        key = pin.client_key(request.headers.get("x-forwarded-for"), request.client.host if request.client else None)
        with w.conn() as c:
            shelf = db.public_shelf(c)
            if shelf is None or shelf.shelf_next or not pin.is_set(c):
                return guest_page(request)
            epoch = pin.epoch(c)  # до проверки: PIN сменили посередине — cookie прежней эпохи, гость введёт заново
            a = pin.try_pin(c, key, value, w.now())
            if a.just_closed:
                notify.once(c, pin.MAIL_KEY, "appshelf: ввод PIN публичной полки закрыт",
                            _lock_mail(a.until, cfg.public_base), w.env.send, cfg.mail_to, w.now())
        if a.result == "ok":
            resp = back("/p")
            w.set_cookie(resp, pin.COOKIE, pin.guest_cookie(w.key, epoch, w.now()), pin.GUEST_DAYS * 86400)
            return resp
        if a.result == "wrong":
            return guest_page(request, 401, f"Неверный PIN, осталось попыток: {a.left}")
        if a.result == "format":
            return guest_page(request, 400, "PIN — 6 цифр")
        if a.result == "ip_blocked":
            left = datetime.fromisoformat(a.until) - datetime.fromisoformat(w.now())
            minutes = max(1, -(-int(left.total_seconds()) // 60))
            return guest_page(request, 429, f"Слишком много попыток с этого адреса, попробуйте через {minutes} мин")
        return guest_page(request, 429)  # closed: страница сама скажет «ввод временно закрыт»

    @app.get("/apk/{token}/{file}")
    def app_link(token: str, file: str, version: int = 0):
        """Ссылка на приложение: последняя версия либо явно выбранная сохранённая. Без входа,
        как сами файлы /d/: секрет — токен каталога полки, его смена (rotate-token, смена PIN) гасит и ссылку."""
        package = file.removesuffix(".apk")
        if not file.endswith(".apk") or not apk.PACKAGE_RE.match(package) or not TOKEN_RE.match(token):
            return PlainTextResponse("not found", status_code=404)
        with w.conn() as c:
            shelf = db.shelf_by_token(c, token)
            v = db.downloadable_version(c, shelf.id, package, version) if shelf is not None and not shelf.shelf_next else None
        if v is None:
            return PlainTextResponse("not found", status_code=404)
        return RedirectResponse(fetch.file_url(cfg, shelf, v["dir"], fetch.apk_name(package, v["version_name"])),
                                status_code=302)

    @app.get("/apk/{token}/{file}/qr.png")
    def app_qr(token: str, file: str, version: int = 0):
        package = file.removesuffix(".apk")
        if not file.endswith(".apk") or not apk.PACKAGE_RE.match(package) or not TOKEN_RE.match(token):
            return PlainTextResponse("not found", status_code=404)
        with w.conn() as c:
            shelf = db.shelf_by_token(c, token)
            if (shelf is None or shelf.shelf_next
                    or db.downloadable_version(c, shelf.id, package, version) is None):
                return PlainTextResponse("not found", status_code=404)
        link = fetch.app_url(cfg, shelf, package) + (f"?version={version}" if version else "")
        return Response(_qr_png(link), media_type="image/png",
                        headers={"Cache-Control": "private, no-store"})

    @app.get("/api/android/status")
    def api_status(request: Request, public: int = 0):
        with w.conn() as c:
            shelf = db.public_shelf(c) if public else db.shelf_for_user(c, request.state.who.id)
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
        return db.public_shelf(c) if scope.public else db.shelf_for_user(c, request.state.who.id)

    def ensure(c, request: Request):
        """Полка появляется при первом добавлении."""
        if scope.public:
            return db.ensure_public_shelf(c, w.now())
        return db.ensure_shelf(c, request.state.who.id, w.now())

    from . import uploads as upload_web
    upload_web.register(app, w, scope, shelf_of, ensure)

    def google_accounts(c, request: Request) -> list:
        """Google-аккаунты, которыми качают из Play на полку области: своя — активного участника, публичная —
        владельца (spec §6.4)."""
        if not scope.public:
            return people.google_of(c, request.state.who.id)
        return [g for u in people.list_users(c) if u.role == people.OWNER for g in people.google_of(c, u.id)]

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
            pending = [_job_view(j) for j in db.open_jobs(c, shelf.id)
                       if j["package"] not in cards or json.loads(j["payload"] or "{}").get("split_only")] if shelf else []
            busy = shelf is not None and db.has_active(c, shelf.id)
            waiting = {a.package: store.android_waiting_reason(c, shelf.id, a.package)
                       for a in apps if a.status == "queued"} if shelf else {}
            nightly = (store.get_state(c, "nightly_last"), nightly_part(store.get_state(c, "nightly_result")))
            if scope.public and edit:  # блок «Гостевой доступ» — владельцу
                guest = {"url": f"{cfg.public_base}/p", "closed": pin.closed_until(c, w.now()),
                         "set_at": store.get_state(c, pin.SET_AT_KEY) if pin.is_set(c) else ""}
            used = {r[0] for r in c.execute("SELECT google_account_id FROM android_apps WHERE shelf_id=? "
                                           "AND source='play'", (shelf.id,))} if shelf else set()
            expired = [g.email for g in google_accounts(c, request)
                       if g.play == "expired" and (not scope.public or g.id in used)] if edit else []
            can_publish = not scope.public and request.state.who.role == people.OWNER
            public_shelf = db.public_shelf(c) if can_publish else None
            public_packages = {a.package for a in db.list_apps(c, public_shelf.id)} if public_shelf else set()
        banners = [error] if error else []
        banners += [f"Подключение Google Play {email} истекло — подключите заново на странице «Аккаунты»"
                    for email in expired]
        try:
            free = jobs.free_space(w.env)
        except jobs.ArchiveUnavailable:
            banners.append("Архив недоступен: скачивание и установка не работают — смонтирован ли он?")
        else:
            if free < jobs.BANNER_FREE:
                banners.append(f"Мало места: свободно {free / GB:.1f} ГБ.")
        fresh = _fresh(apps, w.now())
        statuses = json.dumps({a.package: a.status for a in apps})
        apps, filters = filter_shelf(apps, request, fresh, android=True)
        resp = w.page(request, "android.html", scope.nav, status_code, apps=apps, jobs=pending, banners=banners, **filters,
                      busy=busy, nightly=nightly, fresh=fresh, statuses=statuses, waiting=waiting,
                      status_url=f"/api/android/status?person={request.state.who.id}" + ("&public=1" if scope.public else ""), sources=SOURCES,
                      notices=NOTICES, source_page=links.page_url,
                      android_version=apk.android_version, apk_name=fetch.apk_name,
                      url=lambda d, f: fetch.file_url(cfg, shelf, d, f),
                      app_url=lambda package: fetch.app_url(cfg, shelf, package), prefix=p, public=scope.public,
                      edit=edit, guest=guest, can_publish=can_publish, public_packages=public_packages)
        set_section(w, resp, "android")
        return resp

    def add_page(request: Request, q: str = "", status_code: int = 200, error: str = "", link_url: str = ""):
        found, search_errors = [], []
        q = q.strip()[:200]
        with w.conn() as c:
            shelf = shelf_of(c, request)
            on_shelf = {a.package for a in db.list_apps(c, shelf.id)} if shelf is not None else set()
            accounts = google_accounts(c, request)
            mine = _owner_shelf(c, request) if scope.public else None
            own_apps = [a for a in db.list_apps(c, mine.id) if a.current is not None] if mine else []
        play_accounts = [g for g in accounts if g.play == "ok"]
        return w.page(request, "android_add.html", "a_public" if scope.public else "a_add", status_code, q=q,
                      found=found, search_errors=search_errors, sources=SOURCES,
                      on_shelf=on_shelf, error=error, link_url=link_url,
                      prefix=p, public=scope.public, play_accounts=play_accounts, has_google=bool(accounts),
                      own_apps=own_apps)

    @app.get(f"{p}/search", dependencies=deps)
    def search_stream(request: Request, q: str = ""):
        """SSE: состояние источника отправляется до его запроса, результаты — по мере получения."""
        q = q.strip()[:200]
        if not q:
            return PlainTextResponse("Введите название приложения", status_code=400)
        with w.conn() as c:
            shelf = shelf_of(c, request)
            apps = db.list_apps(c, shelf.id) if shelf else []
            on_shelf = {a.package for a in apps}
            accounts = [g for g in google_accounts(c, request) if g.play == "ok"]
            split_packages = {a.package for a in apps if "набором файлов" in a.last_error}
            if shelf:
                for job in db.open_jobs(c, shelf.id):
                    ref = json.loads(job["payload"] or "{}").get("ref", "")
                    if "набором файлов" in job["error"] and apk.PACKAGE_RE.fullmatch(ref):
                        split_packages.add(ref)

        def message(kind, data):
            return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def stream():
            previous = None
            try:
                for update in w.env.sources.search_events(q, accounts=accounts, split_packages=split_packages,
                                                         user_id=None if scope.public else request.state.who.id):
                    if update.kind == "status":
                        yield message("status", {"source": update.source, "state": update.state,
                                                 "detail": update.detail})
                    if update.result is not None and (update.result != previous or update.kind == "done"):
                        result = update.result
                        html = TEMPLATES.get_template("android_search_results.html").render(
                            found=result.options, search_errors=result.errors, on_shelf=on_shelf,
                            sources=SOURCES, prefix=p, complete=update.kind == "done",
                            post_url=lambda path: mutation_url(path, request.state.acct, request.state.who))
                        yield message(update.kind, {"html": html, "count": len(result.options),
                                                   "errors": len(result.errors)})
                        previous = result
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Android source search failed")
                yield message("failed", {"message": "Поиск прервался — повторите запрос. Найденные варианты сохранены."})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

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

    def requeue(c, shelf, app, ack: bool = False, previous: list | None = None) -> None:
        """«Повторить» и «Проверить обновление»: есть текущая — проверка, нет — добавление заново (APKPure — с
        галочкой ack из «Сменить источник»); свой файл без источника и уже ждущее задание — ничего. previous —
        прежние (source, ref, etag) после «Сменить источник»: проверка скачиванием, отказ возвращает их."""
        package = app["package"]
        with nullcontext() if c.in_transaction else store.tx(c):
            if app["source"] == "upload" or db.active_for(c, shelf.id, package):
                return
            db.set_status(c, shelf.id, package, "queued")
            if db.has_current(c, shelf.id, package):
                db.enqueue(c, shelf.id, "check", w.now(), package=package,
                           payload={"previous": previous} if previous else None)
            else:
                payload = {"source": app["source"], "ref": app["source_ref"], "name": app["name"],
                           "source_page": app["source_page"]}
                if app["source"] == "apkpure":
                    payload["ack"] = ack
                db.enqueue(c, shelf.id, "add", w.now(), package=package, payload=payload)

    @app.get(p, response_class=HTMLResponse)
    def android_catalog(request: Request):
        return catalog_page(request)

    @app.get(p + "/apps/{package}/source/{action}")
    def open_source(request: Request, package: str, action: str):
        with w.conn() as c:
            _, original = own(c, request, package)
        if original is None or action not in ("download", "page"):
            return not_found()
        source, ref = original["source"], original["source_ref"]
        try:
            if action == "download":
                if source in ("upload", "play"):
                    return not_found()
                target = w.env.sources.download_url(source, ref)
            else:
                target = links.page_url(source, ref, original["source_page"])
                if not target and source == "appgallery":
                    target = links.page_url(source, w.env.sources._appgallery(ref).app_id)
                if not target:
                    return not_found()
            net.check_url(target)
        except (net.SourceError, net.FetchError) as e:
            return catalog_page(request, 502, f"Не удалось открыть источник: {e}")
        return RedirectResponse(target, status_code=302,
                                headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})

    @app.get(f"{p}/add", response_class=HTMLResponse, dependencies=deps)
    def android_add(request: Request, q: str = ""):
        return add_page(request, q)

    def add_package(request: Request, source: str, package: str, name: str, payload: dict,
                    account: int | None = None, ref: str | None = None, paid: bool = False):
        """Карточка пакета и задание add (payload — к source и ref); пакет уже на полке — второго не ставим."""
        with w.conn() as c:
            with store.tx(c):
                shelf = ensure(c, request)
                if db.get_app(c, shelf.id, package) is None:
                    db.put_app(c, shelf.id, package, name or package, source, ref or package, w.now(),
                               google_account_id=account, paid=paid)
                    db.enqueue(c, shelf.id, "add", w.now(), package=package,
                               payload={"source": source, "ref": ref or package, **payload})
        return back(p)

    def add_checked_play(request: Request, package: str, acct, link_url: str = ""):
        """Все способы добавления из Play проверяют доступ и состав APK до записи карточки и задания."""
        with w.conn() as c:
            _, existing = own(c, request, package)
        if existing is not None:
            return back(p)
        try:
            checked = w.env.sources.play_app(package, account=acct,
                                             allow_paid=not scope.public and acct.user_id == request.state.who.id)
        except play.PlayAuthError as e:
            with w.conn() as c:
                people.set_play(c, acct.id, "expired", w.now())
            return add_page(request, status_code=400, error=str(e), link_url=link_url)
        except net.SourceError as e:
            return add_page(request, status_code=400, error=str(e), link_url=link_url)
        except net.FetchError as e:
            return add_page(request, status_code=502, error=str(e), link_url=link_url)
        return add_package(request, "play", package, checked.name, {"account": acct.id}, acct.id, paid=checked.paid)

    @app.post(f"{p}/add/source", response_class=HTMLResponse, dependencies=deps)
    def add_source(request: Request, source: str = Form(""), ref: str = Form(""), package: str = Form(""),
                   name: str = Form(""), ack: str = Form(""), account: int = Form(0)):
        """Выбранный результат поиска: поля проверяются независимо от формы."""
        ref, name = ref.strip(), name.strip()[:200]
        try:
            if len(ref) > MAX_URL or package and not apk.PACKAGE_RE.fullmatch(package):
                raise net.SourceError("не похоже на приложение Android")
            if source in fdroid_index.REPOSITORIES:
                expected, _ = fdroid_index.split_ref(ref)
                if package != expected:
                    raise net.SourceError("пакет не совпал с выбранным результатом")
            elif source == "github":
                if not github.parse_ref(ref) or github.parse_ref(ref) != ref:
                    raise net.SourceError("не похоже на репозиторий GitHub")
            elif source in ("rustore", "appgallery", "apkpure", "play"):
                if not apk.PACKAGE_RE.fullmatch(ref) or ref != package:
                    raise net.SourceError("не похоже на имя пакета Android")
            else:
                raise net.SourceError("неизвестный источник")
        except net.SourceError as e:
            return add_page(request, status_code=400, error=str(e))
        if source == "play":
            return add_play(request, package, account)
        payload = {"source": source, "ref": ref, "name": name}
        if source == "apkpure":
            payload["ack"] = ack == "1"
        if package and source != "apkpure":
            return add_package(request, source, package, name, payload, ref=ref)
        with w.conn() as c, store.tx(c):
            db.enqueue(c, ensure(c, request).id, "add", w.now(), payload=payload)
        return back(p)

    @app.post(f"{p}/add/rustore", response_class=HTMLResponse, dependencies=deps)
    def add_rustore(request: Request, package: str = Form(""), name: str = Form("")):
        if not apk.PACKAGE_RE.match(package):
            return add_page(request, status_code=400, error="Не похоже на имя пакета Android")
        name = name.strip()[:200]
        return add_package(request, "rustore", package, name, {"name": name})

    @app.post(f"{p}/add/play", response_class=HTMLResponse, dependencies=deps)
    def add_play(request: Request, package: str = Form(""), account: int = Form(0)):
        """Из Google Play подключённым аккаунтом области, после проверки доступности и доставки."""
        package = play_package(package)
        if not package:
            return add_page(request, status_code=400, error=PLAY_PACKAGE)
        with w.conn() as c:
            acct = next((g for g in google_accounts(c, request) if g.id == account and g.play == "ok"), None)
        if acct is None:
            return add_page(request, status_code=400, error=NEED_PLAY)
        return add_checked_play(request, package, acct)

    def check_fourpda(request):
        if scope.public:
            raise net.SourceError("4PDA доступен только на личной полке")
        sessions = w.env.sources.fourpda
        if sessions is None:
            raise net.SourceError("Подключите свой аккаунт 4PDA на странице «Аккаунты»")
        sessions.client(request.state.who.id).who()

    @app.post(f"{p}/add/link", response_class=HTMLResponse, dependencies=deps)
    def add_link(request: Request, url: str = Form(""), name: str = Form(""), ack: str = Form("")):
        """Страница приложения в магазине (links.parse) или прямая ссылка на APK. Google Play — первым подключённым
        аккаунтом области; ссылка и ID AppGallery — без карточки, пока обработчик не узнает пакет; APKPure — тоже без
        карточки (до сверки подписи), с галочкой ack."""
        url, name = url.strip(), name.strip()[:200]
        try:
            if len(url) > MAX_URL:
                raise net.FetchError("ссылка слишком длинная")
            source, ref = links.parse(url)
            if source == "fourpda":
                check_fourpda(request)
        except (net.FetchError, net.SourceError) as e:
            return add_page(request, status_code=400, error=f"{url[:200]}: {e}", link_url=url[:MAX_URL])
        if source == "play":
            with w.conn() as c:
                acct = next((g for g in google_accounts(c, request) if g.play == "ok"), None)
            if acct is None:
                return add_page(request, status_code=400, error=NEED_PLAY, link_url=url)
            return add_checked_play(request, ref, acct, link_url=url)
        if source in fdroid_index.REPOSITORIES and "@" in ref:
            package, _ = fdroid_index.split_ref(ref)
            return add_package(request, source, package, name, {"name": name}, ref=ref)
        if source not in ("link", "appgallery", "apkpure", "github", "fourpda"):
            return add_package(request, source, ref, name, {"name": name})
        payload = {"source": source, "ref": ref, "name": name}
        if source == "apkpure":
            payload["ack"] = ack == "1"
        with w.conn() as c, store.tx(c):
            shelf = ensure(c, request)
            db.enqueue(c, shelf.id, "add", w.now(), payload=payload)
        return back(p)

    @app.post(f"{p}/add/upload", response_class=HTMLResponse, dependencies=deps)
    def add_upload(request: Request, file: UploadFile | None = File(None), name: str = Form("")):
        path, err = save_upload(file)
        if err:
            return add_page(request, status_code=err[0], error=err[1])
        try:
            with w.conn() as c, store.tx(c):
                shelf = ensure(c, request)
                db.enqueue(c, shelf.id, "upload", w.now(), payload={"path": str(path), "name": name.strip()[:200]})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
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
        try:
            with w.conn() as c, store.tx(c):
                current, app_row = own(c, request, package)
                if app_row is None or current.id != shelf.id:
                    raise store.AppGone(package)
                db.enqueue(c, shelf.id, "upload", w.now(), package=package, payload={"path": str(path), "name": ""})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
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

    def change(request: Request, package: str, fn, missing=not_found):
        """Действие, которое может удалить каталоги на полке: архив недоступен — 503 и ничего не тронуто.
        fn(c, root, shelf_id) → False — нет такой версии: ответ missing() (по умолчанию 404)."""
        try:
            with jobs.hold(cfg.download_lock, jobs.REMOVE_LOCK_WAIT), w.conn() as c:
                jobs._archive(cfg)
                shelf, app_row = own(c, request, package)
                done = app_row is not None and fn(c, fetch.shelf_root(cfg, shelf), shelf.id)
        except jobs.ArchiveUnavailable:
            return catalog_page(request, 503, ARCHIVE_DOWN)
        except ipatool.IpatoolError:
            return catalog_page(request, 409, "Полка занята скачиванием — повторите через минуту")
        return back(p) if done else missing()

    @app.post(p + "/apps/{package}/remove", response_class=HTMLResponse, dependencies=deps)
    def remove(request: Request, package: str):
        def drop(c, root, sid):
            db.remove_app(c, root, sid, package)
            return True
        return change(request, package, drop)

    @app.post(p + "/apps/{package}/accept-signer", response_class=HTMLResponse, dependencies=deps)
    def accept_signer(request: Request, package: str, version_id: int = Form(0), confirm: str = Form("")):
        def accept(c, root, sid):
            pending = c.execute("SELECT id FROM android_versions WHERE shelf_id=? AND package=? AND role='pending'",
                                (sid, package)).fetchone()
            if pending is None or pending["id"] != version_id or confirm != "1":
                return False
            db.accept_pending(c, root, sid, package)  # принимать нечего (уже приняли) — просто назад
            return True
        return change(request, package, accept)

    @app.post(p + "/apps/{package}/reject-signer", response_class=HTMLResponse, dependencies=deps)
    def reject_signer(request: Request, package: str, version_id: int = Form(0)):
        return change(request, package, lambda c, root, sid: db.reject_pending(c, root, sid, package, version_id))

    @app.post(p + "/apps/{package}/versions/{version_id}/pin", response_class=HTMLResponse, dependencies=deps)
    def pin_version(request: Request, package: str, version_id: int):
        return change(request, package, lambda c, root, sid: db.pin_version(c, root, sid, package, version_id))

    @app.post(p + "/apps/{package}/versions/{version_id}/unpin", response_class=HTMLResponse, dependencies=deps)
    def unpin_version(request: Request, package: str, version_id: int):
        return change(request, package, lambda c, root, sid: db.unpin_version(c, root, sid, package, version_id))

    def remove_version_page(request: Request, package: str, version_id: int, status_code: int = 200, error: str = ""):
        """Подтверждение удаления; после устаревшего подтверждения — та же страница с новым снимком (409) или,
        если версии уже нет, страница 404 с «Назад»."""
        with w.conn() as c:
            shelf, app_row = own(c, request, package)
            row = db.old_version(c, shelf.id, package, version_id) if app_row is not None else None
        cancel = p + f"?person={request.state.who.id}"
        if row is None:
            return w.page(request, "confirm.html", scope.nav, 404, refused=True, cancel=cancel,
                          question="Этой версии уже нет на полке: её удалили или заменили.")
        pinned = " Закрепление будет снято." if row["pinned"] else ""
        return w.page(request, "confirm.html", scope.nav, status_code, error=error,
                      question=f"Удалить версию {row['version_name']} приложения «{row['app_name']}»? "
                               f"Файл APK и ссылка установки этой версии удалятся.{pinned}",
                      action=f"{p}/apps/{package}/versions/{version_id}/remove", button="Удалить версию",
                      cancel=cancel, hidden={"snapshot": store.version_snapshot(row)})

    @app.get(p + "/apps/{package}/versions/{version_id}/remove", response_class=HTMLResponse, dependencies=deps)
    def confirm_remove_version(request: Request, package: str, version_id: int):
        return remove_version_page(request, package, version_id)

    @app.post(p + "/apps/{package}/versions/{version_id}/remove", response_class=HTMLResponse, dependencies=deps)
    def remove_version(request: Request, package: str, version_id: int, snapshot: str = Form("")):
        if not snapshot:
            return PlainTextResponse("Подтвердите удаление на странице версии", status_code=400)
        return change(request, package,
                      lambda c, root, sid: db.remove_version(c, root, sid, package, version_id, snapshot),
                      missing=lambda: remove_version_page(
                          request, package, version_id, 409,
                          "Версия изменилась, пока страница была открыта (например, её закрепили в другой вкладке). "
                          "Проверьте и подтвердите удаление заново."))

    @app.post(p + "/apps/{package}/source", response_class=HTMLResponse, dependencies=deps)
    def change_source(request: Request, package: str, source: str = Form(""), ref: str = Form(""),
                      ack: str = Form("")):
        """Сменить источник: закрепление остаётся, первая версия из нового источника — по правилу подписи и ротации.
        Ссылка — страница магазина (links.parse) или прямая на APK; страница другого пакета — отказ. Новый источник
        сразу проверяется скачиванием; его отказ (подпись, набор файлов, нет пакета) возвращает прежний."""
        with w.conn() as c:
            shelf, app_row = own(c, request, package)
        if app_row is None:
            return not_found()
        ref = ref.strip()
        if source in BY_PACKAGE:
            ref = package
        elif source == "link":
            try:
                if len(ref) > MAX_URL:
                    raise net.FetchError("ссылка слишком длинная")
                source, ref = links.parse(ref)
                if source == "fourpda":
                    check_fourpda(request)
            except (net.FetchError, net.SourceError) as e:
                return catalog_page(request, 400, f"{ref[:200]}: {e}")
            if source == "play":
                return catalog_page(request, 400, NO_PLAY_SOURCE)
            want = fdroid_index.split_ref(ref)[0] if source in fdroid_index.REPOSITORIES else ref
            if source not in ("link", "appgallery", "github", "fourpda") and want != package:
                return catalog_page(request, 400, f"На странице — приложение {ref}, а на карточке — {package}")
        else:
            return catalog_page(request, 400, "Источник — магазин, его страница или ссылка на APK")
        with w.conn() as c, store.tx(c):
            old = db.get_app(c, shelf.id, package)
            if old is None:
                return not_found()
            if db.active_for(c, shelf.id, package):
                return catalog_page(request, 409, "Идёт проверка или скачивание — смените источник после завершения")
            db.set_source(c, shelf.id, package, source, ref)
            requeue(c, shelf, db.get_app(c, shelf.id, package), ack=ack == "1",
                    previous=[old["source"], old["source_ref"], old["source_etag"], old["source_page"]])
        return back(p)

    @app.post(p + "/jobs/{job_id}/dismiss", dependencies=deps)
    def dismiss(request: Request, job_id: int):
        with w.conn() as c:
            shelf = shelf_of(c, request)
            if shelf is not None:
                db.dismiss_job(c, shelf.id, job_id)
        return back(p)

    @app.post(p + "/jobs/{job_id}/alternative", dependencies=deps)
    def alternative(request: Request, job_id: int, source: str = Form("")):
        """После набора файлов — повторное добавление того же пакета из выбранного доступного источника."""
        with w.conn() as c, store.tx(c):
            shelf = shelf_of(c, request)
            job = c.execute("SELECT * FROM android_jobs WHERE id=? AND shelf_id=? AND kind='add' AND status='error'",
                            (job_id, shelf.id)).fetchone() if shelf else None
            payload = json.loads(job["payload"] or "{}") if job else {}
            package = payload.get("ref", "")
            allowed = {a["source"] for a in payload.get("alternatives", [])}
            if not payload.get("split_only") or source not in allowed or source not in ("rustore", "appgallery", "fdroid") \
                    or not apk.PACKAGE_RE.fullmatch(package):
                return not_found()
            if db.has_current(c, shelf.id, package) or db.active_for(c, shelf.id, package):
                return back(p)
            db.put_app(c, shelf.id, package, payload.get("name") or package, source, package, w.now())
            db.set_source(c, shelf.id, package, source, package)
            db.set_status(c, shelf.id, package, "queued", "")
            db.enqueue(c, shelf.id, "add", w.now(), package=package,
                       payload={"source": source, "ref": package, "name": payload.get("name", "")})
            db.dismiss_job(c, shelf.id, job_id)
        return back(p)

    return catalog_page
