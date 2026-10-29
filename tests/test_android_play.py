"""Клиент Google Play (spec 2026-10-28 §6.4) на записанных ответах tests/data/play/ и подставном net.Http."""
import json
import os
import urllib.parse
from dataclasses import replace
from pathlib import Path

import pytest

from appshelf.android import net, pb, play

DATA = Path(__file__).parent / "data" / "play"
GID = 7
DEVICE = {"gsf": "3a9f0c11d2e4b567", "consistency": "cons-token", "config_token": "config-token",
          "email": "user@example.com"}       # адрес — для запроса токена Play: Email в нём обязателен
TOKEN_OK = b"Auth=ya29.play-token\nExpiry=1790003600\nExpiresInDurationSec=3600\nissueAdvice=auto\n"
OAUTH = "oauth2_4/0AVGzR1-secret-oauth"
AAS = "aas_et/AKppINsecret-master"


def read(name: str) -> bytes:
    return (DATA / name).read_bytes()


class FakeHttp:
    """net.Http для play: ответы по (метод, адрес без запроса) по очереди; запросы — в .calls."""

    def __init__(self):
        self.routes, self.calls, self.file, self.kw = {}, [], b"", {}

    def on(self, method: str, url: str, status: int, body: bytes):
        self.routes.setdefault((method, url), []).append((status, body))
        return self

    def request(self, method, url, *, headers=None, body=None, max_bytes=None):
        self.calls.append((method, url, headers or {}, body))
        queue = self.routes.get((method, url.split("?")[0]))
        if not queue:
            raise AssertionError(f"нет ответа на {method} {url}")
        status, data = queue.pop(0)
        return status, {}, data

    def download(self, url, dest, *, max_bytes=net.MAX_APK, headers=None):
        self.calls.append(("DOWNLOAD", url, headers or {}, None))
        dest.write_bytes(self.file)
        return net.Meta(url, 200, "", "", len(self.file), "application/vnd.android.package-archive")

    def query(self, i: int) -> dict:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.calls[i][1]).query))

    def form(self, i: int) -> dict:
        return dict(urllib.parse.parse_qsl(self.calls[i][3].decode()))


def fdfe(name: str) -> str:
    return f"{play.FDFE}/{name}"


def connect_files(cfg, gid=GID, aas=AAS):
    d = play.token_dir(cfg, gid)
    d.mkdir(parents=True)
    (d / "aas").write_text(aas)
    (d / "device.json").write_text(json.dumps(DEVICE))


@pytest.fixture(autouse=True)
def no_cached_tokens():
    play._TOKENS.clear()
    yield
    play._TOKENS.clear()


@pytest.fixture
def client(cfg, clock):
    """Подключённый аккаунт GID: файлы токена на месте, http — FakeHttp с выдачей токена Play."""
    connect_files(cfg)
    http = FakeHttp().on("POST", play.AUTH_URL, 200, TOKEN_OK)

    def factory(extra_ca=None, **kw):
        http.kw = kw
        return http
    c = play.PlayClient(cfg, GID, "", http_factory=factory, clock=lambda: clock.t)
    c.fake = http
    return c


def test_details_fixture_free_and_paid(client):
    http = client.fake.on("GET", fdfe("details"), 200, read("details_free_single.bin"))
    app = client.details("com.fsck.k9")
    assert (app.package, app.name, app.version_code, app.version_name, app.paid, app.owned) == \
        ("com.fsck.k9", "K-9 Mail", 39046, "24.0", False, True)
    assert app.icon_url.startswith("https://play-lh.googleusercontent.com/")
    method, url, headers, _ = http.calls[1]
    assert (method, http.query(1)) == ("GET", {"doc": "com.fsck.k9"})
    assert headers["Authorization"] == "Bearer ya29.play-token" and headers["X-DFE-Device-Id"] == DEVICE["gsf"]
    assert headers["X-DFE-Device-Config-Token"] == "config-token" and headers["User-Agent"].startswith("Android-Finsky/")
    # платное: владение — доставкой без «покупки» (status 2/3 — нет); купить её запросом сервер не пробует
    http.on("GET", fdfe("details"), 200, read("details_paid.bin"))
    http.on("GET", fdfe("delivery"), 200, read("delivery_paid_not_owned.bin"))
    paid = client.details("com.teslacoilsw.launcher.prime")
    assert (paid.paid, paid.owned, paid.version_code) == (True, False, 8)
    assert [c[0] for c in http.calls[2:]] == ["GET", "GET"] and "dtok" not in http.query(3)
    http.on("GET", fdfe("details"), 200, read("details_paid.bin"))
    http.on("GET", fdfe("delivery"), 200, read("delivery_single.bin"))     # status 1 — куплено
    assert client.details("com.teslacoilsw.launcher.prime").owned is True
    assert sum(c[1] == play.AUTH_URL for c in http.calls) == 1                 # токен Play — из памяти


