import pytest

from apk_factory import cert_sha, make_apk
from appshelf.android import db, fetch, net, rustore
from appshelf.android.sources import Latest
from helpers import make_account

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


def test_add_link_rejects_http_and_private(web, conn):
    for url in ("http://bank.example/x.apk", "https://10.0.0.1/x.apk", "https://[::1]/x.apk", "не ссылка"):
        r = web.client.post("/android/add/link", data={"url": url, "name": ""})
        assert r.status_code == 400 and url in r.text, url
    assert conn.execute("SELECT COUNT(*) FROM android_jobs").fetchone()[0] == 0


def test_add_link_names_card_after_download(web, conn, tmp_path):
    add_link(web, conn, tmp_path, label="Банк по ссылке")
    page = web.client.get("/android").text
    assert "Банк по ссылке" in page and "сайт" in page


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
    assert conn.execute("SELECT kind, package FROM android_jobs WHERE status='queued'").fetchone()[:] == \
        ("check", PKG)
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


@pytest.mark.parametrize("path", ["/android", "/android/add", "/api/android/status"])
def test_android_pages_need_login(make_web, cfg, conn, path):
    r = make_web(cfg, conn).client.get(path, follow_redirects=False)
    assert r.status_code in (303, 401)
