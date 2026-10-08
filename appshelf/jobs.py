"""Задания и общий путь скачивания версии (spec 2026-10-05 §7, 2026-10-08 §8). Обработчик — поток в appshelf-web;
ночная проверка (nightly.py) вызывает fetch_version в своём процессе. Каталоги tmp/web и tmp/nightly у каждого
свои. Всё — по Apple ID: свой ipatool (Env.tools), своя полка в архиве (cfg.shelf_root)."""
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

from . import ipa, notify, people, removed, store
from .config import GB, Config
from .ipatool import IpatoolError, LicenseNotFound, SessionExpired, hold, try_lock

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


def _archive(cfg: Config) -> Path:
    if not cfg.archive.is_dir():
        raise ArchiveUnavailable(f"архив {cfg.archive} недоступен — смонтирован ли он?")
    return cfg.archive


def free_space(env: Env) -> int:
    """Меньшее из свободного места: локальный tmp (скачивание) и архив (может быть сетевой шарой)."""
    return min(env.disk_free(env.cfg.data_dir), env.disk_free(_archive(env.cfg)))


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
    tools: Callable[[people.Account], object]      # ipatool Apple ID: ipatool.for_account(cfg, acct) или подмена
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


def mark_expired(env: Env, c, acct) -> None:
    """Токен Apple ID истёк: его задания ждут входа. Письмо — только владельцу о его Apple ID, одно на эпизод;
    участники видят баннер в панели."""
    now = env.now()
    people.set_expired(c, acct.id, now)
    if people.is_owner_account(c, acct):
        notify.once_for(lambda: people.claim_expired_mail(c, acct.id, now),
                        lambda: people.release_expired_mail(c, acct.id), EXPIRED_SUBJECT,
                        f"Токен App Store для {acct.email} истёк: публикация и ночная проверка этого Apple ID стоят, "
                        f"задания ждут.\nВойти: {env.cfg.public_base}/apple\n", env.send, env.cfg.mail_to)


def low_space(env: Env, c, e: LowSpace) -> None:
    notify.once(c, notify.LOW_SPACE_KEY, LOW_SPACE_SUBJECT, f"{e}\nКаталог: {env.cfg.public_base}/\n",
                env.send, env.cfg.mail_to, env.now())


def refresh_history(env: Env, c, acct) -> int:
    return store.upsert_purchases(c, acct.id, env.tools(acct).list_purchases(), env.now())


def version_dir_name(c, aid: int, app_id: int, version: str, external_id: str) -> str:
    """<app_id>-<version>; занято другой версией этого Apple ID — с external id, затем -2, -3…"""
    base = f"{app_id}-{SAFE_RE.sub('_', version) or 'v'}"
    name, n = base, 1
    while store.dir_in_use(c, aid, name):
        name = f"{base}-{SAFE_RE.sub('_', external_id)}" if n == 1 else f"{base}-{n}"
        n += 1
    return name


def fetch_version(env: Env, c, acct, app_id: int, work_root: Path) -> str:
    """Скачать последнюю версию на полку Apple ID. "updated" — новая current, "same" — такая уже есть.
    На сервере качается один IPA за раз (download_lock): иначе два скачивания вместе пройдут проверку места."""
    cfg = env.cfg
    with hold(cfg.download_lock):
        free = free_space(env)
        if free < FREE_MIN:
            raise LowSpace(f"мало места: свободно {free / GB:.1f} ГБ, для скачивания нужно 4 ГБ")
        notify.reset(c, notify.LOW_SPACE_KEY)  # место есть — эпизод «мало места» закончился
        root = cfg.shelf_root(acct)
        root.mkdir(exist_ok=True)
        root.chmod(0o711)  # UMask служб 0077, а в каталог Apple ID заходит Apache (www-data)
        work = work_root / f"dl-{acct.id}-{app_id}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            src = env.tools(acct).download(app_id, work)
            info = ipa.read_info(src)
            if info.item_id != app_id:
                raise ipa.IpaError(f"в IPA itemId {info.item_id}, ожидался {app_id}")
            if info.title and info.bundle_id:
                store.fill_names(c, acct.id, app_id, info.title, info.bundle_id)  # добавленное по ссылке — с именем
            cur = store.current_version(c, acct.id, app_id)
            if cur is not None and cur["external_version_id"] == info.external_version_id:
                store.set_app_status(c, acct.id, app_id, "ok")
                return "same"
            name = version_dir_name(c, acct.id, app_id, info.version, info.external_version_id)
            final, partial = root / name, root / f".{name}.partial"
            shutil.rmtree(partial, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)  # остаток прерванной публикации: имя в базе не занято
            partial.mkdir(parents=True)
            size = src.stat().st_size
            shutil.move(src, partial / "app.ipa")  # tmp локально, архив на шаре: os.replace дал бы EXDEV
            (partial / "icon.png").write_bytes(ipa.extract_icon(partial / "app.ipa"))
            (partial / "manifest.plist").write_bytes(ipa.manifest(
                cfg.shelf_url(acct, name, "app.ipa"), cfg.shelf_url(acct, name, "icon.png"),
                info.bundle_id, info.version, info.title))
            for f in partial.iterdir():
                f.chmod(0o644)  # UMask служб 0077, а файлы отдаёт Apache (www-data)
            partial.chmod(0o755)
            os.replace(partial, final)
            try:
                store.add_version(c, acct.id, root, app_id, store.NewVersion(
                    version=info.version, build=info.build, external_version_id=info.external_version_id,
                    min_ios=info.min_ios, device_family=info.device_family, size=size, dir=name,
                    built=info.built), env.now())
            except store.AppGone:
                shutil.rmtree(final, ignore_errors=True)
                raise
            return "updated"
        finally:
            shutil.rmtree(work, ignore_errors=True)


