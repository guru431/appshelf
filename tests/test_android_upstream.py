import pytest

from android_fakes import FakeHttp
from appshelf.android import net, upstream

PACKAGE = "com.example.app"
PLAY = upstream.play_url(PACKAGE)


def page(body):
    return 200, {"content-type": "text/html"}, body.encode()


def metadata(description="", website="https://example.org"):
    return (f'<meta name="appstore:bundle_id" content="{PACKAGE}">'
            f'<meta name="appstore:developer_url" content="{website}">'
            f'<div data-g-id="description">{description}</div>')


def test_explicit_source_metadata_does_not_search_or_load_other_pages():
    http = FakeHttp()
    assert upstream.repositories(http, PACKAGE, metadata_links=("https://github.com/author/android-app",)) == (
        "author/android-app",)
    assert http.calls == []


def test_play_author_website_gives_android_download_repo_without_desktop_or_suggestions():
    http = FakeHttp(raw={
        PLAY: page(metadata() + '<section>Suggested app <a href="https://github.com/unrelated/android">GitHub</a></section>'),
        "https://example.org": page('<a href="https://github.com/author/desktop/releases/latest/download/client.exe">Windows</a>'
                                    '<script>window.downloads={"android":"https://github.com/author/android/releases/latest/download/client.apk"};</script>'
                                    '<a href="https://github.com/author/android/releases/latest/download/client_beta.apk">Beta</a>'
                                    '<a href="https://github.com/library/core">Powered by library</a>')})
    pauses = []
    assert upstream.repositories(http, PACKAGE, wait=lambda: pauses.append(True)) == ("author/android",)
    assert len(pauses) == len(http.calls) == 2


def test_description_github_repo_is_scoped_and_credits_are_excluded():
    body = metadata('Source code: <a href="https://github.com/author/app">GitHub</a>'
                    '<br>Thanks to library https://github.com/other/library', website="")
    http = FakeHttp(raw={PLAY: page(body + '<a href="https://github.com/other/suggested">Android</a>')})
    assert upstream.repositories(http, PACKAGE) == ("author/app",)
    assert len(http.calls) == 1


def test_direct_developer_website_is_followed_once_and_repo_count_is_bounded():
    body = ''.join(f'<a href="https://github.com/author/app{i}/releases/latest/download/app.apk">Android</a>' for i in range(6))
    body += '<a href="https://second.example">Source</a>'
    http = FakeHttp(raw={"https://example.org": page(body)})
    assert upstream.repositories(http, PACKAGE, metadata_links=("https://example.org", "https://second.example")) == (
        "author/app0", "author/app1", "author/app2")
    assert len(http.calls) == 1


def test_direct_source_metadata_duplicates_are_removed():
    http = FakeHttp()
    assert upstream.repositories(http, PACKAGE, metadata_links=(
        "https://github.com/author/app", "https://github.com/Author/App/releases/latest")) == ("author/app",)


def test_app_missing_or_without_author_links_is_not_guessed():
    assert upstream.repositories(FakeHttp(raw={PLAY: (404, {}, b"")}), PACKAGE) == ()
    assert upstream.repositories(FakeHttp(raw={PLAY: page(metadata("No links", website=""))}), PACKAGE) == ()


def test_store_metadata_does_not_accept_another_package():
    with pytest.raises(net.SourceError, match="описание"):
        upstream.repositories(FakeHttp(raw={PLAY: page(metadata().replace(PACKAGE, "other.package"))}), PACKAGE)


def test_source_failures_are_reported():
    with pytest.raises(net.FetchError, match="403"):
        upstream.repositories(FakeHttp(raw={PLAY: (403, {}, b"")}), PACKAGE)
    http = FakeHttp(raw={PLAY: page(metadata()), "https://example.org": net.BlockedAddress(net.BLOCKED)})
    with pytest.raises(net.BlockedAddress):
        upstream.repositories(http, PACKAGE)


def test_apkpure_description_links_do_not_require_another_store():
    http = FakeHttp()
    refs = upstream.repositories(http, "com.example.app", description='Source: https://github.com/example/android\nLibraries: https://github.com/other/library')
    assert refs == ("example/android",) and not http.calls
