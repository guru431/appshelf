"""APK, собранные в тесте: двоичный манифест (AXML), блок подписи v2/v3, PKCS#7 для v1, lib/<abi>/, иконки.

Сертификат — произвольные байты в обёртке DER SEQUENCE: разбор хэширует DER целиком, как настоящий X.509."""
from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from pathlib import Path

from PIL import Image

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ATTR_IDS = {"label": 0x01010001, "icon": 0x01010002, "minSdkVersion": 0x0101020c, "versionCode": 0x0101021b,
            "versionName": 0x0101021c, "targetSdkVersion": 0x01010270}
T_REF, T_STRING, T_INT, T_BOOL = 0x01, 0x03, 0x10, 0x12
V2_ID, V3_ID = 0x7109871a, 0xf05368c0


def png_bytes(size: int, rgba=(0, 128, 0, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (size, size), rgba).save(out, "PNG")
    return out.getvalue()


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def der(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def der_cert(cert: bytes) -> bytes:
    return der(0x30, cert)


def cert_sha(cert: bytes) -> str:
    return hashlib.sha256(der_cert(cert)).hexdigest()


# --- AXML --------------------------------------------------------------------

def _string_pool(strings: list[str], utf8: bool) -> bytes:
    offsets, data = [], b""
    for s in strings:
        offsets.append(len(data))
        if utf8:
            raw = s.encode("utf-8")
            assert len(s) < 0x80 and len(raw) < 0x80
            data += bytes([len(s), len(raw)]) + raw + b"\x00"
        else:
            data += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\x00\x00"
    data += b"\x00" * (-len(data) % 4)
    header_size = 28
    start = header_size + 4 * len(strings)
    body = b"".join(struct.pack("<I", o) for o in offsets) + data
    return struct.pack("<HHIIIIII", 0x0001, header_size, header_size + len(body), len(strings), 0,
                       0x100 if utf8 else 0, start, 0) + body


class _Pool:
    """Строки атрибутов android:* с id — первыми (индекс строки ↔ resource map): их регистрируют до прочих."""

    def __init__(self):
        self.ids: list[int] = []
        self.strings: list[str] = []

    def s(self, value: str, res_id: int = 0) -> int:
        if res_id:
            if res_id not in self.ids:
                assert len(self.ids) == len(self.strings), "имена с id — до прочих строк"
                self.ids.append(res_id)
                self.strings.append(value)
            return self.ids.index(res_id)
        if value not in self.strings[len(self.ids):]:
            self.strings.append(value)
        return self.strings.index(value, len(self.ids))


def _axml(manifest_attrs, children, utf8: bool) -> bytes:
    """manifest_attrs и атрибуты детей: (ns_android: bool, name, kind, value)."""
    ax = _Pool()
    for attrs in [manifest_attrs] + [a for _, a in children]:
        for ns, name, _, _ in attrs:
            if ns and name in ATTR_IDS:
                ax.s(name, ATTR_IDS[name])

    def node(type_, ext: bytes) -> bytes:
        return struct.pack("<HHIII", type_, 16, 16 + len(ext), 1, 0xFFFFFFFF) + ext

    def attr(ns, name, kind, value) -> bytes:
        ns_i = ax.s(ANDROID_NS) if ns else 0xFFFFFFFF
        name_i = ax.s(name, ATTR_IDS.get(name, 0) if ns else 0)
        if kind == "str":
            raw = ax.s(value)
            return struct.pack("<IIIHBBI", ns_i, name_i, raw, 8, 0, T_STRING, raw)
        typ, data = {"int": (T_INT, value), "bool": (T_BOOL, 0xFFFFFFFF if value else 0),
                     "ref": (T_REF, value)}[kind]
        return struct.pack("<IIIHBBI", ns_i, name_i, 0xFFFFFFFF, 8, 0, typ, data)

    def element(name, attrs) -> tuple[bytes, bytes]:
        body = b"".join(attr(*a) for a in attrs)
        ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, ax.s(name), 20, 20, len(attrs), 0, 0, 0) + body
        return node(0x0102, ext), node(0x0103, struct.pack("<II", 0xFFFFFFFF, ax.s(name)))

    prefix, uri = ax.s("android"), ax.s(ANDROID_NS)
    start, end = element("manifest", manifest_attrs)
    nodes = [node(0x0100, struct.pack("<II", prefix, uri)), start]
    for name, attrs in children:
        nodes += element(name, attrs)
    nodes += [end, node(0x0101, struct.pack("<II", prefix, uri))]
    pool = _string_pool(ax.strings, utf8)
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(ax.ids)) + b"".join(struct.pack("<I", i) for i in ax.ids)
    body = pool + resmap + b"".join(nodes)
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


# --- resources.arsc ----------------------------------------------------------

