"""Разбор IPA и manifest.plist для OTA-установки (itms-services), spec §6."""
from __future__ import annotations

import io
import plistlib
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

INFO_RE = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
ICON_RE = re.compile(r"^Payload/[^/]+\.app/AppIcon[^/]*\.png$")
FAMILIES = {1: "iphone", 2: "ipad"}
PLACEHOLDER_RGB = (142, 142, 147)


class IpaError(ValueError):
    pass


@dataclass(frozen=True)
class IpaInfo:
    item_id: int
    bundle_id: str
    version: str               # CFBundleShortVersionString — bundle-version в manifest
    build: str
    title: str                 # CFBundleDisplayName
    min_ios: str
    device_family: str         # "iphone", "ipad", "iphone,ipad"
    external_version_id: str   # iTunesMetadata softwareVersionExternalIdentifier
    built: str = ""            # YYYY-MM-DD — время Info.plist в zip; releaseDate Apple — дата первого выпуска


def read_info(path: Path) -> IpaInfo:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        info_name = next((n for n in names if INFO_RE.match(n)), None)
        if info_name is None:
            raise IpaError("в IPA нет Payload/*.app/Info.plist")
        if "iTunesMetadata.plist" not in names:
            raise IpaError("в IPA нет iTunesMetadata.plist")
        info = plistlib.loads(z.read(info_name))
        meta = plistlib.loads(z.read("iTunesMetadata.plist"))
        y, m, d = z.getinfo(info_name).date_time[:3]
    version = str(info.get("CFBundleShortVersionString") or "")
    if not version:
        raise IpaError("в Info.plist нет CFBundleShortVersionString")
    fam = info.get("UIDeviceFamily", [1])
    fam = [fam] if isinstance(fam, int) else fam
    return IpaInfo(
        item_id=int(meta.get("itemId") or 0),
        bundle_id=str(info.get("CFBundleIdentifier", "")),
        version=version,
        build=str(info.get("CFBundleVersion", "")),
        title=str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or meta.get("itemName") or ""),
        min_ios=str(info.get("MinimumOSVersion", "")),
        device_family=",".join(FAMILIES[f] for f in sorted(set(fam)) if f in FAMILIES) or "iphone",
        external_version_id=str(meta.get("softwareVersionExternalIdentifier", "")),
        built=f"{y:04d}-{m:02d}-{d:02d}" if y > 1980 else "",
    )


def extract_icon(path: Path) -> bytes:
    """PNG иконки: iTunesArtwork → самая большая AppIcon*.png (часто CgBI) → нейтральная заглушка."""
    candidates = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "iTunesArtwork" in names:
            candidates.append(z.read("iTunesArtwork"))
        icons = sorted((n for n in names if ICON_RE.match(n)), key=lambda n: z.getinfo(n).file_size, reverse=True)
        if icons:
            candidates.append(z.read(icons[0]))
    for raw in candidates:
        try:
            return _to_png(raw)
        except Exception:  # битая или незнакомая картинка — следующий источник
            continue
    return placeholder_png()


def placeholder_png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (512, 512), PLACEHOLDER_RGB).save(out, "PNG")
    return out.getvalue()


def manifest(ipa_url: str, icon_url: str, bundle_id: str, version: str, title: str) -> bytes:
    return plistlib.dumps({"items": [{
        "assets": [
            {"kind": "software-package", "url": ipa_url},
            {"kind": "display-image", "url": icon_url},
        ],
        "metadata": {"bundle-identifier": bundle_id, "bundle-version": version, "kind": "software", "title": title},
    }]}, fmt=plistlib.FMT_XML)


def _to_png(raw: bytes) -> bytes:
    if raw[12:16] == b"CgBI":  # первый чанк после сигнатуры PNG
        return _cgbi_to_png(raw)
    img = Image.open(io.BytesIO(raw))
    img.load()
    out = io.BytesIO()
    img.convert("RGBA").save(out, "PNG")
    return out.getvalue()


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)


def _cgbi_to_png(raw: bytes) -> bytes:
    """Apple CgBI → обычный PNG: zlib-заголовок для IDAT, BGRA → RGBA, снять premultiplied alpha."""
    ihdr, idat, pos = b"", b"", 8
    while pos + 8 <= len(raw):
        length, ctype = struct.unpack(">I4s", raw[pos:pos + 8])
        data = raw[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            ihdr = data
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
        pos += 12 + length
    width, height = struct.unpack(">II", ihdr[:8])
    filtered = zlib.decompressobj(-15).decompress(idat)
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(filtered)) + _chunk(b"IEND", b"")
    b, g, r, a = Image.open(io.BytesIO(png)).convert("RGBA").split()  # Pillow видит BGRA как RGBA
    px = bytearray(Image.merge("RGBA", (r, g, b, a)).tobytes())
    for i in range(0, len(px), 4):
        alpha = px[i + 3]
        if 0 < alpha < 255:
            for k in range(3):
                px[i + k] = min(255, px[i + k] * 255 // alpha)
    out = io.BytesIO()
    Image.frombytes("RGBA", (width, height), bytes(px)).save(out, "PNG")
    return out.getvalue()
