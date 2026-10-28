"""Вход через Google и «Добавить Google» (spec 2026-10-28 §5)."""
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from appshelf import google, people, webauth
from appshelf.android.net import FetchError
from helpers import HOST, sign_in
from test_google import id_token

CID = "cid.apps.googleusercontent.example"


class FakeHttp:
    """Token endpoint Google: отдаёт id_token из .token (или .fail); запросы — в .calls."""

    def __init__(self):
        self.token, self.fail, self.calls = "", None, []

    def form(self, url, data, *, headers=None):
        self.calls.append((url, data))
        if self.fail is not None:
            raise self.fail
        return {"id_token": self.token, "token_type": "Bearer"}


@pytest.fixture
def gcfg(cfg):
    return replace(cfg, google_client_id=CID, google_client_secret="s3cret")


@pytest.fixture
def gw(make_web, gcfg, conn, acct):
    """Сервер с настроенным входом Google, в браузере никто не вошёл; владелец на сервере есть."""
    http = FakeHttp()
    wb = make_web(gcfg, conn, google_http=http)
    wb.http = http
    return wb


def start(client, next_="/", add=0):
    r = client.get("/google/start", params={"next": next_, "add": add}, follow_redirects=False)
    assert r.status_code == 303
    return parse_qs(urlsplit(r.headers["location"]).query)


def google_login(wb, client=None, sub="g-1", email="maria@gmail.example", name="Мария", add=0, next_="/", **claims):
    """Весь вход: /google/start → «Google» → /google/callback с кодом и state потока."""
    client = client or wb.client
    q = start(client, next_, add)
    wb.http.token = id_token(nonce=q["nonce"][0], sub=sub, email=email, name=name, aud=CID, **claims)
    return client.get("/google/callback", params={"code": "code-1", "state": q["state"][0]}, follow_redirects=False)


def signed_uids(client, cfg, conn, clock) -> list[int]:
    return [u.id for u, _ in webauth.signed_users(webauth.load_key(cfg.cookie_key),
                                                  client.cookies.get(webauth.COOKIE) or "", int(clock.t),
                                                  lambda uid: people.get_user(conn, uid))]


def invite(wb, conn, clock, label="друзья"):
    token = people.create_link(conn, people.INVITE, label, None, clock.iso())
    wb.client.get(f"/join/{token}")


def test_login_page_has_google_button_only_when_configured(gw, make_web, cfg, conn):
    r = gw.client.get("/login")
    assert 'href="/google/start?next=%2F"' in r.text and "сервер его не видит" in r.text
    assert "Войти через Google" not in make_web(cfg, conn).client.get("/login").text


def test_known_google_signs_in(gw, conn, cfg, clock):
    uid = people.create_user(conn, "Мария", people.MEMBER, None, clock.iso())
    gid = people.create_google(conn, uid, "g-1", "old@gmail.example", "", clock.iso())
    clock.advance(60)
    r = google_login(gw, next_="/android")
    assert (r.status_code, r.headers["location"]) == (303, "/android")
    assert signed_uids(gw.client, cfg, conn, clock) == [uid]
    assert r.cookies.get("appshelf_who") == str(uid) and "appshelf_acct" not in r.cookies
    g = people.get_google(conn, gid)
    assert (g.email, g.name, g.last_login_at) == ("maria@gmail.example", "Мария", clock.iso())
    assert gw.client.cookies.get(google.COOKIE) is None                     # поток завершён
    url, data = gw.http.calls[0]
    assert (url, data["client_secret"], data["redirect_uri"]) == (google.TOKEN_URL, "s3cret",
                                                                    "https://apps.example/google/callback")


def test_known_google_of_apple_person_opens_apple_shelf(gw, conn, cfg, clock):
    people.create_google(conn, 1, "g-1", "owner@gmail.example", "", clock.iso())   # владелец связал Google
    r = google_login(gw)
    assert r.status_code == 303 and r.cookies.get("appshelf_acct") == "1"
    assert gw.client.get("/history").status_code == 200


def test_unknown_without_invite_refused(gw, conn, cfg, clock):
    users = len(people.list_users(conn))
    r = google_login(gw)
    assert r.status_code == 403 and "не зарегистрирован — нужна ссылка-приглашение" in r.text
    assert len(people.list_users(conn)) == users and people.google_by_sub(conn, "g-1") is None
    assert not gw.client.cookies.get(webauth.COOKIE) and gw.sent == []


def test_invite_creates_member_and_mails_owner(gw, conn, cfg, clock):
    invite(gw, conn, clock)
    r = google_login(gw)
    assert r.status_code == 303
    g = people.google_by_sub(conn, "g-1")
    user = people.get_user(conn, g.user_id)
    assert (user.name, user.role) == ("Мария", "member") and people.invite_of(conn, user.id)["label"] == "друзья"
    assert signed_uids(gw.client, cfg, conn, clock) == [user.id]
    assert gw.client.cookies.get("appshelf_invite") is None
    [(subject, body)] = gw.sent
    assert subject == "appshelf: новый участник" and "Мария, Google maria@gmail.example" in body
    assert "приглашение «друзья»" in body


