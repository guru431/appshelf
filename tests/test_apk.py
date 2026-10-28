import io

import pytest
from PIL import Image

from apk_factory import cert_sha, make_apk, png_bytes
from appshelf import apk


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


def test_android_version():
    assert apk.android_version(26) == "8" and apk.android_version(99) == "API 99"
    assert [apk.android_version(s) for s in (21, 23, 24, 28, 29, 35, 36)] == ["5", "6", "7", "9", "10", "15", "16"]
