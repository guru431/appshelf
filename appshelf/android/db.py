"""Таблицы Android (spec 2026-10-28 §7, §9, §12): полки участников, приложения, версии APK, задания.

Полка — у участника (user_id; NULL — публичная, этап 2), каталог — <архив>/<shelf>/<пакет>-<versionCode>[-N]/.
Роли версий — как у IPA (current, previous, kept) и pending: новая версия с другой подписью ждёт «Принять новую
подпись». Транзакции и удаление каталогов после COMMIT — как в store."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..store import AppGone, rmtree_logged, tx

ANDROID_DIR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+-\d+(-\d+)?$")
PLAY_OK = "SELECT id FROM google_accounts WHERE play = 'ok'"
# Google-аккаунт задания Play: из payload add, иначе — карточки Play («Повторить», проверка); у аккаунта нет
# подключения — задание ждёт (spec §9). Аккаунта нет вовсе — задание идёт и падает ошибкой «удалён»
JOB_ACCOUNT = """COALESCE(CASE WHEN json_valid(j.payload) AND json_extract(j.payload, '$.source') = 'play'
                               THEN json_extract(j.payload, '$.account') END,
                          (SELECT a.google_account_id FROM android_apps a WHERE a.shelf_id = j.shelf_id
                               AND a.package = j.package AND a.source = 'play'))"""
RUNNABLE = f"NOT EXISTS (SELECT 1 FROM google_accounts g WHERE g.id = {JOB_ACCOUNT} AND g.play != 'ok')"


@dataclass(frozen=True)
class Shelf:
    id: int
    user_id: int | None
    shelf: str
    shelf_next: str


@dataclass(frozen=True)
class NewApk:
    version_name: str
    version_code: int
    min_sdk: int
    target_sdk: int
    abis: str
    signer: str
    source: str
    size: int
    sha256: str
    dir: str


@dataclass
class AppView:
    package: str
    name: str
    source: str
    source_ref: str
    signer: str
    status: str
    last_error: str
    checked_at: str
    check_result: str
    current: sqlite3.Row | None
    previous: sqlite3.Row | None
    kept: sqlite3.Row | None
    pending: sqlite3.Row | None
    paid: bool = False      # Google Play: купленное


# --- полки -------------------------------------------------------------------

def _shelf(row) -> Shelf | None:
    return None if row is None else Shelf(row["id"], row["user_id"], row["shelf"], row["shelf_next"])


def shelf_for_user(c, user_id: int) -> Shelf | None:
    return _shelf(c.execute("SELECT * FROM android_shelves WHERE user_id=?", (user_id,)).fetchone())


def ensure_shelf(c, user_id: int, now: str) -> Shelf:
    """Полка появляется при первом добавлении; токен каталога — случайный, как accounts.shelf."""
    c.execute("INSERT OR IGNORE INTO android_shelves (user_id, shelf, created_at) VALUES (?, ?, ?)",
              (user_id, secrets.token_hex(16), now))
    return shelf_for_user(c, user_id)


def public_shelf(c) -> Shelf | None:
    return _shelf(c.execute("SELECT * FROM android_shelves WHERE user_id IS NULL").fetchone())


def ensure_public_shelf(c, now: str) -> Shelf:
    """Публичная полка — одна; UNIQUE на NULL не срабатывает, поэтому поиск и вставка — в одной транзакции."""
    with tx(c):
        shelf = public_shelf(c)
        if shelf is None:
            c.execute("INSERT INTO android_shelves (user_id, shelf, created_at) VALUES (NULL, ?, ?)",
                      (secrets.token_hex(16), now))
            shelf = public_shelf(c)
    return shelf


def set_shelf_next(c, shelf_id: int, token: str) -> None:
    c.execute("UPDATE android_shelves SET shelf_next=? WHERE id=?", (token, shelf_id))


def finish_shelf_move(c, shelf_id: int) -> None:
    c.execute("UPDATE android_shelves SET shelf=shelf_next, shelf_next='' WHERE id=? AND shelf_next != ''",
              (shelf_id,))


def get_shelf(c, shelf_id: int) -> Shelf | None:
    return _shelf(c.execute("SELECT * FROM android_shelves WHERE id=?", (shelf_id,)).fetchone())


def all_shelves(c) -> list[Shelf]:
    return [_shelf(r) for r in c.execute("SELECT * FROM android_shelves ORDER BY id")]


def delete_shelf(c, shelf_id: int) -> None:
    """Строки полки целиком. Без транзакции: вызывающий удаляет их вместе с участником, каталог — после COMMIT."""
    for table in ("android_versions", "android_apps", "android_jobs"):
        c.execute(f"DELETE FROM {table} WHERE shelf_id=?", (shelf_id,))
    c.execute("DELETE FROM android_shelves WHERE id=?", (shelf_id,))


# --- приложения --------------------------------------------------------------

def get_app(c, shelf_id: int, package: str):
    return c.execute("SELECT * FROM android_apps WHERE shelf_id=? AND package=?", (shelf_id, package)).fetchone()


def put_app(c, shelf_id: int, package: str, name: str, source: str, source_ref: str, now: str,
            google_account_id: int | None = None, paid: bool = False) -> None:
    """Карточка (queued) — до первой версии; есть — не трогаем. Google Play — каким аккаунтом качать и купленное ли."""
    c.execute("INSERT OR IGNORE INTO android_apps (shelf_id, package, name, source, source_ref, status, published_at, "
              "google_account_id, paid) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
              (shelf_id, package, name, source, source_ref, now, google_account_id, int(paid)))


def set_name(c, shelf_id: int, package: str, name: str) -> None:
    c.execute("UPDATE android_apps SET name=? WHERE shelf_id=? AND package=?", (name, shelf_id, package))


def set_paid(c, shelf_id: int, package: str, paid: bool) -> None:
    """Купленное ли — по ответу Google Play при скачивании."""
    c.execute("UPDATE android_apps SET paid=? WHERE shelf_id=? AND package=?", (int(paid), shelf_id, package))


def set_status(c, shelf_id: int, package: str, status: str, error: str = "") -> None:
    c.execute("UPDATE android_apps SET status=?, last_error=? WHERE shelf_id=? AND package=?",
              (status, error[:500], shelf_id, package))


def set_checked(c, shelf_id: int, package: str, now: str, result: str, etag: str | None = None) -> None:
    """Итог проверки обновления (same | updated | pending | error); etag — признаки файла по ссылке, None — прежние."""
    c.execute("UPDATE android_apps SET checked_at=?, check_result=?, source_etag=COALESCE(?, source_etag) "
              "WHERE shelf_id=? AND package=?", (now, result, etag, shelf_id, package))


def set_etag(c, shelf_id: int, package: str, etag: str) -> None:
    """Признаки файла по ссылке после первого скачивания: первая ночь не качает его заново."""
    c.execute("UPDATE android_apps SET source_etag=? WHERE shelf_id=? AND package=?", (etag, shelf_id, package))


def set_source(c, shelf_id: int, package: str, source: str, source_ref: str) -> None:
    """Сменить источник карточки: версии остаются, признаки прежней ссылки — нет."""
    c.execute("UPDATE android_apps SET source=?, source_ref=?, source_etag='' WHERE shelf_id=? AND package=?",
              (source, source_ref, shelf_id, package))


def list_apps(c, shelf_id: int) -> list[AppView]:
    out = []
    for a in c.execute("SELECT * FROM android_apps WHERE shelf_id=?", (shelf_id,)).fetchall():
        vs = {r["role"]: r for r in c.execute("SELECT * FROM android_versions WHERE shelf_id=? AND package=?",
                                              (shelf_id, a["package"]))}
        out.append(AppView(a["package"], a["name"], a["source"], a["source_ref"], a["signer"], a["status"],
                           a["last_error"], a["checked_at"], a["check_result"], vs.get("current"),
                           vs.get("previous"), vs.get("kept"), vs.get("pending"), bool(a["paid"])))
    return sorted(out, key=lambda v: (v.name.casefold(), v.package))


def has_current(c, shelf_id: int, package: str) -> bool:
    return c.execute("SELECT 1 FROM android_versions WHERE shelf_id=? AND package=? AND role='current'",
                     (shelf_id, package)).fetchone() is not None


def apps_to_check(c) -> list[sqlite3.Row]:
    """Ночная проверка: все полки; свой файл (upload) не проверяется, Google Play — только аккаунтом с действующим
    подключением."""
    return c.execute("SELECT * FROM android_apps WHERE status IN ('ok', 'error') AND (source IN ('rustore', 'link') "
                     f"OR source = 'play' AND google_account_id IN ({PLAY_OK})) ORDER BY shelf_id, package").fetchall()


def remove_app(c, root: Path, shelf_id: int, package: str) -> None:
    """Снять с полки: строки версий и карточки, ждущие задания — cancelled, каталоги — после COMMIT."""
    with tx(c):
        dirs = [r["dir"] for r in c.execute("SELECT dir FROM android_versions WHERE shelf_id=? AND package=?",
                                            (shelf_id, package))]
        c.execute("DELETE FROM android_versions WHERE shelf_id=? AND package=?", (shelf_id, package))
        c.execute("DELETE FROM android_apps WHERE shelf_id=? AND package=?", (shelf_id, package))
        c.execute("UPDATE android_jobs SET status='cancelled', error='снято с полки' "
                  "WHERE shelf_id=? AND package=? AND status='queued'", (shelf_id, package))
    remove_dirs(root, dirs)


# --- версии ------------------------------------------------------------------

def version_dir(c, shelf_id: int, root: Path, package: str, version_code: int) -> str:
    """<пакет>-<versionCode>; занято строкой этой полки или каталогом на диске — -2, -3…"""
    base = name = f"{package}-{version_code}"
    n = 1
    while (c.execute("SELECT 1 FROM android_versions WHERE shelf_id=? AND dir=?", (shelf_id, name)).fetchone()
           is not None or (root / name).exists()):
        n += 1
        name = f"{base}-{n}"
    return name


def _rotate(c, shelf_id: int, package: str) -> list[str]:
    """Место для новой current: незакреплённая previous — прочь (каталог — вызывающему после COMMIT), закреплённая —
    kept, current — previous."""
    old = [r["dir"] for r in c.execute("SELECT dir FROM android_versions WHERE shelf_id=? AND package=? "
                                       "AND role='previous' AND NOT pinned", (shelf_id, package))]
    c.execute("DELETE FROM android_versions WHERE shelf_id=? AND package=? AND role='previous' AND NOT pinned",
              (shelf_id, package))
    c.execute("UPDATE android_versions SET role='kept' WHERE shelf_id=? AND package=? AND role='previous'",
              (shelf_id, package))
    c.execute("UPDATE android_versions SET role='previous' WHERE shelf_id=? AND package=? AND role='current'",
              (shelf_id, package))
    return old


def add_version(c, root: Path, shelf_id: int, package: str, v: NewApk, now: str) -> str:
    """Новая версия: 'current' — с ротацией, как у IPA; 'pending' — подпись не та, что у приложения (§7): текущей не
    становится, прежняя pending — прочь. Первая версия задаёт подпись приложения. Карточки нет — AppGone."""
    with tx(c):
        app = get_app(c, shelf_id, package)
        if app is None:
            raise AppGone(package)
        if app["signer"] and app["signer"] != v.signer:
            role = "pending"
            old = [r["dir"] for r in c.execute("SELECT dir FROM android_versions WHERE shelf_id=? AND package=? "
                                               "AND role='pending'", (shelf_id, package))]
            c.execute("DELETE FROM android_versions WHERE shelf_id=? AND package=? AND role='pending'",
                      (shelf_id, package))
        else:
            role = "current"
            old = _rotate(c, shelf_id, package)
            c.execute("UPDATE android_apps SET signer=? WHERE shelf_id=? AND package=?", (v.signer, shelf_id, package))
        c.execute("""INSERT INTO android_versions (shelf_id, package, version_name, version_code, min_sdk, target_sdk,
                         abis, signer, source, size, sha256, dir, downloaded_at, role)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (shelf_id, package, v.version_name, v.version_code, v.min_sdk, v.target_sdk, v.abis, v.signer,
                   v.source, v.size, v.sha256, v.dir, now, role))
        set_status(c, shelf_id, package, "ok")
    remove_dirs(root, old)
    return role


