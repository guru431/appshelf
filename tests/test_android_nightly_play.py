"""Ночная проверка Google Play (spec 2026-10-28 §6.4, §9): новая версия — на полку, новая только набором файлов —
текущая остаётся без ошибки, отказ токена — подключение истекло у одного аккаунта, остальные проверяются."""
import pytest

from android_fakes import FakeSources
from apk_factory import make_apk
from appshelf import nightly, people
from appshelf.android import db, fetch, play, sources
from helpers import owner
from play_fakes import FakePlay, FakePool
from test_android_sources_play import FakeHttp, connected


class Mixed(FakeSources):
    """RuStore и ссылки — как FakeSources, Google Play — настоящий Sources на FakePool."""

    def __init__(self, pool):
        super().__init__()
        self.real = sources.Sources(FakeHttp(), play=pool, sleep=lambda s: None)

    def latest(self, source, ref, stored_etag, **kw):
        return self.real.latest(source, ref, stored_etag, **kw) if source == "play" else super().latest(
            source, ref, stored_etag)

    def fetch(self, source, ref, dest, **kw):
        return self.real.fetch(source, ref, dest, **kw) if source == "play" else super().fetch(source, ref, dest)


def version(fake, tmp_path, package, code, split=False):
    """Play отдаёт package версии code (набором файлов — split)."""
    fake.apps[package] = play.PlayApp(package, package, code, f"{code}.0", False, True, "")
    fake.apks[package] = make_apk(tmp_path / f"{package}-{code}.apk", package=package, version_code=code)
    (fake.split.add if split else fake.split.discard)(package)


def card(ctx, conn, clock, shelf, gid, package):
    db.put_app(conn, shelf.id, package, package, "play", package, clock.iso(), google_account_id=gid)
    db.enqueue(conn, shelf.id, "add", clock.iso(), package=package, payload={"source": "play", "ref": package,
                                                                             "account": gid})
    fetch.run_job(ctx.env, conn, db.take_job(conn))
    assert db.has_current(conn, shelf.id, package)


def current(conn, shelf, package):
    return conn.execute("SELECT version_code FROM android_versions WHERE shelf_id=? AND package=? AND role='current'",
                        (shelf.id, package)).fetchone()[0]


@pytest.fixture
def owner_play(ctx, conn, clock, tmp_path):
    """Владелец (Apple ID без входа), его Google-аккаунт подключён; на полке — com.a.one версии 1 из Play."""
    acct = owner(conn, clock)
    g = connected(conn, clock, acct.user_id)
    fake = FakePlay({})
    ctx.env.sources = Mixed(FakePool({g.id: fake}))
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    version(fake, tmp_path, "com.a.one", 1)
    card(ctx, conn, clock, shelf, g.id, "com.a.one")
    return g, fake, shelf


def test_nightly_play_updates(owner_play, ctx, conn, clock, tmp_path):
    g, fake, shelf = owner_play
    result = nightly.run(ctx.env, conn)
    assert result.endswith("Android: обновлено 0, без изменений 1, ошибок 0")
    assert [c for c in fake.calls if c[1] == "com.a.one"][-1] == ("details", "com.a.one")   # новее нет — один details
    version(fake, tmp_path, "com.a.one", 2)
    result = nightly.run(ctx.env, conn)
    assert result.endswith("Android: обновлено 1, без изменений 0, ошибок 0")
    assert current(conn, shelf, "com.a.one") == 2
    assert db.get_app(conn, shelf.id, "com.a.one")["check_result"] == "updated"