def test_delivery_fixture_single_and_split(client, tmp_path):
    http = client.fake.on("POST", fdfe("purchase"), 200, read("purchase_free.bin"))
    http.on("GET", fdfe("delivery"), 200, read("delivery_single.bin"))
    app = play.PlayApp("com.fsck.k9", "K-9 Mail", 39046, "24.0", False, True, "")
    d = client.delivery(app)
    assert d.url == "https://play.googleapis.com/download/by-token/download?token=TEST-URL-TOKEN"
    assert (d.headers, d.splits, d.extra_files) == ({"Cookie": "a=cookie-value"}, 0, 0)
    assert d.sha256 == "Idb_zV-cQuMJpV7XyyIlW4YahLLIvWkBXgWmQssyOAM"
    assert http.form(1) == {"ot": "1", "doc": "com.fsck.k9", "vc": "39046"}       # бесплатное — «покупка» за 0
    assert http.query(2)["dtok"] == "T" * 119 and http.query(2)["vc"] == "39046"
    http.on("POST", fdfe("purchase"), 200, read("purchase_free.bin"))
    http.on("GET", fdfe("delivery"), 200, read("delivery_split.bin"))
    assert client.delivery(replace(app, package="org.videolan.vlc")).splits == 2


def test_download_checks_sha256(client, tmp_path):
    import base64
    import hashlib
    client.fake.file = b"APK"
    good = base64.urlsafe_b64encode(hashlib.sha256(b"APK").digest()).decode().rstrip("=")
    d = play.Delivery("https://play.googleapis.com/d?t=1", {"Cookie": "a=b"}, 0, 0, good)
    client.download(d, tmp_path / "a.apk")
    assert (tmp_path / "a.apk").read_bytes() == b"APK" and client.fake.calls[-1][2] == {"Cookie": "a=b"}
    with pytest.raises(play.PlayError, match="контрольн"):
        client.download(replace(d, sha256="A" * 43), tmp_path / "b.apk")
    assert not (tmp_path / "b.apk").exists()


def test_purchases_fixture(client):
    # формы ответа purchaseHistory (G3 ещё не проверен) — как разбирает Aurora: listResponse.item.subItem[].subItem[]
    def item(pkg, title, micros, vc):
        return [(1, pkg), (5, title), (8, [(1, micros), (2, "USD")]), (13, [(1, [(3, vc), (14, pkg)])])]
    body = pb.encode([(1, [(1, [(2, [(11, [(11, item("com.paid.a", "Платное А", 990000, 12)),
                                            (11, item("com.free.b", "Бесплатное Б", 0, 3))])])])])])
    client.fake.on("GET", fdfe("purchaseHistory"), 200, body)
    client.fake.on("GET", fdfe("purchaseHistory"), 200, pb.encode([(1, [(1, [])])]))   # дальше пусто
    got = client.purchases()
    assert [(a.package, a.name, a.paid, a.owned, a.version_code) for a in got] == \
        [("com.paid.a", "Платное А", True, True, 12), ("com.free.b", "Бесплатное Б", False, True, 3)]
    assert client.fake.query(1) == {"o": "0"} and client.fake.query(2) == {"o": "2"}


