"""Запуск bin/ipatool — форк ipatool-cpp с патчами appshelf (spec §5).

Пароль Apple ID — только через stdin (--password-stdin): argv виден любому пользователю в
/proc/<pid>/cmdline. Каждый Apple ID — свой HOME и своя блокировка: его вызовы идут по одному, другие Apple ID
не ждут (spec 2026-10-08 §7)."""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
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


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _acquire(fd: int, wait: float | None) -> bool:
    """wait=None — ждать сколько угодно (обработчик, ночная проверка), иначе не дольше wait секунд."""
    deadline = None if wait is None else time.monotonic() + wait
    while not try_lock(fd):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def gate_path(lock: Path) -> Path:
    """Очередь к замку: её держит ждущий с ограничением (вход, удаление), пока не возьмёт сам замок."""
    return lock.with_suffix(".wait")


@contextmanager
def hold(path: Path, wait: float | None = None):
    """Файловая блокировка на время блока: wait=None — ждать сколько угодно, иначе не дольше wait секунд;
    не дождались — IpatoolError("busy"). Ждущий с ограничением идёт вне очереди: пока он ждёт, фоновые вызовы
    (обработчик, ночная проверка) новых блокировок не берут — проверка справочника из ~480 вызовов подряд отпускает
    замок лишь на мгновение, и вход без очереди не дождался бы её конца."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    gate, got = -1, False
    deadline = None if wait is None else time.monotonic() + wait
    try:
        gate = os.open(gate_path(path), os.O_RDWR | os.O_CREAT, 0o600)
        if deadline is None:
            _acquire(gate, None)  # пропустить вперёд ждущего входа
            _unlock(gate)
            got = _acquire(fd, None)
        elif _acquire(gate, wait):
            try:
                got = _acquire(fd, max(0.0, deadline - time.monotonic()))
            finally:
                _unlock(gate)
        if not got:
            raise IpatoolError("busy", "ipatool этого Apple ID занят: идёт скачивание или проверка")
        yield
    finally:
        if got:
            _unlock(fd)
        os.close(fd)
        if gate >= 0:
            os.close(gate)


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


def _recover_login_cookies(home: Path) -> None:
    """После обрыва входа вернуть cookies прежней сессии до любого вызова ipatool."""
    backup = home / ".ipatool" / "cookies.before-login"
    if backup.exists():
        os.replace(backup, backup.with_name("cookies"))


def recover_logins(home: Path, lock: Path) -> None:
    """После перезапуска web незавершённых шагов уже нет; сохранить прежнюю сессию и убрать их cookies."""
    with hold(lock, 0):
        _recover_login_cookies(home)
        for path in (home / ".ipatool").glob("cookies.login-*"):
            path.unlink(missing_ok=True)


@contextmanager
def _login_cookies(home: Path, pending: Path, resume: bool):
    """Новый вход — с чистыми cookies, шаг 2 — с cookies шага 1; прежняя сессия живёт до успеха."""
    root = home / ".ipatool"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cookies, backup = root / "cookies", root / "cookies.before-login"
    prepared = committed = False
    try:
        if cookies.exists():
            os.replace(cookies, backup)
        prepared = True
        if resume and pending.exists():
            os.replace(pending, cookies)
        else:
            pending.unlink(missing_ok=True)
        # libcurl opens this jar for writing; create it with private permissions.
        fd = os.open(cookies, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            yield
        except AuthCodeRequired:
            cookies.chmod(0o600)
            os.replace(cookies, pending)
            raise
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
        else:
            cookies.chmod(0o600)
            committed = True
    finally:
        if prepared:
            if not committed:
                cookies.unlink(missing_ok=True)
                if backup.exists():
                    os.replace(backup, cookies)
            else:
                backup.unlink(missing_ok=True)
                pending.unlink(missing_ok=True)


@dataclass
class Ipatool:
    argv0: list[str]       # [/var/_sh/appshelf/bin/ipatool]; в тестах [python, fake_ipatool.py]
    home: Path             # HOME Apple ID: учётка и cookies — в $HOME/.ipatool
    lock: Path             # блокировка Apple ID: его вызовы по одному, другие Apple ID не ждут
    proxy: str = ""
    device_mac: str = ""   # IPATOOL_DEVICE_MAC (патч 05); пусто — настоящий MAC сервера
    _login_id: str = field(default_factory=lambda: secrets.token_hex(12), init=False, repr=False)

    @property
    def login_cookies(self) -> Path:
        return self.home / ".ipatool" / f"cookies.login-{self._login_id}"

    def run(self, args: list[str], timeout: float, stdin: str = "", lock_wait: float | None = None):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        if os.name == "nt":  # тесты: python.exe без SYSTEMROOT не запускается
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        if self.proxy:
            env["https_proxy"] = env["HTTPS_PROXY"] = self.proxy
        if self.device_mac:
            env["IPATOOL_DEVICE_MAC"] = self.device_mac
        with hold(self.lock, lock_wait):
            login = args[:2] == ["auth", "login"]
            try:
                _recover_login_cookies(self.home)
                jar = _login_cookies(self.home, self.login_cookies, "--auth-code" in args) if login else nullcontext()
                with jar:
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
                    if error == "error" and message == "account is disabled":
                        error = "account_disabled"
                    raise BY_EXIT.get(proc.returncode, IpatoolError)(
                        error or "error", message or _tail(err) or f"код выхода {proc.returncode}")
            except OSError:
                if login:
                    raise IpatoolError("save_failed", "Не удалось подготовить или сохранить вход") from None
                raise

    def discard_login_cookies(self) -> None:
        """Отменённый/истёкший шаг 2: временные cookies этого входа больше не нужны."""
        try:
            with hold(self.lock, 0):
                _recover_login_cookies(self.home)
                self.login_cookies.unlink(missing_ok=True)
        except (OSError, IpatoolError):
            print("appshelf: временные cookies входа не удалены", file=sys.stderr)

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
