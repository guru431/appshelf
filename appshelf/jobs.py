"""Задания и общий путь скачивания версии (spec 2026-10-05 §7, 2026-10-08 §8). Обработчик — поток в appshelf-web;
ночная проверка (nightly.py) вызывает fetch_version в своём процессе. Каталоги tmp/web и tmp/nightly у каждого
свои. Всё — по Apple ID: свой ipatool (Env.tools), своя полка в архиве (cfg.shelf_root)."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import ipa, notify, people, removed, store
from .android import db as android_db, fetch as android_fetch, play, sources as android_sources
from .config import GB, Config
from .ipatool import IpatoolError, LicenseNotFound, SessionExpired, gate_path, hold, try_lock

RESERVE = 3 * GB         # неприкосновенный запас места: архив — общая шара
FREE_MIN = RESERVE + GB  # + 1 ГБ на файл: размер IPA до скачивания неизвестен
BANNER_FREE = RESERVE    # баннер на каталоге
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
EXPIRED_SUBJECT = "appshelf: нужен вход в Apple ID"
LOW_SPACE_SUBJECT = f"appshelf: мало места на {notify.HOST}"
REMOVE_LOCK_WAIT = 10.0  # идёт вызов ipatool этого Apple ID — ждём, как вход; скачивание отсекает проверка заданий
NOT_PURCHASED = "этого приложения нет в покупках Apple ID — проверьте ссылку или ID"
ACCOUNT_GONE = "Apple ID удалён"
FAILED_MAX = 50          # id с технической ошибкой в ходе проверки справочника — не больше


class LowSpace(Exception):
    pass


class ArchiveUnavailable(Exception):
    """Каталога архива нет: шара не смонтирована. Писать под пустую точку монтирования — забить локальный диск."""


class AccountBusy(Exception):
    """У Apple ID идёт задание или вызов ipatool: удали его сейчас — каталог полки и HOME появятся снова без хозяина."""


class LastOwnerAccount(Exception):
    """Последний Apple ID владельца: с ним удалился бы и владелец — сервер остался бы без него."""


class LinkRefused(Exception):
    """«Связать» не по правилам: у уходящего есть Apple ID (к другому участнику он не переходит никогда) или одного
    из участников уже нет."""


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


def check_user(cfg: Config) -> None:
    """Запуск только от владельца accounts/ (appshelf). У root ipatool подмешивает в ключ учётки product_uuid — ни одна
    учётка не расшифруется, и ночь пометила бы все Apple ID «вход истёк»; файлы базы, созданные root, служба потом
    не откроет."""
    euid = getattr(os, "geteuid", lambda: None)()
    if euid is None:  # Windows: тесты
        return
    try:
        owner = cfg.accounts_dir.stat().st_uid
    except FileNotFoundError:
        owner = euid
    if euid == 0 or euid != owner:
        raise SystemExit("appshelf: запускайте от пользователя appshelf — sudo -u appshelf … "
                         "или systemctl start appshelf-nightly")


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
    sources: object = None                          # источники APK: android.sources.Sources или подмена


def describe(e: Exception) -> str:
    """Текст ошибки для карточки и jobs.error (пароль сюда не попадает: вход — не задание)."""
    if isinstance(e, LicenseNotFound):
        return "у Apple ID нет лицензии на это приложение"
    if isinstance(e, IpatoolError):
        return f"ipatool: {e.message}"[:500]
    if isinstance(e, (LowSpace, ArchiveUnavailable, ipa.IpaError)):
        return str(e)[:500]
    return f"{type(e).__name__}: {e}"[:500]


def stub(c, aid: int, app_id: int) -> bool:
    """Добавлено по ссылке или ID, и лицензию Apple не подтвердил — в справочнике ID или нет."""
    return store.unconfirmed(c, aid, app_id)


def app_failed(c, aid: int, app_id: int, e: Exception, now: str) -> None:
    """Ошибка скачивания — у карточки. Есть рабочая версия — приложение остаётся «ok», ошибка — строкой «не удалось
    обновить»: ставить можно, повторит ночь или «Проверить обновление» (итог проверки — error). Добавленное по ссылке
    без лицензии — nolicense (повторять бесполезно) и отметка «нет лицензии» (на «Удалённых» — «нет в аккаунте»)."""
    if isinstance(e, LicenseNotFound) and stub(c, aid, app_id):
        store.set_app_status(c, aid, app_id, "nolicense", NOT_PURCHASED)
        with suppress(store.AccountGone):  # Apple ID удалили, пока шёл запрос, — отмечать нечего
            store.mark_no_license(c, aid, app_id, now)
    elif store.current_version(c, aid, app_id) is not None:
        store.set_app_status(c, aid, app_id, "ok", describe(e))
        store.set_checked(c, aid, app_id, now, "error")
    else:
        store.set_app_status(c, aid, app_id, "error", describe(e))


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


def version_dir_name(c, aid: int, root: Path, app_id: int, version: str, external_id: str) -> str:
    """<app_id>-<version>; занято (версия этого Apple ID в базе или каталог на диске) — с external id, затем -2, -3…
    Каталог на диске без строки не перезаписываем: его может ещё удалять завершающееся снятие с публикации
    (store.unpublish удаляет каталоги после COMMIT). Остаток жёсткого падения между os.replace и COMMIT поэтому
    остаётся сиротой на полке."""
    base = f"{app_id}-{SAFE_RE.sub('_', version) or 'v'}"
    name, n = base, 1
    while store.dir_in_use(c, aid, name) or (root / name).exists():
        name = f"{base}-{SAFE_RE.sub('_', external_id)}" if n == 1 else f"{base}-{n}"
        n += 1
    return name


def fetch_version(env: Env, c, acct, app_id: int, work_root: Path) -> str:
    """Скачать последнюю версию на полку Apple ID. "updated" — новая current, "same" — такая уже есть.
    На сервере качается один IPA за раз (download_lock): иначе два скачивания вместе пройдут проверку места."""
    cfg = env.cfg
    with hold(cfg.download_lock):
        fresh = people.get_account(c, acct.id)  # каталог полки мог переехать, пока ждали замок (move_shelf)
        if fresh is None:  # Apple ID удалили, пока ждали замок: каталог полки в архиве заново не создаём
            raise store.AccountGone(acct.id)
        acct = fresh
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
            store.set_downloaded(c, acct.id, app_id, info.version, env.now())
            cur = store.current_version(c, acct.id, app_id)
            if cur is not None and cur["external_version_id"] == info.external_version_id:
                store.set_app_status(c, acct.id, app_id, "ok")
                return "same"
            size = src.stat().st_size
            digest = ipa.sha256(src)  # по локальному файлу в tmp: с шары читать вдвое дольше
            free = env.disk_free(_archive(cfg))  # размер теперь известен: запас шары не трогаем и большим IPA
            if free - size < RESERVE:
                raise LowSpace(f"мало места в архиве: свободно {free / GB:.1f} ГБ, IPA — {size / GB:.1f} ГБ, "
                               f"запас — {RESERVE // GB} ГБ")
            name = version_dir_name(c, acct.id, root, app_id, info.version, info.external_version_id)
            final, partial = root / name, root / f".{name}.partial"
            shutil.rmtree(partial, ignore_errors=True)
            try:
                partial.mkdir(parents=True)
                shutil.move(src, partial / "app.ipa")  # tmp локально, архив на шаре: os.replace дал бы EXDEV
                (partial / "icon.png").write_bytes(ipa.extract_icon(partial / "app.ipa"))
                (partial / "manifest.plist").write_bytes(ipa.manifest(
                    cfg.shelf_url(acct, name, "app.ipa"), cfg.shelf_url(acct, name, "icon.png"),
                    info.bundle_id, info.version, info.title))
                for f in partial.iterdir():
                    f.chmod(0o644)  # UMask служб 0077, а файлы отдаёт Apache (www-data)
                partial.chmod(0o755)
                os.replace(partial, final)
                store.add_version(c, acct.id, root, app_id, store.NewVersion(
                    version=info.version, build=info.build, external_version_id=info.external_version_id,
                    min_ios=info.min_ios, device_family=info.device_family, size=size, dir=name,
                    built=info.built, sha256=digest), env.now())
            except BaseException:
                # обрыв шары, ENOSPC, «database is locked», сняли с публикации (AppGone): строки версии нет, а
                # .partial с частью IPA или каталог без строки потом не убрал бы никто
                shutil.rmtree(partial, ignore_errors=True)
                shutil.rmtree(final, ignore_errors=True)
                raise
            return "updated"
        finally:
            shutil.rmtree(work, ignore_errors=True)


def unpublish(cfg: Config, c, acct, app_id: int) -> None:
    """Снять с публикации. Архив недоступен — ArchiveUnavailable и ничего не тронуто: файлы остались бы на шаре без
    строк, и удалить их из панели было бы нечем. Добавленное по ссылке, лицензию на которое Apple не подтвердил,
    уходит и из «Истории»."""
    _archive(cfg)
    store.unpublish(c, acct.id, cfg.shelf_root(acct), app_id)
    if stub(c, acct.id, app_id):
        store.drop_purchase(c, acct.id, app_id)


def remove_account(cfg: Config, c, acct) -> None:
    """Apple ID целиком: строки полки, каталог в архиве, HOME ipatool (учётка и cookies). Был последним у
    человека — удаляется и человек. Архив недоступен — ArchiveUnavailable и ничего не удалено: IPA с данными
    покупателя не должен остаться без хозяина. Идёт задание или вызов ipatool этого Apple ID — AccountBusy.
    Последний Apple ID владельца — LastOwnerAccount: проверка здесь, под транзакцией, — два удаления разных его
    полок в один момент оба видели бы «не последний»."""
    def last_owner_id():
        last = [a.id for a in people.accounts_of(c, acct.user_id)] == [acct.id]
        if last and people.is_owner_account(c, acct):
            raise LastOwnerAccount(acct.email)

    _remove(cfg, c, [acct], last_owner_id)


def _remove(cfg: Config, c, accts, check: Callable[[], None] = lambda: None, user_id: int | None = None) -> None:
    """Apple ID под замками их ipatool, строки — одной транзакцией: обработчик не возьмёт задание посередине, и
    занятость любого — AccountBusy до удаления первого. Каталоги полок — по строкам из той же транзакции:
    rotate-token мог перенести полку, пока ждали замок. Участник уходит, когда у него не осталось ни Apple ID, ни
    Google-аккаунтов (spec 2026-10-28 §4); user_id — участник целиком: и его Google-аккаунты."""
    _archive(cfg)
    locks = [cfg.locks_dir / f"{a.id}.lock" for a in accts]
    with ExitStack() as held:
        try:
            held.enter_context(hold(cfg.download_lock, REMOVE_LOCK_WAIT))
        except IpatoolError as e:
            raise AccountBusy("Android-полка") from e
        for a, lock in zip(accts, locks):
            try:
                held.enter_context(hold(lock, REMOVE_LOCK_WAIT))
            except IpatoolError as e:  # hold не дождался вызова ipatool этого Apple ID
                raise AccountBusy(a.email) from e
        with store.tx(c):
            busy = next((a for a in accts if store.account_running(c, a.id)), None)
            if busy is not None:
                raise AccountBusy(busy.email)
            check()
            fresh = [a for a in (people.get_account(c, a.id) for a in accts) if a is not None]
            uids = dict.fromkeys([a.user_id for a in fresh] + ([user_id] if user_id is not None else []))
            removing = {a.id for a in fresh}
            for uid in uids:
                last = user_id is not None or not people.google_of(c, uid) and not any(
                    a.id not in removing for a in people.accounts_of(c, uid))
                shelf = android_db.shelf_for_user(c, uid)
                if last and shelf is not None and _android_running(c, shelf.id):
                    raise AccountBusy("Android-полка")
            for a in fresh:
                store.delete_shelf(c, a.id)
                c.execute("DELETE FROM accounts WHERE id=?", (a.id,))
            gids = []
            if user_id is not None:
                gids = [g.id for g in people.google_of(c, user_id)]
                if any(_google_running(c, gid) for gid in gids):
                    raise AccountBusy("Google Play")
                c.execute("DELETE FROM google_accounts WHERE user_id=?", (user_id,))
            for gid in gids:
                _google_apps_gone(c, gid)
            gone = [s for s in (_drop_user_if_empty(c, uid) for uid in uids) if s is not None]
        for shelf in gone:
            _drop_android_dirs(cfg, shelf)
        for gid in gids:
            store.rmtree_logged(cfg.google_dir / str(gid))  # мастер-токен Play (spec 2026-10-28 §11)
            play.forget(gid)
        for a in fresh:
            store.rmtree_logged(cfg.shelf_root(a))
            if a.shelf_next:  # перенос прервался после переименования каталога
                store.rmtree_logged(cfg.archive / a.shelf_next)
            store.rmtree_logged(cfg.accounts_dir / str(a.id))
    for lock in locks:
        _drop_lock(lock)


def _drop_user_if_empty(c, uid: int):
    """В транзакции вызывающего: у участника не осталось аккаунтов — строка участника и его Android-полки прочь (spec
    2026-10-28 §4: полка — у участника). Ушла полка — она (каталоги — вызывающему после COMMIT), иначе None."""
    if people.has_apple(c, uid) or people.google_of(c, uid):
        return None
    people.delete_user_row(c, uid)
    shelf = android_db.shelf_for_user(c, uid)
    if shelf is not None:
        android_db.delete_shelf(c, shelf.id)
    return shelf


def _drop_android_dirs(cfg: Config, shelf) -> None:
    store.rmtree_logged(cfg.archive / shelf.shelf)
    if shelf.shelf_next:
        store.rmtree_logged(cfg.archive / shelf.shelf_next)


def _drop_lock(lock) -> None:
    """Файл блокировки и её очереди. Открыт другим процессом — останется пустым, Apple ID уже нет."""
    for path in (lock, gate_path(lock)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def drop_account_files(cfg: Config, aid: int) -> None:
    """HOME ipatool и блокировка Apple ID, строки которого уже нет (удалили посреди входа)."""
    shutil.rmtree(cfg.accounts_dir / str(aid), ignore_errors=True)
    _drop_lock(cfg.locks_dir / f"{aid}.lock")


def remove_user(cfg: Config, c, uid: int) -> None:
    """Человек со всеми Apple ID, Google-аккаунтами и Android-полкой — разом (_remove), и участник без Apple ID тоже:
    задание или вызов ipatool у любого его Apple ID — AccountBusy, и не удалено ничего."""
    _remove(cfg, c, people.accounts_of(c, uid), user_id=uid)


def remove_google(cfg: Config, c, gid: int) -> bool:
    """Google-аккаунт; был последним аккаунтом участника — уходят и участник, и его Android-полка (True). Полка
    уходит — под замком скачивания, как rotate_shelf: store_apk не пишет в удаляемый каталог; не дождались или у
    полки либо аккаунта (включая публичную полку) идёт задание — AccountBusy. Архив недоступен при удалении личной
    полки — ArchiveUnavailable, и не удалено ничего. Уже удалён — False."""
    g = people.get_google(c, gid)
    if g is None:
        return False
    with ExitStack() as held:
        if android_db.shelf_for_user(c, g.user_id) is not None:
            _archive(cfg)
        try:
            held.enter_context(hold(cfg.download_lock, REMOVE_LOCK_WAIT))
        except IpatoolError as e:  # аккаунтом могут качать на публичную полку без личной
            raise AccountBusy(g.email) from e
        with store.tx(c):
            if people.get_google(c, gid) is None:
                return False
            if _google_running(c, gid):
                raise AccountBusy(g.email)
            _google_apps_gone(c, gid)
            c.execute("DELETE FROM google_accounts WHERE id=?", (gid,))
            last = not people.has_apple(c, g.user_id) and not people.google_of(c, g.user_id)
            shelf = android_db.shelf_for_user(c, g.user_id)
            if last and shelf is not None and _android_running(c, shelf.id):
                raise AccountBusy(g.email)
            gone = _drop_user_if_empty(c, g.user_id)
        if gone is not None:
            _drop_android_dirs(cfg, gone)
        store.rmtree_logged(cfg.google_dir / str(gid))  # мастер-токен Play (spec 2026-10-28 §11)
        play.forget(gid)
    return last


def _google_running(c, gid: int) -> bool:
    """Включая задание публичной полки, уже взятое обработчиком и ещё ждущее замка скачивания."""
    return c.execute(f"SELECT 1 FROM android_jobs j WHERE j.status='running' AND {android_db.JOB_ACCOUNT}=? LIMIT 1",
                     (gid,)).fetchone() is not None


def _google_apps_gone(c, gid: int) -> None:
    """Существующие APK остаются доступны, но источник удалённого аккаунта больше не проверяется."""
    for app in c.execute("SELECT * FROM android_apps WHERE source='play' AND google_account_id=?", (gid,)).fetchall():
        current = android_db.has_current(c, app["shelf_id"], app["package"])
        note = android_fetch._notice(app["last_error"]) if current else ""
        android_db.set_status(c, app["shelf_id"], app["package"], "ok" if current else "error",
                              (note + "\n" if note else "") + android_sources.ACCOUNT_GONE)


def link_users(cfg: Config, c, keep_uid: int, gone_uid: int) -> None:
    """«Связать» (spec 2026-10-28 §5): Google-аккаунты и Android-полка уходящего — остающемуся; уходящий удаляется
    вместе со своими запасными ссылками входа. Под замком скачивания — store_apk не пишет в полки посреди переноса
    (не дождались — AccountBusy); строки — одной транзакцией. Сбой переноса каталога — перенесённые каталоги
    возвращаются назад, транзакция откатывается, исключение уходит наверх. Каталоги уходящей полки — после COMMIT.
    У уходящего Apple ID — LinkRefused; у его полки идёт задание или не доделана смена токена — AccountBusy; архив
    недоступен — ArchiveUnavailable."""
    _archive(cfg)
    try:
        with hold(cfg.download_lock, REMOVE_LOCK_WAIT):
            moved: list[tuple[Path, Path]] = []  # (откуда, куда) — для возврата при сбое
            try:
                with store.tx(c):
                    drop = _link(cfg, c, keep_uid, gone_uid, moved)
            except BaseException:
                for src, dst in reversed(moved):
                    try:
                        os.replace(dst, src)
                    except OSError as e:  # шара отвалилась совсем: каталог остался сиротой у остающегося
                        print(f"appshelf: каталог {dst.name} не возвращён после сбоя связки: {e}", file=sys.stderr)
                raise
    except IpatoolError as e:  # не дождались замка скачивания
        raise AccountBusy(f"участник №{gone_uid}") from e
    if drop is not None:
        _drop_android_dirs(cfg, drop)


def _link(cfg: Config, c, keep_uid: int, gone_uid: int, moved: list) -> android_db.Shelf | None:
    """Строки связки — в транзакции link_users. → полка уходящего, если её каталоги надо удалить после COMMIT."""
    if people.get_user(c, keep_uid) is None or people.get_user(c, gone_uid) is None:
        raise LinkRefused("участника уже нет")
    if people.has_apple(c, gone_uid):
        raise LinkRefused("у уходящего есть Apple ID")
    src, dst = android_db.shelf_for_user(c, gone_uid), android_db.shelf_for_user(c, keep_uid)
    if src is not None and (_android_running(c, src.id) or src.shelf_next or (dst is not None and dst.shelf_next)):
        raise AccountBusy(f"Android-полка №{src.id}")
    c.execute("UPDATE google_accounts SET user_id=? WHERE user_id=?", (keep_uid, gone_uid))
    c.execute("DELETE FROM links WHERE user_id=? AND kind=?", (gone_uid, people.LOGIN))
    drop = None
    if src is not None and dst is None:  # у остающегося полки нет — полка переходит целиком, каталог на месте
        c.execute("UPDATE android_shelves SET user_id=? WHERE id=?", (keep_uid, src.id))
    elif src is not None:
        _merge_shelves(cfg, c, src, dst, moved)
        android_db.delete_shelf(c, src.id)  # остались совпавшие пакеты и задания — их строки прочь
        drop = src
    people.delete_user_row(c, gone_uid)
    return drop


def _merge_shelves(cfg: Config, c, src, dst, moved: list) -> None:
    """Пакеты src, которых нет на dst: карточка, версии и ждущие задания — на dst, каталоги — в корень dst (занятое
    строкой или на диске имя — android_db.version_dir). Совпавшие остаются у dst как были."""
    src_root, dst_root = cfg.archive / src.shelf, cfg.archive / dst.shelf
    theirs = {r["package"] for r in c.execute("SELECT package FROM android_apps WHERE shelf_id=?", (dst.id,))}
    for (pkg,) in c.execute("SELECT package FROM android_apps WHERE shelf_id=?", (src.id,)).fetchall():
        if pkg in theirs:
            continue
        c.execute("UPDATE android_apps SET shelf_id=? WHERE shelf_id=? AND package=?", (dst.id, src.id, pkg))
        for v in c.execute("SELECT id, dir, version_code FROM android_versions WHERE shelf_id=? AND package=?",
                           (src.id, pkg)).fetchall():
            name = v["dir"]
            if (dst_root / name).exists() or c.execute("SELECT 1 FROM android_versions WHERE shelf_id=? AND dir=?",
                                                       (dst.id, name)).fetchone() is not None:
                name = android_db.version_dir(c, dst.id, dst_root, pkg, v["version_code"])
            if android_db.ANDROID_DIR_RE.match(v["dir"]) and (src_root / v["dir"]).is_dir():
                dst_root.mkdir(exist_ok=True)
                dst_root.chmod(0o711)  # UMask служб 0077, а в каталог полки заходит Apache (www-data)
                os.replace(src_root / v["dir"], dst_root / name)
                moved.append((src_root / v["dir"], dst_root / name))
            c.execute("UPDATE android_versions SET shelf_id=?, dir=? WHERE id=?", (dst.id, name, v["id"]))
    c.execute(f"UPDATE android_jobs SET shelf_id=? WHERE shelf_id=? AND status='queued' "
              f"AND package NOT IN ({','.join('?' * len(theirs))})", (dst.id, src.id, *theirs))


def _android_running(c, shelf_id: int) -> bool:
    return c.execute("SELECT 1 FROM android_jobs WHERE shelf_id=? AND status='running'",
                     (shelf_id,)).fetchone() is not None


def move_shelf(cfg: Config, c, acct) -> None:
    """Новый случайный токен каталога полки: каталог в архиве переименовать, ссылки в manifest.plist — поправить.
    appshelf rotate-token (ссылки полки утекли) и перенос каталогов версии 2 (move_shelves). Новый токен сначала
    ложится в shelf_next: сбой посередине доделает следующий вызов. На время переноса держим замок скачивания —
    fetch_version не начнёт писать в прежний каталог — и замок Apple ID: удаление не пройдёт посреди переноса.
    Apple ID удалили, пока ждали замки, — переносить нечего. Архив недоступен — ArchiveUnavailable; идёт задание,
    удаление или вызов ipatool Apple ID или чьё-то скачивание — AccountBusy."""
    _archive(cfg)
    try:
        with hold(cfg.download_lock, REMOVE_LOCK_WAIT), hold(cfg.locks_dir / f"{acct.id}.lock", REMOVE_LOCK_WAIT):
            with store.tx(c):
                if store.account_running(c, acct.id):
                    raise AccountBusy(acct.email)
                cur = people.get_account(c, acct.id)
                if cur is None:
                    return
                if not cur.shelf_next:
                    people.set_shelf_next(c, acct.id, people.new_shelf_token())
            acct = people.get_account(c, acct.id)
            old, new = cfg.shelf_token(acct), acct.shelf_next
            src, dst = cfg.archive / old, cfg.archive / new
            if src.is_dir() and not dst.exists():
                os.replace(src, dst)
            for m in dst.glob("*/manifest.plist"):
                _relink(m, f"{cfg.public_base}/d/{old}/", f"{cfg.public_base}/d/{new}/")
            people.set_shelf(c, acct.id, new)
    except IpatoolError as e:  # не дождались замка скачивания или Apple ID
        raise AccountBusy(acct.email) from e


def _relink(manifest: Path, old: str, new: str) -> None:
    data = manifest.read_bytes()
    fixed = data.replace(old.encode(), new.encode())
    if fixed != data:  # Apache может отдавать manifest прямо сейчас — подмена целиком
        tmp = manifest.with_name(".manifest.plist.tmp")
        tmp.write_bytes(fixed)
        tmp.chmod(0o644)
        os.replace(tmp, manifest)


def move_shelves(cfg: Config, c) -> None:
    """При старте appshelf-web и ночной проверки: доделать прерванные переносы (и Android-полок) и перенести каталоги Apple ID
    версии 2 (HMAC от PUB_TOKEN) на случайные токены. Полка владельца из версии 1 остаётся <PUB_TOKEN>. Архив
    недоступен, Apple ID занят, сбой шары — в журнал, повторит следующий запуск: служба из-за этого не встаёт."""
    for acct in people.all_accounts(c):
        if acct.shelf_next or not (acct.legacy_pub or acct.shelf):
            try:
                move_shelf(cfg, c, acct)
            except (ArchiveUnavailable, AccountBusy, OSError) as e:
                why = "идёт задание или скачивание" if isinstance(e, AccountBusy) else str(e)  # без адреса Apple ID
                print(f"appshelf: каталог полки Apple ID №{acct.id} не перенесён: {why}", file=sys.stderr)
    for shelf in android_db.all_shelves(c):
        if shelf.shelf_next:  # смена токена Android-полки прервалась (rotate-token, смена PIN)
            try:
                android_fetch.rotate_shelf(cfg, c, shelf.id)
            except (ArchiveUnavailable, AccountBusy, OSError) as e:
                why = "идёт задание или скачивание" if isinstance(e, AccountBusy) else str(e)
                print(f"appshelf: каталог Android-полки №{shelf.id} не перенесён: {why}", file=sys.stderr)


def merge_progress(prev: dict | None, cur: dict) -> dict:
    """Ход проверки справочника для jobs.progress — накопительно по проходам задания (уступила очередь, ждала входа,
    перезапуск службы). id с технической ошибкой нигде не запоминаются, и следующий проход спрашивает их снова:
    прежние ошибки из счёта уходят, остаются ошибки этого прохода."""
    if prev:
        base = prev["done"] - prev["errors"]
        cur = {**cur, "done": base + cur["done"], "total": base + cur["total"],
               "added": prev["added"] + cur["added"], "missing": prev["missing"] + cur["missing"]}
    return {**cur, "failed": cur["failed"][:FAILED_MAX]}


class Worker:
    """Обработчик заданий publish, refresh_history, check_removed всех Apple ID и заданий Android-полок (когда
    Apple-заданий нет) — один поток в appshelf-web."""

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
        """После перезапуска: прерванные задания — снова в очередь, свой tmp — прочь, как и временные HOME входов
        новых Apple ID (их пароли ушли вместе с прежним процессом) и HOME удалённых посреди входа Apple ID."""
        c = self.connect()
        try:
            store.requeue_running(c)
            known = {str(a.id) for a in people.all_accounts(c)}
            cfg = self.env.cfg
            shutil.rmtree(cfg.tmp_dir / "web", ignore_errors=True)
            android_fetch.recover(c, self.env.now())  # после очистки tmp/web: загрузки без файла — ошибка
        finally:
            c.close()
        for home in cfg.accounts_dir.glob("*"):
            if home.name.startswith(".new-") or (home.name.isdigit() and home.name not in known):
                shutil.rmtree(home, ignore_errors=True)
        for lock in [*cfg.locks_dir.glob("*.lock"), *cfg.locks_dir.glob("*.wait")]:
            if lock.stem.startswith(".new-") or (lock.stem.isdigit() and lock.stem not in known):
                lock.unlink(missing_ok=True)

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
                job = android_db.take_job(c)
                if job is None:
                    return False
                android_fetch.run_job(self.env, c, job)
                return True
            acct = people.get_account(c, job["account_id"])
            if acct is None:  # Apple ID удалили между постановкой задания и его взятием
                store.finish_job(c, job["id"], "cancelled", self.env.now(), ACCOUNT_GONE)
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
                prev = json.loads(job["progress"]) if job["progress"] else None
                try:
                    removed.check(env, c, acct, stop=lambda: store.has_runnable(c, "publish"),
                                  progress=lambda p: store.set_progress(c, job["id"], merge_progress(prev, p)))
                except removed.Interrupted:  # публикация вперёд, проверка — следом с того же места
                    store.requeue_job(c, job["id"])
                    return
            else:
                store.set_app_status(c, acct.id, app_id, "downloading")
                cur = store.current_version(c, acct.id, app_id)
                if cur is not None and env.tools(acct).latest_version_id(app_id) == cur["external_version_id"]:
                    store.set_app_status(c, acct.id, app_id, "ok")  # «Повторить» при той же версии — IPA не качаем
                    result = "same"
                else:
                    result = fetch_version(env, c, acct, app_id, env.cfg.tmp_dir / "web")
                if cur is not None:  # проверка обновления («Проверить обновление»), а не первое скачивание
                    store.set_checked(c, acct.id, app_id, env.now(), result)
            store.finish_job(c, job["id"], "done", env.now())
        except SessionExpired as e:
            print(f"appshelf: Apple ID #{acct.id}: вход истёк в задании {job['kind']} {app_id or ''} — {e.message}",
                  file=sys.stderr)
            store.requeue_job(c, job["id"])
            if app_id is not None:
                store.set_app_status(c, acct.id, app_id, "queued")
            mark_expired(env, c, acct)
        except store.AppGone:
            store.finish_job(c, job["id"], "cancelled", env.now(), "снято с публикации во время скачивания")
        except store.AccountGone:  # строки задания может уже не быть — UPDATE ничего не тронет
            store.finish_job(c, job["id"], "cancelled", env.now(), ACCOUNT_GONE)
        except Exception as e:  # ошибка задания — текст у карточки
            if isinstance(e, LowSpace):
                low_space(env, c, e)
            store.finish_job(c, job["id"], "error", env.now(), describe(e))
            if app_id is not None:
                app_failed(c, acct.id, app_id, e, env.now())
