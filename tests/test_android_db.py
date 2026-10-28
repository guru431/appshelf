import re
import sqlite3

from appshelf import store
from appshelf.android import db

NOW = "2026-10-28T00:00:00+00:00"
A, B = "a" * 64, "b" * 64


def v(code: int, signer: str = A, dir: str = "") -> db.NewApk:
    return db.NewApk(version_name=f"{code}.0", version_code=code, min_sdk=26, target_sdk=34, abis="", signer=signer,
                     source="rustore", size=10, sha256="0" * 64, dir=dir or f"ru.x-{code}")


def add(conn, root, sid, ver: db.NewApk) -> str:
    """Каталог версии на диске — как после скачивания, затем строка."""
    (root / ver.dir).mkdir(parents=True)
    return db.add_version(conn, root, sid, "ru.x", ver, NOW)


def shelf_with_app(conn, tmp_path):
    shelf = db.ensure_shelf(conn, 1, NOW)
    db.put_app(conn, shelf.id, "ru.x", "Икс", "rustore", "ru.x", NOW)
    return tmp_path / shelf.shelf, shelf.id


def v4_schema() -> str:
    """Схема версии 4: нынешняя без таблиц Android и счётчиков PIN."""
    return ";".join(s for s in store.SCHEMA.split(";") if "android_" not in s and "pin_attempts" not in s)


def test_v4_database_upgrades_to_v5_keeping_apple_rows(tmp_path):
    path = tmp_path / "appshelf.db"
    c = sqlite3.connect(path)
    c.executescript(v4_schema())
    c.execute("INSERT INTO users (id, name, role, created_at) VALUES (1, 'Иван', 'owner', 't0')")
    c.execute("INSERT INTO accounts (id, user_id, email, created_at) VALUES (1, 1, 'owner@example', 't0')")
    c.execute("INSERT INTO apps (account_id, app_id, name, bundle_id, published_at, status) "
              "VALUES (1, 123, 'СберБанк', 'ru.sb', 't0', 'ok')")
    c.execute("PRAGMA user_version = 4")
    c.commit()
    c.close()
    c = store.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert [r["email"] for r in c.execute("SELECT email FROM accounts")] == ["owner@example"]
    assert [r["name"] for r in c.execute("SELECT name FROM apps")] == ["СберБанк"]
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"android_shelves", "android_apps", "android_versions", "android_jobs"} <= tables
    assert store.get_state(c, store.READABLE_BY_KEY) == "4"     # откат на код версии 4 возможен
    c.close()
    c = store.connect(path)                                      # повторное открытие ничего не меняет
    assert c.execute("SELECT COUNT(*) FROM apps").fetchone()[0] == 1
    c.close()


def test_rotation_current_previous_kept(conn, tmp_path):
    root, sid = shelf_with_app(conn, tmp_path)
    assert add(conn, root, sid, v(1)) == "current"
    assert add(conn, root, sid, v(2)) == "current"
    assert add(conn, root, sid, v(3)) == "current"
    view = db.list_apps(conn, sid)[0]
    assert (view.current["version_code"], view.previous["version_code"], view.kept, view.pending) == (3, 2, None, None)
    assert (view.status, view.signer) == ("ok", A)
    assert not (root / "ru.x-1").exists() and (root / "ru.x-2").exists()
    assert db.pin_version(conn, root, sid, "ru.x", view.previous["id"])
    add(conn, root, sid, v(4))
    view = db.list_apps(conn, sid)[0]
    assert (view.current["version_code"], view.previous["version_code"], view.kept["version_code"]) == (4, 3, 2)
    assert db.unpin_version(conn, root, sid, "ru.x", view.kept["id"])
    assert db.list_apps(conn, sid)[0].kept is None and not (root / "ru.x-2").exists()


def test_other_signer_goes_pending_and_is_accepted(conn, tmp_path):
    root, sid = shelf_with_app(conn, tmp_path)
    add(conn, root, sid, v(1))
    assert db.add_version(conn, root, sid, "ru.x", v(2, signer=B), NOW) == "pending"
    assert db.list_apps(conn, sid)[0].current["version_code"] == 1
    assert db.accept_pending(conn, root, sid, "ru.x")
    view = db.list_apps(conn, sid)[0]
    assert (view.current["version_code"], view.signer, view.pending) == (2, B, None)
    assert view.previous["version_code"] == 1
    assert not db.accept_pending(conn, root, sid, "ru.x")


def test_new_pending_replaces_old_pending(conn, tmp_path):
    root, sid = shelf_with_app(conn, tmp_path)
    add(conn, root, sid, v(1))
    assert add(conn, root, sid, v(2, signer=B)) == "pending"
    assert add(conn, root, sid, v(3, signer=B)) == "pending"
    view = db.list_apps(conn, sid)[0]
    assert (view.current["version_code"], view.pending["version_code"]) == (1, 3)
    assert not (root / "ru.x-2").exists() and (root / "ru.x-3").exists()


def test_remove_app_drops_rows_dirs_and_queued_jobs(conn, tmp_path):
    root, sid = shelf_with_app(conn, tmp_path)
    add(conn, root, sid, v(1))
    add(conn, root, sid, v(2))
    job = db.enqueue(conn, sid, "check", NOW, package="ru.x")
    db.remove_app(conn, root, sid, "ru.x")
    assert db.list_apps(conn, sid) == [] and list(root.iterdir()) == []
    assert conn.execute("SELECT status FROM android_jobs WHERE id=?", (job,)).fetchone()["status"] == "cancelled"


