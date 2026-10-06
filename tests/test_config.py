from pathlib import Path

import pytest

from appshelf.config import from_env

TOKEN = "0123456789abcdef0123456789abcdef"
BASE = "https://apps.example.com"


def test_defaults_and_paths():
    cfg = from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": BASE})
    assert cfg.data_dir == Path("/var/lib/appshelf")
    assert cfg.db == Path("/var/lib/appshelf/appshelf.db")
    assert cfg.pub_root == Path("/var/lib/appshelf/pub") / TOKEN
    assert cfg.lock == Path("/var/lib/appshelf/ipatool.lock")
    assert cfg.ipatool_home == Path("/etc/appshelf")
    assert cfg.public_url("123-1.0", "app.ipa") == f"{BASE}/d/{TOKEN}/123-1.0/app.ipa"


def test_overrides():
    cfg = from_env({"PUB_TOKEN": TOKEN, "APPSHELF_DATA": "/tmp/a", "APPSHELF_PUBLIC_BASE": "https://x.example/",
                    "IPATOOL_PROXY": "socks5h://proxy.example:1080", "MAIL_TO": "owner@example.com"})
    assert cfg.data_dir == Path("/tmp/a")
    assert cfg.public_base == "https://x.example"
    assert cfg.ipatool_proxy == "socks5h://proxy.example:1080"
    assert cfg.mail_to == "owner@example.com"
    assert cfg.pub_root == Path("/tmp/a/pub") / TOKEN


def test_archive_on_share():
    cfg = from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": BASE, "APPSHELF_PUB": "/mnt/archive/appshelf/pub"})
    assert cfg.pub_root == Path("/mnt/archive/appshelf/pub") / TOKEN
    assert cfg.db == Path("/var/lib/appshelf/appshelf.db")  # SQLite на CIFS портится — база остаётся локально


@pytest.mark.parametrize("token", ["", "abc", TOKEN.upper(), "g" * 32, TOKEN + "0"])
def test_bad_token_rejected(token):
    with pytest.raises(ValueError):
        from_env({"PUB_TOKEN": token, "APPSHELF_PUBLIC_BASE": BASE})


@pytest.mark.parametrize("base", ["", "apps.example.com", "ftp://apps.example.com"])
def test_public_base_required(base):
    with pytest.raises(ValueError):
        from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": base})
