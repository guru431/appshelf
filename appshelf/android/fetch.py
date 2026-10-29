"""Конвейер Android (spec 2026-10-28 §6, §7, §9): APK из источника или из браузера → разбор и проверки → на полку
участника. Задания add / upload / check — обработчик appshelf-web (jobs.Worker), проверка обновлений — и ночью
(nightly.py). Скачивание и запись на полку — под общей блокировкой cfg.download_lock: IPA и APK по одному, иначе два
скачивания вместе прошли бы проверку места. Место — как у IPA: FREE_MIN до, RESERVE после."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

from .. import apk, ipa, jobs, notify, people, store
from ..ipatool import IpatoolError
from . import db, net, play
from .net import SourceError
from .sources import ACCOUNT_GONE, SplitOnly

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
SOURCE_NAMES = {"rustore": "RuStore", "link": "сайтом", "upload": "файлом", "play": "Google Play",
                "appgallery": "AppGallery", "fdroid": "F-Droid", "apkpure": "APKPure"}
UPLOAD_LOST = "файл загрузки потерян при перезапуске сервера — загрузите его заново"
SPLIT_UPDATE = "новая версия только набором файлов — на полке прежняя"
SIGNER_DIFFERS = "подписан не так, как в RuStore: не встанет поверх версии оттуда"
NOT_VERIFIED = "подпись не сверена: пакета нет в RuStore"
NEED_ACK = ("нужно подтверждение для APKPure: пакета нет в RuStore, подпись не с чем сверить — добавьте ещё раз "
            "с галочкой")
NOTICES = (SIGNER_DIFFERS, NOT_VERIFIED)   # метки сверки у карточки: проверка «новее нет» их не стирает
PLAY_EXPIRED_SUBJECT = "appshelf: подключение Google Play истекло"


def shelf_root(cfg, shelf: db.Shelf) -> Path:
    return cfg.archive / shelf.shelf


def file_url(cfg, shelf: db.Shelf, dir_name: str, file: str) -> str:
    return f"{cfg.public_base}/d/{shelf.shelf}/{dir_name}/{file}"


def apk_name(package: str, version_name: str) -> str:
    return f"{package}-{SAFE_RE.sub('_', version_name) or 'v'}.apk"


def describe(e: Exception) -> str:
    if isinstance(e, (apk.ApkError, net.FetchError, SourceError)):
        return str(e)[:500]
    return jobs.describe(e)


def _space(env, c) -> None:
    free = jobs.free_space(env)
    if free < jobs.FREE_MIN:
        raise jobs.LowSpace(f"мало места: свободно {free / jobs.GB:.1f} ГБ, для скачивания нужно 4 ГБ")
    notify.reset(c, notify.LOW_SPACE_KEY)  # место есть — эпизод «мало места» закончился


def store_apk(env, c, shelf: db.Shelf, path: Path, *, source: str, source_ref: str, expected_package: str | None,
              expected_signer: str, name_hint: str, icon: bytes | None, google_account_id: int | None = None,
              paid: bool = False, signer_from: str = "") -> tuple[str, str]:
    """APK из path — на полку: (пакет, "updated" | "pending" | "same"). path переносится на полку или остаётся
    вызывающему. expected_package — карточка уже есть (RuStore, проверка, новая версия файлом): нет её — AppGone;
    без него карточка появляется, когда пакет стал известен (ссылка, свой файл). signer_from — чья подпись
    expected_signer, если не самого источника (APKPure сверяется с RuStore)."""
    cfg, now = env.cfg, env.now()
    info = apk.read_apk(path)
    if expected_package and info.package != expected_package:
        raise apk.ApkError(f"в файле приложение {info.package}, а на карточке — {expected_package}")
    if expected_signer and info.signer != expected_signer:
        raise apk.ApkError(f"подпись не совпала с {SOURCE_NAMES.get(signer_from or source, source)}")
    app = db.get_app(c, shelf.id, info.package)
    if app is None and expected_package:
        raise store.AppGone(info.package)
    roles = {r["role"]: r["version_code"] for r in c.execute(
        "SELECT role, version_code FROM android_versions WHERE shelf_id=? AND package=?", (shelf.id, info.package))}
    if "current" in roles and info.version_code <= roles["current"]:
        return info.package, "same"
    if app is not None and app["signer"] and info.signer != app["signer"] \
            and info.version_code <= roles.get("pending", -1):
        return info.package, "same"  # эта же версия с другой подписью уже ждёт «Принять»
    size = path.stat().st_size
    digest = ipa.sha256(path)  # по локальному файлу в tmp: с шары читать вдвое дольше
    free = env.disk_free(jobs._archive(cfg))
    if free - size < jobs.RESERVE:
        raise jobs.LowSpace(f"мало места в архиве: свободно {free / jobs.GB:.1f} ГБ, APK — {size / jobs.GB:.1f} ГБ, "
                            f"запас — {jobs.RESERVE // jobs.GB} ГБ")
    if app is None:
        if db.get_shelf(c, shelf.id) is None:  # участника удалили, пока качали: карточку в ничью полку не ставим
            raise store.AppGone(info.package)
        db.put_app(c, shelf.id, info.package, info.label or name_hint.strip() or info.package, source, source_ref, now,
                   google_account_id, paid)
    root = shelf_root(cfg, shelf)
    root.mkdir(exist_ok=True)
    root.chmod(0o711)  # UMask служб 0077, а в каталог полки заходит Apache (www-data)
    name = db.version_dir(c, shelf.id, root, info.package, info.version_code)
    final, partial = root / name, root / f".{name}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        partial.mkdir(parents=True)
        apk_file = partial / apk_name(info.package, info.version_name)
        shutil.move(path, apk_file)  # tmp локально, архив на шаре: os.replace дал бы EXDEV
        (partial / "icon.png").write_bytes(apk.extract_icon(apk_file) or icon or ipa.placeholder_png())
        for f in partial.iterdir():
            f.chmod(0o644)  # UMask служб 0077, а файлы отдаёт Apache (www-data)
        partial.chmod(0o755)
        os.replace(partial, final)
        role = db.add_version(c, root, shelf.id, info.package, db.NewApk(
            version_name=info.version_name, version_code=info.version_code, min_sdk=info.min_sdk,
            target_sdk=info.target_sdk, abis=",".join(info.abis), signer=info.signer, source=source, size=size,
            sha256=digest, dir=name), now)
    except BaseException:
        # обрыв шары, «database is locked», сняли с полки (AppGone): строки версии нет, а .partial или каталог
        # без строки потом не убрал бы никто
        shutil.rmtree(partial, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        raise
    return info.package, "updated" if role == "current" else "pending"


def _account(c, source: str, gid: int | None):
    """Google-аккаунт, которым качать из Play; у прочих источников — None."""
    if source != "play":
        return None
    acct = people.get_google(c, gid) if gid else None
    if acct is None:
        raise SourceError(ACCOUNT_GONE)
    return acct


def _own_shelf(shelf: db.Shelf, account) -> bool:
    """Купленное — только на Android-полку участника, чей аккаунт купил (spec §6.4): не публичная и не чужая."""
    return account is not None and shelf.user_id is not None and shelf.user_id == account.user_id


def play_expired(env, c, gid: int) -> None:
    """Google Play не принял токен аккаунта: подключение истекло, его задания ждут нового. Письмо — только о
    Google-аккаунте владельца, одно на эпизод (как jobs.mark_expired); участники видят баннер."""
    now = env.now()
    people.set_play(c, gid, "expired", now)
    g = people.get_google(c, gid)
    user = people.get_user(c, g.user_id) if g is not None else None
    if user is not None and user.role == people.OWNER:
        notify.once_for(lambda: people.claim_play_mail(c, gid, now), lambda: people.release_play_mail(c, gid),
                        PLAY_EXPIRED_SUBJECT,
                        f"Google Play не принял ключ {g.email} (сменили пароль или отозвали доступ): скачивание и "
                        f"ночная проверка из Play этим аккаунтом стоят, задания ждут.\n"
                        f"Подключить заново: {env.cfg.public_base}/apple\n", env.send, env.cfg.mail_to)


def _download(env, c, shelf: db.Shelf, source: str, ref: str, work: Path, *, expected_package: str | None,
              name_hint: str, account=None, ack: bool = False) -> tuple[str, str, str]:
    """Скачать из источника в рабочий каталог и положить на полку — под замком скачивания: (пакет, итог, метка
    карточки). account — Google-аккаунт источника play. Сверка с RuStore (план этапа 5): APKPure — подпись обязана
    совпасть, нет пакета в RuStore — нужна галочка ack (у приложения на полке подпись уже задана) и метка «не
    сверена»; AppGallery и F-Droid — другая подпись не отказ, а метка."""
    with jobs.hold(env.cfg.download_lock):
        shelf = db.get_shelf(c, shelf.id) or shelf  # токен каталога мог смениться (rotate_shelf), пока ждали замок
        _space(env, c)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            reference = env.sources.rustore_signature(ref) if source == "apkpure" else None
            if source == "apkpure" and reference is None and not ack and not db.has_current(c, shelf.id, ref):
                raise SourceError(NEED_ACK)
            got = env.sources.fetch(source, ref, work / "app.apk", account=account,
                                    allow_paid=_own_shelf(shelf, account))
            expected, signer_from, notice = got.expected_signer, "", ""
            if source == "apkpure":
                expected, signer_from, notice = (reference, "rustore", "") if reference else ("", "", NOT_VERIFIED)
            elif source in ("appgallery", "fdroid"):
                reference = env.sources.rustore_signature(got.ref or ref)
                if reference and apk.read_apk(work / "app.apk").signer != reference:
                    notice = SIGNER_DIFFERS
            package, result = store_apk(env, c, shelf, work / "app.apk", source=source, source_ref=got.ref or ref,
                                        expected_package=expected_package, expected_signer=expected,
                                        name_hint=name_hint or got.name, icon=got.icon,
                                        google_account_id=account.id if account is not None else None, paid=got.paid,
                                        signer_from=signer_from)
            if notice and result != "same":
                db.set_status(c, shelf.id, package, "ok", notice)
            app = db.get_app(c, shelf.id, package)
            if got.ref and got.ref != ref and app is not None and (app["source"], app["source_ref"]) == (source, ref):
                db.set_source(c, shelf.id, package, source, got.ref)  # AppGallery: ID со страницы → пакет
            if got.etag and app is not None and (app["source"], app["source_ref"]) == (source, ref):
                db.set_etag(c, shelf.id, package, got.etag)
            if source == "play" and app is not None:
                db.set_paid(c, shelf.id, package, got.paid)
                if got.name and app["name"] == package:  # панель знала только пакет — название из Play
                    db.set_name(c, shelf.id, package, got.name)
            return package, result, notice
        finally:
            shutil.rmtree(work, ignore_errors=True)


def check_app(env, c, shelf: db.Shelf, app, work: Path) -> str:
    """Проверка обновления карточки (кроме своего файла): "updated" | "pending" | "same". RuStore, Google Play,
    AppGallery, APKPure: versionCode не больше известного (текущей или ждущей «Принять») — без скачивания; ссылка
    (ETag/Last-Modified/Length) и F-Droid (рекомендуемая версия) — метка та же — тоже. Иначе — скачать и сравнить
    versionCode самого APK. Новая версия из Play или APKPure только набором файлов — "same" и отметка у карточки
    (текущая остаётся); эту версию Play следующие ночи уже не запрашивают. Метка сверки с RuStore без скачивания
    остаётся."""
    pkg = app["package"]
    account = _account(c, app["source"], app["google_account_id"])
    latest = env.sources.latest(app["source"], app["source_ref"], app["source_etag"], account=account)
    known = c.execute("SELECT MAX(version_code) AS v FROM android_versions WHERE shelf_id=? AND package=? "
                      "AND role IN ('current', 'pending')", (shelf.id, pkg)).fetchone()["v"]
    split_mark = f"split:{latest.version_code}" if app["source"] == "play" else ""
    note = ""
    if known is not None and (latest.version_code is not None and latest.version_code <= known
                              or latest.version_code is None and not latest.changed):
        result = "same"
        if app["last_error"] in NOTICES:  # не скачивали — сверку с RuStore не повторяли
            note = app["last_error"]
    elif known is not None and split_mark and app["source_etag"] == split_mark:
        result, note = "same", SPLIT_UPDATE
    else:
        try:
            _, result, note = _download(env, c, shelf, app["source"], app["source_ref"], work, expected_package=pkg,
                                        name_hint=app["name"], account=account)
        except SplitOnly:
            if known is None:  # первой версии нет — это ошибка добавления, а не отметка
                raise
            result, note = "same", SPLIT_UPDATE
    db.set_status(c, shelf.id, pkg, "ok", note)
    db.set_checked(c, shelf.id, pkg, env.now(), result, split_mark if note == SPLIT_UPDATE else latest.etag or None)
    return result


def check_all(env, c, work: Path) -> str:
    """Ночная проверка всех Android-полок (spec §9): "" — проверять нечего; ошибка одного приложения не
    останавливает остальные. pending считается обновлением. Google Play не принял токен аккаунта — подключение
    истекло: его приложения этой ночью пропускаются и в итог не идут, остальные аккаунты и источники — дальше."""
    apps = db.apps_to_check(c)
    if not apps:
        return ""
    updated = same = errors = 0
    for app in apps:
        shelf = db.get_shelf(c, app["shelf_id"])
        if shelf is None:
            continue
        if app["source"] == "play":
            acct = people.get_google(c, app["google_account_id"])
            if acct is not None and acct.play != "ok":  # истекло раньше этой ночью
                continue
        try:
            if check_app(env, c, shelf, app, work / f"{shelf.id}-{app['package']}") == "same":
                same += 1
            else:
                updated += 1
        except store.AppGone:  # сняли с полки посреди проверки
            continue
        except play.PlayAuthError:
            play_expired(env, c, app["google_account_id"])
            continue
        except Exception as e:
            if isinstance(e, jobs.LowSpace):
                jobs.low_space(env, c, e)
            errors += 1
            app_failed(c, shelf.id, app["package"], e, env.now())
    return f"Android: обновлено {updated}, без изменений {same}, ошибок {errors}"


def app_failed(c, shelf_id: int, package: str, e: Exception, now: str) -> None:
    """Ошибка — у карточки. Есть текущая версия — приложение остаётся «ok» со строкой ошибки и итогом проверки error:
    ставить можно, повторит ночь или «Проверить обновление»."""
    if db.get_app(c, shelf_id, package) is None:
        return
    if db.has_current(c, shelf_id, package):
        db.set_status(c, shelf_id, package, "ok", describe(e))
        db.set_checked(c, shelf_id, package, now, "error")
    else:
        db.set_status(c, shelf_id, package, "error", describe(e))


def run_job(env, c, job) -> None:
    """Задание обработчика: add (payload source, ref, name; play — и account, apkpure — и ack), upload (path, name,
    package?), check (package). Google Play не принял токен — задание снова в очереди, подключение истекло (ждёт нового)."""
    shelf = db.get_shelf(c, job["shelf_id"])
    payload = json.loads(job["payload"]) if job["payload"] else {}
    package = job["package"] or payload.get("package", "")
    work = env.cfg.tmp_dir / "web" / f"android-{job['id']}"
    gid = None
    try:
        if shelf is None:
            raise store.AppGone(package)
        card = db.get_app(c, shelf.id, package) if package else None
        if card is not None and card["source"] == "play":
            gid = card["google_account_id"]
        if package:
            db.set_status(c, shelf.id, package, "downloading")
        if job["kind"] == "add":
            gid = payload.get("account") or gid
            _download(env, c, shelf, payload["source"], payload["ref"], work, expected_package=package or None,
                      name_hint=payload.get("name", ""), account=_account(c, payload["source"], gid),
                      ack=payload.get("ack") is True)
        elif job["kind"] == "upload":
            with jobs.hold(env.cfg.download_lock):
                _space(env, c)
                _, result = store_apk(env, c, shelf, Path(payload["path"]), source="upload", source_ref="",
                                      expected_package=package or None, expected_signer="",
                                      name_hint=payload.get("name", ""), icon=None)
            if result == "same":  # файл не лёг на полку — человек должен это увидеть, а карточка — не «скачивается»
                raise apk.ApkError("эта или более новая версия уже на полке — загрузите версию новее")
        else:
            app = db.get_app(c, shelf.id, package)
            if app is None:
                raise store.AppGone(package)
            check_app(env, c, shelf, app, work)
        db.finish_job(c, job["id"], "done", env.now())
    except store.AppGone:
        db.finish_job(c, job["id"], "cancelled", env.now(), "снято с полки во время скачивания")
    except play.PlayAuthError:  # как SessionExpired у Apple ID: задание ждёт, пока аккаунт не подключат заново
        db.requeue_job(c, job["id"])
        if package:
            db.set_status(c, shelf.id, package, "queued")
        if gid is not None:
            play_expired(env, c, gid)
    except Exception as e:  # ошибка задания — текст у карточки и у задания
        if isinstance(e, jobs.LowSpace):
            jobs.low_space(env, c, e)
        db.finish_job(c, job["id"], "error", env.now(), describe(e))
        if package:
            app_failed(c, shelf.id, package, e, env.now())
    finally:
        if job["kind"] == "upload":
            Path(payload["path"]).unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def rotate_shelf(cfg, c, shelf_id: int) -> db.Shelf | None:
    """Новый случайный токен каталога Android-полки: appshelf rotate-token и смена PIN публичной полки (spec §7, §8).
    Файлы внутри не меняются — ссылки на APK строятся на странице. Как jobs.move_shelf: новый токен сначала ложится
    в shelf_next, сбой посередине доделает jobs.move_shelves; под замком скачивания store_apk не пишет в прежний
    каталог. Идёт задание полки или чьё-то скачивание — AccountBusy, архив недоступен — ArchiveUnavailable; полку
    удалили, пока ждали замок, — None."""
    jobs._archive(cfg)
    try:
        with jobs.hold(cfg.download_lock, jobs.REMOVE_LOCK_WAIT):
            with store.tx(c):
                if c.execute("SELECT 1 FROM android_jobs WHERE shelf_id=? AND status='running'",
                             (shelf_id,)).fetchone() is not None:
                    raise jobs.AccountBusy(f"Android-полка №{shelf_id}")
                shelf = db.get_shelf(c, shelf_id)
                if shelf is None:
                    return None
                if not shelf.shelf_next:
                    db.set_shelf_next(c, shelf_id, secrets.token_hex(16))
            shelf = db.get_shelf(c, shelf_id)
            src, dst = cfg.archive / shelf.shelf, cfg.archive / shelf.shelf_next
            if src.is_dir() and not dst.exists():
                os.replace(src, dst)
            db.finish_shelf_move(c, shelf_id)
            return db.get_shelf(c, shelf_id)
    except IpatoolError as e:  # не дождались замка скачивания
        raise jobs.AccountBusy(f"Android-полка №{shelf_id}") from e


def recover(c, now: str) -> None:
    """После перезапуска appshelf-web (его tmp/web уже очищен): прерванные задания — снова в очередь, загрузки,
    чей файл пропал вместе с tmp, — ошибка с просьбой загрузить заново."""
    db.requeue_running(c)
    for job in c.execute("SELECT id, payload FROM android_jobs WHERE kind='upload' AND status='queued'").fetchall():
        path = json.loads(job["payload"] or "{}").get("path", "")
        if not path or not Path(path).exists():
            db.finish_job(c, job["id"], "error", now, UPLOAD_LOST)
