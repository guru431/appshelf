from types import SimpleNamespace

from android_fakes import FakeHttp
from appshelf.android import apkpure, appgallery, discovery, fdroid_index, github, net, rustore, sources

PKG = "com.example.app"


def setup_search(monkeypatch):
    src = sources.Sources(FakeHttp(), sleep=lambda s: None)
    app = fdroid_index.App(PKG, "Example", "https://github.com/example/android", 2, (
        fdroid_index.Build("2.0", 2, "app.apk", "a" * 64, "", ()),
        fdroid_index.Build("1.0", 1, "old.apk", "b" * 64, "", ())))
    src.fdroid_catalog = SimpleNamespace(search=lambda *a: [])
    src.fdroid_archive = SimpleNamespace(search=lambda *a: [app])
    monkeypatch.setattr(rustore, "search", lambda *a: [])
    monkeypatch.setattr(rustore, "info", lambda *a: (_ for _ in ()).throw(net.StatusError("404", 404)))
    monkeypatch.setattr(discovery, "discover", lambda *a, **kw: [(PKG, "Example")])
    monkeypatch.setattr(appgallery, "info", lambda *a: (_ for _ in ()).throw(net.SourceError("AppGallery не нашёл")))
    monkeypatch.setattr(apkpure, "info", lambda *a: SimpleNamespace(name="Example", version_name="3.0", kind="xapk"))
    release = github.GithubApp("example/android", "android", "v2.1", "https://github.com/example/android/releases/download/v2/app.apk", "app.apk", "gh:1")
    monkeypatch.setattr(github, "info", lambda *a, **kw: release)
    return src


def test_search_archive_versions_upstream_github_and_xapk(monkeypatch):
    src = setup_search(monkeypatch)
    result = src.search_all("Example")
    assert not result.errors
    found = {o.source: o for o in result.options}
    assert found["fdroid_archive"].versions == (("2.0", PKG + "@2"), ("1.0", PKG + "@1"))
    assert found["github"].package == "" and found["github"].related_package == PKG
    assert len([o for o in result.options if o.source == "github"]) == 1
    assert "набором" in found["apkpure"].reason
    assert "подключите" in found["play"].reason


def test_partial_source_failure_keeps_other_results(monkeypatch):
    src = setup_search(monkeypatch)
    monkeypatch.setattr(rustore, "search", lambda *a: (_ for _ in ()).throw(net.FetchError("503")))
    result = src.search_all("Example")
    assert result.errors == ("RuStore: 503",)
    assert any(o.source == "fdroid_archive" for o in result.options)


def test_exact_package_and_play_account_are_used(monkeypatch):
    src = setup_search(monkeypatch)
    account = SimpleNamespace(id=7)
    src.play = SimpleNamespace(client=lambda a: SimpleNamespace(details=lambda p: SimpleNamespace(
        name="Example", version_name="3.1", paid=True, owned=False)))
    result = src.search_all("https://f-droid.org/app/" + PKG, accounts=[account])
    play = next(o for o in result.options if o.source == "play")
    assert play.account == 7 and "не куплено" in play.reason


def test_search_deadline_preserves_results(monkeypatch):
    src = setup_search(monkeypatch)
    now = [0.0]
    src.clock = lambda: now[0]

    def slow_search(*args):
        now[0] = 100.0
        return []

    monkeypatch.setattr(rustore, "search", slow_search)
    result = src.search_all("Example")
    assert any(o.source == "fdroid_archive" for o in result.options)
    assert any("слишком долго" in e for e in result.errors)


def test_timeout_keeps_the_domain_for_network_exceptions(monkeypatch):
    src = setup_search(monkeypatch)
    monkeypatch.setattr(apkpure, "info", lambda *a: (_ for _ in ()).throw(
        net.FetchError("api.pureapk.com: TimeoutError: timed out")))
    result = src.search_all("Example")
    assert any("api.pureapk.com" in e and "вовремя" in e for e in result.errors)


def test_single_apk_does_not_query_github(monkeypatch):
    src = setup_search(monkeypatch)
    monkeypatch.setattr(apkpure, "info", lambda *a: SimpleNamespace(name="Example", version_name="3.0", kind="apk"))

    def forbidden(*args, **kwargs):
        raise AssertionError("single APK must not trigger GitHub lookups")

    monkeypatch.setattr(github, "info", forbidden)
    src.search_all("Example")


def test_status_and_partial_results_arrive_before_later_source_calls(monkeypatch):
    src = setup_search(monkeypatch)
    called = []
    monkeypatch.setattr(rustore, "search", lambda *a: called.append("rustore") or [])
    updates = src.search_events("Example")
    first = next(updates)
    assert (first.kind, first.source, first.state) == ("status", "rustore", "running") and not called
    assert next(updates).state == "done" and called == ["rustore"]
    assert next(updates).kind == "results"


def test_github_release_is_cached_between_searches(monkeypatch):
    src = setup_search(monkeypatch)
    now, calls = [0.0], []
    src.clock = lambda: now[0]
    release = github.GithubApp("example/android", "Example", "2", "https://github.com/example/android/releases/download/2/app.apk", "app.apk", "mark")
    monkeypatch.setattr(github, "info", lambda *a, **kw: calls.append(a[1]) or release)
    assert src.github_app("example/android") == release
    assert src.github_app("example/android") == release and calls == ["example/android"]
    now[0] = 901
    src.github_app("example/android")
    assert len(calls) == 2


def test_known_split_still_checks_author_when_apkpure_is_unavailable(monkeypatch):
    src = setup_search(monkeypatch)
    monkeypatch.setattr(apkpure, "info", lambda *a: (_ for _ in ()).throw(net.FetchError("api.pureapk.com: timed out")))
    result = src.search_all(PKG, split_packages=(PKG,))
    assert any(o.source == "github" and o.related_package == PKG for o in result.options)
