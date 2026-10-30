"""Раздел Android панели (spec 2026-10-28 §5 «Раздел по умолчанию», §6, §8, §10): каталог Android-полки, «Добавить»
(RuStore, Google Play, ссылка, свой файл), действия карточки. Две области с одними обработчиками (Scope): своя полка — активного
участника раздела (request.state.who, выбор — /who/<id>), публичная — одна на сервер: читают все вошедшие, меняет и задаёт PIN только
владелец. Пакет ищется только на полке области, чужой — 404. Скачивает обработчик заданий (android.fetch.run_job),
страница только ставит задания."""
from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from .. import apk, jobs, notify, people, store
from ..android import db, fdroid_index, fetch, github, links, net, pin
from ..config import GB
from ..nightly import SEP
from .admin import owner_only
from .common import TEMPLATES, back

OS_COOKIE = "appshelf_os"   # раздел, открытый последним: apple | android — для компьютера (spec §5)
SOURCES = {"rustore": "RuStore", "link": "сайт", "upload": "свой файл", "play": "Google Play",
           "appgallery": "AppGallery", "fdroid": "F-Droid", "fdroid_archive": "Архив F-Droid",
           "github": "GitHub", "apkpure": "APKPure"}
NEW_VERSION_DAYS = 7
ARCHIVE_DOWN = "Архив недоступен — изменить полку сейчас нельзя"
TOO_BIG = "Файл больше 1 ГБ — такой не принимается"
MAX_URL = 2000
PIN_FORMAT = "PIN — 6 цифр, только цифры 0–9"
PIN_BUSY = "Идёт скачивание — попробуйте через минуту: PIN не изменён"
NEED_PLAY = "Сначала подключите Google-аккаунт к Google Play на странице «Аккаунты»"
PLAY_PACKAGE = "Не похоже на ссылку Google Play или имя пакета"
NO_PLAY_SOURCE = "Источником карточки Google Play не ставится — снимите её и добавьте приложение из Google Play"
BY_PACKAGE = ("rustore", "appgallery", "fdroid", "fdroid_archive", "apkpure")
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
            f"Открыть сразу — «Открыть ввод PIN» на вкладке Android → «Публичная» ({base}/android/public) или смена "
            f"PIN там же; если PIN мог утечь — смените его.")


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

    def guest_page(request: Request, status_code: int = 200, error: str = ""):
        """Страница гостя (spec §8): вне входа в панель — middleware её пропускает, request.state пуст. Список видят
        гость с действующей cookie и вошедший участник; остальным — форма PIN, пока ввод не закрыт всем."""
        with w.conn() as c:
            shelf = db.public_shelf(c)
            if shelf is None or not pin.is_set(c):
                return w.page(request, "guest.html", "guest", status_code, configured=False)
            guest = pin.guest_ok(w.key, request.cookies.get(pin.COOKIE, ""), pin.epoch(c), w.now())
            closed = pin.closed_until(c, w.now())
            apps = [a for a in db.list_apps(c, shelf.id) if a.current is not None]
        allowed = guest or w.person(request) is not None
        return w.page(request, "guest.html", "guest", status_code, configured=True, allowed=allowed,
                      closed=closed, error=error, apps=apps if allowed else [], fresh=_fresh(apps, w.now()),
                      sources=SOURCES, notices=NOTICES, android_version=apk.android_version,
                      apk_name=fetch.apk_name,
                      url=lambda d, f: fetch.file_url(cfg, shelf, d, f))

    @app.get("/p", response_class=HTMLResponse)
    def guest(request: Request):
        return guest_page(request)

    @app.post("/p", response_class=HTMLResponse)
    def guest_pin(request: Request, value: str = Form("", alias="pin")):
        key = pin.client_key(request.headers.get("x-forwarded-for"), request.client.host if request.client else None)
        with w.conn() as c:
            if db.public_shelf(c) is None or not pin.is_set(c):
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
    def app_link(token: str, file: str):
        """Постоянная ссылка на приложение («Скопировать ссылку»): переадресация на файл текущей версии. Без входа,
        как сами файлы /d/: секрет — токен каталога полки, его смена (rotate-token, смена PIN) гасит и ссылку."""
        package = file.removesuffix(".apk")
        if not file.endswith(".apk") or not apk.PACKAGE_RE.match(package) or not TOKEN_RE.match(token):
            return PlainTextResponse("not found", status_code=404)
        with w.conn() as c:
            shelf = db.shelf_by_token(c, token)
            v = db.current_version(c, shelf.id, package) if shelf is not None else None
        if v is None:
            return PlainTextResponse("not found", status_code=404)
        return RedirectResponse(fetch.file_url(cfg, shelf, v["dir"], fetch.apk_name(package, v["version_name"])),
                                status_code=302)

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
            nightly = (store.get_state(c, "nightly_last"), nightly_part(store.get_state(c, "nightly_result")))
            if scope.public and edit:  # блок «Гостевой доступ» — владельцу
                guest = {"url": f"{cfg.public_base}/p", "closed": pin.closed_until(c, w.now()),
                         "set_at": store.get_state(c, pin.SET_AT_KEY) if pin.is_set(c) else ""}
            used = {r[0] for r in c.execute("SELECT google_account_id FROM android_apps WHERE shelf_id=? "
                                           "AND source='play'", (shelf.id,))} if shelf else set()
            expired = [g.email for g in google_accounts(c, request)
                       if g.play == "expired" and (not scope.public or g.id in used)] if edit else []
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
        resp = w.page(request, "android.html", scope.nav, status_code, apps=apps, jobs=pending, banners=banners,
                      busy=busy, nightly=nightly, fresh=_fresh(apps, w.now()), statuses=json.dumps({a.package: a.status for a in apps}),
                      status_url="/api/android/status" + ("?public=1" if scope.public else ""), sources=SOURCES,
                      notices=NOTICES,
                      android_version=apk.android_version, apk_name=fetch.apk_name,
                      url=lambda d, f: fetch.file_url(cfg, shelf, d, f),
                      app_url=lambda package: fetch.app_url(cfg, shelf, package), prefix=p, public=scope.public,
                      edit=edit, guest=guest)
        set_section(w, resp, "android")
        return resp

    def add_page(request: Request, q: str = "", status_code: int = 200, error: str = "", link_url: str = ""):
        found, search_errors = [], []
        q = q.strip()[:200]
        with w.conn() as c:
            shelf = shelf_of(c, request)
            on_shelf = {a.package for a in db.list_apps(c, shelf.id)} if shelf is not None else set()
            accounts = google_accounts(c, request)
        play_accounts = [g for g in accounts if g.play == "ok"]
        return w.page(request, "android_add.html", "a_public" if scope.public else "a_add", status_code, q=q,
                      found=found, search_errors=search_errors, sources=SOURCES,
                      on_shelf=on_shelf, error=error, link_url=link_url,
                      prefix=p, public=scope.public, play_accounts=play_accounts, has_google=bool(accounts))

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
                for update in w.env.sources.search_events(q, accounts=accounts, split_packages=split_packages):
                    if update.kind == "status":
                        yield message("status", {"source": update.source, "state": update.state,
                                                 "detail": update.detail})
                    if update.result is not None and (update.result != previous or update.kind == "done"):
                        result = update.result
                        html = TEMPLATES.get_template("android_search_results.html").render(
                            found=result.options, search_errors=result.errors, on_shelf=on_shelf,
                            sources=SOURCES, prefix=p, complete=update.kind == "done")
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
        if app["source"] == "upload" or db.active_for(c, shelf.id, package):
            return
        with store.tx(c):
            db.set_status(c, shelf.id, package, "queued")
            if db.has_current(c, shelf.id, package):
                db.enqueue(c, shelf.id, "check", w.now(), package=package,
                           payload={"previous": previous} if previous else None)
            else:
                payload = {"source": app["source"], "ref": app["source_ref"], "name": app["name"]}
                if app["source"] == "apkpure":
                    payload["ack"] = ack
                db.enqueue(c, shelf.id, "add", w.now(), package=package, payload=payload)

    @app.get(p, response_class=HTMLResponse)
    def android_catalog(request: Request):
        return catalog_page(request)

    @app.get(f"{p}/add", response_class=HTMLResponse, dependencies=deps)
    def android_add(request: Request, q: str = ""):
        return add_page(request, q)

    def add_package(request: Request, source: str, package: str, name: str, payload: dict,
                    account: int | None = None, ref: str | None = None):
        """Карточка пакета и задание add (payload — к source и ref); пакет уже на полке — второго не ставим."""
        with w.conn() as c:
            shelf = ensure(c, request)
            with store.tx(c):
                if db.get_app(c, shelf.id, package) is None:
                    db.put_app(c, shelf.id, package, name or package, source, ref or package, w.now(),
                               google_account_id=account)
                    db.enqueue(c, shelf.id, "add", w.now(), package=package,
                               payload={"source": source, "ref": ref or package, **payload})
        return back(p)

    @app.post(f"{p}/add/source", response_class=HTMLResponse, dependencies=deps)
    def add_source(request: Request, source: str = Form(""), ref: str = Form(""), package: str = Form(""),
                   name: str = Form(""), ack: str = Form(""), account: int = Form(0)):
        """Выбранный результат поиска: поля проверяются независимо от формы."""
        ref, name = ref.strip(), name.strip()[:200]
        try:
            if len(ref) > MAX_URL or package and not apk.PACKAGE_RE.fullmatch(package):
                raise net.SourceError("не похоже на приложение Android")
            if source in ("fdroid", "fdroid_archive"):
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
        with w.conn() as c:
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
        """Из Google Play подключённым аккаунтом области; купленное и набор файлов отсеет обработчик (sources)."""
        package = play_package(package)
        if not package:
            return add_page(request, status_code=400, error=PLAY_PACKAGE)
        with w.conn() as c:
            acct = next((g for g in google_accounts(c, request) if g.id == account and g.play == "ok"), None)
        if acct is None:
            return add_page(request, status_code=400, error=NEED_PLAY)
        return add_package(request, "play", package, "", {"account": acct.id}, acct.id)

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
        except net.FetchError as e:
            return add_page(request, status_code=400, error=f"{url[:200]}: {e}", link_url=url[:MAX_URL])
        if source == "play":
            with w.conn() as c:
                acct = next((g for g in google_accounts(c, request) if g.play == "ok"), None)
            if acct is None:
                return add_page(request, status_code=400, error=NEED_PLAY, link_url=url)
            return add_package(request, "play", ref, "", {"account": acct.id}, acct.id)
        if source in ("fdroid", "fdroid_archive") and "@" in ref:
            package, _ = fdroid_index.split_ref(ref)
            return add_package(request, source, package, name, {"name": name}, ref=ref)
        if source not in ("link", "appgallery", "apkpure", "github"):
            return add_package(request, source, ref, name, {"name": name})
        payload = {"source": source, "ref": ref, "name": name}
        if source == "apkpure":
            payload["ack"] = ack == "1"
        with w.conn() as c:
            shelf = ensure(c, request)
            db.enqueue(c, shelf.id, "add", w.now(), payload=payload)
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
    def change_source(request: Request, package: str, source: str = Form(""), ref: str = Form(""),
                      ack: str = Form("")):
        """Сменить источник: версии и закрепления остаются, первая версия из нового источника — по правилу подписи.
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
            except net.FetchError as e:
                return catalog_page(request, 400, f"{ref[:200]}: {e}")
            if source == "play":
                return catalog_page(request, 400, NO_PLAY_SOURCE)
            want = fdroid_index.split_ref(ref)[0] if source in ("fdroid", "fdroid_archive") else ref
            if source not in ("link", "appgallery", "github") and want != package:
                return catalog_page(request, 400, f"На странице — приложение {ref}, а на карточке — {package}")
        else:
            return catalog_page(request, 400, "Источник — магазин, его страница или ссылка на APK")
        with w.conn() as c:
            old = db.get_app(c, shelf.id, package)
            db.set_source(c, shelf.id, package, source, ref)
            requeue(c, shelf, db.get_app(c, shelf.id, package), ack=ack == "1",
                    previous=[old["source"], old["source_ref"], old["source_etag"]])
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
