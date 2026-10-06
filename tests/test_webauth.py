from appshelf import webauth
from helpers import basic


def test_password_file_and_header(tmp_path):
    path = tmp_path / "web-auth"
    webauth.set_password(path, "admin", "pw", iterations=1000)
    users, seen = webauth.read_users(path), set()
    assert webauth.check(users, basic("admin", "pw")["Authorization"], seen) is True
    assert webauth.check(users, basic("admin", "nope")["Authorization"], seen) is False
    assert webauth.check(users, "Bearer x", seen) is False
