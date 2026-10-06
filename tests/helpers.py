"""Общее для тестов: токен, подменённые часы, наполнение базы."""
from __future__ import annotations

import base64
import shutil
from datetime import datetime, timezone
from pathlib import Path

from appshelf import store
from appshelf.ipatool import LicenseNotFound

TOKEN = "0123456789abcdef0123456789abcdef"


class Clock:
    """Подменённые часы: iso() — для базы, monotonic() — для TTL входа."""

    def __init__(self, t: float = 1_790_000_000.0):
        self.t = t

    def iso(self) -> str:
        return datetime.fromtimestamp(self.t, timezone.utc).isoformat(timespec="seconds")

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def seed_app(conn, cfg, clock, app_id=123, name="СберБанк Онлайн", bundle_id="ru.sberbank.onlineiphone",
             versions=(), make_dirs=False):
    """Покупка + публикация (задание publish в очереди) + версии; последняя — current, ext id 900, 901…"""
    store.upsert_purchases(conn, [{"id": app_id, "bundleId": bundle_id, "name": name,
                                   "purchaseDate": "2020-01-02T03:04:05Z"}], clock.iso())
    store.publish(conn, app_id, clock.iso())
    for i, v in enumerate(versions):
        d = f"{app_id}-{v}"
        if make_dirs:
            (cfg.pub_root / d).mkdir(parents=True)
        store.add_version(conn, cfg.pub_root, app_id, store.NewVersion(
            version=v, build="1", external_version_id=str(900 + i), min_ios="13.0",
            device_family="iphone", size=291 * 1024 * 1024, dir=d, built="2026-06-18"), clock.iso())


class FakeTool:
    """Подмена ipatool.Ipatool: ответы — из полей, исключения — из errors (каждое бросается один раз)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.purchases: list[dict] = []
        self.latest: dict[int, str] = {}
        self.ipas: dict[int, Path] = {}
        self.errors: dict[str, Exception] = {}
        self.on_download = None
        self.versions: dict[int, str] = {}       # display_version
        self.no_license: set[int] = set()   # latest_version_id → LicenseNotFound
        self.account = {"name": "Иван Петров", "email": "owner@example", "storefront": "RU", "success": True}

    def _call(self, name, *args):
        self.calls.append((name, *args))
        exc = self.errors.pop(name, None)
        if exc is not None:
            raise exc

    def login(self, email, password, auth_code="", lock_wait=None):
        self._call("login", email, password, auth_code)
        return self.account

    def list_purchases(self):
        self._call("list_purchases")
        return self.purchases

    def latest_version_id(self, app_id):
        self._call("latest_version_id", app_id)
        if app_id in self.no_license:
            raise LicenseNotFound("license_not_found", "this Apple ID has no license for the app")
        return self.latest[app_id]

    def display_version(self, app_id, external_id):
        self._call("display_version", app_id, external_id)
        return self.versions.get(app_id, "")

    def download(self, app_id, out_dir):
        self._call("download", app_id)
        if self.on_download is not None:
            self.on_download(app_id)
        dest = Path(out_dir) / f"{app_id}.ipa"
        shutil.copyfile(self.ipas[app_id], dest)
        return dest
