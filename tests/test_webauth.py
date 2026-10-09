import threading

import pytest

from appshelf import people, webauth

KEY = b"k" * 32


def user(epoch=0):
    return people.User(1, "Иван", "owner", epoch)


def check(value, now=1000, found=user):
    return webauth.check_cookie(KEY, value, now, lambda uid: found() if uid == 1 else None)


def test_cookie_points_to_person_with_epoch():
    value = webauth.make_cookie(KEY, user(), 1000)
    assert check(value) == user()
    assert check(value, found=lambda: user(epoch=1)) is None             # «выйти везде»
    assert check(value, found=lambda: None) is None                      # человека удалили
    assert check(value, now=1000 + webauth.COOKIE_AGE + 1) is None       # истёк


def test_forged_and_old_format_cookies_rejected():  # Review Focus 3
    uid, epoch, expires, mac = webauth.make_cookie(KEY, user(), 1000).split(":")
    assert check(f"{uid}:{epoch}:{int(expires) + 10 ** 6}:{mac}") is None   # срок подделкой не продлить
    assert check(f"2:{epoch}:{expires}:{mac}") is None                      # чужой человек
    assert check("admin:1890000000:" + "0" * 64) is None                   # cookie версии 1 (пароль страниц)
    assert check("") is None and check("a:b:c:d") is None and check(f"{uid}:{epoch}:{expires}:ё") is None
    assert webauth.check_cookie(b"x" * 32, f"{uid}:{epoch}:{expires}:{mac}", 1000, lambda uid: user()) is None


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
