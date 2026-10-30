"""Источники APK (spec 2026-10-28 §6.1–6.2, §6.4–6.5): RuStore, прямая ссылка, Google Play (подключённым
Google-аккаунтом, android/play.py), AppGallery, F-Droid, APKPure; свой файл приходит из браузера и источника не имеет.
Запросы — последовательно, с паузой между ними (правила источников запрещают выкачку, §11)."""
from __future__ import annotations

import io
import time
import threading
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .. import apk
from . import apkpure, appgallery, fdroid, fdroid_index, github, links, rustore
from .net import FetchError, Http, Meta, SourceError, StatusError

__all__ = ["Got", "Latest", "SourceError", "Sources"]
ICON_MAX = 1 << 20
SPLIT_TEXT = "набором файлов — из браузера не ставится"
RUSTORE_DOWN = "RuStore не ответил — подпись не с чем сверить, повторите позже"
NOT_OWNED = "платное — не куплено этим аккаунтом"
PAID_ELSEWHERE = "купленное в Google Play — только на личную полку того, чей аккаунт его купил"
ACCOUNT_GONE = "Google-аккаунт удалён — смените источник"


class SplitOnly(SourceError):
    """Google Play отдаёт приложение только набором файлов (splits, OBB): браузер его не поставит."""


@dataclass(frozen=True)
class Latest:
    version_code: int | None   # RuStore — из сведений; у ссылки версия известна только из самого APK
    etag: str                  # ссылка: "<ETag>|<Last-Modified>|<Content-Length>"; RuStore — ""
    changed: bool              # стоит ли скачивать (у RuStore решает сравнение version_code)


@dataclass(frozen=True)
class Got:
    name: str                  # название в источнике — подсказка, если в манифесте ссылка на ресурс
    expected_signer: str       # подпись, которую обещал источник ("" — сверять не с чем)
    icon: bytes | None         # PNG из источника
    etag: str = ""             # признаки файла по ссылке — чтобы первая ночь не качала его заново
    paid: bool = False         # Google Play: купленное
    ref: str = ""              # ref карточки, если источник его уточнил (AppGallery: пакет вместо ID со страницы)


def _etag(meta: Meta) -> str:
    return f"{meta.etag}|{meta.last_modified}|{'' if meta.length is None else meta.length}"


def _fdroid_mark(pkg: fdroid.FdPackage) -> str:
    """Метка F-Droid в source_etag: сменилась рекомендуемая версия — качать."""
    return f"fd:{pkg.suggested}"


