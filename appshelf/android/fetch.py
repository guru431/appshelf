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
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from .. import apk, ipa, jobs, notify, people, store
from ..ipatool import IpatoolError
from . import db, fdroid_index, links, net, play
from .net import SourceError
from .sources import ACCOUNT_GONE, SplitOnly

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
SOURCE_NAMES = {"rustore": "RuStore", "link": "сайтом", "upload": "файлом", "play": "Google Play",
                "appgallery": "AppGallery", "fdroid": "F-Droid", "fdroid_archive": "архивом F-Droid",
                "izzy": "IzzyOnDroid", "github": "GitHub", "apkpure": "APKPure", "fourpda": "4PDA",
                "codeberg": "Codeberg", "gitlab": "GitLab"}
UPLOAD_LOST = "файл загрузки потерян при перезапуске сервера — загрузите его заново"
SPLIT_UPDATE = "новая версия только набором файлов — на полке прежняя"
SIGNER_DIFFERS = "подписан не так, как в RuStore: не встанет поверх версии оттуда"
NOT_VERIFIED = "подпись не сверена: пакета нет в RuStore"
NEED_ACK = ("нужно подтверждение для APKPure: пакета нет в RuStore, подпись не с чем сверить — добавьте ещё раз "
            "с галочкой")
NOTICES = (SIGNER_DIFFERS, NOT_VERIFIED)   # метки сверки у карточки: проверка «новее нет» их не стирает
PACKAGE_REFS = ("rustore", "play", "fdroid", "apkpure")   # ref — пакет: файл обязан быть им
NOT_CHANGED = " — источник не сменён"
PLAY_EXPIRED_SUBJECT = "appshelf: подключение Google Play истекло"


def shelf_root(cfg, shelf: db.Shelf) -> Path:
    return cfg.archive / shelf.shelf


def file_url(cfg, shelf: db.Shelf, dir_name: str, file: str) -> str:
    return f"{cfg.public_base}/d/{shelf.shelf}/{dir_name}/{file}"


