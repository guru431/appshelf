import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import stat
from contextlib import closing

import pytest

from appshelf import cli, config, integrity, jobs, store
from appshelf.config import GB
from helpers import OWNER_EMAIL, TOKEN, FakeTool, make_account, owner, seed_app
from ipa_factory import make_ipa

PETR = "petr@example"


def publish(ctx, cfg, conn, clock, tmp_path, acct, app_id=123):
    """Настоящее скачивание (fetch_version с подменённым ipatool): файлы и строка — как на сервере. → каталог версии."""
    store.upsert_purchases(conn, acct.id, [{"id": app_id, "bundleId": "ru.example.app", "name": "Пример"}],
                           clock.iso())
    store.publish(conn, acct.id, app_id, clock.iso())
    ctx.tools.setdefault(acct.id, FakeTool()).ipas[app_id] = make_ipa(tmp_path / f"src-{acct.id}-{app_id}.ipa",
                                                                      item_id=app_id)
    assert jobs.fetch_version(ctx.env, conn, acct, app_id, cfg.tmp_dir / "t") == "updated"
    return cfg.shelf_root(acct) / store.current_version(conn, acct.id, app_id)["dir"]


@pytest.fixture
def two(ctx, cfg, conn, clock, tmp_path):
    """Полка владельца (каталог <TOKEN>) и участника (случайный токен), по версии на каждой."""
    own = owner(conn, clock)
    petr = make_account(conn, clock, email=PETR, role="member", legacy=False)
    return {"owner": publish(ctx, cfg, conn, clock, tmp_path, own), "petr": publish(ctx, cfg, conn, clock, tmp_path,
                                                                                    petr, app_id=7), "petr_acct": petr}


def kinds(rep):
    return sorted(p.kind for p in rep.problems)


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
            for p in sorted(root.rglob("*"))}


def assert_private(rep, *tokens):
    for out in (rep.text(), json.dumps(rep.as_dict(), ensure_ascii=False)):
        for secret in (*tokens, OWNER_EMAIL, PETR):
            assert secret not in out


def test_fetch_version_saves_sha256(two, conn):
    ipa_file = two["owner"] / "app.ipa"
    assert store.current_version(conn, 1, 123)["sha256"] == hashlib.sha256(ipa_file.read_bytes()).hexdigest()


def test_healthy_shelves_have_no_problems_and_nothing_changes(two, cfg, conn):
    db_before, files_before = list(conn.iterdump()), tree(cfg.archive)
    for deep in (False, True):
        with closing(integrity.open_ro(cfg.db)) as ro:  # запись на таком соединении упала бы
            rep = integrity.check(cfg, ro, deep=deep)
        assert rep.ok, rep.text()
        assert (rep.versions, rep.accounts, rep.no_sha256) == (2, 2, 0)
        assert "проблем нет" in rep.text()
    assert list(conn.iterdump()) == db_before and tree(cfg.archive) == files_before


def test_missing_ipa_and_wrong_size(two, cfg, conn):
    (two["owner"] / "app.ipa").unlink()
    with open(two["petr"] / "app.ipa", "ab") as f:
        f.write(b"x")
    rep = integrity.check(cfg, conn)
    assert kinds(rep) == ["no_ipa", "size"]
    no_ipa, size = sorted(rep.problems, key=lambda p: p.kind)
    assert (no_ipa.account, no_ipa.path) == (1, "123-1.2.3/app.ipa")
    assert size.account == two["petr_acct"].id and "в базе" in size.detail
    assert not rep.ok and "нет app.ipa — 1" in rep.text()


def test_wrong_manifest_link_and_version(two, cfg, conn):
    m = two["petr"] / "manifest.plist"
    data = plistlib.loads(m.read_bytes())
    data["items"][0]["assets"][0]["url"] = data["items"][0]["assets"][0]["url"].replace(
        two["petr_acct"].shelf, "f" * 32)  # ссылка на прежний каталог — как после оборванного rotate-token
    data["items"][0]["metadata"]["bundle-version"] = "9.9"
    m.write_bytes(plistlib.dumps(data))
    rep = integrity.check(cfg, conn)
    assert kinds(rep) == ["manifest_url", "manifest_version"]
    url = next(p for p in rep.problems if p.kind == "manifest_url")
    assert "<другой токен>" in url.detail and "/d/<токен>/7-1.2.3/app.ipa" in url.detail
    assert_private(rep, TOKEN, two["petr_acct"].shelf, "f" * 32)


def test_broken_manifest_and_missing_icon(two, cfg, conn):
    (two["owner"] / "manifest.plist").write_bytes(b"not a plist")
    (two["owner"] / "icon.png").unlink()
    assert kinds(integrity.check(cfg, conn)) == ["bad_manifest", "no_icon"]