def remove_account(cfg: Config, c, acct) -> None:
    """Apple ID целиком: строки полки, каталог в архиве, HOME ipatool (учётка и cookies). Был последним у
    человека — удаляется и человек. Архив недоступен — ArchiveUnavailable и ничего не удалено: IPA с данными
    покупателя не должен остаться без хозяина."""
    _archive(cfg)
    with store.tx(c):
        store.delete_shelf(c, acct.id)
        c.execute("DELETE FROM accounts WHERE id=?", (acct.id,))
        if not people.accounts_of(c, acct.user_id):
            people.delete_user_row(c, acct.user_id)
    shutil.rmtree(cfg.shelf_root(acct), ignore_errors=True)
    shutil.rmtree(cfg.accounts_dir / str(acct.id), ignore_errors=True)


def remove_user(cfg: Config, c, uid: int) -> None:
    """Человек со всеми Apple ID: удаление последнего удаляет и строку человека."""
    for acct in people.accounts_of(c, uid):
        remove_account(cfg, c, acct)


class Worker:
    """Обработчик заданий publish, refresh_history, check_removed всех Apple ID — один поток в appshelf-web."""

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
        """Одно задание Apple ID с входом. True — что-то сделано; задания Apple ID без входа ждут (spec §7.6)."""
        self.purge()  # пароль шага входа живёт не дольше TTL, даже если страницу бросили
        c = self.connect()
        try:
            if store.has_running(c, "update"):
                with nightly_lock(self.env.cfg) as idle:
                    if idle:  # ночную проверку убили посреди скачивания — не ждать следующей ночи
                        store.fail_stale_updates(c, self.env.now())
            job = store.take_job(c)
            if job is None:
                return False
            acct = people.get_account(c, job["account_id"])
            if acct is None:  # Apple ID удалили между постановкой задания и его взятием
                store.finish_job(c, job["id"], "cancelled", self.env.now(), "Apple ID удалён")
                return True
            self.run_job(c, job, acct)
            return True
        finally:
            c.close()

    def run_job(self, c, job, acct) -> None:
        env, app_id = self.env, job["app_id"]
        try:
            if job["kind"] == "refresh_history":
                refresh_history(env, c, acct)
            elif job["kind"] == "check_removed":
                try:
                    removed.check(env, c, acct, stop=lambda: store.has_runnable(c, "publish"))
                except removed.Interrupted:  # публикация вперёд, проверка — следом с того же места
                    store.requeue_job(c, job["id"])
                    return
            else:
                store.set_app_status(c, acct.id, app_id, "downloading")
                fetch_version(env, c, acct, app_id, env.cfg.tmp_dir / "web")
            store.finish_job(c, job["id"], "done", env.now())
        except SessionExpired:
            store.requeue_job(c, job["id"])
            if app_id is not None:
                store.set_app_status(c, acct.id, app_id, "queued")
            mark_expired(env, c, acct)
        except store.AppGone:
            store.finish_job(c, job["id"], "cancelled", env.now(), "снято с публикации во время скачивания")
        except Exception as e:  # ошибка задания — текст у карточки, «Повторить»
            if isinstance(e, LowSpace):
                low_space(env, c, e)
            msg = describe(e)
            store.finish_job(c, job["id"], "error", env.now(), msg)
            if app_id is not None:
                store.set_app_status(c, acct.id, app_id, "error", msg)