def app_url(cfg, shelf: db.Shelf, package: str) -> str:
    """Постоянная ссылка на приложение — всегда текущая версия (web/android.py, /apk/)."""
    return f"{cfg.public_base}/apk/{shelf.shelf}/{package}.apk"


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
               paid: bool = False, signer_from: str = "", allow_older: bool = False) -> tuple[str, str]:
    """APK из path — на полку: (пакет, "updated" | "saved" | "pending" | "same"). path переносится на полку или остаётся
    вызывающему. expected_package — карточка уже есть (RuStore, проверка, новая версия файлом): нет её — AppGone;
    без него карточка появляется, когда пакет стал известен (ссылка, свой файл). signer_from — чья подпись
    expected_signer, если не самого источника (APKPure сверяется с RuStore)."""
    cfg, now = env.cfg, env.now()
    info = apk.read_apk(path)
    info = replace(info, signer=apk.verify_apk(path))
    live = db.get_shelf(c, shelf.id)
    if (live is None or live.user_id != shelf.user_id or live.shelf != shelf.shelf
            or live.user_id is not None and people.get_user(c, live.user_id) is None):
        raise store.AppGone(info.package)
    if expected_package and info.package != expected_package:
        raise apk.ApkError(f"в файле приложение {info.package}, а на карточке — {expected_package}")
    if expected_signer and info.signer != expected_signer:
        raise apk.ApkError(f"подпись не совпала с {SOURCE_NAMES.get(signer_from or source, source)}")
    app = db.get_app(c, shelf.id, info.package)
    if app is None and expected_package:
        raise store.AppGone(info.package)
    roles = {r["role"]: r["version_code"] for r in c.execute(
        "SELECT role, version_code FROM android_versions WHERE shelf_id=? AND package=?", (shelf.id, info.package))}
    if not allow_older and "current" in roles and info.version_code <= roles["current"]:
        return info.package, "same"
    size = path.stat().st_size
    digest = ipa.sha256(path)  # по локальному файлу в tmp: с шары читать вдвое дольше
    if db.rejected_apk(c, shelf.id, info.package, digest):
        return info.package, "same"
    if c.execute("SELECT 1 FROM android_versions WHERE shelf_id=? AND package=? AND sha256=?",
                 (shelf.id, info.package, digest)).fetchone():
        return info.package, "same"
    rotation = apk.verified_rotation(path, app["signer"], info.signer) if app is not None else 0
    free = env.disk_free(jobs._archive(cfg))
    if free - size < jobs.RESERVE:
        raise jobs.LowSpace(f"мало места в архиве: свободно {free / jobs.GB:.1f} ГБ, APK — {size / jobs.GB:.1f} ГБ, "
                            f"запас — {jobs.RESERVE // jobs.GB} ГБ")
    if app is None:
        if db.get_shelf(c, shelf.id) is None:  # участника удалили, пока качали: карточку в ничью полку не ставим
            raise store.AppGone(info.package)
        title = info.label or name_hint.strip() or apk.resource_label(path) or info.package
        db.put_app(c, shelf.id, info.package, title, source, source_ref, now, google_account_id, paid)
    elif app["name"] == info.package:  # карточка заведена по пакету (F-Droid, Play) — название из источника или файла
        if title := name_hint.strip() or info.label or apk.resource_label(path):
            db.set_name(c, shelf.id, info.package, title)
    root = shelf_root(cfg, shelf)
    root.mkdir(exist_ok=True)
    root.chmod(0o711)  # UMask служб 0077, а в каталог полки заходит Apache (www-data)
    # другой ключ — версия ляжет pending (db.add_version): каталог со случайным именем, до решения не скачать
    pending = app is not None and bool(app["signer"]) and app["signer"] != info.signer
    name = db.version_dir(c, shelf.id, root, info.package, info.version_code, pending=pending)
    final, partial = root / name, root / f".{name}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        partial.mkdir(parents=True)
        apk_file = partial / apk_name(info.package, info.version_name)
        shutil.move(path, apk_file)  # tmp локально, архив на шаре: os.replace дал бы EXDEV
        (partial / "icon.png").write_bytes(apk.extract_icon(apk_file) or icon or apk.resource_icon(apk_file)
                                           or ipa.placeholder_png())
        for f in partial.iterdir():
            f.chmod(0o644)  # UMask служб 0077, а файлы отдаёт Apache (www-data)
        partial.chmod(0o755)
        os.replace(partial, final)
        role = db.add_version(c, root, shelf.id, info.package, db.NewApk(
            version_name=info.version_name, version_code=info.version_code, min_sdk=info.min_sdk,
            target_sdk=info.target_sdk, abis=",".join(info.abis), signer=info.signer, source=source, size=size,
            sha256=digest, dir=name, signature_verified=True, rotation_min_sdk=rotation), now)
    except BaseException:
        # обрыв шары, «database is locked», сняли с полки (AppGone): строки версии нет, а .partial или каталог
        # без строки потом не убрал бы никто
        shutil.rmtree(partial, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        raise
    return info.package, {"current": "updated", "kept": "saved", "pending": "pending"}[role]


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
              name_hint: str, account=None, ack: bool = False, allow_older: bool = False,
              source_page: str = "") -> tuple[str, str, str]:
    """Скачать из источника в рабочий каталог и положить на полку — под замком скачивания: (пакет, итог, метка
    карточки). account — Google-аккаунт источника play. Сверка с RuStore (план этапа 5): APKPure — подпись обязана
    совпасть, нет пакета в RuStore — нужна галочка ack (у приложения на полке подпись уже задана) и метка «не
    сверена»; AppGallery и F-Droid — другая подпись не отказ, а метка."""
    with jobs.hold(env.cfg.download_lock):
        shelf = finish_move(env.cfg, c, db.get_shelf(c, shelf.id))  # токен мог смениться, пока ждали замок
        if shelf is None or expected_package and db.get_app(c, shelf.id, expected_package) is None:
            raise store.AppGone(expected_package or ref)
        _space(env, c)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            reference = env.sources.rustore_signature(ref) if source == "apkpure" else None  # не ответил — отказ
            if source == "apkpure" and reference is None and not ack and not db.has_current(c, shelf.id, ref):
                env.sources.apkpure_app(ref)  # только XAPK — отказ сразу: галочка его не поставит
                raise SourceError(NEED_ACK)
            # У 4PDA учётка определяется владельцем полки, не параметром формы/задания.
            private = {"user_id": shelf.user_id} if source == "fourpda" else {}
            got = env.sources.fetch(source, ref, work / "app.apk", account=account,
                                    allow_paid=_own_shelf(shelf, account), **private)
            want = got.ref or (ref if source in PACKAGE_REFS else "")
            if source in fdroid_index.REPOSITORIES:
                want = fdroid_index.split_ref(ref)[0]
            if want and (found := apk.read_apk(work / "app.apk").package) != want:
                # сверяли подпись и карточку этого пакета, а пришёл другой — не на полку
                raise apk.ApkError(f"в файле приложение {found}, а в {SOURCE_NAMES[source]} — {want}")
            expected, signer_from, notice = got.expected_signer, "", ""
            if source == "apkpure":
                expected, signer_from, notice = (reference, "rustore", "") if reference else ("", "", NOT_VERIFIED)
            elif source == "appgallery" or source in fdroid_index.REPOSITORIES:
                try:
                    reference = env.sources.rustore_signature(want)
                except SourceError:  # метка — подсказка, не защита: RuStore не ответил — без неё
                    reference = None
                if reference and apk.read_apk(work / "app.apk").signer != reference:
                    notice = SIGNER_DIFFERS
            package, result = store_apk(env, c, shelf, work / "app.apk", source=source, source_ref=got.ref or ref,
                                        expected_package=expected_package, expected_signer=expected,
                                        name_hint=name_hint or got.name, icon=got.icon,
                                        google_account_id=account.id if account is not None else None, paid=got.paid,
                                        signer_from=signer_from, allow_older=allow_older)
            if notice and result in ("updated", "pending"):
                db.set_status(c, shelf.id, package, "ok", notice)
            if got.whats_new and result in ("updated", "pending"):
                row = c.execute("SELECT version_code FROM android_versions WHERE shelf_id=? AND package=? AND role=?",
                                (shelf.id, package, "current" if result == "updated" else "pending")).fetchone()
                if row is not None:
                    db.set_whats_new(c, shelf.id, package, row["version_code"], got.whats_new)
            app = db.get_app(c, shelf.id, package)
            if source == "fourpda" and app is not None and app["source"] == "fourpda":
                track_attachment = result == "updated"
                if result == "pending":
                    pending = c.execute("SELECT version_code FROM android_versions WHERE shelf_id=? AND package=? "
                                        "AND role='pending'", (shelf.id, package)).fetchone()
                    current = db.current_version(c, shelf.id, package)
                    track_attachment = current is None or pending["version_code"] > current["version_code"]
                if track_attachment:
                    db.set_source(c, shelf.id, package, source, ref, source_page or app["source_page"])
            if got.ref and got.ref != ref and app is not None and (app["source"], app["source_ref"]) == (source, ref):
                db.set_source(c, shelf.id, package, source, got.ref, got.page_url or app["source_page"])
            app = db.get_app(c, shelf.id, package)
            if app is not None and (app["source"], app["source_ref"]) == (source, got.ref or ref):
                page = links.page_url(source, ref, got.page_url or source_page or app["source_page"])
                if page:
                    c.execute("UPDATE android_apps SET source_page=? WHERE shelf_id=? AND package=?",
                              (page, shelf.id, package))
            if got.etag and app is not None and (app["source"], app["source_ref"]) == (source, ref):
                db.set_etag(c, shelf.id, package, got.etag)
            if source == "play" and app is not None:
                db.set_paid(c, shelf.id, package, got.paid)
            return package, result, notice
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _notice(error: str) -> str:
    return "\n".join(line for line in error.splitlines() if line in NOTICES)


