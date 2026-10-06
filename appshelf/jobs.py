"""Задания и общий путь скачивания версии (spec §7). Обработчик — поток в appshelf-web; ночная проверка
(nightly.py) вызывает fetch_version в своём процессе. Каталоги tmp/web и tmp/nightly у каждого свои."""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import ipa, notify, removed, store
from .config import GB, Config
from .ipatool import IpatoolError, LicenseNotFound, SessionExpired, try_lock

FREE_MIN = 4 * GB      # 3 ГБ неприкосновенного запаса + 1 ГБ на файл: размер IPA до скачивания неизвестен
BANNER_FREE = 3 * GB   # баннер на каталоге
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
EXPIRED_SUBJECT = "appshelf: нужен вход в Apple ID"
LOW_SPACE_SUBJECT = f"appshelf: мало места на {notify.HOST}"


class LowSpace(Exception):
    pass


class ArchiveUnavailable(Exception):
    """Каталога архива нет: шара не смонтирована. Писать под пустую точку монтирования — забить локальный диск."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def free_space(env: Env) -> int:
    """Меньшее из свободного места: локальный tmp (скачивание) и архив (может быть сетевой шарой)."""
    archive = env.cfg.pub_root.parent
    if not archive.is_dir():
        raise ArchiveUnavailable(f"архив {archive} недоступен — смонтирован ли он?")
    return min(env.disk_free(env.cfg.data_dir), env.disk_free(archive))


@contextmanager
def nightly_lock(cfg: Config):
    """Замок ночной проверки на весь её прогон; yield True — взяли, значит другой проверки нет.
    Ядро снимает flock со смертью процесса, поэтому свободный замок = упавшая проверка не идёт."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.data_dir / "nightly.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        yield try_lock(fd)
    finally:
        os.close(fd)


@dataclass
class Env:
    cfg: Config
    tool: object                                   # ipatool.Ipatool или подмена в тестах
    now: Callable[[], str] = now_iso
    send: Callable[[str, str, str], None] = notify.send_mail
    disk_free: Callable[[Path], int] = free_bytes


def describe(e: Exception) -> str:
    """Текст ошибки для карточки и jobs.error (пароль сюда не попадает: вход — не задание)."""
    if isinstance(e, LicenseNotFound):
        return "у Apple ID нет лицензии на это приложение"
    if isinstance(e, IpatoolError):
        return f"ipatool: {e.message}"[:500]
    if isinstance(e, (LowSpace, ArchiveUnavailable, ipa.IpaError)):
        return str(e)[:500]
    return f"{type(e).__name__}: {e}"[:500]


def mark_expired(env: Env, c) -> None:
    now = env.now()
    if store.get_state(c, "session") != "expired":
        store.set_state(c, "session", "expired")
        store.set_state(c, "session_since", now)
    notify.once(c, notify.EXPIRED_KEY, EXPIRED_SUBJECT,
                "Токен App Store истёк: публикация и ночная проверка стоят, задания ждут.\n"
                f"Войти: {env.cfg.public_base}/apple\n", env.send, env.cfg.mail_to, now)


def mark_ok(c, account: dict, now: str) -> None:
    store.set_state(c, "session", "ok")
    store.set_state(c, "session_since", now)
    store.set_state(c, "account_name", str(account.get("name", "")))
    store.set_state(c, "storefront", str(account.get("storefront", "")))
    notify.reset(c, notify.EXPIRED_KEY)


def low_space(env: Env, c, e: LowSpace) -> None:
    notify.once(c, notify.LOW_SPACE_KEY, LOW_SPACE_SUBJECT, f"{e}\nКаталог: {env.cfg.public_base}/\n",
                env.send, env.cfg.mail_to, env.now())


def refresh_history(env: Env, c) -> int:
    return store.upsert_purchases(c, env.tool.list_purchases(), env.now())


def version_dir_name(c, app_id: int, version: str, external_id: str) -> str:
    """<app_id>-<version>; занято другой версией — с external id, затем -2, -3…"""
    base = f"{app_id}-{SAFE_RE.sub('_', version) or 'v'}"
    name, n = base, 1
    while store.dir_in_use(c, name):
        name = f"{base}-{SAFE_RE.sub('_', external_id)}" if n == 1 else f"{base}-{n}"
        n += 1
    return name


