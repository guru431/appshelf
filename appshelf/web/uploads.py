"""Small raw chunk requests avoid multipart spooling and retain progress across reconnects."""
from __future__ import annotations

import json

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .. import apk
from ..android.uploads import CHUNK_SIZE, UploadError, Uploads
from .admin import owner_only


def register(app, w, scope, shelf_of, ensure):
    manager, prefix = Uploads(w.cfg), scope.prefix + "/uploads"
    deps = [Depends(owner_only)] if scope.public else []

    def run(request, operation, upload_id=None, *args):
        try:
            with w.conn() as c:
                if upload_id is None:
                    payload = args[0]
                    if not isinstance(payload, dict):
                        raise UploadError(400, "Неверные сведения о файле")
                    package = payload.get("package", "")
                    if package and (not isinstance(package, str) or not apk.PACKAGE_RE.fullmatch(package)):
                        raise UploadError(400, "Неверный пакет приложения")
                    shelf = shelf_of(c, request)
                    if package and (shelf is None or c.execute("SELECT 1 FROM android_apps WHERE shelf_id=? AND package=?",
                                                              (shelf.id, package)).fetchone() is None):
                        raise UploadError(404, "Приложение больше не находится на полке")
                    shelf = shelf or ensure(c, request)
                    result = manager.create(c, request.state.who.id, shelf.id, payload, w.now())
                else:
                    shelf = shelf_of(c, request)
                    if shelf is None:
                        raise UploadError(404, "Полка больше не существует")
                    row = manager.get(c, upload_id, request.state.who.id, shelf.id, w.now())
                    if row["package"] and c.execute("SELECT 1 FROM android_apps WHERE shelf_id=? AND package=?",
                                                   (shelf.id, row["package"])).fetchone() is None:
                        raise UploadError(404, "Приложение больше не находится на полке")
                    if operation == "part":
                        result = manager.part(c, row, *args, w.now())
                        result = {"id": result["id"], "received": args[0], "job_id": result["job_id"]}
                    elif operation == "finish":
                        result = manager.finish(c, row, w.now())
                    elif operation == "cancel":
                        manager.cancel(c, row)
                        result = {"cancelled": True}
                    else:
                        result = manager.view(row)
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except UploadError as error:
            return JSONResponse({"error": error.message}, status_code=error.status)
        except OSError:
            return JSONResponse({"error": "Не удалось сохранить часть файла. Повторите отправку позднее"}, status_code=503)

    @app.post(prefix, dependencies=deps)
    async def create(request: Request):
        raw = await limited_body(request, 4096)
        if raw is None:
            return JSONResponse({"error": "Слишком большой запрос"}, status_code=413)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "Неверные сведения о файле"}, status_code=400)
        return await run_in_threadpool(run, request, "create", None, payload)

    @app.get(prefix + "/{upload_id}", dependencies=deps)
    def status(request: Request, upload_id: str):
        return run(request, "status", upload_id)

    @app.post(prefix + "/{upload_id}/parts/{index}", dependencies=deps)
    async def part(request: Request, upload_id: str, index: int):
        # Authenticate the session before reading a body, including on stale tabs.
        checked = await run_in_threadpool(run, request, "status", upload_id)
        if checked.status_code != 200:
            return checked
        raw = await limited_body(request, CHUNK_SIZE)
        if raw is None:
            return JSONResponse({"error": "Часть файла слишком велика"}, status_code=413)
        return await run_in_threadpool(run, request, "part", upload_id, index, raw,
                                      request.headers.get("X-Chunk-SHA256", ""))

    @app.post(prefix + "/{upload_id}/finish", dependencies=deps)
    def finish(request: Request, upload_id: str):
        return run(request, "finish", upload_id)

    @app.post(prefix + "/{upload_id}/cancel", dependencies=deps)
    def cancel(request: Request, upload_id: str):
        return run(request, "cancel", upload_id)


async def limited_body(request, limit):
    chunks, size = [], 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
    except ClientDisconnect:
        return b""
    return b"".join(chunks)
