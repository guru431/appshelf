"""Проверка целостности полок (appshelf check) и снимок базы для восстановления (appshelf backup).

Проверка — только чтение: ни одной записи в базу и архив, сироты не удаляются (автоматическая уборка отклонена —
IDEAS-archive.md). По каждой строке versions — каталог, файлы, размер, ссылки manifest; deep — ещё SHA-256 и
содержимое IPA. Сироты — каталоги без строк, .partial, незавершённые переносы, чужие каталоги архива.
В отчёте Apple ID — по номеру (accounts.id), не по почте; токенов полок нет: каталог полки — «<токен>»,
неизвестный каталог архива — маской. Android-полки (spec 2026-10-28 §7) — по номеру полки: строки android_versions
против каталогов <пакет>-<versionCode>, deep — ещё SHA-256 APK."""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import ipa, jobs, people, store
from .android import db as android_db, fetch as android_fetch
from .config import TOKEN_RE, Config

DAILY_RE = re.compile(r"^appshelf-\d{4}-\d{2}-\d{2}\.db$")

KINDS = {  # вид проблемы → подпись в кратком итоге; порядок — порядок итога
    "no_shelf": "нет каталога полки, а версии в базе есть",
    "no_dir": "нет каталога версии",
    "bad_dir": "недопустимое имя каталога версии в базе",
    "no_ipa": "нет app.ipa",
    "size": "размер app.ipa не совпадает с базой",
    "no_icon": "нет icon.png",
    "no_manifest": "нет manifest.plist",
    "bad_manifest": "manifest.plist не разбирается",
    "manifest_url": "ссылка в manifest.plist не та",
    "manifest_version": "bundle-version в manifest.plist не совпадает с базой",
    "sha256": "SHA-256 app.ipa не совпадает с базой",
    "bad_ipa": "app.ipa не разбирается",
    "ipa_item_id": "itemId в IPA не совпадает с базой",
    "ipa_external_id": "external version id в IPA не совпадает с базой",
    "ipa_bundle_id": "bundle id в IPA не совпадает с manifest.plist",
    "no_apk": "нет файла APK",
    "apk_size": "размер APK не совпадает с базой",
    "apk_sha256": "SHA-256 APK не совпадает с базой",
    "orphan": "в полке, но нет строки в базе",
    "partial": "незавершённое скачивание (.partial)",
    "shelf_next": "незавершённый перенос каталога полки (доделает старт appshelf-web)",
    "no_account": "строки версий без Apple ID",
    "unknown": "каталог архива не принадлежит ни одному Apple ID",
}


class DbError(Exception):
    """Базу или снимок не открыть на чтение: нет файла, не SQLite, другая версия схемы."""


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class Problem:
    kind: str
    account: int | None  # accounts.id; None — архив целиком или Android-полка
    path: str = ""       # от каталога полки: <dir>/app.ipa; у unknown — маска имени
    detail: str = ""
    shelf: int | None = None  # android_shelves.id — проблема Android-полки


@dataclass
class Report:
    deep: bool = False
    versions: int = 0
    accounts: int = 0
    no_sha256: int = 0   # deep: версии без сохранённой SHA-256 (скачаны до версии 4 схемы)
    apks: int = 0
    android_shelves: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def add(self, kind: str, account: int | None, path: str = "", detail: str = "", shelf: int | None = None) -> None:
        # страховка: ни одного токена полки в отчёте, что бы ни лежало в именах и manifest
        self.problems.append(Problem(kind, account, TOKEN_RE.sub("<токен>", path), TOKEN_RE.sub("<токен>", detail),
                                     shelf))

    def as_dict(self) -> dict:
        return {"ok": self.ok, "deep": self.deep, "versions": self.versions, "accounts": self.accounts,
                "no_sha256": self.no_sha256, "apks": self.apks, "android_shelves": self.android_shelves,
                "counts": dict(Counter(p.kind for p in self.problems)),
                "problems": [asdict(p) for p in self.problems]}

    def text(self) -> str:
        head = f"проверено версий: {self.versions}, Apple ID: {self.accounts}"
        if self.android_shelves:
            head += f"; APK: {self.apks}, Android-полок: {self.android_shelves}"
        head += f"; без сохранённой SHA-256: {self.no_sha256}" if self.deep else \
            "; SHA-256 и содержимое IPA не проверялись (--deep)"
        if self.ok:
            return f"{head}\nпроблем нет"
        lines = [head, f"проблем: {len(self.problems)}"]
        by_kind = defaultdict(list)
        for p in self.problems:
            by_kind[p.kind].append(p)
        for kind in KINDS:
            if kind not in by_kind:
                continue
            lines.append(f"{KINDS[kind]} — {len(by_kind[kind])}:")
            for p in by_kind[kind]:
                who = f"Apple ID №{p.account}: " if p.account is not None else                     f"Android-полка №{p.shelf}: " if p.shelf is not None else ""
                lines.append(f"  {who}{p.path}{' — ' + p.detail if p.detail else ''}")
        return "\n".join(lines)


