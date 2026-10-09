import threading
from pathlib import Path

import pytest

from appshelf.ipatool import AuthCodeRequired, IpatoolError
from appshelf.web.login import Intent, Limiter, LoginBusy, LoginExpired, LoginFlow, LoginInProgress, LoginJoined
from helpers import Clock, FakeTool

KNOWN = Intent("owner@example", account_id=1)
NEW = Intent("new@example", invite_id=5, home=Path("new-home"), device_mac="00:03:93:01:02:03")


def flow(**kw):
    clock, dropped = Clock(), []
    f = LoginFlow(clock=clock.monotonic, schedule=lambda delay, fn: None, discard=dropped.append, **kw)
    return f, clock, dropped


def needs_code():
    tool = FakeTool()
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    return tool


def test_password_kept_until_code_then_removed():
    f, _, dropped = flow()
    tool = needs_code()
    intent, flow_id, info = f.start(tool, KNOWN, "S3cret-pw")
    assert intent == KNOWN and info is None and flow_id and f.pending(flow_id) == KNOWN
    intent, info = f.finish(flow_id, "123456")
    assert intent == KNOWN and info["name"] == "Иван Петров"
    assert tool.calls[-1] == ("login", "owner@example", "S3cret-pw", "123456")
    assert f.pending(flow_id) is None and f._pending == {} and dropped == []


def test_login_without_2fa():
    f, _, dropped = flow()
    assert f.start(FakeTool(), KNOWN, "pw") == (KNOWN, "", FakeTool().account)
    assert f._pending == {} and f._running == {} and dropped == []


def test_failed_code_drops_password_and_intent():
    f, _, dropped = flow()
    tool = needs_code()
    _, flow_id, _ = f.start(tool, NEW, "S3cret-pw")
    tool.errors["login"] = IpatoolError("invalid_credentials", "")
    with pytest.raises(IpatoolError):
        f.finish(flow_id, "000000")
    assert f._pending == {} and dropped == [NEW]


def test_refused_first_step_drops_intent():
    f, _, dropped = flow()
    tool = FakeTool()
    tool.errors["login"] = IpatoolError("edge_rejected", "HTTP 301")
    with pytest.raises(IpatoolError):
        f.start(tool, NEW, "pw")
    assert dropped == [NEW] and f._pending == {} and f._running == {}


def test_ttl_drops_password_and_new_home():
    f, clock, dropped = flow()
    tool = needs_code()
    _, flow_id, _ = f.start(tool, NEW, "S3cret-pw")
    clock.advance(599)
    assert f.pending(flow_id) == NEW
    clock.advance(2)
    assert f.pending(flow_id) is None and dropped == [NEW]
    with pytest.raises(LoginExpired):
        f.finish(flow_id, "123456")
    assert len(tool.calls) == 1


def test_timer_drops_password_without_requests():  # TTL не зависит от обработчика и страниц
    clock, dropped, timers = Clock(), [], []
    f = LoginFlow(clock=clock.monotonic, schedule=lambda delay, fn: timers.append((delay, fn)),
                  discard=dropped.append)
    f.start(needs_code(), NEW, "S3cret-pw")
    [(delay, expire)] = timers
    assert delay == 600.0
    expire()
    assert f._pending == {} and dropped == [NEW]
    expire()                                   # повторное срабатывание ничего не трогает
    assert dropped == [NEW]


def test_cancel_drops_password_and_new_home():
    f, _, dropped = flow()
    _, flow_id, _ = f.start(needs_code(), NEW, "S3cret-pw")
    f.cancel(flow_id)
    f.cancel(flow_id)                          # повторная отмена ничего не трогает
    assert f._pending == {} and dropped == [NEW]
    with pytest.raises(LoginExpired):
        f.finish(flow_id, "123456")


