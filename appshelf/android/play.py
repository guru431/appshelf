"""Google Play (spec 2026-10-28 §6.4): свой небольшой клиент протокола /fdfe — по заметкам живой проверки 2026-10-29
(вики проекта, google-play-protocol). Подключение: oauth_token со страницы Google → мастер-токен (AAS) только файлом
token_dir/aas (0600, каталог 0700) и профиль «устройства» рядом (device.json); токен Play — в памяти процесса до
срока из ответа. Ни один токен — не в тексте ошибок, журнале и argv. Запросы — только по адресам ниже, через прокси
аккаунта, если он задан (net.Http(proxy=…)): сбой прокси — ошибка, а не прямое соединение."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from . import net, pb
from .net import SourceError

BASE = "https://android.clients.google.com"
AUTH_URL = f"{BASE}/auth"
CHECKIN_URL = f"{BASE}/checkin"
FDFE = f"{BASE}/fdfe"
CALLER_SIG = "38918a453d07199354f8b19af05ec6562ced5788"   # так называет себя Play Store в обмене токенов
PLAY_SCOPE = "oauth2:https://www.googleapis.com/auth/googleplay"
FORM = "application/x-www-form-urlencoded"
OFFER_TYPE = 1               # у всех проверенных приложений, бесплатных и платных
PURCHASE_PAGES = 20          # purchaseHistory — страницами; больше — обрезаем, а не крутимся вечно
TOKEN_MARGIN = 60            # токен Play, которому меньше минуты, — уже нет
UNEXPECTED = "Google Play ответил непонятно — возможно, сменился протокол"
GONE = "нет в Google Play: удалено или такого пакета нет"
NOT_CONNECTED = "подключение к Google Play истекло — подключите аккаунт заново"

# «Устройство» — Pixel 9a на Android 15: Play отдаёт сборку, совместимую с ним. Возможности — стандартные имена
# Android (developer.android.com/reference/android/content/pm/PackageManager); список телефонный, не исчерпывающий
BUILD = {"fingerprint": "google/tegu/tegu:15/BD4A.250405.003/13238919:user/release-keys", "id": "BD4A.250405.003",
         "hardware": "tegu", "brand": "google", "device": "tegu", "product": "tegu", "model": "Pixel 9a",
         "manufacturer": "Google", "sdk": 35, "release": "15"}
GMS_VERSION = 251333035
VENDING_VERSION, VENDING_CODE = "45.8.21-31 [0] [PR] 747433787", 84582130
PLATFORMS = ("arm64-v8a",)
LOCALES = ("en_US", "en_GB", "ru_RU", "de_DE", "fr_FR", "es_ES", "it_IT", "pt_BR", "tr_TR", "uk_UA", "kk_KZ", "zh_CN")
LIBRARIES = ("android.ext.shared", "android.hidl.base-V1.0-java", "android.hidl.manager-V1.0-java",
             "android.net.ipsec.ike", "android.test.base", "android.test.mock", "android.test.runner",
             "com.android.future.usb.accessory", "com.android.location.provider", "com.android.media.remotedisplay",
             "com.android.mediadrm.signer", "com.android.nfc_extras", "com.google.android.gms", "com.google.android.maps",
             "javax.obex", "org.apache.http.legacy")
GL_EXTENSIONS = ("GL_EXT_color_buffer_float", "GL_EXT_texture_filter_anisotropic", "GL_KHR_texture_compression_astc_ldr",
                 "GL_OES_EGL_image", "GL_OES_EGL_image_external", "GL_OES_compressed_ETC1_RGB8_texture",
                 "GL_OES_depth24", "GL_OES_packed_depth_stencil", "GL_OES_rgb8_rgba8", "GL_OES_texture_npot",
                 "GL_OES_vertex_array_object")
FEATURES = tuple(f"android.hardware.{f}" for f in (
    "audio.low_latency", "audio.output", "audio.pro", "bluetooth", "bluetooth_le", "camera", "camera.any",
    "camera.autofocus", "camera.capability.manual_post_processing", "camera.capability.manual_sensor",
    "camera.capability.raw", "camera.flash", "camera.front", "camera.level.full", "faketouch", "fingerprint",
    "location", "location.gps", "location.network", "microphone", "nfc", "nfc.any", "nfc.hce", "opengles.aep",
    "ram.normal", "screen.landscape", "screen.portrait", "sensor.accelerometer", "sensor.barometer",
    "sensor.compass", "sensor.gyroscope", "sensor.light", "sensor.proximity", "sensor.stepcounter",
    "sensor.stepdetector", "strongbox_keystore", "telephony", "telephony.calling", "telephony.data",
    "telephony.euicc", "telephony.gsm", "telephony.ims", "telephony.messaging", "telephony.radio.access",
    "telephony.subscription", "touchscreen", "touchscreen.multitouch", "touchscreen.multitouch.distinct",
    "touchscreen.multitouch.jazzhand", "usb.accessory", "usb.host", "vulkan.compute", "vulkan.level",
    "vulkan.version", "wifi", "wifi.aware", "wifi.direct", "wifi.passpoint", "wifi.rtt")) + tuple(
    f"android.software.{f}" for f in (
        "activities_on_secondary_displays", "app_widgets", "autofill", "backup", "companion_device_setup",
        "connectionservice", "credentials", "cts", "device_admin", "file_based_encryption", "home_screen",
        "input_methods", "ipsec_tunnels", "live_wallpaper", "managed_users", "midi", "picture_in_picture", "print",
        "secure_lock_screen", "securely_removes_users", "verified_boot", "voice_recognizers", "webview"))

_TOKENS: dict[int, tuple[str, float]] = {}   # токен Play по Google-аккаунту: (токен, до какого времени)
_LOCK = threading.Lock()


class PlayError(SourceError):
    """Google Play не отдал приложение или ответил не то; текст — для карточки, без токенов."""


class PlayAuthError(PlayError):
    """Токен не принят (сменили пароль, отозвали доступ, отключили): подключение истекло."""


class PlayGone(PlayError):
    """Приложения нет в Google Play (удалено или такого пакета нет)."""


@dataclass(frozen=True)
class PlayApp:
    package: str
    name: str
    version_code: int
    version_name: str
    paid: bool
    owned: bool       # бесплатное — всегда; платное — куплено этим аккаунтом
    icon_url: str


@dataclass(frozen=True)
class Delivery:
    url: str
    headers: dict[str, str]   # cookie скачивания
    splits: int               # частей набора сверх base
    extra_files: int          # OBB и пакеты ресурсов
    sha256: str = ""          # base64url без «=», как отдаёт Play


def token_dir(cfg, gid: int) -> Path:
    return cfg.google_dir / str(gid)


def forget(gid: int) -> None:
    """Токен Play из памяти прочь: аккаунт переподключили, отключили или удалили."""
    with _LOCK:
        _TOKENS.pop(gid, None)


def _http(cfg, proxy: str, factory):
    """proxy — имя из APPSHELF_PROXIES; '' — напрямую."""
    if not proxy:
        return factory(cfg.sources_ca)
    url = cfg.proxies.get(proxy)
    if url is None:
        raise PlayError(f"прокси «{proxy}» не настроен в APPSHELF_PROXIES")
    return factory(cfg.sources_ca, proxy=url, proxy_name=proxy)


@contextmanager
def _parsing():
    """Разбор ответа: не тот формат — PlayError, а не PbError, KeyError или TypeError."""
    try:
        yield
    except (pb.PbError, UnicodeDecodeError, IndexError, TypeError, ValueError):
        raise PlayError(UNEXPECTED) from None


def _one(vals: list):
    if not vals:
        raise pb.PbError("нет поля")
    return vals[0]


def _int(vals: list, default: int | None = None) -> int:
    if not vals and default is not None:
        return default
    v = _one(vals)
    if not isinstance(v, int):
        raise pb.PbError("не число")
    return v


def _text(vals: list) -> str:
    v = vals[0] if vals else b""
    if not isinstance(v, bytes):
        raise pb.PbError("не строка")
    return v.decode("utf-8")


# --- устройство и токены --------------------------------------------------------

def _device_config() -> list:
    return [(1, 3), (2, 1), (3, 1), (4, 2), (5, False), (6, False), (7, 420), (8, 196610),
            *[(9, x) for x in LIBRARIES], *[(10, x) for x in FEATURES], *[(11, x) for x in PLATFORMS],
            (12, 1080), (13, 2424), *[(14, x) for x in LOCALES], *[(15, x) for x in GL_EXTENSIONS],
            *[(26, [(1, x), (2, 0)]) for x in FEATURES]]


def _finsky_ua() -> str:
    b = BUILD
    return (f"Android-Finsky/{VENDING_VERSION} (api=3,versionCode={VENDING_CODE},sdk={b['sdk']},device={b['device']},"
            f"hardware={b['hardware']},product={b['product']},platformVersionRelease={b['release']},"
            f"model={b['model']},buildId={b['id']},isWideScreen=0,supportedAbis={';'.join(PLATFORMS)})")


def _auth_headers(gsf: str) -> dict:
    h = {"app": "com.google.android.gms", "User-Agent": f"GoogleAuth/1.4 ({BUILD['device']} {BUILD['id']})",
         "Content-Type": FORM}
    if gsf:
        h["device"] = gsf
    return h


def _dfe_headers(dev: dict, token: str = "") -> dict:
    h = {"User-Agent": _finsky_ua(), "Accept-Language": "en-US", "X-DFE-Client-Id": "am-android-google",
         "X-DFE-Network-Type": "4", "X-DFE-UserLanguages": "en_US", "X-DFE-Device-Id": dev["gsf"],
         "X-DFE-Device-Checkin-Consistency-Token": dev.get("consistency", "")}
    if dev.get("config_token"):
        h["X-DFE-Device-Config-Token"] = dev["config_token"]
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _base_params(dev: dict, email: str) -> dict:
    return {"androidId": dev["gsf"], "sdk_version": str(BUILD["sdk"]), "Email": email,
            "google_play_services_version": str(GMS_VERSION), "device_country": "us", "lang": "en",
            "callerSig": CALLER_SIG, "droidguard_results": "null"}


def _auth(http, params: dict, gsf: str) -> tuple[int, dict[str, str]]:
    """POST /auth: ответ — строки «ключ=значение»."""
    status, _, body = http.request("POST", AUTH_URL, headers=_auth_headers(gsf),
                                   body=urllib.parse.urlencode(params).encode())
    reply = {}
    for line in body.decode("utf-8", "replace").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            reply[k] = v
    return status, reply


def _checkin(http, now: float) -> dict:
    """Анонимный чек-ин «устройства»: GSF ID и токен согласованности — один раз на аккаунт, при подключении."""
    build = [(1, BUILD["fingerprint"]), (2, BUILD["hardware"]), (3, BUILD["brand"]), (6, "android-google"),
             (7, int(now)), (8, GMS_VERSION), (9, BUILD["device"]), (10, BUILD["sdk"]), (11, BUILD["model"]),
             (12, BUILD["manufacturer"]), (13, BUILD["product"]), (14, False)]
    req = pb.encode([(2, 0), (4, [(1, build), (2, 0), (8, "mobile-notroaming"), (9, 0)]), (6, "en_US"),
                     (12, "UTC"), (14, 3), (18, _device_config()), (20, 0)])
    status, _, data = http.request("POST", CHECKIN_URL, headers={**_auth_headers(""),
                                                                 "Content-Type": "application/x-protobuf"}, body=req)
    if status != 200:
        raise PlayError(f"Google не принял регистрацию устройства ({status})")
    with _parsing():
        return {"gsf": format(_int(pb.path(data, 7)), "x"), "consistency": _text(pb.path(data, 12))}


def _upload_config(http, dev: dict) -> str:
    status, _, data = http.request("POST", f"{FDFE}/uploadDeviceConfig",
                                   headers={**_dfe_headers(dev), "Content-Type": "application/x-protobuf"},
                                   body=pb.encode([(1, _device_config())]))
    if status != 200:
        raise PlayError(f"Google Play не принял профиль устройства ({status})")
    with _parsing():
        return _text([_one(pb.path(data, 1, 28, 1))])


def _write_secret(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _save(d: Path, aas: str, dev: dict) -> None:
    """Каталог токена целиком — рядом и переименованием: прежнее подключение заменяется, недописанного не бывает."""
    d.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = d.with_name(d.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(mode=0o700)
    try:
        os.chmod(tmp, 0o700)
        _write_secret(tmp / "aas", aas)
        _write_secret(tmp / "device.json", json.dumps(dev))
        shutil.rmtree(d, ignore_errors=True)
        os.replace(tmp, d)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def connect(cfg, gid: int, email: str, oauth_token: str, proxy: str, *, http_factory=net.Http,
            clock=time.time) -> str:
    """oauth_token (со страницы Google, ~10 минут, один раз) → мастер-токен в token_dir/aas. Адрес из ответа ≠
    email — PlayError, и ничего не сохранено. Возвращает страну аккаунта; ответы Play её не называют — ''."""
    http = _http(cfg, proxy, http_factory)
    dev = _checkin(http, clock())
    status, reply = _auth(http, {**_base_params(dev, email), "service": "ac2dm", "add_account": "1",
                                 "get_accountid": "1", "ACCESS_TOKEN": "1", "callerPkg": "com.google.android.gms",
                                 "Token": oauth_token}, dev["gsf"])
    aas = reply.get("Token", "")
    if status == 403 or reply.get("Error"):
        raise PlayError("Google не принял oauth_token — получите новый: он живёт около 10 минут и годится один раз")
    if status != 200 or not aas:
        raise PlayError(f"Google ответил без мастер-токена ({status})")
    account = reply.get("Email", "").strip().lower()
    if account != email.strip().lower():
        raise PlayError("это другой Google-аккаунт: на странице Google войдите тем же аккаунтом")
    dev["config_token"] = _upload_config(http, dev)
    dev["email"] = account
    _save(token_dir(cfg, gid), aas, dev)
    forget(gid)
    return ""


# --- клиент ------------------------------------------------------------------------

class PlayPool:
    """Клиенты Play для конвейера (Sources(play=…)): по Google-аккаунту (people.GoogleAccount) с его прокси; без
    действующего подключения — PlayAuthError, задание ждёт."""

    def __init__(self, cfg, *, http_factory=net.Http):
        self.cfg, self.http_factory = cfg, http_factory

    def client(self, acct) -> PlayClient:
        if acct.play != "ok":
            raise PlayAuthError(NOT_CONNECTED)
        return PlayClient(self.cfg, acct.id, acct.proxy, http_factory=self.http_factory)


class PlayClient:
    """Запросы к Play от имени подключённого Google-аккаунта gid; proxy — имя из APPSHELF_PROXIES."""

    def __init__(self, cfg, gid: int, proxy: str, *, http_factory=net.Http, clock=time.time):
        self.cfg, self.gid, self.clock = cfg, gid, clock
        self.http = _http(cfg, proxy, http_factory)
        self._dev: dict | None = None

    def _device(self) -> dict:
        if self._dev is None:
            try:
                self._dev = json.loads((token_dir(self.cfg, self.gid) / "device.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise PlayAuthError(NOT_CONNECTED) from None
        return self._dev

    def _token(self, force: bool = False) -> str:
        now = self.clock()
        with _LOCK:
            cached = _TOKENS.get(self.gid)
        if cached and not force and cached[1] > now + TOKEN_MARGIN:
            return cached[0]
        try:
            aas = (token_dir(self.cfg, self.gid) / "aas").read_text(encoding="utf-8").strip()
        except OSError:
            raise PlayAuthError(NOT_CONNECTED) from None
        dev = self._device()
        status, reply = _auth(self.http, {**_base_params(dev, dev.get("email", "")), "app": "com.android.vending",
                                          "client_sig": CALLER_SIG, "callerPkg": "com.google.android.gms",
                                          "Token": aas, "oauth2_foreground": "1", "token_request_options": "CAA4AVAB",
                                          "check_email": "1", "system_partition": "1", "service": PLAY_SCOPE},
                              dev["gsf"])
        if status == 403 or reply.get("Error"):
            forget(self.gid)
            raise PlayAuthError(NOT_CONNECTED)
        token = reply.get("Auth", "")
        if status != 200 or not token:
            raise PlayError(f"Google не выдал токен Play ({status})")
        expiry = reply.get("Expiry", "")
        ttl = reply.get("ExpiresInDurationSec", "")
        until = int(expiry) if expiry.isdigit() else now + (int(ttl) if ttl.isdigit() else 3600)
        with _LOCK:
            _TOKENS[self.gid] = (token, until)
        return token

    def _fdfe(self, method: str, endpoint: str, query: dict | None = None, body: bytes | None = None,
              content_type: str = "") -> tuple[int, bytes, str]:
        """(статус, тело, текст ошибки Play). 401 — новый токен и ещё раз; снова 401 — подключение истекло."""
        url = f"{FDFE}/{endpoint}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        for attempt in range(2):
            headers = _dfe_headers(self._device(), self._token(force=attempt > 0))
            if content_type:
                headers["Content-Type"] = content_type
            status, _, data = self.http.request(method, url, headers=headers, body=body)
            if status != 401:
                break
        else:
            forget(self.gid)
            raise PlayAuthError(NOT_CONNECTED)
        try:
            err = _text(pb.path(data, 2, 2))
        except (pb.PbError, UnicodeDecodeError):
            err = ""
        return status, data, err

    def _ok(self, status: int, err: str) -> None:
        if status != 200:
            raise PlayError(f"Google Play ответил {status}" + (f": {err[:200]}" if err else ""))

    def details(self, package: str) -> PlayApp:
        status, data, err = self._fdfe("GET", "details", {"doc": package})
        if status == 404:
            raise PlayGone(GONE)
        self._ok(status, err)
        with _parsing():
            app = self._app(_one(pb.path(data, 1, 2, 4)))
        if app.paid:   # куплено ли — доставка без «покупки»: purchase для некупленного платного не зовём
            q = {"ot": OFFER_TYPE, "doc": app.package, "vc": app.version_code}
            status, data, err = self._fdfe("GET", "delivery", q)
            self._ok(status, err)
            with _parsing():
                app = replace(app, owned=_int(pb.path(data, 1, 21, 1), 0) == 1)
        return app

    @staticmethod
    def _app(item: bytes, listed: bool = False) -> PlayApp:
        """Item из details (версия обязательна) или из purchaseHistory (listed: версии может не быть): пакет —
        id, название, versionCode, цена, иконка (изображение типа 4). owned — True: платное уточняет details."""
        info = (pb.path(item, 13, 1) or [b""])[0] if listed else _one(pb.path(item, 13, 1))
        offer = pb.path(item, 8)
        micros = _int(pb.path(offer[0], 1), 0) if offer else 0
        icon = next((_text(pb.path(image, 5)) for image in pb.path(item, 10) if _int(pb.path(image, 1), 0) == 4), "")
        return PlayApp(_text([_one(pb.path(item, 1))]), _text(pb.path(item, 5)),
                       _int(pb.path(info, 3), 0 if listed else None), _text(pb.path(info, 4)), micros > 0, True, icon)

    def delivery(self, app: PlayApp) -> Delivery:
        """Адрес base APK и сколько ещё файлов в наборе. Бесплатное — сначала «покупка» за 0 (без неё Play отвечает
        «не получено»), платное — только если куплено (без purchase)."""
        q = {"ot": OFFER_TYPE, "doc": app.package, "vc": app.version_code}
        if not app.paid:
            status, data, err = self._fdfe("POST", "purchase", body=urllib.parse.urlencode(q).encode(),
                                           content_type=FORM)
            self._ok(status, err)
            with _parsing():
                q["dtok"] = _text([_one(pb.path(data, 1, 4, 55))])
        status, data, err = self._fdfe("GET", "delivery", q)
        self._ok(status, err)
        with _parsing():
            resp = _one(pb.path(data, 1, 21))
            code = _int(pb.path(resp, 1), 0)
            if code == 1:
                add = _one(pb.path(resp, 2))
                url = _text([_one(pb.path(add, 3))])
                cookies = [f"{_text(pb.path(c, 1))}={_text(pb.path(c, 2))}" for c in pb.path(add, 5)]
                return Delivery(url, {"Cookie": "; ".join(cookies)} if cookies else {}, len(pb.path(add, 15)),
                                len(pb.path(add, 4)), _text(pb.path(add, 19)))
        if code == 7:
            raise PlayGone(GONE)
        if code == 3:
            raise PlayError("платное — не куплено этим аккаунтом" if app.paid
                            else "Google Play не выдал приложение этому аккаунту")
        if code in (2, 9):
            raise PlayError("Google Play не отдаёт приложение этому устройству или стране аккаунта")
        raise PlayError(f"Google Play не отдал приложение (код {code})")

    def purchases(self) -> list[PlayApp]:
        """Купленное и полученное аккаунтом (purchaseHistory) — страницами по смещению."""
        out: dict[str, PlayApp] = {}
        offset = 0
        for _ in range(PURCHASE_PAGES):
            status, data, err = self._fdfe("GET", "purchaseHistory", {"o": offset})
            self._ok(status, err)
            with _parsing():
                page = [self._app(x, listed=True) for item in pb.path(data, 1, 1, 2)[:1]
                        for sub in pb.path(item, 11) for x in (pb.path(sub, 11) or [sub])]
            if not page:
                break
            for app in page:
                out.setdefault(app.package, app)
            offset += len(page)
        return list(out.values())

    def download(self, delivery: Delivery, dest: Path) -> None:
        """Base APK в dest; не совпал SHA-256 из доставки — файл удалён, PlayError."""
        self.http.download(delivery.url, dest, headers=delivery.headers)
        if delivery.sha256:
            h = hashlib.sha256()
            with open(dest, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            if base64.urlsafe_b64encode(h.digest()).decode().rstrip("=") != delivery.sha256.rstrip("="):
                dest.unlink(missing_ok=True)
                raise PlayError("файл из Google Play не совпал с контрольной суммой — скачается заново")
