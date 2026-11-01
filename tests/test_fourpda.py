import json
import os
from types import SimpleNamespace

import pytest

from android_fakes import FakeHttp
from appshelf import jobs, people
from appshelf.android import db, fetch, fourpda
from appshelf.android.sources import Sources
from appshelf.android.net import SourceError
from apk_factory import make_apk
from helpers import make_account, sign_in
from appshelf.fourpda_login import BrowserLogin, Pending, TTL

FILE = fourpda.BASE + "/forum/dl/post/123/example.apk"
TOPIC = fourpda.FORUM + "?showtopic=456"
GATE = fourpda.FORUM + "?act=attach&id=123&dlsess=temporary-secret"
COOKIE = "private-session-secret"
AUTH = '<a href="/forum/index.php?showuser=88">Личный аккаунт</a><a href="?act=auth&amp;action=logout">Выход</a>'


def response(text, status=200):
    return status, {"content-type": "text/html; charset=windows-1251"}, text.encode("cp1251")


def session():
    return fourpda.Session("Личный аккаунт", "Mozilla/5.0", {"member_id": "88", "pass_hash": COOKIE})


def raw_session():
    s = session()
    return json.dumps({"user_agent": s.user_agent, "cookies": s.cookies}).encode()


@pytest.fixture
def sessions(cfg):
    http = FakeHttp(raw={fourpda.FORUM + "?act=idx": response(AUTH)})
    return fourpda.Sessions(cfg, http)


@pytest.fixture
def connected(make_web, cfg, conn, acct, clock, sessions):
    sessions.save(1, session())
    browser = SimpleNamespace(call=lambda *a, **kw: None, close=lambda: None)
    web = make_web(cfg, conn, fourpda_sessions=sessions, fourpda_browser=browser)
    sign_in(web.client, cfg, conn, clock)
    web.sessions, web.browser = sessions, browser
    return web