def test_add_google_from_panel_creates_separate_person(gw, conn, cfg, clock):
    sign_in(gw.client, cfg, conn, clock, 1)
    r = google_login(gw, add=1, next_="/apple")
    assert (r.status_code, r.headers["location"]) == (303, "/apple")
    g = people.google_by_sub(conn, "g-1")
    assert g.user_id != 1 and people.google_of(conn, 1) == []               # новый Google — новый участник
    assert signed_uids(gw.client, cfg, conn, clock) == [1, g.user_id]       # в браузере теперь оба
    [(_, body)] = gw.sent
    assert "добавлен из панели участника «Иван Петров» (owner@example)" in body


def test_adder_signed_out_before_callback_refused(gw, conn, cfg, clock):
    sign_in(gw.client, cfg, conn, clock, 1)
    q = start(gw.client, "/apple", 1)
    other = TestClient(gw.app, base_url=f"https://{HOST}")
    sign_in(other, cfg, conn, clock, 1)
    other.post("/logout-all/1")                                             # «Выйти на всех устройствах»
    gw.http.token = id_token(nonce=q["nonce"][0], sub="g-1", aud=CID)
    r = gw.client.get("/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert r.status_code == 403 and people.google_by_sub(conn, "g-1") is None


def test_callback_without_flow_cookie_refused(gw, conn, cfg, clock):
    invite(gw, conn, clock)
    q = start(gw.client)
    stranger = TestClient(gw.app, base_url=f"https://{HOST}")              # ответ Google в чужом браузере
    gw.http.token = id_token(nonce=q["nonce"][0], sub="g-1", aud=CID)
    r = stranger.get("/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert r.status_code == 400 and gw.http.calls == [] and not stranger.cookies.get(webauth.COOKIE)
    clock.advance(google.FLOW_TTL)                                          # своя cookie, но 10 минут вышли
    r = gw.client.get("/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert r.status_code == 400 and gw.http.calls == [] and people.google_by_sub(conn, "g-1") is None


def test_state_mismatch_refused(gw, conn, cfg, clock):
    invite(gw, conn, clock)
    q = start(gw.client)
    gw.http.token = id_token(nonce=q["nonce"][0], sub="g-1", aud=CID)
    for state in ("other", ""):
        r = gw.client.get("/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
        assert r.status_code == 400 and gw.http.calls == []
    assert people.google_by_sub(conn, "g-1") is None and not gw.client.cookies.get(webauth.COOKIE)


def test_bad_id_token_is_google_error(gw, conn, cfg, clock, capsys):
    invite(gw, conn, clock)
    r = google_login(gw, email_verified=False)
    assert r.status_code == 502 and "Google не ответил" in r.text and people.google_by_sub(conn, "g-1") is None
    gw.http.fail = FetchError("oauth2.googleapis.com ответил 500")
    r = google_login(gw)
    assert r.status_code == 502
    err = capsys.readouterr().err
    assert "адрес не подтверждён" in err and "s3cret" not in err and "code-1" not in err


def test_same_new_google_twice_one_person(gw, conn, cfg, clock, monkeypatch):
    invite(gw, conn, clock)
    q1 = start(gw.client)
    second = TestClient(gw.app, base_url=f"https://{HOST}")
    token = people.create_link(conn, people.INVITE, "ещё", None, clock.iso())
    second.get(f"/join/{token}")
    q2 = start(second)
    gw.http.token = id_token(nonce=q1["nonce"][0], sub="g-1", aud=CID)
    assert gw.client.get("/google/callback", params={"code": "c", "state": q1["state"][0]},
                         follow_redirects=False).status_code == 303
    real = people.google_by_sub
    monkeypatch.setattr(people, "google_by_sub", lambda c, sub: None)       # второй проверял до записи первого
    gw.http.token = id_token(nonce=q2["nonce"][0], sub="g-1", aud=CID)
    r = second.get("/google/callback", params={"code": "c", "state": q2["state"][0]}, follow_redirects=False)
    assert r.status_code == 403 and "уже зарегистрирован — войдите ещё раз" in r.text
    monkeypatch.setattr(people, "google_by_sub", real)
    assert conn.execute("SELECT COUNT(*) FROM google_accounts").fetchone()[0] == 1
    assert len(people.list_users(conn)) == 2                                # владелец и один новый участник
    r = google_login(gw, client=second)                                     # войти ещё раз — тот же участник
    assert r.status_code == 303 and signed_uids(second, cfg, conn, clock) == [people.google_by_sub(conn, "g-1").user_id]


def test_google_routes_404_when_not_configured(make_web, cfg, conn, acct):
    wb = make_web(cfg, conn)
    assert wb.client.get("/google/start", follow_redirects=False).status_code == 404
    assert wb.client.get("/google/callback?code=c&state=s", follow_redirects=False).status_code == 404


def test_access_denied_shows_message(gw, conn, cfg, clock):
    q = start(gw.client)
    r = gw.client.get("/google/callback", params={"error": "access_denied", "state": q["state"][0]},
                      follow_redirects=False)
    assert r.status_code == 400 and "Вход через Google отменён" in r.text and gw.http.calls == []


def test_start_next_is_own_path_only(gw):
    for bad in ("https://evil.example/", "//evil.example", "/" + "x" * 3000):
        q = start(gw.client, bad)
        flow = google.read_flow(gw.app.state.w.key, gw.client.cookies.get(google.COOKIE), gw.app.state.w.epoch())
        assert flow.next == "/" and flow.state == q["state"][0]
