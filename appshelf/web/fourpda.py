"""Подключение личной сессии 4PDA, поиск тем и выбор APK на свою полку."""
from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import replace
from urllib.parse import urlencode

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import jobs, people, webauth
from ..android import db, fourpda
from ..android.net import FetchError, SourceError
from ..ipatool import IpatoolError
from .common import WHO_COOKIE, back


def register_routes(app, w, sessions, browser):
    def owner(request, uid):
        return next((u for u in request.state.users if u.id == uid), None)

    def missing():
        return PlainTextResponse("not found", status_code=404)

    def connected_response(uid):
        response = back("/android/add")
        w.set_cookie(response, WHO_COOKIE, str(uid), webauth.COOKIE_AGE)
        return response

    def connect_page(request, uid, error="", status=200, token=""):
        user = owner(request, uid)
        if user is None:
            return missing()
        try:
            session = sessions.load(uid)
        except SourceError:
            session = None
            error = error or fourpda.EXPIRED
        response = w.page(request, "fourpda_connect.html", "apple", status, person=user,
                          connected=session.name if session else "", error=error, token=token)
        response.headers["Cache-Control"] = "no-store"
        return response

    def save(request, uid, session):
        # Проверка сети до записи: неверная/просроченная сессия не заменит рабочую.
        session = replace(session, name=fourpda.Client(sessions.http, session).who())
        with jobs.hold(w.cfg.download_lock, 10), w.conn() as c:
            if people.get_user(c, uid) is None:
                raise SourceError("Участник удалён")
            sessions.save(uid, session)

    @app.get("/fourpda/{uid}/connect")
    def connect(request: Request, uid: int):
        return connect_page(request, uid)

    @app.post("/fourpda/{uid}/login")
    def start(request: Request, uid: int):
        if owner(request, uid) is None:
            return missing()
        if w.limiter.email_wait(f"4pda:{uid}"):
            return connect_page(request, uid, "Слишком много попыток — попробуйте позже", 429)
        w.limiter.email_fail(f"4pda:{uid}")
        try:
            mobile = bool(re.search(r"Android|iPhone|Mobile", request.headers.get("user-agent", ""), re.I))
            token = browser.call(uid, "", "start", mobile=mobile)
            return connect_page(request, uid, token=token)
        except SourceError as e:
            return connect_page(request, uid, str(e), 503)

    @app.get("/fourpda/{uid}/screen")
    def screen(request: Request, uid: int):
        if owner(request, uid) is None:
            return missing()
        try:
            frame = browser.call(uid, request.headers.get("x-4pda-window", ""), "frame")
            return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        except SourceError as e:
            return PlainTextResponse(str(e), status_code=409, headers={"Cache-Control": "no-store"})

    @app.post("/fourpda/{uid}/input")
    def input_(request: Request, uid: int, action: str = Form(""), text: str = Form(""),
               key: str = Form(""), x: str = Form(""), y: str = Form(""), delta: str = Form("0")):
        if owner(request, uid) is None:
            return missing()
        if action not in ("click", "text", "key", "scroll"):
            return PlainTextResponse("unknown action", status_code=400)
        try:
            browser.call(uid, request.headers.get("x-4pda-window", ""), action,
                         text=text, key=key, x=x, y=y, delta=delta)
            return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        except SourceError as e:
            return JSONResponse({"error": str(e)}, status_code=409, headers={"Cache-Control": "no-store"})

    @app.post("/fourpda/{uid}/finish")
    def finish(request: Request, uid: int, token: str = Form("")):
        if owner(request, uid) is None:
            return missing()
        try:
            session = browser.call(uid, token, "finish")
            save(request, uid, session)
        except (SourceError, FetchError, IpatoolError) as e:
            return connect_page(request, uid, str(e), 400, token)
        with suppress(SourceError):
            browser.call(uid, token, "cancel")
        return connected_response(uid)

    @app.post("/fourpda/{uid}/cancel")
    def cancel(request: Request, uid: int, token: str = Form("")):
        if owner(request, uid) is None:
            return missing()
        try:
            browser.call(uid, token, "cancel")
        except SourceError:
            pass
        return back(f"/fourpda/{uid}/connect")

    @app.post("/fourpda/{uid}/import")
    def import_(request: Request, uid: int, file: UploadFile | None = File(None),
                session_json: str = Form(""), user_agent: str = Form("")):
        if owner(request, uid) is None:
            return missing()
        try:
            if file is not None and session_json.strip():
                raise SourceError("Вставьте JSON или выберите файл сессии — что-то одно")
            raw = file.file.read(fourpda.MAX_SESSION + 1) if file is not None else session_json.encode()
            if not raw.strip():
                raise SourceError("Вставьте JSON сессии или выберите файл")
            if len(raw) > fourpda.MAX_SESSION:
                raise SourceError("Сессия слишком большая (не более 32 КБ)")
            data = json.loads(raw)
            if isinstance(data, list):
                if any(not isinstance(c, dict) or c.get("domain", "").lstrip(".") != "4pda.to" for c in data):
                    raise SourceError("В файле должны быть только cookies 4pda.to")
                data = {"cookies": {c["name"]: c["value"] for c in data},
                        "user_agent": user_agent or request.headers.get("user-agent", "")}
            save(request, uid, sessions.parse(json.dumps(data).encode()))
        except (ValueError, TypeError, KeyError):
            return connect_page(request, uid, "Не удалось прочитать файл сессии", 400)
        except (SourceError, FetchError, IpatoolError) as e:
            return connect_page(request, uid, str(e), 400)
        finally:
            if file is not None:
                file.file.close()
        with suppress(SourceError):
            browser.call(uid, "", "revoke")
        return connected_response(uid)

    @app.post("/fourpda/{uid}/disconnect")
    def disconnect(request: Request, uid: int):
        if owner(request, uid) is None:
            return missing()
        try:
            with jobs.hold(w.cfg.download_lock, 10):
                sessions.forget(uid)
            with suppress(SourceError):
                browser.call(uid, "", "revoke")
        except IpatoolError:
            return connect_page(request, uid, "Идёт скачивание — отключите аккаунт через минуту", 409)
        return back(f"/fourpda/{uid}/connect")

    def catalog(request, topic="", error="", status=200):
        uid = request.state.who.id
        files, title, name = [], "", ""
        try:
            if topic:
                topic = fourpda.topic_url(topic)
            session = sessions.load(uid)
            if session is not None:
                name = session.name
                client = sessions.client(uid)
                if topic:
                    title, files = client.files(topic)
        except (FetchError, SourceError) as e:
            error, status = str(e), 400
            # Не возвращать непроверенный href (в том числе javascript:) в ссылку на форум.
            if not title:
                topic = ""
        return w.page(request, "fourpda_catalog.html", "a_add", status, connected=name, topic=topic,
                      files=files, title=title, error=error)

    @app.get("/android/4pda")
    def search(request: Request, q: str = ""):
        query = q.strip()[:200]
        return back("/android/add" + ("?" + urlencode({"q": query}) if query else ""))

    @app.get("/android/4pda/topic")
    def topic(request: Request, url: str = ""):
        return catalog(request, topic=url[:2000])

    @app.post("/android/4pda/add")
    def add(request: Request, url: str = Form(""), name: str = Form(""), topic: str = Form("")):
        try:
            ref = fourpda.file_url(url)
            page = fourpda.topic_url(topic) if topic else ""
            # Полку определяет сервер. ID участника/аккаунта из формы не принимается.
            sessions.client(request.state.who.id).who()
        except (FetchError, SourceError) as e:
            return catalog(request, error=str(e), status=400)
        with w.conn() as c:
            shelf = db.ensure_shelf(c, request.state.who.id, w.now())
            db.enqueue(c, shelf.id, "add", w.now(), payload={"source": "fourpda", "ref": ref,
                       "name": name.strip()[:200], "source_page": page})
        return back("/android")
