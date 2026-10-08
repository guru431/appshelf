import sqlite3

import pytest

from appshelf import people, store

V1 = """
CREATE TABLE purchases (app_id INTEGER PRIMARY KEY, bundle_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
    purchase_date TEXT NOT NULL DEFAULT '', refreshed_at TEXT NOT NULL, version TEXT NOT NULL DEFAULT '');
CREATE TABLE apps (app_id INTEGER PRIMARY KEY, name TEXT NOT NULL, bundle_id TEXT NOT NULL, published_at TEXT NOT NULL,
    status TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL DEFAULT '');
CREATE TABLE versions (id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL, version TEXT NOT NULL, build TEXT NOT NULL,
    external_version_id TEXT NOT NULL, min_ios TEXT NOT NULL, device_family TEXT NOT NULL, size INTEGER NOT NULL,
    dir TEXT NOT NULL, downloaded_at TEXT NOT NULL, role TEXT NOT NULL, built TEXT NOT NULL DEFAULT '');
CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, app_id INTEGER, status TEXT NOT NULL,
    created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE removed_checks (app_id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL);
"""


def v1(path):
    """База версии 1 с одним Apple ID: покупка, опубликованное приложение, версия, задание, состояние входа."""
    c = sqlite3.connect(path)
    c.executescript(V1)
    c.execute("INSERT INTO purchases VALUES (123, 'ru.sberbank.onlineiphone', 'СберБанк', '2020-01-02', 't0', '12.15')")
    c.execute("INSERT INTO apps VALUES (123, 'СберБанк', 'ru.sberbank.onlineiphone', 't0', 'ok', '', '')")
    c.execute("INSERT INTO versions VALUES (5, 123, '12.15', '1', '900', '13.0', 'iphone', 10, '123-12.15', 't0', "
              "'current', '')")
    c.execute("INSERT INTO jobs VALUES (9, 'publish', 123, 'done', 't0', 't1', '')")
    c.execute("INSERT INTO removed_checks VALUES (564177498, 't0')")
    c.executemany("INSERT INTO state VALUES (?, ?)", [
        ("session", "ok"), ("session_since", "t2"), ("account_name", "Иван Петров"), ("storefront", "RU"),
        ("expired_mail_sent", ""), ("nightly_last", "t3"), ("nightly_result", "обновлено 0")])
    c.commit()
    c.close()


def test_v1_shelf_becomes_owner_apple_id(tmp_path):
    db = tmp_path / "appshelf.db"
    v1(db)
    c = store.connect(db, "owner@example")
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    [user] = people.list_users(c)
    assert (user.id, user.name, user.role) == (1, "Иван Петров", "owner")
    a = people.get_account(c, 1)
    assert (a.user_id, a.email, a.name, a.storefront, a.session, a.session_since, a.legacy_pub, a.device_mac) == \
        (1, "owner@example", "Иван Петров", "RU", "ok", "t2", True, "")
    for t in store.SHELF_TABLES:
        assert {r[0] for r in c.execute(f"SELECT account_id FROM {t}")} == {1}, t
    assert [r["version"] for r in store.list_purchases(c, 1)] == ["12.15"]
    assert store.current_version(c, 1, 123)["id"] == 5 and store.no_license_ids(c, 1) == {564177498}
    assert c.execute("SELECT id FROM jobs").fetchone()[0] == 9
    assert {r["key"] for r in c.execute("SELECT key FROM state")} == {"nightly_last", "nightly_result"}
    assert not c.execute("SELECT name FROM sqlite_master WHERE name LIKE '%\\_v1' ESCAPE '\\'").fetchall()
    c.close()
    c = store.connect(db, "owner@example")                        # повторное открытие ничего не меняет
    assert len(people.all_accounts(c)) == 1 and len(store.list_purchases(c, 1)) == 1
    c.close()


def test_v1_with_data_needs_owner(tmp_path):
    db = tmp_path / "appshelf.db"
    v1(db)
    with pytest.raises(store.MigrationError, match="APPSHELF_OWNER"):
        store.connect(db, "")
    c = sqlite3.connect(db)                                        # переход откатился целиком
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1
    assert c.execute("SELECT value FROM state WHERE key='session'").fetchone()[0] == "ok"
    c.close()


def test_empty_database_gets_schema_without_owner(tmp_path):
    c = store.connect(tmp_path / "appshelf.db", "")
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert people.list_users(c) == [] and people.all_accounts(c) == []
    c.close()


def test_oldest_v1_without_version_columns(tmp_path, clock):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE purchases (app_id INTEGER PRIMARY KEY, bundle_id TEXT NOT NULL DEFAULT '', "
                "name TEXT NOT NULL DEFAULT '', purchase_date TEXT NOT NULL DEFAULT '', refreshed_at TEXT NOT NULL)")
    old.execute("INSERT INTO purchases VALUES (1, 'a', 'Старое', '', '')")
    old.commit()
    old.close()
    c = store.connect(db, "owner@example")
    store.upsert_purchases(c, 1, [{"id": 2, "bundleId": "b", "name": "Новое", "version": "1.2.3"}], clock.iso())
    assert {r["app_id"]: r["version"] for r in store.list_purchases(c, 1)} == {1: "", 2: "1.2.3"}
    assert people.get_account(c, 1).session == "none"
    c.close()