def check_app(env, c, shelf: db.Shelf, app, work: Path, force: bool = False, *, retry_split: bool = False) -> str:
    """Проверка обновления карточки (кроме своего файла): "updated" | "pending" | "same". RuStore, Google Play,
    AppGallery, APKPure: versionCode не больше текущего принятого или та же версия источника ждёт решения по подписи
    либо отклонена — без скачивания; ссылка
    (ETag/Last-Modified/Length) и F-Droid (рекомендуемая версия) — метка та же — тоже. Иначе — скачать и сравнить
    versionCode самого APK. Новая версия из Play или APKPure только набором файлов — "same" и отметка у карточки
    (текущая остаётся); эту версию Play следующие ночи уже не запрашивают. Метка сверки с RuStore без скачивания
    остаётся. force (первая проверка после «Сменить источник») — скачивать, даже если новее нет, и набор файлов —
    отказ: новый источник проверяется своим файлом. retry_split — ручная проверка: повторить доставку ранее
    отклонённой версии, потому что Play мог начать отдавать её одним APK."""
    pkg = app["package"]
    account = _account(c, app["source"], app["google_account_id"])
    latest = env.sources.latest(app["source"], app["source_ref"], app["source_etag"], account=account)
    current = db.current_version(c, shelf.id, pkg)
    known = current["version_code"] if current is not None else None
    split_mark = f"split:{latest.version_code}" if app["source"] == "play" else ""
    note = ""
    pending = c.execute("SELECT source, version_code FROM android_versions WHERE shelf_id=? AND package=? "
                        "AND role='pending'", (shelf.id, pkg)).fetchone()
    # та же версия того же источника уже ждёт решения по подписи или отклонена — не скачивать её каждую ночь
    decided = latest.version_code is not None and (
        pending is not None and (pending["source"], pending["version_code"]) == (app["source"], latest.version_code)
        or db.rejected_code(c, shelf.id, pkg, app["source"], latest.version_code))
    if not force and known is not None and (latest.version_code is not None and latest.version_code <= known
                                            or latest.version_code is None and not latest.changed or decided):
        result = "same"
        note = _notice(app["last_error"])  # не скачивали — сверку с RuStore не повторяли
    elif not force and not retry_split and known is not None and split_mark and app["source_etag"] == split_mark:
        result, note = "same", SPLIT_UPDATE
    else:
        try:
            _, result, note = _download(env, c, shelf, app["source"], app["source_ref"], work, expected_package=pkg,
                                        name_hint=app["name"], account=account)
        except SplitOnly:
            if known is None or force:  # первой версии нет — это ошибка добавления, а не отметка
                raise
            result, note = "same", SPLIT_UPDATE
    db.set_status(c, shelf.id, pkg, "ok", note)
    db.set_checked(c, shelf.id, pkg, env.now(), result, split_mark if note == SPLIT_UPDATE else latest.etag or None)
    if result == "pending":
        _pending_mail(env, c, shelf, pkg)
    return result


