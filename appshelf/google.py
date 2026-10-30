"""Вход через Google (spec 2026-10-28 §5): OpenID Connect, Authorization Code + PKCE, state и nonce — stdlib, без новых
зависимостей. Сервер получает только личность (sub, адрес, имя); пароль Google вводится на странице Google.

Поток (state, nonce, verifier PKCE, куда вернуть, кто добавляет) — в cookie браузера на FLOW_TTL, подписанной ключом
cookie: ответ Google в чужом браузере, старый или с подменённым state не пройдёт. id_token приходит прямо с token
endpoint Google по TLS — подпись можно не проверять (OIDC Core 3.1.3.7), поля — проверяются. Секрет клиента — только в
запросе к token endpoint: не в адресе, cookie, журнале, тексте ошибок."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

from .android.net import FetchError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
FLOW_TTL = 600
COOKIE = "appshelf_google"


class GoogleError(Exception):
    """Текст — для журнала (без токенов и секрета), не для страницы."""


@dataclass(frozen=True)
class Flow:
    state: str
    nonce: str
    verifier: str          # code_verifier PKCE
    next: str              # куда вернуть после входа
    add_uid: int | None    # «Добавить Google» из панели этого участника; None — обычный вход
    expires: int           # unix


@dataclass(frozen=True)
class Claims:
    sub: str
    email: str
    name: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_flow(next_: str, add_uid: int | None, now_epoch: int) -> Flow:
    return Flow(secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32), next_, add_uid,
                now_epoch + FLOW_TTL)


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, f"google\0{payload}".encode(), hashlib.sha256).hexdigest()


def flow_cookie(key: bytes, flow: Flow) -> str:
    """<base64url JSON>.<HMAC-SHA256>: подделать поток или продлить его без cookie-key нельзя."""
    payload = _b64(json.dumps(asdict(flow), separators=(",", ":")).encode())
    return f"{payload}.{_sign(key, payload)}"


def read_flow(key: bytes, value: str, now_epoch: int) -> Flow | None:
    payload, _, sig = (value or "").partition(".")
    if not payload or not hmac.compare_digest(sig.encode(), _sign(key, payload).encode()):
        return None
    try:
        flow = Flow(**json.loads(_unb64(payload)))
    except (ValueError, TypeError):
        return None
    return flow if now_epoch < flow.expires else None


def auth_url(client_id: str, redirect_uri: str, flow: Flow) -> str:
    challenge = _b64(hashlib.sha256(flow.verifier.encode()).digest())
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": "openid email profile", "prompt": "select_account", "state": flow.state, "nonce": flow.nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"})


def exchange(http, client_id: str, client_secret: str, redirect_uri: str, code: str, verifier: str) -> str:
    """Код — на id_token (android.net.Http.form: только https и глобальные адреса). Ошибка сети или ответа —
    GoogleError с кодом ошибки Google, без кода, токенов и секрета."""
    try:
        r = http.form(TOKEN_URL, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                  "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier},
                        follow=False)
    except FetchError as e:
        raise GoogleError(f"token endpoint: {e}") from None
    if not isinstance(r, dict):
        raise GoogleError("token endpoint ответил не объектом")
    if "error" in r:
        raise GoogleError(f"token endpoint: {str(r['error'])[:100]} {str(r.get('error_description', ''))[:200]}")
    if not isinstance(r.get("id_token"), str):
        raise GoogleError("token endpoint не прислал id_token")
    return r["id_token"]


def claims(id_token: str, client_id: str, nonce: str, now_epoch: int) -> Claims:
    """Поля id_token (OIDC Core 3.1.3.7): издатель Google, аудитория — наш клиент, не истёк, nonce нашего потока,
    адрес подтверждён Google. Иначе — GoogleError."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise GoogleError("id_token — не JWT")
    try:
        p = json.loads(_unb64(parts[1]))
    except ValueError:  # и binascii.Error, и UnicodeDecodeError
        raise GoogleError("id_token не разбирается") from None
    if not isinstance(p, dict):
        raise GoogleError("id_token не разбирается")
    exp = p.get("exp")
    checks = [
        (p.get("iss") in ISSUERS, "чужой издатель"),
        (p.get("aud") == client_id, "чужая аудитория"),
        (isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > now_epoch, "истёк"),
        (isinstance(p.get("nonce"), str) and hmac.compare_digest(p["nonce"].encode(), nonce.encode()), "не тот nonce"),
        (p.get("email_verified") is True, "адрес не подтверждён Google"),
        (isinstance(p.get("sub"), str) and 0 < len(p["sub"]) <= 255, "нет sub"),
        (isinstance(p.get("email"), str) and 0 < len(p["email"]) <= 320, "нет адреса"),
    ]
    for ok, why in checks:
        if not ok:
            raise GoogleError(f"id_token: {why}")
    name = p.get("name")
    return Claims(p["sub"], p["email"], name.strip()[:200] if isinstance(name, str) else "")
