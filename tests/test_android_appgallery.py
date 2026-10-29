import hashlib
import json
from pathlib import Path

import pytest

from android_fakes import FakeHttp
from apk_factory import png_bytes
from appshelf.android import appgallery, db, net, sources

DATA = Path(__file__).parent / "data" / "appgallery"
VTB, VTB_ID = "ru.vtb24.mobilebanking.android", "C102312137"
API = f"https://{appgallery.HOST}/hwmarket/api/clientApi"
CDN = ("https://appdl-11-dra.dbankcdn.com/dl/appdl/application/apk/d5/d5add246e75049b4b0144c30a175fcf2/"
       "ru.vtb24.mobilebanking.android.2610110422.apk?maple=0&trackId=0&distOpEntity=HKS")


def fixture(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def served(data: bytes = b"APK") -> tuple[dict, str, str]:
    """Ответ updateCheck, у которого размер и SHA-256 — как у data; (ответ, ссылка на APK, иконка)."""
    answer = fixture("update_check")
    rec = answer["list"][0]
    rec["size"], rec["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    return answer, rec["fullDownUrl"], rec["icon"]


def test_appgallery_detail_fixture():
    http = FakeHttp(forms={API: fixture("update_check")})
    app = appgallery.info(http, appgallery.HOST, VTB)
    assert (app.app_id, app.package, app.name, app.version_name, app.version_code, app.size) == \
        (VTB_ID, VTB, "Банк ВТБ", "20.19.0.6", 2090141590, 189014452)
    assert app.sha256 == "aefbcd22d2514d586fe0cbb35896d410974bc92b7009ca65e0a8b836977e1764"
    assert app.apk_url.startswith("https://appdlc-drru.hispace.dbankcloud.ru/dl/appdl/")
    assert app.icon_url.endswith(".webp")
    _, url, headers, form = http.calls[0]
    assert url == API and headers["User-Agent"].startswith("UpdateSDK##")
    assert form["method"] == "client.updateCheck" and json.loads(form["pkgInfo"])["params"][0]["package"] == VTB


def test_appgallery_empty_zone_error():
    http = FakeHttp(forms={API: fixture("update_check_empty")})
    with pytest.raises(net.SourceError, match=r"AppGallery не нашёл приложение ru\.vtb24\.mobilebanking\.android "
                                              r"\(зона RU\)"):
        appgallery.info(http, appgallery.HOST, VTB)
    with pytest.raises(net.SourceError, match="неизвестном формате"):
        appgallery.info(FakeHttp(forms={API: {"rtnCode": 1, "rtnDesc": "x"}}), appgallery.HOST, VTB)


def test_appgallery_link_id_to_package():
    home = "https://appgallery.cloud.huawei.com/"
    http = FakeHttp(heads={appgallery.APPDL + VTB_ID: net.Meta(CDN, 206, "", "", 189014452, ""),
                           appgallery.APPDL + "C000000001": net.Meta(home, 200, "", "", None, "text/html")})
    assert appgallery.package_of(http, VTB_ID) == VTB
    with pytest.raises(net.SourceError, match="AppGallery не нашёл приложение C000000001"):
        appgallery.package_of(http, "C000000001")


def test_appgallery_fetch_by_id_checks_file(tmp_path):
    answer, url, icon = served()
    http = FakeHttp(heads={appgallery.APPDL + VTB_ID: net.Meta(CDN, 206, "", "", 3, "")}, forms={API: answer},
                    files={url: b"APK", icon: png_bytes(16)})
    src = sources.Sources(http, sleep=lambda s: None)
    got = src.fetch("appgallery", VTB_ID, tmp_path / "a.apk")
    assert (tmp_path / "a.apk").read_bytes() == b"APK"
    assert (got.name, got.expected_signer, got.ref) == ("Банк ВТБ", "", VTB) and got.icon.startswith(b"\x89PNG")
    assert src.fetch("appgallery", VTB, tmp_path / "b.apk").ref == VTB              # пакет — без appdl
    assert [c[0] for c in http.calls].count("head") == 1
    http.files[url] = b"APX"                                                          # битый файл
    with pytest.raises(net.SourceError, match="контрольн"):
        src.fetch("appgallery", VTB, tmp_path / "c.apk")
    assert not (tmp_path / "c.apk").exists()


def test_appgallery_latest_is_version_code():
    src = sources.Sources(FakeHttp(forms={API: fixture("update_check")}), sleep=lambda s: None)
    assert src.latest("appgallery", VTB, "") == sources.Latest(2090141590, "", True)


def test_appgallery_host_from_sources():
    http = FakeHttp(forms={"https://store-dre.hispace.dbankcloud.com/hwmarket/api/clientApi": fixture("update_check")})
    src = sources.Sources(http, sleep=lambda s: None, appgallery_host="store-dre.hispace.dbankcloud.com")
    assert src.latest("appgallery", VTB, "").version_code == 2090141590


def test_appgallery_apps_checked_at_night(conn, clock, acct):
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    db.put_app(conn, shelf.id, VTB, "ВТБ", "appgallery", VTB, clock.iso())
    db.set_status(conn, shelf.id, VTB, "ok")
    assert [a["package"] for a in db.apps_to_check(conn)] == [VTB]
