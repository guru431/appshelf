"""Общий поиск с промежуточными результатами и состоянием каждого источника.

GitHub проверяется только по явной ссылке или ссылке автора приложения, которое
пришло набором файлов. Поиска репозиториев по названию здесь нет.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..apk import PACKAGE_RE
from . import apkpure, appgallery, discovery, fdroid_index, github, links, play, rustore
from .net import FetchError, SourceError, StatusError

NAMES = {**links.NAMES, "fdroid_archive": "Архив F-Droid", "github": "GitHub автора"}
DOMAINS = {"rustore": "backapi.rustore.ru", "play": "play.google.com", "appgallery": appgallery.HOST,
           "apkpure": "api.pureapk.com", "fdroid": "f-droid.org", "fdroid_archive": "f-droid.org",
           "github": "github.com"}
MAX_PACKAGES = 5


@dataclass(frozen=True)
class Option:
    source: str
    ref: str
    package: str
    name: str
    version: str = ""
    reason: str = ""
    versions: tuple[tuple[str, str], ...] = ()
    account: int = 0
    related_package: str = ""   # APK автора может иметь другой пакет: не подменять карточку магазина


@dataclass(frozen=True)
class Results:
    options: tuple[Option, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class Update:
    kind: str                  # status | results | done
    source: str = ""
    state: str = ""            # running | done | error | skipped
    detail: str = ""
    result: Results | None = None


def events(src, query: str, *, accounts=(), split_packages=()):
    query = query.strip()[:200]
    options, errors, candidates, author_links, descriptions = {}, {}, {}, {}, {}
    split = set(split_packages)
    exact = query if PACKAGE_RE.fullmatch(query) else ""
    direct_repo = github.parse_ref(query)
    if query.startswith("https://"):
        try:
            kind, ref = links.parse(query)
            if kind in ("rustore", "play", "appgallery", "apkpure", "fdroid", "fdroid_archive"):
                exact = fdroid_index.split_ref(ref)[0] if kind.startswith("fdroid") else ref
                query = exact
        except (FetchError, SourceError):
            pass
    if exact:
        candidates[exact] = exact

    def add(option):
        options[(option.source, option.ref, option.account)] = option
        if option.package:
            candidates.setdefault(option.package, option.name)

    def snapshot():
        def rank(option):
            name, term = option.name.casefold(), query.casefold()
            return (0 if option.package == exact and exact or name == term else 1 if name.startswith(term)
                    else 2 if term in name else 3)
        group_rank = {}
        for option in options.values():
            key = option.package or option.related_package or option.ref
            group_rank[key] = min(group_rank.get(key, 9), rank(option))
        ordered = sorted(options.values(), key=lambda o: (group_rank[o.package or o.related_package or o.ref],
                         o.package or o.related_package or o.ref, list(NAMES).index(o.source)))
        return Results(tuple(ordered), tuple(errors.values()))

    def request(fn):
        src._wait()
        return fn()

    def run(source, detail, fn):
        # Первый yield обязательно до сетевого запроса: браузер уже видит, чего ждёт.
        yield Update("status", source, "running", detail)
        try:
            result = fn()
        except (FetchError, SourceError) as exc:
            absent = (isinstance(exc, (play.PlayGone, StatusError)) and getattr(exc, "status", 404) == 404
                      or isinstance(exc, SourceError) and str(exc).startswith(("нет в ", "AppGallery не нашёл")))
            if absent:
                yield Update("status", source, "done", "не найдено")
            else:
                message = str(exc)
                if "TimeoutError" in message or "timed out" in message:
                    match = re.search(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", message)
                    host = match.group() if match else DOMAINS[source]
                    message = f"{host}: источник не ответил вовремя — повторите поиск позже"
                errors.setdefault(source, f"{NAMES[source]}: {message[:200]}")
                yield Update("status", source, "error", message[:200])
            return None
        yield Update("status", source, "done", "проверено")
        return result

    if direct_repo:
        release = yield from run("github", direct_repo, lambda: request(lambda: src.github_app(direct_repo)))
        if release:
            add(Option("github", release.repository, "", release.name, release.version_name))
        yield Update("done", result=snapshot())
        return

    found = yield from run("rustore", "поиск — backapi.rustore.ru", lambda: request(
        lambda: [rustore.info(src.http, exact)] if exact else rustore.search(src.http, query)))
    for app in found or ():
        add(Option("rustore", app.package, app.package, app.name, app.version_name,
                   "платное — недоступно" if app.price > 0 else ""))
    yield Update("results", result=snapshot())

    for source, catalog in (("fdroid", src.fdroid_catalog), ("fdroid_archive", src.fdroid_archive)):
        detail = "загрузка каталога — f-droid.org" if getattr(catalog, "_apps", True) is None else "поиск в каталоге"
        found = yield from run(source, detail, lambda: catalog.search(query, src._wait))
        for app in found or ():
            build = app.select()
            choices = tuple((b.name, f"{app.package}@{b.code}") for b in app.choices())
            add(Option(source, app.package, app.package, app.name, build.name, versions=choices))
            if app.upstream:
                author_links.setdefault(app.package, []).append(app.upstream)
        yield Update("results", result=snapshot())

    public_play = {}
    if not exact:
        found = yield from run("play", "поиск пакетов — play.google.com", lambda: discovery.discover(
            src.http, query, wait=src._wait))
        for package, name in found or ():
            public_play[package] = name
            candidates.setdefault(package, name)
        yield Update("results", result=snapshot())

    packages = sorted(candidates, key=lambda p: (p != exact, candidates[p].casefold() != query.casefold(),
                                                p not in public_play, len(candidates[p])))[:MAX_PACKAGES]
    for position, package in enumerate(packages, 1):
        counter = f"{position}/{len(packages)} · {package}"
        for source, check in (("appgallery", lambda: appgallery.info(src.http, src.appgallery_host, package)),
                              ("apkpure", lambda: apkpure.info(src.http, package))):
            app = yield from run(source, f"{counter} — {DOMAINS[source]}", lambda: request(check))
            if app is not None:
                bundled = source == "apkpure" and app.kind != "apk"
                if bundled:
                    split.add(package)
                    descriptions[package] = getattr(app, "description", "")
                add(Option(source, package, package, app.name, app.version_name,
                           "набором файлов — из браузера не ставится" if bundled else ""))
            yield Update("results", result=snapshot())
        if not accounts and (package in public_play or package == exact):
            add(Option("play", package, package, candidates[package], reason="подключите Google Play в «Аккаунтах»"))
        for account in accounts:
            app = yield from run("play", counter, lambda: request(lambda: src._play(account).details(package)))
            if app is not None:
                add(Option("play", package, package, app.name, app.version_name,
                           "платное — не куплено этим аккаунтом" if app.paid and not app.owned else "", account=account.id))
        yield Update("results", result=snapshot())

    checked = set()
    for package in packages:
        if package not in split:
            continue
        from . import upstream
        repos = yield from run("github", f"ссылки автора в описании {package}", lambda: upstream.repositories(
            src.http, package, metadata_links=author_links.get(package, ()),
            description=descriptions.get(package, ""), wait=src._wait))
        for repo in repos or ():
            if repo.lower() in checked:
                continue
            checked.add(repo.lower())
            release = yield from run("github", f"проверка выпусков {repo}", lambda: request(lambda: src.github_app(repo)))
            if release:
                add(Option("github", release.repository, "", candidates[package], release.version_name,
                           related_package=package))
            yield Update("results", result=snapshot())
    if not checked:
        yield Update("status", "github", "skipped", "нет набора файлов со ссылкой на репозиторий автора")
    yield Update("done", result=snapshot())
