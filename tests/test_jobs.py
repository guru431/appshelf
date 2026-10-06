import errno
import os
import plistlib
import socket
from pathlib import Path

from appshelf import jobs, store
from appshelf.config import GB
from appshelf.ipatool import LicenseNotFound, SessionExpired
from helpers import TOKEN, seed_app
from ipa_factory import make_ipa, png_bytes


def published(conn, clock, app_id=123):
    store.upsert_purchases(conn, [{"id": app_id, "bundleId": "ru.example.app", "name": "Пример"}], clock.iso())
    store.publish(conn, app_id, clock.iso())
    store.set_state(conn, "session", "ok")


def worker(ctx, cfg, **kw):
    return jobs.Worker(ctx.env, lambda: store.connect(cfg.db), **kw)


def test_publish_job_builds_version_dir(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "src.ipa", item_id=123, version="1.2.3", external_id="900",
                                  artwork=png_bytes())
    assert worker(ctx, cfg).tick() is True
    d = cfg.pub_root / "123-1.2.3"
    assert sorted(p.name for p in d.iterdir()) == ["app.ipa", "icon.png", "manifest.plist"]
    assets = plistlib.loads((d / "manifest.plist").read_bytes())["items"][0]["assets"]
    assert assets[0]["url"] == f"https://apps.example/d/{TOKEN}/123-1.2.3/app.ipa"
    assert store.current_version(conn, 123)["external_version_id"] == "900"
    assert store.current_version(conn, 123)["built"] == "2026-06-18"
    assert store.list_apps(conn)[0].status == "ok"
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "done"
    assert not (cfg.tmp_dir / "web" / "dl-123").exists()
    assert not [p for p in cfg.pub_root.iterdir() if p.name.startswith(".")]


def test_worker_waits_for_login_but_purges_password(ctx, cfg, conn, clock):
    published(conn, clock)
    store.set_state(conn, "session", "expired")
    purged = []
    assert worker(ctx, cfg, purge=lambda: purged.append(1)).tick() is False
    assert purged == [1] and ctx.tool.calls == []
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"


def test_session_expired_requeues_and_mails_once_per_episode(ctx, cfg, conn, clock):
    published(conn, clock)
    ctx.tool.errors["download"] = SessionExpired("session_expired", "истёк")
    w = worker(ctx, cfg)
    w.tick()
    assert store.get_state(conn, "session") == "expired"
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"
    assert store.list_apps(conn)[0].status == "queued"
    assert ctx.sent == ["appshelf: нужен вход в Apple ID"]
    assert w.tick() is False
    jobs.mark_expired(ctx.env, conn)
    assert len(ctx.sent) == 1
    jobs.mark_ok(conn, {"name": "Иван", "storefront": "RU"}, clock.iso())
    jobs.mark_expired(ctx.env, conn)
    assert len(ctx.sent) == 2


def test_low_space_fails_job_and_mails_once(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock, 1)
    published(conn, clock, 2)
    ctx.free["bytes"] = 3 * GB
    w = worker(ctx, cfg)
    w.tick()
    w.tick()
    apps = store.list_apps(conn)
    assert [a.status for a in apps] == ["error", "error"] and "мало места" in apps[0].last_error
    assert ctx.sent == [f"appshelf: мало места на {socket.gethostname()}"]
    assert all(c[0] != "download" for c in ctx.tool.calls)
    ctx.free["bytes"] = 50 * GB
    ctx.tool.ipas[1] = make_ipa(tmp_path / "a.ipa", item_id=1)
    store.retry(conn, 1, clock.iso())
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
    published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "src.ipa", item_id=123)
    worker(ctx, cfg).tick()
    assert store.list_apps(conn)[0].status == "ok"
    assert (cfg.pub_root / "123-1.2.3" / "app.ipa").is_file()


def test_unmounted_share_fails_job_without_writing_locally(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "src.ipa", item_id=123)
    cfg.pub_root.parent.rmdir()  # шара не смонтирована: под точкой монтирования пусто
    worker(ctx, cfg).tick()
    app = store.list_apps(conn)[0]
    assert app.status == "error" and "архив" in app.last_error and TOKEN not in app.last_error
    assert not cfg.pub_root.parent.exists()
    assert all(c[0] != "download" for c in ctx.tool.calls)


def test_low_space_on_share_counts_too(ctx, cfg, conn, clock):
    published(conn, clock)
    ctx.env.disk_free = lambda path: 1 * GB if path == cfg.pub_root.parent else 50 * GB
    worker(ctx, cfg).tick()
    assert "мало места" in store.list_apps(conn)[0].last_error


def test_license_not_found_shows_text(ctx, cfg, conn, clock):
    published(conn, clock)
    ctx.tool.errors["download"] = LicenseNotFound("license_not_found", "no license")
    worker(ctx, cfg).tick()
    app = store.list_apps(conn)[0]
    assert app.status == "error" and app.last_error == "у Apple ID нет лицензии на это приложение"


