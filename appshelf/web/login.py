"""Вход в Apple ID в два шага (spec §7.5). Пароль живёт только в памяти процесса appshelf-web:
не на диске, не в логах, не дольше TTL; удаляется после шага 2 при любом исходе."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..ipatool import AuthCodeRequired

TTL = 600.0


def _timer(delay: float, fn):
    """TTL не зависит от обработчика заданий: пока он полчаса качает IPA, purge() из tick не зовётся."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


class LoginExpired(Exception):
    """Шаг 2 без шага 1 или после TTL."""


@dataclass
class _Pending:
    email: str
    password: str = field(repr=False)
    expires: float = 0.0


class LoginFlow:
    def __init__(self, tool, clock=time.monotonic, ttl: float = TTL, schedule=_timer):
        self.tool, self.clock, self.ttl, self.schedule = tool, clock, ttl, schedule
        self._pending: _Pending | None = None
        self._lock = threading.Lock()

    def _expire(self, p: _Pending) -> None:
        with self._lock:
            if self._pending is p:  # таймер прошлого шага 1 не трогает пароль нового
                self._pending = None

    def purge(self) -> None:
        with self._lock:
            if self._pending is not None and self.clock() >= self._pending.expires:
                self._pending = None

    @property
    def waiting_code(self) -> bool:
        self.purge()
        return self._pending is not None

    @property
    def pending_email(self) -> str:
        self.purge()
        p = self._pending
        return p.email if p is not None else ""

    def start(self, email: str, password: str) -> dict | None:
        """Шаг 1. dict — вход выполнен без кода; None — Apple ждёт код 2FA."""
        with self._lock:
            self._pending = None
        try:
            return self.tool.login(email, password)
        except AuthCodeRequired:
            p = _Pending(email, password, self.clock() + self.ttl)
            with self._lock:
                self._pending = p
            self.schedule(self.ttl, lambda: self._expire(p))
            return None

    def finish(self, code: str) -> dict:
        """Шаг 2. Пароль удаляется из памяти при любом исходе."""
        with self._lock:
            p, self._pending = self._pending, None
        if p is None or self.clock() >= p.expires:
            raise LoginExpired()
        return self.tool.login(p.email, p.password, code)