def test_session_is_private_atomic_and_never_in_repr(cfg, sessions, monkeypatch):
    sessions.save(1, session())
    sessions.save(2, fourpda.Session("Другой", "Browser", {"member_id": "99", "pass_hash": "other"}))
    assert sessions.load(1).cookies["pass_hash"] == COOKIE
    assert sessions.load(2).cookies["pass_hash"] == "other"
    assert COOKIE not in repr(session())
    if os.name != "nt":
        assert sessions.path(1).stat().st_mode & 0o777 == 0o600
        assert sessions.root.stat().st_mode & 0o777 == 0o700
    original = sessions.path(1).read_bytes()
    monkeypatch.setattr(fourpda.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        sessions.save(1, fourpda.Session("Replacement", "Browser", {"member_id": "99", "pass_hash": "new"}))
    assert sessions.path(1).read_bytes() == original
    assert not list(sessions.root.glob(".*"))


@pytest.mark.parametrize("change", [
    {"user_agent": "Browser\r\nCookie: bad"}, {"cookies": {"member_id": "1", "pass_hash": "bad; injected=yes"}},
    {"cookies": {"member_id": 1, "pass_hash": "x"}}, {"cookies": {"member_id": "1"}},
])
def test_session_rejects_header_injection_and_bad_credentials(change):
    data = json.loads(raw_session())
    data.update(change)
    with pytest.raises(SourceError):
        fourpda.Sessions.parse(json.dumps(data).encode())


@pytest.mark.parametrize("url", ["https://localhost/forum/index.php?showtopic=1",
    "https://4pda.to@localhost/forum/index.php?showtopic=1", "https://4pda.to:443/forum/index.php?showtopic=1",
    "http://4pda.to/forum/index.php?showtopic=1", "https://4pda.to/forum/index.php?act=auth"])
def test_topic_rejects_other_origins_and_operations(url):
    with pytest.raises(SourceError):
        fourpda.topic_url(url)


def test_search_and_attachments_parse_forum_without_trusting_external_links(sessions):
    query_url = fourpda.FORUM + "?act=search&query=%D2%E5%F1%F2&source=top&forums=all&subforums=1&result=topics&noform=1"
    sessions.http.raw[query_url] = response(AUTH + f'<a href="{TOPIC}">Тест Android</a><a href="{TOPIC}">duplicate</a>'
                                         '<a href="https://evil.example/forum/index.php?showtopic=9">other</a>')
    sessions.http.raw[TOPIC] = response('<title>Тест - 4PDA</title>' + AUTH + f'<a href="{FILE}">Версия 1.apk</a>'
                                      f'<a href="{FILE}">duplicate</a><a href="https://evil.example/app.apk">bad</a>'
                                      '<a href="/forum/dl/post/8/bundle.apks">APKS</a>')
    client = fourpda.Client(sessions.http, session())
    assert client.search("Тест") == [fourpda.Link("Тест Android", TOPIC)]
    assert client.files(TOPIC) == ("Тест", [fourpda.Link("Версия 1.apk", FILE)])
    assert fourpda.topic_url(TOPIC + "&view=findpost&p=789&secret=x") == TOPIC + "&view=findpost&p=789"


def test_download_resolves_fresh_session_link_and_validates_file(sessions, tmp_path):
    sessions.http.raw[FILE] = response(f'<a href="{GATE.replace("&", "&amp;")}">Скачать</a>')
    sessions.http.files[GATE] = b"PK\x03\x04APK"
    dest = tmp_path / "app.apk"
    client = fourpda.Client(sessions.http, session())
    client.download(FILE, dest)
    assert dest.read_bytes() == b"PK\x03\x04APK"
    sessions.http.files[GATE] = b"<html>login</html>"
    with pytest.raises(fourpda.AuthError):
        client.download(FILE, dest)
    assert not dest.exists()
    sessions.http.raw[FILE] = response('<a href="https://evil.example/?act=attach&id=1&dlsess=x">bad</a>')
    with pytest.raises(fourpda.AuthError):
        client.download(FILE, dest)


def test_expired_and_challenged_sessions_are_explicit(sessions):
    client = fourpda.Client(sessions.http, session())
    sessions.http.raw[TOPIC] = response("guest")
    with pytest.raises(fourpda.AuthError, match="истёк"):
        client.files(TOPIC)
    sessions.http.raw[TOPIC] = response("Enable JavaScript and cookies to continue", 403)
    with pytest.raises(fourpda.AuthError, match="проверку браузера"):
        client.files(TOPIC)


def test_import_checks_identity_and_does_not_expose_secrets(connected):
    client = connected.client
    result = client.post("/fourpda/1/import", files={"file": ("session.json", raw_session(), "application/json")},
                         follow_redirects=False)
    assert result.status_code == 303
    page = client.get("/fourpda/1/connect")
    assert "Личный аккаунт" in page.text and COOKIE not in page.text
    assert page.headers["cache-control"] == "no-store"
    connected.sessions.http.raw[fourpda.FORUM + "?act=idx"] = response("expired")
    result = client.post("/fourpda/1/import", files={"file": ("x.json", raw_session())})
    assert result.status_code == 400 and COOKIE not in result.text
    assert connected.sessions.load(1).name == "Личный аккаунт"
    assert client.post("/fourpda/1/import", files={"file": ("huge.json", b"x" * (fourpda.MAX_SESSION + 1))}).status_code == 400


def test_session_endpoints_are_bound_to_signed_in_person(connected, conn, clock):
    uid = people.create_user(conn, "Other", people.MEMBER, None, clock.iso())
    connected.sessions.save(uid, session())
    for path in (f"/fourpda/{uid}/connect", f"/fourpda/{uid}/screen"):
        assert connected.client.get(path).status_code == 404
    for path in (f"/fourpda/{uid}/login", f"/fourpda/{uid}/disconnect", f"/fourpda/{uid}/cancel", f"/fourpda/{uid}/finish"):
        assert connected.client.post(path).status_code == 404
    assert connected.sessions.load(uid) is not None
    result = connected.client.post("/fourpda/1/disconnect", headers={"Origin": "https://evil.example"})
    assert result.status_code == 403 and connected.sessions.load(1) is not None


def test_selected_apk_uses_shelf_owner_and_existing_pipeline(connected, conn, cfg, tmp_path):
    web = connected
    apk = make_apk(tmp_path / "input.apk", package="ru.example.app")
    web.sessions.http.raw[FILE] = response(f'<a href="{GATE.replace("&", "&amp;")}">Download</a>')
    web.sessions.http.files[GATE] = apk.read_bytes()
    web.app.state.w.env.sources = Sources(web.sessions.http, fourpda=web.sessions, sleep=lambda _: None, pause=0)
    result = web.client.post("/android/4pda/add", data={"url": FILE, "name": "Example", "user_id": "999"}, follow_redirects=False)
    assert result.status_code == 303
    job = db.take_job(conn)
    assert COOKIE not in job["payload"] and "dlsess" not in job["payload"]
    fetch.run_job(web.app.state.w.env, conn, job)
    shelf = db.shelf_for_user(conn, 1)
    app = db.get_app(conn, shelf.id, "ru.example.app")
    assert app["status"] == "ok" and app["source"] == "fourpda" and app["source_ref"] == FILE
    assert db.current_version(conn, shelf.id, "ru.example.app") is not None
    assert COOKIE not in web.client.get("/android").text
    assert web.client.get("/android/public/4pda").status_code == 404
    with pytest.raises(SourceError, match="личной полке"):
        web.app.state.w.env.sources.fetch("fourpda", FILE, tmp_path / "public.apk", user_id=None)
    # Следующее выбранное вложение того же пакета обновляет и файл, и закреплённую ссылку.
    newer = FILE.replace("/123/", "/124/")
    newer_gate = GATE.replace("id=123", "id=124")
    web.sessions.http.raw[newer] = response(f'<a href="{newer_gate.replace("&", "&amp;")}">Download</a>')
    web.sessions.http.files[newer_gate] = make_apk(tmp_path / "new.apk", version_code=2).read_bytes()
    web.client.post("/android/4pda/add", data={"url": newer})
    fetch.run_job(web.app.state.w.env, conn, db.take_job(conn))
    assert db.get_app(conn, shelf.id, "ru.example.app")["source_ref"] == newer
    assert db.current_version(conn, shelf.id, "ru.example.app")["version_code"] == 2


def test_disconnect_preserves_shelf_and_deletes_session(connected, conn, clock):
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    result = connected.client.post("/fourpda/1/disconnect", follow_redirects=False)
    assert result.status_code == 303 and connected.sessions.load(1) is None
    assert db.get_shelf(conn, shelf.id) is not None


def test_invalid_topic_never_becomes_an_executable_link(connected):
    result = connected.client.get("/android/4pda/topic", params={"url": "javascript:alert(document.domain)"})
    assert result.status_code == 400 and 'href="javascript:' not in result.text


def test_encoded_file_path_cannot_escape_attachment():
    with pytest.raises(SourceError):
        fourpda.file_url(fourpda.BASE + "/forum/dl/post/123/a%2F..%2Fapp.apk")


def test_russian_attachment_name_is_encoded_for_http():
    url = fourpda.file_url(fourpda.BASE + "/forum/dl/post/123/Пример программы.apk")
    assert url.isascii() and "%20" in url
    assert fourpda.file_url(url) == url


def test_user_removal_cleans_session_even_without_android_shelf(cfg, conn, clock, sessions):
    account = make_account(conn, clock, email="member@example", role=people.MEMBER, legacy=False)
    sessions.save(account.user_id, session())
    jobs.remove_user(cfg, conn, account.user_id)
    assert not sessions.path(account.user_id).exists()


def test_removing_last_google_identity_cleans_fourpda(cfg, conn, clock, sessions):
    uid = people.create_user(conn, "Google member", people.MEMBER, None, clock.iso())
    gid = people.create_google(conn, uid, "external-sub", "member@example", "Member", clock.iso())
    sessions.save(uid, session())
    assert jobs.remove_google(cfg, conn, gid)
    assert not sessions.path(uid).exists()


def test_link_moves_session_and_refuses_to_drop_either_account(cfg, conn, clock, acct, sessions):
    uid = people.create_user(conn, "Other", people.MEMBER, None, clock.iso())
    sessions.save(uid, session())
    sessions.save(acct.user_id, session())
    with pytest.raises(jobs.LinkRefused, match="4PDA"):
        jobs.link_users(cfg, conn, acct.user_id, uid)
    assert sessions.path(uid).exists() and people.get_user(conn, uid)
    sessions.forget(acct.user_id)
    jobs.link_users(cfg, conn, acct.user_id, uid)
    assert sessions.load(acct.user_id).name == "Личный аккаунт"
    assert not sessions.path(uid).exists()


def test_login_window_endpoints_require_person_and_window_ticket(connected):
    seen = []

    def call(uid, token, action, **kw):
        seen.append((uid, token, action))
        if action == "start":
            return "window-ticket"
        if token != "window-ticket":
            raise SourceError("Окно входа устарело")
        if action == "frame":
            return b"jpeg-image"
        if action == "finish":
            return session()

    connected.browser.call = call
    client = connected.client
    page = client.post("/fourpda/1/login")
    assert page.status_code == 200 and "window-ticket" in page.text
    assert COOKIE not in page.text and page.headers["cache-control"] == "no-store"
    assert client.get("/fourpda/1/screen").status_code == 409
    frame = client.get("/fourpda/1/screen", headers={"X-4PDA-Window": "window-ticket"})
    assert frame.content == b"jpeg-image" and frame.headers["cache-control"] == "no-store"
    assert client.post("/fourpda/1/finish", data={"token": "wrong"}).status_code == 400
    assert client.post("/fourpda/1/finish", data={"token": "window-ticket"}, follow_redirects=False).status_code == 303
    assert (1, "window-ticket", "cancel") in seen


def test_browser_context_isolation_expiry_and_input_limits(cfg, clock):
    closed = []
    page = SimpleNamespace(screenshot=lambda **kw: b"frame")
    pool = BrowserLogin(cfg, clock=clock.monotonic)
    pool.pending = {1: Pending(SimpleNamespace(close=lambda: closed.append(1)), page, "one", clock.t + TTL),
                    2: Pending(SimpleNamespace(close=lambda: closed.append(2)), page, "two", clock.t + TTL)}
    try:
        with pytest.raises(SourceError, match="устарело"):
            pool.call(1, "two", "frame")
        assert pool.call(1, "one", "frame") == b"frame"
        with pytest.raises(SourceError):
            pool.call(1, "one", "text", text="x" * 257)
        with pytest.raises(SourceError):
            pool.call(1, "one", "key", key="Control+L")
        clock.advance(TTL)
        with pytest.raises(SourceError, match="устарело"):
            pool.call(1, "one", "frame")
        assert sorted(closed) == [1, 2]
    finally:
        pool.close()


@pytest.mark.parametrize("url,allowed", [("https://4pda.to/forum/", True),
    ("https://s.4pda.to/auth.js", True), ("https://challenges.cloudflare.com/widget", True),
    ("http://4pda.to/", False), ("https://4pda.to:443@127.0.0.1/", False),
    ("https://4pda.to.evil.example/", False), ("https://localhost/", False)])
def test_login_browser_only_reaches_forum_and_challenge(url, allowed):
    called = []
    route = SimpleNamespace(request=SimpleNamespace(url=url, resource_type="document"),
                            abort=lambda: called.append(False), continue_=lambda: called.append(True))
    BrowserLogin._route(route)
    assert called == [allowed]


def test_browser_does_not_inherit_service_secrets(cfg, monkeypatch):
    import sys
    from types import ModuleType
    from appshelf import fourpda_login
    seen = {}

    def launch(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(close=lambda: None)

    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=lambda: None)
    package, api = ModuleType("playwright"), ModuleType("playwright.sync_api")
    package.__path__ = []
    api.sync_playwright = lambda: SimpleNamespace(start=lambda: runtime)
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("PUB_TOKEN", "private-pub-token")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "private-client-secret")
    monkeypatch.setenv("HOME", "/readonly-code-directory")
    monkeypatch.setattr(fourpda_login.sys, "platform", "linux")
    pool = BrowserLogin(cfg)
    try:
        pool._launch()
        assert seen["chromium_sandbox"] is True
        assert "PUB_TOKEN" not in seen["env"] and "GOOGLE_CLIENT_SECRET" not in seen["env"]
        assert seen["env"]["DISPLAY"] == ":99"
        home = pool.home.name
        from pathlib import Path
        assert Path(home).parent == cfg.tmp_dir
        assert seen["env"]["HOME"] == home and Path(home).is_dir()
        assert seen["env"]["XDG_CONFIG_HOME"] == os.path.join(home, "config")
        assert os.environ["HOME"] == "/readonly-code-directory"
    finally:
        pool.close()
    assert not Path(home).exists()
