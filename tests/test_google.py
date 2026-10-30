import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from appshelf import google
from appshelf.android import net
from appshelf.config import from_env

NOW = 1_790_000_000
KEY = b"k" * 32
CID = "cid.apps.googleusercontent.example"
REDIRECT = "https://apps.example/google/callback"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def id_token(**claims) -> str:
    """id_token как от token endpoint: header.payload.signature; подпись не проверяется — любые байты."""
    payload = {"iss": "https://accounts.google.com", "aud": CID, "exp": NOW + 3600, "iat": NOW, "nonce": "n-1",
               "sub": "1234567890", "email": "ivan@gmail.example", "email_verified": True, "name": "Иван", **claims}
    return ".".join([b64url(b'{"alg":"RS256"}'), b64url(json.dumps(payload).encode()), b64url(b"sig")])


class FakeHttp:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def form(self, url, data, *, headers=None, follow=True):
        assert not follow, "секрет клиента не должен уходить на адрес редиректа"
        self.calls.append((url, data))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_auth_url_has_pkce_state_nonce_and_select_account():
    flow = google.new_flow("/android", None, NOW)
    u = urlsplit(google.auth_url(CID, REDIRECT, flow))
    q = parse_qs(u.query)
    assert f"{u.scheme}://{u.netloc}{u.path}" == google.AUTH_URL
    assert q["code_challenge"] == [b64url(hashlib.sha256(flow.verifier.encode()).digest())]
    assert q["code_challenge_method"] == ["S256"]
    assert q["prompt"] == ["select_account"] and q["scope"] == ["openid email profile"]
    assert (q["state"], q["nonce"], q["client_id"], q["redirect_uri"], q["response_type"]) == \
        ([flow.state], [flow.nonce], [CID], [REDIRECT], ["code"])
    assert "client_secret" not in q and flow.verifier not in u.query
    assert len({flow.state, flow.nonce, flow.verifier}) == 3 and len(flow.verifier) >= 43
    assert flow.expires == NOW + google.FLOW_TTL


def test_flow_cookie_roundtrip_expiry_and_tamper():
    flow = google.new_flow("/apple", 7, NOW)
    value = google.flow_cookie(KEY, flow)
    assert google.read_flow(KEY, value, NOW + google.FLOW_TTL - 1) == flow
    assert google.read_flow(KEY, value, NOW + google.FLOW_TTL) is None              # 10 минут вышли
    assert google.read_flow(b"x" * 32, value, NOW) is None                         # чужой ключ
    payload, _, sig = value.partition(".")
    forged = b64url(json.dumps({**json.loads(base64.urlsafe_b64decode(payload + "==")), "add_uid": 1}).encode())
    for bad in (f"{forged}.{sig}", f"{payload}.{'0' * 64}", f"{payload}.{'é' * 64}", payload, "", "garbage.x"):
        assert google.read_flow(KEY, bad, NOW) is None, bad


def test_claims_ok():
    c = google.claims(id_token(), CID, "n-1", NOW)
    assert c == google.Claims("1234567890", "ivan@gmail.example", "Иван")
    assert google.claims(id_token(iss="accounts.google.com", name=None), CID, "n-1", NOW).name == ""


def test_claims_rejected():
    for bad in [{"aud": "other"}, {"aud": [CID]}, {"iss": "https://evil.example"}, {"exp": NOW - 1}, {"exp": NOW},
                {"exp": "9999999999"}, {"nonce": "x"}, {"nonce": None}, {"email_verified": False},
                {"email_verified": "true"}, {"sub": ""}, {"sub": 5}, {"email": ""}]:
        with pytest.raises(google.GoogleError):
            google.claims(id_token(**bad), CID, "n-1", NOW)
    for garbage in ("", "a.b", "a.%%%.c", "a." + b64url(b"[1]") + ".c", "a." + b64url(b"\xff") + ".c"):
        with pytest.raises(google.GoogleError):
            google.claims(garbage, CID, "n-1", NOW)


def test_exchange_posts_form_and_returns_id_token():
    http = FakeHttp({"access_token": "at", "id_token": "the.id.token", "token_type": "Bearer"})
    assert google.exchange(http, CID, "secret", REDIRECT, "code-1", "verifier-1") == "the.id.token"
    assert http.calls == [(google.TOKEN_URL, {"grant_type": "authorization_code", "code": "code-1",
                                              "redirect_uri": REDIRECT, "client_id": CID, "client_secret": "secret",
                                              "code_verifier": "verifier-1"})]


def test_exchange_error_response():
    for answer in ({"error": "invalid_grant", "error_description": "Bad Request"}, {"access_token": "at"}, [],
                   net.FetchError("oauth2.googleapis.com ответил 500")):
        with pytest.raises(google.GoogleError) as e:
            google.exchange(FakeHttp(answer), CID, "secret", REDIRECT, "code-1", "verifier-1")
        assert "secret" not in str(e.value) and "code-1" not in str(e.value)


def test_config_google_enabled_only_with_both():
    base = {"PUB_TOKEN": "0123456789abcdef0123456789abcdef", "APPSHELF_PUBLIC_BASE": "https://apps.example"}
    assert not from_env(base).google_enabled
    assert not from_env({**base, "GOOGLE_CLIENT_ID": CID}).google_enabled
    cfg = from_env({**base, "GOOGLE_CLIENT_ID": CID, "GOOGLE_CLIENT_SECRET": "secret"})
    assert cfg.google_enabled and (cfg.google_client_id, cfg.google_client_secret) == (CID, "secret")