def fetch_version(env: Env, c, app_id: int, work_root: Path) -> str:
    """Скачать последнюю версию в pub/<токен>/<dir>. "updated" — новая current, "same" — такая уже есть."""
    cfg = env.cfg
    free = free_space(env)
    if free < FREE_MIN:
        raise LowSpace(f"мало места: свободно {free / GB:.1f} ГБ, для скачивания нужно 4 ГБ")
    notify.reset(c, notify.LOW_SPACE_KEY)  # место есть — эпизод «мало места» закончился
    work = work_root / f"dl-{app_id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        src = env.tool.download(app_id, work)
        info = ipa.read_info(src)
        if info.item_id != app_id:
            raise ipa.IpaError(f"в IPA itemId {info.item_id}, ожидался {app_id}")
        if info.title and info.bundle_id:
            store.fill_names(c, app_id, info.title, info.bundle_id)  # добавленное по ссылке получает имя
        cur = store.current_version(c, app_id)
        if cur is not None and cur["external_version_id"] == info.external_version_id:
            store.set_app_status(c, app_id, "ok")
            return "same"
        name = version_dir_name(c, app_id, info.version, info.external_version_id)
        final, partial = cfg.pub_root / name, cfg.pub_root / f".{name}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)  # остаток прерванной публикации: имя в базе не занято
        partial.mkdir(parents=True)
        size = src.stat().st_size
        shutil.move(src, partial / "app.ipa")  # tmp локально, архив на шаре: os.replace дал бы EXDEV
        (partial / "icon.png").write_bytes(ipa.extract_icon(partial / "app.ipa"))
        (partial / "manifest.plist").write_bytes(ipa.manifest(
            cfg.public_url(name, "app.ipa"), cfg.public_url(name, "icon.png"),
            info.bundle_id, info.version, info.title))
        for f in partial.iterdir():
            f.chmod(0o644)  # UMask служб 0077, а файлы отдаёт Apache (www-data)
        partial.chmod(0o755)
        os.replace(partial, final)
        try:
            store.add_version(c, cfg.pub_root, app_id, store.NewVersion(
                version=info.version, build=info.build, external_version_id=info.external_version_id,
                min_ios=info.min_ios, device_family=info.device_family, size=size, dir=name,
                built=info.built), env.now())
        except store.AppGone:
            shutil.rmtree(final, ignore_errors=True)
            raise
        return "updated"
    finally:
        shutil.rmtree(work, ignore_errors=True)


class Worker:
    """Обработчик заданий publish и refresh_history — один поток в appshelf-web."""

    def __init__(self, env: Env, connect: Callable, purge: Callable[[], None] = lambda: None,
                 interval: float = 2.0, sleep: Callable[[float], None] = time.sleep):
        self.env, self.connect, self.purge = env, connect, purge
        self.interval, self.sleep = interval, sleep

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="appshelf-worker", daemon=True)
        t.start()
        return t

    def run_forever(self) -> None:
        self.recover()
        while True:
            try:
                self.tick()
            except Exception:  # сбой одного прохода не останавливает обработчик
                traceback.print_exc()
            self.sleep(self.interval)

    def recover(self) -> None:
        """После перезапуска: прерванные задания — снова в очередь, свой tmp — прочь."""
        c = self.connect()
        try:
            store.requeue_running(c)
        finally:
            c.close()
        shutil.rmtree(self.env.cfg.tmp_dir / "web", ignore_errors=True)

    def tick(self) -> bool:
        """Одно задание. True — что-то сделано; без входа в Apple ID задания ждут (spec §7.6)."""
        self.purge()  # пароль шага входа живёт не дольше TTL, даже если страницу бросили
        c = self.connect()
        try:
            if store.has_running(c, "update"):
                with nightly_lock(self.env.cfg) as idle:
                    if idle:  # ночную проверку убили посреди скачивания — не ждать следующей ночи
                        store.fail_stale_updates(c, self.env.now())
            if store.get_state(c, "session") != "ok":
                return False
            job = store.take_job(c)
            if job is None:
                return False
            self.run_job(c, job)
            return True
        finally:
            c.close()

    def run_job(self, c, job) -> None:
        env, app_id = self.env, job["app_id"]
        try:
            if job["kind"] == "refresh_history":
                refresh_history(env, c)
            elif job["kind"] == "check_removed":
                try:
                    removed.check(env, c, stop=lambda: store.has_queued(c, "publish"))
                except removed.Interrupted:  # публикация вперёд, проверка — следом с того же места
                    store.requeue_job(c, job["id"])
                    return
            else:
                store.set_app_status(c, app_id, "downloading")
                fetch_version(env, c, app_id, env.cfg.tmp_dir / "web")
            store.finish_job(c, job["id"], "done", env.now())
        except SessionExpired:
            store.requeue_job(c, job["id"])
            if app_id is not None:
                store.set_app_status(c, app_id, "queued")
            mark_expired(env, c)
        except store.AppGone:
            store.finish_job(c, job["id"], "cancelled", env.now(), "снято с публикации во время скачивания")
        except Exception as e:  # ошибка задания — текст у карточки, «Повторить»
            if isinstance(e, LowSpace):
                low_space(env, c, e)
            msg = describe(e)
            store.finish_job(c, job["id"], "error", env.now(), msg)
            if app_id is not None:
                store.set_app_status(c, app_id, "error", msg)
