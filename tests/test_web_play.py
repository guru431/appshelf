"""Подключение Google-аккаунта к Google Play и добавление из Play в панели (spec 2026-10-28 §6.4, §10)."""
import json
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from apk_factory import make_apk
from appshelf import people
from appshelf.android import db, fetch, play, sources
from helpers import HOST, sign_in
from test_web_accounts_google import google_only

OAUTH = "oauth2_4/0AVGzR1-very-secret-token"


@pytest.fixture
def pweb(make_web, cfg, conn, clock, acct):
    """Владелец вошёл; у него Google-аккаунт (не подключён к Play); в настройках прокси de."""
    wb = make_web(replace(cfg, proxies={"de": "socks5h://u:p@de.example:1080"}), conn)
    wb.cfg = wb.app.state.w.cfg
    wb.gid = people.create_google(conn, 1, "g-own", "owner@gmail.example", "Иван", clock.iso())
    sign_in(wb.client, cfg, conn, clock, 1)
    return wb


@pytest.fixture
def connects(monkeypatch):
    """play.connect без сети: вызовы — в .calls, .error — исключение; успех кладёт мастер-токен в каталог."""
    def fake(cfg, gid, email, token, proxy, **kw):
        fake.calls.append((gid, email, token, proxy))
        if fake.error is not None:
            raise fake.error
        d = play.token_dir(cfg, gid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "aas").write_text("aas_et/x")
        return ""
    fake.calls, fake.error = [], None
    monkeypatch.setattr(play, "connect", fake)
    return fake


