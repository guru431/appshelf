"""Каталоги основного и архивного F-Droid: поиск, версии, ссылки на исходный GitHub.

index-v1.jar загружается по HTTPS, с пределами сжатого и распакованного размера. Как у
пакетного API F-Droid, подпись самого индекса пока не проверяется; APK сверяется с его
SHA-256 и сертификатом. Каталог в памяти обновляется раз в шесть часов, под замком.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import urllib.parse
import zipfile
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from ..apk import PACKAGE_RE
from .net import FetchError, Http, SourceError

MAX_JAR = 32 << 20
MAX_INDEX = 192 << 20
TTL = 6 * 3600
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def split_ref(ref: str) -> tuple[str, int | None]:
    package, sep, code = ref.partition("@")
    if not PACKAGE_RE.fullmatch(package) or sep and (len(code) > 20 or not code.isascii() or not code.isdigit()):
        raise SourceError("не похоже на пакет и версию F-Droid")
    return package, int(code) if sep else None


@dataclass(frozen=True)
class Build:
    name: str
    code: int
    filename: str
    sha256: str
    signer: str
    abis: tuple[str, ...]

    @property
    def arm_rank(self) -> int:
        return 0 if not self.abis else 1 if "arm64-v8a" in self.abis else 2 if "armeabi-v7a" in self.abis or "armeabi" in self.abis else 3


@dataclass(frozen=True)
class App:
    package: str
    name: str
    upstream: str
    suggested: int
    builds: tuple[Build, ...]

    def choices(self) -> tuple[Build, ...]:
        """По одной совместимой сборке каждой версии, новые сверху."""
        found = {}
        for build in sorted(self.builds, key=lambda b: (-b.code, b.arm_rank)):
            if build.arm_rank < 3 and (build.name not in found or build.arm_rank < found[build.name].arm_rank):
                found[build.name] = build
        return tuple(sorted(found.values(), key=lambda b: -b.code))

    def select(self, code: int | None = None) -> Build:
        if code is not None:
            found = next((b for b in self.builds if b.code == code and b.arm_rank < 3), None)
        else:
            suggested = next((b.name for b in self.builds if b.code == self.suggested), "")
            found = next((b for b in self.choices() if b.name == suggested), None)
            found = found or next(iter(self.choices()), None)
        if found is None:
            raise SourceError("в F-Droid нет этой версии для ARM")
        return found


class Catalog:
    def __init__(self, http: Http, *, archive: bool = False, clock=time.monotonic):
        self.http, self.clock = http, clock
        self.repo = f"https://f-droid.org/{'archive' if archive else 'repo'}/"
        self._apps: dict[str, App] | None = None
        self._until = 0.0
        self._lock = threading.Lock()

    def apps(self, wait=lambda: None) -> dict[str, App]:
        with self._lock:
            if self._apps is None or self.clock() >= self._until:
                wait()
                http = copy(self.http)
                if isinstance(http, Http):
                    http.timeout = min(http.timeout, 10.0)
                status, _, raw = http.request("GET", self.repo + "index-v1.jar", max_bytes=MAX_JAR)
                if status != 200:
                    raise FetchError(f"каталог F-Droid ответил {status}")
                self._apps = parse(raw)
                self._until = self.clock() + TTL
            return self._apps

    def info(self, ref: str, wait=lambda: None) -> tuple[App, Build]:
        package, code = split_ref(ref)
        app = self.apps(wait).get(package)
        if app is None:
            raise SourceError("нет в архиве F-Droid" if "/archive/" in self.repo else "нет в F-Droid")
        return app, app.select(code)

    def search(self, query: str, wait=lambda: None, limit: int = 15) -> list[App]:
        words = query.casefold().split()
        matches = [a for a in self.apps(wait).values()
                   if a.choices() and all(w in f"{a.name} {a.package}".casefold() for w in words)]
        return sorted(matches, key=lambda a: (a.package.casefold() != query.casefold(),
                                             a.name.casefold() != query.casefold(), a.name.casefold()))[:limit]

    def download(self, build: Build, dest: Path, wait=lambda: None) -> None:
        wait()
        self.http.download(self.repo + urllib.parse.quote(build.filename), dest)
        with dest.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        if digest != build.sha256:
            dest.unlink(missing_ok=True)
            raise SourceError("APK из F-Droid не совпал с SHA-256 в каталоге")


def parse(raw: bytes) -> dict[str, App]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as jar:
            entry = jar.getinfo("index-v1.json")
            if entry.file_size > MAX_INDEX:
                raise SourceError("каталог F-Droid слишком большой")
            data = json.loads(jar.read(entry))
        apps = {}
        for meta in data["apps"]:
            package = meta["packageName"]
            if not PACKAGE_RE.fullmatch(package):
                continue
            builds = []
            for b in data["packages"].get(package, []):
                filename, digest, signer = b["apkName"], b["hash"], b.get("signer", "")
                if not filename.endswith(".apk") or "/" in filename or "\\" in filename \
                        or b.get("hashType") != "sha256" or not SHA256_RE.fullmatch(digest) \
                        or signer and not SHA256_RE.fullmatch(signer):
                    continue
                builds.append(Build(str(b["versionName"]), int(b["versionCode"]), filename, digest,
                                    signer, tuple(b.get("nativecode") or ())))
            localized = meta.get("localized") or {}
            name = next((localized[k].get("name") for k in ("ru", "ru-RU", "en-US", "en")
                         if localized.get(k, {}).get("name")), None)
            name = name or meta.get("name") or next((v.get("name") for v in localized.values() if v.get("name")), package)
            apps[package] = App(package, str(name), str(meta.get("sourceCode", "")),
                                int(meta.get("suggestedVersionCode") or 0), tuple(builds))
        return apps
    except (ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise SourceError("каталог F-Droid ответил в неизвестном формате") from None
