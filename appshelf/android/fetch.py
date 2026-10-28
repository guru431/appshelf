"""Конвейер Android (spec 2026-10-28 §6, §7, §9): APK из источника или из браузера → разбор и проверки → на полку
участника. Задания add / upload / check — обработчик appshelf-web (jobs.Worker), проверка обновлений — и ночью
(nightly.py). Скачивание и запись на полку — под общей блокировкой cfg.download_lock: IPA и APK по одному, иначе два
скачивания вместе прошли бы проверку места. Место — как у IPA: FREE_MIN до, RESERVE после."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .. import apk, ipa, jobs, notify, store
from . import db, net
from .net import SourceError

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
SOURCE_NAMES = {"rustore": "RuStore", "link": "сайтом", "upload": "файлом"}
UPLOAD_LOST = "файл загрузки потерян при перезапуске сервера — загрузите его заново"


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
              expected_signer: str, name_hint: str, icon: bytes | None) -> tuple[str, str]:
    """APK из path — на полку: (пакет, "updated" | "pending" | "same"). path переносится на полку или остаётся
    вызывающему. expected_package — карточка уже есть (RuStore, проверка, новая версия файлом): нет её — AppGone;
    без него карточка появляется, когда пакет стал известен (ссылка, свой файл)."""
    cfg, now = env.cfg, env.now()
    info = apk.read_apk(path)
    if expected_package and info.package != expected_package:
        raise apk.ApkError(f"в файле приложение {info.package}, а на карточке — {expected_package}")
    if expected_signer and info.signer != expected_signer:
        raise apk.ApkError(f"подпись не совпала с {SOURCE_NAMES.get(source, source)}")
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
        db.put_app(c, shelf.id, info.package, info.label or name_hint.strip() or info.package, source, source_ref, now)
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


def _download(env, c, shelf: db.Shelf, source: str, ref: str, work: Path, *, expected_package: str | None,
              name_hint: str) -> tuple[str, str]:
    """Скачать из источника в рабочий каталог и положить на полку — под замком скачивания."""
    with jobs.hold(env.cfg.download_lock):
        _space(env, c)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            got = env.sources.fetch(source, ref, work / "app.apk")
            package, result = store_apk(env, c, shelf, work / "app.apk", source=source, source_ref=ref,
                                        expected_package=expected_package, expected_signer=got.expected_signer,
                                        name_hint=name_hint or got.name, icon=got.icon)
            app = db.get_app(c, shelf.id, package)
            if got.etag and app is not None and (app["source"], app["source_ref"]) == (source, ref):
                db.set_etag(c, shelf.id, package, got.etag)
            return package, result
        finally:
            shutil.rmtree(work, ignore_errors=True)


def check_app(env, c, shelf: db.Shelf, app, work: Path) -> str:
    """Проверка обновления карточки rustore / link: "updated" | "pending" | "same". RuStore: versionCode не больше
    известного (текущей или ждущей «Принять») — без скачивания; ссылка: ETag/Last-Modified/Length те же — тоже.
    Иначе — скачать и сравнить versionCode самого APK."""
    pkg = app["package"]
    latest = env.sources.latest(app["source"], app["source_ref"], app["source_etag"])
    known = c.execute("SELECT MAX(version_code) AS v FROM android_versions WHERE shelf_id=? AND package=? "
                      "AND role IN ('current', 'pending')", (shelf.id, pkg)).fetchone()["v"]
    if known is not None and (latest.version_code is not None and latest.version_code <= known
                              or latest.version_code is None and not latest.changed):
        result = "same"
    else:
        _, result = _download(env, c, shelf, app["source"], app["source_ref"], work, expected_package=pkg,
                              name_hint=app["name"])
    db.set_status(c, shelf.id, pkg, "ok")
    db.set_checked(c, shelf.id, pkg, env.now(), result, latest.etag or None)
    return result


def check_all(env, c, work: Path) -> str:
    """Ночная проверка всех Android-полок (spec §9): "" — проверять нечего; ошибка одного приложения не
    останавливает остальные. pending считается обновлением."""
    apps = db.apps_to_check(c)
    if not apps:
        return ""
    updated = same = errors = 0
    for app in apps:
        shelf = db.get_shelf(c, app["shelf_id"])
        if shelf is None:
            continue
        try:
            if check_app(env, c, shelf, app, work / f"{shelf.id}-{app['package']}") == "same":
                same += 1
            else:
                updated += 1
        except store.AppGone:  # сняли с полки посреди проверки
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
    """Задание обработчика: add (payload source, ref, name), upload (path, name, package?), check (package)."""
    shelf = db.get_shelf(c, job["shelf_id"])
    payload = json.loads(job["payload"]) if job["payload"] else {}
    package = job["package"] or payload.get("package", "")
    work = env.cfg.tmp_dir / "web" / f"android-{job['id']}"
    try:
        if shelf is None:
            raise store.AppGone(package)
        if package:
            db.set_status(c, shelf.id, package, "downloading")
        if job["kind"] == "add":
            _download(env, c, shelf, payload["source"], payload["ref"], work, expected_package=package or None,
                      name_hint=payload.get("name", ""))
        elif job["kind"] == "upload":
            with jobs.hold(env.cfg.download_lock):
                _space(env, c)
                store_apk(env, c, shelf, Path(payload["path"]), source="upload", source_ref="",
                          expected_package=package or None, expected_signer="", name_hint=payload.get("name", ""),
                          icon=None)
        else:
            app = db.get_app(c, shelf.id, package)
            if app is None:
                raise store.AppGone(package)
            check_app(env, c, shelf, app, work)
        db.finish_job(c, job["id"], "done", env.now())
    except store.AppGone:
        db.finish_job(c, job["id"], "cancelled", env.now(), "снято с полки во время скачивания")
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


def recover(c, now: str) -> None:
    """После перезапуска appshelf-web (его tmp/web уже очищен): прерванные задания — снова в очередь, загрузки,
    чей файл пропал вместе с tmp, — ошибка с просьбой загрузить заново."""
    db.requeue_running(c)
    for job in c.execute("SELECT id, payload FROM android_jobs WHERE kind='upload' AND status='queued'").fetchall():
        path = json.loads(job["payload"] or "{}").get("path", "")
        if not path or not Path(path).exists():
            db.finish_job(c, job["id"], "error", now, UPLOAD_LOST)