def test_update_became_split_keeps_current(owner_play, ctx, conn, clock, tmp_path):
    g, fake, shelf = owner_play
    version(fake, tmp_path, "com.a.one", 2, split=True)
    for night in range(2):
        before = len(fake.calls)
        result = nightly.run(ctx.env, conn)
        assert result.endswith("Android: обновлено 0, без изменений 1, ошибок 0"), result
        app = db.get_app(conn, shelf.id, "com.a.one")
        assert (app["status"], app["last_error"], app["check_result"]) == ("ok", fetch.SPLIT_UPDATE, "same")
        assert current(conn, shelf, "com.a.one") == 1
        calls = [c[0] for c in fake.calls[before:]]
        assert calls == (["details", "details", "delivery"] if night == 0 else ["details"]), calls
    version(fake, tmp_path, "com.a.one", 3)                       # снова одним APK — ставится
    assert nightly.run(ctx.env, conn).endswith("Android: обновлено 1, без изменений 0, ошибок 0")
    app = db.get_app(conn, shelf.id, "com.a.one")
    assert (current(conn, shelf, "com.a.one"), app["last_error"]) == (3, "")


def test_nightly_expired_play_account_isolated(owner_play, ctx, conn, clock, tmp_path):
    g, fake, shelf = owner_play
    version(fake, tmp_path, "com.a.two", 1)
    card(ctx, conn, clock, shelf, g.id, "com.a.two")
    second = connected(conn, clock, g.user_id, sub="g-2")
    other = FakePlay({})
    ctx.env.sources.real.play.by_gid[second.id] = other
    version(other, tmp_path, "com.b.one", 1)
    card(ctx, conn, clock, shelf, second.id, "com.b.one")
    ctx.env.sources.apks["ru.store"] = make_apk(tmp_path / "ru-1.apk", package="ru.store", version_code=1)
    db.put_app(conn, shelf.id, "ru.store", "ru.store", "rustore", "ru.store", clock.iso())
    db.enqueue(conn, shelf.id, "add", clock.iso(), package="ru.store", payload={"source": "rustore", "ref": "ru.store"})
    fetch.run_job(ctx.env, conn, db.take_job(conn))
    # ночью: у первого аккаунта сменили пароль, у второго и в RuStore — новые версии
    fake.expired = True
    version(other, tmp_path, "com.b.one", 2)
    ctx.env.sources.apks["ru.store"] = make_apk(tmp_path / "ru-2.apk", package="ru.store", version_code=2)
    ctx.env.sources._latest["ru.store"] = sources.Latest(2, "", True)
    ctx.sent.clear()
    result = nightly.run(ctx.env, conn)
    assert result.endswith("Android: обновлено 2, без изменений 0, ошибок 0"), result
    assert people.get_google(conn, g.id).play == "expired" and people.get_google(conn, second.id).play == "ok"
    assert ctx.sent == [fetch.PLAY_EXPIRED_SUBJECT]                     # одно письмо, хотя приложений два
    assert [c for c in fake.calls if c[0] == "details"][-1:] == [("details", "com.a.one")]  # второе — пропущено
    for package in ("com.a.one", "com.a.two"):                         # карточки не в ошибке: ждут подключения
        assert (db.get_app(conn, shelf.id, package)["status"], db.get_app(conn, shelf.id, package)["last_error"]) == \
            ("ok", "")
    assert (current(conn, shelf, "com.b.one"), current(conn, shelf, "ru.store")) == (2, 2)
    nightly.run(ctx.env, conn)
    assert ctx.sent == [fetch.PLAY_EXPIRED_SUBJECT]                     # следующая ночь — без нового письма


def test_member_expired_no_mail(ctx, conn, clock, tmp_path):
    owner(conn, clock)
    uid = people.create_user(conn, "Мария", people.MEMBER, None, clock.iso())
    g = connected(conn, clock, uid)
    fake = FakePlay({})
    ctx.env.sources = Mixed(FakePool({g.id: fake}))
    shelf = db.ensure_shelf(conn, uid, clock.iso())
    version(fake, tmp_path, "com.m.one", 1)
    card(ctx, conn, clock, shelf, g.id, "com.m.one")
    fake.expired = True
    ctx.sent.clear()
    assert nightly.run(ctx.env, conn).endswith("Android: обновлено 0, без изменений 0, ошибок 0")
    assert people.get_google(conn, g.id).play == "expired" and ctx.sent == []
