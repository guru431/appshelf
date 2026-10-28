"""Люди, их Apple ID, Google-аккаунты и ссылки (spec 2026-10-08 §4–5, 2026-10-28 §4).

Человек (users) владеет одним или несколькими Apple ID (accounts) и/или Google-аккаунтами (google_accounts): вход
любым из них открывает все его полки. С 2026-10-13 новый Apple ID всегда приходит отдельным человеком
(web/signin.py) — связки остались от прежних версий; Google-аккаунт переходит к другому только через «Связать».
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
    shelf: str = ""      # токен каталога в архиве (config.shelf_token); '' — legacy_pub или Apple ID версии 2
    shelf_next: str = ""  # новый токен, пока каталог переезжает (jobs.move_shelf)
    history_at: str = ""  # время последней успешной загрузки истории покупок, в том числе пустой


@dataclass(frozen=True)
class GoogleAccount:
    id: int
    user_id: int
    sub: str             # ключ аккаунта OpenID; адрес и имя — только для показа и писем
    email: str
    name: str
    created_at: str
    last_login_at: str


def _user(r) -> User | None:
    return None if r is None else User(r["id"], r["name"], r["role"], r["epoch"])


def _account(r) -> Account | None:
    return None if r is None else Account(r["id"], r["user_id"], r["email"], r["name"], r["storefront"],
                                          r["session"], r["session_since"], r["device_mac"], bool(r["legacy_pub"]),
                                          r["last_login_at"], r["shelf"], r["shelf_next"], r["history_at"])


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


def create_account(c, uid: int, email: str, info: dict, device_mac: str, now: str, session: str = "ok") -> int:
    """Apple ID после первого успешного входа: токен магазина уже есть — session=ok. Регистрация (web/signin.py)
    пишет session=none, пока учётка не легла в accounts/<id>, — тогда mark_ready. Каталог полки в архиве —
    случайный токен: ни из своего токена, ни из ссылок владельца чужой не вычислить."""
    return c.execute("""INSERT INTO accounts (user_id, email, name, storefront, session, session_since, device_mac,
                            last_login_at, created_at, shelf) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (uid, normalize_email(email), str(info.get("name", "")), str(info.get("storefront", "")),
                      session, now, device_mac, now, now, new_shelf_token())).lastrowid


def mark_ready(c, aid: int, now: str) -> bool:
    """Учётка нового Apple ID легла в accounts/<id>: session=none → ok, задания и ночь его теперь берут. Вход тем же
    Apple ID из другого браузера мог успеть раньше — его session не трогаем. False — Apple ID уже удалили."""
    c.execute("UPDATE accounts SET session='ok', session_since=? WHERE id=? AND session='none'", (now, aid))
    return get_account(c, aid) is not None


def drop_unready(c, aid: int) -> bool:
    """Строка Apple ID, регистрация которого не перенесла учётку, — только пока session=none: вход тем же Apple ID
    из другого браузера мог её уже оживить. True — удалили."""
    return c.execute("DELETE FROM accounts WHERE id=? AND session='none'", (aid,)).rowcount > 0


def new_shelf_token() -> str:
    return secrets.token_hex(16)


def set_shelf_next(c, aid: int, token: str) -> None:
    c.execute("UPDATE accounts SET shelf_next=? WHERE id=?", (token, aid))


def set_shelf(c, aid: int, token: str) -> None:
    """Каталог переехал: новый токен — рабочий, связь с PUB_TOKEN (legacy_pub) — в прошлом."""
    c.execute("UPDATE accounts SET shelf=?, shelf_next='', legacy_pub=0 WHERE id=?", (token, aid))


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


def has_apple(c, uid: int) -> bool:
    return c.execute("SELECT 1 FROM accounts WHERE user_id=?", (uid,)).fetchone() is not None


# --- Google-аккаунты ---------------------------------------------------------

def _google(r) -> GoogleAccount | None:
    return None if r is None else GoogleAccount(r["id"], r["user_id"], r["sub"], r["email"], r["name"],
                                                r["created_at"], r["last_login_at"])


def google_by_sub(c, sub: str) -> GoogleAccount | None:
    return _google(c.execute("SELECT * FROM google_accounts WHERE sub=?", (sub,)).fetchone())


def get_google(c, gid: int) -> GoogleAccount | None:
    return _google(c.execute("SELECT * FROM google_accounts WHERE id=?", (gid,)).fetchone())


