from appshelf import cli, jobs, people, store
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


def test_login_link_prints_one_time_url(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "now_iso", lambda: "2026-10-08T00:00:00+00:00")
    c = store.connect(tmp_path / "data" / "appshelf.db")
    uid = people.create_user(c, "Иван", people.OWNER, None, "2026-10-08T00:00:00+00:00")
    people.create_account(c, uid, "owner@example", {}, "", "2026-10-08T00:00:00+00:00")
    c.close()
    assert cli.main(["login-link", "--email", " Owner@Example "]) == 0
    link = capsys.readouterr().out.strip()
    assert link.startswith("https://apps.example.com/l/")
    c = store.connect(tmp_path / "data" / "appshelf.db")
    assert people.use_login_link(c, link.rsplit("/", 1)[1], "2026-10-08T01:00:00+00:00") == uid
    c.close()


def test_login_link_unknown_apple_id(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    assert cli.main(["login-link", "--email", "nobody@example"]) == 2
    assert "нет" in capsys.readouterr().err
