"""Общий поиск: кандидаты по названию, затем наличие APK в каждом источнике.

Магазины без открытого поиска по названию проверяются по найденным именам пакетов.
Сбой одного источника виден отдельно и не скрывает результаты остальных.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..apk import PACKAGE_RE
from . import apkpure, appgallery, discovery, fdroid_index, github, links, play, rustore
from .net import FetchError, SourceError, StatusError

NAMES = {**links.NAMES, "fdroid_archive": "Архив F-Droid", "github": "GitHub"}
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


@dataclass(frozen=True)
class Results:
    options: tuple[Option, ...]
    errors: tuple[str, ...]


def search(src, query: str, *, accounts=()) -> Results:
    query = query.strip()[:200]
    options, errors, candidates, upstream = {}, {}, {}, {}
    exact = query if PACKAGE_RE.fullmatch(query) else ""
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

    def failed(source, exc):
        # 404 и явный ответ «пакета нет» — отсутствие, всё остальное — неполный поиск.
        if isinstance(exc, (play.PlayGone, StatusError)) and getattr(exc, "status", 404) == 404:
            return
        if isinstance(exc, SourceError) and (str(exc).startswith(("нет в ", "AppGallery не нашёл"))):
            return
        message = str(exc)
        if "TimeoutError" in message or "timed out" in message:
            message = "источник не ответил вовремя — повторите поиск позже"
        errors.setdefault(source, f"{NAMES[source]}: {message[:200]}")

    try:
        if exact:
            src._wait()
            found = [rustore.info(src.http, exact)]
        else:
            found = src.search(query)
        for app in found:
            add(Option("rustore", app.package, app.package, app.name, app.version_name,
                       "платное — недоступно" if app.price > 0 else ""))
    except (FetchError, SourceError) as e:
        failed("rustore", e)

    for source, catalog in (("fdroid", src.fdroid_catalog), ("fdroid_archive", src.fdroid_archive)):
        try:
            for app in catalog.search(query, src._wait):
                build = app.select()
                choices = tuple((b.name, f"{app.package}@{b.code}") for b in app.choices())
                add(Option(source, app.package, app.package, app.name, build.name, versions=choices))
                repo = github.parse_ref(app.upstream)
                if repo and (query.casefold() in app.name.casefold() or app.package == exact):
                    upstream.setdefault(repo, app)
        except (FetchError, SourceError) as e:
            failed(source, e)

    public_play = {}
    if not exact:
        try:
            for package, name in discovery.discover(src.http, query, wait=src._wait):
                public_play[package] = name
                candidates.setdefault(package, name)
        except (FetchError, SourceError) as e:
            failed("play", e)

    # Точный пакет и совпадение названия имеют приоритет; не опрашивать магазины сотнями запросов.
    packages = sorted(candidates, key=lambda p: (p != exact, candidates[p].casefold() != query.casefold(),
                                                p not in public_play, len(candidates[p])))[:MAX_PACKAGES]
    for package in packages:
        for source, check in (("appgallery", lambda: appgallery.info(src.http, src.appgallery_host, package)),
                              ("apkpure", lambda: apkpure.info(src.http, package))):
            try:
                src._wait()
                app = check()
                reason = "набором файлов — из браузера не ставится" if source == "apkpure" and app.kind != "apk" else ""
                add(Option(source, package, package, app.name, app.version_name, reason))
            except (FetchError, SourceError) as e:
                failed(source, e)
        if not accounts:
            if package in public_play or package == exact:
                add(Option("play", package, package, candidates[package], reason="подключите Google Play в «Аккаунтах»"))
        for account in accounts:
            try:
                src._wait()
                app = src._play(account).details(package)
                reason = "платное — не куплено этим аккаунтом" if app.paid and not app.owned else ""
                add(Option("play", package, package, app.name, app.version_name, reason, account=account.id))
            except (FetchError, SourceError) as e:
                failed("play", e)

    seen = set()
    for repo, app in list(upstream.items())[:3]:
        try:
            src._wait()
            release = github.info(src.http, repo)
            seen.add(release.repository.lower())
            add(Option("github", release.repository, app.package, app.name, release.version_name))
        except (FetchError, SourceError) as e:
            failed("github", e)
    try:
        github_query = next((o.name for o in options.values() if o.package == exact and o.name != exact), query) if exact else query
        for app in github.search(src.http, github_query, wait=src._wait):
            if app.repository.lower() not in seen:
                add(Option("github", app.repository, "", app.name, app.version_name))
    except (FetchError, SourceError) as e:
        failed("github", e)
    def relevance(option):
        name = option.name.casefold()
        term = query.casefold()
        return (0 if option.package == exact and exact or name == term else 1 if name.startswith(term)
                else 2 if term in name else 3)

    group_rank = {}
    for option in options.values():
        key = option.package or option.ref
        group_rank[key] = min(group_rank.get(key, 9), relevance(option))
    ordered = sorted(options.values(), key=lambda o: (group_rank[o.package or o.ref], o.package or o.ref,
                                                      list(NAMES).index(o.source)))
    return Results(tuple(ordered), tuple(errors.values()))