def test_remove_dirs_ignores_foreign_names(tmp_path):
    for name in ("ru.x-5", "x", "123-1.0"):
        (tmp_path / "root" / name).mkdir(parents=True)
    db.remove_dirs(tmp_path / "root", ["ru.x-5", "..", "x", "123-1.0"])
    assert sorted(p.name for p in (tmp_path / "root").iterdir()) == ["123-1.0", "x"]
    assert (tmp_path / "root").exists()


def test_version_dir_skips_taken_names(conn, tmp_path):
    root, sid = shelf_with_app(conn, tmp_path)
    assert db.version_dir(conn, sid, root, "ru.x", 7) == "ru.x-7"
    add(conn, root, sid, v(7))                                   # строка в базе
    (root / "ru.x-7-2").mkdir()                                  # каталог на диске без строки
    assert db.version_dir(conn, sid, root, "ru.x", 7) == "ru.x-7-3"
    assert db.ANDROID_DIR_RE.match("ru.x-7-3") and not db.ANDROID_DIR_RE.match("7-ru.x")


def test_jobs_take_in_order_and_requeue(conn):
    shelf = db.ensure_shelf(conn, 1, NOW)
    first = db.enqueue(conn, shelf.id, "add", NOW, payload={"source": "rustore", "ref": "ru.x", "name": "Икс"})
    second = db.enqueue(conn, shelf.id, "check", NOW, package="ru.y")
    assert db.has_active(conn, shelf.id)
    job = db.take_job(conn)
    assert (job["id"], job["kind"]) == (first, "add")
    assert conn.execute("SELECT status FROM android_jobs WHERE id=?", (first,)).fetchone()["status"] == "running"
    db.requeue_running(conn)
    assert db.take_job(conn)["id"] == first
    db.finish_job(conn, first, "error", NOW, "сбой" * 200)
    assert len(conn.execute("SELECT error FROM android_jobs WHERE id=?", (first,)).fetchone()["error"]) == 500
    assert [j["id"] for j in db.open_jobs(conn, shelf.id)] == [first]          # check — не виден на странице
    assert db.take_job(conn)["id"] == second
    assert db.take_job(conn) is None
    db.finish_job(conn, second, "done", NOW)
    other = db.ensure_shelf(conn, 2, NOW)
    assert not db.dismiss_job(conn, other.id, first)                            # чужая полка
    assert db.dismiss_job(conn, shelf.id, first)
    assert db.open_jobs(conn, shelf.id) == [] and not db.has_active(conn, shelf.id)


def test_shelf_token_is_random_and_stable(conn):
    assert db.shelf_for_user(conn, 1) is None
    shelf = db.ensure_shelf(conn, 1, NOW)
    assert re.fullmatch(r"[0-9a-f]{32}", shelf.shelf) and shelf.user_id == 1 and shelf.shelf_next == ""
    assert db.ensure_shelf(conn, 1, NOW) == shelf == db.get_shelf(conn, shelf.id) == db.shelf_for_user(conn, 1)
    other = db.ensure_shelf(conn, 2, NOW)
    assert other.shelf != shelf.shelf
    assert db.all_shelves(conn) == [shelf, other]


def test_single_public_shelf(conn):
    assert db.public_shelf(conn) is None
    mine = db.ensure_shelf(conn, 1, NOW)
    a = db.ensure_public_shelf(conn, NOW)
    b = db.ensure_public_shelf(conn, NOW)
    assert a == b and a.user_id is None and db.public_shelf(conn) == a and a.shelf != mine.shelf
    assert conn.execute("SELECT COUNT(*) FROM android_shelves WHERE user_id IS NULL").fetchone()[0] == 1
    db.set_shelf_next(conn, a.id, "f" * 32)
    assert db.public_shelf(conn).shelf_next == "f" * 32
    db.finish_shelf_move(conn, a.id)
    assert (db.public_shelf(conn).shelf, db.public_shelf(conn).shelf_next) == ("f" * 32, "")


def test_status_and_check_marks(conn, tmp_path):
    root, sid = shelf_with_app(conn, tmp_path)
    db.put_app(conn, sid, "ru.x", "Другое имя", "link", "https://a.example/x.apk", NOW)   # есть — не трогает
    assert (db.get_app(conn, sid, "ru.x")["name"], db.get_app(conn, sid, "ru.x")["status"]) == ("Икс", "queued")
    db.set_status(conn, sid, "ru.x", "error", "x" * 600)
    assert len(db.get_app(conn, sid, "ru.x")["last_error"]) == 500
    assert [r["package"] for r in db.apps_to_check(conn)] == ["ru.x"]
    db.set_checked(conn, sid, "ru.x", NOW, "same", etag='"e"||5')
    db.set_checked(conn, sid, "ru.x", NOW, "error")                             # etag не задан — прежний
    app = db.get_app(conn, sid, "ru.x")
    assert (app["checked_at"], app["check_result"], app["source_etag"]) == (NOW, "error", '"e"||5')
    db.set_source(conn, sid, "ru.x", "link", "https://a.example/x.apk")
    app = db.get_app(conn, sid, "ru.x")
    assert (app["source"], app["source_ref"], app["source_etag"]) == ("link", "https://a.example/x.apk", "")
    db.put_app(conn, sid, "ru.up", "Своё", "upload", "", NOW)
    db.set_status(conn, sid, "ru.up", "ok")
    assert [r["package"] for r in db.apps_to_check(conn)] == ["ru.x"]          # свой файл ночью не проверяется
