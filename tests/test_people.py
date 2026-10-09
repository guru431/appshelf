from pathlib import Path

from appshelf import people


def owner(conn, clock):
    uid = people.create_user(conn, "Иван", people.OWNER, None, clock.iso())
    aid = people.create_account(conn, uid, "  Owner@Example ", {"name": "Иван Петров", "storefront": "RU"}, "", clock.iso())
    return uid, aid


def test_user_and_accounts(conn, clock):
    assert not people.owner_exists(conn)
    uid, aid = owner(conn, clock)
    second = people.create_account(conn, uid, "second@example", {"name": "Иван", "storefront": "US"},
                                   "00:03:93:01:02:03", clock.iso())
    assert people.owner_exists(conn) and people.get_user(conn, uid) == people.User(uid, "Иван", "owner", 0)
    a = people.account_by_email(conn, "OWNER@example")
    assert (a.id, a.email, a.name, a.storefront, a.session, a.legacy_pub) == (aid, "owner@example", "Иван Петров", "RU", "ok", False)
    assert len(a.shelf) == 32 and a.shelf != people.get_account(conn, second).shelf   # каталог полки — случайный
    assert a.session_since == a.last_login_at == clock.iso()
    assert [x.id for x in people.accounts_of(conn, uid)] == [aid, second]
    assert [x.email for x in people.all_accounts(conn)] == ["owner@example", "second@example"]
    assert people.is_owner_account(conn, a)
    assert people.account_by_email(conn, "nobody@example") is None and people.get_account(conn, 99) is None


def test_expiry_episode_and_login(conn, clock):
    _, aid = owner(conn, clock)
    since = clock.iso()
    clock.advance(60)
    people.set_expired(conn, aid, clock.iso())
    clock.advance(60)
    people.set_expired(conn, aid, clock.iso())                       # повторно — время эпизода не сдвигается
    a = people.get_account(conn, aid)
    assert a.session == "expired" and a.session_since != since and a.session_since < clock.iso()
    assert people.claim_expired_mail(conn, aid, clock.iso()) is True
    assert people.claim_expired_mail(conn, aid, clock.iso()) is False
    people.release_expired_mail(conn, aid)
    assert people.claim_expired_mail(conn, aid, clock.iso()) is True
    people.mark_login(conn, aid, {"name": "Иван П.", "storefront": "US"}, clock.iso())
    a = people.get_account(conn, aid)
    assert (a.session, a.name, a.storefront, a.last_login_at) == ("ok", "Иван П.", "US", clock.iso())
    assert people.claim_expired_mail(conn, aid, clock.iso()) is True  # новый эпизод — метка сброшена входом


def test_epoch_and_delete(conn, clock):
    uid, _ = owner(conn, clock)
    people.bump_epoch(conn, uid)
    assert people.get_user(conn, uid).epoch == 1
    people.delete_user_row(conn, uid)
    assert people.get_user(conn, uid) is None and people.list_users(conn) == []


def test_invites_stored_as_hash(conn, clock):
    token = people.create_link(conn, people.INVITE, "коллеги", None, clock.iso())
    assert token not in "\n".join(conn.iterdump())
    invite = people.active_invite(conn, token)
    assert invite["label"] == "коллеги" and invite["expires_at"] == ""
    assert people.invite_active(conn, invite["id"]) and people.invite_label(conn, invite["id"]) == "коллеги"
    people.create_user(conn, "Пётр", people.MEMBER, invite["id"], clock.iso())
    assert people.list_invites(conn)[0]["joined"] == 1
    people.disable_invite(conn, invite["id"], clock.iso())
    assert people.active_invite(conn, token) is None and not people.invite_active(conn, invite["id"])
    assert people.active_invite(conn, "") is None and people.active_invite(conn, "nope") is None


def test_invite_of_person(conn, clock):
    token = people.create_link(conn, people.INVITE, "все", None, clock.iso())
    invite = people.active_invite(conn, token)
    a = people.create_user(conn, "Пётр", people.MEMBER, invite["id"], clock.iso())
    people.create_user(conn, "Анна", people.MEMBER, invite["id"], clock.iso())
    uid, _ = owner(conn, clock)
    row = people.invite_of(conn, a)
    assert (row["id"], row["label"], row["joined"]) == (invite["id"], "все", 2)
    assert people.invite_of(conn, uid) is None                       # владелец пришёл без приглашения


def test_login_link_once_and_24h(conn, clock):
    uid, _ = owner(conn, clock)
    token = people.create_link(conn, people.LOGIN, "", uid, clock.iso())
    assert people.active_invite(conn, token) is None                 # запасная ссылка — не приглашение
    assert people.login_link_user(conn, token, clock.iso()) == uid   # показать кнопку — не расход
    assert people.login_link_user(conn, token, clock.iso()) == uid
    assert people.use_login_link(conn, token, clock.iso()) == uid
    assert people.login_link_user(conn, token, clock.iso()) is None
    assert people.use_login_link(conn, token, clock.iso()) is None   # одноразовая
    late = people.create_link(conn, people.LOGIN, "", uid, clock.iso())
    clock.advance(24 * 3600)
    assert people.use_login_link(conn, late, clock.iso()) is None    # ровно сутки — уже нет
    assert people.use_login_link(conn, "", clock.iso()) is None


def test_device_mac_has_apple_oui_and_is_unicast():
    mac = people.new_device_mac(lambda n: bytes([3, 0xAB, 0xCD, 0xEF])[:n])
    assert mac == people.APPLE_OUIS[3] + ":AB:CD:EF"
    for oui in people.APPLE_OUIS:
        first = int(oui[:2], 16)
        assert first & 0x01 == 0 and first & 0x02 == 0              # не multicast, не «локальный» адрес
    real = people.new_device_mac()
    assert len(real) == 17 and real[:8] in people.APPLE_OUIS


def test_normalize_email():
    assert people.normalize_email("  Ivan@Example.COM\t") == "ivan@example.com"