def open_ro(path: Path) -> sqlite3.Connection:
    """База только на чтение (URI mode=ro): ни перехода схемы, ни записи в WAL. Схема — ровно этой версии appshelf."""
    if not path.is_file():
        raise DbError(f"базы {path} нет")
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version != store.SCHEMA_VERSION:
            raise DbError(f"{path}: схема версии {version}, а этой версии appshelf нужна {store.SCHEMA_VERSION}")
    except sqlite3.DatabaseError as e:
        c.close()
        raise DbError(f"{path}: не база SQLite ({e})") from e
    except BaseException:
        c.close()
        raise
    return c


def check(cfg: Config, c, deep: bool = False) -> Report:
    """Полки всех Apple ID против строк versions и чужое в архиве. Архив недоступен — ArchiveUnavailable."""
    jobs._archive(cfg)
    rep = Report(deep=deep)
    rows = defaultdict(list)
    for r in c.execute("SELECT * FROM versions ORDER BY account_id, app_id, id"):
        rows[r["account_id"]].append(r)
        rep.versions += 1
    owned = set()
    for acct in people.all_accounts(c):
        rep.accounts += 1
        owned.add(cfg.shelf_token(acct))
        if acct.shelf_next:
            owned.add(acct.shelf_next)
            rep.add("shelf_next", acct.id)
        _shelf(cfg, acct, rows.pop(acct.id, []), deep, rep)
    for aid, vs in rows.items():
        rep.add("no_account", aid, detail=f"строк: {len(vs)}")
    apks = defaultdict(list)
    for r in c.execute("SELECT * FROM android_versions ORDER BY shelf_id, package, id"):
        apks[r["shelf_id"]].append(r)
        rep.apks += 1
    for shelf in android_db.all_shelves(c):
        rep.android_shelves += 1
        owned.add(shelf.shelf)
        if shelf.shelf_next:
            owned.add(shelf.shelf_next)
            rep.add("shelf_next", None, shelf=shelf.id)
        _android_shelf(cfg, shelf, apks.pop(shelf.id, []), deep, rep)
    for p in sorted(cfg.archive.iterdir()):
        if p.name not in owned:
            rep.add("unknown", None, p.name[:4] + "…")
    return rep


def _shelf(cfg: Config, acct, versions: list, deep: bool, rep: Report) -> None:
    root = cfg.shelf_root(acct)
    if not root.is_dir():
        if versions:
            rep.add("no_shelf", acct.id, detail=f"версий в базе: {len(versions)}")
        return
    for v in versions:
        _version(cfg, acct, root, v, deep, rep)
    known = {v["dir"] for v in versions}
    for p in sorted(root.iterdir()):
        if p.name not in known:
            rep.add("partial" if p.name.startswith(".") and p.name.endswith(".partial") else "orphan", acct.id, p.name)


def _version(cfg: Config, acct, root: Path, v, deep: bool, rep: Report) -> None:
    d, aid = v["dir"], acct.id
    if not store.DIR_RE.match(d):  # имя из базы — к пути не приклеиваем без проверки формы
        rep.add("bad_dir", aid, d)
        return
    vd = root / d
    if not vd.is_dir():
        rep.add("no_dir", aid, d)
        return
    app = vd / "app.ipa"
    if not app.is_file():
        rep.add("no_ipa", aid, f"{d}/app.ipa")
    elif (size := app.stat().st_size) != v["size"]:
        rep.add("size", aid, f"{d}/app.ipa", f"на диске {size}, в базе {v['size']}")
    if not (vd / "icon.png").is_file():
        rep.add("no_icon", aid, f"{d}/icon.png")
    bundle = _manifest(cfg, acct, vd, v, rep)
    if deep and app.is_file():
        _deep(app, v, bundle, aid, f"{d}/app.ipa", rep)


def _manifest(cfg: Config, acct, vd: Path, v, rep: Report) -> str | None:
    """Ссылки и bundle-version manifest.plist; bundle-identifier — для сверки с IPA (None — не разобран)."""
    path, aid = f"{v['dir']}/manifest.plist", acct.id
    m = vd / "manifest.plist"
    if not m.is_file():
        rep.add("no_manifest", aid, path)
        return None
    try:
        item = plistlib.loads(m.read_bytes())["items"][0]
        urls = {a["kind"]: a["url"] for a in item["assets"]}
        bundle, version = str(item["metadata"]["bundle-identifier"]), str(item["metadata"]["bundle-version"])
    except Exception as e:  # битый plist или не та структура
        rep.add("bad_manifest", aid, path, type(e).__name__)
        return None
    token = cfg.shelf_token(acct)
    for kind, file in (("software-package", "app.ipa"), ("display-image", "icon.png")):
        want, got = cfg.shelf_url(acct, v["dir"], file), str(urls.get(kind) or "")
        if got != want:  # свой токен — «<токен>», любой другой — «<другой токен>»: видно, что ссылка устарела
            got = TOKEN_RE.sub("<другой токен>", got.replace(token, "<токен>")) or "нет"
            rep.add("manifest_url", aid, path, f"{kind}: {got}, нужно {want.replace(token, '<токен>')}")
    if version != v["version"]:
        rep.add("manifest_version", aid, path, f"{version}, в базе {v['version']}")
    return bundle


