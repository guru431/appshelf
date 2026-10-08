import errno
import os
import plistlib
import socket
from pathlib import Path

import pytest

from appshelf import ipatool, jobs, people, store
from appshelf.config import GB
from appshelf.ipatool import LicenseNotFound, SessionExpired
from helpers import TOKEN, FakeTool, make_account, owner, seed_app, set_session
from ipa_factory import make_ipa, png_bytes


def published(conn, clock, app_id=123, acct=None):
    acct = acct or owner(conn, clock)
    store.upsert_purchases(conn, acct.id, [{"id": app_id, "bundleId": "ru.example.app", "name": "Пример"}],
                           clock.iso())
    store.publish(conn, acct.id, app_id, clock.iso())
    set_session(conn, acct.id, "ok")
    return people.get_account(conn, acct.id)


def member(conn, clock, email="petr@example", **kw):
    owner(conn, clock)
    return make_account(conn, clock, email=email, role="member", legacy=False, **kw)


def worker(ctx, cfg, **kw):
    return jobs.Worker(ctx.env, lambda: store.connect(cfg.db), **kw)


def test_publish_job_builds_version_dir(ctx, cfg, conn, clock, tmp_path):
    acct = published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "src.ipa", item_id=123, version="1.2.3", external_id="900",
                                  artwork=png_bytes())
    assert worker(ctx, cfg).tick() is True
    d = cfg.shelf_root(acct) / "123-1.2.3"
    assert sorted(p.name for p in d.iterdir()) == ["app.ipa", "icon.png", "manifest.plist"]
    assets = plistlib.loads((d / "manifest.plist").read_bytes())["items"][0]["assets"]
    assert assets[0]["url"] == f"https://apps.example/d/{TOKEN}/123-1.2.3/app.ipa"
    assert store.current_version(conn, 1, 123)["external_version_id"] == "900"
    assert store.current_version(conn, 1, 123)["built"] == "2026-06-18"
    assert store.list_apps(conn, 1)[0].status == "ok"
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "done"
    assert not (cfg.tmp_dir / "web" / "dl-1-123").exists()
    assert not [p for p in cfg.shelf_root(acct).iterdir() if p.name.startswith(".")]


def test_new_apple_id_publishes_into_own_shelf(ctx, cfg, conn, clock, tmp_path, monkeypatch):  # Review Focus 5
    petr = published(conn, clock, app_id=7, acct=member(conn, clock))
    modes, real = {}, Path.chmod

    def chmod(self, mode, **kw):
        modes[self] = mode
        return real(self, mode, **kw)

    monkeypatch.setattr(Path, "chmod", chmod)
    ctx.tools.setdefault(petr.id, FakeTool()).ipas[7] = make_ipa(tmp_path / "p.ipa", item_id=7)
    worker(ctx, cfg).tick()
    root = cfg.shelf_root(petr)
    assert modes[root] == 0o711                          # Apache (www-data) проходит в каталог Apple ID
    assets = plistlib.loads((root / "7-1.2.3" / "manifest.plist").read_bytes())["items"][0]["assets"]
    assert assets[0]["url"] == f"https://apps.example/d/{cfg.shelf_token(petr)}/7-1.2.3/app.ipa"
    assert not (cfg.archive / TOKEN).exists() and ctx.tool.calls == []   # полка владельца не тронута


def test_worker_waits_for_login_but_purges_password(ctx, cfg, conn, clock):
    published(conn, clock)
    set_session(conn, 1, "expired")
    purged = []
    assert worker(ctx, cfg, purge=lambda: purged.append(1)).tick() is False
    assert purged == [1] and ctx.tool.calls == []
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"


def test_jobs_of_apple_id_without_login_wait_others_run(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock)
    set_session(conn, 1, "expired")
    petr = published(conn, clock, app_id=7, acct=member(conn, clock))
    ctx.tools.setdefault(petr.id, FakeTool()).ipas[7] = make_ipa(tmp_path / "p.ipa", item_id=7)
    assert worker(ctx, cfg).tick() is True
    assert store.list_apps(conn, petr.id)[0].status == "ok" and store.list_apps(conn, 1)[0].status == "queued"
    assert ctx.tool.calls == []


def test_session_expired_requeues_and_mails_owner_once_per_episode(ctx, cfg, conn, clock):
    acct = published(conn, clock)
    ctx.tool.errors["download"] = SessionExpired("session_expired", "истёк")
    w = worker(ctx, cfg)
    w.tick()
    assert people.get_account(conn, 1).session == "expired"
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"
    assert store.list_apps(conn, 1)[0].status == "queued"
    assert ctx.sent == ["appshelf: нужен вход в Apple ID"]
    assert w.tick() is False
    jobs.mark_expired(ctx.env, conn, acct)
    assert len(ctx.sent) == 1
    people.mark_login(conn, 1, {"name": "Иван", "storefront": "RU"}, clock.iso())
    jobs.mark_expired(ctx.env, conn, acct)
    assert len(ctx.sent) == 2