def _pending_mail(env, c, shelf: db.Shelf, package: str) -> None:
    """Проверка нашла версию с другим ключом: на публичной полке и полке владельца — письмо владельцу, одно до
    решения (db.pending_mail_key). Полки участников решают сами участники — им письма нет."""
    user = people.get_user(c, shelf.user_id) if shelf.user_id is not None else None
    if shelf.user_id is not None and (user is None or user.role != people.OWNER):
        return
    app = db.get_app(c, shelf.id, package)
    p = c.execute("SELECT version_name, version_code, rotation_min_sdk FROM android_versions WHERE shelf_id=? "
                  "AND package=? AND role='pending'", (shelf.id, package)).fetchone()
    if app is None or p is None:
        return
    where, page = (("публичной Android-полке", "/android/public") if shelf.user_id is None
                   else ("вашей Android-полке", "/android"))
    chain = ("Разработчик подтвердил смену ключа подписанной цепочкой." if p["rotation_min_sdk"]
             else "Цепочка смены ключа не подтверждена: установка может потребовать удаления приложения с данными.")
    notify.once(c, db.pending_mail_key(shelf.id, package), f"appshelf: другая подпись у {app['name']}",
                f"На {where} у «{app['name']}» ({package}) новая версия {p['version_name']} ({p['version_code']}) "
                f"подписана другим ключом. {chain}\nПока решения нет, доступна прежняя версия.\n"
                f"Сравнить и решить: {env.cfg.public_base}{page}\n", env.send, env.cfg.mail_to, env.now())


