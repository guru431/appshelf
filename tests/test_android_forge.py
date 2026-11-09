import json
from pathlib import Path

import pytest

from android_fakes import FakeHttp
from apk_factory import make_apk
from appshelf.android import forge, links, net, sources

DATA = Path(__file__).parent / "data"
CB_LATEST = f"{forge.API['codeberg']}/repos/gitnex/GitNex/releases/latest"
GL = forge.API["gitlab"]


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def response(value, status=200):
    return status, {"content-type": "application/json"}, json.dumps(value).encode()


def gitlab_http(name, path, releases=None):
    project = load(f"gitlab/project_{name}.json")
    return FakeHttp(raw={
        f"{GL}/projects/{path.replace('/', '%2F')}": response(project),
        f"{GL}/projects/{project['id']}/releases?per_page=5": response(
            load(f"gitlab/releases_{name}.json") if releases is None else releases)})


@pytest.mark.parametrize("source,value,expected", [
    ("codeberg", "gitnex/GitNex", "gitnex/GitNex"),
    ("codeberg", "https://codeberg.org/gitnex/GitNex/releases/tag/15.1.0", "gitnex/GitNex"),
    ("codeberg", "https://codeberg.org/gitnex/GitNex.git", "gitnex/GitNex"),
    ("codeberg", "https://codeberg.org/gitnex/GitNex/issues/1", ""),
    ("codeberg", "https://codeberg.org.evil.example/gitnex/GitNex", ""),
    ("codeberg", "https://user@codeberg.org/gitnex/GitNex", ""),
    ("codeberg", "http://codeberg.org/gitnex/GitNex", ""),
    ("codeberg", "a/b/c", ""), ("codeberg", "a/..", ""), ("codeberg", "a/.hidden", ""),
    ("gitlab", "https://gitlab.com/group/sub/project/-/releases", "group/sub/project"),
    ("gitlab", "group/sub/project", "group/sub/project"),
    ("gitlab", "https://gitlab.com/-/project/1/uploads/x/app.apk", ""),
    ("gitlab", "https://gitlab.com:8443/group/project", ""),
    ("gitlab", "https://codeberg.org/group/project", ""),
    ("gitlab", "group", ""), ("gitlab", "group/proj%2e", ""),
])
def test_parse_ref(source, value, expected):
    assert forge.parse_ref(source, value) == expected


def test_codeberg_release_from_recorded_answer():
    http = FakeHttp(raw={CB_LATEST: response(load("codeberg/release_latest.json"))})
    app = forge.info(http, "codeberg", "https://codeberg.org/gitnex/GitNex")
    assert (app.repository, app.name, app.version_name, app.asset_name) == (
        "gitnex/GitNex", "GitNex", "15.1.0", "15.1.0.apk")
    assert app.asset_url == "https://codeberg.org/gitnex/GitNex/releases/download/15.1.0/15.1.0.apk"
    assert app.mark == "cb:13496490:2187714:248541bd-e7a4-4dcb-9839-f0bf8fc37f71"
    assert "Improved SVG rendering" in app.whats_new
    assert [c[2] for c in http.calls] == [CB_LATEST]


@pytest.mark.parametrize("change", [
    {"prerelease": True}, {"draft": True}, {"assets": "wrong"}, {"tag_name": ""}])
def test_codeberg_unknown_or_unstable_release(change):
    d = {**load("codeberg/release_latest.json"), **change}
    with pytest.raises(net.SourceError, match="неизвестном формате"):
        forge.info(FakeHttp(raw={CB_LATEST: response(d)}), "codeberg", "gitnex/GitNex")


def test_codeberg_asset_of_other_repository_or_host_is_refused():
    d = load("codeberg/release_latest.json")
    for url in ("https://codeberg.org/other/app/releases/download/1/app.apk", "https://127.0.0.1/app.apk"):
        d["assets"][0]["browser_download_url"] = url
        with pytest.raises(net.SourceError, match="нет одного APK"):
            forge.info(FakeHttp(raw={CB_LATEST: response(d)}), "codeberg", "gitnex/GitNex")


def test_gitlab_release_links_and_description_uploads():
    pauses = []
    app = forge.info(gitlab_http("exif", "juanitobananas/scrambled-exif"), "gitlab", "juanitobananas/scrambled-exif",
                     wait=lambda: pauses.append(1))
    assert (app.repository, app.version_name, app.asset_name) == (
        "juanitobananas/scrambled-exif", "v1.8.3", "scrambled-exif-standard-release-1.8.3.apk")
    assert app.asset_url.startswith("https://gitlab.com/-/project/5252730/uploads/") and pauses == [1]
    # Только загрузки в описании (ссылок выпуска нет): из трёх APK — обычная сборка, ссылки убраны из текста.
    app = forge.info(gitlab_http("aurora", "AuroraOSS/AuroraStore"), "gitlab", "AuroraOSS/AuroraStore")
    assert app.asset_url == ("https://gitlab.com/-/project/6922885/uploads/245eec8867a47057cbc54535f73b32ee/"
                             "AuroraStore-4.8.4.apk")
    assert app.mark == f"gl:6922885:4.8.4:{app.asset_url}"
    assert "Jetpack Compose\n" in app.whats_new and "/uploads/" not in app.whats_new and "\\" not in app.whats_new


