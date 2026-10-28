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


def test_play_state_proxy_and_mail_episode(conn, clock):
    uid = people.create_user(conn, "Иван", people.MEMBER, None, clock.iso())
    gid = people.create_google(conn, uid, "sub-1", "a@gmail.example", "", clock.iso())
    g = people.get_google(conn, gid)
    assert (g.play, g.play_since, g.play_country, g.proxy) == ("none", "", "", "")
    people.set_proxy(conn, gid, "de")
    people.set_play(conn, gid, "ok", "t1", country="DE")
    assert people.claim_play_mail(conn, gid, "t2") and not people.claim_play_mail(conn, gid, "t3")   # одно на эпизод
    people.set_play(conn, gid, "expired", "t2")
    people.set_play(conn, gid, "expired", "t3")                      # время начала эпизода не сдвигается
    g = people.get_google(conn, gid)
    assert (g.play, g.play_since, g.play_country, g.proxy) == ("expired", "t2", "DE", "de")
    people.set_play(conn, gid, "ok", "t4", country="")                # подключили заново — эпизод окончен
    assert people.claim_play_mail(conn, gid, "t5")
    people.release_play_mail(conn, gid)                               # письмо не ушло — следующая попытка напишет
    assert people.claim_play_mail(conn, gid, "t6")
    people.set_play(conn, gid, "none", "t7")                          # отключили: опоздавший отказ токена — не «истекло»
    people.set_play(conn, gid, "expired", "t8")
    assert (people.get_google(conn, gid).play, people.get_google(conn, gid).play_country) == ("none", "")
    assert not people.claim_play_mail(conn, 99, "t9")
