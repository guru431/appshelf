from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from appshelf import jobs, removed, store
from appshelf.config import GB, Config
from appshelf.web.app import create_app
from helpers import HOST, TOKEN, Clock, FakeTool, owner, sign_in


@pytest.fixture(autouse=True)
def empty_removed_catalog(request, monkeypatch):
    """Настоящий справочник (~480 id) — только в test_removed; остальным он бы добавлял вызовы ipatool."""
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
    """Env с подменами: ipatool на каждый Apple ID (tools[id]; tool — у Apple ID №1), письма (только темы),
    свободное место."""
    tools, sent, free = {}, [], {"bytes": 50 * GB}
    env = jobs.Env(cfg, lambda acct: tools.setdefault(acct.id, FakeTool()), now=clock.iso,
                   send=lambda subject, body, to: sent.append(subject), disk_free=lambda path: free["bytes"])
    return SimpleNamespace(env=env, tools=tools, sent=sent, free=free, tool=tools.setdefault(1, FakeTool()))


@pytest.fixture
def make_web(clock):
    """Приложение с подменами. tools[id] — ipatool Apple ID (HOME — accounts/<id>); queue — ipatool первых входов
    новых Apple ID по порядку (пусто — свежий FakeTool), выданные — в new_tools (у каждого .home и .mac);
    sent — (тема, текст)."""
    def make(cfg, conn):
        wb = SimpleNamespace(tools={}, queue=[], new_tools=[], sent=[], free={"bytes": 50 * GB})

        def new_tool(home, mac):
            t = wb.queue.pop(0) if wb.queue else FakeTool()
            t.home, t.mac = home, mac
            wb.new_tools.append(t)
            return t

        def tool(acct):
            return wb.tools.setdefault(acct.id, FakeTool(cfg.accounts_dir / str(acct.id)))

        wb.app = create_app(cfg, tool, new_tool=new_tool,
                            now=clock.iso, clock=clock.monotonic,
                            send=lambda subject, body, to: wb.sent.append((subject, body)),
                            disk_free=lambda path: wb.free["bytes"], start_worker=False)
        wb.client = TestClient(wb.app, base_url=f"https://{HOST}")
        return wb
    return make


@pytest.fixture
def web(make_web, cfg, conn, clock, acct):
    """Владелец вошёл: cookie человека 1, активная полка — Apple ID №1; web.tool — его ipatool."""
    wb = make_web(cfg, conn)
    sign_in(wb.client, cfg, conn, clock, acct.user_id)
    wb.tool = wb.tools.setdefault(acct.id, FakeTool(cfg.accounts_dir / str(acct.id)))
    return wb
