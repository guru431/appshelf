import json
import re
from html.parser import HTMLParser

import pytest

from apk_factory import make_apk
from appshelf import jobs, people, store
from appshelf.android import db, fetch, pin, rustore
from helpers import make_account, sign_in

PKG = "ru.example.app"
URL = "https://bank.example/app.apk"


def add_personal(web, conn, tmp_path, source="link"):
    path = make_apk(tmp_path / "mine.apk", label="Личное приложение")
    if source == "upload":
        web.client.post("/android/add/upload", files={"file": ("mine.apk", path.read_bytes())})
    else:
        web.sources.apks[URL] = path
        web.client.post("/android/add/link", data={"url": URL})
    work(web, conn)
    return db.shelf_for_user(conn, 1)


def test_public_copy_preview_compares_hashes_and_refuses_stale_versions(web, conn, tmp_path):
    mine = add_personal(web, conn, tmp_path)
    page = web.client.get('/android/public/add/from-shelf', params={'package': PKG})
    assert page.status_code == 200 and 'Добавить версии: 1' in page.text
    assert 'Ночные обновления' in page.text and db.public_shelf(conn) is None  # Preview is read-only.
    snapshot = re.search(r'name="snapshot" value="([a-f0-9]+)"', page.text).group(1)
    version = make_apk(tmp_path / 'newer.apk', version_code=2, version_name='2.0')
    web.client.post('/android/add/upload', files={'file': ('newer.apk', version.read_bytes())})
    work(web, conn)
    result = web.client.post('/android/public/add/from-shelf', data={'package': PKG, 'snapshot': snapshot, 'sync_history': '1'})
    assert result.status_code == 409 and 'подтвердите заново' in result.text
    assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='copy'").fetchone()[0] == 0
    snapshot = re.search(r'name="snapshot" value="([a-f0-9]+)"', result.text).group(1)
    result = web.client.post('/android/public/add/from-shelf', data={'package': PKG, 'snapshot': snapshot, 'sync_history': '1'},
                             follow_redirects=False)
    assert result.status_code == 303
    work(web, conn)
    page = web.client.get('/android/public/add/from-shelf', params={'package': PKG})
    assert 'Версии совпадают' in page.text and 'Уже есть по SHA-256' in page.text
    assert 'name="snapshot"' not in page.text


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    """Сто неверных PIN по 200 000 итераций — десятки секунд."""
    monkeypatch.setattr(pin, "ITERATIONS", 1000)


@pytest.fixture
def member(make_web, cfg, conn, clock, acct):
    """Другой браузер: вошёл только участник, владелец — нет."""
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    wb = make_web(cfg, conn)
    sign_in(wb.client, cfg, conn, clock, petr.user_id)
    return wb


def work(web, conn):
    job = db.take_job(conn)
    assert job is not None
    fetch.run_job(web.app.state.w.env, conn, job)


