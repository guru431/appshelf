import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from appshelf import ipatool
from appshelf.ipatool import AuthCodeRequired, Ipatool, IpatoolError, LicenseNotFound, SessionExpired

FAKE = Path(__file__).with_name("fake_ipatool.py")
LOGIN_OK = {"name": "Иван Петров", "email": "owner@example", "storefront": "RU", "success": True}


def make_tool(tmp_path, scenario: dict, proxy: str = "") -> Ipatool:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    return Ipatool([sys.executable, str(FAKE)], home, tmp_path / "ipatool.lock", proxy)


def calls(tool: Ipatool) -> list[dict]:
    path = tool.home / "calls.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_login_sends_password_only_via_stdin(tmp_path):
    tool = make_tool(tmp_path, {"auth login": {"code": 0, "out": LOGIN_OK}})
    assert tool.login("owner@example", "S3cret-pw", "123456") == LOGIN_OK
    [call] = calls(tool)
    assert call["stdin"] == "S3cret-pw\n"
    assert not any("S3cret-pw" in a for a in call["args"])
    assert call["args"][:2] == ["--format", "json"]
    assert "--password-stdin" in call["args"] and call["args"][-2:] == ["--auth-code", "123456"]


@pytest.mark.parametrize("code, exc, error", [
    (3, SessionExpired, "session_expired"),
    (4, AuthCodeRequired, "auth_code_required"),
    (5, LicenseNotFound, "license_not_found"),
    (1, IpatoolError, "edge_rejected"),
])
def test_exit_codes_map_to_exceptions(tmp_path, code, exc, error):
    tool = make_tool(tmp_path, {"list-purchases": {"code": code, "out": {"error": error, "message": "текст"}}})
    with pytest.raises(exc) as e:
        tool.list_purchases()
    assert e.value.error == error and e.value.message == "текст"


def test_failure_without_json_reports_exit_code(tmp_path):
    tool = make_tool(tmp_path, {"download": {"code": 2}})
    with pytest.raises(IpatoolError) as e:
        tool.download(1, tmp_path)
    assert type(e.value) is IpatoolError and e.value.error == "error" and "код выхода 2" in e.value.message


def test_timeout_kills_ipatool(tmp_path):
    tool = make_tool(tmp_path, {"list-purchases": {"code": 0, "out": [], "sleep": 10}})
    with pytest.raises(IpatoolError) as e:
        tool.run(["list-purchases"], timeout=0.5)
    assert e.value.error == "timeout"


def test_busy_when_lock_is_held(tmp_path):
    tool = make_tool(tmp_path, {"list-purchases": {"code": 0, "out": []}})
    fd = os.open(tool.lock, os.O_RDWR | os.O_CREAT)
    try:
        assert ipatool.try_lock(fd)
        with pytest.raises(IpatoolError) as e:
            tool.run(["list-purchases"], timeout=5, lock_wait=0)
        assert e.value.error == "busy" and calls(tool) == []
    finally:
        os.close(fd)
    assert tool.list_purchases() == []


def test_proxy_goes_to_https_proxy(tmp_path):
    tool = make_tool(tmp_path, {"list-purchases": {"code": 0, "out": []}}, proxy="socks5h://proxy.example:1080")
    tool.list_purchases()
    assert calls(tool)[0]["https_proxy"] == "socks5h://proxy.example:1080"


def test_download_and_versions_parse_output(tmp_path):
    src = tmp_path / "src.ipa"
    src.write_bytes(b"ipa")
    out = tmp_path / "out"
    out.mkdir()
    tool = make_tool(tmp_path, {
        "download": {"code": 0, "copy": str(src)},
        "list-versions": {"code": 0, "out": {"externalVersionIdentifiers": ["1", "2"],
                                             "latestExternalVersionID": "2", "success": True}}})
    assert tool.download(123, out).read_bytes() == b"ipa"
    assert tool.latest_version_id(123) == "2"
    assert calls(tool)[0]["args"][2:] == ["download", "-i", "123", "-o", str(out)]


