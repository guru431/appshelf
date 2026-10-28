import json
from datetime import datetime, timedelta

import pytest

from appshelf import store
from appshelf.android import pin

T0 = "2026-10-28T00:00:00+00:00"
K = "ip:198.51.100.7"
OTHER = "ip:203.0.113.9"
PIN, WRONG = "482913", "000000"
KEY = b"k" * 32
PRODUCTION_ITERATIONS = pin.ITERATIONS


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    """Сто неверных PIN по 200 000 итераций — десятки секунд."""
    monkeypatch.setattr(pin, "ITERATIONS", 1000)


def at(seconds: float) -> str:
    return (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat()


def close_for_all(conn, prefix="198.51.100") -> list:
    return [pin.try_pin(conn, f"ip:{prefix}.{i}", WRONG, T0) for i in range(pin.ALL_LIMIT)]


def test_production_iterations():
    assert PRODUCTION_ITERATIONS == 200_000


def test_right_pin_ok_and_hash_only_in_state(conn):
    assert not pin.is_set(conn) and pin.epoch(conn) == 0
    pin.set_pin(conn, PIN, T0)
    assert pin.is_set(conn) and pin.epoch(conn) == 1 and store.get_state(conn, "pub_pin_set_at") == T0
    assert pin.try_pin(conn, K, PIN, T0).result == "ok"
    assert pin.try_pin(conn, K, WRONG, T0) == pin.Attempt("wrong", pin.IP_LIMIT - 1, "", False)
    dump = json.dumps([tuple(r) for t in ("state", "pin_attempts") for r in conn.execute(f"SELECT * FROM {t}")])
    assert PIN not in dump
    with pytest.raises(ValueError):
        pin.set_pin(conn, "12345", T0)


def test_twenty_wrong_block_address_for_hour(conn):
    pin.set_pin(conn, PIN, T0)
    for _ in range(pin.IP_LIMIT - 1):
        a = pin.try_pin(conn, K, WRONG, T0)
    assert a == pin.Attempt("wrong", 1, "", False)
    assert pin.try_pin(conn, K, WRONG, T0) == pin.Attempt("ip_blocked", 0, at(3600), False)
    assert pin.try_pin(conn, K, PIN, at(1800)).result == "ip_blocked"        # верный PIN в этот час — тоже
    assert pin.try_pin(conn, OTHER, PIN, at(1800)).result == "ok"            # другой адрес — свой счёт
    assert pin.try_pin(conn, K, PIN, at(3600)).result == "ok"


def test_hundred_wrong_from_many_close_for_all(conn):
    pin.set_pin(conn, PIN, T0)
    results = close_for_all(conn)
    assert [r.just_closed for r in results] == [False] * (pin.ALL_LIMIT - 1) + [True]
    assert (results[-1].result, results[-1].until) == ("closed", at(86400))
    assert pin.closed_until(conn, at(3600)) == at(86400)
    assert pin.try_pin(conn, OTHER, PIN, at(3600)) == pin.Attempt("closed", 0, at(86400), False)
    assert pin.try_pin(conn, OTHER, PIN, at(86400)).result == "ok"
    assert pin.closed_until(conn, at(86400)) == ""


def test_reopen_and_set_pin_open_immediately(conn):
    pin.set_pin(conn, PIN, T0)
    close_for_all(conn)
    store.set_state(conn, pin.MAIL_KEY, T0)                                  # письмо об эпизоде ушло
    pin.reopen(conn)
    assert pin.closed_until(conn, T0) == "" and store.get_state(conn, pin.MAIL_KEY) == ""
    assert pin.try_pin(conn, OTHER, PIN, T0).result == "ok"
    close_for_all(conn, "192.0.2")
    for _ in range(pin.IP_LIMIT):
        pin.try_pin(conn, OTHER, WRONG, T0)
    store.set_state(conn, pin.MAIL_KEY, T0)
    pin.set_pin(conn, "111111", T0)
    assert pin.epoch(conn) == 2 and pin.closed_until(conn, T0) == "" and store.get_state(conn, pin.MAIL_KEY) == ""
    assert pin.try_pin(conn, OTHER, PIN, T0).result == "wrong"                # и адрес открыт, и PIN новый
    assert pin.try_pin(conn, OTHER, "111111", T0).result == "ok"


def test_bad_format_not_counted(conn):
    pin.set_pin(conn, PIN, T0)
    for bad in ["12345", "1234567", " 123456", "123456\n", "１２３４５６", "١٢٣٤٥٦", "abcdef", ""]:
        assert not pin.valid_pin(bad)
        assert pin.try_pin(conn, K, bad, T0).result == "format"
    assert conn.execute("SELECT COUNT(*) FROM pin_attempts").fetchone()[0] == 0
    assert pin.valid_pin(PIN)


def test_window_restarts_after_hour(conn):
    pin.set_pin(conn, PIN, T0)
    for _ in range(10):
        pin.try_pin(conn, K, WRONG, T0)
    for _ in range(15):
        a = pin.try_pin(conn, K, WRONG, at(3600))
    assert a == pin.Attempt("wrong", pin.IP_LIMIT - 15, "", False)


def test_old_address_rows_purged(conn):
    pin.set_pin(conn, PIN, T0)
    for _ in range(pin.IP_LIMIT):                                             # K закрыт на час
        pin.try_pin(conn, K, WRONG, T0)
    pin.try_pin(conn, OTHER, WRONG, at(1800))                                 # окно OTHER ещё идёт
    pin.try_pin(conn, "ip:192.0.2.1", PIN, at(3600))                          # любая новая попытка
    assert {r["key"] for r in conn.execute("SELECT key FROM pin_attempts")} == {pin.ALL, OTHER}
    pin.try_pin(conn, "ip:192.0.2.1", "12", at(3 * 3600))                     # и не по форме — тоже
    assert {r["key"] for r in conn.execute("SELECT key FROM pin_attempts")} == {pin.ALL}  # общий счёт не трогается


def test_without_pin_nothing_opens_and_nothing_counts(conn):
    assert pin.try_pin(conn, K, PIN, T0).result == "closed"
    assert conn.execute("SELECT COUNT(*) FROM pin_attempts").fetchone()[0] == 0


def test_client_key_takes_last_forwarded():
    assert pin.client_key("1.1.1.1, 203.0.113.9", "127.0.0.1") == "ip:203.0.113.9"
    assert pin.client_key("203.0.113.9", "127.0.0.1") == "ip:203.0.113.9"
    assert pin.client_key(None, "198.51.100.7") == pin.client_key("", "198.51.100.7") == "ip:198.51.100.7"
    assert pin.client_key(None, None) == "ip:local"


def test_ipv6_grouped_by_64_and_mapped_v4():
    assert pin.client_key("2001:db8:1:2::5", None) == pin.client_key("2001:db8:1:2:ffff::1", None) == \
        "ip:2001:db8:1:2::/64"
    assert pin.client_key("2001:db8:1:3::5", None) != pin.client_key("2001:db8:1:2::5", None)
    assert pin.client_key("::ffff:198.51.100.7", None) == "ip:198.51.100.7"


def test_guest_cookie_epoch_expiry_and_tamper():
    v = pin.guest_cookie(KEY, 3, T0)
    assert pin.guest_ok(KEY, v, 3, at(89 * 86400))
    assert not pin.guest_ok(KEY, v, 4, T0)                                   # PIN сменили
    assert not pin.guest_ok(KEY, v, 3, at(pin.GUEST_DAYS * 86400))           # срок вышел
    assert not pin.guest_ok(b"x" * 32, v, 3, T0)                              # чужой ключ
    epoch, expires, sig = v.split(":")
    for bad in (f"{epoch}:{int(expires) + 86400}:{sig}", f"4:{expires}:{sig}", f"{epoch}:{expires}:{'é' * 64}",
                f"٣:{expires}:{sig}", f"{epoch}:{expires}", "", "garbage"):
        assert not pin.guest_ok(KEY, bad, 3, T0), bad


def test_counters_survive_new_connection(cfg):
    c = store.connect(cfg.db)
    pin.set_pin(c, PIN, T0)
    for _ in range(pin.IP_LIMIT):
        pin.try_pin(c, K, WRONG, T0)
    c.close()
    c = store.connect(cfg.db)                                                 # перезапуск службы
    assert pin.try_pin(c, K, PIN, T0).result == "ip_blocked"
    c.close()