def google_of(c, uid: int) -> list[GoogleAccount]:
    return [_google(r) for r in c.execute("SELECT * FROM google_accounts WHERE user_id=? ORDER BY id", (uid,))]


def create_google(c, uid: int, sub: str, email: str, name: str, now: str) -> int:
    """Google-аккаунт после первого входа; тот же sub второй раз — sqlite3.IntegrityError."""
    return c.execute("INSERT INTO google_accounts (user_id, sub, email, name, created_at, last_login_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (uid, sub, email, name, now, now)).lastrowid


def mark_google_login(c, gid: int, email: str, name: str, now: str) -> None:
    """Вход: адрес и имя — как сейчас у Google (их меняют), время входа."""
    c.execute("UPDATE google_accounts SET email=?, name=?, last_login_at=? WHERE id=?", (email, name, now, gid))


def link_roles(c, a_uid: int, b_uid: int, order: list[int]) -> tuple[int, int] | None:
    """«Связать» (spec 2026-10-28 §5): (остаётся, уходит) — решает сервер, не человек. Остаётся участник с Apple ID
    (Apple ID никогда не переходит к другому), без Apple ID у обоих — владелец, иначе тот, чья запись в cookie
    раньше (order). Apple ID у обоих или участник один — None: их не связать (решение 2026-10-13)."""
    if a_uid == b_uid:
        return None
    a_apple, b_apple = has_apple(c, a_uid), has_apple(c, b_uid)
    if a_apple and b_apple:
        return None
    if a_apple != b_apple:
        return (a_uid, b_uid) if a_apple else (b_uid, a_uid)
    roles = {uid: get_user(c, uid).role for uid in (a_uid, b_uid)}
    if (roles[a_uid] == OWNER) != (roles[b_uid] == OWNER):
        return (a_uid, b_uid) if roles[a_uid] == OWNER else (b_uid, a_uid)
    return (a_uid, b_uid) if order.index(a_uid) < order.index(b_uid) else (b_uid, a_uid)


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


def invite_of(c, uid: int):
    """Приглашение, по которому пришёл человек, и сколько людей им пришло (joined); None — пришёл без него."""
    return c.execute("""SELECT l.*, (SELECT COUNT(*) FROM users x WHERE x.invite_id = l.id) AS joined
                        FROM users u JOIN links l ON l.id = u.invite_id WHERE u.id=? AND l.kind=?""",
                     (uid, INVITE)).fetchone()


def disable_invite(c, link_id: int, now: str) -> None:
    c.execute("UPDATE links SET disabled_at=? WHERE id=? AND kind=? AND disabled_at=''", (now, link_id, INVITE))


def delete_invite(c, link_id: int) -> None:
    """Приглашение пропадает из списка и перестаёт работать; пришедшие им остаются участниками без отметки о нём."""
    c.execute("UPDATE users SET invite_id=NULL WHERE invite_id=?", (link_id,))
    c.execute("DELETE FROM links WHERE id=? AND kind=?", (link_id, INVITE))


def _login_link(c, token: str, now: str):
    """Запасная ссылка, ещё годная: не использована и не истекла."""
    row = c.execute("SELECT id, user_id, expires_at FROM links WHERE token_hash=? AND kind=? AND used_at=''",
                    (hash_token(token), LOGIN)).fetchone() if token else None
    return row if row is not None and row["expires_at"] > now else None


def login_link_user(c, token: str, now: str) -> int | None:
    """Кому годна запасная ссылка — без расхода: страница по ссылке только показывает кнопку входа."""
    row = _login_link(c, token, now)
    return None if row is None else row["user_id"]


def use_login_link(c, token: str, now: str) -> int | None:
    """Запасной вход: одноразовая ссылка на 24 ч. user_id — ссылка годна и теперь использована."""
    with store.tx(c):
        row = _login_link(c, token, now)
        if row is None:
            return None
        c.execute("UPDATE links SET used_at=? WHERE id=?", (now, row["id"]))
    return row["user_id"]


# --- устройство --------------------------------------------------------------

def new_device_mac(rand=secrets.token_bytes) -> str:
    """MAC нового Apple ID для IPATOOL_DEVICE_MAC (патч 05): у каждого Apple ID свой «Mac»."""
    raw = rand(4)
    return APPLE_OUIS[raw[0] % len(APPLE_OUIS)] + "".join(f":{b:02X}" for b in raw[1:])
