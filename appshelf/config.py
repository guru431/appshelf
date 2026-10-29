"""Настройки из окружения: /etc/appshelf/appshelf.env (EnvironmentFile служб)."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

GB = 1024 ** 3
TOKEN_RE = re.compile(r"[0-9a-f]{32}")
PROXY_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
HOST_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


@dataclass(frozen=True)
class Config:
    data_dir: Path       # /var/lib/appshelf
    pub_token: str       # 32 hex: каталог полки, перенесённой из версии 1; есть только здесь и в appshelf.env
    public_base: str     # https://apps.example.com — адрес сайта, из него собираются ссылки установки
    ipatool_bin: Path
    ipatool_home: Path   # /etc/appshelf: HOME каждого Apple ID — accounts/<id> (учётка и cookies ipatool)
    ipatool_proxy: str   # необязательный https_proxy для ipatool (отказ Apple на edge)
    mail_to: str
    pub_dir: Path | None = None  # архив IPA; может быть сетевой шарой (APPSHELF_PUB), база и tmp — локально
    owner_email: str = ""        # APPSHELF_OWNER: Apple ID владельца, в нижнем регистре
    backup_dir: Path | None = None  # APPSHELF_BACKUP_DIR: ночные снимки базы (appshelf backup --daily)
    sources_ca: Path | None = None  # APPSHELF_SOURCES_CA: PEM доп. корней (НУЦ) — только для загрузчика источников APK
    google_client_id: str = ""      # GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET: клиент OAuth входа через Google; секрет —
    google_client_secret: str = field(default="", repr=False)  # только в appshelf.env и запросе к token endpoint
    # APPSHELF_PROXIES: имя → http://… или socks5h://… (бывает с паролем) — только для запросов к Google Play
    proxies: dict[str, str] = field(default_factory=dict, repr=False)
    appgallery_host: str = ""    # APPSHELF_APPGALLERY_HOST: хост API AppGallery; "" — android.appgallery.HOST

    @property
    def google_dir(self) -> Path:
        """Мастер-токены Google Play и профили «устройства» — google/<id Google-аккаунта> (spec 2026-10-28 §6.4)."""
        return self.ipatool_home / "google"

    @property
    def google_enabled(self) -> bool:
        """Нет обоих — кнопок Google нет, /google/* — 404."""
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def db(self) -> Path:
        return self.data_dir / "appshelf.db"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def status_dir(self) -> Path:
        return self.data_dir / "status"

    @property
    def cookie_key(self) -> Path:
        return self.data_dir / "cookie-key"

    @property
    def archive(self) -> Path:
        """Корень архива IPA; внутри — каталог на каждый Apple ID (shelf_root)."""
        return self.pub_dir or self.data_dir / "pub"

    @property
    def accounts_dir(self) -> Path:
        return self.ipatool_home / "accounts"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def download_lock(self) -> Path:
        return self.locks_dir / "download.lock"

    def shelf_token(self, acct) -> str:
        """Каталог Apple ID в архиве — его случайный токен (accounts.shelf): ни из своего токена, ни из ссылок
        владельца чужой не вычислить. Перенесённый из версии 1 (legacy_pub) остаётся в каталоге <PUB_TOKEN>:
        выданные ссылки установки работают. Apple ID версии 2, ещё не перенесённый (shelf пуст), — hmac_token."""
        if acct.legacy_pub:
            return self.pub_token
        return acct.shelf or self.hmac_token(acct.id)

    def hmac_token(self, aid: int) -> str:
        """Каталог Apple ID версии 2 — HMAC от PUB_TOKEN: PUB_TOKEN из ссылки полки владельца раскрывал все полки,
        поэтому такие каталоги переезжают на случайные токены (jobs.move_shelves)."""
        return hmac.new(self.pub_token.encode(), f"account:{aid}".encode(), hashlib.sha256).hexdigest()[:32]

    def shelf_root(self, acct) -> Path:
        return self.archive / self.shelf_token(acct)

    def shelf_url(self, acct, dir_name: str, file: str) -> str:
        return f"{self.public_base}/d/{self.shelf_token(acct)}/{dir_name}/{file}"


def from_env(env=None) -> Config:
    env = os.environ if env is None else env
    token = env.get("PUB_TOKEN", "")
    if not TOKEN_RE.fullmatch(token):  # fullmatch, а не match с «$»: «$» совпадает и перед хвостовым «\n»
        raise ValueError("PUB_TOKEN в appshelf.env — 32 символа 0-9a-f")
    base = env.get("APPSHELF_PUBLIC_BASE", "")
    if not base.startswith(("https://", "http://")):
        raise ValueError("APPSHELF_PUBLIC_BASE в appshelf.env — адрес сайта, например https://apps.example.com")
    return Config(
        data_dir=Path(env.get("APPSHELF_DATA", "/var/lib/appshelf")),
        pub_token=token,
        public_base=base.rstrip("/"),
        ipatool_bin=Path(env.get("APPSHELF_IPATOOL", "/var/_sh/appshelf/bin/ipatool")),
        ipatool_home=Path(env.get("APPSHELF_IPATOOL_HOME", "/etc/appshelf")),
        ipatool_proxy=env.get("IPATOOL_PROXY", ""),
        mail_to=env.get("MAIL_TO", ""),
        pub_dir=Path(env["APPSHELF_PUB"]) if env.get("APPSHELF_PUB") else None,
        owner_email=env.get("APPSHELF_OWNER", "").strip().lower(),
        backup_dir=Path(env["APPSHELF_BACKUP_DIR"]) if env.get("APPSHELF_BACKUP_DIR") else None,
        sources_ca=Path(env["APPSHELF_SOURCES_CA"]) if env.get("APPSHELF_SOURCES_CA") else None,
        google_client_id=env.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=env.get("GOOGLE_CLIENT_SECRET", "").strip(),
        proxies=_proxies(env.get("APPSHELF_PROXIES", "")),
        appgallery_host=_host(env.get("APPSHELF_APPGALLERY_HOST", "")),
    )


def _host(text: str) -> str:
    host = text.strip().lower()
    if host and not HOST_RE.fullmatch(host):
        raise ValueError(f"APPSHELF_APPGALLERY_HOST в appshelf.env — только имя хоста, без https:// и пути: «{host}»")
    return host


def _proxies(text: str) -> dict[str, str]:
    """«имя=url имя=url»; url — http://хост:порт или socks5h://[логин:пароль@]хост:порт. В тексте ошибки — только
    имя: в url бывает пароль."""
    proxies: dict[str, str] = {}
    for item in text.split():
        name, _, url = item.partition("=")
        if not PROXY_NAME_RE.fullmatch(name) or name in proxies:
            raise ValueError(f"APPSHELF_PROXIES в appshelf.env — «имя=url» через пробел, имена разные: «{name}»")
        try:
            u = urllib.parse.urlsplit(url)
            ok = u.scheme in ("http", "socks5h") and bool(u.hostname) and u.port is not None
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"APPSHELF_PROXIES в appshelf.env: у «{name}» нужен http://хост:порт или "
                             "socks5h://хост:порт")
        proxies[name] = url
    return proxies
