"""SQLite appshelf (spec §6): история покупок, каталог, версии, задания, состояние.

База на локальном диске (/var/lib/appshelf), пишут два процесса — appshelf-web и ночная проверка:
WAL, busy_timeout и явные транзакции BEGIN IMMEDIATE (tx)."""
from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    app_id INTEGER PRIMARY KEY,
    bundle_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    purchase_date TEXT NOT NULL DEFAULT '',
    refreshed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS apps (
    app_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    app_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    build TEXT NOT NULL,
    external_version_id TEXT NOT NULL,
    min_ios TEXT NOT NULL,
    device_family TEXT NOT NULL,
    size INTEGER NOT NULL,
    dir TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    app_id INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS removed_checks (app_id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    invite_id INTEGER,
    created_at TEXT NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    storefront TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL DEFAULT 'none',
    session_since TEXT NOT NULL DEFAULT '',
    expired_mail_sent TEXT NOT NULL DEFAULT '',
    device_mac TEXT NOT NULL DEFAULT '',
    legacy_pub INTEGER NOT NULL DEFAULT 0,
    last_login_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    used_at TEXT NOT NULL DEFAULT '',
    disabled_at TEXT NOT NULL DEFAULT ''
);
"""
DIR_RE = re.compile(r"^\d+-[A-Za-z0-9._-]+$")
WORKER_KINDS = ("publish", "refresh_history", "check_removed")


class AppGone(Exception):
    """Приложение сняли с публикации, пока качалась версия."""


@dataclass(frozen=True)
class NewVersion:
    version: str
    build: str
    external_version_id: str
    min_ios: str
    device_family: str
    size: int
    dir: str
    built: str = ""


@dataclass
class AppView:
    app_id: int
    name: str
    bundle_id: str
    status: str
    last_error: str
    current: sqlite3.Row | None
    previous: sqlite3.Row | None


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30, isolation_level=None)  # транзакции — явно, через tx()
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.executescript(SCHEMA)
    for table, column in (("purchases", "version"), ("versions", "built")):  # база до 2026-10-05
        if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError as e:  # второй процесс успел первым
                if "duplicate column" not in str(e):
                    raise
    return c


@contextmanager
def tx(c):
    """BEGIN IMMEDIATE: запись сразу берёт блокировку, второй процесс ждёт (busy_timeout)."""
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


# --- состояние ---------------------------------------------------------------

def get_state(c, key: str, default: str = "") -> str:
    row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(c, key: str, value: str) -> None:
    c.execute("INSERT INTO state (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def claim_state(c, key: str, value: str) -> bool:
    """Поставить метку, если её нет. True — поставили мы: «одно письмо на эпизод» из двух процессов."""
    with tx(c):
        if get_state(c, key):
            return False
        set_state(c, key, value)
    return True


# --- история покупок ---------------------------------------------------------

def upsert_purchases(c, items: list[dict], now: str) -> int:
    n = 0
    with tx(c):
        for it in items:
            app_id = int(it.get("id") or 0)
            if app_id <= 0:
                continue
            c.execute("""INSERT INTO purchases (app_id, bundle_id, name, purchase_date, refreshed_at, version)
                         VALUES (?, ?, ?, ?, ?, ?)
                         ON CONFLICT(app_id) DO UPDATE SET bundle_id=excluded.bundle_id, name=excluded.name,
                             purchase_date=excluded.purchase_date, refreshed_at=excluded.refreshed_at,
                             version=excluded.version""",
                      (app_id, str(it.get("bundleId", "")), str(it.get("name", "")),
                       str(it.get("purchaseDate", "")), now, str(it.get("version", ""))))
            n += 1
    return n


def list_purchases(c) -> list[sqlite3.Row]:
    return c.execute("""SELECT p.*, a.app_id IS NOT NULL AS published FROM purchases p
                        LEFT JOIN apps a ON a.app_id = p.app_id
                        ORDER BY p.purchase_date DESC, p.name""").fetchall()


def history_refreshed_at(c) -> str:
    return c.execute("SELECT MAX(refreshed_at) AS t FROM purchases").fetchone()["t"] or ""


# --- каталог -----------------------------------------------------------------

def _active(c, kind: str, app_id) -> bool:
    return c.execute("SELECT 1 FROM jobs WHERE kind=? AND app_id IS ? AND status IN ('queued', 'running')",
                     (kind, app_id)).fetchone() is not None


def _enqueue(c, kind: str, app_id, now: str) -> int:
    return c.execute("INSERT INTO jobs (kind, app_id, status, created_at) VALUES (?, ?, 'queued', ?)",
                     (kind, app_id, now)).lastrowid


def publish(c, app_id: int, now: str) -> bool:
    """Строка apps (queued) и задание publish. False — уже в каталоге или нет в истории."""
    with tx(c):
        p = c.execute("SELECT name, bundle_id FROM purchases WHERE app_id=?", (app_id,)).fetchone()
        if p is None:
            return False
        cur = c.execute("INSERT OR IGNORE INTO apps (app_id, name, bundle_id, published_at, status) "
                        "VALUES (?, ?, ?, ?, 'queued')", (app_id, p["name"], p["bundle_id"], now))
        if cur.rowcount == 0:
            return False
        _enqueue(c, "publish", app_id, now)
    return True


def add_manual(c, app_id: int, now: str) -> bool:
    """Публикация по ссылке или ID App Store: удалённые из магазина приложения (СберБанк, VK, банки)
    Apple не отдаёт в истории покупок DAAP, хотя по id они скачиваются. Строка истории — заглушка
    без bundle id; имя и bundle id подставит первое скачивание (fill_names)."""
    add_known(c, app_id, f"App Store {app_id}")
    return publish(c, app_id, now)


def no_license_ids(c) -> set[int]:
    """Приложения справочника, на которые у Apple ID нет лицензии (проверены раньше)."""
    return {r["app_id"] for r in c.execute("SELECT app_id FROM removed_checks")}


def mark_no_license(c, app_id: int, now: str) -> None:
    c.execute("INSERT INTO removed_checks (app_id, checked_at) VALUES (?, ?) "
              "ON CONFLICT(app_id) DO UPDATE SET checked_at=excluded.checked_at", (app_id, now))


def set_purchase_version(c, app_id: int, version: str) -> None:
    c.execute("UPDATE purchases SET version=? WHERE app_id=?", (version, app_id))


def clear_no_license(c, app_id: int) -> None:
    c.execute("DELETE FROM removed_checks WHERE app_id=?", (app_id,))


def add_known(c, app_id: int, name: str) -> None:
    """Строка «Истории» для приложения, которого нет в DAAP (удалено из App Store): bundle id пуст."""
    c.execute("INSERT OR IGNORE INTO purchases (app_id, name, refreshed_at) VALUES (?, ?, '')", (app_id, name))


def fill_names(c, app_id: int, name: str, bundle_id: str) -> None:
    """Имя и bundle id из IPA — для строк, добавленных по ссылке (bundle id пуст)."""
    with tx(c):
        for table in ("apps", "purchases"):
            c.execute(f"UPDATE {table} SET name=?, bundle_id=? WHERE app_id=? AND bundle_id=''",
                      (name, bundle_id, app_id))


def retry(c, app_id: int, now: str) -> bool:
    with tx(c):
        if c.execute("SELECT 1 FROM apps WHERE app_id=? AND status='error'", (app_id,)).fetchone() is None:
            return False
        c.execute("UPDATE apps SET status='queued', last_error='' WHERE app_id=?", (app_id,))
        if not _active(c, "publish", app_id):
            _enqueue(c, "publish", app_id, now)
    return True


def unpublish(c, pub_root: Path, app_id: int) -> None:
    """Каталоги версий и строка apps; история покупок остаётся (spec §7.3)."""
    with tx(c):
        dirs = [r["dir"] for r in c.execute("SELECT dir FROM versions WHERE app_id=?", (app_id,))]
        c.execute("DELETE FROM versions WHERE app_id=?", (app_id,))
        c.execute("DELETE FROM apps WHERE app_id=?", (app_id,))
        c.execute("UPDATE jobs SET status='cancelled', error='снято с публикации' "
                  "WHERE app_id=? AND status='queued'", (app_id,))
    remove_dirs(pub_root, dirs)


def list_apps(c) -> list[AppView]:
    out = []
    for a in c.execute("SELECT * FROM apps").fetchall():
        vs = {r["role"]: r for r in c.execute("SELECT * FROM versions WHERE app_id=?", (a["app_id"],))}
        out.append(AppView(a["app_id"], a["name"], a["bundle_id"], a["status"], a["last_error"],
                           vs.get("current"), vs.get("previous")))
    # по алфавиту в Python: COLLATE NOCASE в SQLite не знает кириллицу («альфа» после «Яндекс»)
    return sorted(out, key=lambda v: (v.name.casefold(), v.app_id))


def apps_to_check(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM apps WHERE status IN ('ok', 'error') ORDER BY app_id").fetchall()


def set_app_status(c, app_id: int, status: str, error: str = "") -> None:
    c.execute("UPDATE apps SET status=?, last_error=? WHERE app_id=?", (status, error[:500], app_id))


def set_checked(c, app_id: int, now: str) -> None:
    c.execute("UPDATE apps SET checked_at=? WHERE app_id=?", (now, app_id))


# --- версии ------------------------------------------------------------------

def current_version(c, app_id: int):
    return c.execute("SELECT * FROM versions WHERE app_id=? AND role='current'", (app_id,)).fetchone()


def dir_in_use(c, name: str) -> bool:
    return c.execute("SELECT 1 FROM versions WHERE dir=?", (name,)).fetchone() is not None


def add_version(c, pub_root: Path, app_id: int, v: NewVersion, now: str) -> None:
    """Новая версия — current, прежняя current — previous, прежняя previous — с диска (spec §7.4)."""
    with tx(c):
        if c.execute("SELECT 1 FROM apps WHERE app_id=?", (app_id,)).fetchone() is None:
            raise AppGone(app_id)
        old = [r["dir"] for r in c.execute(
            "SELECT dir FROM versions WHERE app_id=? AND role='previous'", (app_id,))]
        c.execute("DELETE FROM versions WHERE app_id=? AND role='previous'", (app_id,))
        c.execute("UPDATE versions SET role='previous' WHERE app_id=? AND role='current'", (app_id,))
        c.execute("""INSERT INTO versions (app_id, version, build, external_version_id, min_ios, device_family,
                         size, dir, downloaded_at, built, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'current')""",
                  (app_id, v.version, v.build, v.external_version_id, v.min_ios, v.device_family,
                   v.size, v.dir, now, v.built))
        c.execute("UPDATE apps SET status='ok', last_error='' WHERE app_id=?", (app_id,))
    remove_dirs(pub_root, old)


def remove_dirs(pub_root: Path, names) -> None:
    for name in names:
        if DIR_RE.match(name):  # имя из базы, но rmtree без проверки формы не делаем
            shutil.rmtree(pub_root / name, ignore_errors=True)


# --- задания -----------------------------------------------------------------

def enqueue_once(c, kind: str, app_id, now: str) -> bool:
    """Задание, если такого же нет в очереди или в работе: двойной клик — одно задание."""
    with tx(c):
        if _active(c, kind, app_id):
            return False
        _enqueue(c, kind, app_id, now)
    return True


def take_job(c, kinds=WORKER_KINDS):
    """Публикация — раньше фоновых заданий: владелец ждёт её у экрана, а проверка справочника — минуты."""
    with tx(c):
        marks = ",".join("?" * len(kinds))
        job = c.execute(f"""SELECT * FROM jobs WHERE status='queued' AND kind IN ({marks})
                            ORDER BY CASE kind WHEN 'publish' THEN 0 WHEN 'refresh_history' THEN 1 ELSE 2 END, id
                            LIMIT 1""", tuple(kinds)).fetchone()
        if job is not None:
            c.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    return job


def start_job(c, kind: str, app_id: int, now: str) -> int:
    """Задание ночной проверки: сразу running, для истории в таблице jobs."""
    return c.execute("INSERT INTO jobs (kind, app_id, status, created_at) VALUES (?, ?, 'running', ?)",
                     (kind, app_id, now)).lastrowid


def finish_job(c, job_id: int, status: str, now: str, error: str = "") -> None:
    c.execute("UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?", (status, now, error[:500], job_id))


def has_queued(c, kind: str) -> bool:
    return c.execute("SELECT 1 FROM jobs WHERE status='queued' AND kind=? LIMIT 1", (kind,)).fetchone() is not None


def requeue_job(c, job_id: int) -> None:
    c.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))


def requeue_running(c) -> None:
    """Перезапуск appshelf-web: прерванные задания обработчика — снова в очередь, их приложения — queued.
    Задания ночной проверки (update) не трогаем: она может идти прямо сейчас."""
    with tx(c):
        c.execute("""UPDATE apps SET status='queued' WHERE app_id IN
                     (SELECT app_id FROM jobs WHERE status='running' AND kind='publish' AND app_id IS NOT NULL)""")
        c.execute("UPDATE jobs SET status='queued' WHERE status='running' AND kind IN ('publish', 'refresh_history', "
                  "'check_removed')")


def fail_stale_updates(c, now: str) -> None:
    """Начало ночной проверки: остатки прошлой, упавшей на середине."""
    with tx(c):
        c.execute("UPDATE jobs SET status='error', finished_at=?, error='прервано' "
                  "WHERE status='running' AND kind='update'", (now,))
        stale = c.execute("""SELECT a.app_id, EXISTS (SELECT 1 FROM versions v
                                 WHERE v.app_id = a.app_id AND v.role = 'current') AS has_current
                             FROM apps a WHERE a.status = 'downloading' AND NOT EXISTS
                                 (SELECT 1 FROM jobs j WHERE j.app_id = a.app_id AND j.kind = 'publish'
                                  AND j.status IN ('queued', 'running'))""").fetchall()
        for r in stale:
            if r["has_current"]:
                set_app_status(c, r["app_id"], "ok")
            else:
                set_app_status(c, r["app_id"], "error", "скачивание прервано")


def has_running(c, kind: str) -> bool:
    return c.execute("SELECT 1 FROM jobs WHERE kind=? AND status='running' LIMIT 1", (kind,)).fetchone() is not None


def refresh_status(c) -> tuple[str, str, str]:
    """Для страницы «История»: (что идёт — "refresh_history" | "check_removed" | "", ошибка последнего
    обновления истории, её время)."""
    active = next((k for k in ("refresh_history", "check_removed") if _active(c, k, None)), "")
    last = c.execute("SELECT status, error, finished_at FROM jobs WHERE kind='refresh_history' "
                     "AND status IN ('done', 'error') ORDER BY id DESC LIMIT 1").fetchone()
    if last is not None and last["status"] == "error":
        return active, last["error"], last["finished_at"]
    return active, "", ""


def has_active_jobs(c) -> bool:
    """Есть ли что ждать странице: running — всегда, queued — только когда вход выполнен."""
    statuses = ("queued", "running") if get_state(c, "session") == "ok" else ("running",)
    marks = ",".join("?" * len(statuses))
    return c.execute(f"SELECT 1 FROM jobs WHERE status IN ({marks}) LIMIT 1", statuses).fetchone() is not None
