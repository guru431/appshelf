import urllib.parse

import pytest

from android_fakes import FakeHttp
from appshelf.android import discovery, net


def page(html, status=200):
    return status, {"content-type": "text/html; charset=utf-8"}, html.encode()


def test_discovers_packages_from_links_without_css_contract():
    query = "Happ & sync"
    html = '''<title>Android Apps on Google Play</title>
      <a href="/store/apps/details?id=com.happproxy" aria-label="Happ - Proxy Utility"></a>
      <a href="intent://play.google.com/store/apps/details?id=com.fake">Do not use</a>
      <a href="https://other.example/store/apps/details?id=com.fake">Do not use</a>
      <a href="/store/apps/details?id=com.happproxy">Duplicate</a>
      <a href="https://play.google.com/store/apps/details?id=com.example.sync&amp;x=1">
        <img alt="Thumbnail image"><div><span>Sync &amp; Share</span></div><span>Developer</span></a>
      <a href="/store/apps/details?id=../invalid">Invalid</a>'''
    http = FakeHttp(raw={discovery.search_url(query): page(html)})
    waits = []
    assert discovery.discover(http, query, wait=lambda: waits.append(1)) == [
        ("com.happproxy", "Happ - Proxy Utility"), ("com.example.sync", "Sync & Share")]
    assert waits == [1]
    url = http.calls[0][2]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) == {
        "q": [query], "c": ["apps"], "hl": ["en"]}


def test_discovery_is_bounded_and_valid_empty_search_is_not_an_error():
    html = '<title>Apps - Google Play</title>' + ''.join(
        f'<a href="/store/apps/details?id=com.example.app{i}">App {i}</a>' for i in range(20))
    http = FakeHttp(raw={discovery.search_url("app"): page(html),
                         discovery.search_url("empty"): page('<title>Google Play</title>')})
    assert discovery.discover(http, "app") == [(f"com.example.app{i}", f"App {i}") for i in range(5)]
    assert discovery.discover(http, "empty") == []
    assert discovery.discover(http, " ") == []
    assert len(http.calls) == 2


@pytest.mark.parametrize("answer,error", [
    (page("forbidden", 403), net.FetchError),
    (page("<title>Before you continue to Google</title>consent"), net.SourceError),
    ((200, {}, b"\xff"), net.SourceError),
])
def test_discovery_failure_is_not_silently_treated_as_no_results(answer, error):
    http = FakeHttp(raw={discovery.search_url("test"): answer})
    with pytest.raises(error):
        discovery.discover(http, "test")