def test_member_expiry_sends_no_mail(ctx, cfg, conn, clock):
    petr = member(conn, clock, session="ok")
    jobs.mark_expired(ctx.env, conn, petr)
    assert people.get_account(conn, petr.id).session == "expired" and ctx.sent == []


def test_low_space_fails_job_and_mails_once(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock, 1)
    published(conn, clock, 2)
    ctx.free["bytes"] = 3 * GB
    w = worker(ctx, cfg)
    w.tick()
    w.tick()
    apps = store.list_apps(conn, 1)
    assert [a.status for a in apps] == ["error", "error"] and "мало места" in apps[0].last_error
    assert ctx.sent == [f"appshelf: мало места на {socket.gethostname()}"]
    assert all(c[0] != "download" for c in ctx.tool.calls)
    ctx.free["bytes"] = 50 * GB
    ctx.tool.ipas[1] = make_ipa(tmp_path / "a.ipa", item_id=1)
    store.retry(conn, 1, 1, clock.iso())
    w.tick()
    assert store.get_state(conn, "low_space_mail_sent") == ""


def test_publish_moves_ipa_from_local_tmp_to_share(ctx, cfg, conn, clock, tmp_path, monkeypatch):
    # скачивание — в локальный tmp, архив — на шаре (CIFS): os.replace между ними даёт EXDEV
    real = os.replace

    def replace(src, dst):
        if Path(src).is_relative_to(cfg.tmp_dir) != Path(dst).is_relative_to(cfg.tmp_dir):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    acct = published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "src.ipa", item_id=123)
    worker(ctx, cfg).tick()
    assert store.list_apps(conn, 1)[0].status == "ok"
    assert (cfg.shelf_root(acct) / "123-1.2.3" / "app.ipa").is_file()


def test_unmounted_share_fails_job_without_writing_locally(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "src.ipa", item_id=123)
    cfg.archive.rmdir()  # шара не смонтирована: под точкой монтирования пусто
    worker(ctx, cfg).tick()
    app = store.list_apps(conn, 1)[0]
    assert app.status == "error" and "архив" in app.last_error and TOKEN not in app.last_error
    assert not cfg.archive.exists()
    assert all(c[0] != "download" for c in ctx.tool.calls)


def test_low_space_on_share_counts_too(ctx, cfg, conn, clock):
    published(conn, clock)
    ctx.env.disk_free = lambda path: 1 * GB if path == cfg.archive else 50 * GB
    worker(ctx, cfg).tick()
    assert "мало места" in store.list_apps(conn, 1)[0].last_error


def test_license_not_found_shows_text(ctx, cfg, conn, clock):
    published(conn, clock)
    ctx.tool.errors["download"] = LicenseNotFound("license_not_found", "no license")
    worker(ctx, cfg).tick()
    app = store.list_apps(conn, 1)[0]
    assert app.status == "error" and app.last_error == "у Apple ID нет лицензии на это приложение"


def test_same_version_keeps_current(ctx, cfg, conn, clock, tmp_path):
    acct = owner(conn, clock)
    seed_app(conn, cfg, clock, versions=("1.2.3",), make_dirs=True)  # ext 900, задание publish в очереди
    set_session(conn, 1, "ok")
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, version="1.2.3", external_id="900")
    worker(ctx, cfg).tick()
    assert [r["role"] for r in conn.execute("SELECT role FROM versions")] == ["current"]
    assert list(cfg.shelf_root(acct).iterdir()) == [cfg.shelf_root(acct) / "123-1.2.3"]


def test_unpublish_during_download_leaves_no_files(ctx, cfg, conn, clock, tmp_path):
    acct = published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123)
    ctx.tool.on_download = lambda app_id: store.unpublish(conn, 1, cfg.shelf_root(acct), app_id)
    worker(ctx, cfg).tick()
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "cancelled"
    assert list(cfg.shelf_root(acct).iterdir()) == []
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0


def test_download_holds_server_wide_lock(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123)
    seen = []

    def probe(app_id):  # пока качается, второй процесс замок скачивания не возьмёт
        fd = os.open(cfg.download_lock, os.O_RDWR)
        try:
            seen.append(ipatool.try_lock(fd))
        finally:
            os.close(fd)

    ctx.tool.on_download = probe
    worker(ctx, cfg).tick()
    assert seen == [False] and store.list_apps(conn, 1)[0].status == "ok"


def test_version_dir_name_skips_dirs_in_use(conn, cfg, clock):
    seed_app(conn, cfg, clock, app_id=5, versions=("1.0",))
    assert jobs.version_dir_name(conn, 1, 5, "1.0", "777") == "5-1.0-777"
    assert jobs.version_dir_name(conn, 1, 5, "2.0", "778") == "5-2.0"
    assert jobs.version_dir_name(conn, 1, 5, "1.0/../x", "1") == "5-1.0_.._x"


