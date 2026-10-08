from appshelf import cli
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