def _deep(app: Path, v, bundle: str | None, aid: int, path: str, rep: Report) -> None:
    if not v["sha256"]:
        rep.no_sha256 += 1
    elif ipa.sha256(app) != v["sha256"]:
        rep.add("sha256", aid, path)
    try:
        info = ipa.read_info(app)
    except Exception as e:  # IpaError, битый zip или plist
        rep.add("bad_ipa", aid, path, str(e)[:200] if isinstance(e, ipa.IpaError) else type(e).__name__)
        return
    if info.item_id != v["app_id"]:
        rep.add("ipa_item_id", aid, path, f"в IPA {info.item_id}, в базе {v['app_id']}")
    if info.external_version_id != v["external_version_id"]:
        rep.add("ipa_external_id", aid, path, f"в IPA {info.external_version_id}, в базе {v['external_version_id']}")
    if bundle is not None and info.bundle_id != bundle:
        rep.add("ipa_bundle_id", aid, path, f"в IPA {info.bundle_id}, в manifest {bundle}")


def _android_shelf(cfg: Config, shelf, versions: list, deep: bool, rep: Report) -> None:
    root = cfg.archive / shelf.shelf
    if not root.is_dir():
        if versions:
            rep.add("no_shelf", None, detail=f"версий в базе: {len(versions)}", shelf=shelf.id)
        return
    for v in versions:
        _apk(root, shelf.id, v, deep, rep)
    known = {v["dir"] for v in versions}
    for p in sorted(root.iterdir()):
        if p.name not in known:
            rep.add("partial" if p.name.startswith(".") and p.name.endswith(".partial") else "orphan", None, p.name,
                    shelf=shelf.id)


def _apk(root: Path, sid: int, v, deep: bool, rep: Report) -> None:
    d = v["dir"]
    if not android_db.ANDROID_DIR_RE.match(d):  # имя из базы — к пути не приклеиваем без проверки формы
        rep.add("bad_dir", None, d, shelf=sid)
        return
    vd = root / d
    if not vd.is_dir():
        rep.add("no_dir", None, d, shelf=sid)
        return
    name = android_fetch.apk_name(v["package"], v["version_name"])
    f = vd / name
    if not f.is_file():
        rep.add("no_apk", None, f"{d}/{name}", shelf=sid)
    elif (size := f.stat().st_size) != v["size"]:
        rep.add("apk_size", None, f"{d}/{name}", f"на диске {size}, в базе {v['size']}", shelf=sid)
    elif deep and ipa.sha256(f) != v["sha256"]:
        rep.add("apk_sha256", None, f"{d}/{name}", shelf=sid)
    if not (vd / "icon.png").is_file():
        rep.add("no_icon", None, f"{d}/icon.png", shelf=sid)


def backup(db: Path, out: Path) -> Path:
    """Согласованный снимок живой базы (sqlite3 backup API) в out/appshelf.db, 0600; out создаётся или пуст.
    Перечень версий и SHA-256 — в самом снимке (versions); проверка копии — check по снимку и копии архива.
    Снимок — один файл без WAL; пишется во временный и переименовывается: оборванный не выглядит готовым."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise BackupError(f"каталог {out} не пуст — нужен новый или пустой")
    tmp, dst = out / ".appshelf.db.tmp", out / "appshelf.db"
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))  # 0600 сразу: в снимке люди и токены полок
    try:
        with closing(open_ro(db)) as src, closing(sqlite3.connect(tmp)) as d:
            src.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")  # живая база в WAL — флаг переехал бы в снимок
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst


def daily_backup(db: Path, dest: Path, work: Path, day: str, keep: int) -> Path:
    """Ночной снимок (appshelf-backup.timer): dest/appshelf-<день>.db, хранятся keep последних, чужие файлы dest не
    трогаются. Снимок — локально в work, на шару — готовой копией: SQLite на CIFS пишет ненадёжно. dest должен уже
    быть: при несмонтированной шаре его нет, и снимок не ляжет на локальный диск под пустой точкой монтирования."""
    if not dest.is_dir():
        raise BackupError(f"нет каталога {dest} — смонтирована ли шара? Каталог создаётся один раз, при установке")
    final = dest / f"appshelf-{day}.db"
    tmp = dest / f".{final.name}.tmp"
    shutil.rmtree(work, ignore_errors=True)
    try:
        shutil.copyfile(backup(db, work), tmp)
        with suppress(OSError):  # на шаре с noperm права задаёт монтирование
            tmp.chmod(0o600)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for old in sorted(p for p in dest.iterdir() if DAILY_RE.match(p.name))[:-keep]:
        old.unlink()
    return final
