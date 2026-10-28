"""«Связать» двух участников (spec 2026-10-28 §5 «Связка»): кто остаётся, перенос Google-аккаунтов и Android-полки."""
import os

import pytest

from appshelf import ipatool, jobs, people
from appshelf.android import db
from helpers import make_account, owner


def google_only(conn, clock, sub, name="Гость", role=people.MEMBER):
    uid = people.create_user(conn, name, role, None, clock.iso())
    people.create_google(conn, uid, sub, f"{sub}@gmail.example", name, clock.iso())
    return uid


def put_version(cfg, conn, clock, uid, package, code, signer="A"):
    """Карточка и версия на Android-полке участника; каталог версии с файлом, в котором — чей он."""
    shelf = db.ensure_shelf(conn, uid, clock.iso())
    root = cfg.archive / shelf.shelf
    name = db.version_dir(conn, shelf.id, root, package, code)
    (root / name).mkdir(parents=True)
    (root / name / "app.apk").write_text(f"{uid}:{package}:{code}")
    db.put_app(conn, shelf.id, package, package, "rustore", package, clock.iso())
    db.add_version(conn, root, shelf.id, package, db.NewApk(str(code), code, 24, 34, "", signer, "rustore", 10,
                                                            "0" * 64, name), clock.iso())
    return shelf


def files(cfg, shelf) -> dict:
    root = cfg.archive / shelf.shelf
    return {p.parent.name: p.read_text() for p in root.glob("*/app.apk")} if root.exists() else {}


def rows(conn, shelf) -> list:
    return sorted((r["package"], r["version_code"], r["dir"]) for r in conn.execute(
        "SELECT package, version_code, dir FROM android_versions WHERE shelf_id=?", (shelf.id,)))


def test_link_roles(conn, clock):
    own = owner(conn, clock)
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    g1, g2 = google_only(conn, clock, "g1"), google_only(conn, clock, "g2")
    assert people.link_roles(conn, own.user_id, g1, [own.user_id, g1]) == (own.user_id, g1)  # Apple остаётся
    assert people.link_roles(conn, g1, petr.user_id, [g1, petr.user_id]) == (petr.user_id, g1)
    assert people.link_roles(conn, own.user_id, petr.user_id, [own.user_id, petr.user_id]) is None
    assert people.link_roles(conn, g1, g2, [g2, g1]) == (g2, g1)                 # без Apple — чья запись раньше
    assert people.link_roles(conn, g1, g2, [g1, g2]) == (g1, g2)
    boss = google_only(conn, clock, "g3", role=people.OWNER)                      # без Apple ID — владелец
    assert people.link_roles(conn, g1, boss, [g1, boss]) == (boss, g1)
    assert people.link_roles(conn, g1, g1, [g1]) is None


def test_link_moves_google_and_android_shelf(ctx, cfg, conn, clock):
    own = owner(conn, clock)
    g = google_only(conn, clock, "g1")
    shelf = put_version(cfg, conn, clock, g, "ru.x", 5)
    token = people.create_link(conn, people.LOGIN, "", g, clock.iso())
    jobs.link_users(cfg, conn, own.user_id, g)
    assert people.get_user(conn, g) is None and people.google_by_sub(conn, "g1").user_id == own.user_id
    assert db.shelf_for_user(conn, own.user_id) == db.Shelf(shelf.id, own.user_id, shelf.shelf, "")
    assert files(cfg, shelf) == {"ru.x-5": f"{g}:ru.x:5"}                         # полка просто перешла
    assert people.login_link_user(conn, token, clock.iso()) is None               # запасной вход ушедшего — прочь
    assert people.get_account(conn, own.id) is not None


