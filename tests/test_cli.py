import os

import pytest

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


def test_nightly_gets_android_sources_with_extra_ca(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPSHELF_SOURCES_CA", str(tmp_path / "ca.pem"))
    seen = []
    monkeypatch.setattr(cli.net, "Http", lambda ca: seen.append(ca) or "http")
    monkeypatch.setattr(cli.nightly, "run", lambda e, c: seen.append(e.sources) or "ok")
    assert cli.main(["nightly"]) == 0
    assert seen[0] == tmp_path / "ca.pem" and seen[1].http == "http"


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


def test_rotate_token_moves_shelf(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    c = store.connect(tmp_path / "data" / "appshelf.db")
    uid = people.create_user(c, "Пётр", people.MEMBER, None, "2026-10-08T00:00:00+00:00")
    aid = people.create_account(c, uid, "petr@example", {}, "", "2026-10-08T00:00:00+00:00")
    old = people.get_account(c, aid).shelf
    (tmp_path / "data" / "pub" / old / "7-1.0").mkdir(parents=True)
    assert cli.main(["rotate-token", "--email", "petr@example"]) == 0
    new = people.get_account(c, aid).shelf
    c.close()
    assert new != old and (tmp_path / "data" / "pub" / new / "7-1.0").is_dir()
    assert not (tmp_path / "data" / "pub" / old).exists() and "готово" in capsys.readouterr().out


def test_refuses_to_run_as_root(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(SystemExit, match="sudo -u appshelf"):
        cli.main(["nightly"])
    assert not (tmp_path / "data" / "status" / "nightly.stamp").exists()


def test_cli_rotate_public_and_android(monkeypatch, tmp_path, capsys):
    from appshelf.android import db as android_db
    env(monkeypatch, tmp_path)
    c = store.connect(tmp_path / "data" / "appshelf.db")
    uid = people.create_user(c, "Пётр", people.MEMBER, None, "2026-10-08T00:00:00+00:00")
    people.create_account(c, uid, "petr@example", {}, "", "2026-10-08T00:00:00+00:00")
    assert cli.main(["rotate-token", "--public"]) == 2                                # полок ещё нет
    assert cli.main(["rotate-token", "--email", "petr@example", "--android"]) == 2
    assert "нет" in capsys.readouterr().err
    mine, pub = android_db.ensure_shelf(c, uid, "t0"), android_db.ensure_public_shelf(c, "t0")
    (tmp_path / "data" / "pub" / pub.shelf / "ru.x-1").mkdir(parents=True)
    assert cli.main(["rotate-token", "--public"]) == 0
    new_pub = android_db.public_shelf(c)
    assert new_pub.shelf != pub.shelf and (tmp_path / "data" / "pub" / new_pub.shelf / "ru.x-1").is_dir()
    assert android_db.get_shelf(c, mine.id) == mine                                   # своя полка не тронута
    apple = people.get_account(c, people.account_by_email(c, "petr@example").id).shelf
    assert cli.main(["rotate-token", "--email", "petr@example", "--android"]) == 0
    assert android_db.get_shelf(c, mine.id).shelf != mine.shelf
    assert people.account_by_email(c, "petr@example").shelf == apple                  # полка Apple ID — та же
    assert "готово" in capsys.readouterr().out
    c.close()
    with pytest.raises(SystemExit):
        cli.main(["rotate-token", "--public", "--android"])