def test_connect_form_shows_warning_and_no_token_echo(pweb, connects, conn):
    page = pweb.client.get(f"/google/{pweb.gid}/play")
    assert page.status_code == 200
    for text in ("ключ с доступом к Google-аккаунту", "владелец сервера технически может им воспользоваться",
                 "может заблокировать аккаунт", "accounts.google.com/EmbeddedSetup", "oauth_token", "10 минут"):
        assert text in page.text, text
    assert 'name="oauth_token" autocomplete="off"' in page.text
    connects.error = play.PlayError("Google не принял oauth_token — получите новый")
    r = pweb.client.post(f"/google/{pweb.gid}/play", data={"oauth_token": OAUTH})
    assert r.status_code == 400 and "Google не принял oauth_token" in r.text and OAUTH not in r.text
    assert people.get_google(conn, pweb.gid).play == "none"
    connects.error = None
    r = pweb.client.post(f"/google/{pweb.gid}/play", data={"oauth_token": f"  {OAUTH}\n"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/apple")
    assert connects.calls[-1] == (pweb.gid, "owner@gmail.example", OAUTH, "")
    g = people.get_google(conn, pweb.gid)
    assert (g.play, g.proxy) == ("ok", "")
    page = pweb.client.get("/apple").text
    assert "Google Play подключён" in page and f"/google/{pweb.gid}/play/disconnect" in page


def test_connect_other_email_400(pweb, connects, conn, clock):
    connects.error = play.PlayError("это другой Google-аккаунт: на странице Google войдите тем же аккаунтом")
    r = pweb.client.post(f"/google/{pweb.gid}/play", data={"oauth_token": OAUTH})
    assert r.status_code == 400 and "это другой Google-аккаунт" in r.text
    assert people.get_google(conn, pweb.gid).play == "none"
    _, foreign = google_only(conn, clock)                                       # Google-аккаунт не вошедшего
    assert pweb.client.get(f"/google/{foreign}/play").status_code == 404
    assert pweb.client.post(f"/google/{foreign}/play", data={"oauth_token": OAUTH}).status_code == 404
    assert pweb.client.post(f"/google/{foreign}/play/disconnect").status_code == 404
    connects.error = None
    r = pweb.client.post(f"/google/{pweb.gid}/play", data={"oauth_token": " "})
    assert r.status_code == 400 and len(connects.calls) == 1                    # пустой — к Google не идём


def test_proxy_select_owner_only(pweb, connects, make_web, cfg, conn, clock):
    page = pweb.client.get(f"/google/{pweb.gid}/play").text
    assert '<select name="proxy"' in page and '<option value="de"' in page
    assert pweb.client.post(f"/google/{pweb.gid}/play", data={"oauth_token": OAUTH, "proxy": "us"}).status_code == 400
    r = pweb.client.post(f"/google/{pweb.gid}/play", data={"oauth_token": OAUTH, "proxy": "de"}, follow_redirects=False)
    assert r.status_code == 303 and connects.calls[-1][3] == "de" and people.get_google(conn, pweb.gid).proxy == "de"
    assert "прокси de" in pweb.client.get("/apple").text
    uid, gid = google_only(conn, clock)                                         # участник без владельца в браузере
    member = TestClient(pweb.app, base_url=f"https://{HOST}")
    sign_in(member, cfg, conn, clock, uid)
    assert '<select name="proxy"' not in member.get(f"/google/{gid}/play").text
    r = member.post(f"/google/{gid}/play", data={"oauth_token": OAUTH, "proxy": "de"})
    assert r.status_code == 403 and connects.calls[-1][0] != gid
    people.set_proxy(conn, gid, "de")                                           # выбрал владелец раньше
    r = member.post(f"/google/{gid}/play", data={"oauth_token": OAUTH}, follow_redirects=False)
    assert r.status_code == 303 and connects.calls[-1] == (gid, f"g1@gmail.example", OAUTH, "de")


def jobs_of(conn, shelf) -> list[dict]:
    return [json.loads(j["payload"]) for j in conn.execute("SELECT payload FROM android_jobs WHERE shelf_id=?",
                                                           (shelf.id,))]


def test_add_from_play_by_url_and_package(pweb, conn, clock):
    people.set_play(conn, pweb.gid, "ok", clock.iso())
    page = pweb.client.get("/android/add").text
    assert "Google Play" in page and 'action="/android/add/play"' in page and f'value="{pweb.gid}"' in page
    r = pweb.client.post("/android/add/play", follow_redirects=False, data={
        "package": "https://play.google.com/store/apps/details?id=com.fsck.k9&hl=ru", "account": pweb.gid})
    assert r.status_code == 303
    pweb.client.post("/android/add/play", data={"package": " org.videolan.vlc ", "account": pweb.gid})
    pweb.client.post("/android/add/play", data={"package": "com.fsck.k9", "account": pweb.gid})   # уже на полке
    shelf = db.shelf_for_user(conn, 1)
    app = db.get_app(conn, shelf.id, "com.fsck.k9")
    assert (app["source"], app["source_ref"], app["google_account_id"], app["status"]) == \
        ("play", "com.fsck.k9", pweb.gid, "queued")
    assert jobs_of(conn, shelf) == [{"source": "play", "ref": "com.fsck.k9", "account": pweb.gid},
                                    {"source": "play", "ref": "org.videolan.vlc", "account": pweb.gid}]
    r = pweb.client.post("/android/public/add/play", follow_redirects=False,         # публичная — аккаунтом владельца
                         data={"package": "com.fsck.k9", "account": pweb.gid})
    assert r.status_code == 303 and db.get_app(conn, db.public_shelf(conn).id, "com.fsck.k9")["source"] == "play"


def test_add_play_needs_connected_account(pweb, conn, clock, cfg):
    assert 'action="/android/add/play"' not in pweb.client.get("/android/add").text   # подключённого нет
    assert "Google-аккаунт добавлен, но к Google Play ещё не подключён" in pweb.client.get("/android/add").text
    r = pweb.client.post("/android/add/play", data={"package": "com.fsck.k9", "account": pweb.gid})
    assert r.status_code == 400 and "подключите Google-аккаунт к Google Play" in r.text
    uid, foreign = google_only(conn, clock)                                     # чужой подключённый аккаунт
    people.set_play(conn, foreign, "ok", clock.iso())
    people.set_play(conn, pweb.gid, "ok", clock.iso())
    assert pweb.client.post("/android/add/play", data={"package": "com.fsck.k9", "account": foreign}).status_code == 400
    for bad in ("https://evil.example/store/apps/details?id=com.fsck.k9", "не пакет", ""):
        r = pweb.client.post("/android/add/play", data={"package": bad, "account": pweb.gid})
        assert r.status_code == 400, bad
    assert db.shelf_for_user(conn, 1) is None or not db.list_apps(conn, db.shelf_for_user(conn, 1).id)
    member = TestClient(pweb.app, base_url=f"https://{HOST}")               # участник: публичную не меняет
    sign_in(member, cfg, conn, clock, uid)
    r = member.post("/android/public/add/play", data={"package": "com.fsck.k9", "account": foreign})
    assert r.status_code == 403


def test_disconnect_deletes_token_dir(pweb, connects, conn, clock):
    pweb.client.post(f"/google/{pweb.gid}/play", data={"oauth_token": OAUTH})
    d = play.token_dir(pweb.cfg, pweb.gid)
    assert (d / "aas").exists()
    confirmation = pweb.client.get(f"/google/{pweb.gid}/play/disconnect")
    assert confirmation.status_code == 200 and "повторного подключения" in confirmation.text
    assert f'action="/google/{pweb.gid}/play/disconnect"' in confirmation.text
    assert (d / "aas").exists() and people.get_google(conn, pweb.gid).play == "ok"
    play._TOKENS[pweb.gid] = ("ya29.x", 9e9)
    r = pweb.client.post(f"/google/{pweb.gid}/play/disconnect", follow_redirects=False)
    assert r.status_code == 303 and not d.exists() and pweb.gid not in play._TOKENS
    assert people.get_google(conn, pweb.gid).play == "none"
    assert "Подключить к Google Play" in pweb.client.get("/apple").text


def test_public_expired_banner_only_for_accounts_used_on_shelf(pweb, conn, clock):
    other = people.create_google(conn, 1, "g-unused", "unused@gmail.example", "", clock.iso())
    people.set_play(conn, pweb.gid, "ok", clock.iso())
    people.set_play(conn, other, "ok", clock.iso())
    people.set_play(conn, pweb.gid, "expired", clock.iso())
    people.set_play(conn, other, "expired", clock.iso())
    shelf = db.ensure_public_shelf(conn, clock.iso())
    db.put_app(conn, shelf.id, "com.example.one", "x", "play", "com.example.one", clock.iso(),
               google_account_id=pweb.gid)
    page = pweb.client.get("/android/public").text
    assert "Подключение Google Play owner@gmail.example истекло" in page
    assert "unused@gmail.example" not in page


def test_split_job_alternatives_download_same_package_and_check_access(pweb, conn, cfg, clock, tmp_path):
    from test_web_android import work
    package = "ru.example.app"
    people.set_play(conn, pweb.gid, "ok", clock.iso())
    pweb.sources.fail[package] = sources.SplitOnly(sources.SPLIT_TEXT, [
        {"source": "rustore", "signature": "a" * 64}, {"source": "fdroid", "signature": ""}])
    pweb.client.post("/android/add/play", data={"package": package, "account": pweb.gid})
    work(pweb, conn)
    shelf = db.shelf_for_user(conn, 1)
    jid = db.open_jobs(conn, shelf.id)[0]["id"]
    page = pweb.client.get("/android").text
    assert "Взять из RuStore" in page and "a" * 64 in page and "Подпись будет показана после скачивания" in page
    endpoint = f"/android/jobs/{jid}/alternative"
    assert pweb.client.post(endpoint, data={"source": "apkpure"}).status_code == 404
    uid, _ = google_only(conn, clock)
    foreign = TestClient(pweb.app, base_url=f"https://{HOST}")
    sign_in(foreign, cfg, conn, clock, uid)
    assert foreign.post(endpoint, data={"source": "rustore"}).status_code == 404
    assert foreign.post(f"/android/public/jobs/{jid}/alternative", data={"source": "rustore"}).status_code == 403
    del pweb.sources.fail[package]
    pweb.sources.apks[package] = make_apk(tmp_path / "alternative.apk", package=package)
    response = pweb.client.post(endpoint, data={"source": "rustore"}, follow_redirects=False)
    assert response.status_code == 303
    work(pweb, conn)
    assert db.has_current(conn, shelf.id, package) and db.get_app(conn, shelf.id, package)["source"] == "rustore"
    assert conn.execute("SELECT status FROM android_jobs WHERE id=?", (jid,)).fetchone()[0] == "dismissed"


def test_expired_banner_and_play_card(pweb, conn, clock, tmp_path):
    people.set_play(conn, pweb.gid, "ok", clock.iso())
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    pweb.free["bytes"] = 50 << 30
    db.put_app(conn, shelf.id, "com.paid.x", "Платное", "play", "com.paid.x", clock.iso(), google_account_id=pweb.gid,
               paid=True)
    page = pweb.client.get("/android").text
    assert "купленное" in page and "Play Store для проверки лицензии" in page and "истекло" not in page
    people.set_play(conn, pweb.gid, "expired", clock.iso())
    page = pweb.client.get("/android").text
    assert "Подключение Google Play owner@gmail.example истекло" in page
    accounts = pweb.client.get("/apple").text
    assert "истекло" in accounts and "Подключить заново" in accounts


def test_split_note_shown_on_card(pweb, conn, clock, tmp_path, ctx):
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    db.put_app(conn, shelf.id, "ru.example.app", "Пример", "play", "ru.example.app", clock.iso(),
               google_account_id=pweb.gid)
    ctx.env.cfg.archive.mkdir(parents=True, exist_ok=True)
    fetch.store_apk(ctx.env, conn, shelf, make_apk(tmp_path / "a.apk"), source="play", source_ref="ru.example.app",
                    expected_package="ru.example.app", expected_signer="", name_hint="", icon=None)
    db.set_status(conn, shelf.id, "ru.example.app", "ok", fetch.SPLIT_UPDATE)
    db.set_checked(conn, shelf.id, "ru.example.app", clock.iso(), "same")
    page = pweb.client.get("/android").text
    assert fetch.SPLIT_UPDATE in page and "Google Play" in page
