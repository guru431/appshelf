from types import SimpleNamespace

import pytest

from appshelf import jobs, removed, store
from appshelf.config import GB, Config
from helpers import TOKEN, Clock, FakeTool, owner


@pytest.fixture(autouse=True)
def empty_removed_catalog(request, monkeypatch):
    """Настоящий справочник (205 id) — только в test_removed; остальным он бы добавлял вызовы ipatool."""
    if request.module.__name__ != "test_removed":
        monkeypatch.setattr(removed, "load", lambda: [])


@pytest.fixture
def cfg(tmp_path) -> Config:
    """Архив — отдельно от data_dir, как на сервере с архивом на шаре; каталог архива уже есть."""
    (tmp_path / "share" / "pub").mkdir(parents=True)
    return Config(data_dir=tmp_path / "data", pub_token=TOKEN, public_base="https://apps.example",
                  ipatool_bin=tmp_path / "no-ipatool", ipatool_home=tmp_path / "home",
                  ipatool_proxy="", mail_to="owner@example", pub_dir=tmp_path / "share" / "pub")


@pytest.fixture
def conn(cfg):
    c = store.connect(cfg.db)
    yield c
    c.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def acct(conn, clock):
    """Владелец и его Apple ID №1 (legacy_pub: каталог архива — <TOKEN>)."""
    return owner(conn, clock)


@pytest.fixture
def ctx(cfg, clock):
    """Env с подменами: ipatool, письма (только темы), свободное место."""
    tool, sent, free = FakeTool(), [], {"bytes": 50 * GB}
    env = jobs.Env(cfg, tool, now=clock.iso, send=lambda subject, body, to: sent.append(subject),
                   disk_free=lambda path: free["bytes"])
    return SimpleNamespace(env=env, tool=tool, sent=sent, free=free)
