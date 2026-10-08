from pathlib import Path

import pytest

from appshelf.ipatool import AuthCodeRequired, IpatoolError
from appshelf.web.login import Intent, Limiter, LoginBusy, LoginExpired, LoginFlow
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
    flow_id, info = f.start(tool, KNOWN, "S3cret-pw")
    assert info is None and flow_id and f.pending(flow_id) == KNOWN
    intent, info = f.finish(flow_id, "123456")
    assert intent == KNOWN and info["name"] == "Иван Петров"
    assert tool.calls[-1] == ("login", "owner@example", "S3cret-pw", "123456")
    assert f.pending(flow_id) is None and f._pending == {} and dropped == []


def test_login_without_2fa():
    f, _, dropped = flow()
    assert f.start(FakeTool(), KNOWN, "pw") == ("", FakeTool().account)
    assert f._pending == {} and dropped == []


def test_failed_code_drops_password_and_intent():
    f, _, dropped = flow()
    tool = needs_code()
    flow_id, _ = f.start(tool, NEW, "S3cret-pw")
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
    assert dropped == [NEW] and f._pending == {}


def test_ttl_drops_password_and_new_home():
    f, clock, dropped = flow()
    tool = needs_code()
    flow_id, _ = f.start(tool, NEW, "S3cret-pw")
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


def test_two_browsers_keep_separate_steps():
    f, _, _ = flow()
    a, b = needs_code(), needs_code()
    first, _ = f.start(a, KNOWN, "pw-a")
    second, _ = f.start(b, NEW, "pw-b")
    assert first != second
    assert f.finish(second, "222222")[0] == NEW
    assert b.calls[-1] == ("login", "new@example", "pw-b", "222222")
    assert a.calls == [("login", "owner@example", "pw-a", "")]
    assert f.pending(first) == KNOWN


def test_pending_limit():
    f, _, dropped = flow(limit=2)
    f.start(needs_code(), KNOWN, "a")
    f.start(needs_code(), KNOWN, "b")
    third = needs_code()
    with pytest.raises(LoginBusy):
        f.start(third, NEW, "c")
    assert third.calls == [] and dropped == [NEW]


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
