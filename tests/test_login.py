import pytest

from appshelf.ipatool import AuthCodeRequired, IpatoolError
from appshelf.web.login import LoginExpired, LoginFlow
from helpers import Clock, FakeTool


def waiting_code():
    tool, clock = FakeTool(), Clock()
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    flow = LoginFlow(tool, clock=clock.monotonic)
    assert flow.start("owner@example", "S3cret-pw") is None
    return flow, tool, clock


def test_password_kept_until_code_then_removed():
    flow, tool, _ = waiting_code()
    assert flow.waiting_code and flow.pending_email == "owner@example"
    assert flow.finish("123456")["name"] == "Иван Петров"
    assert tool.calls[-1] == ("login", "owner@example", "S3cret-pw", "123456")
    assert flow._pending is None


def test_password_removed_after_failed_code():
    flow, tool, _ = waiting_code()
    tool.errors["login"] = IpatoolError("invalid_credentials", "")
    with pytest.raises(IpatoolError):
        flow.finish("000000")
    assert flow._pending is None


def test_password_removed_by_ttl():
    flow, tool, clock = waiting_code()
    clock.advance(599)
    flow.purge()
    assert flow.waiting_code
    clock.advance(2)
    flow.purge()
    assert flow._pending is None
    with pytest.raises(LoginExpired):
        flow.finish("123456")
    assert len(tool.calls) == 1


def test_login_without_2fa():
    flow = LoginFlow(FakeTool(), clock=Clock().monotonic)
    assert flow.start("owner@example", "pw")["storefront"] == "RU"
    assert not flow.waiting_code


def test_password_removed_by_timer_without_tick():  # review #7: TTL не зависит от обработчика
    tool, clock, timers = FakeTool(), Clock(), []
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    flow = LoginFlow(tool, clock=clock.monotonic, schedule=lambda delay, fn: timers.append((delay, fn)))
    flow.start("owner@example", "S3cret-pw")
    [(delay, expire)] = timers
    assert delay == 600.0
    expire()
    assert flow._pending is None


def test_stale_timer_keeps_newer_password():
    tool, clock, timers = FakeTool(), Clock(), []
    flow = LoginFlow(tool, clock=clock.monotonic, schedule=lambda delay, fn: timers.append(fn))
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    flow.start("owner@example", "first")
    tool.errors["login"] = AuthCodeRequired("auth_code_required", "")
    flow.start("owner@example", "second")
    timers[0]()
    assert flow.waiting_code