def test_two_browsers_keep_separate_steps():
    f, _, _ = flow()
    a, b = needs_code(), needs_code()
    _, first, _ = f.start(a, KNOWN, "pw-a")
    _, second, _ = f.start(b, NEW, "pw-b")
    assert first != second
    assert f.finish(second, "222222")[0] == NEW
    assert b.calls[-1] == ("login", "new@example", "pw-b", "222222")
    assert a.calls == [("login", "owner@example", "pw-a", "")]
    assert f.pending(first) == KNOWN


def test_pending_limit():
    f, _, dropped = flow(limit=2)
    f.start(needs_code(), KNOWN, "a")
    f.start(needs_code(), Intent("b@example", account_id=2), "b")
    third = needs_code()
    with pytest.raises(LoginBusy):
        f.start(third, NEW, "c")
    assert third.calls == [] and dropped == [NEW]


class SpyEvent(threading.Event):
    """Событие «первый вход закончен», которое сообщает, что второй запрос его уже ждёт."""

    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


def in_thread(fn):
    out = {}

    def run():
        try:
            out["result"] = fn()
        except Exception as e:
            out["error"] = e

    t = threading.Thread(target=run)
    t.start()
    return t, out


def slow_first_login(f, tool, intent, password):
    """Шаг 1, застрявший в Apple: (поток, его итог, «отпустить Apple», событие его конца)."""
    release, entered = threading.Event(), threading.Event()
    tool.on_login = lambda email: (entered.set(), release.wait(5))
    t, out = in_thread(lambda: f.start(tool, intent, password))
    assert entered.wait(5)
    done = f._running[intent.email].done = SpyEvent()
    return t, out, release, done


def test_double_continue_joins_first_login():
    # браузер бросил первый запрос, а с ним и cookie шага кода: второй получает тот же шаг, к Apple не идёт
    f, _, dropped = flow()
    first_tool, second_tool = needs_code(), FakeTool()
    second = Intent("new@example", invite_id=5, home=Path("second-home"))
    t, out, release, done = slow_first_login(f, first_tool, NEW, "S3cret-pw")
    j, joined = in_thread(lambda: f.start(second_tool, second, "S3cret-pw"))
    assert done.waiting.wait(5)
    release.set()
    t.join(5)
    j.join(5)
    assert joined["result"] == out["result"] and joined["result"][0] == NEW and joined["result"][1]
    assert second_tool.calls == [] and len(first_tool.calls) == 1 and dropped == [second]
    assert f._running == {}


def test_double_continue_with_other_password_does_not_wait():
    f, _, _ = flow()
    other = FakeTool()
    t, _, release, _ = slow_first_login(f, needs_code(), KNOWN, "pw-1")
    with pytest.raises(LoginInProgress):
        f.start(other, KNOWN, "pw-2")
    release.set()
    t.join(5)
    assert other.calls == []


def test_double_continue_shares_refusal():
    f, _, _ = flow()
    tool = FakeTool()
    tool.errors["login"] = IpatoolError("invalid_credentials", "")
    t, out, release, done = slow_first_login(f, tool, KNOWN, "bad")
    j, joined = in_thread(lambda: f.start(FakeTool(), KNOWN, "bad"))
    assert done.waiting.wait(5)
    release.set()
    t.join(5)
    j.join(5)
    assert isinstance(joined["error"], LoginJoined) and joined["error"].error is out["error"]


def test_limiter_global_minute_and_apple_id_hour():
    clock = Clock()
    lim = Limiter(clock.monotonic)
    for _ in range(9):
        lim.fail()
    assert not lim.blocked()
    lim.fail()
    assert lim.blocked()
    clock.advance(61)
    assert not lim.blocked()
    for _ in range(4):
        lim.email_fail("a@example")
    assert lim.email_wait("a@example") == 0
    lim.email_fail("a@example")
    assert lim.email_wait("a@example") == 3600 and lim.email_wait("b@example") == 0
    clock.advance(1800)
    assert lim.email_wait("a@example") == 1800
    clock.advance(1800)
    assert lim.email_wait("a@example") == 0
