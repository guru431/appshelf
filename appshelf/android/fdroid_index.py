"""Каталоги F-Droid, его архива и IzzyOnDroid: поиск, версии, ссылки на исходный GitHub.

entry.jar проверяется Java JarFile по закреплённому сертификату; его SHA-256 аутентифицирует index-v2.json.
Нормализованный кэш живёт на локальном диске; APK сверяется с SHA-256 принятого каталога.
"""
from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile
from copy import copy
from dataclasses import asdict, dataclass
from pathlib import Path

from ..apk import PACKAGE_RE, zip_directory
from ..ipatool import IpatoolError, hold
from .net import FetchError, Http, SourceError

MAX_JAR = 32 << 20
# В entry.jar и index-v1.jar — JSON каталога и META-INF подписи: единицы файлов. Без предела оглавление в 32 МБ
# раздуло бы zipfile до ~340 МБ ещё до проверки подписи.
JAR_ENTRIES, JAR_DIRECTORY = 64, 64 << 10
MAX_INDEX = 192 << 20
TTL = 6 * 3600
RETRY = 15 * 60  # после неудачного обновления каталога
MAX_AGE = 14 * 86400
# Official repository key: https://f-droid.org/docs/Release_Channels_and_Signing_Keys/
REPO_SIGNER = "43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab"
# Published at https://apt.izzysoft.de/fdroid/ (repository link with fingerprint).
IZZY_SIGNER = "3bf0d6abfeae2f401707b6d966be743bf0eee49c2561b9ba39073711f628937a"
REPOSITORIES = {
    "fdroid": ("https://f-droid.org/repo/", REPO_SIGNER, "repo.json"),
    "fdroid_archive": ("https://f-droid.org/archive/", REPO_SIGNER, "archive.json"),
    "izzy": ("https://apt.izzysoft.de/fdroid/repo/", IZZY_SIGNER, "izzy.json"),
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
wall_time = time.time


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
    min_sdk: int = 0
    target_sdk: int = 0

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
    whats_new: str = ""          # whatsNew index-v2 рекомендуемой версии (ru, иначе en) — только как текст
    whats_new_version: str = ""  # её versionName: у сборок одной версии под разные ABI текст общий

    def notes(self, build: Build) -> str:
        """«Что нового» для сборки; у другой версии текста в каталоге нет."""
        return self.whats_new if build.name == self.whats_new_version else ""

    def choices(self) -> tuple[Build, ...]:
        """По одной совместимой сборке каждой версии, новые сверху."""
        found = {}
        for build in sorted(self.builds, key=lambda b: (-b.code, b.arm_rank)):
            if build.arm_rank < 3 and (build.name not in found or build.arm_rank < found[build.name].arm_rank):
                found[build.name] = build
        return tuple(sorted(found.values(), key=lambda b: -b.code))

    def select(self, code: int | None = None) -> Build:
        if code is not None:
            found = next((b for b in self.builds if b.code == code), None)
        else:
            suggested = next((b.name for b in self.builds if b.code == self.suggested), "")
            found = next((b for b in self.choices() if b.name == suggested), None)
            found = found or next(iter(self.choices()), None)
        if found is None:
            raise SourceError("в F-Droid нет этой версии для ARM")
        return found


class Catalog:
    def __init__(self, http: Http, *, archive: bool = False, clock=time.monotonic,
                 cache_dir: Path | None = None, wall_clock=None, repository: str = "fdroid"):
        self.http, self.clock = http, clock
        self.wall_clock = wall_clock or wall_time
        self.repo, self.signer, cache_name = REPOSITORIES["fdroid_archive" if archive else repository]
        self.cache = cache_dir / cache_name if cache_dir else None
        self._apps: dict[str, App] | None = None
        self._until = 0.0
        self._timestamp = 0
        self.stale = False
        self._error: Exception | None = None  # последняя неудача обновления, до _retry_at не повторяется
        self._retry_at = 0.0
        self._lock = threading.Lock()

    def apps(self, wait=lambda: None, *, allow_stale=False) -> dict[str, App]:
        """Каталог; устарел — обновляется. Неудача обновления помнится RETRY секунд: при устойчивом сбое (отстающее
        зеркало, не та подпись) каждое приложение и каждый поиск иначе заново качали бы index-v2 и запускали JVM.
        Сеть недоступна, а allow_stale и прежний каталог есть — прежний, с пометкой stale."""
        with self._lock:
            if self._apps is None and self.cache and self.cache.exists():
                self._load_cache()
            if self._apps is None or self.clock() >= self._until:
                if self._error is not None and self.clock() < self._retry_at:
                    error = self._error
                else:
                    wait()  # отказ бюджета вызывающего (поиск) — не сбой каталога: его не запоминаем
                    try:
                        self._refresh()
                        self._error = None
                        return self._apps
                    except (FetchError, SourceError) as e:
                        error, self._error, self._retry_at = e, e, self.clock() + RETRY
                if not isinstance(error, FetchError) or not allow_stale or self._apps is None:
                    raise error.with_traceback(None)
                self.stale = True
            return self._apps

    def _refresh(self) -> None:
        http = copy(self.http)
        if isinstance(http, Http):
            http.timeout = min(http.timeout, 10.0)
        status, _, raw = http.request("GET", self.repo + "entry.jar", max_bytes=MAX_JAR)
        if status != 200:
            raise FetchError(f"каталог F-Droid ответил {status}")
        verify(raw, self.signer)
        try:  # один разбор: каталог index-v1 — до 192 МиБ JSON, вторая копия удвоила бы пик
            entry = _index_data(raw)
        except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise SourceError("каталог F-Droid ответил в неизвестном формате") from None
        timestamp = _timestamp(entry)
        if timestamp < self._timestamp or timestamp < (self.wall_clock() - MAX_AGE) * 1000 \
                or timestamp > (self.wall_clock() + 86400) * 1000:
            raise SourceError("устаревший каталог F-Droid или откат его версии")
        if "index" in entry:
            meta = entry["index"]
            if meta.get("name") != "/index-v2.json" or not SHA256_RE.fullmatch(str(meta.get("sha256", ""))) \
                    or not isinstance(meta.get("size"), int) or not 0 < meta["size"] <= MAX_INDEX:
                raise SourceError("неизвестный формат entry F-Droid")
            status, headers, body = http.request("GET", self.repo + "index-v2.json",
                headers={"Accept-Encoding": "gzip"}, max_bytes=MAX_INDEX)
            if status != 200:
                raise FetchError(f"каталог F-Droid ответил {status}")
            if headers.get("content-encoding", "").lower() == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                        body = stream.read(MAX_INDEX + 1)
                except (OSError, EOFError):
                    raise SourceError("сжатый каталог F-Droid повреждён") from None
            if len(body) != meta["size"] or hashlib.sha256(body).hexdigest() != meta["sha256"]:
                raise SourceError("каталог F-Droid не совпал с SHA-256 подписанного entry")
            apps = parse_v2(body)
        else:
            apps = _parse_v1(entry)
        try:
            self._save_cache(apps, timestamp)
        except (OSError, IpatoolError, ValueError, KeyError):
            raise SourceError("не удалось сохранить локальный кэш F-Droid; прежнее поколение сохранено") from None
        self._apps, self._timestamp = apps, timestamp
        self._until, self.stale = self.clock() + TTL, False

    def _load_cache(self):
        try:
            if self.cache.stat().st_size > MAX_INDEX:
                raise ValueError()
            data = json.loads(self.cache.read_bytes())
            if data["schema"] != 1 or data["repo"] != self.repo or data["signer"] != self.signer:
                raise ValueError()
            # whats_new — необязательные поля: кэш прежней версии читается, прежний код лишние поля App не читает
            self._apps = {p: App(p, a["name"], a["upstream"], a["suggested"],
                                tuple(Build(**{**b, "abis": tuple(b["abis"])}) for b in a["builds"]),
                                str(a.get("whats_new", "")), str(a.get("whats_new_version", "")))
                          for p, a in data["apps"].items()}
            self._timestamp = int(data["timestamp"])
            age = max(0, self.wall_clock() - float(data["checked_at"]))
            self._until = self.clock() + max(0, min(TTL - age,
                self._timestamp / 1000 + MAX_AGE - self.wall_clock()))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise SourceError("локальный кэш F-Droid повреждён; восстановите его из доверенной копии") from None

    def _save_cache(self, apps, timestamp):
        if self.cache is None:
            return
        self.cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with hold(self.cache.with_suffix(".lock"), wait=15):
            if self.cache.exists():
                saved = json.loads(self.cache.read_bytes())
                if int(saved["timestamp"]) > timestamp:
                    raise SourceError("откат версии каталога F-Droid")
            data = {"schema": 1, "repo": self.repo, "signer": self.signer, "timestamp": timestamp,
                    "checked_at": self.wall_clock(), "apps": {p: asdict(a) for p, a in apps.items()}}
            fd, name = tempfile.mkstemp(dir=self.cache.parent, suffix=".partial")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.cache)
            finally:
                Path(name).unlink(missing_ok=True)

    def info(self, ref: str, wait=lambda: None) -> tuple[App, Build]:
        package, code = split_ref(ref)
        app = self.apps(wait).get(package)
        if app is None:
            raise SourceError("нет в архиве F-Droid" if "/archive/" in self.repo else "нет в F-Droid")
        return app, app.select(code)

    def search(self, query: str, wait=lambda: None, limit: int = 15) -> list[App]:
        words = query.casefold().split()
        matches = [a for a in self.apps(wait, allow_stale=True).values()
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


def _index_entry(jar: zipfile.ZipFile) -> zipfile.ZipInfo:
    """Файл каталога — по составу архива, без разбора JSON: Java проверяет именно тот, что потом читается."""
    entries = jar.infolist()
    if len({e.filename for e in entries}) != len(entries):
        raise SourceError("повторяющиеся файлы в каталоге F-Droid")
    entry = jar.getinfo("entry.json" if "entry.json" in jar.namelist() else "index-v1.json")
    if entry.file_size > MAX_INDEX or sum(e.file_size for e in entries if e != entry) > 8 << 20:
        raise SourceError("каталог F-Droid слишком большой")
    return entry


def _open_jar(raw: bytes) -> zipfile.ZipFile:
    if len(raw) > MAX_JAR:
        raise SourceError("каталог F-Droid слишком большой")
    found = zip_directory(io.BytesIO(raw))
    if found is not None and (found[0] > JAR_ENTRIES or found[1] > JAR_DIRECTORY):
        raise SourceError("каталог F-Droid слишком большой")
    return zipfile.ZipFile(io.BytesIO(raw))


def _index_data(raw: bytes) -> dict:
    with _open_jar(raw) as jar:
        return json.loads(jar.read(_index_entry(jar)))


def verify(raw: bytes, signer: str | None = None) -> None:
    """Java's maintained JAR verifier; pinned certificate, signed JSON and full digest verification."""
    try:
        with _open_jar(raw) as jar:
            entry_name = _index_entry(jar).filename
        with tempfile.TemporaryDirectory(prefix="appshelf-fdroid-") as temp:
            path = Path(temp) / "index.jar"
            path.write_bytes(raw)
            helper = Path(__file__).resolve().parent.parent / "data" / "VerifyIndex.java"
            result = subprocess.run(["java", "-Xmx384m", "--source", "11", str(helper), str(path),
                                     signer or REPO_SIGNER, entry_name],
                                    capture_output=True, timeout=25)
            if result.returncode != 0 or result.stdout.strip() != b"verified":
                raise SourceError("подпись каталога F-Droid не прошла проверку доверенным ключом")
    except (OSError, subprocess.SubprocessError):
        raise SourceError("проверка каталога F-Droid недоступна: нужен Java 11+ (JDK)") from None
    except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise SourceError("каталог F-Droid ответил в неизвестном формате") from None


def index_timestamp(raw: bytes) -> int:
    try:
        data = _index_data(raw)
    except (KeyError, ValueError, TypeError):
        raise SourceError("в каталоге F-Droid нет корректного времени публикации") from None
    return _timestamp(data)


def _timestamp(data: dict) -> int:
    try:
        timestamp = int(data["timestamp"] if "index" in data else data["repo"]["timestamp"])
        if timestamp <= 0:
            raise ValueError()
        return timestamp
    except (KeyError, ValueError, TypeError):
        raise SourceError("в каталоге F-Droid нет корректного времени публикации") from None


def parse(raw: bytes) -> dict[str, App]:
    try:
        data = _index_data(raw)
    except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise SourceError("каталог F-Droid ответил в неизвестном формате") from None
    return _parse_v1(data)


def _parse_v1(data: dict) -> dict[str, App]:
    try:
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
                                    signer, tuple(b.get("nativecode") or ()), int(b.get("minSdkVersion") or 0),
                                    int(b.get("targetSdkVersion") or 0)))
            localized = meta.get("localized") or {}
            name = next((localized[k].get("name") for k in ("ru", "ru-RU", "en-US", "en")
                         if localized.get(k, {}).get("name")), None)
            name = name or meta.get("name") or next((v.get("name") for v in localized.values() if v.get("name")), package)
            apps[package] = App(package, str(name), str(meta.get("sourceCode", "")),
                                int(meta.get("suggestedVersionCode") or 0), tuple(builds))
        return apps
    except (ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise SourceError("каталог F-Droid ответил в неизвестном формате") from None


def parse_v2(raw: bytes) -> dict[str, App]:
    """Normalize the authenticated v2 format to the same small catalog used by the shelf."""
    try:
        data = json.loads(raw)
        apps = {}
        for package, app in data["packages"].items():
            if not PACKAGE_RE.fullmatch(package):
                continue
            meta, builds, notes = app["metadata"], [], {}
            preferred = meta.get("preferredSigner", "")
            for version in app.get("versions", {}).values():
                if version.get("releaseChannels"):
                    continue
                # Необязательный текст: его странный вид не должен ронять весь каталог
                texts = version.get("whatsNew") if isinstance(version.get("whatsNew"), dict) else {}
                text = next((texts[k] for k in ("ru", "ru-RU", "en-US", "en") if isinstance(texts.get(k), str)
                             and texts[k].strip()), "")
                file, manifest = version["file"], version["manifest"]
                filename = file["name"].removeprefix("/")
                digest = file["sha256"]
                signers = manifest.get("signer", {}).get("sha256", [])
                signer = signers[0] if len(signers) == 1 else ""
                if not filename.endswith(".apk") or "/" in filename or "\\" in filename \
                        or not SHA256_RE.fullmatch(digest) or not SHA256_RE.fullmatch(signer):
                    continue
                builds.append(Build(str(manifest["versionName"]), int(manifest["versionCode"]), filename,
                                    digest, signer, tuple(manifest.get("nativecode", ())),
                                    int((manifest.get("usesSdk") or {}).get("minSdkVersion") or 0),
                                    int((manifest.get("usesSdk") or {}).get("targetSdkVersion") or 0)))
                notes[builds[-1].code] = text
            names = meta.get("name", {})
            name = next((names[k] for k in ("ru", "ru-RU", "en-US", "en") if names.get(k)), None)
            name = name or next(iter(names.values()), package)
            suggested = max((b.code for b in builds if not preferred or b.signer == preferred), default=0)
            noted = next((b.name for b in builds if b.code == suggested), "") if notes.get(suggested) else ""
            apps[package] = App(package, str(name), str(meta.get("sourceCode", "")), suggested, tuple(builds),
                                notes.get(suggested, ""), noted)
        return apps
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SourceError("каталог F-Droid v2 ответил в неизвестном формате") from None