def test_auth_failure_is_play_auth_error(cfg, clock):
    connect_files(cfg)
    http = FakeHttp().on("POST", play.AUTH_URL, 403, read("auth_error.txt").split(b"\n", 1)[1])
    c = play.PlayClient(cfg, GID, "", http_factory=lambda *a, **kw: http, clock=lambda: clock.t)
    with pytest.raises(play.PlayAuthError):                       # мастер-токен отозван (смена пароля)
        c.details("com.fsck.k9")
    # токен Play не принят: один раз берём новый, второй отказ — истекло
    http.on("POST", play.AUTH_URL, 200, TOKEN_OK).on("POST", play.AUTH_URL, 200, TOKEN_OK)
    http.on("GET", fdfe("details"), 401, read("details_unauthorized.bin"))
    http.on("GET", fdfe("details"), 401, read("details_unauthorized.bin"))
    with pytest.raises(play.PlayAuthError):
        c.details("com.fsck.k9")
    http.on("POST", play.AUTH_URL, 200, TOKEN_OK).on("POST", play.AUTH_URL, 200, TOKEN_OK)
    http.on("GET", fdfe("details"), 401, read("details_unauthorized.bin"))
    http.on("GET", fdfe("details"), 200, read("details_free_single.bin"))
    assert c.details("com.fsck.k9").version_code == 39046          # протух в памяти — новый, и дальше
    (play.token_dir(cfg, GID) / "aas").unlink()
    play._TOKENS.clear()
    with pytest.raises(play.PlayAuthError):                       # отключили, пока шло задание
        c.details("com.fsck.k9")


def test_token_cached_until_expiry(client, clock):
    http = client.fake
    for _ in range(3):
        http.on("GET", fdfe("details"), 200, read("details_free_single.bin"))
    client.details("com.fsck.k9")
    client.details("com.fsck.k9")
    clock.advance(3600)                                            # срок из ответа вышел
    http.on("POST", play.AUTH_URL, 200, TOKEN_OK.replace(b"1790003600", b"1790007200"))
    client.details("com.fsck.k9")
    assert [c[1] for c in http.calls].count(play.AUTH_URL) == 2


@pytest.mark.parametrize("status,body", [
    (200, b"\xff\xff\xff"),                                        # не protobuf
    (200, read("details_free_single.bin")[:200]),                  # обрезан
    (200, pb.encode([(1, [(2, [(3, "нет item")])])])),             # нет item
    (200, pb.encode([(1, [(2, [(4, [(1, "com.fsck.k9"), (13, [(1, [(3, "строка")])])])])])])),  # не тот тип
    (500, b""),
])
def test_unexpected_response_is_play_error(client, status, body):
    client.fake.on("GET", fdfe("details"), status, body)
    with pytest.raises(play.PlayError) as e:
        client.details("com.fsck.k9")
    assert not isinstance(e.value, play.PlayAuthError) and str(e.value)
    if status == 500:
        assert "500" in str(e.value)


def test_unexpected_delivery_and_removed(client):
    http = client.fake
    http.on("GET", fdfe("details"), 404, read("details_removed.bin"))
    with pytest.raises(play.PlayGone):
        client.details("ru.sberbankmobile")
    app = play.PlayApp("com.fsck.k9", "K-9 Mail", 39046, "24.0", False, True, "")
    http.on("POST", fdfe("purchase"), 200, pb.encode([(1, [(4, [(1, [])])])]))   # нет токена доставки
    with pytest.raises(play.PlayError):
        client.delivery(app)
    for status in (3, 2, 7):
        http.on("POST", fdfe("purchase"), 200, read("purchase_free.bin"))
        http.on("GET", fdfe("delivery"), 200, pb.encode([(1, [(21, [(1, status)])])]))
        with pytest.raises(play.PlayError) as e:
            client.delivery(app)
        assert isinstance(e.value, play.PlayGone) == (status == 7)
    http.on("POST", fdfe("purchase"), 200, read("purchase_free.bin"))
    http.on("GET", fdfe("delivery"), 200, pb.encode([(1, [(21, [(1, 1), (2, [(1, 5)])])])]))  # без адреса
    with pytest.raises(play.PlayError):
        client.delivery(app)


def checkin_reply() -> bytes:
    return pb.encode([(1, True)]) + pb.varint(7 << 3 | 1) + (0x3A9F0C11D2E4B567).to_bytes(8, "little") \
        + pb.encode([(12, "cons-token")])


def connect_http(exchange: bytes, status: int = 200, upload_status: int = 200) -> FakeHttp:
    return (FakeHttp().on("POST", play.CHECKIN_URL, 200, checkin_reply())
            .on("POST", play.AUTH_URL, status, exchange)
            .on("POST", fdfe("uploadDeviceConfig"), upload_status,
                pb.encode([(1, [(28, [(1, "config-token")])])]) if upload_status == 200 else b""))


def exchange_ok() -> bytes:
    return read("exchange_ok.txt").split(b"\n", 1)[1].replace(b"aas_et/TEST-MASTER-TOKEN", AAS.encode())


