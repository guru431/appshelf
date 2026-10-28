"""Разбор APK (spec 2026-10-28 §7): двоичный манифест, сертификат подписи, ABI, иконка — без JVM и aapt.

Манифест — AXML (AOSP ResourceTypes.h): пул строк UTF-8/UTF-16, resource map, узлы start/end element; атрибуты
android:* опознаются по id из resource map (имена в обфусцированных APK бывают пустыми), остальные — по имени.
Подпись — блок «APK Sig Block 42» перед central directory (v3 раньше v2), иначе v1 — PKCS#7 из META-INF. Отпечаток —
SHA-256 DER первого сертификата, как поле signature у RuStore."""
from __future__ import annotations

import hashlib
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ATTRS = {0x01010001: "label", 0x0101020c: "minSdkVersion", 0x0101021b: "versionCode", 0x0101021c: "versionName",
         0x01010270: "targetSdkVersion"}
ELEMENTS = ("manifest", "uses-sdk", "application")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")  # имя пакета идёт в имена каталога и файла
V1_RE = re.compile(r"^META-INF/[^/]+\.(RSA|DSA|EC)$", re.IGNORECASE)
MIPMAP_RE = re.compile(r"^res/mipmap-[^/]+/ic_launcher\.(png|webp)$")
DRAWABLE_RE = re.compile(r"^res/drawable-[^/]+/ic_launcher[^/]*\.(png|webp)$")
MAGIC = b"APK Sig Block 42"
V2_ID, V3_ID = 0x7109871a, 0xf05368c0
MAX_MANIFEST = 16 << 20
ICON_MAX = 8 << 20  # «иконка» больше — не читается: zip-бомба съела бы память appshelf-web
MAX_SIG_BLOCK = 64 << 20
VERSIONS = {21: "5", 22: "5.1", 23: "6", 24: "7", 25: "7.1", 26: "8", 27: "8.1", 28: "9", 29: "10", 30: "11",
            31: "12", 32: "12L", 33: "13", 34: "14", 35: "15", 36: "16"}


class ApkError(ValueError):
    """Текст — для человека: почему файл не встанет на полку."""


@dataclass(frozen=True)
class ApkInfo:
    package: str
    version_code: int
    version_name: str
    min_sdk: int
    target_sdk: int
    label: str                 # "" — в манифесте ссылка на ресурс: название берут из источника или пакета
    abis: tuple[str, ...]      # каталоги lib/<abi>/; пусто — без нативного кода
    signer: str                # SHA-256 DER сертификата, 64 hex


def read_apk(path: Path) -> ApkInfo:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "AndroidManifest.xml" not in names:
                raise ApkError("в архиве нет AndroidManifest.xml — это не APK (наборы XAPK, APKS, APKM не подходят)")
            if z.getinfo("AndroidManifest.xml").file_size > MAX_MANIFEST:
                raise ApkError("манифест APK слишком большой")
            manifest = z.read("AndroidManifest.xml")
            v1 = sorted(n for n in names if V1_RE.match(n))
            pkcs7 = z.read(v1[0]) if v1 else b""
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError):
        raise ApkError("файл не APK: это не zip-архив или он повреждён") from None
    try:
        tags = _manifest(manifest)
    except (struct.error, IndexError, KeyError, ValueError, UnicodeDecodeError):
        raise ApkError("манифест APK не разбирается") from None
    m, sdk, app = (tags.get(t, {}) for t in ELEMENTS)
    if m.get("split") or _true(m.get("isFeatureSplit")):
        raise ApkError("это часть набора split APK, а не целое приложение — браузер ставит только целый APK")
    if m.get("requiredSplitTypes") or _true(app.get("isSplitRequired")):
        raise ApkError("приложению нужен набор файлов (split APK) — одним файлом из браузера не ставится")
    package = m.get("package")
    if not isinstance(package, str) or not PACKAGE_RE.match(package):
        raise ApkError("в манифесте нет годного имени пакета")
    code = m.get("versionCode")
    if not isinstance(code, int) or isinstance(code, bool):
        raise ApkError("в манифесте нет versionCode")
    min_sdk = _int(sdk.get("minSdkVersion"), 1)
    try:
        signer = _signer(path, pkcs7)
    except (struct.error, IndexError, ValueError):
        raise ApkError("подпись APK не разбирается") from None
    if signer is None:
        raise ApkError("APK без подписи — Android его не поставит")
    label, name = app.get("label"), m.get("versionName")
    return ApkInfo(
        package=package,
        version_code=code,
        version_name=name[:100] if isinstance(name, str) else "",
        min_sdk=min_sdk,
        target_sdk=_int(sdk.get("targetSdkVersion"), min_sdk),
        label=label.strip()[:200] if isinstance(label, str) else "",
        abis=tuple(sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2} - {""})),
        signer=signer,
    )