def check_all(env, c, work: Path) -> str:
    """Ночная проверка всех Android-полок (spec §9): "" — проверять нечего; ошибка одного приложения не
    останавливает остальные. pending считается обновлением; в итоге — сколько версий на всех полках ждут решения по
    подписи (если есть). Google Play не принял токен аккаунта — подключение
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
    waiting = c.execute("SELECT COUNT(*) FROM android_versions WHERE role='pending'").fetchone()[0]
    return (f"Android: обновлено {updated}, без изменений {same}, ошибок {errors}"
            + (f", ждут решения по подписи: {waiting}" if waiting else ""))


def app_failed(c, shelf_id: int, package: str, e: Exception, now: str) -> None:
    """Ошибка — у карточки. Есть текущая версия — приложение остаётся «ok» со строкой ошибки и итогом проверки error:
    ставить можно, повторит ночь или «Проверить обновление»."""
    app = db.get_app(c, shelf_id, package)
    if app is None:
        return
    if db.has_current(c, shelf_id, package):
        note = _notice(app["last_error"])
        db.set_status(c, shelf_id, package, "ok", (note + "\n" if note else "") + describe(e))
        db.set_checked(c, shelf_id, package, now, "error")
    else:
        db.set_status(c, shelf_id, package, "error", describe(e))


def _copy_to_public(env, c, shelf: db.Shelf, package: str, source_id: int, work: Path) -> None:
    """Принятые APK полки владельца — независимыми копиями, без обращения к магазину.
    Повторный запуск дополняет историю, сохраняя настройки публичной карточки и проверку новой подписи."""
    with jobs.hold(env.cfg.download_lock):
        shelf = finish_move(env.cfg, c, db.get_shelf(c, shelf.id))
        if shelf is None:
            raise store.AppGone(package)
        _space(env, c)
        source = finish_move(env.cfg, c, db.get_shelf(c, source_id))
        user = people.get_user(c, source.user_id) if source is not None and source.user_id is not None else None
        original = db.get_app(c, source_id, package)
        version = db.current_version(c, source_id, package)
        if shelf.user_id is not None or user is None or user.role != people.OWNER:
            raise SourceError("Копировать в публичную можно только с полки владельца")
        if original is None or version is None:
            raise SourceError("Приложение больше не доступно на полке владельца")
        if original["paid"]:
            raise SourceError("Купленное в Google Play нельзя добавлять на публичную полку")
        target = db.get_app(c, shelf.id, package)
        if target is None:
            raise store.AppGone(package)
        fresh = not db.has_current(c, shelf.id, package)
        versions = c.execute("SELECT * FROM android_versions WHERE shelf_id=? AND package=? AND role!='pending' "
                             "ORDER BY (role='current') DESC, version_code, id", (source_id, package)).fetchall()
        work.mkdir(parents=True, exist_ok=True)
        path = work / "app.apk"
        for version in versions:
            if c.execute("SELECT 1 FROM android_versions WHERE shelf_id=? AND package=? AND sha256=?",
                         (shelf.id, package, version["sha256"])).fetchone() or db.rejected_apk(
                             c, shelf.id, package, version["sha256"]):
                continue
            # Дополнение истории не заменяет сборку, по которой владелец ещё не принял решение.
            current = db.current_version(c, shelf.id, package)
            if current is not None and current["signer"] != version["signer"] and c.execute(
                    "SELECT 1 FROM android_versions WHERE shelf_id=? AND package=? AND role='pending'",
                    (shelf.id, package)).fetchone():
                continue
            _space(env, c)
            folder = shelf_root(env.cfg, source) / version["dir"]
            try:
                shutil.copyfile(folder / apk_name(package, version["version_name"]), path)
                icon = (folder / "icon.png").read_bytes()
            except OSError:  # в пути — токен личной полки, а ошибку видят на публичной
                raise SourceError("APK на полке владельца сейчас недоступен — повторите позже") from None
            if ipa.sha256(path) != version["sha256"]:
                raise apk.ApkError("Сохранённый APK повреждён — контрольная сумма не совпала")
            store_apk(env, c, shelf, path, source=version["source"], source_ref=original["source_ref"],
                      expected_package=package, expected_signer=version["signer"], name_hint=original["name"], icon=icon,
                      allow_older=True)
        db.copy_whats_new(c, source_id, shelf.id, package)
        if fresh:
            db.set_source(c, shelf.id, package, original["source"], original["source_ref"], original["source_page"])
            c.execute("UPDATE android_apps SET google_account_id=? WHERE shelf_id=? AND package=?",
                      (original["google_account_id"], shelf.id, package))
            db.set_etag(c, shelf.id, package, original["source_etag"])
        notice = original["last_error"] if fresh else target["last_error"]
        db.set_status(c, shelf.id, package, "ok", notice if notice in NOTICES else "")


def run_job(env, c, job) -> None:
    """Задание обработчика: add (payload source, ref, name; play — и account, apkpure — и ack), copy (from_shelf), upload (path, name,
    package?), check (package; после «Сменить источник» — payload previous: отказ нового источника возвращает
    прежний). Google Play не принял токен — задание снова в очереди, подключение истекло (ждёт нового)."""
    shelf, payload = None, {}
    package = job["package"] or ""
    work = env.cfg.tmp_dir / "web" / f"android-{job['id']}"
    gid = None
    try:
        payload = json.loads(job["payload"]) if job["payload"] else {}
        package = package or payload.get("package", "")
        shelf = db.get_shelf(c, job["shelf_id"])
        if shelf is None:
            raise store.AppGone(package)
        card = db.get_app(c, shelf.id, package) if package else None
        if card is not None and card["source"] == "play":
            gid = card["google_account_id"]
        if package:
            db.set_status(c, shelf.id, package, "downloading")
        if job["kind"] == "add":
            gid = payload.get("account") or gid
            _, result, _ = _download(env, c, shelf, payload["source"], payload["ref"], work, expected_package=package or None,
                                    name_hint=payload.get("name", ""), account=_account(c, payload["source"], gid),
                                    ack=payload.get("ack") is True, allow_older=True,
                                    source_page=payload.get("source_page", ""))
            if result == "same":
                raise apk.ApkError("этот APK уже на полке или был отклонён — выберите другую сборку")
        elif job["kind"] == "copy":
            _copy_to_public(env, c, shelf, package, payload["from_shelf"], work)
        elif job["kind"] == "upload":
            with jobs.hold(env.cfg.download_lock):
                shelf = finish_move(env.cfg, c, db.get_shelf(c, shelf.id))  # токен мог смениться, пока ждали замок
                if shelf is None:
                    raise store.AppGone(package)
                _space(env, c)
                _, result = store_apk(env, c, shelf, Path(payload["path"]), source="upload", source_ref="",
                                      expected_package=package or None, expected_signer="",
                                      name_hint=payload.get("name", ""), icon=None, allow_older=True)
            if result == "same":  # файл не лёг на полку — человек должен это увидеть, а карточка — не «скачивается»
                raise apk.ApkError("этот APK уже на полке или был отклонён — выберите другую сборку")
        else:
            app = db.get_app(c, shelf.id, package)
            if app is None:
                raise store.AppGone(package)
            previous = payload.get("previous")  # «Сменить источник»: прежние (source, ref, etag) — откатить при отказе
            try:
                check_app(env, c, shelf, app, work, force=bool(previous), retry_split=True)
            except (apk.ApkError, net.FetchError, SourceError) as e:
                if not previous:
                    raise
                db.set_source(c, shelf.id, package, previous[0], previous[1], previous[3] if len(previous) > 3 else "")
                db.set_etag(c, shelf.id, package, previous[2] or "")
                raise SourceError(describe(e) + NOT_CHANGED) from e
        db.finish_job(c, job["id"], "done", env.now())
    except store.AppGone:
        db.finish_job(c, job["id"], "cancelled", env.now(), "снято с полки во время скачивания")
    except play.PlayAuthError:  # как SessionExpired у Apple ID: задание ждёт, пока аккаунт не подключат заново
        db.requeue_job(c, job["id"])
        if package:
            db.set_status(c, shelf.id, package, "queued")
        if gid is not None:
            play_expired(env, c, gid)
    except SplitOnly as e:
        payload["split_only"], payload["alternatives"] = True, list(e.alternatives)
        c.execute("UPDATE android_jobs SET payload=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), job["id"]))
        db.finish_job(c, job["id"], "error", env.now(), describe(e))
        if package:
            app_failed(c, shelf.id, package, e, env.now())
    except Exception as e:  # ошибка задания — текст у карточки и у задания
        if isinstance(e, jobs.LowSpace):
            jobs.low_space(env, c, e)
        db.finish_job(c, job["id"], "error", env.now(), describe(e))
        if package:
            app_failed(c, job["shelf_id"], package, e, env.now())
    finally:
        if job["kind"] == "upload" and isinstance(payload, dict) and payload.get("path"):
            Path(payload["path"]).unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def rotate_shelf(cfg, c, shelf_id: int, *, transaction=None) -> db.Shelf | None:
    """Новый случайный токен каталога Android-полки: appshelf rotate-token и смена PIN публичной полки (spec §7, §8).
    Файлы внутри не меняются — ссылки на APK строятся на странице. Как jobs.move_shelf: новый токен сначала ложится
    в shelf_next, сбой посередине доделает jobs.move_shelves или первая же запись либо удаление в каталоге полки
    (finish_move); под замком скачивания store_apk не пишет в прежний каталог. Идёт задание полки или чьё-то скачивание — AccountBusy, архив недоступен — ArchiveUnavailable; полку
    удалили, пока ждали замок, — None. transaction позволяет сменить PIN вместе с записью shelf_next:
    после COMMIT незавершённый перенос доделывается уже с новым PIN."""
    jobs._archive(cfg)
    try:
        with jobs.hold(cfg.download_lock, jobs.REMOVE_LOCK_WAIT):
            with transaction if transaction is not None else store.tx(c):
                if c.execute("SELECT 1 FROM android_jobs WHERE shelf_id=? AND status='running'",
                             (shelf_id,)).fetchone() is not None:
                    raise jobs.AccountBusy(f"Android-полка №{shelf_id}")
                shelf = db.get_shelf(c, shelf_id)
                if shelf is None:
                    return None
                if not shelf.shelf_next:
                    db.set_shelf_next(c, shelf_id, secrets.token_hex(16))
            _move_dirs(cfg, db.get_shelf(c, shelf_id))
            db.finish_shelf_move(c, shelf_id)
            return db.get_shelf(c, shelf_id)
    except IpatoolError as e:  # не дождались замка скачивания
        raise jobs.AccountBusy(f"Android-полка №{shelf_id}") from e


def _move_dirs(cfg, shelf: db.Shelf) -> None:
    """Каталог полки — на shelf_next, под замком скачивания вызывающего. Оба каталога есть (перенос прервался, а
    прежний код успел положить версию в старый) — версии, которых нет в новом, переезжают туда (как jobs._move_files)."""
    src, dst = cfg.archive / shelf.shelf, cfg.archive / shelf.shelf_next
    if src.is_dir() and not dst.exists():
        os.replace(src, dst)
    elif src.is_dir():
        for d in src.iterdir():
            if db.ANDROID_DIR_RE.match(d.name) and not (dst / d.name).exists():
                os.replace(d, dst / d.name)
        with suppress(OSError):  # остались чужие файлы — их покажет appshelf check
            src.rmdir()


def finish_move(cfg, c, shelf: db.Shelf | None) -> db.Shelf | None:
    """Под замком скачивания: смена токена прервалась (shelf_next) — доделать, прежде чем писать в каталог полки или
    удалять из него, иначе файлы лягут в каталог отозванного токена, а строки — к новому. Сбой шары —
    ArchiveUnavailable (без пути: в нём токен)."""
    if shelf is None or not shelf.shelf_next:
        return shelf
    try:
        _move_dirs(cfg, shelf)
    except OSError as e:
        raise jobs.ArchiveUnavailable(f"перенос каталога полки не завершён: {e.strerror or type(e).__name__}") from e
    db.finish_shelf_move(c, shelf.id)
    return db.get_shelf(c, shelf.id)


def recover(c, now: str) -> None:
    """После перезапуска appshelf-web (его tmp/web уже очищен): прерванные задания — снова в очередь, загрузки,
    чей файл пропал вместе с tmp, — ошибка с просьбой загрузить заново и у карточки (иначе «скачивается…» навсегда)."""
    db.requeue_running(c)
    for job in c.execute("SELECT id, shelf_id, package, payload FROM android_jobs "
                         "WHERE kind='upload' AND status='queued'").fetchall():
        path = json.loads(job["payload"] or "{}").get("path", "")
        if not path or not Path(path).exists():
            db.finish_job(c, job["id"], "error", now, UPLOAD_LOST)
            if job["package"]:
                app_failed(c, job["shelf_id"], job["package"], SourceError(UPLOAD_LOST), now)


def hide_pending(cfg, c) -> None:
    """При старте appshelf-web: версии pending, скачанные до случайных имён каталогов, — под случайное имя
    (db.hide_pending), под замком скачивания. Архив недоступен, замок занят, сбой шары — в журнал (без пути: в нём
    токен), повторит следующий старт; служба из-за этого не встаёт."""
    rows = [r for r in c.execute("SELECT id, shelf_id, dir FROM android_versions WHERE role='pending'").fetchall()
            if not db.PENDING_DIR_RE.search(r["dir"])]
    if not rows:
        return
    try:
        jobs._archive(cfg)
        with jobs.hold(cfg.download_lock, jobs.REMOVE_LOCK_WAIT):
            for r in rows:
                shelf = finish_move(cfg, c, db.get_shelf(c, r["shelf_id"]))
                if shelf is not None:
                    db.hide_pending(c, shelf_root(cfg, shelf), shelf.id, r["id"])
    except (jobs.ArchiveUnavailable, IpatoolError, OSError) as e:
        if isinstance(e, IpatoolError):
            why = "идёт скачивание"
        else:
            why = (e.strerror or type(e).__name__) if isinstance(e, OSError) else str(e)
        print(f"appshelf: версии, ждущие решения по подписи, не перенесены под случайное имя: {why}", file=sys.stderr)