def add_public(web, conn, tmp_path, url=URL, **apk):
    """Владелец добавил APK по ссылке на публичную полку, обработчик скачал."""
    web.sources.apks[url] = make_apk(tmp_path / f"{len(web.sources.apks)}.apk", **apk)
    r = web.client.post("/android/public/add/link", data={"url": url, "name": ""}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android/public")
    work(web, conn)
    return db.public_shelf(conn)


def test_member_sees_public_shelf_read_only(web, member, conn, tmp_path):
    assert member.client.get("/android/public/add?q=example").status_code == 403
    assert member.client.get("/android/public/search?q=example").status_code == 403
    assert member.client.post("/android/public/add/source", data={"source": "github", "ref": "example/app"}).status_code == 403
    shelf = add_public(web, conn, tmp_path)
    r = member.client.get("/android/public")
    assert r.status_code == 200 and f"/d/{shelf.shelf}/{PKG}-1/{PKG}-1.0.apk" in r.text
    assert "Действия" not in r.text and "/android/public/add" not in r.text and "Гостевой доступ" not in r.text
    assert 'href="/android/public"' in member.client.get("/android").text                 # вкладка «Публичная»
    for path, data in (("/android/public/add/rustore", {"package": "ru.other", "name": ""}),
                       ("/android/public/add/link", {"url": URL, "name": ""}),
                       (f"/android/public/apps/{PKG}/remove", {}), (f"/android/public/apps/{PKG}/check", {}),
                       (f"/android/public/apps/{PKG}/source", {"source": "rustore", "ref": ""}),
                       ("/android/public/pin", {"pin": "123456"}), ("/android/public/pin/reopen", {})):
        assert member.client.post(path, data=data).status_code == 403, path
    r = member.client.post("/android/public/add/upload", data={"name": ""}, files={"file": ("a.apk", b"x")})
    assert r.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE status='queued'").fetchone()[0] == 0
    assert db.get_app(conn, shelf.id, PKG) is not None and not pin.is_set(conn)


def test_owner_adds_to_public_and_worker_fills_it(web, conn, tmp_path):
    web.sources.found = [rustore.StoreApp(1, PKG, "Пример", "", 1, "", 0, "", 0)]
    response = web.client.get("/android/public/search", params={"q": "пример"})
    messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert 'action="/android/public/add/source"' in messages[-1]["html"]
    web.sources.apks[PKG] = make_apk(tmp_path / "a.apk")
    r = web.client.post("/android/public/add/rustore", data={"package": PKG, "name": "Пример"}, follow_redirects=False)
    assert r.headers["location"] == "/android/public"
    shelf = db.public_shelf(conn)
    assert shelf is not None and db.get_app(conn, shelf.id, PKG)["status"] == "queued"
    assert web.client.get("/api/android/status", params={"public": 1}).json() == {"busy": True, "apps": {PKG: "queued"}}
    assert web.client.get("/api/android/status").json() == {"busy": False, "apps": {}}   # своя полка пуста
    work(web, conn)
    page = web.client.get("/android/public").text
    assert f"/d/{shelf.shelf}/{PKG}-1/" in page and f'action="/android/public/apps/{PKG}/check"' in page
    assert 'href="/android/public/add"' in page                                          # «Добавить в публичную»


def test_personal_and_public_are_separate(web, conn, tmp_path):
    add_public(web, conn, tmp_path, label="Публичное")
    web.sources.apks["https://bank.example/mine.apk"] = make_apk(tmp_path / "m.apk", package="ru.mine", label="Своё")
    web.client.post("/android/add/link", data={"url": "https://bank.example/mine.apk", "name": ""})
    work(web, conn)
    mine, public = web.client.get("/android").text, web.client.get("/android/public").text
    assert "Своё" in mine and "Публичное" not in mine and "Публичное" in public and "Своё" not in public
    assert web.client.post(f"/android/apps/{PKG}/remove").status_code == 404
    assert web.client.post("/android/public/apps/ru.mine/remove").status_code == 404


@pytest.mark.parametrize("source", ["link", "upload"])
def test_copy_personal_apk_to_public_without_download(web, cfg, conn, tmp_path, source):
    mine = add_personal(web, conn, tmp_path, source)
    current = db.current_version(conn, mine.id, PKG)
    original_dir = fetch.shelf_root(cfg, mine) / current["dir"]
    original_apk = original_dir / fetch.apk_name(PKG, current["version_name"])
    calls = list(web.sources.calls)
    assert "Сравнить и добавить в публичную" in web.client.get("/android").text
    choices = web.client.get("/android/public/add").text
    assert "Из моей полки" in choices and "Личное приложение" in choices
    response = web.client.post("/android/public/add/from-shelf", data={"package": PKG}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/android/public")
    public = db.public_shelf(conn)
    # Повторная отправка не затирает карточку и не создаёт второго задания.
    web.client.post("/android/public/add/from-shelf", data={"package": PKG})
    assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='copy'").fetchone()[0] == 1
    work(web, conn)
    copied = db.current_version(conn, public.id, PKG)
    folder = fetch.shelf_root(cfg, public) / copied["dir"]
    assert (folder / fetch.apk_name(PKG, copied["version_name"])).read_bytes() == original_apk.read_bytes()
    assert (folder / "icon.png").read_bytes() == (original_dir / "icon.png").read_bytes()
    app = db.get_app(conn, public.id, PKG)
    assert (app["source"], app["source_ref"], app["name"], app["status"]) == \
        (source, URL if source == "link" else "", "Личное приложение", "ok")
    assert web.sources.calls == calls
    assert "Уже на публичной" in web.client.get("/android/public/add").text
    # Удаление личной копии не ломает публичную.
    web.client.post(f"/android/apps/{PKG}/remove")
    assert (folder / fetch.apk_name(PKG, copied["version_name"])).is_file()
    assert db.current_version(conn, public.id, PKG) is not None


def test_copy_only_owner_shelf_even_when_member_is_active(web, member, cfg, conn, clock, tmp_path):
    mine = add_personal(web, conn, tmp_path)
    uid = conn.execute("SELECT id FROM users WHERE role='member'").fetchone()[0]
    foreign = db.ensure_shelf(conn, uid, clock.iso())
    db.put_app(conn, foreign.id, "ru.foreign", "Чужое", "upload", "", clock.iso())
    sign_in(web.client, cfg, conn, clock, uid)
    web.client.get(f"/who/{uid}")
    assert "Личное приложение" in web.client.get("/android/public/add").text
    assert web.client.post("/android/public/add/from-shelf", data={"package": "ru.foreign"}).status_code == 404
    assert member.client.post("/android/public/add/from-shelf", data={"package": PKG}).status_code == 403
    assert web.client.post("/android/public/add/from-shelf", data={"package": PKG}).status_code == 200
    job = db.take_job(conn)
    assert json.loads(job["payload"])["from_shelf"] == mine.id


@pytest.mark.parametrize("existing", [False, True])
def test_copy_history_makes_old_versions_available_to_public_guests(web, member, make_web, cfg, conn, clock,
                                                                  tmp_path, existing):
    mine = add_personal(web, conn, tmp_path)
    if existing:
        web.client.post("/android/public/add/from-shelf", data={"package": PKG})
        work(web, conn)
    for code in (3, 2):
        path = make_apk(tmp_path / f"history-{code}.apk", version_code=code, version_name=f"{code}.0")
        web.client.post("/android/add/upload", files={"file": ("history.apk", path.read_bytes())})
        work(web, conn)
    # A build awaiting the owner's signature decision must stay private.
    path = make_apk(tmp_path / "pending.apk", version_code=4, cert=b"OTHER-KEY")
    web.client.post("/android/add/upload", files={"file": ("pending.apk", path.read_bytes())})
    work(web, conn)
    data = {"package": PKG, "sync_history": "1"} if existing else {"package": PKG}
    response = web.client.post("/android/public/add/from-shelf", data=data, follow_redirects=False)
    assert response.status_code == 303
    web.client.post("/android/public/add/from-shelf", data=data)
    work(web, conn)
    assert db.take_job(conn) is None
    public = db.public_shelf(conn)
    view = db.list_apps(conn, public.id)[0]
    assert [v["version_code"] for v in view.versions] == [3, 2, 1]
    assert view.current["version_code"] == 3 and view.pending is None
    assert {v["sha256"] for v in view.versions} == {v["sha256"] for v in db.list_apps(conn, mine.id)[0].versions}
    pin.set_pin(conn, "482913", clock.iso())
    guest = make_web(cfg, conn)
    guest.client.post("/p", data={"pin": "482913"})

    class VersionOptions(HTMLParser):
        def __init__(self, html):
            super().__init__()
            self.links = []
            self.feed(html)

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "option" and "data-link" in attrs:
                self.links.append(attrs["data-link"])

    for client, page in ((web.client, "/android/public"), (member.client, "/android/public"), (guest.client, "/p")):
        links = VersionOptions(client.get(page).text).links
        assert len(links) == 3
        old = client.get(links[-1], follow_redirects=False)
        assert old.status_code == 302 and old.headers["location"].endswith(f"/{PKG}-1.0.apk")
        assert mine.shelf not in old.headers["location"]
    before = [dict(v) for v in view.versions]
    web.client.post("/android/public/add/from-shelf", data={"package": PKG, "sync_history": "1"})
    work(web, conn)
    assert [dict(v) for v in db.list_apps(conn, public.id)[0].versions] == before


def test_sync_history_preserves_public_source_latest_and_pending_decision(web, cfg, conn, clock, tmp_path):
    mine = add_personal(web, conn, tmp_path)
    path = make_apk(tmp_path / "private-key.apk", version_code=2, cert=b"PRIVATE-KEY")
    web.client.post("/android/add/upload", files={"file": ("private.apk", path.read_bytes())})
    work(web, conn)
    assert db.accept_pending(conn, fetch.shelf_root(cfg, mine), mine.id, PKG)
    public = add_public(web, conn, tmp_path, version_code=5)
    db.set_source(conn, public.id, PKG, "rustore", PKG, "https://www.rustore.ru/catalog/app/" + PKG)
    db.set_etag(conn, public.id, PKG, "public-etag")
    add_public(web, conn, tmp_path, version_code=6, cert=b"PUBLIC-PENDING")
    before = db.list_apps(conn, public.id)[0]
    metadata = dict(db.get_app(conn, public.id, PKG))
    web.client.post("/android/public/add/from-shelf", data={"package": PKG, "sync_history": "1"})
    work(web, conn)
    after = db.list_apps(conn, public.id)[0]
    assert dict(after.current) == dict(before.current) and dict(after.pending) == dict(before.pending)
    assert [v["version_code"] for v in after.versions] == [5, 1]
    for key in ("name", "source", "source_ref", "source_page", "source_etag", "google_account_id"):
        assert db.get_app(conn, public.id, PKG)[key] == metadata[key]
    db.reject_pending(conn, fetch.shelf_root(cfg, public), public.id, PKG, after.pending["id"])
    web.client.post("/android/public/add/from-shelf", data={"package": PKG, "sync_history": "1"})
    work(web, conn)
    after = db.list_apps(conn, public.id)[0]
    assert after.pending["version_code"] == 2 and after.current["version_code"] == 5
    assert db.accept_pending(conn, fetch.shelf_root(cfg, public), public.id, PKG)
    assert [v["version_code"] for v in db.list_apps(conn, public.id)[0].versions] == [5, 2, 1]


def test_copy_refuses_paid_and_not_downloaded(web, conn, clock, tmp_path):
    mine = add_personal(web, conn, tmp_path)
    db.set_paid(conn, mine.id, PKG, True)
    response = web.client.post("/android/public/add/from-shelf", data={"package": PKG})
    assert response.status_code == 400 and "Купленное" in response.text
    assert 'action="/android/public/add/from-shelf"' not in web.client.get("/android/public/add").text
    db.put_app(conn, mine.id, "ru.waiting", "Ждём", "rustore", "ru.waiting", clock.iso())
    assert web.client.post("/android/public/add/from-shelf", data={"package": "ru.waiting"}).status_code == 409
    assert db.public_shelf(conn) is None


def test_copy_free_play_apk_with_expired_connection(web, conn, clock, tmp_path):
    mine = add_personal(web, conn, tmp_path)
    gid = people.create_google(conn, 1, "owner-google", "owner@google.example", "Владелец", clock.iso())
    people.set_play(conn, gid, "expired", clock.iso())
    db.set_source(conn, mine.id, PKG, "play", PKG)
    conn.execute("UPDATE android_apps SET google_account_id=? WHERE shelf_id=?", (gid, mine.id))
    web.client.post("/android/public/add/from-shelf", data={"package": PKG})
    work(web, conn)
    public = db.public_shelf(conn)
    assert db.current_version(conn, public.id, PKG) is not None
    app = db.get_app(conn, public.id, PKG)
    assert (app["source"], app["google_account_id"], app["status"]) == ("play", gid, "ok")


@pytest.mark.parametrize("change", ["paid", "removed", "corrupt"])
def test_copy_rechecks_source_when_worker_runs(web, cfg, conn, tmp_path, change):
    mine = add_personal(web, conn, tmp_path)
    web.client.post("/android/public/add/from-shelf", data={"package": PKG})
    if change == "paid":
        db.set_paid(conn, mine.id, PKG, True)
    elif change == "removed":
        web.client.post(f"/android/apps/{PKG}/remove")
    else:
        version = db.current_version(conn, mine.id, PKG)
        path = fetch.shelf_root(cfg, mine) / version["dir"] / fetch.apk_name(PKG, version["version_name"])
        path.write_bytes(b"damaged")
    work(web, conn)
    public = db.public_shelf(conn)
    assert db.current_version(conn, public.id, PKG) is None
    assert db.get_app(conn, public.id, PKG)["status"] == "error"
    assert not list(fetch.shelf_root(cfg, public).glob("*"))


def test_owner_sets_pin_then_change_rotates_token(web, cfg, conn, clock, tmp_path):
    shelf = add_public(web, conn, tmp_path)
    page = web.client.get("/android/public").text
    assert "Гостевой доступ" in page and "https://apps.example/p" in page and "Задайте PIN" in page
    r = web.client.post("/android/public/pin", data={"pin": "482913"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/android/public")
    assert db.public_shelf(conn) == shelf and pin.epoch(conn) == 1                  # первая установка — токен прежний
    assert pin.try_pin(conn, "ip:x", "482913", clock.iso()).result == "ok"
    assert "Задайте PIN" not in web.client.get("/android/public").text
    web.client.post("/android/public/pin", data={"pin": "111111"})
    new = db.public_shelf(conn)
    assert new.shelf != shelf.shelf and pin.epoch(conn) == 2
    assert (cfg.archive / new.shelf / f"{PKG}-1").is_dir() and not (cfg.archive / shelf.shelf).exists()
    assert f"/d/{new.shelf}/{PKG}-1/{PKG}-1.0.apk" in web.client.get("/android/public").text


def test_pin_change_refused_while_downloading(web, cfg, conn, clock, tmp_path):
    shelf = add_public(web, conn, tmp_path)
    web.client.post("/android/public/pin", data={"pin": "482913"})
    web.client.post(f"/android/public/apps/{PKG}/check")
    db.take_job(conn)                                                              # обработчик качает
    r = web.client.post("/android/public/pin", data={"pin": "111111"})
    assert r.status_code == 409 and "Идёт скачивание" in r.text
    assert db.public_shelf(conn) == shelf and pin.try_pin(conn, "ip:x", "482913", clock.iso()).result == "ok"
    conn.execute("UPDATE android_jobs SET status='done'")
    cfg.archive.rename(cfg.archive.with_name("gone"))                              # шара не смонтирована
    assert web.client.post("/android/public/pin", data={"pin": "111111"}).status_code == 503
    assert pin.epoch(conn) == 1


def test_pin_and_pending_rotation_survive_failure_before_rename(web, cfg, conn, clock, tmp_path, make_web,
                                                               monkeypatch):
    shelf = add_public(web, conn, tmp_path)
    web.client.post("/android/public/pin", data={"pin": "482913"})
    guest = make_web(cfg, conn)
    guest.client.post("/p", data={"pin": "482913"})
    old_cookie = guest.client.cookies.get(pin.COOKIE)
    real_replace = fetch.os.replace

    def interrupted(src, dst):
        if src == cfg.archive / shelf.shelf:
            raise RuntimeError("interrupted before rename")
        return real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(fetch.os, "replace", interrupted)
        with pytest.raises(RuntimeError, match="interrupted before rename"):
            web.client.post("/android/public/pin", data={"pin": "111111"})
    pending = db.public_shelf(conn)
    assert pending.shelf == shelf.shelf and pending.shelf_next
    assert pin.try_pin(conn, "ip:x", "111111", clock.iso()).result == "ok"
    assert not pin.guest_ok(web.app.state.w.key, old_cookie, pin.epoch(conn), clock.iso())
    assert guest.client.get("/p").status_code == 503
    assert guest.client.post("/p", data={"pin": "111111"}).status_code == 503
    old_link = f"/apk/{shelf.shelf}/{PKG}.apk"
    assert guest.client.get(old_link).status_code == 404
    jobs.move_shelves(cfg, conn)
    new = db.public_shelf(conn)
    assert new.shelf == pending.shelf_next and new.shelf_next == ""
    assert not (cfg.archive / shelf.shelf).exists()
    guest.client.post("/p", data={"pin": "111111"})
    page = guest.client.get("/p").text
    assert f"/d/{new.shelf}/" in page and f"/d/{shelf.shelf}/" not in page


def test_bad_pin_format_on_owner_form(web, conn):
    for bad in ("12a456", "12345", "１２３４５６"):
        r = web.client.post("/android/public/pin", data={"pin": bad})
        assert r.status_code == 400 and "6 цифр" in r.text, bad
    assert not pin.is_set(conn) and store.get_state(conn, pin.HASH_KEY) == ""


def test_reopen_button_only_for_owner(web, member, conn, clock):
    web.client.post("/android/public/pin", data={"pin": "482913"})
    for i in range(pin.ALL_LIMIT):
        pin.try_pin(conn, f"ip:198.51.100.{i}", "000000", clock.iso())
    page = web.client.get("/android/public").text
    assert "Ввод PIN закрыт до" in page and 'action="/android/public/pin/reopen"' in page
    assert "/android/public/pin/reopen" not in member.client.get("/android/public").text
    assert member.client.post("/android/public/pin/reopen").status_code == 403 and pin.closed_until(conn, clock.iso())
    r = web.client.post("/android/public/pin/reopen", follow_redirects=False)
    assert r.status_code == 303 and pin.closed_until(conn, clock.iso()) == ""
