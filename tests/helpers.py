"""Общее для тестов: токен, подменённые часы, Apple ID, наполнение базы."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from appshelf import people, store, webauth
from appshelf.ipatool import LicenseNotFound

TOKEN = "0123456789abcdef0123456789abcdef"
OWNER_EMAIL = "owner@example"
HOST = "apps.example"


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


def sign_in(client, cfg, conn, clock, uid: int = 1) -> None:
    """cookie человека uid — как после входа Apple ID (ключ — тот же cookie-key, что у приложения)."""
    key = webauth.load_key(cfg.cookie_key)
    client.cookies.set(webauth.COOKIE, webauth.make_cookie(key, people.get_user(conn, uid), int(clock.t)), domain=HOST)


def make_account(conn, clock, email=OWNER_EMAIL, role="owner", legacy=True, session="none", user_id=None,
                 name="Иван Петров") -> people.Account:
    """Человек (или ещё один Apple ID человека user_id) и его Apple ID. legacy=True — каталог архива <TOKEN>,
    как у перенесённого из версии 1; иначе — HMAC-токен."""
    uid = user_id or people.create_user(conn, name, role, None, clock.iso())
    aid = people.create_account(conn, uid, email, {"name": name, "storefront": "RU"}, "", clock.iso())
    conn.execute("UPDATE accounts SET legacy_pub=?, session=? WHERE id=?", (int(legacy), session, aid))
    return people.get_account(conn, aid)


def owner(conn, clock) -> people.Account:
    """Владелец и его Apple ID №1 (legacy: каталог <TOKEN>); создаётся при первом обращении."""
    return people.get_account(conn, 1) or make_account(conn, clock)


def set_session(conn, aid: int, session: str) -> None:
    conn.execute("UPDATE accounts SET session=? WHERE id=?", (session, aid))


def to_weekday(clock, weekday: int) -> None:
    """Часы вперёд до нужного дня недели (0 — понедельник)."""
    while datetime.fromtimestamp(clock.t, timezone.utc).weekday() != weekday:
        clock.advance(86400)


def seed_app(conn, cfg, clock, app_id=123, name="СберБанк Онлайн", bundle_id="ru.sberbank.onlineiphone",
             versions=(), make_dirs=False, acct=None):
    """Покупка + публикация (задание publish в очереди) + версии на полке acct (по умолчанию — Apple ID №1
    владельца); последняя — current, ext id 900, 901…"""
    acct = acct or owner(conn, clock)
    root = cfg.shelf_root(acct)
    store.upsert_purchases(conn, acct.id, [{"id": app_id, "bundleId": bundle_id, "name": name,
                                            "purchaseDate": "2020-01-02T03:04:05Z"}], clock.iso())
    store.publish(conn, acct.id, app_id, clock.iso())
    for i, v in enumerate(versions):
        d = f"{app_id}-{v}"
        if make_dirs:
            (root / d).mkdir(parents=True)
        store.add_version(conn, acct.id, root, app_id, store.NewVersion(
            version=v, build="1", external_version_id=str(900 + i), min_ios="13.0",
            device_family="iphone", size=291 * 1024 * 1024, dir=d, built="2026-06-18"), clock.iso())


class FakeTool:
    """Подмена ipatool.Ipatool: ответы — из полей, исключения — из errors (каждое бросается один раз).
    С home вход, как настоящий ipatool, создаёт $HOME/.ipatool, а удачный — и учётку в нём."""

    def __init__(self, home: Path | None = None):
        self.home = home
        self.calls: list[tuple] = []
        self.purchases: list[dict] = []
        self.latest: dict[int, str] = {}
        self.ipas: dict[int, Path] = {}
        self.errors: dict[str, Exception] = {}
        self.on_download = None
        self.on_login = None
        self.versions: dict[int, str] = {}       # display_version
        self.no_license: set[int] = set()   # latest_version_id → LicenseNotFound
        self.account = {"name": "Иван Петров", "email": "owner@example", "storefront": "RU", "success": True}

    def _call(self, name, *args):
        self.calls.append((name, *args))
        exc = self.errors.pop(name, None)
        if exc is not None:
            raise exc

    def login(self, email, password, auth_code="", lock_wait=None):
        if self.home is not None:
            (self.home / ".ipatool").mkdir(parents=True, exist_ok=True)
        if self.on_login is not None:
            self.on_login(email)
        self._call("login", email, password, auth_code)
        if self.home is not None:  # save_account: каталог учётки создаётся заново, если его успели удалить
            (self.home / ".ipatool").mkdir(parents=True, exist_ok=True)
            (self.home / ".ipatool" / "account").write_bytes(b"store token")
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
