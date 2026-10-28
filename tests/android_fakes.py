"""Подставные источники APK: тот же интерфейс, что у appshelf.android.sources.Sources, без сети."""
from __future__ import annotations

import shutil
from pathlib import Path

from appshelf.android.sources import Got, Latest


class FakeSources:
    """fetch копирует apks[ref] в dest; latest — из latest[ref]. found — ответ search, got[ref] — что вернёт fetch
    (по умолчанию Got("", "", None)), fail[ref] — исключение fetch и latest. Вызовы — в calls."""

    def __init__(self, apks: dict[str, Path] | None = None, latest: dict[str, Latest] | None = None):
        self.apks, self._latest = dict(apks or {}), dict(latest or {})
        self.found: list = []
        self.got: dict[str, Got] = {}
        self.fail: dict[str, Exception] = {}
        self.calls: list = []

    def search(self, query: str) -> list:
        self.calls.append(("search", query))
        return list(self.found)

    def latest(self, source: str, ref: str, stored_etag: str) -> Latest:
        self.calls.append(("latest", source, ref))
        if ref in self.fail:
            raise self.fail[ref]
        return self._latest[ref]

    def fetch(self, source: str, ref: str, dest: Path) -> Got:
        self.calls.append(("fetch", source, ref))
        if ref in self.fail:
            raise self.fail[ref]
        shutil.copyfile(self.apks[ref], dest)
        return self.got.get(ref, Got("", "", None))
