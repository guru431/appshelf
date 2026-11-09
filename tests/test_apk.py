import io

import pytest
from PIL import Image

from apk_factory import cert_sha, drawable_xml, make_apk, png_bytes
from appshelf import apk


def test_reads_long_version_code(tmp_path):
    path = make_apk(tmp_path / "long.apk", version_code=1, version_code_major=1)
    assert apk.read_apk(path).version_code == (1 << 32) | 1
    import zipfile
    with zipfile.ZipFile(path) as z:
        manifest = z.read("AndroidManifest.xml")
    obfuscated = manifest.replace("versionCodeMajor".encode("utf-16-le"), "x".encode("utf-16-le") * 16)
    assert apk._manifest(obfuscated)["manifest"]["versionCodeMajor"] == 1


@pytest.mark.parametrize("utf8", [False, True])
def test_reads_manifest_fields_and_v2_signer(tmp_path, utf8):
    info = apk.read_apk(make_apk(tmp_path / "a.apk", version_code=42, version_name="4.2", cert=b"C1", utf8=utf8,
                                 label="Primer" if utf8 else "Пример", abis=("arm64-v8a", "armeabi-v7a")))
    assert (info.package, info.version_code, info.version_name, info.min_sdk, info.target_sdk, info.label) == \
        ("ru.example.app", 42, "4.2", 26, 34, "Primer" if utf8 else "Пример")
    assert info.signer == cert_sha(b"C1")
    assert info.abis == ("arm64-v8a", "armeabi-v7a")


def test_v3_signer_wins_over_v2(tmp_path):
    info = apk.read_apk(make_apk(tmp_path / "a.apk", scheme="v2+v3", cert=b"OLD", v3_cert=b"NEW"))
    assert info.signer == cert_sha(b"NEW")


def test_v1_only_signer_from_pkcs7(tmp_path):
    info = apk.read_apk(make_apk(tmp_path / "a.apk", scheme="v1", cert=b"V1-CERT"))
    assert info.signer == cert_sha(b"V1-CERT")


def test_resource_label_gives_empty_label(tmp_path):
    assert apk.read_apk(make_apk(tmp_path / "a.apk", label="@0x7f120001")).label == ""


