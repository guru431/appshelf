import errno
import os
import plistlib
import socket
import sqlite3
from pathlib import Path

import pytest

from appshelf import ipa, ipatool, jobs, people, removed, store
from appshelf.android import db as android_db
from appshelf.config import GB
from appshelf.ipatool import LicenseNotFound, SessionExpired
from helpers import TOKEN, FakeTool, assert_shelf_gone, make_account, owner, seed_app, set_session
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
    # UMask служб 0077: без явных прав Apache отдал бы 403 на каждую установку
    assert {p.name: m for p, m in modes.items() if p.parent.name == ".7-1.2.3.partial"} == \
        {"app.ipa": 0o644, "icon.png": 0o644, "manifest.plist": 0o644}
    assert modes[root / ".7-1.2.3.partial"] == 0o755
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
    ctx.tool.latest[123] = "900"
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, version="1.2.3", external_id="900")
    worker(ctx, cfg).tick()
    assert [r["role"] for r in conn.execute("SELECT role FROM versions")] == ["current"]
    assert list(cfg.shelf_root(acct).iterdir()) == [cfg.shelf_root(acct) / "123-1.2.3"]
    assert all(c[0] != "download" for c in ctx.tool.calls)   # «Повторить» при той же версии IPA не качает
    assert store.list_apps(conn, 1)[0].status == "ok"


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


def test_version_dir_name_skips_dirs_in_use(conn, cfg, clock, acct):
    seed_app(conn, cfg, clock, app_id=5, versions=("1.0",))
    root = cfg.shelf_root(acct)
    assert jobs.version_dir_name(conn, 1, root, 5, "1.0", "777") == "5-1.0-777"
    assert jobs.version_dir_name(conn, 1, root, 5, "2.0", "778") == "5-2.0"
    assert jobs.version_dir_name(conn, 1, root, 5, "1.0/../x", "1") == "5-1.0_.._x"
    (root / "5-2.0").mkdir(parents=True)                  # каталог без строки: его ещё может удалять снятие
    assert jobs.version_dir_name(conn, 1, root, 5, "2.0", "778") == "5-2.0-778"


def test_republish_while_unpublish_removes_dirs_keeps_new_version(ctx, cfg, conn, clock, tmp_path, monkeypatch):
    # снятие удаляет каталоги после COMMIT: повторная публикация в это окно не пишет в удаляемый каталог
    acct = owner(conn, clock)
    seed_app(conn, cfg, clock, versions=("1.2.3",), make_dirs=True)  # ext 900, каталог 123-1.2.3
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123, version="1.2.3", external_id="900")
    real, paused = store.remove_dirs, []

    def remove_dirs(root, names):
        if not paused:                                    # COMMIT снятия прошёл, каталоги ещё не удалены
            paused.append(names)
            store.publish(conn, 1, 123, clock.iso())
            jobs.fetch_version(ctx.env, conn, acct, 123, cfg.tmp_dir / "web")
        real(root, names)

    monkeypatch.setattr(store, "remove_dirs", remove_dirs)
    jobs.unpublish(cfg, conn, acct, 123)
    cur = store.current_version(conn, 1, 123)
    assert paused == [["123-1.2.3"]] and cur is not None
    assert (cfg.shelf_root(acct) / cur["dir"] / "app.ipa").is_file()


def test_same_version_string_new_build_gets_own_dir(ctx, cfg, conn, clock, tmp_path):
    acct = owner(conn, clock)
    seed_app(conn, cfg, clock, versions=("1.2.3",), make_dirs=True)  # ext 900
    set_session(conn, 1, "ok")
    ctx.tool.latest[123] = "901"
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


def test_empty_history_is_remembered(ctx, cfg, conn, clock):
    acct = owner(conn, clock)
    assert jobs.refresh_history(ctx.env, conn, acct) == 0                # Apple ответил: покупок нет
    assert people.get_account(conn, 1).history_at == clock.iso() == store.history_refreshed_at(conn, 1)


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
    assert [(r["name"], r["bundle_id"], r["source"]) for r in store.list_purchases(conn, 1)] == \
        [("Пример & Ко", "ru.example.app", "license")]                 # Apple отдал IPA — лицензия подтверждена