def test_link_merges_shelves_conflict_keeps_keeper(ctx, cfg, conn, clock):
    own = owner(conn, clock)
    g = google_only(conn, clock, "g1")
    keep = put_version(cfg, conn, clock, own.user_id, "ru.x", 5)
    gone = put_version(cfg, conn, clock, g, "ru.x", 7, signer="B")
    put_version(cfg, conn, clock, g, "ru.y", 1)
    db.enqueue(conn, gone.id, "check", clock.iso(), package="ru.y")
    db.enqueue(conn, gone.id, "add", clock.iso(), payload={"source": "link", "ref": "https://a.example/z.apk"})
    jobs.link_users(cfg, conn, own.user_id, g)
    assert rows(conn, keep) == [("ru.x", 5, "ru.x-5"), ("ru.y", 1, "ru.y-1")]
    assert files(cfg, keep) == {"ru.x-5": f"{own.user_id}:ru.x:5", "ru.y-1": f"{g}:ru.y:1"}
    assert db.get_app(conn, keep.id, "ru.x")["signer"] == "A"                     # совпавший — у остающегося
    assert db.get_shelf(conn, gone.id) is None and not (cfg.archive / gone.shelf).exists()
    for table in ("android_apps", "android_versions", "android_jobs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE shelf_id=?", (gone.id,)).fetchone()[0] == 0
    assert sorted((j["kind"], j["package"]) for j in conn.execute(
        "SELECT * FROM android_jobs WHERE shelf_id=? AND status='queued'", (keep.id,))) == \
        [("add", ""), ("check", "ru.y")]                                         # ждущие задания — за полкой


def test_link_moves_dirs_with_rename(ctx, cfg, conn, clock):
    own = owner(conn, clock)
    g = google_only(conn, clock, "g1")
    keep = put_version(cfg, conn, clock, own.user_id, "ru.z", 1)
    (cfg.archive / keep.shelf / "ru.x-5").mkdir()                                # каталог без строки — занят
    (cfg.archive / keep.shelf / "ru.x-5" / "app.apk").write_text("чужой")
    put_version(cfg, conn, clock, g, "ru.x", 5)
    jobs.link_users(cfg, conn, own.user_id, g)
    assert ("ru.x", 5, "ru.x-5-2") in rows(conn, keep)
    assert files(cfg, keep)["ru.x-5-2"] == f"{g}:ru.x:5" and files(cfg, keep)["ru.x-5"] == "чужой"


def test_link_restores_dirs_when_move_fails(ctx, cfg, conn, clock, monkeypatch):
    own = owner(conn, clock)
    g = google_only(conn, clock, "g1")
    keep = put_version(cfg, conn, clock, own.user_id, "ru.z", 1)
    gone = put_version(cfg, conn, clock, g, "ru.a", 1)
    put_version(cfg, conn, clock, g, "ru.b", 1)
    before = (files(cfg, keep), files(cfg, gone), rows(conn, keep), rows(conn, gone))
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("обрыв шары")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        jobs.link_users(cfg, conn, own.user_id, g)
    monkeypatch.setattr(os, "replace", real)
    assert (files(cfg, keep), files(cfg, gone), rows(conn, keep), rows(conn, gone)) == before
    assert people.get_user(conn, g) is not None and people.google_by_sub(conn, "g1").user_id == g


def test_link_refused_while_downloading(ctx, cfg, conn, clock, monkeypatch):
    own = owner(conn, clock)
    g = google_only(conn, clock, "g1")
    gone = put_version(cfg, conn, clock, g, "ru.x", 5)
    db.enqueue(conn, gone.id, "check", clock.iso(), package="ru.x")
    db.take_job(conn)                                                            # задание полки уходящего идёт
    with pytest.raises(jobs.AccountBusy):
        jobs.link_users(cfg, conn, own.user_id, g)
    db.requeue_running(conn)
    monkeypatch.setattr(jobs, "REMOVE_LOCK_WAIT", 0)
    with ipatool.hold(cfg.download_lock):                                        # чьё-то скачивание
        with pytest.raises(jobs.AccountBusy):
            jobs.link_users(cfg, conn, own.user_id, g)
    assert people.get_user(conn, g) is not None and db.get_shelf(conn, gone.id) == gone
    assert files(cfg, gone) == {"ru.x-5": f"{g}:ru.x:5"}


def test_link_refused_when_gone_has_apple(ctx, cfg, conn, clock):
    owner(conn, clock)
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    g = google_only(conn, clock, "g1")
    with pytest.raises(jobs.LinkRefused):
        jobs.link_users(cfg, conn, g, petr.user_id)                             # Apple ID не переходит никогда
    assert people.get_account(conn, petr.id).user_id == petr.user_id and people.get_user(conn, g) is not None
