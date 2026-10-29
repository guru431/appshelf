from pathlib import Path

import pytest

from android_fakes import FakeHttp
from apk_factory import make_apk
from appshelf import apk
from appshelf.android import db, fdroid, net, sources

DATA = Path(__file__).parent / "data" / "fdroid"
VLC = "org.videolan.vlc"
JSON = {"content-type": "application/json; charset=utf-8"}


def api(name: str = "package.json", status: int = 200) -> tuple[int, dict, bytes]:
    return status, JSON, (DATA / name).read_bytes() if status == 200 else b'{"error":"NOT_FOUND"}'


def repo(tmp_path, builds: dict[int, tuple]) -> dict[str, bytes]:
    """Сборки VLC 3.7.2 по versionCode: {код: ABI} → {ссылка в репозитории: байты APK}."""
    return {fdroid.REPO + f"{VLC}_{code}.apk": make_apk(tmp_path / f"{code}.apk", package=VLC, version_code=code,
                                                         version_name="3.7.2", abis=abis).read_bytes()
            for code, abis in builds.items()}


def test_fdroid_info_fixture():
    http = FakeHttp(raw={fdroid.API + VLC: api()})
    pkg = fdroid.info(http, VLC)
    assert (pkg.package, pkg.suggested) == (VLC, 13070208)
    assert pkg.versions[:2] == (("3.7.2", 13070208), ("3.7.2", 13070207))
    assert fdroid.candidates(pkg) == [13070208, 13070207, 13070206, 13070205]
    own = fdroid.info(FakeHttp(raw={fdroid.API + "org.fdroid.fdroid": api("package_fdroid.json")}), "org.fdroid.fdroid")
    assert fdroid.candidates(own) == [1023052]                     # рекомендуемая 1.23.2, не новейшая 2.0.2


def test_fdroid_picks_universal_or_arm64(tmp_path):
    (tmp_path / "out").mkdir()
    http = FakeHttp(raw={fdroid.API + VLC: api()}, files=repo(tmp_path, {
        13070208: ("x86_64",), 13070207: ("x86",), 13070206: ("arm64-v8a",), 13070205: ("armeabi-v7a",)}))
    got = sources.Sources(http, sleep=lambda s: None).fetch("fdroid", VLC, tmp_path / "out" / "a.apk")
    assert apk.read_apk(tmp_path / "out" / "a.apk").abis == ("arm64-v8a",)
    assert [c[1] for c in http.calls if c[0] == "download"] == [fdroid.REPO + f"{VLC}_{c}.apk"
                                                              for c in (13070208, 13070207, 13070206)]
    assert (got.expected_signer, got.etag) == ("", "fd:13070208")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["a.apk"]          # кандидаты не остались
    universal = FakeHttp(raw={fdroid.API + VLC: api()}, files=repo(tmp_path, {13070208: ()}))
    sources.Sources(universal, sleep=lambda s: None).fetch("fdroid", VLC, tmp_path / "out" / "b.apk")
    assert [c[0] for c in universal.calls].count("download") == 1


def test_fdroid_armeabi_when_no_arm64(tmp_path):
    (tmp_path / "out").mkdir()
    http = FakeHttp(raw={fdroid.API + VLC: api()}, files=repo(tmp_path, {
        13070208: ("x86_64",), 13070207: ("armeabi-v7a",), 13070206: ("x86",), 13070205: ("mips",)}))
    sources.Sources(http, sleep=lambda s: None).fetch("fdroid", VLC, tmp_path / "out" / "a.apk")
    assert apk.read_apk(tmp_path / "out" / "a.apk").abis == ("armeabi-v7a",)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["a.apk"]


def test_fdroid_no_arm_build_error(tmp_path):
    (tmp_path / "out").mkdir()
    http = FakeHttp(raw={fdroid.API + VLC: api()}, files=repo(tmp_path, {
        13070208: ("x86_64",), 13070207: ("x86",), 13070206: ("x86",), 13070205: ("x86_64",)}))
    with pytest.raises(net.SourceError, match="в F-Droid нет сборки для ARM"):
        sources.Sources(http, sleep=lambda s: None).fetch("fdroid", VLC, tmp_path / "out" / "a.apk")
    assert list((tmp_path / "out").iterdir()) == []


def test_fdroid_missing_package(tmp_path):
    http = FakeHttp(raw={fdroid.API + "org.example.none": api(status=404)})
    with pytest.raises(net.SourceError, match="нет в F-Droid"):
        sources.Sources(http, sleep=lambda s: None).latest("fdroid", "org.example.none", "")
    http = FakeHttp(raw={fdroid.API + VLC: (503, {}, b"")})
    with pytest.raises(net.FetchError, match="503"):
        fdroid.info(http, VLC)


def test_fdroid_latest_by_suggested_mark():
    src = sources.Sources(FakeHttp(raw={fdroid.API + VLC: api()}), sleep=lambda s: None)
    assert src.latest("fdroid", VLC, "") == sources.Latest(None, "fd:13070208", True)
    assert src.latest("fdroid", VLC, "fd:13070208") == sources.Latest(None, "fd:13070208", False)


def test_fdroid_apps_checked_at_night(conn, clock, acct):
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    db.put_app(conn, shelf.id, VLC, "VLC", "fdroid", VLC, clock.iso())
    db.set_status(conn, shelf.id, VLC, "ok")
    assert [a["package"] for a in db.apps_to_check(conn)] == [VLC]
