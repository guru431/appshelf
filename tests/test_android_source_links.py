from types import SimpleNamespace

import pytest

from android_fakes import FakeHttp
from appshelf.android import db, fdroid_index, fetch, links, net, sources
from helpers import make_account, sign_in

PKG = "org.example.app"
FILE = "https://4pda.to/forum/dl/post/123/example.apk"
TOPIC = "https://4pda.to/forum/index.php?showtopic=456&view=findpost&p=789"


@pytest.mark.parametrize("source, ref, saved, expected", [
    ("github", "author/app", "", "https://github.com/author/app"),
    ("fourpda", FILE, TOPIC + "&dlsess=discard", TOPIC),
    ("fourpda", FILE, "", ""),
    ("fourpda", FILE, "javascript:alert(1)", ""),
    ("fdroid_archive", PKG + "@42", "", f"https://f-droid.org/packages/{PKG}/"),
    ("izzy", PKG + "@42", "", f"https://apt.izzysoft.de/fdroid/index/apk/{PKG}"),
    ("play", PKG, "", f"https://play.google.com/store/apps/details?id={PKG}"),
    ("appgallery", PKG, "https://appgallery.huawei.com/app/C123456", "https://appgallery.huawei.com/app/C123456"),
    ("appgallery", PKG, "https://evil.example/C123", ""),
    ("upload", "", "", ""),
])
def test_source_pages_are_canonical_and_do_not_expose_session(source, ref, saved, expected):
    assert links.page_url(source, ref, saved) == expected


def test_direct_source_links_need_no_file_transfer_or_personal_session(monkeypatch):
    http = FakeHttp()
    src = sources.Sources(http, sleep=lambda _: None, pause=0)
    asset = "https://github.com/author/app/releases/download/v2/app.apk"
    monkeypatch.setattr(src, "release_app", lambda source, ref: SimpleNamespace(asset_url=asset))
    assert src.download_url("github", "author/app") == asset
    assert src.download_url("fourpda", FILE) == FILE
    assert src.download_url("link", "https://example.org/app.apk") == "https://example.org/app.apk"
    with pytest.raises(net.SourceError, match="нет прямой ссылки"):
        src.download_url("play", PKG)
    assert http.calls == []


@pytest.mark.parametrize("source", ["fdroid", "fdroid_archive", "izzy"])
def test_direct_fdroid_link_respects_pinned_version_and_arm_build(monkeypatch, source):
    src = sources.Sources(FakeHttp(), sleep=lambda _: None, pause=0)
    catalog = src._fdroid_index(source)
    app = fdroid_index.App(PKG, "Example", "", 43, (
        fdroid_index.Build("1", 42, "old arm.apk", "a" * 64, "b" * 64, ("arm64-v8a",)),
        fdroid_index.Build("2", 43, "new.apk", "c" * 64, "b" * 64, ()),
    ))
    monkeypatch.setattr(catalog, "apps", lambda wait: {PKG: app})
    assert src.download_url(source, PKG + "@42") == catalog.repo + "old%20arm.apk"


def test_direct_fdroid_link_falls_back_to_archive(monkeypatch):
    src = sources.Sources(FakeHttp(), sleep=lambda _: None, pause=0)
    monkeypatch.setattr(src.fdroid_catalog, "apps", lambda wait: {})
    build = fdroid_index.Build("1", 42, "old.apk", "a" * 64, "b" * 64, ())
    app = fdroid_index.App(PKG, "Example", "", 42, (build,))
    monkeypatch.setattr(src.fdroid_archive, "apps", lambda wait: {PKG: app})
    assert src.download_url("fdroid", PKG) == "https://f-droid.org/archive/old.apk"


@pytest.mark.parametrize("public", [False, True])
def test_source_redirect_checks_shelf_and_leaves_download_to_browser(web, conn, clock, public):
    shelf = db.ensure_public_shelf(conn, clock.iso()) if public else db.ensure_shelf(conn, 1, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Example", "fourpda", FILE, clock.iso())
    src = sources.Sources(FakeHttp(), sleep=lambda _: None, pause=0)
    web.app.state.w.env.sources = src
    path = ("/android/public" if public else "/android") + f"/apps/{PKG}/source/download"
    result = web.client.get(path, follow_redirects=False)
    assert result.status_code == 302 and result.headers["location"] == FILE
    assert result.headers["cache-control"] == "private, no-store"
    assert result.headers["referrer-policy"] == "no-referrer"
    assert src.http.calls == []
    conn.execute("DELETE FROM android_apps WHERE shelf_id=?", (shelf.id,))
    assert web.client.get(path).status_code == 404


def test_source_route_does_not_read_another_users_shelf(web, conn, clock):
    other = make_account(conn, clock, email="other@example.org", role="member", legacy=False)
    shelf = db.ensure_shelf(conn, other.user_id, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Private", "link", "https://example.org/private.apk", clock.iso())
    assert web.client.get(f"/android/apps/{PKG}/source/download").status_code == 404


def test_source_link_keeps_person_from_original_tab(web, conn, cfg, clock):
    import html
    import re
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Example", "link", "https://example.org/first.apk", clock.iso())
    page = web.client.get("/android").text
    path = html.unescape(re.search(r'href="([^"]+/source/download[^"]*)"', page)[1])
    other = make_account(conn, clock, email="other@example.org", role="member", legacy=False)
    sign_in(web.client, cfg, conn, clock, other.user_id)
    web.client.get(f"/who/{other.user_id}")
    web.app.state.w.env.sources = sources.Sources(FakeHttp(), pause=0)
    result = web.client.get(path, follow_redirects=False)
    assert result.status_code == 302 and result.headers["location"] == "https://example.org/first.apk"


@pytest.mark.parametrize("target", ["javascript:alert(1)", "https://user:password@example.org/app.apk", "https://127.0.0.1/app.apk"])
def test_source_route_rejects_unsafe_redirect(web, conn, clock, monkeypatch, target):
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Example", "github", "author/app", clock.iso())
    monkeypatch.setattr(web.sources, "download_url", lambda *args: target, raising=False)
    result = web.client.get(f"/android/apps/{PKG}/source/download", follow_redirects=False)
    assert result.status_code == 502 and "location" not in result.headers


def test_source_change_failure_restores_topic_link(web, conn, cfg, clock):
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Example", "fourpda", FILE, clock.iso())
    db.set_source(conn, shelf.id, PKG, "fourpda", FILE, TOPIC)
    db.add_version(conn, fetch.shelf_root(cfg, shelf), shelf.id, PKG,
                   db.NewApk("1", 1, 28, 35, "", "a" * 64, "fourpda", 10, "b" * 64, PKG + "-1"), clock.iso())
    web.client.post(f"/android/apps/{PKG}/source", data={"source": "rustore"})
    web.sources.fail[PKG] = net.SourceError("нет в магазине")
    fetch.run_job(web.app.state.w.env, conn, db.take_job(conn))
    app = db.get_app(conn, shelf.id, PKG)
    assert (app["source"], app["source_ref"], app["source_page"]) == ("fourpda", FILE, TOPIC)
