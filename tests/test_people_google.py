import sqlite3

import pytest

from appshelf import people
from helpers import make_account


def test_google_accounts_of_person(conn, clock):
    acct = make_account(conn, clock)
    t0 = clock.iso()
    first = people.create_google(conn, acct.user_id, "sub-1", "ivan@gmail.example", "Иван", t0)
    clock.advance(60)
    second = people.create_google(conn, acct.user_id, "sub-2", "work@gmail.example", "", clock.iso())
    assert people.google_by_sub(conn, "sub-1") == people.GoogleAccount(first, acct.user_id, "sub-1",
                                                                       "ivan@gmail.example", "Иван", t0, t0)
    assert people.get_google(conn, second).email == "work@gmail.example"
    assert [x.id for x in people.google_of(conn, acct.user_id)] == [first, second]
    assert people.google_by_sub(conn, "nobody") is None and people.get_google(conn, 99) is None
    clock.advance(60)
    people.mark_google_login(conn, first, "new@gmail.example", "Иван П.", clock.iso())
    g = people.get_google(conn, first)
    assert (g.email, g.name, g.last_login_at) == ("new@gmail.example", "Иван П.", clock.iso())
    assert people.has_apple(conn, acct.user_id)
    solo = people.create_user(conn, "Гость", people.MEMBER, None, clock.iso())
    people.create_google(conn, solo, "sub-3", "solo@gmail.example", "Гость", clock.iso())
    assert not people.has_apple(conn, solo)


def test_sub_is_unique(conn, clock):
    uid = people.create_user(conn, "Иван", people.MEMBER, None, clock.iso())
    people.create_google(conn, uid, "sub-1", "a@gmail.example", "", clock.iso())
    with pytest.raises(sqlite3.IntegrityError):
        people.create_google(conn, uid, "sub-1", "b@gmail.example", "", clock.iso())
