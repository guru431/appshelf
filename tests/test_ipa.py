import io
import plistlib

import pytest
from PIL import Image

from appshelf import ipa
from ipa_factory import cgbi_bytes, jpeg_bytes, make_ipa, png_bytes

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def pixel(png: bytes):
    assert png[:8] == PNG_SIG
    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel((0, 0))


def test_read_info(tmp_path):
    info = ipa.read_info(make_ipa(tmp_path / "a.ipa", families=(1, 2)))
    assert info == ipa.IpaInfo(item_id=123, bundle_id="ru.example.app", version="1.2.3", build="45",
                               title="Пример & Ко", min_ios="15.0", device_family="iphone,ipad",
                               external_version_id="900", built="2026-06-18")


def test_read_info_without_metadata_fails(tmp_path):
    with pytest.raises(ipa.IpaError):
        ipa.read_info(make_ipa(tmp_path / "a.ipa", metadata=False))


def test_manifest_contract_with_ios():
    m = plistlib.loads(ipa.manifest("https://apps.example/d/t/1-2.0/app.ipa", "https://apps.example/d/t/1-2.0/icon.png",
                                    "ru.example.app", "2.0", "Пример & Ко"))
    [item] = m["items"]
    assert item["assets"] == [
        {"kind": "software-package", "url": "https://apps.example/d/t/1-2.0/app.ipa"},
        {"kind": "display-image", "url": "https://apps.example/d/t/1-2.0/icon.png"},
    ]
    assert item["metadata"] == {"bundle-identifier": "ru.example.app", "bundle-version": "2.0",
                                "kind": "software", "title": "Пример & Ко"}


def test_icon_from_itunes_artwork_jpeg(tmp_path):
    r, g, b, a = pixel(ipa.extract_icon(make_ipa(tmp_path / "a.ipa", artwork=jpeg_bytes((0, 0, 255)))))
    assert b > 240 and r < 15 and a == 255


def test_icon_from_cgbi_app_icon(tmp_path):
    assert pixel(ipa.extract_icon(make_ipa(tmp_path / "a.ipa", app_icon=cgbi_bytes((200, 100, 50, 255))))) \
        == (200, 100, 50, 255)


def test_cgbi_alpha_is_unpremultiplied(tmp_path):
    r, g, b, a = pixel(ipa.extract_icon(make_ipa(tmp_path / "a.ipa", app_icon=cgbi_bytes((200, 100, 50, 128)))))
    assert a == 128 and abs(r - 200) <= 3 and abs(g - 100) <= 3 and abs(b - 50) <= 3


def test_icon_falls_back_to_app_icon_then_placeholder(tmp_path):
    png = ipa.extract_icon(make_ipa(tmp_path / "a.ipa", artwork=b"not an image", app_icon=png_bytes((0, 255, 0, 255))))
    assert pixel(png) == (0, 255, 0, 255)
    assert ipa.extract_icon(make_ipa(tmp_path / "b.ipa")) == ipa.placeholder_png()
