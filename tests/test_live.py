"""Живые проверки bin/ipatool на сервере (-m integration). Куда ходить — APPSHELF_SSH* из .env, как у
deploy/deploy.sh; нужен вход в Apple ID от appshelf. flock — тот же, что у appshelf-web и ночной проверки."""
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def deploy_env() -> dict:
    """.env в корне проекта (строки KEY=VALUE); переменные окружения важнее."""
    env = {}
    f = Path(__file__).resolve().parent.parent / ".env"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                env[key] = value.strip("\"'")
    return {**env, **os.environ}


ENV = deploy_env()
KEY = ENV.get("APPSHELF_SSH_KEY", "")
SSH = ["ssh", "-o", "BatchMode=yes", "-p", ENV.get("APPSHELF_SSH_PORT", "22"),
       *(["-i", str(Path(KEY).expanduser()), "-o", "IdentitiesOnly=yes"] if KEY else []), ENV.get("APPSHELF_SSH", "")]
BIN = "/var/_sh/appshelf/bin/ipatool"
AS_APPSHELF = f"sudo -u appshelf env HOME=/etc/appshelf flock /var/lib/appshelf/ipatool.lock {BIN} --format json"


def remote(cmd: str) -> subprocess.CompletedProcess:
    if not ENV.get("APPSHELF_SSH"):
        pytest.skip("нет APPSHELF_SSH в .env")
    return subprocess.run([*SSH, cmd], capture_output=True, text=True, encoding="utf-8", timeout=200)


def last_json(out: str):
    return json.loads(out.strip().splitlines()[-1])


def test_kbsync_without_apple():
    r = remote(f"HOME=$(mktemp -d) {BIN} --format json kbsync --dsid 1")
    assert r.returncode == 0 and last_json(r.stdout)["success"] is True


def test_password_not_stored():
    r = remote(f"{AS_APPSHELF} auth info")
    assert r.returncode == 0, r.stdout + r.stderr
    assert last_json(r.stdout)["passwordStored"] is False


def test_list_purchases_live():
    r = remote(f"{AS_APPSHELF} list-purchases")
    assert r.returncode == 0, r.stdout + r.stderr
    apps = last_json(r.stdout)
    assert apps and {"id", "bundleId", "name", "version", "purchaseDate"} <= set(apps[0])