def accept_pending(c, root: Path, shelf_id: int, package: str) -> bool:
    """«Принять новую подпись»: pending — current с ротацией, подпись приложения — её. False — нечего принимать."""
    with tx(c):
        p = c.execute("SELECT id, signer FROM android_versions WHERE shelf_id=? AND package=? AND role='pending'",
                      (shelf_id, package)).fetchone()
        if p is None:
            return False
        old = _rotate(c, shelf_id, package)
        c.execute("UPDATE android_versions SET role='current' WHERE id=?", (p["id"],))
        c.execute("UPDATE android_apps SET signer=? WHERE shelf_id=? AND package=?", (p["signer"], shelf_id, package))
    remove_dirs(root, old)
    return True


def _version_row(c, shelf_id: int, package: str, version_id: int):
    """Версия приложения этой полки; pending не закрепляется — её заменит следующая."""
    return c.execute("SELECT id, role, dir, pinned FROM android_versions WHERE id=? AND shelf_id=? AND package=? "
                     "AND role != 'pending'", (version_id, shelf_id, package)).fetchone()


def _unpin(c, rows) -> list[str]:
    dirs = []
    for r in rows:
        if r["role"] == "kept":
            c.execute("DELETE FROM android_versions WHERE id=?", (r["id"],))
            dirs.append(r["dir"])
        else:
            c.execute("UPDATE android_versions SET pinned=0 WHERE id=?", (r["id"],))
    return dirs