def test_orphans_are_reported_not_removed(two, cfg, conn, clock):
    own_root = cfg.shelf_root(owner(conn, clock))
    (own_root / "123-0.9").mkdir()
    (own_root / ".123-2.0.partial").mkdir()
    stranger = "beef" + "0" * 28
    (cfg.archive / stranger).mkdir()
    conn.execute("UPDATE accounts SET shelf_next=? WHERE id=?", ("a" * 32, two["petr_acct"].id))
    files_before = tree(cfg.archive)
    rep = integrity.check(cfg, conn)
    assert kinds(rep) == ["orphan", "partial", "shelf_next", "unknown"]
    assert next(p for p in rep.problems if p.kind == "orphan").path == "123-0.9"
    assert next(p for p in rep.problems if p.kind == "unknown").path == "beef…"
    assert_private(rep, TOKEN, two["petr_acct"].shelf, stranger, "a" * 32)
    assert tree(cfg.archive) == files_before


def test_shelf_dir_missing_and_version_dir_missing(cfg, conn, clock):
    own = owner(conn, clock)
    petr = make_account(conn, clock, email=PETR, role="member", legacy=False)
    seed_app(conn, cfg, clock, app_id=7, versions=("1.0",), acct=petr)  # каталога полки нет вовсе
    seed_app(conn, cfg, clock, app_id=123, versions=("1.0", "2.0"), make_dirs=True)
    (cfg.shelf_root(own) / "123-1.0").rmdir()
    rep = integrity.check(cfg, conn)
    assert sorted((p.kind, p.account) for p in rep.problems if p.kind in ("no_shelf", "no_dir")) == \
        [("no_dir", 1), ("no_shelf", petr.id)]


def test_deep_finds_wrong_sha256_and_foreign_ipa(two, cfg, conn, tmp_path):
    conn.execute("UPDATE versions SET sha256=? WHERE app_id=123", ("0" * 64,))
    assert integrity.check(cfg, conn).ok  # без --deep SHA-256 не считается
    make_ipa(two["petr"] / "app.ipa", item_id=8, external_id="901", bundle_id="ru.other.app")
    conn.execute("UPDATE versions SET size=?, sha256='' WHERE app_id=7", ((two["petr"] / "app.ipa").stat().st_size,))
    rep = integrity.check(cfg, conn, deep=True)
    assert kinds(rep) == ["ipa_bundle_id", "ipa_external_id", "ipa_item_id", "sha256"]
    assert rep.no_sha256 == 1 and "без сохранённой SHA-256: 1" in rep.text()
    assert_private(rep, TOKEN, two["petr_acct"].shelf)


def test_archive_unavailable(cfg, conn):
    cfg.archive.rmdir()
    with pytest.raises(jobs.ArchiveUnavailable):
        integrity.check(cfg, conn)


# --- CLI -----------------------------------------------------------------------

