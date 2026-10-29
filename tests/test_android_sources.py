import json
from pathlib import Path

import pytest

from android_fakes import FakeHttp
from apk_factory import png_bytes
from appshelf.android import net, rustore, sources

DATA = Path(__file__).parent / "data" / "rustore"
SBER_SIG = "fea43ebfc12201c7d860b1de28a0f8a330ecc4c30863dae7ce6cf4c98b99a2ea"


def fixture(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


OVERALL = f"{rustore.BASE}/applicationData/overallInfo/ru.sberbankmobile"
SEARCH = f"{rustore.BASE}/applicationData/apps"
LINK = f"{rustore.BASE}/applicationData/v2/download-link"
ZIP = fixture("download")["body"]["downloadUrls"][0]["url"]
APK = ZIP[:-len(".zip")] + ".apk"
ICON = fixture("overall")["body"]["iconUrl"]


def rustore_http(**files) -> FakeHttp:
    return FakeHttp({OVERALL: fixture("overall"), SEARCH: fixture("search"), LINK: fixture("download")},
                    {APK: b"APK", ICON: png_bytes(16), **files})


def test_parse_overall_info_fixture():
    app = rustore.info(rustore_http(), "ru.sberbankmobile")
    assert (app.app_id, app.package, app.name, app.version_name, app.version_code, app.price) == \
        (462271, "ru.sberbankmobile", "СберБанк Онлайн", "17.14.0", 2026062416, 0)
    assert app.signature == SBER_SIG and app.icon_url == ICON and app.size == 126327301


def test_signature_normalized_to_lower_hex():
    data = fixture("overall")
    data["body"]["signature"] = ":".join(SBER_SIG[i:i + 2].upper() for i in range(0, 64, 2))
    assert rustore.info(FakeHttp({OVERALL: data}), "ru.sberbankmobile").signature == SBER_SIG


def test_search_fixture_lists_apps():
    found = rustore.search(rustore_http(), "сбер", limit=5)
    assert [a.package for a in found][:2] == ["ru.sberbankmobile", "ru.sberbank.sberkids"]
    assert found[0].app_id == 462271 and found[0].version_code == 2026062416 and found[0].price == 0


def test_rustore_sends_version_header():
    http = rustore_http()
    rustore.search(http, "сбер")
    rustore.info(http, "ru.sberbankmobile")
    rustore.apk_url(http, 462271)
    assert all(c[2] == {"ruStoreVerCode": "1105002"} for c in http.calls)
    assert "query=%D1%81%D0%B1%D0%B5%D1%80" in http.calls[0][1]
    assert http.calls[2][3]["withoutSplits"] is True and http.calls[2][3]["appId"] == 462271


def test_apk_url_takes_apk_instead_of_zip_wrapper():
    assert rustore.apk_url(rustore_http(), 462271) == APK


def test_unknown_format_is_source_error():
    bad = FakeHttp({OVERALL: {"code": "ERROR", "body": None}, LINK: {"code": "OK", "body": {"downloadUrls": []}}})
    src = sources.Sources(bad, sleep=lambda s: None)
    with pytest.raises(sources.SourceError, match="неизвестном формате"):
        src.latest("rustore", "ru.sberbankmobile", "")
    with pytest.raises(sources.SourceError):
        rustore.apk_url(bad, 1)


def test_rustore_fetch_returns_signature_name_and_icon(tmp_path):
    src = sources.Sources(rustore_http(), sleep=lambda s: None)
    got = src.fetch("rustore", "ru.sberbankmobile", tmp_path / "a.apk")
    assert (tmp_path / "a.apk").read_bytes() == b"APK"
    assert (got.name, got.expected_signer) == ("СберБанк Онлайн", SBER_SIG)
    assert got.icon.startswith(b"\x89PNG")
    broken = sources.Sources(rustore_http(**{ICON: net.FetchError("нет")}), sleep=lambda s: None)
    assert broken.fetch("rustore", "ru.sberbankmobile", tmp_path / "b.apk").icon is None


def test_paid_app_refused(tmp_path):
    data = fixture("overall")
    data["body"]["price"] = 19900
    http = FakeHttp({OVERALL: data, LINK: fixture("download")}, {APK: b"APK"})
    with pytest.raises(sources.SourceError, match="платное"):
        sources.Sources(http, sleep=lambda s: None).fetch("rustore", "ru.sberbankmobile", tmp_path / "a.apk")
    assert [c[0] for c in http.calls] == ["json"] and not (tmp_path / "a.apk").exists()


def test_rustore_latest_is_version_code():
    latest = sources.Sources(rustore_http(), sleep=lambda s: None).latest("rustore", "ru.sberbankmobile", "")
    assert latest == sources.Latest(2026062416, "", True)


def test_link_latest_changed_by_validators():
    url = "https://bank.example/app.apk"
    meta = net.Meta(url, 200, '"abc"', "Wed, 28 Oct 2026 10:54:49 GMT", 176172934, "")
    src = sources.Sources(FakeHttp(heads={url: meta}), sleep=lambda s: None)
    etag = '"abc"|Wed, 28 Oct 2026 10:54:49 GMT|176172934'
    assert src.latest("link", url, etag) == sources.Latest(None, etag, False)
    assert src.latest("link", url, '"old"||1') == sources.Latest(None, etag, True)


def test_link_without_validators_always_downloads():
    url = "https://bank.example/app.apk"
    src = sources.Sources(FakeHttp(heads={url: net.Meta(url, 200, "", "", None, "")}), sleep=lambda s: None)
    assert src.latest("link", url, "||") == sources.Latest(None, "||", True)


def test_link_fetch_downloads_url_with_etag(tmp_path):
    url = "https://bank.example/app.apk"
    got = sources.Sources(FakeHttp(files={url: b"APK"}), sleep=lambda s: None).fetch("link", url, tmp_path / "a.apk")
    assert (got.name, got.expected_signer, got.icon, got.etag) == ("", "", None, '"e"||3')
    assert (tmp_path / "a.apk").read_bytes() == b"APK"


def test_unknown_source_refused(tmp_path):
    with pytest.raises(sources.SourceError):
        sources.Sources(FakeHttp(), sleep=lambda s: None).fetch("upload", "x", tmp_path / "a.apk")


def test_pause_between_requests():
    slept = []
    src = sources.Sources(rustore_http(), sleep=slept.append, clock=lambda: 100.0)
    src.search("сбер")
    assert slept == []                              # первый запрос в процессе — без паузы
    src.latest("rustore", "ru.sberbankmobile", "")
    assert slept == [2.0]
    times = iter([100.0, 100.5, 200.0])
    src = sources.Sources(rustore_http(), sleep=slept.append, clock=lambda: next(times))
    src.search("a")                                 # t=100
    src.search("b")                                 # t=100.5 — пауза 1.5
    src.search("c")                                 # t=200 — пауза не нужна
    assert slept == [2.0, 1.5]
