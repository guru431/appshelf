"""Durable, owner-bound APK chunks on local disk; finalization enqueues exactly one existing upload job."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .. import store
from ..config import GB
from . import db, net

CHUNK_SIZE = 1024 * 1024
MAX_RESERVED = 2 * GB
TTL = timedelta(days=7)
LOCK = threading.RLock()  # One web process; also shared with user deletion and recovery.
HEX = re.compile(r"[a-f0-9]{32}")
DIGEST = re.compile(r"[a-f0-9]{64}")


class UploadError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


class Uploads:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.data_dir / "uploads"

    def folder(self, upload_id: str) -> Path:
        if not HEX.fullmatch(upload_id):
            raise UploadError(404, "Загрузка не найдена")
        return self.root / upload_id

    def cleanup(self, c, now: str) -> None:
        cutoff = (datetime.fromisoformat(now) - TTL).isoformat()
        with LOCK:
            for row in c.execute("SELECT u.* FROM android_uploads u WHERE "
                                 "(u.updated_at < ? OR NOT EXISTS (SELECT 1 FROM users WHERE id=u.user_id) "
                                 "OR NOT EXISTS (SELECT 1 FROM android_shelves WHERE id=u.shelf_id) "
                                 "OR (u.package<>'' AND NOT EXISTS (SELECT 1 FROM android_apps a "
                                 "WHERE a.shelf_id=u.shelf_id AND a.package=u.package)))",
                                 (cutoff,)).fetchall():
                if self.active(c, row):
                    continue
                shutil.rmtree(self.folder(row["id"]), ignore_errors=True)
                c.execute("DELETE FROM android_uploads WHERE id=?", (row["id"],))
            if self.root.is_dir():
                known = {r[0] for r in c.execute("SELECT id FROM android_uploads")}
                for folder in self.root.iterdir():
                    if folder.is_dir() and HEX.fullmatch(folder.name) and folder.name not in known:
                        shutil.rmtree(folder)

    def remove_user(self, c, user_id: int) -> None:
        with LOCK:
            for row in c.execute("SELECT * FROM android_uploads WHERE user_id=?", (user_id,)).fetchall():
                if self.active(c, row):
                    continue
                shutil.rmtree(self.folder(row["id"]), ignore_errors=True)
                c.execute("DELETE FROM android_uploads WHERE id=?", (row["id"],))

    def active(self, c, row) -> bool:
        if row["job_id"] is None:
            return False
        job = c.execute("SELECT status,payload FROM android_jobs WHERE id=?", (row["job_id"],)).fetchone()
        return bool(job and job["status"] in ("queued", "running") and json.loads(job["payload"] or "{}").get("path")
                    == str(self.folder(row["id"]) / "complete.apk"))

    def get(self, c, upload_id: str, user_id: int, shelf_id: int, now: str):
        self.folder(upload_id)
        row = c.execute("SELECT * FROM android_uploads u WHERE id=? AND user_id=? AND shelf_id=? "
                        "AND EXISTS (SELECT 1 FROM users WHERE id=u.user_id) "
                        "AND EXISTS (SELECT 1 FROM android_shelves WHERE id=u.shelf_id)",
                        (upload_id, user_id, shelf_id)).fetchone()
        if row is None:
            raise UploadError(404, "Загрузка недоступна. Отмените эту передачу и выберите файл заново")
        if row["job_id"] is None and datetime.fromisoformat(now) - datetime.fromisoformat(row["updated_at"]) > TTL:
            raise UploadError(410, "Срок докачки истёк. Отмените эту передачу и загрузите файл заново")
        return row

    @staticmethod
    def view(row) -> dict:
        return {"id": row["id"], "size": row["size"], "fingerprint": row["fingerprint"],
                "chunk_size": CHUNK_SIZE, "parts": json.loads(row["parts"]), "job_id": row["job_id"]}

    def create(self, c, user_id: int, shelf_id: int, payload: dict, now: str) -> dict:
        if not isinstance(payload, dict):
            raise UploadError(400, "Неверные сведения о файле")
        size, client = payload.get("size"), payload.get("client_id", "")
        fingerprint, package = payload.get("fingerprint", ""), payload.get("package", "")
        if type(size) is not int or not 0 < size <= net.MAX_APK:
            raise UploadError(413, "Выберите непустой APK до 1 ГБ")
        if (not isinstance(client, str) or not HEX.fullmatch(client)
                or not isinstance(fingerprint, str) or not DIGEST.fullmatch(fingerprint)
                or not isinstance(package, str) or len(package) > 200):
            raise UploadError(400, "Неверные сведения о файле")
        name, filename = payload.get("name", ""), payload.get("filename", "")
        if not isinstance(name, str) or not isinstance(filename, str) or not filename or len(filename) > 255:
            raise UploadError(400, "Неверное имя файла")
        with LOCK, store.tx(c):
            self.cleanup(c, now)
            if (not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
                    or not c.execute("SELECT 1 FROM android_shelves WHERE id=?", (shelf_id,)).fetchone()
                    or package and not db.get_app(c, shelf_id, package)):
                raise UploadError(404, "Полка или приложение больше не существует")
            previous = c.execute("SELECT * FROM android_uploads WHERE user_id=? AND client_id=?",
                                 (user_id, client)).fetchone()
            if previous is not None:
                if (previous["shelf_id"], previous["size"], previous["fingerprint"], previous["package"]) != (
                        shelf_id, size, fingerprint, package):
                    raise UploadError(409, "Эта передача уже закреплена за другим файлом или полкой")
                return self.view(previous)
            previous = c.execute("SELECT * FROM android_uploads WHERE user_id=? AND shelf_id=? AND size=? "
                                 "AND fingerprint=? AND package=? AND job_id IS NULL ORDER BY created_at DESC LIMIT 1",
                                 (user_id, shelf_id, size, fingerprint, package)).fetchone()
            if previous is not None:
                return self.view(previous)
            pending = c.execute("SELECT COUNT(*) FROM android_uploads WHERE user_id=? AND job_id IS NULL",
                                (user_id,)).fetchone()[0]
            reserved = 0
            for item in c.execute("SELECT * FROM android_uploads").fetchall():
                folder = self.folder(item["id"])
                if item["job_id"] is None or folder.is_dir() and any(folder.iterdir()):
                    reserved += item["size"]
            if pending >= 2 or reserved + size > MAX_RESERVED:
                raise UploadError(429, "Завершите или отмените предыдущую загрузку перед началом новой")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            upload_id = secrets.token_hex(16)
            folder = self.folder(upload_id)
            folder.mkdir(mode=0o700)
            try:
                c.execute("INSERT INTO android_uploads (id,user_id,shelf_id,client_id,package,name,filename,size,"
                          "fingerprint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (upload_id, user_id, shelf_id, client, package, name.strip()[:200], filename,
                           size, fingerprint, now, now))
            except BaseException:
                shutil.rmtree(folder)
                raise
            return self.view(self.get(c, upload_id, user_id, shelf_id, now))

    def part(self, c, row, index: int, raw: bytes, digest: str, now: str) -> dict:
        with LOCK:
            row = self.get(c, row["id"], row["user_id"], row["shelf_id"], now)
            if row["job_id"] is not None:
                return self.view(row)
            count = math.ceil(row["size"] / CHUNK_SIZE)
            if not 0 <= index < count or len(raw) != min(CHUNK_SIZE, row["size"] - index * CHUNK_SIZE):
                raise UploadError(400, "Неверный размер или номер части файла")
            if not DIGEST.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest:
                raise UploadError(422, "Контрольная сумма части файла не совпала")
            parts = json.loads(row["parts"])
            key = str(index)
            if key in parts:
                if parts[key] != digest:
                    raise UploadError(409, "Сохранённая часть отличается. Выберите исходный файл")
                return self.view(row)
            folder = self.folder(row["id"])
            temp, dest = folder / f"{index}.tmp", folder / f"{index}.part"
            try:
                with temp.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, dest)
                parts[key] = digest
                c.execute("UPDATE android_uploads SET parts=?,updated_at=? WHERE id=?",
                          (json.dumps(parts, sort_keys=True), now, row["id"]))
            finally:
                temp.unlink(missing_ok=True)
            return self.view(self.get(c, row["id"], row["user_id"], row["shelf_id"], now))

    def finish(self, c, row, now: str) -> dict:
        with LOCK:
            row = self.get(c, row["id"], row["user_id"], row["shelf_id"], now)
            if row["job_id"] is not None:
                return self.view(row)
            parts, count = json.loads(row["parts"]), math.ceil(row["size"] / CHUNK_SIZE)
            if len(parts) != count:
                raise UploadError(409, "Не все части файла получены. Продолжите загрузку")
            folder = self.folder(row["id"])
            temp, dest = folder / "assembled.tmp", folder / "complete.apk"
            try:
                with temp.open("wb") as output:
                    for index in range(count):
                        path = folder / f"{index}.part"
                        raw = path.read_bytes() if path.is_file() else b""
                        if hashlib.sha256(raw).hexdigest() != parts[str(index)]:
                            del parts[str(index)]
                            c.execute("UPDATE android_uploads SET parts=? WHERE id=?", (json.dumps(parts), row["id"]))
                            raise UploadError(409, "Сохранённая часть повреждена. Продолжите загрузку: повторится только эта часть")
                        output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, dest)
                with store.tx(c):
                    if (not c.execute("SELECT 1 FROM users WHERE id=?", (row["user_id"],)).fetchone()
                            or not c.execute("SELECT 1 FROM android_shelves WHERE id=?", (row["shelf_id"],)).fetchone()
                            or row["package"] and not db.get_app(c, row["shelf_id"], row["package"])):
                        raise UploadError(404, "Полка или приложение больше не существует")
                    job_id = db.enqueue(c, row["shelf_id"], "upload", now, package=row["package"],
                                        payload={"path": str(dest), "name": row["name"]})
                    c.execute("UPDATE android_uploads SET job_id=?,updated_at=? WHERE id=?", (job_id, now, row["id"]))
            finally:
                temp.unlink(missing_ok=True)
            for part in folder.glob("*.part"):
                part.unlink(missing_ok=True)
            return self.view(self.get(c, row["id"], row["user_id"], row["shelf_id"], now))

    def cancel(self, c, row) -> None:
        with LOCK:
            current = c.execute("SELECT job_id FROM android_uploads WHERE id=?", (row["id"],)).fetchone()
            if current is not None and current["job_id"] is not None:
                raise UploadError(409, "Файл уже передан на обработку")
            c.execute("DELETE FROM android_uploads WHERE id=?", (row["id"],))
            shutil.rmtree(self.folder(row["id"]), ignore_errors=True)
