"""Настройки из окружения: /etc/appshelf/appshelf.env (EnvironmentFile служб)."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path

GB = 1024 ** 3
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Config:
    data_dir: Path       # /var/lib/appshelf
    pub_token: str       # 32 hex: из него — токены каталогов Apple ID (shelf_token); есть только здесь и в appshelf.env
    public_base: str     # https://apps.example.com — адрес сайта, из него собираются ссылки установки
    ipatool_bin: Path
    ipatool_home: Path   # /etc/appshelf: HOME каждого Apple ID — accounts/<id> (учётка и cookies ipatool)
    ipatool_proxy: str   # необязательный https_proxy для ipatool (отказ Apple на edge)
    mail_to: str
    pub_dir: Path | None = None  # архив IPA; может быть сетевой шарой (APPSHELF_PUB), база и tmp — локально
    owner_email: str = ""        # APPSHELF_OWNER: Apple ID владельца, в нижнем регистре

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
        """Каталог Apple ID в архиве — HMAC от PUB_TOKEN: из своего токена чужой не вычислить. Перенесённый из
        версии 1 Apple ID (legacy_pub) остаётся в каталоге <PUB_TOKEN>: выданные ссылки установки работают."""
        if acct.legacy_pub:
            return self.pub_token
        return hmac.new(self.pub_token.encode(), f"account:{acct.id}".encode(), hashlib.sha256).hexdigest()[:32]

    def shelf_root(self, acct) -> Path:
        return self.archive / self.shelf_token(acct)

    def shelf_url(self, acct, dir_name: str, file: str) -> str:
        return f"{self.public_base}/d/{self.shelf_token(acct)}/{dir_name}/{file}"


def from_env(env=None) -> Config:
    env = os.environ if env is None else env
    token = env.get("PUB_TOKEN", "")
    if not TOKEN_RE.match(token):
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
    )
