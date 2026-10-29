"""Источники APK (spec 2026-10-28 §6.1–6.2, §6.4–6.5): RuStore, прямая ссылка, Google Play (подключённым
Google-аккаунтом, android/play.py), AppGallery; свой файл приходит из браузера и источника не имеет. Запросы — последовательно, с паузой между ними
(правила источников запрещают выкачку, §11)."""
from __future__ import annotations

import io
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import appgallery, links, rustore
from .net import FetchError, Http, Meta, SourceError

__all__ = ["Got", "Latest", "SourceError", "Sources"]
ICON_MAX = 1 << 20
SPLIT_TEXT = "набором файлов — из браузера не ставится"
RUSTORE_HINT = "; есть в RuStore — смените источник"
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


class Sources:
    def __init__(self, http: Http, *, play=None, sleep=time.sleep, pause: float = 2.0, clock=time.monotonic,
                 appgallery_host: str = ""):
        """play — android.play.PlayPool (или подмена): client(Google-аккаунт) → клиент Play; appgallery_host —
        APPSHELF_APPGALLERY_HOST ("" — appgallery.HOST)."""
        self.http, self.play, self.sleep, self.pause, self.clock = http, play, sleep, pause, clock
        self.appgallery_host = appgallery_host or appgallery.HOST
        self._last: float | None = None

    def search(self, query: str) -> list[rustore.StoreApp]:
        self._wait()
        return rustore.search(self.http, query)

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
        raise SourceError(f"источник «{source}» не скачивается")

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
            raise SplitOnly(SPLIT_TEXT + (RUSTORE_HINT if self._free_in_rustore(package) else ""))
        self._wait()
        client.download(delivery, dest)
        return Got(app.name, "", self._icon(app.icon_url, dest), paid=app.paid)

    def _free_in_rustore(self, package: str) -> bool:
        try:
            self._wait()
            return rustore.info(self.http, package).price == 0
        except (FetchError, SourceError):
            return False

    def _icon(self, url: str, dest: Path) -> bytes | None:
        """Иконка источника как PNG; не скачалась или не картинка — None (будет из APK или заглушка)."""
        if not url:
            return None
        tmp = dest.with_name(dest.name + ".icon")
        try:
            self._wait()
            self.http.download(url, tmp, max_bytes=ICON_MAX)
            img = Image.open(tmp)
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
