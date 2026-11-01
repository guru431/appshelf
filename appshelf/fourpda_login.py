"""Изолированное окно 4PDA: ввод и капчу выполняет человек, сохраняются только cookies.

Playwright работает в одном выделенном потоке. Контексты не пишутся на диск,
ограничены по количеству и живут десять минут. Сеть загрузчика APK остаётся net.Http.
"""
from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from threading import Timer
from urllib.parse import urlsplit

from .android import fourpda
from .android.net import SourceError

TTL = 600
UNAVAILABLE = "Окно входа в 4PDA недоступно. Обратитесь к владельцу сервера или подключите файл сессии."


@dataclass
class Pending:
    context: object
    page: object
    token: str
    expires: float


class BrowserLogin:
    def __init__(self, cfg, *, clock=time.monotonic):
        self.cfg, self.clock = cfg, clock
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="4pda-login")
        self.pending = {}
        self.runtime = self.browser = self.display = None
        self.home = None
        atexit.register(self.close)

    def call(self, uid, token, action, **data):
        try:
            return self.executor.submit(self._call, uid, token, action, data).result(timeout=35)
        except SourceError:
            raise
        except Exception:
            # В исключениях Playwright бывают URL и введённые строки. Они не идут в логи и ответы.
            raise SourceError(UNAVAILABLE) from None

    def _launch(self):
        if self.browser is not None and self.browser.is_connected():
            return
        try:
            from playwright.sync_api import sync_playwright
            executable = os.environ.get("APPSHELF_FOURPDA_BROWSER", "") or shutil.which("chromium") or shutil.which("google-chrome")
            # Браузеру не нужны PUB_TOKEN, пароли SMTP и ключи Google из EnvironmentFile службы.
            names = {"PATH", "HOME", "TMPDIR", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "LANG", "LC_ALL",
                     "SYSTEMROOT", "WINDIR", "COMSPEC", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP",
                     "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"}
            env = {k: v for k, v in os.environ.items() if k.upper() in names}
            if sys.platform == "linux":
                # HOME службы — каталог кода root. Chromium/Crashpad требует свой
                # доступный для записи HOME, даже у непостоянных контекстов.
                self.cfg.tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.home = tempfile.TemporaryDirectory(prefix="fourpda-browser-", dir=self.cfg.tmp_dir)
                env.update(HOME=self.home.name, XDG_CONFIG_HOME=os.path.join(self.home.name, "config"),
                           XDG_CACHE_HOME=os.path.join(self.home.name, "cache"),
                           XDG_DATA_HOME=os.path.join(self.home.name, "data"))
            if sys.platform == "linux" and not env.get("DISPLAY"):
                xvfb = shutil.which("Xvfb")
                if not xvfb:
                    raise SourceError(UNAVAILABLE)
                read_fd, write_fd = os.pipe()
                try:
                    self.display = subprocess.Popen([xvfb, "-displayfd", str(write_fd), "-screen", "0", "1024x900x24", "-nolisten", "tcp"],
                                                    pass_fds=(write_fd,), env=env,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                finally:
                    os.close(write_fd)
                import select
                try:
                    if not select.select([read_fd], [], [], 5)[0]:
                        raise SourceError(UNAVAILABLE)
                    display = os.read(read_fd, 20).decode().strip()
                finally:
                    os.close(read_fd)
                if not display.isdecimal():
                    raise SourceError(UNAVAILABLE)
                env["DISPLAY"] = ":" + display
            if self.runtime is None:
                self.runtime = sync_playwright().start()
            self.browser = self.runtime.chromium.launch(executable_path=executable or None, headless=False, env=env,
                                                       chromium_sandbox=True,
                                                       args=["--disable-gpu", "--disable-dev-shm-usage"], timeout=15000)
        except Exception:
            self._shutdown()
            raise SourceError(UNAVAILABLE) from None

    @staticmethod
    def _route(route):
        u = urlsplit(route.request.url)
        host = u.hostname or ""
        allowed = (host == "4pda.to" or host.endswith(".4pda.to") or host == "challenges.cloudflare.com")
        if u.scheme != "https" or not allowed or u.port not in (None, 443) or route.request.resource_type in ("media", "websocket"):
            route.abort()
        else:
            route.continue_()

    def _call(self, uid, token, action, data):
        self._purge()
        if action == "start":
            self._cancel(uid)
            if len(self.pending) >= 3:
                raise SourceError("Все окна входа заняты — попробуйте через несколько минут")
            self._launch()
            mobile = data.get("mobile") is True
            options = {"viewport": {"width": 390 if mobile else 1000, "height": 800}, "accept_downloads": False,
                       "service_workers": "block", "locale": "ru-RU"}
            if mobile:
                from .android.net import USER_AGENT
                options.update(is_mobile=True, has_touch=True, user_agent=USER_AGENT)
            context = self.browser.new_context(**options)
            try:
                context.route("**/*", self._route)
                context.on("page", lambda p: p.close() if len(context.pages) > 1 else None)
                page = context.new_page()
                page.set_default_timeout(10000)
                page.goto(fourpda.FORUM + "?act=auth", wait_until="domcontentloaded", timeout=20000)
                token = secrets.token_urlsafe(24)
                self.pending[uid] = Pending(context, page, token, self.clock() + TTL)
                timer = Timer(TTL, self._expire_later, args=(uid, token))
                timer.daemon = True
                timer.start()
                return token
            except Exception:
                context.close()
                raise
        if action == "revoke":
            self._cancel(uid)
            return
        pending = self.pending.get(uid)
        if pending is None or not secrets.compare_digest(pending.token, token):
            raise SourceError("Окно входа устарело — откройте его заново")
        page = pending.page
        if action == "frame":
            return page.screenshot(type="jpeg", quality=75, timeout=5000)
        if action == "click":
            x, y = float(data.get("x", -1)), float(data.get("y", -1))
            size = page.viewport_size
            if not 0 <= x < size["width"] or not 0 <= y < size["height"]:
                raise SourceError("Точка вне окна")
            page.mouse.click(x, y)
        elif action == "text":
            text = str(data.get("text", ""))
            if len(text) > 256:
                raise SourceError("Слишком длинный ввод")
            page.keyboard.insert_text(text)
        elif action == "key":
            key = data.get("key")
            if key not in ("Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Control+A"):
                raise SourceError("Неизвестная клавиша")
            page.keyboard.press(key)
        elif action == "scroll":
            page.mouse.wheel(0, max(-700, min(700, int(data.get("delta", 0)))))
        elif action == "finish":
            if urlsplit(page.url).hostname != "4pda.to":
                raise SourceError("Завершите вход на странице 4PDA")
            cookies = {c["name"]: c["value"] for c in pending.context.cookies(fourpda.BASE)
                       if c["name"] in fourpda.COOKIE_NAMES}
            if not cookies.get("member_id") or not cookies.get("pass_hash"):
                raise SourceError("Завершите вход в 4PDA — на странице должно появиться ваше имя")
            raw = json.dumps({"cookies": cookies, "user_agent": page.evaluate("navigator.userAgent")}).encode()
            return fourpda.Sessions.parse(raw)
        elif action == "cancel":
            self._cancel(uid)
        else:
            raise SourceError("Неизвестное действие")

    def _expire(self, uid, token):
        pending = self.pending.get(uid)
        if pending is not None and secrets.compare_digest(pending.token, token):
            self._cancel(uid)

    def _expire_later(self, uid, token):
        with suppress(RuntimeError):  # приложение уже остановилось
            self.executor.submit(self._expire, uid, token)

    def _purge(self):
        for uid, item in list(self.pending.items()):
            if self.clock() >= item.expires:
                self._cancel(uid)

    def _cancel(self, uid):
        pending = self.pending.pop(uid, None)
        if pending:
            pending.context.close()

    def _shutdown(self):
        for uid in list(self.pending):
            with suppress(Exception):
                self._cancel(uid)
        if self.browser:
            with suppress(Exception):
                self.browser.close()
            self.browser = None
        if self.runtime:
            with suppress(Exception):
                self.runtime.stop()
            self.runtime = None
        if self.display:
            self.display.terminate()
            with suppress(Exception):
                self.display.wait(timeout=3)
            self.display = None
        if self.home is not None:
            self.home.cleanup()
            self.home = None

    def close(self):
        if self.runtime is None and self.browser is None and self.display is None and self.home is None and not self.pending:
            self.executor.shutdown(wait=False, cancel_futures=True)
            return
        with suppress(Exception):
            self.executor.submit(self._shutdown).result(timeout=10)
        self.executor.shutdown(wait=False, cancel_futures=True)
