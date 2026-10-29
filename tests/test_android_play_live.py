"""Живой Google Play на сервере (-m integration): details известного бесплатного пакета подключённым аккаунтом
APPSHELF_PLAY_TEST_GID (id из google_accounts) — от appshelf, с appshelf.env и прокси аккаунта, как у службы."""
import pytest

from test_live import ENV, remote

pytestmark = pytest.mark.integration

PACKAGE = "com.fsck.k9"     # K-9 Mail: бесплатное, Play отдаёт одним APK (живая проверка 2026-10-29)
SCRIPT = ("from appshelf import config, people, store; from appshelf.android import play; cfg = config.from_env(); "
          "c = store.connect(cfg.db); g = people.get_google(c, {gid}); "
          "a = play.PlayClient(cfg, g.id, g.proxy).details('{pkg}'); "
          "print(a.package, a.version_code, int(a.paid), int(a.owned))")


def test_details_of_known_free_app():
    gid = ENV.get("APPSHELF_PLAY_TEST_GID", "")
    if not gid.isdigit():
        pytest.skip("нет APPSHELF_PLAY_TEST_GID в .env — id подключённого к Play Google-аккаунта")
    r = remote("sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; "
               f"/var/_sh/appshelf/.venv/bin/python -c \"{SCRIPT.format(gid=int(gid), pkg=PACKAGE)}\"'")
    assert r.returncode == 0, r.stderr[-2000:]
    package, version_code, paid, owned = r.stdout.split()
    assert (package, paid, owned) == (PACKAGE, "0", "1") and int(version_code) > 0
