"""Подставные источники APK: тот же интерфейс, что у appshelf.android.sources.Sources, без сети."""
from __future__ import annotations

import shutil
from pathlib import Path

from appshelf.android import net
from appshelf.android.sources import Got, Latest


def _answer(value):
    if isinstance(value, Exception):
        raise value
    return value


class FakeHttp:
    """Подставной net.Http по адресам (без query): json, form — ответ JSON (или исключение); request — (статус,
    заголовки, тело); download — байты файла (или исключение); head — Meta. Вызовы — в calls."""

    def __init__(self, json_routes=None, files=None, heads=None, forms=None, raw=None):
        self.json_routes, self.files, self.heads = json_routes or {}, files or {}, heads or {}
        self.forms, self.raw = forms or {}, raw or {}
        self.calls = []

    def json(self, url, *, headers=None, body=None):
        self.calls.append(("json", url, headers, body))
        return _answer(self.json_routes[url.split("?")[0]])

    def form(self, url, data, *, headers=None):
        self.calls.append(("form", url, headers, data))
        return _answer(self.forms[url.split("?")[0]])

    def request(self, method, url, *, headers=None, body=None, max_bytes=net.MAX_JSON):
        self.calls.append(("request", method, url, headers))
        return _answer(self.raw[url])

    def download(self, url, dest, *, max_bytes=net.MAX_APK, headers=None):
        self.calls.append(("download", url, max_bytes))
        data = _answer(self.files[url])
        dest.write_bytes(data)
        return net.Meta(url, 200, '"e"', "", len(data), "")

    def head(self, url, headers=None):
        self.calls.append(("head", url))
        return _answer(self.heads[url])


class FakeSources:
    """fetch копирует apks[ref] в dest; latest — из latest[ref]. found — ответ search, got[ref] — что вернёт fetch
    (по умолчанию Got("", "", None)), fail[ref] — исключение fetch и latest, signatures[пакет] — подпись в RuStore
    (нет — None). Вызовы — в calls."""

    def __init__(self, apks: dict[str, Path] | None = None, latest: dict[str, Latest] | None = None):
        self.apks, self._latest = dict(apks or {}), dict(latest or {})
        self.found: list = []
        self.got: dict[str, Got] = {}
        self.fail: dict[str, Exception] = {}
        self.signatures: dict[str, str] = {}
        self.calls: list = []

    def rustore_signature(self, package: str) -> str | None:
        self.calls.append(("rustore_signature", package))
        return self.signatures.get(package)

    def search(self, query: str) -> list:
        self.calls.append(("search", query))
        return list(self.found)

    def latest(self, source: str, ref: str, stored_etag: str, **kw) -> Latest:
        self.calls.append(("latest", source, ref))
        if ref in self.fail:
            raise self.fail[ref]
        return self._latest[ref]

    def fetch(self, source: str, ref: str, dest: Path, **kw) -> Got:
        self.calls.append(("fetch", source, ref))
        if ref in self.fail:
            raise self.fail[ref]
        shutil.copyfile(self.apks[ref], dest)
        return self.got.get(ref, Got("", "", None))