def arsc(resources: dict[int, list[tuple[str, int, str]]]) -> bytes:
    """Таблица ресурсов пакета 0x7f: {id: [(язык, плотность, строка)]} — строковые значения (название, путь к файлу)
    по конфигурациям; один чанк типа на (тип, язык, плотность), смещения записей — u32."""
    strings = sorted({s for values in resources.values() for _, _, s in values})
    chunks = {}
    for res_id, values in resources.items():
        for lang, density, s in values:
            chunks.setdefault(((res_id >> 16) & 0xFF, lang, density), {})[res_id & 0xFFFF] = strings.index(s)
    body = _string_pool([], False) + _string_pool([], False)  # имена типов и ключей — разбору не нужны
    for (type_id, lang, density), entries in sorted(chunks.items()):
        count = max(entries) + 1
        config = struct.pack("<IHH2s2sBBH", 64, 0, 0, lang.encode().ljust(2, b"\0"), b"\0\0", 0, 0, density)
        config = config.ljust(64, b"\0")
        offsets, data = [], b""
        for i in range(count):
            if i in entries:
                offsets.append(len(data))
                data += struct.pack("<HHI", 8, 0, 0) + struct.pack("<HBBI", 8, 0, T_STRING, entries[i])
            else:
                offsets.append(0xFFFFFFFF)
        header = 20 + len(config)
        start = header + 4 * count
        body += struct.pack("<HHIBBHII", 0x0201, header, start + len(data), type_id, 0, 0, count, start) + config \
            + b"".join(struct.pack("<I", o) for o in offsets) + data
    pkg_header = 288
    package = struct.pack("<HHII", 0x0200, pkg_header, pkg_header + len(body), 0x7f) + "x".encode("utf-16-le").ljust(256, b"\0") \
        + struct.pack("<IIIII", 0, 0, 0, 0, 0) + body
    table = _string_pool(strings, False) + package
    return struct.pack("<HHII", 0x0002, 12, 12 + len(table), 1) + table


# --- подпись -----------------------------------------------------------------

def _lp(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _signer(cert: bytes, v3: bool) -> bytes:
    signed = _lp(b"") + _lp(_lp(der_cert(cert))) + _lp(b"")
    if v3:
        signed += struct.pack("<II", 24, 0x7FFFFFFF)
        return _lp(signed) + struct.pack("<II", 24, 0x7FFFFFFF) + _lp(b"") + _lp(b"PUBKEY")
    return _lp(signed) + _lp(b"") + _lp(b"PUBKEY")


def _sig_block(pairs: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(struct.pack("<QI", len(v) + 4, i) + v for i, v in pairs)
    size = len(body) + 8 + 16
    return struct.pack("<Q", size) + body + struct.pack("<Q", size) + b"APK Sig Block 42"


def _insert_block(raw: bytes, block: bytes) -> bytes:
    eocd = raw.rfind(b"PK\x05\x06")
    cd = struct.unpack("<I", raw[eocd + 16:eocd + 20])[0]
    tail = raw[eocd:eocd + 16] + struct.pack("<I", cd + len(block)) + raw[eocd + 20:]
    return raw[:cd] + block + raw[cd:eocd] + tail


def pkcs7(cert: bytes) -> bytes:
    """ContentInfo { signedData, [0] SignedData { version, digestAlgorithms, contentInfo, [0] certificates, signerInfos } }"""
    oid = der(0x06, bytes.fromhex("2a864886f70d010702"))
    signed = der(0x30, der(0x02, b"\x01") + der(0x31, b"") + der(0x30, der(0x06, bytes.fromhex("2a864886f70d010701")))
                 + der(0xA0, der_cert(cert)) + der(0x31, b""))
    return der(0x30, oid + der(0xA0, signed))


def make_apk(path: Path, *, package="ru.example.app", version_code=1, version_name="1.0", min_sdk=26, target_sdk=34,
             label="Пример", cert=b"CERT-A", scheme="v2", v3_cert: bytes | None = None, split=None,
             required_splits=False, split_types="", abis=(), icon: bytes | None = None,
             files: dict[str, bytes] | None = None, utf8=False, icon_ref: int | None = None,
             resources: dict[int, list[tuple[str, int, str]]] | None = None) -> Path:
    """scheme: "v1", "v2", "v3", "v2+v3" или None (без подписи); label "@0x…" — ссылка на ресурс; icon_ref —
    android:icon ссылкой; resources — resources.arsc (см. arsc)."""
    manifest = [(False, "package", "str", package), (True, "versionCode", "int", version_code),
                (True, "versionName", "str", version_name)]
    if split:
        manifest.append((False, "split", "str", split))
    if split_types:
        manifest.append((True, "requiredSplitTypes", "str", split_types))
    app = [(True, "label", "ref", int(label[1:], 16)) if label.startswith("@") else (True, "label", "str", label)]
    if icon_ref is not None:
        app.append((True, "icon", "ref", icon_ref))
    if required_splits:
        app.append((True, "isSplitRequired", "bool", True))
    children = [("uses-sdk", [(True, "minSdkVersion", "int", min_sdk), (True, "targetSdkVersion", "int", target_sdk)]),
                ("application", app)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml", _axml(manifest, children, utf8))
        z.writestr("classes.dex", b"dex\n035\x00")
        for abi in abis:
            z.writestr(f"lib/{abi}/libexample.so", b"\x7fELF")
        if icon is not None:
            z.writestr("res/mipmap-xxhdpi-v4/ic_launcher.png", icon)
        for name, data in (files or {}).items():
            z.writestr(name, data)
        if resources is not None:
            z.writestr("resources.arsc", arsc(resources))
        if scheme == "v1":
            z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\n")
            z.writestr("META-INF/CERT.SF", b"Signature-Version: 1.0\r\n")
            z.writestr("META-INF/CERT.RSA", pkcs7(cert))
    raw = buf.getvalue()
    pairs = []
    if scheme in ("v2", "v2+v3"):
        pairs.append((V2_ID, _lp(_lp(_signer(cert, False)))))
    if scheme in ("v3", "v2+v3"):
        pairs.append((V3_ID, _lp(_lp(_signer(v3_cert or cert, True)))))
    if pairs:
        raw = _insert_block(raw, _sig_block(pairs))
    path.write_bytes(raw)
    return path