def test_gitlab_skips_upcoming_and_foreign_links():
    release = {"tag_name": "2.0", "description": "", "upcoming_release": False, "assets": {"links": [
        {"name": "app.apk", "url": "https://files.example/app.apk"},
        {"name": "app-arm64.apk", "url": "https://gitlab.com/other/project/-/package_files/1/download"},
        {"name": "app-universal.apk", "url": "https://gitlab.com/AuroraOSS/AuroraStore/-/package_files/7/download"}]}}
    upcoming = {**release, "tag_name": "3.0", "upcoming_release": True}
    app = forge.info(gitlab_http("aurora", "AuroraOSS/AuroraStore", [upcoming, release]), "gitlab",
                     "AuroraOSS/AuroraStore")
    assert (app.version_name, app.asset_name) == ("2.0", "app-universal.apk")
    with pytest.raises(net.SourceError, match="нет одного APK"):
        forge.info(gitlab_http("aurora", "AuroraOSS/AuroraStore", [{**release, "assets": {"links": release[
            "assets"]["links"][:2]}}]), "gitlab", "AuroraOSS/AuroraStore")


def test_forge_errors_remain_distinct():
    with pytest.raises(net.SourceError, match="нет публичного репозитория"):
        forge.info(FakeHttp(raw={CB_LATEST: response({}, 404)}), "codeberg", "gitnex/GitNex")
    with pytest.raises(net.FetchError, match="ограничил"):
        forge.info(FakeHttp(raw={CB_LATEST: response({}, 429)}), "codeberg", "gitnex/GitNex")
    with pytest.raises(net.SourceError, match="ссылка на репозиторий GitLab"):
        forge.info(FakeHttp(), "gitlab", "https://gitlab.com/-/explore")


def test_links_route_repository_pages_but_keep_file_links():
    assert links.parse("https://codeberg.org/gitnex/GitNex/releases") == ("codeberg", "gitnex/GitNex")
    assert links.parse("https://gitlab.com/group/sub/app/-/releases/permalink/latest") == ("gitlab", "group/sub/app")
    assert links.parse("https://gitlab.com/group/app") == ("gitlab", "group/app")
    for url in ("https://codeberg.org/gitnex/GitNex/releases/download/15.1.0/15.1.0.apk",
                "https://gitlab.com/-/project/1/uploads/0123/app.apk",
                "https://gitlab.com/group/app/-/package_files/1/download"):
        assert links.parse(url) == ("link", url)
    assert links.page_url("codeberg", "gitnex/GitNex") == "https://codeberg.org/gitnex/GitNex"
    assert links.page_url("gitlab", "../x") == ""


def test_search_by_repository_link_checks_only_that_release():
    http = gitlab_http("aurora", "AuroraOSS/AuroraStore")
    src = sources.Sources(http, sleep=lambda s: None, pause=0)
    result = src.search_all("https://gitlab.com/AuroraOSS/AuroraStore/-/releases")
    assert not result.errors
    assert [(o.source, o.ref, o.version) for o in result.options] == [("gitlab", "AuroraOSS/AuroraStore", "4.8.4")]
    assert len(http.calls) == 2


def test_sources_fetch_check_and_cache_codeberg(tmp_path):
    d = load("codeberg/release_latest.json")
    url = d["assets"][0]["browser_download_url"]
    body = make_apk(tmp_path / "a.apk", abis=("arm64-v8a",)).read_bytes()
    now = [0.0]
    http = FakeHttp(raw={CB_LATEST: response(d)}, files={url: body})
    src = sources.Sources(http, sleep=lambda s: None, pause=0, clock=lambda: now[0])
    got = src.fetch("codeberg", "gitnex/GitNex", tmp_path / "out.apk")
    assert got.etag.startswith("cb:") and "Improved" in got.whats_new
    assert not src.latest("codeberg", "gitnex/GitNex", got.etag).changed
    assert src.download_url("codeberg", "gitnex/GitNex") == url
    assert [c[0] for c in http.calls] == ["request", "download"]   # выпуск — из кэша
    now[0] = 901
    assert src.latest("codeberg", "gitnex/GitNex", "cb:old").changed


def test_sources_refuse_x86_only_release(tmp_path):
    d = load("codeberg/release_latest.json")
    url = d["assets"][0]["browser_download_url"]
    http = FakeHttp(raw={CB_LATEST: response(d)}, files={url: make_apk(tmp_path / "x.apk", abis=("x86_64",)).read_bytes()})
    with pytest.raises(net.SourceError, match="в Codeberg нет сборки для ARM"):
        sources.Sources(http, sleep=lambda s: None, pause=0).fetch("codeberg", "gitnex/GitNex", tmp_path / "out.apk")