def test_download_updates_version_of_history_row_not_from_daap(ctx, cfg, conn, clock, tmp_path):
    owner(conn, clock)
    set_session(conn, 1, "ok")
    store.add_known(conn, 1, 5, "СберБанк Онлайн", "license")       # нашла проверка справочника: 1.0
    store.set_purchase_version(conn, 1, 5, "1.0", clock.iso())
    store.upsert_purchases(conn, 1, [{"id": 6, "bundleId": "ru.example.six", "name": "Шесть", "version": "9.0"}],
                           clock.iso())
    for app_id in (5, 6):
        store.publish(conn, 1, app_id, clock.iso())
        ctx.tool.ipas[app_id] = make_ipa(tmp_path / f"{app_id}.ipa", item_id=app_id, version="2.0",
                                         external_id=str(900 + app_id))
    clock.advance(60)
    w = worker(ctx, cfg)
    w.tick()
    w.tick()
    rows = {r["app_id"]: (r["version"], r["version_at"]) for r in store.list_purchases(conn, 1)}
    assert rows == {5: ("2.0", clock.iso()), 6: ("9.0", "")}         # строку DAAP обновляет DAAP


def test_remove_account_deletes_rows_dirs_and_last_user(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    seed_app(conn, cfg, clock, app_id=7, acct=petr, versions=("1.0",), make_dirs=True)
    home = cfg.accounts_dir / str(petr.id) / ".ipatool"
    home.mkdir(parents=True)
    jobs.remove_account(cfg, conn, petr)
    assert people.get_account(conn, petr.id) is None and people.get_user(conn, petr.user_id) is None
    assert store.list_purchases(conn, petr.id) == [] and store.shelf_size(conn, petr.id) == 0
    assert not cfg.shelf_root(petr).exists() and not home.parent.exists()
    assert not (cfg.locks_dir / f"{petr.id}.lock").exists() and not (cfg.locks_dir / f"{petr.id}.wait").exists()
    assert people.get_account(conn, 1) is not None


def test_remove_refused_while_apple_id_has_running_job(ctx, cfg, conn, clock):
    petr = member(conn, clock, session="ok")
    seed_app(conn, cfg, clock, app_id=7, acct=petr, versions=("1.0",), make_dirs=True)
    store.take_job(conn)                                    # публикация Петра качается
    with pytest.raises(jobs.AccountBusy):
        jobs.remove_account(cfg, conn, petr)
    with pytest.raises(jobs.AccountBusy):
        jobs.remove_user(cfg, conn, petr.user_id)
    assert people.get_account(conn, petr.id) is not None and cfg.shelf_root(petr).is_dir()


def test_remove_refused_while_ipatool_of_apple_id_runs(ctx, cfg, conn, clock, monkeypatch):
    petr = member(conn, clock)
    monkeypatch.setattr(jobs, "REMOVE_LOCK_WAIT", 0)
    with ipatool.hold(cfg.locks_dir / f"{petr.id}.lock"):  # вход или проверка этого Apple ID идёт
        with pytest.raises(jobs.AccountBusy):
            jobs.remove_account(cfg, conn, petr)
    assert people.get_account(conn, petr.id) is not None


def test_recover_removes_abandoned_new_logins(ctx, cfg, conn, clock):
    owner(conn, clock)
    (cfg.accounts_dir / ".new-0123456789abcdef" / ".ipatool").mkdir(parents=True)
    (cfg.accounts_dir / "1").mkdir()
    (cfg.accounts_dir / "7" / ".ipatool").mkdir(parents=True)   # Apple ID №7 удалили посреди входа
    cfg.locks_dir.mkdir(parents=True)
    for name in (".new-0123456789abcdef.lock", ".new-0123456789abcdef.wait", "1.lock", "1.wait", "7.lock", "7.wait",
                 "download.lock", "download.wait"):
        (cfg.locks_dir / name).write_text("")
    worker(ctx, cfg).recover()
    assert [p.name for p in cfg.accounts_dir.iterdir()] == ["1"]
    assert sorted(p.name for p in cfg.locks_dir.iterdir()) == ["1.lock", "1.wait", "download.lock", "download.wait"]


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
    people.create_google(conn, petr.user_id, "sub-p", "petr@gmail.example", "", clock.iso())
    jobs.remove_user(cfg, conn, petr.user_id)
    assert people.get_user(conn, petr.user_id) is None and people.accounts_of(conn, petr.user_id) == []
    assert people.google_of(conn, petr.user_id) == []


def android_shelf_of(cfg, conn, clock, uid):
    """Android-полка участника с каталогом версии на диске."""
    shelf = android_db.ensure_shelf(conn, uid, clock.iso())
    (cfg.archive / shelf.shelf / "ru.x-1").mkdir(parents=True)
    return shelf


def google_only(conn, clock, *subs):
    """Участник без Apple ID и его Google-аккаунты (id по порядку subs)."""
    owner(conn, clock)
    uid = people.create_user(conn, "Гость", people.MEMBER, None, clock.iso())
    return uid, [people.create_google(conn, uid, s, f"{s}@gmail.example", "", clock.iso()) for s in subs]


def test_deleting_last_apple_id_keeps_person_with_google(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    people.create_google(conn, petr.user_id, "sub-p", "petr@gmail.example", "Пётр", clock.iso())
    shelf = android_shelf_of(cfg, conn, clock, petr.user_id)
    jobs.remove_account(cfg, conn, petr)
    assert people.get_account(conn, petr.id) is None and people.get_user(conn, petr.user_id) is not None
    assert android_db.shelf_for_user(conn, petr.user_id) == shelf and (cfg.archive / shelf.shelf / "ru.x-1").is_dir()
    assert not people.has_apple(conn, petr.user_id)


def test_deleting_last_google_of_google_only_person_removes_person_and_shelf(ctx, cfg, conn, clock):
    uid, (g1, g2) = google_only(conn, clock, "sub-1", "sub-2")
    shelf = android_shelf_of(cfg, conn, clock, uid)
    assert jobs.remove_google(cfg, conn, g1) is False                    # остался второй Google-аккаунт
    assert people.get_user(conn, uid) is not None and android_db.get_shelf(conn, shelf.id) == shelf
    assert jobs.remove_google(cfg, conn, g2) is True
    assert people.get_user(conn, uid) is None and people.google_of(conn, uid) == []
    assert android_db.get_shelf(conn, shelf.id) is None and not (cfg.archive / shelf.shelf).exists()
    assert jobs.remove_google(cfg, conn, g2) is False                    # уже удалён — ничего


def test_deleting_google_of_person_with_apple_keeps_person(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    gid = people.create_google(conn, petr.user_id, "sub-p", "petr@gmail.example", "", clock.iso())
    assert jobs.remove_google(cfg, conn, gid) is False
    assert people.get_user(conn, petr.user_id) is not None and people.google_of(conn, petr.user_id) == []


def test_remove_google_refused_while_downloading(ctx, cfg, conn, clock, monkeypatch):
    uid, (gid,) = google_only(conn, clock, "sub-1")
    shelf = android_shelf_of(cfg, conn, clock, uid)
    monkeypatch.setattr(jobs, "REMOVE_LOCK_WAIT", 0)
    with ipatool.hold(cfg.download_lock):                                # идёт скачивание
        with pytest.raises(jobs.AccountBusy):
            jobs.remove_google(cfg, conn, gid)
    android_db.enqueue(conn, shelf.id, "check", clock.iso(), package="ru.x")
    android_db.take_job(conn)                                            # задание полки — в работе
    with pytest.raises(jobs.AccountBusy):
        jobs.remove_google(cfg, conn, gid)
    assert people.get_google(conn, gid) is not None and android_db.get_shelf(conn, shelf.id) == shelf


def test_remove_user_without_apple_ids(ctx, cfg, conn, clock):
    uid, _ = google_only(conn, clock, "sub-1", "sub-2")
    shelf = android_shelf_of(cfg, conn, clock, uid)
    jobs.remove_user(cfg, conn, uid)
    assert people.get_user(conn, uid) is None and people.google_of(conn, uid) == []
    assert android_db.get_shelf(conn, shelf.id) is None and not (cfg.archive / shelf.shelf).exists()


def test_remove_user_deletes_nothing_while_one_apple_id_is_busy(ctx, cfg, conn, clock, monkeypatch):
    petr = member(conn, clock)
    second = make_account(conn, clock, email="petr2@example", legacy=False, user_id=petr.user_id)
    monkeypatch.setattr(jobs, "REMOVE_LOCK_WAIT", 0)
    with ipatool.hold(cfg.locks_dir / f"{second.id}.lock"):  # идёт вызов ipatool второго Apple ID Петра
        with pytest.raises(jobs.AccountBusy):
            jobs.remove_user(cfg, conn, petr.user_id)
    assert [a.id for a in people.accounts_of(conn, petr.user_id)] == [petr.id, second.id]


def test_remove_after_move_deletes_new_shelf(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    shelf_with_version(cfg, petr.shelf)
    jobs.move_shelf(cfg, conn, petr)            # rotate-token, пока была открыта страница удаления
    moved = people.get_account(conn, petr.id)
    jobs.remove_account(cfg, conn, petr)        # у запроса — строка Apple ID до переноса
    assert not (cfg.archive / moved.shelf).exists()


def test_failed_copy_to_share_leaves_nothing_on_shelf(ctx, cfg, conn, clock, tmp_path, monkeypatch):
    acct = published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123)

    def broken_move(src, dst):  # шара отвалилась посреди копирования
        Path(dst).write_bytes(b"half of ipa")
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(jobs.shutil, "move", broken_move)
    worker(ctx, cfg).tick()
    assert store.list_apps(conn, 1)[0].status == "error" and list(cfg.shelf_root(acct).iterdir()) == []
    assert not (cfg.tmp_dir / "web" / "dl-1-123").exists()


def test_failed_db_write_removes_version_dir(ctx, cfg, conn, clock, tmp_path, monkeypatch):
    acct = published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123)

    def locked(*args, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "add_version", locked)
    worker(ctx, cfg).tick()
    assert list(cfg.shelf_root(acct).iterdir()) == []        # каталог версии без строки не остался


def test_big_ipa_does_not_eat_share_reserve(ctx, cfg, conn, clock, tmp_path):
    # до скачивания размер неизвестен (нужно 4 ГБ), после — запас 3 ГБ сверяется с настоящим размером
    acct = published(conn, clock)
    ctx.tool.ipas[123] = make_ipa(tmp_path / "s.ipa", item_id=123)
    size = ctx.tool.ipas[123].stat().st_size
    ctx.tool.on_download = lambda app_id: ctx.free.update(bytes=size + 2 * GB)
    worker(ctx, cfg).tick()
    app = store.list_apps(conn, 1)[0]
    assert app.status == "error" and "мало места в архиве" in app.last_error
    assert list(cfg.shelf_root(acct).iterdir()) == [] and not (cfg.tmp_dir / "web" / "dl-1-123").exists()
    assert ctx.sent == [f"appshelf: мало места на {socket.gethostname()}"]


def test_failed_update_keeps_working_version_installable(ctx, cfg, conn, clock):
    seed_app(conn, cfg, clock, versions=("1.0",), make_dirs=True)
    set_session(conn, 1, "ok")
    store.set_app_status(conn, 1, 123, "error", "старый сбой")      # «Повторить» из прежней ошибки
    ctx.tool.latest[123] = "901"
    ctx.tool.errors["download"] = ipatool.IpatoolError("timeout", "ipatool не уложился в 1800 с")
    worker(ctx, cfg).tick()
    app = store.list_apps(conn, 1)[0]
    assert (app.status, app.last_error) == ("ok", "ipatool: ipatool не уложился в 1800 с")


def test_link_to_app_not_in_purchases_is_not_retried(ctx, cfg, conn, clock):
    acct = owner(conn, clock)
    set_session(conn, 1, "ok")
    store.add_manual(conn, 1, 987654321, clock.iso())               # ошиблись в ID
    ctx.tool.errors["download"] = LicenseNotFound("license_not_found", "no license")
    worker(ctx, cfg).tick()
    app = store.list_apps(conn, 1)[0]
    assert (app.status, app.last_error) == ("nolicense", jobs.NOT_PURCHASED)
    assert store.retry(conn, 1, 987654321, clock.iso()) is False
    assert store.no_license_ids(conn, 1) == {987654321}
    jobs.unpublish(cfg, conn, acct, 987654321)                       # заглушка уходит и из «Истории»
    assert store.list_purchases(conn, 1) == []


def test_unpublish_keeps_confirmed_history_rows(ctx, cfg, conn, clock, monkeypatch):
    monkeypatch.setattr(removed, "load", lambda: [{"id": 5, "name": "СберБанк Онлайн"}, {"id": 7, "name": "ВТБ"}])
    acct = owner(conn, clock)
    store.add_known(conn, 1, 5, "СберБанк Онлайн", "license")       # лицензию нашла проверка справочника
    store.publish(conn, 1, 5, clock.iso())
    seed_app(conn, cfg, clock, app_id=6, versions=("1.0",))          # из истории покупок
    store.add_manual(conn, 1, 7, clock.iso())                        # по ссылке: ID из справочника, не подтверждено
    for app_id in (5, 6, 7):
        jobs.unpublish(cfg, conn, acct, app_id)
    assert sorted(r["app_id"] for r in store.list_purchases(conn, 1)) == [5, 6]


def test_unpublish_needs_archive(ctx, cfg, conn, clock):
    acct = owner(conn, clock)
    seed_app(conn, cfg, clock, versions=("1.0",))
    cfg.archive.rmdir()                                              # шара отвалилась
    with pytest.raises(jobs.ArchiveUnavailable):
        jobs.unpublish(cfg, conn, acct, 123)
    assert store.list_apps(conn, 1) and store.current_version(conn, 1, 123)


def test_failed_share_delete_goes_to_journal(ctx, cfg, conn, clock, monkeypatch, capsys):
    acct = owner(conn, clock)
    seed_app(conn, cfg, clock, versions=("1.0",), make_dirs=True)
    (cfg.shelf_root(acct) / "123-1.0" / "app.ipa").write_bytes(b"ipa")
    real = os.unlink

    def busy(path, *args, **kw):
        if str(path).endswith("app.ipa"):
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real(path, *args, **kw)

    monkeypatch.setattr(os, "unlink", busy)
    jobs.unpublish(cfg, conn, acct, 123)
    assert store.list_apps(conn, 1) == [] and "не удалось удалить" in capsys.readouterr().err


def test_check_user_refuses_root(cfg, monkeypatch):
    # от root ipatool не расшифрует ни одну учётку — ночь пометила бы все Apple ID «вход истёк»
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(SystemExit, match="sudo -u appshelf"):
        jobs.check_user(cfg)


def shelf_with_version(cfg, token: str, app_id: int = 7) -> Path:
    """Каталог полки с одной версией и manifest со ссылками на этот токен."""
    d = cfg.archive / token / f"{app_id}-1.0"
    d.mkdir(parents=True)
    (d / "app.ipa").write_bytes(b"ipa")
    (d / "manifest.plist").write_bytes(ipa.manifest(f"{cfg.public_base}/d/{token}/{d.name}/app.ipa",
                                                    f"{cfg.public_base}/d/{token}/{d.name}/icon.png", "b", "1.0", "T"))
    return d


def test_v2_shelves_move_off_pub_token(ctx, cfg, conn, clock):
    # каталог версии 2 — HMAC от PUB_TOKEN: PUB_TOKEN из ссылки владельца раскрывал бы все полки
    owner(conn, clock)                                                 # перенесён из версии 1: остаётся <PUB_TOKEN>
    petr = member(conn, clock)
    conn.execute("UPDATE accounts SET shelf='' WHERE id=?", (petr.id,))
    old = cfg.hmac_token(petr.id)
    shelf_with_version(cfg, old)
    shelf_with_version(cfg, TOKEN)
    jobs.move_shelves(cfg, conn)
    petr = people.get_account(conn, petr.id)
    assert petr.shelf not in ("", old) and petr.shelf_next == "" and not (cfg.archive / old).exists()
    manifest = (cfg.archive / petr.shelf / "7-1.0" / "manifest.plist").read_bytes()
    assert f"/d/{petr.shelf}/7-1.0/app.ipa".encode() in manifest and old.encode() not in manifest
    assert people.get_account(conn, 1).legacy_pub and (cfg.archive / TOKEN / "7-1.0").is_dir()
    jobs.move_shelves(cfg, conn)                                       # повторный старт ничего не трогает
    assert people.get_account(conn, petr.id).shelf == petr.shelf


def test_interrupted_move_is_finished_next_time(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    new = "ab" * 16
    shelf_with_version(cfg, petr.shelf)
    people.set_shelf_next(conn, petr.id, new)
    os.replace(cfg.archive / petr.shelf, cfg.archive / new)           # упали после переименования
    jobs.move_shelves(cfg, conn)
    moved = people.get_account(conn, petr.id)
    assert (moved.shelf, moved.shelf_next) == (new, "")
    assert f"/d/{new}/".encode() in (cfg.archive / new / "7-1.0" / "manifest.plist").read_bytes()


def test_rotate_owner_shelf_leaves_pub_token(ctx, cfg, conn, clock):
    acct = owner(conn, clock)
    shelf_with_version(cfg, TOKEN)
    jobs.move_shelf(cfg, conn, acct)
    moved = people.get_account(conn, 1)
    assert not moved.legacy_pub and cfg.shelf_token(moved) == moved.shelf != TOKEN
    assert not (cfg.archive / TOKEN).exists() and (cfg.archive / moved.shelf / "7-1.0" / "app.ipa").is_file()


def test_move_refused_while_apple_id_downloads(ctx, cfg, conn, clock):
    petr = member(conn, clock, session="ok")
    seed_app(conn, cfg, clock, app_id=7, acct=petr)
    petr = people.get_account(conn, petr.id)                           # с history_at загруженной истории
    store.take_job(conn)                                               # публикация Петра качается
    with pytest.raises(jobs.AccountBusy):
        jobs.move_shelf(cfg, conn, petr)
    assert people.get_account(conn, petr.id) == petr


def test_move_of_removed_apple_id_does_nothing(ctx, cfg, conn, clock):
    petr = member(conn, clock)
    jobs.remove_account(cfg, conn, petr)                               # удалили, пока перенос ждал замки
    jobs.move_shelf(cfg, conn, petr)
    assert people.get_account(conn, petr.id) is None and not cfg.shelf_root(petr).exists()


def test_move_waits_for_removal_of_apple_id(ctx, cfg, conn, clock, monkeypatch):
    petr = member(conn, clock)
    monkeypatch.setattr(jobs, "REMOVE_LOCK_WAIT", 0)
    with ipatool.hold(cfg.locks_dir / f"{petr.id}.lock"):              # удаление этого Apple ID идёт
        with pytest.raises(jobs.AccountBusy):
            jobs.move_shelf(cfg, conn, petr)
    assert people.get_account(conn, petr.id) == petr


def test_fetch_for_removed_apple_id_creates_no_shelf(ctx, cfg, conn, clock, tmp_path):
    petr = published(conn, clock, app_id=7, acct=member(conn, clock))
    tool = ctx.tools.setdefault(petr.id, FakeTool())
    tool.ipas[7] = make_ipa(tmp_path / "p.ipa", item_id=7)
    jobs.remove_account(cfg, conn, petr)                               # удалили, пока скачивание ждало замок
    with pytest.raises(store.AccountGone):
        jobs.fetch_version(ctx.env, conn, petr, 7, cfg.tmp_dir / "nightly")
    assert tool.calls == []
    assert_shelf_gone(conn, cfg, petr)


def test_download_after_move_goes_to_new_shelf(ctx, cfg, conn, clock, tmp_path):
    petr = published(conn, clock, app_id=7, acct=member(conn, clock))  # задание взяли до переноса,
    jobs.move_shelf(cfg, conn, petr)                                   # а качать начали после
    ctx.tools.setdefault(petr.id, FakeTool()).ipas[7] = make_ipa(tmp_path / "p.ipa", item_id=7)
    jobs.fetch_version(ctx.env, conn, petr, 7, cfg.tmp_dir / "web")
    moved = people.get_account(conn, petr.id)
    assert (cfg.shelf_root(moved) / "7-1.2.3").is_dir() and not cfg.shelf_root(petr).exists()
