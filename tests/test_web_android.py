import io
import json
from dataclasses import replace

import pytest
from PIL import Image
from starlette.testclient import TestClient

from apk_factory import cert_sha, make_apk
from appshelf import people
from appshelf.android import db, fetch, net, rustore
from appshelf.android.sources import Latest
from appshelf.web.app import create_app
from helpers import HOST, make_account

UA_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0"
PKG = "ru.example.app"
URL = "https://bank.example/app.apk"


def work(web, conn):
    """Обработчик берёт одно Android-задание и выполняет его."""
    job = db.take_job(conn)
    assert job is not None
    fetch.run_job(web.app.state.w.env, conn, job)


def shelf(conn):
    return db.shelf_for_user(conn, 1)


def add_link(web, conn, tmp_path, url=URL, **apk):
    web.sources.apks[url] = make_apk(tmp_path / f"{len(web.sources.apks)}.apk", **apk)
    assert web.client.post("/android/add/link", data={"url": url, "name": ""},
                           follow_redirects=False).status_code == 303
    work(web, conn)


def test_default_section():
    from appshelf.web.android import default_section
    assert default_section(UA_ANDROID, "apple", None) == "android"
    assert default_section(UA_IPHONE, "android", None) == "apple"
    assert default_section(UA_DESKTOP, "android", None) == "android"
    assert default_section(UA_DESKTOP, None, None) == "apple"
    assert default_section(UA_ANDROID, None, "apple") == "apple"
    assert default_section(UA_IPHONE, None, "android") == "android"