def test_list_purchases_rejects_non_list(tmp_path):
    tool = make_tool(tmp_path, {"list-purchases": {"code": 0, "out": {"success": True}}})
    with pytest.raises(IpatoolError) as e:
        tool.list_purchases()
    assert e.value.error == "bad_output"


def test_display_version(tmp_path):
    tool = make_tool(tmp_path, {"get-version-metadata": {"code": 0, "out": {
        "externalVersionID": "847801982", "displayVersion": "12.15.0", "releaseDate": "2022-04-01", "success": True}}})
    assert tool.display_version(492224193, "847801982") == "12.15.0"
    assert calls(tool)[0]["args"][2:] == ["get-version-metadata", "-i", "492224193", "--external-version-id", "847801982"]


def test_device_mac_goes_to_environment(tmp_path):
    tool = make_tool(tmp_path, {"list-purchases": {"code": 0, "out": []}})
    tool.list_purchases()
    tool.device_mac = "00:03:93:12:34:56"
    tool.list_purchases()
    assert [c["device_mac"] for c in calls(tool)] == ["", "00:03:93:12:34:56"]


def test_other_apple_id_does_not_wait(tmp_path):
    a = make_tool(tmp_path / "a", {"list-purchases": {"code": 0, "out": []}})
    b = make_tool(tmp_path / "b", {"list-purchases": {"code": 0, "out": []}})
    with ipatool.hold(a.lock):                       # у Apple ID «a» идёт скачивание
        assert b.list_purchases() == []              # «b» не ждёт
        with pytest.raises(IpatoolError) as e:
            a.run(["list-purchases"], timeout=5, lock_wait=0)
        assert e.value.error == "busy"
    assert a.list_purchases() == []


posix = pytest.mark.skipif(os.name != "posix", reason="flock и группы процессов — как на сервере (Linux)")


@posix
def test_flock_holds_until_released(tmp_path):
    lock = tmp_path / "1.lock"
    with ipatool.hold(lock):
        with pytest.raises(IpatoolError) as e:   # второй open того же файла — своя блокировка, она ждёт первую
            with ipatool.hold(lock, wait=0):
                pass
        assert e.value.error == "busy"
    with ipatool.hold(lock, wait=0):              # отпустили — берётся сразу
        pass


@posix
def test_timeout_kills_ipatool_children_too(tmp_path):
    # ipatool запускается в своей сессии: по таймауту убивается вся группа, а не только он сам
    script = tmp_path / "spawn.py"
    script.write_text("import os, subprocess, sys, time\n"
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                      "open(os.path.join(os.environ['HOME'], 'child.pid'), 'w').write(str(child.pid))\n"
                      "time.sleep(30)\n", encoding="utf-8")
    tool = Ipatool([sys.executable, str(script)], tmp_path, tmp_path / "1.lock")
    with pytest.raises(IpatoolError) as e:
        tool.run(["list-purchases"], timeout=0.8)
    assert e.value.error == "timeout"
    pid = int((tmp_path / "child.pid").read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("потомок ipatool пережил таймаут")


def test_tools_per_account_and_for_new_login(cfg):
    acct = SimpleNamespace(id=7, device_mac="00:03:93:AA:BB:CC")
    tool = ipatool.for_account(cfg, acct)
    assert (tool.home, tool.lock, tool.device_mac) == (cfg.accounts_dir / "7", cfg.locks_dir / "7.lock", acct.device_mac)
    assert tool.argv0 == [str(cfg.ipatool_bin)] and tool.proxy == cfg.ipatool_proxy
    new = ipatool.for_new(cfg, cfg.accounts_dir / ".new-ab12", "00:03:93:01:02:03")
    assert (new.home, new.lock, new.device_mac) == (cfg.accounts_dir / ".new-ab12", cfg.locks_dir / ".new-ab12.lock",
                                                     "00:03:93:01:02:03")
