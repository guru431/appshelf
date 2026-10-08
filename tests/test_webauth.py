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