def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PUB_TOKEN", TOKEN)
    monkeypatch.setenv("APPSHELF_PUBLIC_BASE", "https://apps.example.com")
    monkeypatch.setenv("APPSHELF_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("APPSHELF_IPATOOL", str(tmp_path / "no-ipatool"))
    cfg = config.from_env()
    cfg.archive.mkdir(parents=True)
    return cfg


def cli_shelf(cfg, clock, tmp_path):
    """Живая база и архив с одной версией, через fetch_version. → каталог версии."""
    c = store.connect(cfg.db)
    try:
        acct = make_account(c, clock, email=PETR, role="owner", legacy=False)
        tool = FakeTool()
        tool.ipas[7] = make_ipa(tmp_path / "src.ipa", item_id=7)
        store.upsert_purchases(c, acct.id, [{"id": 7, "bundleId": "ru.example.app", "name": "Пример"}], clock.iso())
        store.publish(c, acct.id, 7, clock.iso())
        env = jobs.Env(cfg, lambda a: tool, now=clock.iso, disk_free=lambda p: 50 * GB)
        jobs.fetch_version(env, c, acct, 7, cfg.tmp_dir / "t")
        return cfg.shelf_root(acct) / store.current_version(c, acct.id, 7)["dir"], acct.shelf
    finally:
        c.close()


def test_cli_check_exit_codes_and_json(monkeypatch, tmp_path, clock, capsys):
    cfg = cli_env(monkeypatch, tmp_path)
    vdir, shelf = cli_shelf(cfg, clock, tmp_path)
    assert cli.main(["check", "--deep"]) == 0
    assert "проблем нет" in capsys.readouterr().out
    (vdir / "icon.png").unlink()
    assert cli.main(["check", "--json"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["counts"] == {"no_icon": 1} and shelf not in out and PETR not in out


def dump(path):
    with closing(sqlite3.connect(path)) as c:
        return list(c.iterdump())


def test_backup_snapshot_and_check_of_the_copy(monkeypatch, tmp_path, clock, capsys):
    cfg = cli_env(monkeypatch, tmp_path)
    vdir, _ = cli_shelf(cfg, clock, tmp_path)
    out = tmp_path / "backup" / "2026-10-21"
    assert cli.main(["backup", "--out", str(out)]) == 0
    assert "check --db" in capsys.readouterr().out
    snap = out / "appshelf.db"
    assert sorted(p.name for p in out.iterdir()) == ["appshelf.db"]  # один файл: без -wal и временного
    assert dump(snap) == dump(cfg.db)
    copy = out / "pub"
    shutil.copytree(cfg.archive, copy)
    snap_before = snap.read_bytes()
    assert cli.main(["check", "--deep", "--db", str(snap), "--archive", str(copy)]) == 0
    assert snap.read_bytes() == snap_before and sorted(p.name for p in out.iterdir()) == ["appshelf.db", "pub"]
    (copy / vdir.relative_to(cfg.archive) / "app.ipa").unlink()
    capsys.readouterr()
    assert cli.main(["check", "--db", str(snap), "--archive", str(copy)]) == 1
    assert "нет app.ipa" in capsys.readouterr().out
    assert (vdir / "app.ipa").is_file()  # живой архив проверка копии не трогала


@pytest.mark.skipif(os.name != "posix", reason="права файлов — как на сервере (Linux)")
def test_backup_snapshot_is_private(monkeypatch, tmp_path, clock):
    cfg = cli_env(monkeypatch, tmp_path)
    cli_shelf(cfg, clock, tmp_path)
    assert cli.main(["backup", "--out", str(tmp_path / "out")]) == 0
    assert stat.S_IMODE((tmp_path / "out" / "appshelf.db").stat().st_mode) == 0o600


def test_backup_refuses_non_empty_dir(monkeypatch, tmp_path, clock, capsys):
    cfg = cli_env(monkeypatch, tmp_path)
    cli_shelf(cfg, clock, tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "old.db").write_bytes(b"")
    assert cli.main(["backup", "--out", str(tmp_path / "out")]) == 2
    assert "не пуст" in capsys.readouterr().err
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["old.db"]


def test_daily_backup_keeps_last_snapshots(monkeypatch, tmp_path, clock, capsys):
    # appshelf-backup.timer: снимок за день — в APPSHELF_BACKUP_DIR, старше keep последних — прочь
    dest = tmp_path / "share" / "backup"
    monkeypatch.setenv("APPSHELF_BACKUP_DIR", str(dest))
    cfg = cli_env(monkeypatch, tmp_path)
    cli_shelf(cfg, clock, tmp_path)
    assert cli.main(["backup", "--daily"]) == 2                     # каталога нет: шара не смонтирована?
    assert "нет каталога" in capsys.readouterr().err and not dest.exists()
    dest.mkdir(parents=True)
    (dest / "notes.txt").write_text("чужой файл", encoding="utf-8")
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        monkeypatch.setattr(jobs, "now_iso", lambda day=day: f"{day}T02:30:00+00:00")
        assert cli.main(["backup", "--daily", "--keep", "2"]) == 0
    assert sorted(p.name for p in dest.iterdir()) == ["appshelf-2026-10-20.db", "appshelf-2026-10-21.db", "notes.txt"]
    assert dump(dest / "appshelf-2026-10-21.db") == dump(cfg.db)
    assert not (cfg.tmp_dir / "backup").exists()                       # локальный черновик убран
    if os.name == "posix":
        assert stat.S_IMODE((dest / "appshelf-2026-10-21.db").stat().st_mode) == 0o600


def test_daily_backup_needs_backup_dir_setting(monkeypatch, tmp_path, clock, capsys):
    monkeypatch.delenv("APPSHELF_BACKUP_DIR", raising=False)
    cfg = cli_env(monkeypatch, tmp_path)
    cli_shelf(cfg, clock, tmp_path)
    assert cli.main(["backup", "--daily"]) == 2
    assert "APPSHELF_BACKUP_DIR" in capsys.readouterr().err
    with pytest.raises(SystemExit):                                    # набор восстановления или ночной — что-то одно
        cli.main(["backup", "--daily", "--out", str(tmp_path / "out")])


def test_check_refuses_snapshot_of_other_schema(monkeypatch, tmp_path, clock, capsys):
    cfg = cli_env(monkeypatch, tmp_path)
    cli_shelf(cfg, clock, tmp_path)
    old = tmp_path / "old.db"
    with closing(sqlite3.connect(old)) as c:
        c.execute("PRAGMA user_version = 3")
    before = old.read_bytes()
    assert cli.main(["check", "--db", str(old)]) == 2
    assert "схема версии 3" in capsys.readouterr().err
    assert old.read_bytes() == before  # без перехода схемы
    assert cli.main(["check", "--db", str(tmp_path / "nope.db")]) == 2


def test_cli_check_reports_unavailable_archive(monkeypatch, tmp_path, clock, capsys):
    cfg = cli_env(monkeypatch, tmp_path)
    cli_shelf(cfg, clock, tmp_path)
    cfg.archive.rename(tmp_path / "gone")
    assert cli.main(["check"]) == 2
    assert "недоступен" in capsys.readouterr().err


# --- Android-полки -------------------------------------------------------------

def android_shelf(ctx, cfg, conn, clock, tmp_path):
    """Android-полка владельца с одной версией — настоящий конвейер (fetch.run_job), как на сервере."""
    from apk_factory import make_apk
    from appshelf.android import db, fetch
    owner(conn, clock)
    shelf = db.ensure_shelf(conn, 1, clock.iso())
    ctx.sources.apks["https://a.example/x.apk"] = make_apk(tmp_path / "x.apk")
    db.enqueue(conn, shelf.id, "add", clock.iso(), payload={"source": "link", "ref": "https://a.example/x.apk",
                                                            "name": ""})
    fetch.run_job(ctx.env, conn, db.take_job(conn))
    return shelf, cfg.archive / shelf.shelf


def test_android_shelf_is_not_unknown(ctx, cfg, conn, clock, tmp_path):
    shelf, root = android_shelf(ctx, cfg, conn, clock, tmp_path)
    for deep in (False, True):
        rep = integrity.check(cfg, conn, deep=deep)
        assert rep.ok, rep.text()
        assert (rep.apks, rep.android_shelves) == (1, 1)
    assert "APK: 1, Android-полок: 1" in rep.text()
    assert_private(rep, shelf.shelf)


def test_android_orphan_and_partial(ctx, cfg, conn, clock, tmp_path):
    shelf, root = android_shelf(ctx, cfg, conn, clock, tmp_path)
    (root / "ru.zzz-1").mkdir()
    (root / ".ru.example.app-2.partial").mkdir()
    conn.execute("UPDATE android_shelves SET shelf_next='fedcba9876543210fedcba9876543210'")
    (cfg.archive / "fedcba9876543210fedcba9876543210").mkdir()
    rep = integrity.check(cfg, conn)
    assert kinds(rep) == ["orphan", "partial", "shelf_next"]             # каталог нового токена — не unknown
    assert {p.path for p in rep.problems} == {"ru.zzz-1", ".ru.example.app-2.partial", ""}
    assert all(p.shelf == shelf.id and p.account is None for p in rep.problems)
    assert f"Android-полка №{shelf.id}: ru.zzz-1" in rep.text()


def test_android_shelf_next_reported(ctx, cfg, conn, clock, tmp_path):
    shelf, root = android_shelf(ctx, cfg, conn, clock, tmp_path)
    conn.execute("UPDATE android_shelves SET shelf_next='fedcba9876543210fedcba9876543210'")  # смена токена прервалась
    rep = integrity.check(cfg, conn)
    assert kinds(rep) == ["shelf_next"]
    assert (rep.problems[0].shelf, rep.problems[0].account) == (shelf.id, None)
    assert f"Android-полка №{shelf.id}" in rep.text()
    assert_private(rep, shelf.shelf, "fedcba9876543210fedcba9876543210")


def test_android_missing_apk(ctx, cfg, conn, clock, tmp_path):
    shelf, root = android_shelf(ctx, cfg, conn, clock, tmp_path)
    (root / "ru.example.app-1" / "ru.example.app-1.0.apk").unlink()
    (root / "ru.example.app-1" / "icon.png").unlink()
    assert kinds(integrity.check(cfg, conn)) == ["no_apk", "no_icon"]
    shutil.rmtree(root / "ru.example.app-1")
    assert kinds(integrity.check(cfg, conn)) == ["no_dir"]
    conn.execute("UPDATE android_versions SET dir='../x'")
    assert kinds(integrity.check(cfg, conn)) == ["bad_dir"]
    shutil.rmtree(root)
    rep = integrity.check(cfg, conn)
    assert kinds(rep) == ["no_shelf"] and rep.problems[0].shelf == shelf.id


def test_android_deep_sha_mismatch(ctx, cfg, conn, clock, tmp_path):
    shelf, root = android_shelf(ctx, cfg, conn, clock, tmp_path)
    f = root / "ru.example.app-1" / "ru.example.app-1.0.apk"
    data = bytearray(f.read_bytes())
    data[-1] ^= 0xFF                                   # тот же размер, другое содержимое
    f.write_bytes(bytes(data))
    assert integrity.check(cfg, conn).ok
    assert kinds(integrity.check(cfg, conn, deep=True)) == ["apk_sha256"]
    with open(f, "ab") as out:
        out.write(b"x")
    assert kinds(integrity.check(cfg, conn)) == ["apk_size"]
