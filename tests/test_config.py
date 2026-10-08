import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace

import pytest

from appshelf.config import from_env

TOKEN = "0123456789abcdef0123456789abcdef"
BASE = "https://apps.example.com"


def test_defaults_and_paths():
    cfg = from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": BASE})
    assert cfg.data_dir == Path("/var/lib/appshelf")
    assert cfg.db == Path("/var/lib/appshelf/appshelf.db")
    assert cfg.archive == Path("/var/lib/appshelf/pub")
    assert cfg.ipatool_home == Path("/etc/appshelf")
    assert cfg.shelf_url(SimpleNamespace(id=1, legacy_pub=True), "123-1.0", "app.ipa") == f"{BASE}/d/{TOKEN}/123-1.0/app.ipa"


def test_overrides():
    cfg = from_env({"PUB_TOKEN": TOKEN, "APPSHELF_DATA": "/tmp/a", "APPSHELF_PUBLIC_BASE": "https://x.example/",
                    "IPATOOL_PROXY": "socks5h://proxy.example:1080", "MAIL_TO": "owner@example.com"})
    assert cfg.data_dir == Path("/tmp/a")
    assert cfg.public_base == "https://x.example"
    assert cfg.ipatool_proxy == "socks5h://proxy.example:1080"
    assert cfg.mail_to == "owner@example.com"
    assert cfg.archive == Path("/tmp/a/pub")


def test_archive_on_share():
    cfg = from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": BASE, "APPSHELF_PUB": "/mnt/archive/appshelf/pub"})
    assert cfg.archive == Path("/mnt/archive/appshelf/pub")
    assert cfg.db == Path("/var/lib/appshelf/appshelf.db")  # SQLite на CIFS портится — база остаётся локально


@pytest.mark.parametrize("token", ["", "abc", TOKEN.upper(), "g" * 32, TOKEN + "0"])
def test_bad_token_rejected(token):
    with pytest.raises(ValueError):
        from_env({"PUB_TOKEN": token, "APPSHELF_PUBLIC_BASE": BASE})


@pytest.mark.parametrize("base", ["", "apps.example.com", "ftp://apps.example.com"])
def test_public_base_required(base):
    with pytest.raises(ValueError):
        from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": base})


def test_owner_and_shelves():
    cfg = from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": BASE, "APPSHELF_OWNER": "  Boss@Example.COM "})
    assert cfg.owner_email == "boss@example.com"
    assert cfg.archive == Path("/var/lib/appshelf/pub")
    assert cfg.accounts_dir == Path("/etc/appshelf/accounts")
    assert cfg.locks_dir == Path("/var/lib/appshelf/locks")
    assert cfg.download_lock == Path("/var/lib/appshelf/locks/download.lock")
    legacy, new = SimpleNamespace(id=1, legacy_pub=True), SimpleNamespace(id=2, legacy_pub=False)
    assert cfg.shelf_token(legacy) == TOKEN                  # перенесённый из версии 1 — прежний каталог
    token = hmac.new(TOKEN.encode(), b"account:2", hashlib.sha256).hexdigest()[:32]
    assert cfg.shelf_token(new) == token and cfg.shelf_root(new) == Path("/var/lib/appshelf/pub") / token
    assert cfg.shelf_url(new, "5-1.0", "app.ipa") == f"{BASE}/d/{token}/5-1.0/app.ipa"
    assert cfg.shelf_token(SimpleNamespace(id=3, legacy_pub=False)) not in (token, TOKEN)


def test_owner_is_optional():
    assert from_env({"PUB_TOKEN": TOKEN, "APPSHELF_PUBLIC_BASE": BASE}).owner_email == ""