class Sources:
    def __init__(self, http: Http, *, play=None, sleep=time.sleep, pause: float = 2.0, clock=time.monotonic,
                 appgallery_host: str = ""):
        """play — android.play.PlayPool (или подмена): client(Google-аккаунт) → клиент Play; appgallery_host —
        APPSHELF_APPGALLERY_HOST ("" — appgallery.HOST)."""
        self.http, self.play, self.sleep, self.pause, self.clock = http, play, sleep, pause, clock
        self.appgallery_host = appgallery_host or appgallery.HOST
        self._last: float | None = None
        self.fdroid_catalog = fdroid_index.Catalog(http, clock=clock)
        self.fdroid_archive = fdroid_index.Catalog(http, archive=True, clock=clock)
        self._github_cache = {}
        self._github_lock = threading.Lock()

    def search(self, query: str) -> list[rustore.StoreApp]:
        self._wait()
        return rustore.search(self.http, query)

    def search_all(self, query: str, *, accounts=(), split_packages=()):
        from .search import Results
        result = Results((), ())
        for update in self.search_events(query, accounts=accounts, split_packages=split_packages):
            if update.result is not None:
                result = update.result
        return result

    def search_events(self, query: str, *, accounts=(), split_packages=()):
        from .search import events
        # Обычное скачивание может ждать дольше; поиск должен вернуть хотя бы частичные
        # результаты до тайм-аута веб-сервера. Настройки общего HTTP-клиента не меняются.
        bounded = copy(self)
        bounded.http = copy(self.http)
        if isinstance(bounded.http, Http):
            bounded.http.timeout = min(bounded.http.timeout, 10.0)
        deadline = self.clock() + 90.0

        def wait():
            if self.clock() >= deadline:
                raise FetchError("поиск занял слишком долго — повторите с именем пакета")
            self._wait()

        bounded._wait = wait
        return events(bounded, query, accounts=accounts, split_packages=split_packages)

    def github_app(self, ref: str) -> github.GithubApp:
        """Повторный поиск и ночная проверка не расходуют квоту для того же выпуска."""
        with self._github_lock:
            key = ref.lower()
            cached = self._github_cache.get(key)
            if cached is not None and self.clock() < cached[0]:
                return cached[1]
            app = github.info(self.http, ref, wait=self._wait)
            self._github_cache[key] = (self.clock() + 900, app)
            return app

    def latest(self, source: str, ref: str, stored_etag: str, *, account=None) -> Latest:
        if source == "rustore":
            self._wait()
            return Latest(rustore.info(self.http, ref).version_code, "", True)
        if source == "link":
            self._wait()
            etag = _etag(self.http.head(ref))
            return Latest(None, etag, etag != stored_etag or etag == "||")
        if source == "play":
            client = self._play(account)
            self._wait()
            return Latest(client.details(ref).version_code, "", True)
        if source == "appgallery":
            return Latest(self._appgallery(ref).version_code, "", True)
        if source == "github":
            self._wait()
            mark = self.github_app(ref).mark
            return Latest(None, mark, mark != stored_etag)
        if source in ("fdroid", "fdroid_archive"):
            # не versionCode: рекомендуемая сборка бывает x86_64, а на полке — ARM с другим кодом
            if source == "fdroid_archive" or "@" in ref:
                _, build = self._fdroid_index(source).info(ref, self._wait)
                mark = f"{source}:{build.code}"
            else:
                self._wait()
                try:
                    mark = _fdroid_mark(fdroid.info(self.http, ref))
                except fdroid.MissingPackage:
                    _, build = self.fdroid_archive.info(ref, self._wait)
                    mark = f"fdroid_archive:{build.code}"
            return Latest(None, mark, mark != stored_etag)
        if source == "apkpure":
            self._wait()
            return Latest(apkpure.info(self.http, ref).version_code, "", True)
        raise SourceError(f"источник «{source}» не проверяется")

    def fetch(self, source: str, ref: str, dest: Path, *, account=None, allow_paid: bool = False) -> Got:
        """APK в dest. rustore, play — ref: пакет; link — ref: адрес; appgallery — пакет или ID со страницы (Got.ref —
        пакет). play — Google-аккаунтом account; купленное — только при allow_paid (полка того, чей аккаунт купил),
        отказ — до доставки."""
        if source == "play":
            return self._fetch_play(ref, dest, account, allow_paid)
        if source == "rustore":
            self._wait()
            app = rustore.info(self.http, ref)
            if app.price > 0:
                raise SourceError("платное — недоступно")
            if not app.signature:
                raise SourceError("RuStore не сообщил подпись приложения")
            self._wait()
            url = rustore.apk_url(self.http, app.app_id)
            self._wait()
            self.http.download(url, dest)
            return Got(app.name, app.signature, self._icon(app.icon_url, dest))
        if source == "link":
            self._wait()
            return Got("", "", None, _etag(self.http.download(ref, dest)))
        if source == "appgallery":
            app = self._appgallery(ref)
            self._wait()
            appgallery.download(self.http, app, dest)
            return Got(app.name, "", self._icon(app.icon_url, dest), ref=app.package)
        if source in ("fdroid", "fdroid_archive"):
            if source == "fdroid_archive" or "@" in ref:
                return self._fetch_fdroid_index(source, ref, dest)
            try:
                return self._fetch_fdroid(ref, dest)
            except fdroid.MissingPackage:
                return self._fetch_fdroid_index("fdroid_archive", ref, dest)
        if source == "github":
            self._wait()
            app = self.github_app(ref)
            self._wait()
            self.http.download(app.asset_url, dest)
            info = apk.read_apk(dest)
            if info.abis and not any(a in info.abis for a in ("arm64-v8a", "armeabi-v7a", "armeabi")):
                raise SourceError("в GitHub нет сборки для ARM")
            return Got(app.name, "", None, app.mark)
        if source == "apkpure":
            app = self.apkpure_app(ref)
            self._wait()
            apkpure.download(self.http, app, dest)
            return Got(app.name, "", self._icon(app.icon_url, dest))
        raise SourceError(f"источник «{source}» не скачивается")

    def apkpure_app(self, package: str) -> apkpure.PureApp:
        """Сведения APKPure; только набор файлов (XAPK) — SplitOnly с подсказкой, где пакет есть одним APK."""
        self._wait()
        app = apkpure.info(self.http, package)
        if app.kind != "apk":
            raise SplitOnly(SPLIT_TEXT + self._hint(package))
        return app

    def rustore_signature(self, package: str) -> str | None:
        """Подпись пакета в RuStore — образец для AppGallery, F-Droid и APKPure. Пакета нет (404) — None; RuStore не
        ответил или ответил не то — SourceError(RUSTORE_DOWN): «нет» и «не знаю» — не одно и то же."""
        try:
            self._wait()
            return rustore.info(self.http, package).signature or None
        except StatusError as e:
            if e.status == 404:
                return None
            raise SourceError(RUSTORE_DOWN) from None
        except (FetchError, SourceError):
            raise SourceError(RUSTORE_DOWN) from None

    def elsewhere(self, package: str) -> list[str]:
        """Где ещё пакет есть одним бесплатным APK — для подсказки к набору файлов (spec §6.4): RuStore (не платное),
        AppGallery, F-Droid. Сбой источника — «нет»."""
        found = []
        for name, check in (("RuStore", lambda: rustore.info(self.http, package).price == 0),
                            ("AppGallery", lambda: bool(appgallery.info(self.http, self.appgallery_host, package))),
                            ("F-Droid", lambda: bool(fdroid.info(self.http, package)))):
            try:
                self._wait()
                if check():
                    found.append(name)
            except (FetchError, SourceError):
                pass
        return found

    def _hint(self, package: str) -> str:
        found = self.elsewhere(package)
        return f"; есть в {', '.join(found)} — смените источник" if found else ""

    def _fdroid_index(self, source: str) -> fdroid_index.Catalog:
        return self.fdroid_archive if source == "fdroid_archive" else self.fdroid_catalog

    def _fetch_fdroid_index(self, source: str, ref: str, dest: Path) -> Got:
        catalog = self._fdroid_index(source)
        app, build = catalog.info(ref, self._wait)
        catalog.download(build, dest, self._wait)
        info = apk.read_apk(dest)
        if info.package != app.package or info.version_code != build.code:
            raise SourceError("APK из F-Droid не совпал с пакетом и версией в каталоге")
        if info.abis and not any(a in info.abis for a in ("arm64-v8a", "armeabi-v7a", "armeabi")):
            raise SourceError("в F-Droid нет сборки для ARM")
        return Got(app.name, build.signer, None, f"{source}:{build.code}")

    def _fetch_fdroid(self, package: str, dest: Path) -> Got:
        """Сборки рекомендуемой версии по очереди, пока не найдётся универсальная или arm64-v8a; нет ни одной —
        первая armeabi-v7a; нет и её — отказ. Скачанные мимо — удаляются."""
        self._wait()
        pkg = fdroid.info(self.http, package)
        fallback = None
        try:
            for code in fdroid.candidates(pkg):
                part = dest.with_name(f"{dest.name}.{code}")
                self._wait()
                self.http.download(fdroid.apk_url(package, code), part)
                abis = apk.read_apk(part).abis
                if not abis or "arm64-v8a" in abis:
                    part.replace(dest)
                    break
                if "armeabi-v7a" in abis and fallback is None:
                    fallback = part
                else:
                    part.unlink()
            else:
                if fallback is None:
                    raise SourceError("в F-Droid нет сборки для ARM")
                fallback.replace(dest)
                fallback = None
        finally:
            if fallback is not None:
                fallback.unlink(missing_ok=True)
        return Got("", "", None, _fdroid_mark(pkg))

    def _appgallery(self, ref: str) -> appgallery.AgApp:
        """ref — пакет или ID со страницы AppGallery (тогда сначала пакет по ID)."""
        if links.APPGALLERY_ID_RE.match(ref):
            self._wait()
            ref = appgallery.package_of(self.http, ref)
        self._wait()
        return appgallery.info(self.http, self.appgallery_host, ref)

    def _play(self, account):
        if account is None:
            raise SourceError(ACCOUNT_GONE)
        if self.play is None:
            raise SourceError("Google Play на этом сервере не настроен")
        return self.play.client(account)

    def _fetch_play(self, package: str, dest: Path, account, allow_paid: bool) -> Got:
        client = self._play(account)
        self._wait()
        app = client.details(package)
        if app.paid and not app.owned:
            raise SourceError(NOT_OWNED)
        if app.paid and not allow_paid:
            raise SourceError(PAID_ELSEWHERE)
        self._wait()
        delivery = client.delivery(app)
        if delivery.splits or delivery.extra_files:
            raise SplitOnly(SPLIT_TEXT + self._hint(package))
        self._wait()
        client.download(delivery, dest)
        return Got(app.name, "", self._icon(app.icon_url, dest), paid=app.paid)

    def _icon(self, url: str, dest: Path) -> bytes | None:
        """Иконка источника как PNG; не скачалась или не картинка — None (будет из APK или заглушка)."""
        if not url:
            return None
        tmp = dest.with_name(dest.name + ".icon")
        try:
            self._wait()
            self.http.download(url, tmp, max_bytes=ICON_MAX)
            with Image.open(tmp) as img:
                if img.width * img.height > apk.ICON_PIXELS:  # картинка-бомба: размер из заголовка, до load()
                    return None
                img.load()
                out = io.BytesIO()
                img.convert("RGBA").save(out, "PNG")
            return out.getvalue()
        except (FetchError, SourceError, OSError, ValueError, Image.DecompressionBombError):
            return None
        finally:
            tmp.unlink(missing_ok=True)

    def _wait(self) -> None:
        """Пауза между запросами к источникам: с прошлого запроса прошло меньше pause — доспать."""
        now = self.clock()
        if self._last is not None:
            left = self.pause - (now - self._last)
            if left > 0:
                self.sleep(left)
                now += left
        self._last = now
