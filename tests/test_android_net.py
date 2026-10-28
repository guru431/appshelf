import json
import socket

import pytest

from appshelf.android import net


def fake_resolve(*addrs, table=None):
    """getaddrinfo: имя → адреса (table) или одни и те же addrs для любого имени."""
    def resolve(host, port, family=0, type=0, *rest):
        found = table[host] if table is not None else addrs
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in found]
    return resolve


class Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status, self.headers, self.body = status, {k.lower(): v for k, v in (headers or {}).items()}, body

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read(self, n=-1):
        n = len(self.body) if n < 0 else n
        chunk, self.body = self.body[:n], self.body[n:]
        return chunk

    def close(self):
        pass


class Server:
    """Подставной connect: ответы по очереди, запросы — в .requests (host, ip, method, target, body, headers)."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, host, ip, port, ctx, timeout):
        server = self

        class Conn:
            def request(self, method, target, body=None, headers=None):
                server.requests.append((host, ip, method, target, body, headers or {}))

            def getresponse(self):
                return server.responses.pop(0)

            def close(self):
                pass
        return Conn()


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "192.168.0.1", "169.254.1.1", "100.64.0.1", "::1",
                                  "fe80::1", "fc00::1", "0.0.0.0", "224.0.0.1", "::ffff:10.0.0.1"])
def test_private_address_blocked_before_connect(addr):
    calls = []
    http = net.Http(resolve=fake_resolve(addr), connect=lambda *a: calls.append(a))
    with pytest.raises(net.BlockedAddress):
        http.head("https://bank.example/app.apk")
    assert calls == []


def test_any_private_address_among_several_blocks():
    server = Server(Resp(200))
    http = net.Http(resolve=fake_resolve("8.8.8.8", "10.0.0.1"), connect=server)
    with pytest.raises(net.BlockedAddress):
        http.head("https://bank.example/app.apk")
    assert server.requests == []


def test_redirect_into_private_network_blocked(tmp_path):
    server = Server(Resp(302, {"Location": "https://intra.example/x.apk"}), Resp(200))
    http = net.Http(resolve=fake_resolve(table={"bank.example": ["8.8.8.8"], "intra.example": ["10.0.0.5"]}),
                    connect=server)
    with pytest.raises(net.BlockedAddress):
        http.download("https://bank.example/app.apk", tmp_path / "x.apk")
    assert len(server.requests) == 1 and not (tmp_path / "x.apk").exists()


def test_relative_redirect_followed():
    server = Server(Resp(301, {"Location": "/apk/new.apk"}), Resp(200, {"ETag": '"e1"', "Content-Length": "5"}))
    meta = net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).head("https://bank.example/old.apk")
    assert [r[3] for r in server.requests] == ["/old.apk", "/apk/new.apk"]
    assert (meta.url, meta.etag, meta.length) == ("https://bank.example/apk/new.apk", '"e1"', 5)


def test_too_many_redirects():
    server = Server(*[Resp(302, {"Location": f"https://bank.example/{i}"}) for i in range(10)])
    with pytest.raises(net.FetchError, match="переадресац"):
        net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).head("https://bank.example/0")
    assert len(server.requests) == net.MAX_REDIRECTS + 1


def test_rejects_http_userinfo_and_ip_literal_private():
    http = net.Http(resolve=fake_resolve("8.8.8.8"), connect=Server())
    for url in ["http://a.example/x.apk", "https://u:p@a.example/x.apk", "https://[::1]/x.apk", "ftp://a/x",
                "https:///x.apk", "https://10.0.0.1/x.apk", "https://a.example:99999/x.apk"]:
        with pytest.raises(net.FetchError):
            http.head(url)
    with pytest.raises(net.FetchError):
        net.check_url("http://a.example/x.apk")
    assert net.check_url("https://a.example/x.apk?v=1").hostname == "a.example"


def test_redirect_to_http_refused():
    server = Server(Resp(302, {"Location": "http://bank.example/x.apk"}))
    with pytest.raises(net.FetchError, match="https"):
        net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).head("https://bank.example/x.apk")


def test_head_falls_back_to_range_get():
    server = Server(Resp(405), Resp(206, {"ETag": '"e2"', "Last-Modified": "Wed, 28 Oct 2026 10:54:49 GMT",
                                          "Content-Range": "bytes 0-0/176172934"}, b"P"))
    meta = net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).head("https://bank.example/x.apk")
    assert [(r[2], r[5].get("Range")) for r in server.requests] == [("HEAD", None), ("GET", "bytes=0-0")]
    assert (meta.status, meta.etag, meta.last_modified, meta.length) == \
        (206, '"e2"', "Wed, 28 Oct 2026 10:54:49 GMT", 176172934)


def test_head_error_status_is_fetch_error():
    server = Server(Resp(404))
    with pytest.raises(net.FetchError, match="404"):
        net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).head("https://bank.example/x.apk")


def test_download_writes_file_and_meta(tmp_path):
    body = b"0123456789" * 300_000
    server = Server(Resp(200, {"Content-Length": str(len(body)), "Content-Type": "application/octet-stream"}, body))
    meta = net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).download("https://bank.example/x.apk",
                                                                              tmp_path / "x.apk")
    assert (tmp_path / "x.apk").read_bytes() == body
    assert (meta.length, meta.content_type) == (len(body), "application/octet-stream")


def test_download_stops_over_limit(tmp_path):
    server = Server(Resp(200, {}, b"x" * 11))
    with pytest.raises(net.FetchError):
        net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).download(
            "https://bank.example/x.apk", tmp_path / "x.apk", max_bytes=10)
    assert not (tmp_path / "x.apk").exists()


def test_download_refuses_declared_oversize_and_truncated(tmp_path):
    big = Server(Resp(200, {"Content-Length": str(net.MAX_APK + 1)}, b""))
    with pytest.raises(net.FetchError, match="больше"):
        net.Http(resolve=fake_resolve("8.8.8.8"), connect=big).download("https://bank.example/x.apk", tmp_path / "a")
    short = Server(Resp(200, {"Content-Length": "100"}, b"x" * 50))
    with pytest.raises(net.FetchError, match="не полностью"):
        net.Http(resolve=fake_resolve("8.8.8.8"), connect=short).download("https://bank.example/x.apk", tmp_path / "b")
    assert list(tmp_path.iterdir()) == []


def test_connects_to_vetted_ip_with_host_for_tls():
    server = Server(Resp(200))
    net.Http(resolve=fake_resolve("8.8.8.8"), connect=server).head("https://bank.example/x.apk")
    host, ip, method, target, _, headers = server.requests[0]
    assert (host, ip, method, target) == ("bank.example", "8.8.8.8", "HEAD", "/x.apk")
    assert headers["User-Agent"].startswith("Mozilla/5.0")    # ВТБ отвечает 403 на User-Agent curl


def test_json_get_and_post():
    server = Server(Resp(200, {}, b'{"code": "OK"}'), Resp(200, {}, b'{"body": 1}'))
    http = net.Http(resolve=fake_resolve("8.8.8.8"), connect=server)
    assert http.json("https://api.example/a?q=1", headers={"X-Ver": "1"}) == {"code": "OK"}
    assert http.json("https://api.example/b", body={"appId": 5}) == {"body": 1}
    (_, _, m1, t1, b1, h1), (_, _, m2, _, b2, h2) = server.requests
    assert (m1, t1, b1, h1["X-Ver"]) == ("GET", "/a?q=1", None, "1")
    assert (m2, json.loads(b2), h2["Content-Type"]) == ("POST", {"appId": 5}, "application/json")


def test_json_errors():
    for resp in (Resp(400, {}, b'{"code": "BAD"}'), Resp(200, {}, b"not json"),
                 Resp(200, {}, b"[" * (net.MAX_JSON + 1))):
        with pytest.raises(net.FetchError):
            net.Http(resolve=fake_resolve("8.8.8.8"), connect=Server(resp)).json("https://api.example/a")


def test_unresolvable_name_is_fetch_error():
    def resolve(*a):
        raise socket.gaierror("no such name")
    with pytest.raises(net.FetchError, match="не найден"):
        net.Http(resolve=resolve, connect=Server()).head("https://nope.example/x.apk")