def test_rejects_split_xapk_and_garbage(tmp_path):
    with pytest.raises(apk.ApkError, match="часть набора"):
        apk.read_apk(make_apk(tmp_path / "s.apk", split="config.arm64_v8a"))
    with pytest.raises(apk.ApkError, match="набор"):
        apk.read_apk(make_apk(tmp_path / "r.apk", required_splits=True))
    with pytest.raises(apk.ApkError, match="набор"):
        apk.read_apk(make_apk(tmp_path / "t.apk", split_types="base__abi"))
    xapk = tmp_path / "x.xapk"
    import zipfile
    with zipfile.ZipFile(xapk, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("base.apk", make_apk(tmp_path / "base.apk").read_bytes())
    with pytest.raises(apk.ApkError, match="AndroidManifest"):
        apk.read_apk(xapk)
    (tmp_path / "g.apk").write_bytes(b"not a zip")
    with pytest.raises(apk.ApkError, match="не APK"):
        apk.read_apk(tmp_path / "g.apk")
    with pytest.raises(apk.ApkError, match="подпис"):
        apk.read_apk(make_apk(tmp_path / "u.apk", scheme=None))


def test_broken_manifest_is_apk_error(tmp_path):
    import zipfile
    path = tmp_path / "b.apk"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00\xff\xff\x00\x00garbage")
    with pytest.raises(apk.ApkError, match="манифест"):
        apk.read_apk(path)


def test_icon_largest_launcher_png(tmp_path):
    path = make_apk(tmp_path / "a.apk", files={"res/mipmap-mdpi-v4/ic_launcher.png": png_bytes(48),
                                               "res/mipmap-xxhdpi-v4/ic_launcher.png": png_bytes(144),
                                               "res/drawable-xxxhdpi-v4/ic_launcher_foreground.png": png_bytes(432)})
    icon = apk.extract_icon(path)
    assert Image.open(io.BytesIO(icon)).size == (144, 144) and icon.startswith(b"\x89PNG")
    only_drawable = make_apk(tmp_path / "d.apk", files={"res/drawable-hdpi/ic_launcher_round.png": png_bytes(72)})
    assert Image.open(io.BytesIO(apk.extract_icon(only_drawable))).size == (72, 72)
    assert apk.extract_icon(make_apk(tmp_path / "n.apk", files={"res/a1.png": png_bytes(48)})) is None
    broken = make_apk(tmp_path / "x.apk", files={"res/mipmap-hdpi/ic_launcher.png": b"not png"})
    assert apk.extract_icon(broken) is None


LABEL, ICON = 0x7f0e0001, 0x7f0d0000
SHORT_RES = {LABEL: [("", 0, "Aurora Store"), ("de", 0, "Aurora Laden")],
             ICON: [("", 160, "res/-B.png"), ("", 640, "res/-x.png"), ("", 0xFFFF, "res/-a.xml")]}
SHORT_FILES = {"res/-B.png": png_bytes(48), "res/-x.png": png_bytes(192), "res/-a.xml": b"\x03\x00\x08\x00"}


def test_label_and_icon_from_resources(tmp_path):
    """Название ссылкой @string и сокращённые пути ресурсов (res/-B.png) — как у Aurora Store из F-Droid: по имени
    ic_launcher иконку не найти, название и файл иконки — через resources.arsc."""
    path = make_apk(tmp_path / "a.apk", label=f"@{LABEL:#x}", icon_ref=ICON, resources=SHORT_RES, files=SHORT_FILES)
    assert apk.read_apk(path).label == "" and apk.extract_icon(path) is None
    assert apk.resource_label(path) == "Aurora Store"                       # без языка, а не немецкое
    assert Image.open(io.BytesIO(apk.resource_icon(path))).size == (192, 192)  # самая большая, XML — мимо
    ru = make_apk(tmp_path / "r.apk", label=f"@{LABEL:#x}", resources={LABEL: [("", 0, "Bank"), ("ru", 0, "Банк")]})
    assert apk.resource_label(ru) == "Банк"
    assert apk.resource_icon(ru) is None                                    # android:icon нет


def test_resources_missing_or_broken(tmp_path):
    no_arsc = make_apk(tmp_path / "n.apk", label=f"@{LABEL:#x}", icon_ref=ICON, files=SHORT_FILES)
    assert (apk.resource_label(no_arsc), apk.resource_icon(no_arsc)) == ("", None)
    broken = make_apk(tmp_path / "b.apk", label=f"@{LABEL:#x}", icon_ref=ICON,
                      files={**SHORT_FILES, "resources.arsc": b"\x02\x00\x0c\x00\xff\xff\x00\x00garbage"})
    assert (apk.resource_label(broken), apk.resource_icon(broken)) == ("", None)
    literal = make_apk(tmp_path / "l.apk", label="Литерал", resources=SHORT_RES)
    assert apk.resource_label(literal) == ""                                # не ссылка — read_apk уже дал название


def vector_drawable(color=0xFF29C1E1):
    return ("vector", {"viewportWidth": ("float", 108.0), "viewportHeight": ("float", 108.0)}, [
        ("path", {"fillColor": ("color", color), "pathData": ("str", "M42,42h24v24h-24z")}, [])])


def adaptive_apk(tmp_path, *, foreground=None, resource_values=None):
    background, front = ICON + 1, ICON + 2
    root = ("adaptive-icon", {}, [
        ("background", {"drawable": ("ref", background)}, []),
        ("foreground", {"drawable": ("ref", front)}, [])])
    return make_apk(tmp_path / "adaptive.apk", icon_ref=ICON,
                    resources={ICON: [("", 0, "res/a.xml")], background: [("", 0, "res/b.xml")],
                               front: [("", 0, "res/c.xml")], **(resource_values or {})},
                    files={"res/a.xml": drawable_xml(root),
                           "res/b.xml": drawable_xml(("vector", vector_drawable()[1], [
                               ("path", {"fillColor": ("color", 0xFFFFFFFF),
                                         "pathData": ("str", "M0,0h108v108h-108z")}, [])])),
                           "res/c.xml": foreground or drawable_xml(vector_drawable())})


def test_adaptive_vector_icon_from_obfuscated_resources(tmp_path):
    path = adaptive_apk(tmp_path)
    assert apk.extract_icon(path) is None
    image = Image.open(io.BytesIO(apk.resource_icon(path)))
    assert image.size == (192, 192)
    assert image.getpixel((96, 96)) == (41, 193, 225, 255)
    assert image.getpixel((8, 8)) == (255, 255, 255, 255)


def test_direct_vector_icon_preserves_transforms_and_alpha(tmp_path):
    vector = vector_drawable()
    vector[2][0][1]["fillAlpha"] = ("float", 0.5)
    vector = (vector[0], vector[1], [("group", {"translateX": ("float", 24.0)}, vector[2])])
    path = make_apk(tmp_path / "vector.apk", icon_ref=ICON, resources={ICON: [("", 0, "res/a.xml")]},
                    files={"res/a.xml": drawable_xml(vector)})
    image = Image.open(io.BytesIO(apk.resource_icon(path)))
    assert image.getpixel((96, 96))[3] == 0
    assert image.getpixel((140, 96))[3] in (127, 128)


def test_adaptive_icon_rejects_oversized_or_broken_xml(tmp_path, monkeypatch):
    assert apk.resource_icon(adaptive_apk(tmp_path, foreground=b"bad-xml")) is None
    path = adaptive_apk(tmp_path)
    monkeypatch.setattr(apk, "ICON_MAX", 50)
    assert apk.resource_icon(path) is None


def test_adaptive_icon_resolves_color_and_drawable_alias(tmp_path):
    path = adaptive_apk(tmp_path, resource_values={ICON + 1: [("", 0, 0xFF102030)],
        ICON + 2: [("", 0, ("ref", ICON + 3))], ICON + 3: [("", 0, "res/c.xml")]})
    image = Image.open(io.BytesIO(apk.resource_icon(path)))
    assert image.getpixel((8, 8)) == (16, 32, 48, 255)
    assert image.getpixel((96, 96)) == (41, 193, 225, 255)


@pytest.mark.parametrize("cycle", ["res/a.xml", ("ref", ICON + 2)])
def test_adaptive_icon_rejects_resource_cycles(tmp_path, cycle):
    path = adaptive_apk(tmp_path, resource_values={ICON + 2: [("", 0, cycle)]})
    assert apk.resource_icon(path) is None


def test_huge_icon_entry_skipped(tmp_path, monkeypatch):
    huge = png_bytes(144) + b"\0" * (apk.ICON_MAX + 1)  # годный PNG с хвостом: без предела была бы выбрана она
    path = make_apk(tmp_path / "a.apk", files={"res/mipmap-mdpi/ic_launcher.png": png_bytes(48),
                                               "res/mipmap-xxhdpi/ic_launcher.png": huge})
    import zipfile
    read, names = zipfile.ZipFile.read, []
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda z, name, *a: names.append(name) or read(z, name, *a))
    assert Image.open(io.BytesIO(apk.extract_icon(path))).size == (48, 48)
    assert "res/mipmap-xxhdpi/ic_launcher.png" not in names
    only_huge = make_apk(tmp_path / "h.apk", files={"res/mipmap-xxhdpi/ic_launcher.png": huge})
    assert apk.extract_icon(only_huge) is None


