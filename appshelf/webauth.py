"""Пароль страниц appshelf. Проверяет само приложение: к сокету /run/appshelf/web.sock ходят все
процессы www-data (php-fpm), и RCE в любом PHP-сайте иначе открывало бы страницы без пароля.
Файл (/var/lib/appshelf/web-auth, 0600 appshelf) — строки
`логин:pbkdf2_sha256$<итерации>$<соль b64>$<хэш b64>`, пишет `appshelf set-web-password`.
Вход — формой /login с cookie на год (PWA на iPhone не показывает окно Basic Auth) или заголовком Basic."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

SCHEME = "pbkdf2_sha256"
ITERATIONS = 600_000  # ~0.2 с; проверенный заголовок запоминается, на каждый запрос не считается
COOKIE = "appshelf_auth"
COOKIE_AGE = 365 * 86400


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str, iterations: int | None = None, salt: bytes | None = None) -> str:
    iterations = iterations or ITERATIONS
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{SCHEME}${iterations}${_b64(salt)}${_b64(dk)}"


def verify(encoded: str, password: str) -> bool:
    try:
        scheme, iterations, salt, dk = encoded.split("$")
        if scheme != SCHEME:
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except ValueError:  # испорченная строка файла: не пускать
        return False


def read_users(path: Path) -> dict[str, str]:
    users = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        user, sep, encoded = line.strip().partition(":")
        if sep:
            users[user] = encoded
    return users


def parse_basic(header: str) -> tuple[str, str] | None:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        raw = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except ValueError:
        return None
    user, sep, password = raw.partition(":")
    return (user, password) if sep else None


def check(users: dict[str, str], header: str, seen: set[bytes]) -> bool:
    """seen — отпечатки уже проверенных пар «строка файла + заголовок»: смена пароля их обнуляет."""
    creds = parse_basic(header)
    if creds is None or creds[0] not in users:
        return False
    stored = users[creds[0]]
    mark = hashlib.sha256(f"{stored}\0{header}".encode()).digest()
    if mark in seen:
        return True
    if not verify(stored, creds[1]):
        return False
    if len(seen) > 64:
        seen.clear()
    seen.add(mark)
    return True


def load_key(path: Path) -> bytes:
    """Ключ подписи cookie (32 байта, 0600); удалить файл — выйти на всех устройствах."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, "wb") as f:
        f.write(key := os.urandom(32))
    return key


def _cookie_mac(key: bytes, user: str, expires: int, stored: str) -> str:
    # строка пароля из файла — в подписи: смена пароля гасит все выданные cookie
    return hmac.new(key, f"{user}\0{expires}\0{stored}".encode(), hashlib.sha256).hexdigest()


def make_cookie(key: bytes, users: dict[str, str], user: str, now: int) -> str:
    expires = now + COOKIE_AGE
    return f"{user}:{expires}:{_cookie_mac(key, user, expires, users[user])}"


def check_cookie(key: bytes, users: dict[str, str], value: str, now: int) -> bool:
    try:
        user, expires, mac = value.rsplit(":", 2)
        expires_at = int(expires)
    except ValueError:
        return False
    if user not in users or expires_at < now:
        return False
    return hmac.compare_digest(mac, _cookie_mac(key, user, expires_at, users[user]))


def set_password(path: Path, user: str, password: str, iterations: int | None = None) -> None:
    users = read_users(path) if path.exists() else {}
    users[user] = hash_password(password, iterations)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)  # остаток прерванного запуска мог быть с другим режимом
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(f"{u}:{e}\n" for u, e in users.items()))
    os.replace(tmp, path)
