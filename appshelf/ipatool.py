"""Запуск bin/ipatool — форк ipatool-cpp с патчами appshelf (spec §5).

Пароль Apple ID — только через stdin (--password-stdin): argv виден любому пользователю в
/proc/<pid>/cmdline. Каждый Apple ID — свой HOME и своя блокировка: его вызовы идут по одному, другие Apple ID
не ждут (spec 2026-10-08 §7)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LOGIN_TIMEOUT = 120.0
HISTORY_TIMEOUT = 120.0
VERSIONS_TIMEOUT = 300.0   # первая генерация kbsync после входа — до минуты
DOWNLOAD_TIMEOUT = 1800.0
LOGIN_LOCK_WAIT = 10.0     # вход не ждёт получасовое скачивание: «занято, попробуйте позже»


class IpatoolError(Exception):
    """error — код JSON-ошибки ipatool (edge_rejected, invalid_credentials…) или наш (busy, timeout, bad_output)."""

    def __init__(self, error: str, message: str):
        super().__init__(f"{error}: {message}")
        self.error, self.message = error, message


class SessionExpired(IpatoolError):
    """Код выхода 3: токен магазина истёк или учётки нет."""


class AuthCodeRequired(IpatoolError):
    """Код выхода 4: Apple ждёт код 2FA."""


class LicenseNotFound(IpatoolError):
    """Код выхода 5: у Apple ID нет лицензии на приложение."""


BY_EXIT = {3: SessionExpired, 4: AuthCodeRequired, 5: LicenseNotFound}


def try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":  # тесты на Windows
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _acquire(fd: int, wait: float | None) -> bool:
    """wait=None — ждать сколько угодно (обработчик, ночная проверка), иначе не дольше wait секунд."""
    deadline = None if wait is None else time.monotonic() + wait
    while not try_lock(fd):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


@contextmanager
def hold(path: Path, wait: float | None = None):
    """Файловая блокировка на время блока: wait=None — ждать сколько угодно, иначе не дольше wait секунд;
    не дождались — IpatoolError("busy")."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _acquire(fd, wait):
            raise IpatoolError("busy", "ipatool этого Apple ID занят: идёт скачивание или проверка")
        yield
    finally:
        os.close(fd)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)  # своя сессия (start_new_session): вместе с потомками
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def _tail(text: str, limit: int = 500) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("PROGRESS:")]
    return "\n".join(lines)[-limit:]


def _last_json(out: str):
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


@dataclass
class Ipatool:
    argv0: list[str]       # [/var/_sh/appshelf/bin/ipatool]; в тестах [python, fake_ipatool.py]
    home: Path             # HOME Apple ID: учётка и cookies — в $HOME/.ipatool
    lock: Path             # блокировка Apple ID: его вызовы по одному, другие Apple ID не ждут
    proxy: str = ""
    device_mac: str = ""   # IPATOOL_DEVICE_MAC (патч 05); пусто — настоящий MAC сервера

    def run(self, args: list[str], timeout: float, stdin: str = "", lock_wait: float | None = None):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        if os.name == "nt":  # тесты: python.exe без SYSTEMROOT не запускается
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        if self.proxy:
            env["https_proxy"] = env["HTTPS_PROXY"] = self.proxy
        if self.device_mac:
            env["IPATOOL_DEVICE_MAC"] = self.device_mac
        with hold(self.lock, lock_wait):
            proc = subprocess.Popen([*self.argv0, "--format", "json", *args], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
                                    encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
            try:
                out, err = proc.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(proc)
                proc.communicate()
                raise IpatoolError("timeout", f"ipatool не уложился в {timeout:.0f} с") from None
        data = _last_json(out)
        if proc.returncode == 0:
            if data is None:
                raise IpatoolError("bad_output", "ipatool не вернул JSON: " + _tail(out + err))
            return data
        error = message = ""
        if isinstance(data, dict):
            error, message = str(data.get("error", "")), str(data.get("message", ""))
        raise BY_EXIT.get(proc.returncode, IpatoolError)(
            error or "error", message or _tail(err) or f"код выхода {proc.returncode}")

    def login(self, email: str, password: str, auth_code: str = "",
              lock_wait: float | None = LOGIN_LOCK_WAIT) -> dict:
        args = ["auth", "login", "-e", email, "--password-stdin"]
        if auth_code:
            args += ["--auth-code", auth_code]
        return self.run(args, LOGIN_TIMEOUT, stdin=password + "\n", lock_wait=lock_wait)

    def list_purchases(self) -> list[dict]:
        data = self.run(["list-purchases"], HISTORY_TIMEOUT)
        if not isinstance(data, list):
            raise IpatoolError("bad_output", "list-purchases вернул не список")
        return data

    def latest_version_id(self, app_id: int) -> str:
        data = self.run(["list-versions", "-i", str(app_id)], VERSIONS_TIMEOUT)
        latest = str(data.get("latestExternalVersionID", "")) if isinstance(data, dict) else ""
        if not latest:
            raise IpatoolError("bad_output", "list-versions без latestExternalVersionID")
        return latest

    def display_version(self, app_id: int, external_id: str) -> str:
        """Версия для людей (CFBundleShortVersionString) по external id — без скачивания IPA."""
        data = self.run(["get-version-metadata", "-i", str(app_id), "--external-version-id", external_id],
                        VERSIONS_TIMEOUT)
        return str(data.get("displayVersion", "")) if isinstance(data, dict) else ""

    def download(self, app_id: int, out_dir: Path) -> Path:
        data = self.run(["download", "-i", str(app_id), "-o", str(out_dir)], DOWNLOAD_TIMEOUT)
        output = data.get("output") if isinstance(data, dict) else None
        if not output:
            raise IpatoolError("bad_output", "download без пути к файлу")
        return Path(output)


def for_account(cfg, acct) -> Ipatool:
    return Ipatool([str(cfg.ipatool_bin)], cfg.accounts_dir / str(acct.id), cfg.locks_dir / f"{acct.id}.lock",
                   cfg.ipatool_proxy, acct.device_mac)


def for_new(cfg, home: Path, device_mac: str) -> Ipatool:
    """Первый вход нового Apple ID — во временный HOME accounts/.new-*: id у него появится после успеха."""
    return Ipatool([str(cfg.ipatool_bin)], home, cfg.locks_dir / f"{home.name}.lock", cfg.ipatool_proxy, device_mac)