def test_connect_refuses_other_email(cfg):
    http = connect_http(exchange_ok())
    with pytest.raises(play.PlayError, match="другой Google-аккаунт"):
        play.connect(cfg, GID, "other@example.com", OAUTH, "", http_factory=lambda *a, **kw: http)
    assert not play.token_dir(cfg, GID).exists()
    http = connect_http(exchange_ok())
    assert play.connect(cfg, GID, "User@Example.com", OAUTH, "", http_factory=lambda *a, **kw: http) == ""
    d = play.token_dir(cfg, GID)
    assert (d / "aas").read_text() == AAS
    assert json.loads((d / "device.json").read_text()) == DEVICE
    form = http.form(1)
    assert (form["Token"], form["Email"], form["androidId"]) == (OAUTH, "User@Example.com", DEVICE["gsf"])
    assert (form["droidguard_results"], form["service"], form["ACCESS_TOKEN"]) == ("null", "ac2dm", "1")
    assert pb.path(http.calls[0][3], 4, 1, 9) == [b"tegu"]                     # чек-ин — профиль Pixel 9a
    assert http.calls[2][2]["X-DFE-Device-Id"] == DEVICE["gsf"]


def test_connect_failure_leaves_no_files(cfg):
    for http in (connect_http(read("exchange_error.txt").split(b"\n", 1)[1], 403),       # токен не принят
                 connect_http(b"Email=user@example.com\n"),                               # без мастер-токена
                 connect_http(exchange_ok(), upload_status=500)):                         # профиль не принят
        with pytest.raises(play.PlayError):
            play.connect(cfg, GID, "user@example.com", OAUTH, "", http_factory=lambda *a, **kw: http)
        assert not play.token_dir(cfg, GID).exists()


@pytest.mark.skipif(os.name != "posix", reason="права файлов — как на сервере (Linux)")
def test_token_file_mode_0600(cfg):
    http = connect_http(exchange_ok())
    play.connect(cfg, GID, "user@example.com", OAUTH, "", http_factory=lambda *a, **kw: http)
    d = play.token_dir(cfg, GID)
    assert (d.stat().st_mode & 0o777, (d / "aas").stat().st_mode & 0o777,
            (d / "device.json").stat().st_mode & 0o777) == (0o700, 0o600, 0o600)


def test_token_never_in_exception_text(cfg, clock):
    texts = []
    http = connect_http(read("exchange_error.txt").split(b"\n", 1)[1], 403)
    with pytest.raises(play.PlayError) as e:
        play.connect(cfg, GID, "user@example.com", OAUTH, "", http_factory=lambda *a, **kw: http)
    texts.append(str(e.value))
    connect_files(cfg)
    http = FakeHttp().on("POST", play.AUTH_URL, 403, b"Error=BadAuthentication")
    with pytest.raises(play.PlayAuthError) as e:
        play.PlayClient(cfg, GID, "", http_factory=lambda *a, **kw: http, clock=lambda: clock.t).details("x.y")
    texts.append(str(e.value))
    http = FakeHttp().on("POST", play.AUTH_URL, 200, TOKEN_OK).on("GET", fdfe("details"), 401, b"")
    http.on("POST", play.AUTH_URL, 200, TOKEN_OK).on("GET", fdfe("details"), 401, b"")
    with pytest.raises(play.PlayAuthError) as e:
        play.PlayClient(cfg, GID, "", http_factory=lambda *a, **kw: http, clock=lambda: clock.t).details("x.y")
    texts.append(str(e.value))
    for t in texts:
        assert t and OAUTH not in t and AAS not in t and "ya29" not in t and "oauth2_4" not in t and "aas_et" not in t


def test_proxy_name_resolved_from_config(cfg, clock):
    cfg = replace(cfg, proxies={"de": "socks5h://u:p@de.example:1080"})
    connect_files(cfg)
    seen = []

    def factory(extra_ca=None, **kw):
        seen.append(kw)
        return FakeHttp()
    play.PlayClient(cfg, GID, "de", http_factory=factory, clock=lambda: clock.t)
    play.PlayClient(cfg, GID, "", http_factory=factory, clock=lambda: clock.t)
    assert seen == [{"proxy": "socks5h://u:p@de.example:1080", "proxy_name": "de"}, {}]
    with pytest.raises(play.PlayError, match="прокси «us» не настроен"):
        play.PlayClient(cfg, GID, "us", http_factory=factory, clock=lambda: clock.t)
