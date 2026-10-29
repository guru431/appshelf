"""Источник Google Play в конвейере (spec 2026-10-28 §6.4): одним APK — на полку, набор файлов и некупленное —
отказ, купленное — только на полку того, чей аккаунт купил, отказ токена — подключение истекло, задание ждёт."""
import json
from dataclasses import replace

import pytest

from apk_factory import make_apk
from appshelf import people
from appshelf.android import db, fetch, net, play, rustore, sources
from play_fakes import FakePlay, FakePool

PKG = "com.example.mail"
FREE = play.PlayApp(PKG, "Почта", 1, "1.0", False, True, "")
PAID = play.PlayApp("com.example.paid", "Платное", 1, "1.0", True, True, "")


class FakeHttp:
    """Подсказка «есть в …»: RuStore — overallInfo по пакету из .rustore (цена), иначе 404; AppGallery — пакеты
    .appgallery, F-Droid — пакеты .fdroid, остальное — «нет»."""

    def __init__(self, rustore_apps=None):
        self.rustore, self.calls = dict(rustore_apps or {}), []
        self.appgallery, self.fdroid = set(), set()

    def form(self, url, data, *, headers=None):
        self.calls.append(url)
        package = json.loads(data["pkgInfo"])["params"][0]["package"]
        found = [{"id": "C1000001", "package": package, "version": "1", "versionCode": 1, "downurl": "https://x/a.apk"}]
        return {"rtnCode": 0, "list": found if package in self.appgallery else []}

    def request(self, method, url, *, headers=None, body=None, max_bytes=0):
        self.calls.append(url)
        if url.rsplit("/", 1)[1] not in self.fdroid:
            return 404, {}, b'{"error":"NOT_FOUND"}'
        return 200, {}, b'{"suggestedVersionCode": 1, "packages": [{"versionName": "1", "versionCode": 1}]}'

    def json(self, url, *, headers=None, body=None):
        self.calls.append(url)
        package = url.rsplit("/", 1)[1]
        if package not in self.rustore:
            raise net.FetchError("backapi.rustore.ru ответил 404")
        return {"code": "OK", "body": {"appId": 5, "packageName": package, "appName": "x", "versionCode": 1,
                                       "price": self.rustore[package]}}


def connected(conn, clock, uid, sub="g-1"):
    gid = people.create_google(conn, uid, sub, f"{sub}@gmail.example", "", clock.iso())
    people.set_play(conn, gid, "ok", clock.iso())
    return people.get_google(conn, gid)


@pytest.fixture
def setup(ctx, conn, clock, tmp_path, acct):
    """Владелец (участник 1) с подключённым Google-аккаунтом; Play знает FREE и PAID, файлы — на диске."""
    g = connected(conn, clock, acct.user_id)
    fake = FakePlay({FREE.package: FREE, PAID.package: PAID},
                    {FREE.package: make_apk(tmp_path / "free.apk", package=FREE.package),
                     PAID.package: make_apk(tmp_path / "paid.apk", package=PAID.package)})
    http = FakeHttp()
    ctx.env.sources = sources.Sources(http, play=FakePool({g.id: fake}), sleep=lambda s: None)
    return g, fake, http


def add(ctx, conn, clock, shelf, gid, package=PKG):
    """Как «Добавить» из Play в панели: карточка с аккаунтом, задание add."""
    db.put_app(conn, shelf.id, package, package, "play", package, clock.iso(), google_account_id=gid)
    job_id = db.enqueue(conn, shelf.id, "add", clock.iso(), package=package,
                        payload={"source": "play", "ref": package, "account": gid})
    job = db.take_job(conn)
    assert job is not None and job["id"] == job_id
    fetch.run_job(ctx.env, conn, job)
    return dict(conn.execute("SELECT * FROM android_jobs WHERE id=?", (job_id,)).fetchone()), \
        db.get_app(conn, shelf.id, package)


def test_play_free_single_apk_added(setup, ctx, conn, clock):
    g, fake, _ = setup
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    job, app = add(ctx, conn, clock, shelf, g.id)
    assert job["status"] == "done", job["error"]
    assert (app["status"], app["source"], app["google_account_id"], app["paid"]) == ("ok", "play", g.id, 0)
    assert app["name"] == "Почта"                                 # панель знала только пакет — название из Play
    assert db.has_current(conn, shelf.id, PKG)
    assert [c[0] for c in fake.calls] == ["details", "delivery", "download"]


def test_play_removed_app_unavailable(setup, ctx, conn, clock):
    g, fake, _ = setup
    del fake.apps[PKG]
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    job, app = add(ctx, conn, clock, shelf, g.id)
    assert job["status"] == "error" and "нет в Google Play" in job["error"]
    assert app["status"] == "error" and "нет в Google Play" in app["last_error"]


def test_play_split_refused_with_rustore_hint(setup, ctx, conn, clock):
    g, fake, http = setup
    fake.split.add(PKG)
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    job, app = add(ctx, conn, clock, shelf, g.id)
    assert job["error"] == sources.SPLIT_TEXT and not db.has_current(conn, shelf.id, PKG)
    assert ("download", PKG) not in fake.calls                    # набор файлов не качаем вовсе
    http.rustore[PKG] = 0                                         # есть в RuStore бесплатно
    db.remove_app(conn, fetch.shelf_root(ctx.env.cfg, shelf), shelf.id, PKG)
    job, _ = add(ctx, conn, clock, shelf, g.id)
    assert job["error"] == sources.SPLIT_TEXT + "; есть в RuStore — смените источник"
    http.rustore[PKG] = 9900                                      # в RuStore платное — подсказки нет
    db.remove_app(conn, fetch.shelf_root(ctx.env.cfg, shelf), shelf.id, PKG)
    assert add(ctx, conn, clock, shelf, g.id)[0]["error"] == sources.SPLIT_TEXT