def test_same_version_string_new_build_gets_own_dir(ctx, cfg, conn, clock, tmp_path):
    acct = owner(conn, clock)
    seed_app(conn, cfg, clock, versions=("1.2.3",), make_dirs=True)  # ext 900
    set_session(conn, 1, "ok")
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, version="1.2.3", external_id="901")
    worker(ctx, cfg).tick()
    assert store.current_version(conn, 1, 123)["dir"] == "123-1.2.3-901"
    root = cfg.shelf_root(acct)
    assert (root / "123-1.2.3").exists() and (root / "123-1.2.3-901" / "app.ipa").exists()


def test_refresh_history_job(ctx, cfg, conn, clock):
    owner(conn, clock)
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "refresh_history", None, clock.iso())
    ctx.tool.purchases = [{"id": 7, "bundleId": "com.vk.app", "name": "VK", "version": "9.0",
                           "purchaseDate": "2019-05-06T07:08:09Z"}]
    worker(ctx, cfg).tick()
    assert [r["name"] for r in store.list_purchases(conn, 1)] == ["VK"]


def test_wrong_item_id_in_ipa_is_error(ctx, cfg, conn, clock, tmp_path):
    acct = published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=999)
    worker(ctx, cfg).tick()
    assert "itemId 999" in store.list_apps(conn, 1)[0].last_error
    assert list(cfg.shelf_root(acct).glob("*")) == []


def test_recover_requeues_and_cleans_web_tmp(ctx, cfg, conn, clock):
    published(conn, clock)
    store.take_job(conn)
    (cfg.tmp_dir / "web" / "dl-1-123").mkdir(parents=True)
    (cfg.tmp_dir / "nightly").mkdir(parents=True)
    worker(ctx, cfg).recover()
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"
    assert not (cfg.tmp_dir / "web").exists() and (cfg.tmp_dir / "nightly").exists()


def test_worker_fails_updates_left_by_crashed_nightly(ctx, cfg, conn, clock):
    seed_app(conn, cfg, clock, app_id=1, versions=("1.0",))
    for j in conn.execute("SELECT id FROM jobs").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    store.start_job(conn, 1, "update", 1, clock.iso())
    store.set_app_status(conn, 1, 1, "downloading")
    w = worker(ctx, cfg)
    with jobs.nightly_lock(cfg) as got:  # ночная проверка идёт — не трогаем
        assert got
        w.tick()
        assert store.list_apps(conn, 1)[0].status == "downloading"
    w.tick()  # замок свободен — процесс ночной проверки умер
    assert store.list_apps(conn, 1)[0].status == "ok"
    assert conn.execute("SELECT status FROM jobs WHERE kind='update'").fetchone()["status"] == "error"


def test_manual_app_gets_name_from_ipa(ctx, cfg, conn, clock, tmp_path):
    owner(conn, clock)
    store.add_manual(conn, 1, 123, clock.iso())
    set_session(conn, 1, "ok")
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, bundle_id="ru.example.app")
    worker(ctx, cfg).tick()
    app = store.list_apps(conn, 1)[0]
    assert (app.name, app.bundle_id, app.status) == ("Пример & Ко", "ru.example.app", "ok")
    assert [(r["name"], r["bundle_id"]) for r in store.list_purchases(conn, 1)] == [("Пример & Ко", "ru.example.app")]


def test_remove_account_deletes_rows_dirs_and_last_user(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    seed_app(conn, cfg, clock, app_id=7, acct=petr, versions=("1.0",), make_dirs=True)
    home = cfg.accounts_dir / str(petr.id) / ".ipatool"
    home.mkdir(parents=True)
    jobs.remove_account(cfg, conn, petr)
    assert people.get_account(conn, petr.id) is None and people.get_user(conn, petr.user_id) is None
    assert store.list_purchases(conn, petr.id) == [] and store.shelf_size(conn, petr.id) == 0
    assert not cfg.shelf_root(petr).exists() and not home.parent.exists()
    assert people.get_account(conn, 1) is not None


def test_remove_one_of_two_apple_ids_keeps_person(ctx, cfg, conn, clock):
    acct = owner(conn, clock)
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=acct.user_id)
    jobs.remove_account(cfg, conn, second)
    assert people.get_user(conn, acct.user_id) is not None
    assert [a.id for a in people.accounts_of(conn, acct.user_id)] == [1]


def test_remove_needs_archive(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    cfg.archive.rmdir()
    with pytest.raises(jobs.ArchiveUnavailable):
        jobs.remove_account(cfg, conn, petr)
    assert people.get_account(conn, petr.id) is not None


def test_remove_user_with_all_apple_ids(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    make_account(conn, clock, email="petr2@example", legacy=False, user_id=petr.user_id)
    jobs.remove_user(cfg, conn, petr.user_id)
    assert people.get_user(conn, petr.user_id) is None and people.accounts_of(conn, petr.user_id) == []
