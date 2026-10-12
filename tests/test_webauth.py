import threading
from dataclasses import replace

import pytest

from appshelf import people, webauth

KEY = b"k" * 32
IVAN = people.User(1, "Иван", "owner", 0)
PETR = people.User(2, "Пётр", "member", 0)


def user(epoch=0):
    return replace(IVAN, epoch=epoch)


def check(value, now=1000, found=user):
    """Единственный вошедший по cookie или None."""
    got = webauth.signed_users(KEY, value, now, lambda uid: found() if uid == 1 else None)
    return got[0][0] if got else None


def among(*users):
    return {u.id: u for u in users}.get


def ids(value, now=1000, get=among(IVAN, PETR)):
    return [u.id for u, _ in webauth.signed_users(KEY, value, now, get)]


def test_cookie_points_to_person_with_epoch():
    value = webauth.make_entry(KEY, user(), 1000)
    assert check(value) == user()
    assert check(value, found=lambda: user(epoch=1)) is None             # «выйти везде»
    assert check(value, found=lambda: None) is None                      # человека удалили
    assert check(value, now=1000 + webauth.COOKIE_AGE + 1) is None       # истёк


def test_forged_and_old_format_cookies_rejected():  # Review Focus 3
    uid, epoch, expires, mac = webauth.make_entry(KEY, user(), 1000).split(":")
    assert check(f"{uid}:{epoch}:{int(expires) + 10 ** 6}:{mac}") is None   # срок подделкой не продлить
    assert check(f"2:{epoch}:{expires}:{mac}") is None                      # чужой человек
    assert check("admin:1890000000:" + "0" * 64) is None                   # cookie версии 1 (пароль страниц)
    assert check("") is None and check("a:b:c:d") is None and check(f"{uid}:{epoch}:{expires}:ё") is None
    assert webauth.signed_users(b"x" * 32, f"{uid}:{epoch}:{expires}:{mac}", 1000, lambda uid: user()) == []


def test_cookie_holds_several_people_in_sign_in_order():
    value = webauth.with_user(KEY, [], PETR, 1000)
    value = webauth.with_user(KEY, webauth.signed_users(KEY, value, 2000, among(IVAN, PETR)), IVAN, 2000)
    assert value.count("|") == 1 and ids(value, now=2000) == [2, 1]
    assert ids(webauth.make_entry(KEY, IVAN, 1000)) == [1]       # cookie версии 2026-10-08 — список из одной записи


def test_bad_entry_is_skipped_and_others_stay():  # 2026-10-12, Review Focus 1
    ivan, petr = webauth.make_entry(KEY, IVAN, 1000), webauth.make_entry(KEY, PETR, 5000)
    both = f"{ivan}|{petr}"
    assert ids(both) == [1, 2]
    assert ids(both, now=1000 + webauth.COOKIE_AGE + 1) == [2]              # срок Ивана вышел, Петра — нет
    assert ids(both, get=among(replace(IVAN, epoch=1), PETR)) == [2]        # «выйти везде» у Ивана
    assert ids(both, get=among(PETR)) == [2]                                # Ивана удалили
    assert ids(f"junk|admin:1890000000:{'0' * 64}||{petr}|a:b:c:d") == [2]
    assert ids(f"{ivan}|{ivan}|{petr}") == [1, 2]                           # повтор человека — один раз
    assert ids("|" * 500) == []


def test_sign_in_again_keeps_place_and_neighbours_keep_their_term():
    get = among(IVAN, PETR)
    first = webauth.signed_users(KEY, webauth.make_entry(KEY, IVAN, 1000), 1000, get)
    value = webauth.with_user(KEY, first, PETR, 1000)
    again = webauth.with_user(KEY, webauth.signed_users(KEY, value, 9000, get), IVAN, 9000)
    assert again.split("|")[1] == value.split("|")[1]                       # запись Петра — как была
    assert again.split("|")[0].split(":")[2] == str(9000 + webauth.COOKIE_AGE)
    assert ids(again, now=9000) == [1, 2]                                   # порядок полок в шапке не меняется
    assert ids(again, now=1000 + webauth.COOKIE_AGE + 1) == [1]             # вход Ивана срок Петра не продлил


def test_without_user_drops_one_entry():
    ivan, petr = webauth.make_entry(KEY, IVAN, 1000), webauth.make_entry(KEY, PETR, 1000)
    signed = webauth.signed_users(KEY, f"{ivan}|{petr}", 1000, among(IVAN, PETR))
    assert webauth.without_user(signed, 1) == petr and webauth.without_user(signed, 2) == ivan
    assert webauth.without_user(signed, 99) == f"{ivan}|{petr}"
    assert webauth.without_user(signed[:1], 1) == ""                        # записей не осталось


def test_key_file_created_once(tmp_path):
    path = tmp_path / "cookie-key"
    key = webauth.load_key(path)
    assert len(key) == 32 and webauth.load_key(path) == key
    assert [p.name for p in tmp_path.iterdir()] == ["cookie-key"]          # временный файл не остался


@pytest.mark.parametrize("content", [b"", b"short"])
def test_empty_or_short_key_refused(tmp_path, content):
    # пустой ключ (сбой записи, гонка первого создания) — подпись cookie владельца посчитал бы кто угодно
    path = tmp_path / "cookie-key"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="перезапустите appshelf-web"):
        webauth.load_key(path)


def test_failed_write_leaves_no_key_file(tmp_path, monkeypatch):
    def full_disk(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(webauth.os, "fsync", full_disk)
    with pytest.raises(OSError):
        webauth.load_key(tmp_path / "cookie-key")
    assert list(tmp_path.iterdir()) == []


def test_concurrent_first_load_gets_one_full_key(tmp_path):
    path, keys = tmp_path / "cookie-key", []
    threads = [threading.Thread(target=lambda: keys.append(webauth.load_key(path))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(keys) == 8 and set(keys) == {path.read_bytes()} and len(keys[0]) == 32
