"""IPA, собранные в тесте: Info.plist, iTunesMetadata.plist, иконки (обычный PNG, JPEG, Apple CgBI)."""
from __future__ import annotations

import io
import plistlib
import struct
import zipfile
import zlib
from pathlib import Path

from PIL import Image


def png_bytes(rgba=(255, 0, 0, 255), size=(4, 4)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, rgba).save(out, "PNG")
    return out.getvalue()


def jpeg_bytes(rgb=(0, 0, 255), size=(4, 4)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, rgb).save(out, "JPEG", quality=100)
    return out.getvalue()


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)


def cgbi_bytes(rgba=(200, 100, 50, 255), size=(2, 2)) -> bytes:
    """Apple CgBI: пиксели BGRA с premultiplied alpha, IDAT — сырой deflate без zlib-заголовка."""
    w, h = size
    r, g, b, a = rgba
    row = b"\x00" + bytes([b * a // 255, g * a // 255, r * a // 255, a]) * w
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    idat = comp.compress(row * h) + comp.flush()
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"CgBI", b"\x50\x00\x20\x02") + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


def make_ipa(path: Path, *, item_id=123, bundle_id="ru.example.app", version="1.2.3", build="45",
             external_id="900", display_name="Пример & Ко", min_ios="15.0", families=(1,),
             artwork: bytes | None = None, app_icon: bytes | None = None, metadata=True,
             built=(2026, 6, 18, 16, 24, 0)) -> Path:
    info = {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version, "CFBundleVersion": build,
            "CFBundleDisplayName": display_name, "CFBundleName": "Example", "MinimumOSVersion": min_ios,
            "UIDeviceFamily": list(families)}
    meta = {"itemId": item_id, "softwareVersionExternalIdentifier": int(external_id),
            "itemName": display_name, "bundleShortVersionString": version}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("Payload/Example.app/Info.plist", built),  # время в zip — дата сборки
                   plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
        if metadata:
            z.writestr("iTunesMetadata.plist", plistlib.dumps(meta))
        if artwork is not None:
            z.writestr("iTunesArtwork", artwork)
        if app_icon is not None:
            z.writestr("Payload/Example.app/AppIcon60x60@3x.png", app_icon)
    return path