def test_android_phone_opens_android_section(web):
    r = web.client.get("/", headers={"User-Agent": UA_ANDROID}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android")
    r = web.client.get("/android", headers={"User-Agent": UA_ANDROID})
    assert r.status_code == 200 and "Добавьте приложение" in r.text
    assert r.cookies.get("appshelf_os") == "android"


def test_iphone_stays_on_apple_and_desktop_follows_cookie(web):
    web.client.get("/android", headers={"User-Agent": UA_DESKTOP})          # открыт последним — Android
    assert web.client.get("/", headers={"User-Agent": UA_IPHONE}, follow_redirects=False).status_code == 200
    r = web.client.get("/", headers={"User-Agent": UA_DESKTOP}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android")
    web.client.get("/?os=apple", headers={"User-Agent": UA_DESKTOP})
    assert web.client.get("/", headers={"User-Agent": UA_DESKTOP}, follow_redirects=False).status_code == 200


def test_explicit_apple_on_android_phone_shows_apple(web):
    r = web.client.get("/?os=apple", headers={"User-Agent": UA_ANDROID}, follow_redirects=False)
    assert r.status_code == 200 and "Каталог пуст" in r.text
    assert r.cookies.get("appshelf_os") == "apple"
    assert 'href="/android"' in r.text and 'href="/?os=apple"' in r.text      # переключатель Apple/Android


def test_add_from_rustore_search_and_install_link(web, conn, tmp_path):
    web.sources.found = [rustore.StoreApp(1, PKG, "Пример Банк", "", 1, "", 0, "", 0),
                         rustore.StoreApp(2, "ru.paid", "Платное", "", 1, "", 199, "", 0)]
    r = web.client.get("/android/add", params={"q": "сбер"})
    assert "Пример Банк" in r.text and "платное — недоступно" in r.text and ("search", "сбер") in web.sources.calls
    web.sources.apks[PKG] = make_apk(tmp_path / "a.apk", min_sdk=26)
    r = web.client.post("/android/add/rustore", data={"package": PKG, "name": "Пример Банк"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android")
    app = db.get_app(conn, shelf(conn).id, PKG)
    assert (app["name"], app["source"], app["status"]) == ("Пример Банк", "rustore", "queued")
    assert "в очереди" in web.client.get("/android").text
    work(web, conn)
    page = web.client.get("/android").text
    assert f"https://apps.example/d/{shelf(conn).shelf}/{PKG}-1/{PKG}-1.0.apk" in page
    assert "Android 8+" in page and "RuStore" in page and cert_sha(b"CERT-A")[:4] in page
    assert "на полке" in web.client.get("/android/add", params={"q": "сбер"}).text
    web.client.post("/android/add/rustore", data={"package": PKG, "name": "x"})                  # уже на полке
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 1


def test_add_rustore_rejects_bad_package(web, conn):
    assert web.client.post("/android/add/rustore", data={"package": "../x", "name": ""}).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 0


def test_search_selects_exact_archive_version_and_github(web, conn, tmp_path):
    from appshelf.android.search import Option
    ref = PKG + "@7"
    web.sources.found = [Option("fdroid_archive", PKG, PKG, "Telegram FOSS", "7.0",
                                versions=(("7.0", ref),)),
                         Option("github", "example/android", "", "Happ", "v4")]
    page = web.client.get("/android/add", params={"q": "telegram"})
    assert "Архив F-Droid" in page.text and ref in page.text and "GitHub" in page.text
    web.sources.apks[ref] = make_apk(tmp_path / "archive.apk", version_code=7, version_name="7.0")
    r = web.client.post("/android/add/source", data={"source": "fdroid_archive", "ref": ref,
                                                    "package": PKG, "name": "Telegram FOSS"}, follow_redirects=False)
    assert r.status_code == 303
    work(web, conn)
    app = db.get_app(conn, shelf(conn).id, PKG)
    assert (app["source"], app["source_ref"]) == ("fdroid_archive", ref)
    assert db.current_version(conn, shelf(conn).id, PKG)["version_code"] == 7
    web.sources.apks["example/android"] = make_apk(tmp_path / "github.apk", package="com.example.happ")
    web.client.post("/android/add/source", data={"source": "github", "ref": "example/android", "name": "Happ"})
    work(web, conn)
    assert db.get_app(conn, shelf(conn).id, "com.example.happ")["source"] == "github"


def test_search_selection_validates_source_ref_and_package(web, conn):
    for source, ref, package in (("fdroid_archive", PKG + "@1", "com.other.app"),
                                 ("github", "https://127.0.0.1/private", ""),
                                 ("link", "https://127.0.0.1/app.apk", ""),
                                 ("rustore", "../x", "../x")):
        r = web.client.post("/android/add/source", data={"source": source, "ref": ref, "package": package})
        assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 0


def test_add_link_rejects_http_and_private(web, conn):
    for url in ("http://bank.example/x.apk", "https://10.0.0.1/x.apk", "https://[::1]/x.apk", "не ссылка"):
        r = web.client.post("/android/add/link", data={"url": url, "name": ""})
        assert r.status_code == 400 and url in r.text, url
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 0


def test_add_link_names_card_after_download(web, conn, tmp_path):
    add_link(web, conn, tmp_path, label="Банк по ссылке")
    page = web.client.get("/android").text
    assert "Банк по ссылке" in page and "сайт" in page


def test_add_link_names_card_from_resources(web, conn, tmp_path, cfg):
    """Название ссылкой на ресурс и сокращённые пути ресурсов (Aurora Store из F-Droid): карточка — с названием и
    иконкой из resources.arsc, а не с именем пакета и заглушкой."""
    from test_apk import ICON, LABEL, SHORT_FILES, SHORT_RES
    add_link(web, conn, tmp_path, label=f"@{LABEL:#x}", icon_ref=ICON, resources=SHORT_RES, files=SHORT_FILES)
    assert db.get_app(conn, shelf(conn).id, PKG)["name"] == "Aurora Store"
    icon = (fetch.shelf_root(cfg, shelf(conn)) / f"{PKG}-1" / "icon.png").read_bytes()
    assert Image.open(io.BytesIO(icon)).size == (192, 192)


def test_store_page_card_named_from_resources(web, conn, tmp_path):
    """Карточку по странице F-Droid панель заводит до скачивания с именем пакета (F-Droid названия не даёт) — название
    приходит из resources.arsc скачанного APK (Fennec с F-Droid)."""
    from test_apk import ICON, LABEL, SHORT_FILES, SHORT_RES
    web.sources.apks[PKG] = make_apk(tmp_path / "a.apk", label=f"@{LABEL:#x}", icon_ref=ICON, resources=SHORT_RES,
                                     files=SHORT_FILES)
    web.client.post("/android/add/link", data={"url": f"https://f-droid.org/packages/{PKG}/", "name": ""})
    assert db.get_app(conn, shelf(conn).id, PKG)["name"] == PKG
    work(web, conn)
    assert db.get_app(conn, shelf(conn).id, PKG)["name"] == "Aurora Store"


def test_add_link_routes_store_pages(web, conn):
    def post(url, name=""):
        return web.client.post("/android/add/link", data={"url": url, "name": name}, follow_redirects=False)

    assert post("https://f-droid.org/en/packages/org.videolan.vlc/").status_code == 303
    app = db.get_app(conn, shelf(conn).id, "org.videolan.vlc")
    assert (app["source"], app["source_ref"], app["status"]) == ("fdroid", "org.videolan.vlc", "queued")
    assert post("https://appgallery.huawei.com/app/C102312137", "ВТБ").status_code == 303   # пакет узнает обработчик
    assert post("https://www.rustore.ru/catalog/app/ru.sberbankmobile").status_code == 303
    jobs = [(j["kind"], j["package"], json.loads(j["payload"])) for j in conn.execute(
        "SELECT kind, package, payload FROM android_jobs ORDER BY id")]
    assert [(k, p, d["source"], d["ref"]) for k, p, d in jobs] == [
        ("add", "org.videolan.vlc", "fdroid", "org.videolan.vlc"), ("add", "", "appgallery", "C102312137"),
        ("add", "ru.sberbankmobile", "rustore", "ru.sberbankmobile")]
    assert jobs[1][2]["name"] == "ВТБ"
    post("https://f-droid.org/packages/org.videolan.vlc/")                               # уже на полке
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 3
    r = post("https://f-droid.org/packages/nodots/")
    assert r.status_code == 400 and "не похоже на страницу приложения" in r.text


def test_add_link_apkpure_passes_ack_without_card(web, conn):
    for ack in ({"ack": "1"}, {}):
        r = web.client.post("/android/add/link", data={"url": "https://apkpure.com/k-9-mail/com.fsck.k9", "name": "",
                                                       **ack}, follow_redirects=False)
        assert r.status_code == 303
    jobs = [(j["package"], json.loads(j["payload"])) for j in conn.execute("SELECT * FROM android_jobs ORDER BY id")]
    assert jobs == [("", {"source": "apkpure", "ref": "com.fsck.k9", "name": "", "ack": True}),
                    ("", {"source": "apkpure", "ref": "com.fsck.k9", "name": "", "ack": False})]
    assert db.shelf_for_user(conn, 1) is not None and db.list_apps(conn, shelf(conn).id) == []


def test_add_link_play_page_needs_connected_account(web, conn, clock, acct):
    r = web.client.post("/android/add/link", data={"url": "https://play.google.com/store/apps/details?id=com.fsck.k9"})
    assert r.status_code == 400 and "подключите Google-аккаунт к Google Play" in r.text
    gid = people.create_google(conn, acct.user_id, "sub-1", "owner@gmail.example", "Владелец", clock.iso())
    people.set_play(conn, gid, "ok", clock.iso())
    web.client.post("/android/add/link", data={"url": "https://play.google.com/store/apps/details?id=com.fsck.k9"})
    job = conn.execute("SELECT package, payload FROM android_jobs").fetchone()
    assert job["package"] == "com.fsck.k9" and json.loads(job["payload"]) == {
        "source": "play", "ref": "com.fsck.k9", "account": gid}


def test_change_source_to_store_page(web, conn, tmp_path):
    add_link(web, conn, tmp_path)
    r = web.client.post(f"/android/apps/{PKG}/source", data={"source": "link", "ref": f"https://apkpure.com/x/{PKG}"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert db.get_app(conn, shelf(conn).id, PKG)["source"] == "apkpure"
    r = web.client.post(f"/android/apps/{PKG}/source",
                        data={"source": "link", "ref": "https://f-droid.org/packages/ru.other/"})
    assert r.status_code == 400 and "ru.other" in r.text
    r = web.client.post(f"/android/apps/{PKG}/source",
                        data={"source": "link", "ref": f"https://play.google.com/store/apps/details?id={PKG}"})
    assert r.status_code == 400
    assert db.get_app(conn, shelf(conn).id, PKG)["source"] == "apkpure"


def test_card_shows_source_and_notices(web, conn, tmp_path):
    web.sources.signatures["ru.f"] = cert_sha(b"CERT-B")                      # в RuStore подписан иначе
    for package, url in (("ru.f", "https://f-droid.org/packages/ru.f/"), ("ru.p", "https://apkpure.com/p/ru.p")):
        web.sources.apks[package] = make_apk(tmp_path / f"{package}.apk", package=package)
        web.client.post("/android/add/link", data={"url": url, "name": "", "ack": "1"})
        work(web, conn)
    page = web.client.get("/android").text
    assert "F-Droid" in page and "APKPure" in page
    assert fetch.SIGNER_DIFFERS in page and "подпись не сверена" in page and "не удалось обновить" not in page
    for name in ("Брать из RuStore", "Брать из AppGallery", "Брать из F-Droid", "Брать из APKPure"):
        assert name in page, name
    assert 'name="ack"' in page.split("Брать из APKPure")[0].rsplit("<form", 1)[1]   # галочка — в форме APKPure
    r = web.client.post("/android/apps/ru.f/source", data={"source": "appgallery", "ref": ""}, follow_redirects=False)
    assert r.status_code == 303
    app = db.get_app(conn, shelf(conn).id, "ru.f")
    assert (app["source"], app["source_ref"]) == ("appgallery", "ru.f")
    assert conn.execute("SELECT kind FROM android_jobs WHERE status='queued'").fetchone()["kind"] == "check"


def test_upload_of_garbage_shows_error(web, conn, cfg):
    r = web.client.post("/android/add/upload", data={"name": "Мусор"}, files={"file": ("x.apk", b"garbage")},
                        follow_redirects=False)
    assert r.status_code == 303
    assert len(list((cfg.tmp_dir / "web").glob("up-*.apk"))) == 1
    work(web, conn)
    page = web.client.get("/android").text
    assert "Мусор" in page and "не APK" in page and not list((cfg.tmp_dir / "web").glob("up-*.apk"))
    job_id = conn.execute("SELECT id FROM android_jobs").fetchone()["id"]
    web.client.post(f"/android/jobs/{job_id}/dismiss")
    assert "не APK" not in web.client.get("/android").text


def test_upload_good_file_and_new_version(web, conn, tmp_path):
    files = {"file": ("a.apk", make_apk(tmp_path / "a.apk").read_bytes())}
    web.client.post("/android/add/upload", data={"name": ""}, files=files)
    work(web, conn)
    assert "свой файл" in web.client.get("/android").text
    files = {"file": ("b.apk", make_apk(tmp_path / "b.apk", version_code=2).read_bytes())}
    assert web.client.post(f"/android/apps/{PKG}/upload", files=files, follow_redirects=False).status_code == 303
    work(web, conn)
    view = db.list_apps(conn, shelf(conn).id)[0]
    assert (view.current["version_code"], view.previous["version_code"]) == (2, 1)


def test_app_link_follows_current_version(web, conn, tmp_path, make_web, cfg):
    """«Скопировать ссылку» — постоянный адрес приложения: открывается без входа (как файлы /d/) и ведёт на текущую
    версию; другой токен, неизвестный пакет, приложение без версии — 404."""
    add_link(web, conn, tmp_path)
    s = shelf(conn)
    link = f"https://apps.example/apk/{s.shelf}/{PKG}.apk"
    assert f'data-copy="{link}"' in web.client.get("/android").text
    anon = make_web(cfg, conn).client
    r = anon.get(f"/apk/{s.shelf}/{PKG}.apk", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (302, fetch.file_url(cfg, s, f"{PKG}-1", f"{PKG}-1.0.apk"))
    files = {"file": ("b.apk", make_apk(tmp_path / "b.apk", version_code=2, version_name="2.0").read_bytes())}
    web.client.post(f"/android/apps/{PKG}/upload", files=files)
    work(web, conn)
    r = anon.get(f"/apk/{s.shelf}/{PKG}.apk", follow_redirects=False)
    assert r.headers["location"] == fetch.file_url(cfg, s, f"{PKG}-2", f"{PKG}-2.0.apk")
    web.client.post("/android/add/rustore", data={"package": "ru.queued", "name": "В очереди"})
    for path in (f"/apk/{'0' * 32}/{PKG}.apk", f"/apk/{s.shelf}/ru.other.apk", f"/apk/{s.shelf}/ru.queued.apk",
                 f"/apk/{s.shelf}/{PKG}", f"/apk/{s.shelf[:-1]}/{PKG}.apk"):
        assert anon.get(path, follow_redirects=False).status_code == 404, path


def test_upload_too_big_and_empty(web, conn, cfg, monkeypatch):
    monkeypatch.setattr(net, "MAX_APK", 10)
    r = web.client.post("/android/add/upload", data={"name": ""}, files={"file": ("a.apk", b"x" * 11)})
    assert r.status_code == 413 and "больше" in r.text
    r = web.client.post("/android/add/upload", data={"name": ""}, files={"file": ("a.apk", b"")})
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 0
    assert not list((cfg.tmp_dir / "web").glob("up-*"))


def test_other_person_package_is_404(web, conn, clock, tmp_path):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    other = db.ensure_shelf(conn, petr.user_id, clock.iso())
    db.put_app(conn, other.id, PKG, "Чужое", "rustore", PKG, clock.iso())
    add_link(web, conn, tmp_path, package="ru.mine")                    # своя полка у владельца есть
    for action in ("retry", "check", "remove", "accept-signer"):
        assert web.client.post(f"/android/apps/{PKG}/{action}").status_code == 404, action
    assert web.client.post(f"/android/apps/{PKG}/versions/1/pin").status_code == 404
    assert web.client.post(f"/android/apps/{PKG}/source", data={"source": "rustore", "ref": ""}).status_code == 404
    assert web.client.post(f"/android/apps/{PKG}/upload", files={"file": ("a.apk", b"x")}).status_code == 404
    other_job = db.enqueue(conn, other.id, "add", clock.iso(), payload={"source": "link", "ref": URL, "name": ""})
    conn.execute("UPDATE android_jobs SET status='error' WHERE id=?", (other_job,))
    web.client.post(f"/android/jobs/{other_job}/dismiss")
    assert conn.execute("SELECT status FROM android_jobs WHERE id=?", (other_job,)).fetchone()[0] == "error"
    assert db.get_app(conn, other.id, PKG) is not None and "Чужое" not in web.client.get("/android").text


def test_pending_signer_card_and_accept(web, conn, tmp_path):
    add_link(web, conn, tmp_path, cert=b"CERT-A")
    web.sources.apks[URL] = make_apk(tmp_path / "b.apk", cert=b"CERT-B", version_code=2, version_name="2.0")
    web.sources._latest[URL] = Latest(None, "e2||", True)
    web.client.post(f"/android/apps/{PKG}/check")
    work(web, conn)
    page = web.client.get("/android").text
    assert "подпись сменилась" in page and "Принять новую подпись" in page and cert_sha(b"CERT-B")[-4:] in page
    web.client.post(f"/android/apps/{PKG}/accept-signer")
    view = db.list_apps(conn, shelf(conn).id)[0]
    assert (view.current["version_code"], view.pending, view.signer) == (2, None, cert_sha(b"CERT-B"))
    assert "подпись сменилась" not in web.client.get("/android").text


def test_change_source_keeps_versions(web, conn, tmp_path):
    add_link(web, conn, tmp_path)
    r = web.client.post(f"/android/apps/{PKG}/source", data={"source": "rustore", "ref": ""}, follow_redirects=False)
    assert r.status_code == 303
    app = db.get_app(conn, shelf(conn).id, PKG)
    assert (app["source"], app["source_ref"], app["status"]) == ("rustore", PKG, "queued")
    assert db.list_apps(conn, shelf(conn).id)[0].current["version_code"] == 1
    job = conn.execute("SELECT kind, package, payload FROM android_jobs WHERE status='queued'").fetchone()
    assert (job["kind"], job["package"], json.loads(job["payload"])) == ("check", PKG, {"previous": ["link", URL, ""]})
    assert web.client.post(f"/android/apps/{PKG}/source", data={"source": "link", "ref": "http://x/a.apk"}
                           ).status_code == 400
    assert web.client.post(f"/android/apps/{PKG}/source", data={"source": "upload", "ref": ""}).status_code == 400


def test_retry_and_check_queue_once(web, conn, tmp_path):
    add_link(web, conn, tmp_path)
    web.client.post(f"/android/apps/{PKG}/check")
    web.client.post(f"/android/apps/{PKG}/check")
    assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE status='queued'").fetchone()[0] == 1
    web.sources.fail[URL] = net.FetchError("сайт ответил 503")
    web.sources._latest[URL] = Latest(None, "x||", True)
    work(web, conn)
    assert "не удалось: сайт ответил 503" in web.client.get("/android").text
    web.client.post(f"/android/apps/{PKG}/retry")
    assert conn.execute("SELECT kind FROM android_jobs WHERE status='queued'").fetchone()[0] == "check"


def test_retry_failed_rustore_add(web, conn):
    web.sources.fail[PKG] = net.SourceError("платное — недоступно")
    web.client.post("/android/add/rustore", data={"package": PKG, "name": "Пример"})
    work(web, conn)
    assert "ошибка: платное — недоступно" in web.client.get("/android").text
    web.client.post(f"/android/apps/{PKG}/retry")
    job = conn.execute("SELECT * FROM android_jobs WHERE status='queued'").fetchone()
    assert (job["kind"], job["package"]) == ("add", PKG) and '"rustore"' in job["payload"]


def test_remove_and_pin(web, conn, cfg, tmp_path):
    add_link(web, conn, tmp_path)
    vid = db.list_apps(conn, shelf(conn).id)[0].current["id"]
    assert web.client.post(f"/android/apps/{PKG}/versions/{vid}/pin", follow_redirects=False).status_code == 303
    assert db.list_apps(conn, shelf(conn).id)[0].current["pinned"] == 1
    web.client.post(f"/android/apps/{PKG}/versions/{vid}/unpin")
    assert db.list_apps(conn, shelf(conn).id)[0].current["pinned"] == 0
    web.client.post(f"/android/apps/{PKG}/remove")
    assert db.list_apps(conn, shelf(conn).id) == [] and not list((cfg.archive / shelf(conn).shelf).iterdir())


def test_old_target_sdk_warning(web, conn, tmp_path):
    add_link(web, conn, tmp_path, target_sdk=22, min_sdk=21)
    page = web.client.get("/android").text
    assert "Android 15+ не поставит" in page and "Android 5+" in page


def test_status_api_reports_android_apps(web, conn, tmp_path):
    assert web.client.get("/api/android/status").json() == {"busy": False, "apps": {}}
    web.client.post("/android/add/rustore", data={"package": PKG, "name": "Пример"})
    assert web.client.get("/api/android/status").json() == {"busy": True, "apps": {PKG: "queued"}}
    page = web.client.get("/android").text
    assert "/api/android/status" in page


def test_app_starts_with_broken_sources_ca(cfg, conn, tmp_path):
    ca = tmp_path / "ca.pem"  # склеено без перевода строки — load_verify_locations бросает ssl.SSLError
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE----------BEGIN CERTIFICATE-----\n")
    app = create_app(replace(cfg, sources_ca=ca), lambda acct: None, start_worker=False)
    assert TestClient(app, base_url=f"https://{HOST}").get("/healthz").status_code == 200


@pytest.mark.parametrize("path", ["/android", "/android/add", "/api/android/status"])
def test_android_pages_need_login(make_web, cfg, conn, path):
    r = make_web(cfg, conn).client.get(path, follow_redirects=False)
    assert r.status_code in (303, 401)