def test_huge_v1_signature_file_refused_unread(tmp_path, monkeypatch):
    """Файл подписи v1 — бомба (сжатые нули): без предела read_apk распаковал бы его целиком в память."""
    path = make_apk(tmp_path / "a.apk", files={"META-INF/A.RSA": b"\0" * (apk.MAX_V1 + 1)})
    import zipfile
    read, names = zipfile.ZipFile.read, []
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda z, name, *a: names.append(name) or read(z, name, *a))
    with pytest.raises(apk.ApkError, match="подпис"):
        apk.read_apk(path)
    assert "META-INF/A.RSA" not in names


def test_string_pool_bomb_refused(tmp_path):
    """Все смещения пула — на одну длинную строку: N×L символов из пула в N×4 + 2L байт роняли appshelf-web."""
    import struct
    import zipfile
    count, length = 200, 100_000
    data = struct.pack("<HH", 0x8000 | length >> 16, length & 0xFFFF) + "я".encode("utf-16-le") * length
    start = 28 + 4 * count
    pool = struct.pack("<HHIIIIII", 0x0001, 28, start + len(data), count, 0, 0, start, 0) + b"\0" * 4 * count + data
    manifest = struct.pack("<HHI", 0x0003, 8, 8 + len(pool)) + pool
    with pytest.raises(ValueError, match="длиннее"):
        apk._manifest(manifest)
    path = tmp_path / "bomb.apk"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml", manifest)
    with pytest.raises(apk.ApkError, match="не разбирается"):
        apk.read_apk(path)


def test_icon_with_too_many_pixels_skipped(tmp_path, monkeypatch):
    """Картинка-бомба (мало байт, много пикселей) не распаковывается: размер — из заголовка, до load()."""
    monkeypatch.setattr(apk, "ICON_PIXELS", 100 * 100)
    path = make_apk(tmp_path / "a.apk", files={"res/mipmap-mdpi/ic_launcher.png": png_bytes(48),
                                               "res/mipmap-xxhdpi/ic_launcher.png": png_bytes(144)})
    assert Image.open(io.BytesIO(apk.extract_icon(path))).size == (48, 48)


def test_android_version():
    assert apk.android_version(26) == "8" and apk.android_version(99) == "API 99"
    assert [apk.android_version(s) for s in (21, 23, 24, 28, 29, 35, 36)] == ["5", "6", "7", "9", "10", "15", "16"]
