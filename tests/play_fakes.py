"""Подставной Google Play для конвейера и ночной проверки: тот же интерфейс, что play.PlayClient."""
import shutil
from pathlib import Path

from appshelf.android import play


class FakePlay:
    """apps — что знает Play (по пакету), apks — какие файлы он отдаёт; split — пакеты, которые приходят набором
    файлов; expired — Play не принимает токен. Вызовы — в .calls."""

    def __init__(self, apps: dict[str, play.PlayApp], apks: dict[str, Path] | None = None, split=(),
                 expired: bool = False):
        self.apps, self.apks, self.split, self.expired = dict(apps), dict(apks or {}), set(split), expired
        self.calls: list[tuple] = []

    def _call(self, *what) -> None:
        self.calls.append(what)
        if self.expired:
            raise play.PlayAuthError(play.NOT_CONNECTED)

    def details(self, package: str) -> play.PlayApp:
        self._call("details", package)
        if package not in self.apps:
            raise play.PlayGone(play.GONE)
        return self.apps[package]

    def delivery(self, app: play.PlayApp) -> play.Delivery:
        self._call("delivery", app.package)
        if app.paid and not app.owned:
            raise play.PlayError("платное — не куплено этим аккаунтом")
        return play.Delivery(f"https://play.example/{app.package}", {}, int(app.package in self.split), 0)

    def purchases(self) -> list[play.PlayApp]:
        self._call("purchases")
        return [a for a in self.apps.values() if a.owned]

    def download(self, delivery: play.Delivery, dest: Path) -> None:
        package = delivery.url.rsplit("/", 1)[1]
        self._call("download", package)
        shutil.copyfile(self.apks[package], dest)


class FakePool:
    """play.PlayPool: клиент — FakePlay своего Google-аккаунта; без действующего подключения — PlayAuthError."""

    def __init__(self, by_gid: dict[int, FakePlay]):
        self.by_gid = by_gid

    def client(self, acct) -> FakePlay:
        if acct.play != "ok":
            raise play.PlayAuthError(play.NOT_CONNECTED)
        return self.by_gid[acct.id]