def test_same_version_keeps_current(ctx, cfg, conn, clock, tmp_path):
    seed_app(conn, cfg, clock, versions=("1.2.3",), make_dirs=True)  # ext 900, задание publish в очереди
    store.set_state(conn, "session", "ok")
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, version="1.2.3", external_id="900")
    worker(ctx, cfg).tick()
    assert [r["role"] for r in conn.execute("SELECT role FROM versions")] == ["current"]
    assert list(cfg.pub_root.iterdir()) == [cfg.pub_root / "123-1.2.3"]


def test_unpublish_during_download_leaves_no_files(ctx, cfg, conn, clock, tmp_path):  # Review Focus 2
    published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123)
    ctx.tool.on_download = lambda app_id: store.unpublish(conn, cfg.pub_root, app_id)
    worker(ctx, cfg).tick()
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "cancelled"
    assert list(cfg.pub_root.iterdir()) == []
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0


def test_version_dir_name_skips_dirs_in_use(conn, cfg, clock):  # Review Focus 5
    seed_app(conn, cfg, clock, app_id=5, versions=("1.0",))
    assert jobs.version_dir_name(conn, 5, "1.0", "777") == "5-1.0-777"
    assert jobs.version_dir_name(conn, 5, "2.0", "778") == "5-2.0"
    assert jobs.version_dir_name(conn, 5, "1.0/../x", "1") == "5-1.0_.._x"


def test_same_version_string_new_build_gets_own_dir(ctx, cfg, conn, clock, tmp_path):  # Review Focus 5
    seed_app(conn, cfg, clock, versions=("1.2.3",), make_dirs=True)  # ext 900
    store.set_state(conn, "session", "ok")
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, version="1.2.3", external_id="901")
    worker(ctx, cfg).tick()
    assert store.current_version(conn, 123)["dir"] == "123-1.2.3-901"
    assert (cfg.pub_root / "123-1.2.3").exists() and (cfg.pub_root / "123-1.2.3-901" / "app.ipa").exists()


def test_refresh_history_job(ctx, cfg, conn, clock):
    store.set_state(conn, "session", "ok")
    store.enqueue_once(conn, "refresh_history", None, clock.iso())
    ctx.tool.purchases = [{"id": 7, "bundleId": "com.vk.app", "name": "VK", "version": "9.0",
                           "purchaseDate": "2019-05-06T07:08:09Z"}]
    worker(ctx, cfg).tick()
    assert [r["name"] for r in store.list_purchases(conn)] == ["VK"]


def test_wrong_item_id_in_ipa_is_error(ctx, cfg, conn, clock, tmp_path):
    published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=999)
    worker(ctx, cfg).tick()
    assert "itemId 999" in store.list_apps(conn)[0].last_error
    assert list(cfg.pub_root.glob("*")) == []


def test_recover_requeues_and_cleans_web_tmp(ctx, cfg, conn, clock):
    published(conn, clock)
    store.take_job(conn)
    (cfg.tmp_dir / "web" / "dl-123").mkdir(parents=True)
    (cfg.tmp_dir / "nightly").mkdir(parents=True)
    worker(ctx, cfg).recover()
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"
    assert not (cfg.tmp_dir / "web").exists() and (cfg.tmp_dir / "nightly").exists()


def test_worker_fails_updates_left_by_crashed_nightly(ctx, cfg, conn, clock):  # review #3, Review Focus 4
    seed_app(conn, cfg, clock, app_id=1, versions=("1.0",))
    for j in conn.execute("SELECT id FROM jobs").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    store.start_job(conn, "update", 1, clock.iso())
    store.set_app_status(conn, 1, "downloading")
    w = worker(ctx, cfg)
    with jobs.nightly_lock(cfg) as got:  # ночная проверка идёт — не трогаем
        assert got
        w.tick()
        assert store.list_apps(conn)[0].status == "downloading"
    w.tick()  # замок свободен — процесс ночной проверки умер
    assert store.list_apps(conn)[0].status == "ok"
    assert conn.execute("SELECT status FROM jobs WHERE kind='update'").fetchone()["status"] == "error"


def test_manual_app_gets_name_from_ipa(ctx, cfg, conn, clock, tmp_path):
    store.add_manual(conn, 123, clock.iso())
    store.set_state(conn, "session", "ok")
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, bundle_id="ru.example.app")
    worker(ctx, cfg).tick()
    app = store.list_apps(conn)[0]
    assert (app.name, app.bundle_id, app.status) == ("Пример & Ко", "ru.example.app", "ok")
    assert [(r["name"], r["bundle_id"]) for r in store.list_purchases(conn)] == [("Пример & Ко", "ru.example.app")]
