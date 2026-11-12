"""Вход в панель Apple ID (spec 2026-10-08 §4): незавершённые входы и лимиты неудач. Пароль живёт только в
памяти процесса appshelf-web: не на диске, не в логах, не дольше TTL; удаляется после шага 2 при любом исходе.
Входить могут несколько человек сразу — у каждого входа свой id (cookie браузера)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import shutil
import threading
import time
import weakref
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


class LoginCodeMismatch(LoginExpired):
    """Повтор шага 2 с другим кодом: не двойное «Войти», а чужой запрос с тем же flow_id — итог первого не отдаём,
    это неудача входа Apple ID email."""

    def __init__(self, email: str):
        super().__init__("code mismatch")
        self.email = email


class LoginBusy(Exception):
    """Незавершённых входов уже MAX_PENDING."""


class LoginInProgress(Exception):
    """Этим Apple ID уже входят с другим паролем — ждём ответа Apple на первый вход."""


class LoginJoined(Exception):
    """Повторное «Продолжить» или «Войти»: первый запрос того же входа отказан — его ошибка, неудача уже посчитана."""

    def __init__(self, error: IpatoolError):
        super().__init__(str(error))
        self.error = error


class LoginFinished(Exception):
    """Повторное «Войти»: первый запрос с тем же шагом кода уже вошёл — его итог (settle), к Apple не идём."""

    def __init__(self, outcome):
        super().__init__("login finished")
        self.outcome = outcome


@dataclass(frozen=True)
class Intent:
    """Кого впускаем после успеха: известный Apple ID (account_id) или новый — по приглашению (invite_id), владелец
    из APPSHELF_OWNER (owner) или из «Добавить» вошедшего человека (user_id — кто добавляет: новый Apple ID ему не
    достаётся, а становится отдельным человеком). user_id у известного Apple ID — тоже вход из «Добавить»: человек
    Apple ID не меняется, но шаг кода и ошибки остаются в форме добавления. У нового — временный HOME ipatool (home)
    и свой MAC (device_mac)."""
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
    timer: object = field(default=None, repr=False)  # его callback держит запись только слабой ссылкой


def _drop(p: _Pending) -> None:
    """Запись входа больше не нужна: пароль — прочь, таймер TTL — отменён."""
    p.password = ""
    timer, p.timer = p.timer, None
    cancel = getattr(timer, "cancel", None)
    if cancel is not None:
        cancel()


@dataclass
class _Running:
    """Шаг 1, который сейчас ждёт Apple: повторное «Продолжить» с тем же паролем получит его итог."""
    password: str = field(repr=False)
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple | None = None          # (intent, flow_id, info)
    error: IpatoolError | None = None


@dataclass
class _Finishing:
    """Шаг 2 у Apple или только что завершённый: повторное «Войти» с той же cookie и тем же кодом получит его итог.
    Пароля нет, кода — только хэш."""
    expires: float
    email: str
    code: bytes = field(repr=False)      # SHA-256 кода первого запроса
    done: threading.Event = field(default_factory=threading.Event)
    outcome: object = None               # что передал settle: у signin — (user_id, account_id)
    error: IpatoolError | None = None


class LoginFlow:
    def __init__(self, clock=time.monotonic, ttl: float = TTL, schedule=_timer, limit: int = MAX_PENDING,
                 discard=drop_home, join_wait: float = JOIN_WAIT):
        self.clock, self.ttl, self.schedule, self.limit, self.discard = clock, ttl, schedule, limit, discard
        self.join_wait = join_wait
        self._pending: dict[str, _Pending] = {}
        self._running: dict[str, _Running] = {}
        self._finishing: dict[str, _Finishing] = {}
        self._lock = threading.Lock()

    def _expire(self, flow_id: str, ref: weakref.ref) -> None:
        with self._lock:
            p = ref()
            if p is None or self._pending.get(flow_id) is not p:  # шаг уже завершён или отброшен
                return
            del self._pending[flow_id]
        _drop(p)
        self.discard(p.intent)

    def purge(self) -> None:
        now = self.clock()
        with self._lock:
            dead = [(k, p) for k, p in self._pending.items() if now >= p.expires]
            for k, _ in dead:
                del self._pending[k]
            for k in [k for k, f in self._finishing.items() if now >= f.expires]:
                del self._finishing[k]
        for _, p in dead:
            _drop(p)
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
        к Apple второй раз не идём: с тем же паролем ждём первый вход и отдаём его итог, с другим — LoginInProgress.

        Место под лимит резервируется до похода к Apple: шаг 1 в _running уже занимает его, при коде 2FA переходит
        в _pending одним захватом lock, при прочих исходах освобождается. Повтор того же входа места не занимает."""
        self.purge()
        with self._lock:
            other = self._running.get(intent.email)
            same = other is not None and hmac.compare_digest(password.encode(), other.password.encode())
            full = other is None and len(self._pending) + len(self._running) >= self.limit
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
        p = None
        try:
            mine.result = (intent, "", tool.login(intent.email, password))
        except AuthCodeRequired:
            flow_id = secrets.token_urlsafe(16)
            pending = _Pending(tool, intent, password, self.clock() + self.ttl)
            ref = weakref.ref(pending)  # callback таймера не держит пароль после _drop
            try:
                pending.timer = self.schedule(self.ttl, lambda: self._expire(flow_id, ref))
            except BaseException:  # поток таймера не запустился: без TTL пароль в очередь не кладём
                _drop(pending)
                self.discard(intent)
                raise
            p = pending
            mine.result = (intent, flow_id, None)
        except BaseException as e:
            mine.error = e if isinstance(e, IpatoolError) else None
            self.discard(intent)
            raise
        finally:
            with self._lock:  # running → pending одним захватом: место не теряется и не считается дважды
                if p is not None:
                    self._pending[flow_id] = p
                del self._running[intent.email]
                mine.password = ""
            mine.done.set()
        return mine.result

    def cancel(self, flow_id: str) -> None:
        """«Начать заново» на шаге кода: пароль и временный HOME нового Apple ID — прочь."""
        with self._lock:
            p = self._pending.pop(flow_id, None)
        if p is not None:
            _drop(p)
            self.discard(p.intent)

    def finish(self, flow_id: str, code: str) -> tuple[Intent, dict]:
        """Шаг 2. Пароль удаляется из памяти при любом исходе; отказ — intent отброшен. Успех вызывающий
        завершает settle(flow_id, итог).

        Повтор с тем же flow_id (двойное «Войти»: браузер бросает первый запрос, а с ним и cookie входа) к Apple не
        идёт: ждёт первый и получает его итог — LoginFinished(итог settle) или LoginJoined(отказ Apple). Итог
        хранится join_wait после конца первого запроса; позже или без итога — LoginExpired. Повтор с другим кодом —
        LoginCodeMismatch: flow_id мог получить и другой браузер, тем же паролем попавший в окно шага 1."""
        self.purge()
        digest = hashlib.sha256(code.encode()).digest()
        with self._lock:
            p = self._pending.pop(flow_id, None)
            if p is None:
                other = self._finishing.get(flow_id)
            else:
                mine = self._finishing[flow_id] = _Finishing(self.clock() + self.ttl, p.intent.email, digest)
        if p is None:
            if other is not None and not hmac.compare_digest(digest, other.code):
                raise LoginCodeMismatch(other.email)
            if other is None or not other.done.wait(self.join_wait):
                raise LoginExpired()
            if other.error is not None:
                raise LoginJoined(other.error)
            if other.outcome is None:
                raise LoginExpired()
            raise LoginFinished(other.outcome)
        try:
            if self.clock() >= p.expires:
                raise LoginExpired()
            return p.intent, p.tool.login(p.intent.email, p.password, code)
        except BaseException as e:
            mine.error = e if isinstance(e, IpatoolError) else None
            self.discard(p.intent)
            self.settle(flow_id, None)
            raise
        finally:
            _drop(p)

    def settle(self, flow_id: str, outcome) -> None:
        """Итог шага 2 для повторов (None — входа нет). Живёт join_wait."""
        with self._lock:
            f = self._finishing.get(flow_id)
            if f is None or f.done.is_set():
                return
            f.outcome, f.expires = outcome, self.clock() + self.join_wait
        f.done.set()


class Limiter:
    """Неудачные входы — отказы Apple «неверный пароль или код». Общий лимит — 10 за минуту, дальше минуту никого.
    На Apple ID — 5 таких отказов за час, дальше этим Apple ID — до часа с первой неудачи: посторонний, знающий адрес,
    не доведёт учётку до блокировки у Apple. Неизвестный адрес и плохие ссылки не считаются: к Apple они не ходят,
    токен не подобрать, а их счёт давал анониму закрыть вход всем. Счётчики — в памяти процесса (один uvicorn)."""

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
        t = self.clock()
        with self._lock:
            # адреса, которыми больше не входят, email_wait не почистит — старые неудачи уходят здесь
            for old in [e for e, times in self._emails.items() if t - times[-1] >= self.email_window]:
                del self._emails[old]
            self._emails.setdefault(email, []).append(t)
