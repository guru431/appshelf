"""Люди, их Apple ID и ссылки (spec 2026-10-08 §4–5).

Человек (users) владеет одним или несколькими Apple ID (accounts): вход любым из них открывает все его полки.
Ссылки (links) — приглашения и запасной вход; в базе только SHA-256 токена. Транзакции открывают только
claim_expired_mail и use_login_link: несколько записей подряд вызывающий оборачивает в store.tx."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import store

OWNER, MEMBER = "owner", "member"
INVITE, LOGIN = "invite", "login"
LOGIN_LINK_TTL = timedelta(hours=24)
# OUI Apple из реестра IEEE (standards-oui.ieee.org/oui/oui.csv). MAC нового Apple ID — префикс отсюда и
# 3 случайных байта: ipatool называет себя Configurator на Mac, а у Mac адреса именно такие
APPLE_OUIS = ("00:03:93", "00:0A:95", "00:1E:C2", "00:25:00", "3C:07:54", "A4:83:E7", "AC:BC:32", "F0:18:98")


@dataclass(frozen=True)
class User:
    id: int
    name: str
    role: str     # owner / member
    epoch: int    # «выйти везде»: +1 гасит все cookie человека


@dataclass(frozen=True)
class Account:
    id: int
    user_id: int
    email: str
    name: str
    storefront: str
    session: str         # токен магазина: none / ok / expired
    session_since: str
    device_mac: str      # IPATOOL_DEVICE_MAC; '' — настоящий MAC сервера (перенесённый из версии 1)
    legacy_pub: bool     # каталог архива — <PUB_TOKEN> (перенесённый из версии 1)
    last_login_at: str


def _user(r) -> User | None:
    return None if r is None else User(r["id"], r["name"], r["role"], r["epoch"])


def _account(r) -> Account | None:
    return None if r is None else Account(r["id"], r["user_id"], r["email"], r["name"], r["storefront"],
                                          r["session"], r["session_since"], r["device_mac"], bool(r["legacy_pub"]),
                                          r["last_login_at"])


def normalize_email(email: str) -> str:
    """Ключ Apple ID — адрес первого входа без пробелов, в нижнем регистре."""
    return email.strip().lower()


# --- люди --------------------------------------------------------------------

def get_user(c, uid: int) -> User | None:
    return _user(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def list_users(c) -> list[User]:
    return [_user(r) for r in c.execute("SELECT * FROM users ORDER BY id")]


def owner_exists(c) -> bool:
    return c.execute("SELECT 1 FROM users WHERE role=?", (OWNER,)).fetchone() is not None


def create_user(c, name: str, role: str, invite_id: int | None, now: str) -> int:
    return c.execute("INSERT INTO users (name, role, invite_id, created_at) VALUES (?, ?, ?, ?)",
                     (name, role, invite_id, now)).lastrowid


def bump_epoch(c, uid: int) -> None:
    c.execute("UPDATE users SET epoch = epoch + 1 WHERE id=?", (uid,))


def delete_user_row(c, uid: int) -> None:
    c.execute("DELETE FROM users WHERE id=?", (uid,))


# --- Apple ID ----------------------------------------------------------------

def get_account(c, aid: int) -> Account | None:
    return _account(c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone())


def account_by_email(c, email: str) -> Account | None:
    return _account(c.execute("SELECT * FROM accounts WHERE email=?", (normalize_email(email),)).fetchone())


def accounts_of(c, uid: int) -> list[Account]:
    return [_account(r) for r in c.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY id", (uid,))]


def all_accounts(c) -> list[Account]:
    return [_account(r) for r in c.execute("SELECT * FROM accounts ORDER BY id")]


def create_account(c, uid: int, email: str, info: dict, device_mac: str, now: str) -> int:
    """Apple ID после первого успешного входа: токен магазина уже есть — session=ok."""
    return c.execute("""INSERT INTO accounts (user_id, email, name, storefront, session, session_since, device_mac,
                            last_login_at, created_at) VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, ?)""",
                     (uid, normalize_email(email), str(info.get("name", "")), str(info.get("storefront", "")),
                      now, device_mac, now, now)).lastrowid


def mark_login(c, aid: int, info: dict, now: str) -> None:
    """Успешный вход: токен свежий, эпизод «вход истёк» закончился."""
    c.execute("""UPDATE accounts SET name=?, storefront=?, session='ok', session_since=?, last_login_at=?,
                     expired_mail_sent='' WHERE id=?""",
              (str(info.get("name", "")), str(info.get("storefront", "")), now, now, aid))


def set_expired(c, aid: int, now: str) -> None:
    c.execute("UPDATE accounts SET session='expired', session_since=? WHERE id=? AND session != 'expired'", (now, aid))


def claim_expired_mail(c, aid: int, now: str) -> bool:
    """Метка «письмо об истечении отправлено» на эпизод. True — поставили мы (пишут два процесса)."""
    with store.tx(c):
        row = c.execute("SELECT expired_mail_sent FROM accounts WHERE id=?", (aid,)).fetchone()
        if row is None or row["expired_mail_sent"]:
            return False
        c.execute("UPDATE accounts SET expired_mail_sent=? WHERE id=?", (now, aid))
    return True


def release_expired_mail(c, aid: int) -> None:
    c.execute("UPDATE accounts SET expired_mail_sent='' WHERE id=?", (aid,))


def is_owner_account(c, acct: Account) -> bool:
    user = get_user(c, acct.user_id)
    return user is not None and user.role == OWNER


# --- ссылки ------------------------------------------------------------------

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_link(c, kind: str, label: str, user_id: int | None, now: str) -> str:
    """Новая ссылка; сырой токен возвращается один раз — в базе только хэш. Запасная — на 24 ч."""
    token = secrets.token_urlsafe(32)
    expires = "" if kind == INVITE else (datetime.fromisoformat(now) + LOGIN_LINK_TTL).isoformat(timespec="seconds")
    c.execute("INSERT INTO links (kind, token_hash, label, user_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
              (kind, hash_token(token), label, user_id, now, expires))
    return token


def active_invite(c, token: str):
    if not token:
        return None
    return c.execute("SELECT * FROM links WHERE token_hash=? AND kind=? AND disabled_at=''",
                     (hash_token(token), INVITE)).fetchone()


def invite_active(c, invite_id: int) -> bool:
    return c.execute("SELECT 1 FROM links WHERE id=? AND kind=? AND disabled_at=''",
                     (invite_id, INVITE)).fetchone() is not None


def invite_label(c, invite_id: int) -> str:
    row = c.execute("SELECT label FROM links WHERE id=?", (invite_id,)).fetchone()
    return row["label"] if row else ""


def list_invites(c) -> list:
    return c.execute("""SELECT l.*, (SELECT COUNT(*) FROM users u WHERE u.invite_id = l.id) AS joined
                        FROM links l WHERE l.kind=? ORDER BY l.id DESC""", (INVITE,)).fetchall()


def disable_invite(c, link_id: int, now: str) -> None:
    c.execute("UPDATE links SET disabled_at=? WHERE id=? AND kind=? AND disabled_at=''", (now, link_id, INVITE))


def use_login_link(c, token: str, now: str) -> int | None:
    """Запасной вход: одноразовая ссылка на 24 ч. user_id — ссылка годна и теперь использована."""
    if not token:
        return None
    with store.tx(c):
        row = c.execute("SELECT id, user_id, expires_at FROM links WHERE token_hash=? AND kind=? AND used_at=''",
                        (hash_token(token), LOGIN)).fetchone()
        if row is None or row["expires_at"] <= now:
            return None
        c.execute("UPDATE links SET used_at=? WHERE id=?", (now, row["id"]))
    return row["user_id"]


# --- устройство --------------------------------------------------------------

def new_device_mac(rand=secrets.token_bytes) -> str:
    """MAC нового Apple ID для IPATOOL_DEVICE_MAC (патч 05): у каждого Apple ID свой «Mac»."""
    raw = rand(4)
    return APPLE_OUIS[raw[0] % len(APPLE_OUIS)] + "".join(f":{b:02X}" for b in raw[1:])
