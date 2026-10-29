"""HTTP для источников APK (spec 2026-10-28 §6.2): только https, только глобальные адреса, пределы.

Имя резолвится перед каждым запросом и после каждого редиректа; хоть один адрес не глобальный (частный, loopback,
link-local, multicast, зарезервированный) — отказ до соединения. Соединение — на проверенный адрес, сертификат и SNI —
по имени: повторный резолв внутри библиотеки не подменит адрес. Проверка сертификата не отключается; корни НУЦ и
недостающие промежуточные — только здесь, файлом APPSHELF_SOURCES_CA."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json as jsonlib
import socket
import ssl
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

MAX_APK = 1 << 30
MAX_JSON = 5 << 20
MAX_REDIRECTS = 5
CHUNK = 1 << 20
REDIRECTS = (301, 302, 303, 307, 308)
BLOCKED = "адрес недоступен для сервера: ссылка ведёт во внутреннюю сеть"
# ВТБ на User-Agent curl отвечает 403, на браузерный — файлом (живая проверка 2026-10-28)
USER_AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"


class FetchError(Exception):
    """Текст — для человека: почему источник не отдал файл."""


class BlockedAddress(FetchError):
    pass


class SourceError(Exception):
    """Источник ответил, но не то: неизвестный формат, платное, набор файлов. Здесь, а не в sources: его поднимает и
    rustore, которого sources импортирует."""


@dataclass(frozen=True)
class Meta:
    url: str              # после редиректов
    status: int
    etag: str
    last_modified: str
    length: int | None    # длина файла целиком (у ответа 206 — из Content-Range)
    content_type: str


def check_url(url: str) -> urllib.parse.SplitResult:
    try:
        u = urllib.parse.urlsplit(url.strip())
        u.port
    except ValueError:
        raise FetchError("ссылка не разбирается") from None
    if u.scheme != "https" or not u.hostname:
        raise FetchError("нужна ссылка https://…")
    if "@" in u.netloc:
        raise FetchError("ссылка с логином и паролем не годится")
    try:
        literal = ipaddress.ip_address(u.hostname.split("%")[0])
    except ValueError:
        return u
    if not _public(str(literal)):  # адрес прямо в ссылке — отказ без резолва (имена проверяет Http перед запросом)
        raise BlockedAddress(BLOCKED)
    return u


def _public(addr: str) -> bool:
    a = ipaddress.ip_address(addr)
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                                or a.is_reserved or a.is_unspecified)


def tunnel(proxy: str, host: str, port: int, timeout: float, dial=socket.create_connection, name: str = ""):
    """Сокет до host:port через прокси: http:// — CONNECT, socks5h:// — SOCKS5 с именем узла (RFC 1928/1929): имя
    резолвит прокси, локально — нет. Сбой — FetchError «прокси <имя> недоступен: …» без адреса и пароля прокси."""
    u = urllib.parse.urlsplit(proxy)
    user, password = urllib.parse.unquote(u.username or ""), urllib.parse.unquote(u.password or "")
    label = name or u.hostname

    def fail(reason: str):
        return FetchError(f"прокси {label} недоступен: {reason}")

    try:
        sock = dial((u.hostname, u.port), timeout)
    except OSError as e:
        raise fail(f"{e.__class__.__name__}: {e.strerror or e}") from None
    try:
        if u.scheme == "socks5h":
            _socks5(sock, host, port, user, password, fail)
        else:
            _connect_tunnel(sock, host, port, user, password, fail)
    except OSError as e:
        sock.close()
        raise fail(f"{e.__class__.__name__}: {e.strerror or e}") from None
    except FetchError:
        sock.close()
        raise
    return sock


def _recv_exact(sock, n: int, fail) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise fail("оборвал соединение")
        data += chunk
    return data


def _socks5(sock, host: str, port: int, user: str, password: str, fail) -> None:
    sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
    ver, method = _recv_exact(sock, 2, fail)
    if ver != 5 or method not in ((0, 2) if user else (0,)):
        raise fail("не принял способ входа")
    if method == 2:
        u, p = user.encode(), password.encode()
        sock.sendall(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
        if _recv_exact(sock, 2, fail)[1] != 0:
            raise fail("не принял логин и пароль")
    name = host.encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    ver, rep, _, atyp = _recv_exact(sock, 4, fail)
    if ver != 5 or rep != 0:
        raise fail(f"отказал в соединении (код {rep})")
    size = {1: 4, 4: 16}.get(atyp)
    if size is None:
        size = _recv_exact(sock, 1, fail)[0] if atyp == 3 else -1
    if size < 0:
        raise fail("ответил непонятно")
    _recv_exact(sock, size + 2, fail)                   # адрес и порт, которыми прокси вышел, — не нужны


def _connect_tunnel(sock, host: str, port: int, user: str, password: str, fail) -> None:
    auth = ""
    if user:
        auth = f"Proxy-Authorization: Basic {base64.b64encode(f'{user}:{password}'.encode()).decode()}\r\n"
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
    head = b""
    while not head.endswith(b"\r\n\r\n"):               # по байту: дальше ответа не читаем — там уже TLS
        head += _recv_exact(sock, 1, fail)
        if len(head) > 16384:
            raise fail("ответил непонятно")
    status = head.split(b"\r\n", 1)[0].split()
    if len(status) < 2 or not status[0].startswith(b"HTTP/") or status[1] != b"200":
        raise fail(f"ответил {status[1].decode('latin-1') if len(status) > 1 else '?'}")


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Сокет — к проверенному адресу (или через прокси), TLS — с именем хоста (SNI и проверка сертификата)."""

    def __init__(self, host: str, ip: str | None, port: int, ctx: ssl.SSLContext, timeout: float, opener):
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._ip, self._ctx, self._opener = ip, ctx, opener

    def connect(self):
        sock = self._opener(self.host, self._ip, self.port, self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


class Http:
    def __init__(self, extra_ca: Path | None = None, *, resolve=socket.getaddrinfo, connect=None,
                 timeout: float = 60.0, proxy: str = "", proxy_name: str = "", dial=socket.create_connection):
        """proxy — только для Google Play (адреса — константы клиента): с ним адрес не проверяется на «глобальный» и
        локально не резолвится, а сбой прокси — ошибка, не прямое соединение."""
        self.ctx = ssl.create_default_context()
        if extra_ca:
            try:
                self.ctx.load_verify_locations(cafile=str(extra_ca))
            except (ssl.SSLError, OSError) as e:  # необязательный файл не роняет службу: CDN под НУЦ — ошибка TLS
                print(f"appshelf: APPSHELF_SOURCES_CA {extra_ca} не загружен, только системные корни: {e}",
                      file=sys.stderr)
        self.resolve, self.timeout = resolve, timeout
        self.proxy, self.proxy_name, self.dial = proxy, proxy_name, dial
        self.connect = connect or (lambda host, ip, port, ctx, timeout:
                                   _PinnedHTTPS(host, ip, port, ctx, timeout, self._socket))

    def _socket(self, host: str, ip: str | None, port: int, timeout: float):
        if self.proxy:
            return tunnel(self.proxy, host, port, timeout, self.dial, self.proxy_name)
        return self.dial((ip, port), timeout)

    def head(self, url: str, headers: dict | None = None) -> Meta:
        """HEAD; 405/501 — GET с Range: bytes=0-0, тело не читается."""
        resp, conn, final = self._request("HEAD", url, headers or {})
        try:
            if resp.status in (405, 501):
                _close(resp, conn)
                resp, conn, final = self._request("GET", final, {**(headers or {}), "Range": "bytes=0-0"})
            if resp.status >= 400:
                raise FetchError(f"{urllib.parse.urlsplit(final).hostname} ответил {resp.status}")
            return _meta(final, resp)
        finally:
            _close(resp, conn)

    def json(self, url: str, *, headers: dict | None = None, body: dict | None = None) -> object:
        """GET, или POST с телом JSON."""
        data = None if body is None else jsonlib.dumps(body).encode()
        hdrs = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        resp, conn, final = self._request("GET" if data is None else "POST", url, hdrs, data)
        try:
            if resp.status != 200:
                raise FetchError(f"{urllib.parse.urlsplit(final).hostname} ответил {resp.status}")
            return _json_body(resp)
        finally:
            _close(resp, conn)

    def form(self, url: str, data: dict, *, headers: dict | None = None) -> object:
        """POST application/x-www-form-urlencoded, ответ — JSON; у статуса 400 тело тоже читается: OAuth кладёт в
        него error (вход через Google)."""
        hdrs = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        resp, conn, final = self._request("POST", url, hdrs, urllib.parse.urlencode(data).encode())
        try:
            if resp.status not in (200, 400):
                raise FetchError(f"{urllib.parse.urlsplit(final).hostname} ответил {resp.status}")
            return _json_body(resp)
        finally:
            _close(resp, conn)

    def request(self, method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
                max_bytes: int = MAX_JSON) -> tuple[int, dict[str, str], bytes]:
        """Ответ как есть — статус (любой), заголовки (имена в нижнем регистре), тело не больше max_bytes: для
        протоколов не на JSON (Google Play)."""
        resp, conn, _ = self._request(method, url, headers or {}, body)
        try:
            data = _read(resp, max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchError("ответ источника слишком большой")
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            _close(resp, conn)

    def download(self, url: str, dest: Path, *, max_bytes: int = MAX_APK, headers: dict | None = None) -> Meta:
        """Файл в dest кусками; ошибка или больше max_bytes — dest удалён, FetchError."""
        try:
            resp, conn, final = self._request("GET", url, headers or {})
            try:
                if resp.status != 200:
                    raise FetchError(f"{urllib.parse.urlsplit(final).hostname} ответил {resp.status}")
                meta = _meta(final, resp)
                if meta.length is not None and meta.length > max_bytes:
                    raise FetchError(f"файл больше {max_bytes >> 20} МБ")
                n = 0
                with open(dest, "wb") as f:
                    while chunk := _read(resp, CHUNK):
                        n += len(chunk)
                        if n > max_bytes:
                            raise FetchError(f"файл больше {max_bytes >> 20} МБ")
                        f.write(chunk)
                if meta.length is not None and n != meta.length:
                    raise FetchError("файл скачан не полностью")
                return meta
            finally:
                _close(resp, conn)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

    def _vetted_ip(self, host: str, port: int) -> str:
        try:
            addrs = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addrs = [info[4][0] for info in self.resolve(host, port, 0, socket.SOCK_STREAM)]
            except OSError:
                raise FetchError(f"адрес {host} не найден") from None
        if not addrs:
            raise FetchError(f"адрес {host} не найден")
        if not all(_public(a.split("%")[0]) for a in addrs):
            raise BlockedAddress(BLOCKED)
        return addrs[0]

    def _request(self, method: str, url: str, headers: dict, body: bytes | None = None):
        """(ответ, соединение, адрес после редиректов). Каждый шаг — check_url и проверка адресов."""
        for _ in range(MAX_REDIRECTS + 1):
            u = check_url(url)
            ip = None if self.proxy else self._vetted_ip(u.hostname, u.port or 443)
            conn = self.connect(u.hostname, ip, u.port or 443, self.ctx, self.timeout)
            target = (u.path or "/") + (f"?{u.query}" if u.query else "")
            try:
                conn.request(method, target, body, {"User-Agent": USER_AGENT, **headers})
                resp = conn.getresponse()
            except FetchError:  # прокси не пустил (tunnel)
                conn.close()
                raise
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                raise FetchError(f"{u.hostname}: {e.__class__.__name__}: {e}") from None
            location = resp.getheader("Location")
            if resp.status not in REDIRECTS or not location:
                return resp, conn, url
            _close(resp, conn)
            url = urllib.parse.urljoin(url, location)
            if resp.status in (301, 302, 303) and method == "POST":
                method, body = "GET", None
        raise FetchError(f"слишком много переадресаций (больше {MAX_REDIRECTS})")


def _read(resp, n: int) -> bytes:
    try:
        return resp.read(n)
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(f"обрыв при скачивании: {e.__class__.__name__}") from None


def _json_body(resp) -> object:
    raw = _read(resp, MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise FetchError("ответ источника слишком большой")
    try:
        return jsonlib.loads(raw)
    except ValueError:
        raise FetchError("источник ответил не JSON") from None


def _close(resp, conn) -> None:
    resp.close()
    conn.close()


def _meta(url: str, resp) -> Meta:
    length = None
    total = (resp.getheader("Content-Range") or "").rpartition("/")[2]
    if resp.status == 206 and total.isdigit():
        length = int(total)
    elif resp.status != 206 and (resp.getheader("Content-Length") or "").isdigit():
        length = int(resp.getheader("Content-Length"))
    return Meta(url=url, status=resp.status, etag=resp.getheader("ETag") or "",
                last_modified=resp.getheader("Last-Modified") or "", length=length,
                content_type=resp.getheader("Content-Type") or "")
