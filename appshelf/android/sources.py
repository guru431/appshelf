"""Источники APK этапа 1 (spec 2026-10-28 §6.1–6.2): RuStore и прямая ссылка; свой файл приходит из браузера и
источника не имеет. Запросы — последовательно, с паузой между ними (правила источников запрещают выкачку, §11)."""
from __future__ import annotations

import io
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import rustore
from .net import FetchError, Http, Meta, SourceError

__all__ = ["Got", "Latest", "SourceError", "Sources"]
ICON_MAX = 1 << 20


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


def _etag(meta: Meta) -> str:
    return f"{meta.etag}|{meta.last_modified}|{'' if meta.length is None else meta.length}"


class Sources:
    def __init__(self, http: Http, *, sleep=time.sleep, pause: float = 2.0, clock=time.monotonic):
        self.http, self.sleep, self.pause, self.clock = http, sleep, pause, clock
        self._last: float | None = None

    def search(self, query: str) -> list[rustore.StoreApp]:
        self._wait()
        return rustore.search(self.http, query)

    def latest(self, source: str, ref: str, stored_etag: str) -> Latest:
        if source == "rustore":
            self._wait()
            return Latest(rustore.info(self.http, ref).version_code, "", True)
        if source == "link":
            self._wait()
            etag = _etag(self.http.head(ref))
            return Latest(None, etag, etag != stored_etag or etag == "||")
        raise SourceError(f"источник «{source}» не проверяется")

    def fetch(self, source: str, ref: str, dest: Path) -> Got:
        """APK в dest. rustore — ref: пакет; link — ref: адрес."""
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
        raise SourceError(f"источник «{source}» не скачивается")

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