def pin_version(c, root: Path, shelf_id: int, package: str, version_id: int) -> bool:
    """Как store.pin_version: одна закреплённая на приложение, прежняя kept — с диска."""
    with tx(c):
        row = _version_row(c, shelf_id, package, version_id)
        if row is None:
            return False
        old = [] if row["pinned"] else _unpin(c, c.execute(
            "SELECT id, role, dir FROM android_versions WHERE shelf_id=? AND package=? AND pinned",
            (shelf_id, package)).fetchall())
        c.execute("UPDATE android_versions SET pinned=1 WHERE id=?", (version_id,))
    remove_dirs(root, old)
    return True


def unpin_version(c, root: Path, shelf_id: int, package: str, version_id: int) -> bool:
    with tx(c):
        row = _version_row(c, shelf_id, package, version_id)
        if row is None:
            return False
        old = _unpin(c, [row]) if row["pinned"] else []
    remove_dirs(root, old)
    return True


def remove_dirs(root: Path, names) -> None:
    for name in names:
        if ANDROID_DIR_RE.match(name):  # имя из базы, но rmtree без проверки формы не делаем
            rmtree_logged(root / name)


# --- задания -----------------------------------------------------------------

def enqueue(c, shelf_id: int, kind: str, now: str, package: str = "", payload: dict | None = None) -> int:
    """add — payload {source, ref, name}; upload — {path, name, package?}; check — package."""
    return c.execute("INSERT INTO android_jobs (shelf_id, kind, package, payload, status, created_at) "
                     "VALUES (?, ?, ?, ?, 'queued', ?)",
                     (shelf_id, kind, package, json.dumps(payload, ensure_ascii=False) if payload else "",
                      now)).lastrowid


