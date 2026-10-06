import io

from appshelf import cli, webauth
from helpers import TOKEN


def env(monkeypatch, tmp_path):
    monkeypatch.setenv("PUB_TOKEN", TOKEN)
    monkeypatch.setenv("APPSHELF_PUBLIC_BASE", "https://apps.example.com")
    monkeypatch.setenv("APPSHELF_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("APPSHELF_IPATOOL", str(tmp_path / "no-ipatool"))


def test_nightly_without_login_only_stamps(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    assert cli.main(["nightly"]) == 0
    assert (tmp_path / "data" / "status" / "nightly.stamp").exists()
    assert "пропущена" in capsys.readouterr().out


def test_set_web_password_from_stdin(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(webauth, "ITERATIONS", 1000)  # боевые 600 000 итераций — секунда на тест
    monkeypatch.setattr("sys.stdin", io.StringIO("pw-from-vault\n"))
    assert cli.main(["set-web-password"]) == 0
    assert webauth.verify(webauth.read_users(tmp_path / "data" / "web-auth")["admin"], "pw-from-vault")


def test_empty_password_rejected(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["set-web-password"]) == 2
