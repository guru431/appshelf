"""Сессия панели appshelf (spec 2026-10-08 §4): cookie на год указывает на человека, вход — Apple ID
(web/signin.py). Проверяет само приложение: к сокету /run/appshelf/web.sock ходят все процессы www-data
(php-fpm), и RCE в любом PHP-сайте иначе открывало бы страницы без входа.

Cookie — `<user_id>:<epoch>:<срок>:<HMAC>`. Ключ подписи — cookie-key (0600), appshelf-web читает его при старте:
удалить файл и перезапустить appshelf-web — выйти на всех устройствах всем; epoch человека (+1 — «выйти везде») —
только ему."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

COOKIE = "appshelf_auth"
COOKIE_AGE = 365 * 86400
KEY_LEN = 32


def load_key(path: Path) -> bytes:
    """Ключ подписи cookie (32 байта, 0600); нет файла — создаётся. Файл другой длины — ValueError: с пустым
    ключом cookie владельца посчитал бы кто угодно, лучше служба не стартует."""
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = _create_key(path)
    if len(key) != KEY_LEN:
        raise ValueError(f"{path}: {len(key)} байт вместо {KEY_LEN} — удалите файл и перезапустите appshelf-web")
    return key


def _create_key(path: Path) -> bytes:
    """Ключ пишется во временный файл и появляется под своим именем уже целым (os.link): сбой записи или второй
    процесс, создающий ключ в ту же секунду, не оставят пустой файл."""
    key = os.urandom(KEY_LEN)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:  # другой процесс успел первым — его ключ уже целиком на диске
            key = path.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    return key


def _mac(key: bytes, uid: int, epoch: int, expires: int) -> str:
    return hmac.new(key, f"{uid}\0{epoch}\0{expires}".encode(), hashlib.sha256).hexdigest()


def make_cookie(key: bytes, user, now: int) -> str:
    expires = now + COOKIE_AGE
    return f"{user.id}:{user.epoch}:{expires}:{_mac(key, user.id, user.epoch, expires)}"


def check_cookie(key: bytes, value: str, now: int, get_user):
    """Человек по cookie или None: подделка, срок, «выйти везде» (epoch), человека удалили.
    get_user(uid) → people.User | None."""
    try:
        uid, epoch, expires, mac = value.split(":")
        uid_i, epoch_i, expires_i = int(uid), int(epoch), int(expires)
    except ValueError:  # пусто, мусор или cookie версии 1 («admin:<срок>:<подпись>»)
        return None
    good = _mac(key, uid_i, epoch_i, expires_i)
    if expires_i < now or not hmac.compare_digest(mac.encode(), good.encode()):
        return None
    user = get_user(uid_i)
    return user if user is not None and user.epoch == epoch_i else None
