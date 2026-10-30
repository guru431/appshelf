import json

import pytest

from android_fakes import FakeHttp
from appshelf.android import github, net

REF = "example/android-client"
LATEST = f"{github.API}/repos/{REF}/releases/latest"


def response(value, status=200):
    return status, {"content-type": "application/json"}, json.dumps(value).encode()


def release(*names, repo=REF):
    return {"id": 100, "tag_name": "v1.2", "draft": False, "prerelease": False,
            "assets": [{"id": i + 1, "name": name, "state": "uploaded", "updated_at": "2026-10-01T12:00:00Z",
                        "browser_download_url": f"https://github.com/{repo}/releases/download/v1.2/{name}"}
                       for i, name in enumerate(names)]}


@pytest.mark.parametrize("value,expected", [
    (REF, REF), (f"https://github.com/{REF}.git", REF),
    (f"https://github.com/{REF}/releases/latest", REF),
    (f"https://github.com/{REF}/releases/download/v1.2/client.apk", REF),
    ("https://github.com.evil.example/example/android-client", ""),
    ("https://user@github.com/example/android-client", ""),
    ("http://github.com/example/android-client", ""),
    ("https://github.com:444/example/android-client", ""),
    ("https://github.com/example/android-client/issues/1", ""),
    ("example/..", ""), ("example/repo/extra", ""), ("example/%2e%2e", ""),
])
def test_parse_repository(value, expected):
    assert github.parse_ref(value) == expected


def test_latest_chooses_apk_for_arm_and_tracks_asset_changes():
    d = release("client.x86_64.apk", "client-armeabi-v7a.apk", "client.apk", "client-arm64-v8a.apk",
                "client-debug-universal.apk", "client.aab", "client.apks")
    http = FakeHttp(raw={LATEST: response(d)})
    app = github.info(http, REF)
    assert (app.repository, app.name, app.version_name, app.asset_name) == (
        REF, "android-client", "v1.2", "client-arm64-v8a.apk")
    assert app.asset_url == d["assets"][3]["browser_download_url"]
    assert app.mark == "gh:100:4:2026-10-01T12:00:00Z"
    assert http.calls == [("request", "GET", LATEST, github.HEADERS)]
    d["assets"][3]["updated_at"] = "2026-10-02T12:00:00Z"
    assert github.info(FakeHttp(raw={LATEST: response(d)}), REF).mark != app.mark


def test_universal_is_preferred_and_armeabi_is_a_fallback():
    d = release("client-arm64-v8a.apk", "client-universal.apk", "client-armeabi-v7a.apk")
    assert github.info(FakeHttp(raw={LATEST: response(d)}), REF).asset_name == "client-universal.apk"
    assert github.info(FakeHttp(raw={LATEST: response(release("client-armeabi-v7a.apk"))}), REF).asset_name == (
        "client-armeabi-v7a.apk")


@pytest.mark.parametrize("d", [
    release("client.x86.apk", "client.apks", "config.arm64_v8a.apk"),
    {**release("client.apk"), "assets": [{**release("client.apk")["assets"][0],
                                        "browser_download_url": "https://127.0.0.1/client.apk"}]},
    {**release("client.apk"), "assets": [{**release("client.apk")["assets"][0],
                                        "browser_download_url": "https://github.com/other/repo/releases/download/v1/client.apk"}]},
])
def test_no_apk_or_foreign_asset_is_refused(d):
    with pytest.raises(net.SourceError, match=github.NO_APK):
        github.info(FakeHttp(raw={LATEST: response(d)}), REF)


@pytest.mark.parametrize("d", [{}, [], {**release("client.apk"), "prerelease": True},
                              {**release("client.apk"), "assets": "wrong"}])
def test_unknown_release(d):
    with pytest.raises(net.SourceError, match=github.UNKNOWN):
        github.info(FakeHttp(raw={LATEST: response(d)}), REF)


def test_github_errors_remain_distinct():
    with pytest.raises(net.SourceError, match="стабильного выпуска"):
        github.info(FakeHttp(raw={LATEST: response({}, 404)}), REF)
    with pytest.raises(net.FetchError, match="ограничил"):
        github.info(FakeHttp(raw={LATEST: response({}, 403),
                                 f"https://github.com/{REF}/releases/latest": response({}, 429)}), REF)
    with pytest.raises(net.FetchError, match="502"):
        github.info(FakeHttp(raw={LATEST: response({}, 502)}), REF)


def test_api_rate_limit_falls_back_to_same_repository_release_html():
    page = f"https://github.com/{REF}/releases/latest"
    fragment = f"https://github.com/{REF}/releases/expanded_assets/v1.2"
    base = f"/{REF}/releases/download/v1.2/"
    http = FakeHttp(raw={LATEST: response({}, 403),
                         page: (200, {}, f'<include-fragment src="{fragment}"></include-fragment>'.encode()),
                         fragment: (200, {}, (f'<a href="{base}client-x86.apk">x86</a>'
                                              f'<a href="{base}client-arm64.apk">ARM</a>'
                                              '<a href="https://github.com/other/app/releases/download/v1.2/client.apk">bad</a>'
                                              f'<a href="/{REF}/releases/download/other/client.apk">old</a>').encode())})
    pauses = []
    app = github.info(http, REF, wait=lambda: pauses.append(True))
    assert (app.asset_name, app.version_name) == ("client-arm64.apk", "v1.2")
    assert app.mark == f"gh-web:v1.2:https://github.com{base}client-arm64.apk"
    assert len(pauses) == 2
    assert [c[2] for c in http.calls] == [LATEST, page, fragment]


def test_html_fallback_does_not_follow_foreign_fragment():
    page = f"https://github.com/{REF}/releases/latest"
    http = FakeHttp(raw={LATEST: response({}, 429),
                         page: (200, {}, b'<include-fragment src="https://evil.example/releases/expanded_assets/v1"></include-fragment>')})
    with pytest.raises(net.SourceError, match="файлы последнего"):
        github.info(http, REF)
    assert len(http.calls) == 2