def extract_icon(path: Path) -> bytes | None:
    """PNG самой большой ic_launcher из res/mipmap-*, иначе из res/drawable-*; нет или не читается — None."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for pattern in (MIPMAP_RE, DRAWABLE_RE):
            best = None
            for name in (n for n in names if pattern.match(n)):
                if z.getinfo(name).file_size > ICON_MAX:
                    continue
                try:
                    img = Image.open(io.BytesIO(z.read(name)))
                    img.load()
                except Exception:  # битая или незнакомая картинка — следующая
                    continue
                if best is None or img.width * img.height > best.width * best.height:
                    best = img
            if best is not None:
                out = io.BytesIO()
                best.convert("RGBA").save(out, "PNG")
                return out.getvalue()
    return None


def android_version(sdk: int) -> str:
    return VERSIONS.get(sdk, f"API {sdk}")


# --- AXML --------------------------------------------------------------------

def _true(v) -> bool:
    return v is True or v == "true"


def _int(v, default: int) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return default  # нет атрибута, ссылка на ресурс или кодовое имя предварительной версии


def _manifest(data: bytes) -> dict[str, dict]:
    """Атрибуты первых <manifest>, <uses-sdk>, <application>: {тег: {имя: str | int | bool | None}}."""
    typ, header, _ = struct.unpack_from("<HHI", data, 0)
    if typ != 0x0003:
        raise ValueError("не AXML")
    strings: list[str] = []
    resmap: tuple[int, ...] = ()
    out: dict[str, dict] = {}
    pos = header
    while pos + 8 <= len(data):
        ctype, chead, size = struct.unpack_from("<HHI", data, pos)
        if size < 8:
            raise ValueError("пустой узел")
        if ctype == 0x0001:
            strings = _strings(data[pos:pos + size])
        elif ctype == 0x0180:
            resmap = struct.unpack_from(f"<{(size - 8) // 4}I", data, pos + 8)
        elif ctype == 0x0102:
            ext = pos + chead
            _, name, start, step, count = struct.unpack_from("<IIHHH", data, ext)
            tag = strings[name]
            if tag in ELEMENTS and tag not in out:
                attrs = {}
                for i in range(count):
                    _, aname, raw, _, _, dtype, value = struct.unpack_from("<IIIHBBI", data, ext + start + i * step)
                    key = ATTRS.get(resmap[aname]) if aname < len(resmap) else None
                    attrs[key or strings[aname]] = _value(strings, raw, dtype, value)
                out[tag] = attrs
        pos += size
    return out


def _value(strings: list[str], raw: int, dtype: int, value: int):
    if dtype == 0x03:
        return strings[value]
    if dtype == 0x12:
        return value != 0
    if 0x10 <= dtype <= 0x1f:
        return value
    if raw != 0xFFFFFFFF:
        return strings[raw]
    return None  # ссылка на ресурс (@0x7f…), атрибут темы, число с плавающей точкой


def _strings(chunk: bytes) -> list[str]:
    _, header, _, count, _, flags, start, _ = struct.unpack_from("<HHIIIIII", chunk, 0)
    out = []
    for off in struct.unpack_from(f"<{count}I", chunk, header):
        p = start + off
        if flags & 0x100:  # UTF-8: длина в символах, затем в байтах
            _, p = _len8(chunk, p)
            n, p = _len8(chunk, p)
            out.append(chunk[p:p + n].decode("utf-8", "replace"))
        else:
            n, p = _len16(chunk, p)
            out.append(chunk[p:p + 2 * n].decode("utf-16-le", "replace"))
    return out


def _len8(b: bytes, p: int) -> tuple[int, int]:
    n = b[p]
    return (((n & 0x7F) << 8) | b[p + 1], p + 2) if n & 0x80 else (n, p + 1)


def _len16(b: bytes, p: int) -> tuple[int, int]:
    (n,) = struct.unpack_from("<H", b, p)
    if n & 0x8000:
        (low,) = struct.unpack_from("<H", b, p + 2)
        return ((n & 0x7FFF) << 16) | low, p + 4
    return n, p + 2


# --- подпись -----------------------------------------------------------------

def _signer(path: Path, pkcs7: bytes) -> str | None:
    with open(path, "rb") as f:
        cert = _block_cert(f)
    if cert is None and pkcs7:
        cert = _pkcs7_cert(pkcs7)
    return None if cert is None else hashlib.sha256(cert).hexdigest()


def _block_cert(f) -> bytes | None:
    """Первый сертификат первого подписанта v3, иначе v2; блока подписи нет — None."""
    size = f.seek(0, 2)
    tail_len = min(size, 0xFFFF + 22)
    f.seek(size - tail_len)
    tail = f.read()
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        return None
    (cd,) = struct.unpack_from("<I", tail, eocd + 16)
    if cd < 32 or cd > size:
        return None
    f.seek(cd - 24)
    foot = f.read(24)
    if foot[8:] != MAGIC:
        return None
    (block_size,) = struct.unpack_from("<Q", foot)
    if block_size > min(cd - 8, MAX_SIG_BLOCK):
        raise ValueError("размер блока подписи")
    f.seek(cd - block_size - 8)
    block = f.read(block_size + 8)
    if struct.unpack_from("<Q", block)[0] != block_size:
        raise ValueError("размеры блока подписи не сходятся")
    pairs: dict[int, bytes] = {}
    p, end = 8, len(block) - 24
    while p + 12 <= end:
        length, pair_id = struct.unpack_from("<QI", block, p)
        if length < 4 or p + 8 + length > end:
            raise ValueError("пара блока подписи")
        pairs.setdefault(pair_id, block[p + 12:p + 8 + length])
        p += 8 + length
    for pair_id in (V3_ID, V2_ID):
        if pair_id in pairs:
            signers, _ = _lp(pairs[pair_id], 0)
            signer, _ = _lp(signers, 0)
            signed, _ = _lp(signer, 0)
            _, p = _lp(signed, 0)        # дайджесты
            certs, _ = _lp(signed, p)
            cert, _ = _lp(certs, 0)
            return cert
    return None


def _lp(buf: bytes, pos: int) -> tuple[bytes, int]:
    """Значение с длиной uint32 LE впереди."""
    (n,) = struct.unpack_from("<I", buf, pos)
    if pos + 4 + n > len(buf):
        raise ValueError("длина за пределами")
    return buf[pos + 4:pos + 4 + n], pos + 4 + n


def _tlv(buf: bytes, pos: int) -> tuple[int, int, int]:
    """DER: (тег, начало содержимого, конец). Неопределённая длина BER — ошибка."""
    tag, n = buf[pos], buf[pos + 1]
    p = pos + 2
    if n & 0x80:
        k = n & 0x7F
        if not 1 <= k <= 4:
            raise ValueError("длина DER")
        n = int.from_bytes(buf[p:p + k], "big")
        p += k
    if p + n > len(buf):
        raise ValueError("длина DER за пределами")
    return tag, p, p + n


def _pkcs7_cert(der: bytes) -> bytes | None:
    """ContentInfo → [0] → SignedData → certificates [0] → первый сертификат (DER целиком)."""
    _, start, end = _tlv(der, 0)
    _, start, end = _tlv(der, _tlv(der, start)[2])     # за OID — [0] EXPLICIT
    _, pos, end = _tlv(der, start)                       # SignedData
    while pos < end:
        tag, content, nxt = _tlv(der, pos)
        if tag == 0xA0:
            return der[content:_tlv(der, content)[2]] if content < nxt else None
        pos = nxt
    return None