def test_split_hint_lists_all_sources(setup, ctx, conn, clock):
    g, fake, http = setup
    fake.split.add(PKG)
    http.rustore[PKG], http.appgallery, http.fdroid = 0, {PKG}, {PKG}
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    job, _ = add(ctx, conn, clock, shelf, g.id)
    assert job["error"] == sources.SPLIT_TEXT + "; есть в RuStore, AppGallery, F-Droid — смените источник"
    http.rustore.clear()
    http.appgallery.clear()
    db.remove_app(conn, fetch.shelf_root(ctx.env.cfg, shelf), shelf.id, PKG)
    assert add(ctx, conn, clock, shelf, g.id)[0]["error"] == sources.SPLIT_TEXT + "; есть в F-Droid — смените источник"


def test_play_paid_not_owned_refused(setup, ctx, conn, clock):
    g, fake, _ = setup
    fake.apps[PAID.package] = replace(PAID, owned=False)
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    job, _ = add(ctx, conn, clock, shelf, g.id, PAID.package)
    assert job["error"] == "платное — не куплено этим аккаунтом"
    assert [c[0] for c in fake.calls] == ["details"]


def test_paid_refused_on_public_and_foreign_shelf(setup, ctx, conn, clock):
    g, fake, _ = setup
    other = people.create_user(conn, "Мария", people.MEMBER, None, clock.iso())
    for shelf in (db.ensure_public_shelf(conn, clock.iso()), db.ensure_shelf(conn, other, clock.iso())):
        job, app = add(ctx, conn, clock, shelf, g.id, PAID.package)
        assert job["status"] == "error" and "купленное" in job["error"], job["error"]
        assert not db.has_current(conn, shelf.id, PAID.package)
    assert ("delivery", PAID.package) not in fake.calls           # отказ — до доставки и скачивания
    own = db.ensure_shelf(conn, g.user_id, clock.iso())
    job, app = add(ctx, conn, clock, own, g.id, PAID.package)
    assert job["status"] == "done" and app["paid"] == 1
    # и проверка обновления чужим аккаунтом на чужой полке — тоже отказ (карточку подделали в базе)
    foreign = db.ensure_shelf(conn, other, clock.iso())
    db.put_app(conn, foreign.id, PAID.package, "x", "play", PAID.package, clock.iso(), google_account_id=g.id,
               paid=True)
    with pytest.raises(sources.SourceError, match="купленное"):
        fetch.check_app(ctx.env, conn, foreign, db.get_app(conn, foreign.id, PAID.package),
                        ctx.env.cfg.tmp_dir / "w")


def test_auth_error_marks_expired_and_requeues(setup, ctx, conn, clock):
    g, fake, _ = setup
    fake.expired = True
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    job, app = add(ctx, conn, clock, shelf, g.id)
    assert job["status"] == "queued" and app["status"] == "queued" and job["error"] == ""
    assert people.get_google(conn, g.id).play == "expired"
    assert db.take_job(conn) is None                              # ждёт, пока аккаунт не подключат заново
    rustore_job = db.enqueue(conn, shelf.id, "add", clock.iso(), payload={"source": "rustore", "ref": "ru.x"})
    assert db.take_job(conn)["id"] == rustore_job                 # остальные задания идут
    people.set_play(conn, g.id, "ok", clock.iso())
    assert db.take_job(conn)["id"] == job["id"]


def test_deleted_account_is_error(setup, ctx, conn, clock):
    g, fake, _ = setup
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    conn.execute("DELETE FROM google_accounts WHERE id=?", (g.id,))
    job, app = add(ctx, conn, clock, shelf, g.id)
    assert job["status"] == "error" and "Google-аккаунт удалён" in job["error"] and fake.calls == []


def test_unknown_proxy_name_error(cfg, conn, clock, acct):
    g = connected(conn, clock, acct.user_id)
    people.set_proxy(conn, g.id, "us")
    pool = play.PlayPool(replace(cfg, proxies={"de": "socks5h://de.example:1080"}))
    with pytest.raises(play.PlayError, match="прокси «us» не настроен"):
        pool.client(people.get_google(conn, g.id))
    people.set_play(conn, g.id, "expired", clock.iso())
    with pytest.raises(play.PlayAuthError):
        pool.client(people.get_google(conn, g.id))


def test_play_apps_checked_only_with_working_connection(setup, conn, clock):
    g, _, _ = setup
    shelf = db.ensure_shelf(conn, g.user_id, clock.iso())
    for package, status in ((PKG, "ok"), ("com.example.b", "error")):
        db.put_app(conn, shelf.id, package, package, "play", package, clock.iso(), google_account_id=g.id)
        db.set_status(conn, shelf.id, package, status)
    assert [a["package"] for a in db.apps_to_check(conn)] == ["com.example.b", PKG]
    people.set_play(conn, g.id, "expired", clock.iso())
    assert db.apps_to_check(conn) == []


def test_web_sources_have_play_pool(cfg, conn):
    from appshelf.web.app import create_app
    app = create_app(cfg, lambda acct: None, start_worker=False)
    assert isinstance(app.state.w.env.sources.play, play.PlayPool)