def take_job(c):
    with tx(c):
        job = c.execute(f"SELECT * FROM android_jobs j WHERE status='queued' AND {RUNNABLE} ORDER BY id LIMIT 1"
                        ).fetchone()
        if job is not None:
            c.execute("UPDATE android_jobs SET status='running' WHERE id=?", (job["id"],))
    return job


def requeue_job(c, job_id: int) -> None:
    """Подключение к Play истекло посреди задания: снова в очередь — возьмётся, когда подключат заново."""
    c.execute("UPDATE android_jobs SET status='queued' WHERE id=?", (job_id,))


def finish_job(c, job_id: int, status: str, now: str, error: str = "") -> None:
    c.execute("UPDATE android_jobs SET status=?, finished_at=?, error=? WHERE id=?", (status, now, error[:500], job_id))


def requeue_running(c) -> None:
    """Перезапуск appshelf-web: прерванные задания — снова в очередь."""
    c.execute("UPDATE android_jobs SET status='queued' WHERE status='running'")


def open_jobs(c, shelf_id: int) -> list[sqlite3.Row]:
    """Задания добавления, которые видны на странице: ждут, идут или упали (до «Скрыть»)."""
    return c.execute("SELECT * FROM android_jobs WHERE shelf_id=? AND kind IN ('add', 'upload') "
                     "AND status IN ('queued', 'running', 'error') ORDER BY id", (shelf_id,)).fetchall()


def dismiss_job(c, shelf_id: int, job_id: int) -> bool:
    return c.execute("UPDATE android_jobs SET status='dismissed' WHERE id=? AND shelf_id=? AND status='error'",
                     (job_id, shelf_id)).rowcount > 0


def active_for(c, shelf_id: int, package: str) -> bool:
    """Есть ли у пакета задание в очереди или в работе: двойной клик — одно задание."""
    return c.execute("SELECT 1 FROM android_jobs WHERE shelf_id=? AND package=? AND status IN ('queued', 'running') "
                     "LIMIT 1", (shelf_id, package)).fetchone() is not None


def has_active(c, shelf_id: int) -> bool:
    return c.execute("SELECT 1 FROM android_jobs WHERE shelf_id=? AND status IN ('queued', 'running') LIMIT 1",
                     (shelf_id,)).fetchone() is not None
