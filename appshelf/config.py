"""Настройки из окружения: /etc/appshelf/appshelf.env (EnvironmentFile служб)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

GB = 1024 ** 3
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Config:
    data_dir: Path       # /var/lib/appshelf
    pub_token: str       # 32 hex: имя каталога с IPA и manifest; есть только здесь и в appshelf.env
    public_base: str     # https://apps.example.com — адрес сайта, из него собираются ссылки установки
    ipatool_bin: Path
    ipatool_home: Path   # HOME для ipatool: учётка и cookies — в $HOME/.ipatool
    ipatool_proxy: str   # необязательный https_proxy для ipatool (отказ Apple на edge)
    mail_to: str
    pub_dir: Path | None = None  # архив IPA; может быть сетевой шарой (APPSHELF_PUB), база и tmp — локально

    @property
    def db(self) -> Path:
        return self.data_dir / "appshelf.db"

    @property
    def pub_root(self) -> Path:
        return (self.pub_dir or self.data_dir / "pub") / self.pub_token

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def status_dir(self) -> Path:
        return self.data_dir / "status"

    @property
    def lock(self) -> Path:
        return self.data_dir / "ipatool.lock"

    @property
    def web_auth(self) -> Path:
        return self.data_dir / "web-auth"

    @property
    def cookie_key(self) -> Path:
        return self.data_dir / "cookie-key"

    def public_url(self, dir_name: str, file: str) -> str:
        return f"{self.public_base}/d/{self.pub_token}/{dir_name}/{file}"


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
    )
