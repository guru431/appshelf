"""Живой RuStore (-m integration): универсальный APK Сбера скачивается и разбирается, подпись — как в сведениях
RuStore. Сеть — машины, где идут тесты (нужен российский адрес); CDN RuStore — с сертификатом НУЦ, файл корней —
APPSHELF_SOURCES_CA из окружения или .env."""
from pathlib import Path

import pytest

from appshelf import apk
from appshelf.android import net, rustore
from appshelf.android.sources import Sources
from test_live import deploy_env

pytestmark = pytest.mark.integration


def test_rustore_sber_apk_live(tmp_path):
    ca = deploy_env().get("APPSHELF_SOURCES_CA")
    src = Sources(net.Http(Path(ca) if ca else None), pause=0.5)
    store_app = rustore.info(src.http, "ru.sberbankmobile")
    got = src.fetch("rustore", "ru.sberbankmobile", tmp_path / "sber.apk")
    info = apk.read_apk(tmp_path / "sber.apk")
    assert info.package == "ru.sberbankmobile" and info.version_code >= store_app.version_code
    assert info.signer == got.expected_signer == store_app.signature
    assert isinstance(info.label, str)
    assert got.icon is None or got.icon.startswith(b"\x89PNG")
