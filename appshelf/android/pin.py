"""PIN публичной Android-полки (spec 2026-10-28 §8): хэш с солью в state, счётчики перебора в pin_attempts (переживают
перезапуск при выкладке), cookie гостя. Сам PIN нигде не хранится и не пишется.

Перебор: IP_LIMIT неверных с адреса за IP_WINDOW — адрес закрыт на IP_WINDOW; ALL_LIMIT неверных со всех адресов за
ALL_WINDOW — ввод закрыт новым гостям на ALL_WINDOW (без общего предела 10⁶ PIN подбираются с сети IPv6 /48 меньше
чем за час). Окно отсчитывается от первой неудачи. Пределы проверяются до хэша: закрытый ввод не грузит процессор,
а 200 000 итераций держат дорогим подбор по украденной базе."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import notify, store
from ..store import tx

IP_LIMIT = 20
IP_WINDOW = 3600
ALL_LIMIT = 100
ALL_WINDOW = 86400
GUEST_DAYS = 90
ITERATIONS = 200_000
COOKIE = "appshelf_pub"
MAIL_KEY = "pub_pin_lock_mail_sent"
HASH_KEY, SALT_KEY, EPOCH_KEY, SET_AT_KEY = "pub_pin_hash", "pub_pin_salt", "pub_pin_epoch", "pub_pin_set_at"
ALL = "all"  # строка общего счёта; адреса — "ip:…"
PIN_RE = re.compile(r"[0-9]{6}")  # не \d: он пускает цифры других алфавитов
COOKIE_RE = re.compile(r"([0-9]{1,9}):([0-9]{1,12}):([0-9a-f]{64})")


@dataclass(frozen=True)
class Attempt:
    result: str        # ok | wrong | format | ip_blocked | closed
    left: int          # сколько неверных ещё можно с этого адреса
    until: str         # ISO: до какого времени закрыто ("" — открыто)
    just_closed: bool  # эта попытка закрыла ввод всем — письмо владельцу


def valid_pin(pin: str) -> bool:
    return PIN_RE.fullmatch(pin) is not None


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def is_set(c) -> bool:
    return bool(store.get_state(c, HASH_KEY))


def epoch(c) -> int:
    return int(store.get_state(c, EPOCH_KEY) or 0)


def set_pin(c, pin: str, now: str) -> None:
    """Задать или сменить PIN: эпоха +1 (cookie гостей недействительны), счётчики и метка письма — сначала."""
    if not valid_pin(pin):
        raise ValueError("PIN — 6 цифр")
    salt = secrets.token_hex(16)
    digest = _hash(pin, salt)
    with tx(c):
        store.set_state(c, SALT_KEY, salt)
        store.set_state(c, HASH_KEY, digest)
        store.set_state(c, EPOCH_KEY, str(epoch(c) + 1))
        store.set_state(c, SET_AT_KEY, now)
        c.execute("DELETE FROM pin_attempts")
        notify.reset(c, MAIL_KEY)


def client_key(forwarded: str | None, peer: str | None) -> str:
    """Ключ счётчика: последний элемент X-Forwarded-For — его дописывает Apache, всё левее прислал клиент; заголовка
    нет — адрес соединения. IPv6 — его /64 (у одного клиента их миллиарды), IPv4 внутри IPv6 — как IPv4."""
    raw = (forwarded or "").split(",")[-1].strip() or peer or "local"
    try:
        a = ipaddress.ip_address(raw.split("%")[0])
    except ValueError:
        return f"ip:{raw[:64]}"
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.version == 6:
        return f"ip:{ipaddress.ip_network(f'{a}/64', strict=False)}"
    return f"ip:{a}"


def _until(row, t: datetime) -> str:
    if row is not None and row["blocked_until"] and t < datetime.fromisoformat(row["blocked_until"]):
        return row["blocked_until"]
    return ""


def _fail(c, key: str, row, t: datetime, limit: int, window: int) -> tuple[int, str]:
    """Неверная попытка в счёт key: окно — от первой неудачи, истекло — новое; предел — закрыто на окно."""
    if row is None or t >= datetime.fromisoformat(row["first_at"]) + timedelta(seconds=window):
        count, first = 1, t.isoformat()
    else:
        count, first = row["count"] + 1, row["first_at"]
    until = (t + timedelta(seconds=window)).isoformat() if count >= limit else ""
    c.execute("INSERT INTO pin_attempts (key, count, first_at, blocked_until) VALUES (?, ?, ?, ?) "
              "ON CONFLICT(key) DO UPDATE SET count=excluded.count, first_at=excluded.first_at, "
              "blocked_until=excluded.blocked_until", (key, count, first, until))
    return count, until


def try_pin(c, key: str, pin: str, now: str) -> Attempt:
    """Попытка гостя — одной транзакцией: параллельные запросы не проскочат предел вместе. Закрыто всем, закрыт адрес,
    PIN не задан — хэш не считается; не 6 цифр — не попытка. Строки адресов, у которых и окно, и блокировка
    истекли, — прочь: иначе таблица копилась бы годами."""
    t = datetime.fromisoformat(now)
    with tx(c):
        c.execute("DELETE FROM pin_attempts WHERE key != ? AND blocked_until <= ? AND first_at <= ?",
                  (ALL, t.isoformat(), (t - timedelta(seconds=IP_WINDOW)).isoformat()))
        rows = {r["key"]: r for r in c.execute("SELECT * FROM pin_attempts WHERE key IN (?, ?)", (ALL, key))}
        if closed := _until(rows.get(ALL), t):
            return Attempt("closed", 0, closed, False)
        if blocked := _until(rows.get(key), t):
            return Attempt("ip_blocked", 0, blocked, False)
        if not valid_pin(pin):
            return Attempt("format", IP_LIMIT - _count(rows.get(key), t, IP_WINDOW), "", False)
        stored, salt = store.get_state(c, HASH_KEY), store.get_state(c, SALT_KEY)
        if not stored:
            return Attempt("closed", 0, "", False)
        if hmac.compare_digest(_hash(pin, salt), stored):
            c.execute("DELETE FROM pin_attempts WHERE key=?", (key,))
            return Attempt("ok", IP_LIMIT, "", False)
        count, blocked = _fail(c, key, rows.get(key), t, IP_LIMIT, IP_WINDOW)
        _, closed = _fail(c, ALL, rows.get(ALL), t, ALL_LIMIT, ALL_WINDOW)
    if closed:
        return Attempt("closed", 0, closed, True)
    if blocked:
        return Attempt("ip_blocked", 0, blocked, False)
    return Attempt("wrong", IP_LIMIT - count, "", False)


def _count(row, t: datetime, window: int) -> int:
    """Неверных в текущем окне."""
    if row is None or t >= datetime.fromisoformat(row["first_at"]) + timedelta(seconds=window):
        return 0
    return row["count"]


def closed_until(c, now: str) -> str:
    """До какого времени ввод закрыт новым гостям; "" — открыт."""
    return _until(c.execute("SELECT * FROM pin_attempts WHERE key=?", (ALL,)).fetchone(), datetime.fromisoformat(now))


def reopen(c) -> None:
    """«Открыть ввод PIN»: общий счёт — с нуля, следующий эпизод — новое письмо. Закрытые адреса остаются."""
    with tx(c):
        c.execute("DELETE FROM pin_attempts WHERE key=?", (ALL,))
        notify.reset(c, MAIL_KEY)


def _sign(key: bytes, epoch_: int, expires: int) -> str:
    return hmac.new(key, f"pub\0{epoch_}\0{expires}".encode(), hashlib.sha256).hexdigest()


def guest_cookie(key: bytes, epoch_: int, now: str) -> str:
    """<эпоха>:<истекает, unix>:<HMAC>: подделать или продлить без cookie-key нельзя, смена PIN гасит все."""
    expires = int(datetime.fromisoformat(now).timestamp()) + GUEST_DAYS * 86400
    return f"{epoch_}:{expires}:{_sign(key, epoch_, expires)}"


def guest_ok(key: bytes, value: str, epoch_: int, now: str) -> bool:
    m = COOKIE_RE.fullmatch(value or "")
    if m is None:
        return False
    e, expires = int(m[1]), int(m[2])
    return (e == epoch_ and datetime.fromisoformat(now).timestamp() < expires
            and hmac.compare_digest(m[3], _sign(key, e, expires)))
