import hashlib
import re
from pathlib import Path

import pytest

from android_fakes import FakeHttp
from apk_factory import png_bytes
from appshelf.android import apkpure, appgallery, fdroid, net, pb, rustore, sources

DATA = Path(__file__).parent / "data" / "apkpure"
K9, WA = "com.fsck.k9", "com.whatsapp"
EMPTY = pb.encode([(1, [(7, [(1, [(2, 1)])])]), (2, [(2, "INVALID_COMMAND"), (3, "SUCCESS")])])
AG_API = f"https://{appgallery.HOST}/hwmarket/api/clientApi"


def answer(name: str) -> tuple[int, dict, bytes]:
    return 200, {"content-type": "application/octet-stream"}, (DATA / name).read_bytes()


def elsewhere(rustore_price=None, in_fdroid=False) -> dict:
    """Маршруты FakeHttp для подсказки «есть в …» о WA: RuStore (цена или 404), AppGallery — нет, F-Droid."""
    routes = {"raw": {fdroid.API + WA: (200, {}, b'{"packageName": "x", "suggestedVersionCode": 1, "packages": '
                                                 b'[{"versionName": "1", "versionCode": 1}]}') if in_fdroid
                      else (404, {}, b'{"error":"NOT_FOUND"}')},
              "forms": {AG_API: {"rtnCode": 0, "count": 0, "list": []}},
              "json_routes": {f"{rustore.BASE}/applicationData/overallInfo/{WA}": net.FetchError("ответил 404")}}
    if rustore_price is not None:
        routes["json_routes"][f"{rustore.BASE}/applicationData/overallInfo/{WA}"] = {"code": "OK", "body": {
            "appId": 7, "packageName": WA, "appName": "WhatsApp", "versionCode": 1, "price": rustore_price}}
    return routes


def test_apkpure_fixture_apk():
    http = FakeHttp(raw={apkpure.info_url(K9): answer("app_version.bin")})
    app = apkpure.info(http, K9)
    assert (app.package, app.name, app.version_name, app.version_code, app.kind, app.size) == \
        (K9, "K-9 Mail", "24.0", 39046, "apk", 11416719)
    assert app.url.startswith("https://download.pureapk.com/b/APK/") and app.sha1 == \
        "dbda2aca07881353b855d1611bfaf22b6266fabf" and app.icon_url.startswith("https://image.winudf.com/")
    assert http.calls[0][3] == {"x-cv": "3172501", "x-sv": "29", "x-abis": "arm64-v8a,armeabi-v7a,armeabi", "x-gp": "1"}
    vlc = apkpure.info(FakeHttp(raw={apkpure.info_url("org.videolan.vlc"): answer("app_version_vlc.bin")}),
                       "org.videolan.vlc")
    assert (vlc.version_code, vlc.kind) == (13070206, "apk")                    # первая запись — сборка arm64
    assert apkpure.info(FakeHttp(raw={apkpure.info_url(WA): answer("app_version_xapk.bin")}), WA).kind == "xapk"


def test_apkpure_missing_and_broken():
    with pytest.raises(net.SourceError, match="нет в APKPure"):
        apkpure.info(FakeHttp(raw={apkpure.info_url(K9): (200, {}, EMPTY)}), K9)
    with pytest.raises(net.SourceError, match="неизвестном формате"):
        apkpure.info(FakeHttp(raw={apkpure.info_url(K9): (200, {}, b"\xff\xff\xff")}), K9)
    with pytest.raises(net.FetchError, match="403"):
        apkpure.info(FakeHttp(raw={apkpure.info_url(K9): (403, {}, b"")}), K9)


def reply(package: str, body: bytes, url: str, icon: str) -> bytes:
    """Ответ app_version того же вида, что записанный (README фикстуры): одна запись APK с размером и SHA-1 body."""
    app = [(1, "K-9 Mail"), (2, "K-9 Mail"), (4, package), (5, "39046"), (6, "24.0"),
           (24, [(3, hashlib.sha1(body).hexdigest()), (4, len(body)), (8, "APK"), (9, url)]), (27, [(1, [(1, icon)])])]
    return pb.encode([(1, [(7, [(1, [(2, 1)]), (2, [(1, "version_list"), (3, [(2, app)])])])]),
                      (2, [(2, "INVALID_COMMAND"), (3, "SUCCESS")])])


def test_apkpure_fetch_checks_sha1(tmp_path):
    url, icon, body = "https://download.pureapk.com/b/APK/x", "https://image.winudf.com/v2/icon.png", b"APK"
    http = FakeHttp(raw={apkpure.info_url(K9): (200, {}, reply(K9, body, url, icon))},
                    files={url: body, icon: png_bytes(16)})
    src = sources.Sources(http, sleep=lambda s: None)
    got = src.fetch("apkpure", K9, tmp_path / "a.apk")
    assert (tmp_path / "a.apk").read_bytes() == body and (got.name, got.expected_signer) == ("K-9 Mail", "")
    assert got.icon.startswith(b"\x89PNG")
    assert src.latest("apkpure", K9, "") == sources.Latest(39046, "", True)
    http.files[url] = b"APX"
    with pytest.raises(net.SourceError, match="контрольн"):
        src.fetch("apkpure", K9, tmp_path / "b.apk")
    assert not (tmp_path / "b.apk").exists()


def test_apkpure_xapk_refused_with_hint(tmp_path):
    http = FakeHttp(**{**elsewhere(rustore_price=0), "raw": {**elsewhere()["raw"],
                                                             apkpure.info_url(WA): answer("app_version_xapk.bin")}})
    with pytest.raises(sources.SplitOnly, match=re.escape(sources.SPLIT_TEXT + "; есть в RuStore — смените источник")):
        sources.Sources(http, sleep=lambda s: None).fetch("apkpure", WA, tmp_path / "a.apk")
    assert not [c for c in http.calls if c[0] == "download"] and not (tmp_path / "a.apk").exists()
    http = FakeHttp(**{**elsewhere(), "raw": {**elsewhere()["raw"],
                                              apkpure.info_url(WA): answer("app_version_xapk.bin")}})
    with pytest.raises(sources.SplitOnly) as e:
        sources.Sources(http, sleep=lambda s: None).fetch("apkpure", WA, tmp_path / "a.apk")
    assert str(e.value) == sources.SPLIT_TEXT                                   # нигде больше нет — без подсказки


def test_hint_lists_free_sources():
    src = sources.Sources(FakeHttp(**elsewhere(rustore_price=0, in_fdroid=True)), sleep=lambda s: None)
    assert src.elsewhere(WA) == ["RuStore", "F-Droid"]
    src = sources.Sources(FakeHttp(**elsewhere(rustore_price=9900)), sleep=lambda s: None)
    assert src.elsewhere(WA) == []                                               # в RuStore платное


def test_rustore_signature_or_none():
    sig = "ab" * 32
    http = FakeHttp(json_routes={f"{rustore.BASE}/applicationData/overallInfo/{K9}": {"code": "OK", "body": {
        "appId": 1, "packageName": K9, "versionCode": 1, "signature": sig}},
        f"{rustore.BASE}/applicationData/overallInfo/{WA}": net.FetchError("ответил 404")})
    src = sources.Sources(http, sleep=lambda s: None)
    assert (src.rustore_signature(K9), src.rustore_signature(WA)) == (sig, None)
