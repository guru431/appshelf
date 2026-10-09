"""Вход в панель Apple ID (spec 2026-10-08 §4): незавершённые входы и лимиты неудач. Пароль живёт только в
памяти процесса appshelf-web: не на диске, не в логах, не дольше TTL; удаляется после шага 2 при любом исходе.
Входить могут несколько человек сразу — у каждого входа свой id (cookie браузера)."""
from __future__ import annotations

import hmac
import secrets
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..ipatool import AuthCodeRequired, IpatoolError

TTL = 600.0
MAX_PENDING = 20
JOIN_WAIT = 150.0  # шаг 1 ждёт ipatool до 130 с (10 с блокировки и 120 с входа)


def _timer(delay: float, fn):
    """TTL не зависит от обработчика заданий: пока он полчаса качает IPA, purge() из tick не зовётся."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


class LoginExpired(Exception):
    """Шаг 2 без шага 1 или после TTL."""


class LoginBusy(Exception):
    """Незавершённых входов уже MAX_PENDING."""


class LoginInProgress(Exception):
    """Этим Apple ID уже входят с другим паролем — ждём ответа Apple на первый вход."""


class LoginJoined(Exception):
    """Повторное «Продолжить»: первый вход того же Apple ID отказан — его ошибка, неудача уже посчитана."""

    def __init__(self, error: IpatoolError):
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True)
class Intent:
    """Кого впускаем после успеха: известный Apple ID (account_id) или новый — по приглашению (invite_id), владелец
    из APPSHELF_OWNER (owner) или второй Apple ID вошедшего человека (user_id). У нового — временный HOME ipatool
    (home) и свой MAC (device_mac)."""
    email: str
    account_id: int | None = None
    user_id: int | None = None
    invite_id: int | None = None
    owner: bool = False
    home: Path | None = None
    device_mac: str = ""


def drop_home(intent: Intent) -> None:
    """Брошенный или неудавшийся вход нового Apple ID: временный HOME с учёткой ipatool — прочь."""
    if intent.home is not None:
        shutil.rmtree(intent.home, ignore_errors=True)


@dataclass
class _Pending:
    tool: object
    intent: Intent
    password: str = field(repr=False)
    expires: float = 0.0


@dataclass
class _Running:
    """Шаг 1, который сейчас ждёт Apple: повторное «Продолжить» с тем же паролем получит его итог."""
    password: str = field(repr=False)
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple | None = None          # (intent, flow_id, info)
    error: IpatoolError | None = None


class LoginFlow:
    def __init__(self, clock=time.monotonic, ttl: float = TTL, schedule=_timer, limit: int = MAX_PENDING,
                 discard=drop_home, join_wait: float = JOIN_WAIT):
        self.clock, self.ttl, self.schedule, self.limit, self.discard = clock, ttl, schedule, limit, discard
        self.join_wait = join_wait
        self._pending: dict[str, _Pending] = {}
        self._running: dict[str, _Running] = {}
        self._lock = threading.Lock()

    def _expire(self, flow_id: str, p: _Pending) -> None:
        with self._lock:
            if self._pending.get(flow_id) is not p:  # шаг уже завершён или отброшен
                return
            del self._pending[flow_id]
        self.discard(p.intent)

    def purge(self) -> None:
        now = self.clock()
        with self._lock:
            dead = [(k, p) for k, p in self._pending.items() if now >= p.expires]
            for k, _ in dead:
                del self._pending[k]
        for _, p in dead:
            self.discard(p.intent)

    def pending(self, flow_id: str) -> Intent | None:
        self.purge()
        with self._lock:
            p = self._pending.get(flow_id)
        return None if p is None else p.intent

    def start(self, tool, intent: Intent, password: str) -> tuple[Intent, str, dict | None]:
        """Шаг 1. (intent, "", info) — вход без кода; (intent, flow_id, None) — Apple ждёт код 2FA. Отказ —
        исключение, intent отброшен (discard).

        Тем же Apple ID уже входят (двойное «Продолжить»: браузер бросает первый запрос, а с ним и cookie шага кода) —
        к Apple второй раз не идём: с тем же паролем ждём первый вход и отдаём его итог, с другим — LoginInProgress."""
        self.purge()
        with self._lock:
            other = self._running.get(intent.email)
            same = other is not None and hmac.compare_digest(password.encode(), other.password.encode())
            full = other is None and len(self._pending) >= self.limit
            if other is None and not full:
                mine = self._running[intent.email] = _Running(password)
        if other is not None or full:
            self.discard(intent)
            if full:
                raise LoginBusy()
            if not same or not other.done.wait(self.join_wait) or (other.result is None and other.error is None):
                raise LoginInProgress()
            if other.error is not None:
                raise LoginJoined(other.error)
            return other.result
        try:
            mine.result = (intent, "", tool.login(intent.email, password))
        except AuthCodeRequired:
            flow_id = secrets.token_urlsafe(16)
            p = _Pending(tool, intent, password, self.clock() + self.ttl)
            with self._lock:
                self._pending[flow_id] = p
            self.schedule(self.ttl, lambda: self._expire(flow_id, p))
            mine.result = (intent, flow_id, None)
        except BaseException as e:
            mine.error = e if isinstance(e, IpatoolError) else None
            self.discard(intent)
            raise
        finally:
            with self._lock:
                del self._running[intent.email]
                mine.password = ""
            mine.done.set()
        return mine.result

    def cancel(self, flow_id: str) -> None:
        """«Начать заново» на шаге кода: пароль и временный HOME нового Apple ID — прочь."""
        with self._lock:
            p = self._pending.pop(flow_id, None)
        if p is not None:
            self.discard(p.intent)

    def finish(self, flow_id: str, code: str) -> tuple[Intent, dict]:
        """Шаг 2. Пароль удаляется из памяти при любом исходе; отказ — intent отброшен."""
        with self._lock:
            p = self._pending.pop(flow_id, None)
        if p is None:
            raise LoginExpired()
        if self.clock() >= p.expires:
            self.discard(p.intent)
            raise LoginExpired()
        try:
            return p.intent, p.tool.login(p.intent.email, p.password, code)
        except BaseException:
            self.discard(p.intent)
            raise


class Limiter:
    """Неудачные входы. Общий лимит — 10 за минуту, дальше минуту никого. На Apple ID — 5 ответов Apple «неверный
    пароль» за час, дальше этим Apple ID — до часа с первой неудачи: посторонний, знающий адрес, не доведёт учётку
    до блокировки у Apple. Счётчики — в памяти процесса (один uvicorn)."""

    def __init__(self, clock=time.monotonic, fail_limit: int = 10, fail_window: float = 60.0,
                 email_limit: int = 5, email_window: float = 3600.0):
        self.clock = clock
        self.fail_limit, self.fail_window = fail_limit, fail_window
        self.email_limit, self.email_window = email_limit, email_window
        self._fails: deque[float] = deque()
        self._emails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self) -> bool:
        t = self.clock()
        with self._lock:
            while self._fails and t - self._fails[0] > self.fail_window:
                self._fails.popleft()
            return len(self._fails) >= self.fail_limit

    def fail(self) -> None:
        with self._lock:
            self._fails.append(self.clock())

    def email_wait(self, email: str) -> float:
        """Сколько секунд ещё закрыт вход этим Apple ID; 0 — открыт."""
        t = self.clock()
        with self._lock:
            times = [x for x in self._emails.get(email, []) if t - x < self.email_window]
            if times:
                self._emails[email] = times
            else:
                self._emails.pop(email, None)
            return self.email_window - (t - times[0]) if len(times) >= self.email_limit else 0.0

    def email_fail(self, email: str) -> None:
        with self._lock:
            self._emails.setdefault(email, []).append(self.clock())
