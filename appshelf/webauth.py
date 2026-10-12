"""Сессия панели appshelf (spec 2026-10-08 §4, 2026-10-12 §3): cookie на год указывает на людей, вошедших в этом
браузере; вход — Apple ID (web/signin.py). Проверяет само приложение: к сокету /run/appshelf/web.sock ходят все
процессы www-data (php-fpm), и RCE в любом PHP-сайте иначе открывало бы страницы без входа.

Cookie — записи через «|», по одной на человека: `<user_id>:<epoch>:<срок>:<HMAC>`, каждая подписана отдельно.
Ключ подписи — cookie-key (0600), appshelf-web читает его при старте: удалить файл и перезапустить appshelf-web —
выйти на всех устройствах всем; epoch человека (+1 — «выйти везде») — только ему."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

COOKIE = "appshelf_auth"
COOKIE_AGE = 365 * 86400
KEY_LEN = 32
SEP = "|"  # между записями: в записи только цифры, «:» и hex, а SimpleCookie такое значение в кавычки не берёт


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


def make_entry(key: bytes, user, now: int) -> str:
    """Запись одного человека; она же — cookie браузера, в котором вошёл он один."""
    expires = now + COOKIE_AGE
    return f"{user.id}:{user.epoch}:{expires}:{_mac(key, user.id, user.epoch, expires)}"


def _check_entry(key: bytes, entry: str, now: int, get_user):
    """Человек по записи или None: подделка, срок, «выйти везде» (epoch), человека удалили."""
    try:
        uid, epoch, expires, mac = entry.split(":")
        uid_i, epoch_i, expires_i = int(uid), int(epoch), int(expires)
    except ValueError:  # пусто, мусор или cookie версии 1 («admin:<срок>:<подпись>»)
        return None
    good = _mac(key, uid_i, epoch_i, expires_i)
    if expires_i < now or not hmac.compare_digest(mac.encode(), good.encode()):
        return None
    user = get_user(uid_i)
    return user if user is not None and user.epoch == epoch_i else None


def signed_users(key: bytes, value: str, now: int, get_user) -> list[tuple]:
    """Вошедшие люди и их записи — по порядку cookie, то есть по порядку входа. Негодная запись пропускается,
    остальные действуют; у повторившегося человека считается первая годная. get_user(uid) → people.User | None."""
    found, seen = [], set()
    for entry in value.split(SEP):
        user = _check_entry(key, entry, now, get_user)
        if user is not None and user.id not in seen:
            seen.add(user.id)
            found.append((user, entry))
    return found


def with_user(key: bytes, signed: list[tuple], user, now: int) -> str:
    """Cookie после входа человека: его запись — новая, на месте прежней или в конце; записи остальных — как
    есть, со своим сроком: вход соседа чужой вход не продлевает. signed — из signed_users."""
    fresh = make_entry(key, user, now)
    entries = [(u.id, e) for u, e in signed]
    if all(uid != user.id for uid, _ in entries):
        entries.append((user.id, fresh))
    return SEP.join(fresh if uid == user.id else e for uid, e in entries)


def without_user(signed: list[tuple], uid: int) -> str:
    """Cookie без записи человека; '' — записей не осталось. signed — из signed_users."""
    return SEP.join(e for u, e in signed if u.id != uid)
