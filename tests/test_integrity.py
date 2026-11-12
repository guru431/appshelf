import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import stat
from contextlib import closing

import pytest

from appshelf import cli, config, integrity, jobs, store, webauth
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


def test_ipatool_home_without_apple_id(two, cfg, conn):
    """HOME без строки Apple ID сам не удаляется (Worker.recover убирает только помеченные): его показывает check
    живой базы; снимок с другим набором Apple ID его не видит."""
    for name in ("1", str(two["petr_acct"].id), "77", ".new-0123456789abcdef", ".deleted-78"):
        (cfg.accounts_dir / name).mkdir(parents=True)
    files_before = tree(cfg.accounts_dir)
    assert integrity.check(cfg, conn).ok
    rep = integrity.check(cfg, conn, homes=True)
    assert [(p.kind, p.account) for p in rep.problems] == [("orphan_home", 77)]
    assert "Apple ID №77" in rep.text() and tree(cfg.accounts_dir) == files_before


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
    assert not (cfg.status_dir / integrity.STAMP).exists()            # сбой — отметки для мониторинга нет
    dest.mkdir(parents=True)
    (dest / "notes.txt").write_text("чужой файл", encoding="utf-8")
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        monkeypatch.setattr(jobs, "now_iso", lambda day=day: f"{day}T02:30:00+00:00")
        assert cli.main(["backup", "--daily", "--keep", "2"]) == 0
    assert (cfg.status_dir / integrity.STAMP).read_text(encoding="utf-8") == "2026-10-21T02:30:00+00:00\n"
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


def test_daily_backup_after_code_rollback(monkeypatch, tmp_path, clock, capsys):
    # база с аддитивным переходом новее кода: connect её пускает, значит и снимать её надо
    dest = tmp_path / "share" / "backup"
    dest.mkdir(parents=True)
    monkeypatch.setenv("APPSHELF_BACKUP_DIR", str(dest))
    cfg = cli_env(monkeypatch, tmp_path)
    cli_shelf(cfg, clock, tmp_path)
    with closing(store.connect(cfg.db)) as c:
        store.set_state(c, store.READABLE_BY_KEY, str(store.SCHEMA_VERSION))
        c.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    store.connect(cfg.db).close()                                     # откат разрешён — web работает
    assert cli.main(["backup", "--daily"]) == 0
    assert dump(next(dest.glob("appshelf-*.db"))) == dump(cfg.db)
    capsys.readouterr()
    assert cli.main(["check"]) == 2                                   # проверке нужна ровно своя схема
    assert f"схема версии {store.SCHEMA_VERSION + 1}" in capsys.readouterr().err


def test_backup_refuses_dir_inside_archive(monkeypatch, tmp_path, clock, capsys):
    cfg = cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPSHELF_BACKUP_DIR", str(cfg.archive / "backup"))
    cli_shelf(cfg, clock, tmp_path)
    (cfg.archive / "backup").mkdir()
    before = tree(cfg.archive)
    assert cli.main(["backup", "--daily"]) == 2
    assert "внутри архива" in capsys.readouterr().err
    assert cli.main(["backup", "--out", str(cfg.archive / "set")]) == 2
    assert "внутри архива" in capsys.readouterr().err
    assert tree(cfg.archive) == before and not (cfg.status_dir / integrity.STAMP).exists()


def test_damaged_snapshot_keeps_old_ones(tmp_path):
    db = tmp_path / "live.db"
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE t (x TEXT)")
        c.execute("CREATE INDEX t_x ON t (x)")
        c.executemany("INSERT INTO t VALUES (?)", [(f"строка {i:05}",) for i in range(3000)])
        c.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION}")
        c.commit()
        page = c.execute("PRAGMA page_size").fetchone()[0]
    with open(db, "r+b") as f:                                        # порча страниц в середине файла
        f.seek(page * 3 + 100)
        f.write(b"\xff" * (page * 2))
    dest = tmp_path / "share"
    dest.mkdir()
    (dest / "appshelf-2026-10-20.db").write_bytes(b"old")
    with pytest.raises(integrity.BackupError, match="quick_check"):
        integrity.daily_backup(db, dest, tmp_path / "work", "2026-10-21", keep=1)
    assert sorted(p.name for p in dest.iterdir()) == ["appshelf-2026-10-20.db"]   # прежний не удалён
    assert not (tmp_path / "work").exists()


def ipatool_tree(tmp_path, monkeypatch):
    """Код как на сервере: bin/ рядом с ipatool/ (исходники сборки). → (cfg, bin_dir, исходники)."""
    code = tmp_path / "code"
    (code / "bin").mkdir(parents=True)
    src = code / "ipatool"
    (src / "patches").mkdir(parents=True)
    (src / "auth").mkdir()
    (src / "UPSTREAM").write_bytes(b"URL=x\nCOMMIT=1\n")
    (src / "patches" / "02-b.patch").write_bytes(b"b\n")
    (src / "patches" / "01-a.patch").write_bytes(b"a\n")
    (src / "auth" / "UPSTREAM").write_bytes(b"URL=y\n")
    (src / "auth" / "main.go").write_bytes(b"package main\n")
    cfg = cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPSHELF_IPATOOL", str(code / "bin" / "ipatool"))
    return cfg, code / "bin", src


def test_check_shows_ipatool_build_drift_and_age(monkeypatch, tmp_path, capsys):
    cfg, bin_dir, src = ipatool_tree(tmp_path, monkeypatch)
    store.connect(cfg.db).close()
    stamp = bin_dir / "ipatool.src-sha256"
    # как deploy/install.sh: cat UPSTREAM patches/*.patch | sha256sum
    stamp.write_text(hashlib.sha256(b"URL=x\nCOMMIT=1\na\nb\n").hexdigest() + "\n", encoding="ascii")
    built = 1792000000                                                # 2026-10-14 UTC
    os.utime(stamp, (built, built))
    monkeypatch.setattr(jobs, "now_iso", lambda: "2026-11-09T03:00:00+00:00")
    assert cli.main(["check"]) == 0                                   # сборка на код возврата не влияет
    out = capsys.readouterr().out
    assert "ipatool: собран 2026-10-14 из выложенных исходников" in out
    assert "ipatool-auth: не собран" in out and "после DSA openssl или раз в квартал" in out
    (src / "patches" / "03-c.patch").write_bytes(b"c\n")
    monkeypatch.setattr(jobs, "now_iso", lambda: "2027-01-20T03:00:00+00:00")
    assert cli.main(["check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ipatool"][0] == \
        "ipatool: собран 2026-10-14 НЕ из выложенных исходников — пересоберите; старше 90 дней"


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


@pytest.mark.parametrize("deep", [False, True])
def test_android_versions_without_shelf_fail_check(monkeypatch, tmp_path, capsys, deep):
    cfg = cli_env(monkeypatch, tmp_path)
    with closing(store.connect(cfg.db)) as c:
        c.execute("INSERT INTO android_versions (shelf_id,package,version_name,version_code,min_sdk,target_sdk,"
                  "signer,source,size,sha256,dir,downloaded_at,role) "
                  "VALUES (999,'ru.example.app','1',1,21,35,'signer','upload',3,'hash','ru.example.app-1','t0','current')")
        rep = integrity.check(cfg, c, deep=deep)
    assert not rep.ok
    assert (rep.apks, rep.android_shelves) == (1, 0)
    assert [(p.kind, p.shelf) for p in rep.problems] == [("no_android_shelf", 999)]
    assert cli.main(["check", "--json", *(["--deep"] if deep else [])]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_restore_report_is_offline_private_and_includes_manual_connections(monkeypatch, tmp_path, capsys):
    cfg = cli_env(monkeypatch, tmp_path)
    with closing(store.connect(cfg.db)) as c:
        from appshelf import people
        uid = people.create_user(c, "Person", "member", None, "t0")
        gid = people.create_google(c, uid, "sub", "person@example.org", "Person", "t0")
        people.set_play(c, gid, "ok", "t0")
    isolated = tmp_path / "isolated"
    snapshot = integrity.backup(cfg.db, isolated)
    archive = isolated / "pub"
    archive.mkdir()
    protected = isolated / "protected"
    protected.mkdir()
    (protected / "appshelf.env").write_text("PRIVATE_ENV_VALUE", encoding="utf-8")
    key = protected / "cookie-key"
    key.write_bytes(b"PRIVATE_COOKIE_KEY" + b"k" * 14)  # ровно webauth.KEY_LEN
    from appshelf.android import net
    def no_network(*args, **kwargs):
        pytest.fail("restore report must not contact providers")
    monkeypatch.setattr(net.Http, "request", no_network)
    before = tree(isolated)
    args = ["restore-report", "--db", str(snapshot), "--archive", str(archive),
            "--secrets-dir", str(protected), "--cookie-key", str(key), "--deep"]
    assert cli.main(args) == 0
    raw = capsys.readouterr().out
    rep = json.loads(raw)
    assert rep["offline_ok"] and not rep["external_connections_verified"]
    assert rep["schema_version"] == store.SCHEMA_VERSION and rep["code_version"]
    assert rep["connections_policy"] == "reconnect"
    assert next(s for s in rep["manual_steps"] if s["provider"] == "Google Play")["accounts"] == 1
    assert all(s not in raw for s in ("PRIVATE_ENV_VALUE", "PRIVATE_COOKIE_KEY", "person@example.org"))
    assert tree(isolated) == before
    key.unlink()
    assert cli.main(args) == 1
    assert not json.loads(capsys.readouterr().out)["protected_files"]["cookie-key"]


def restore_set(monkeypatch, tmp_path):
    cfg = cli_env(monkeypatch, tmp_path)
    store.connect(cfg.db).close()
    snapshot = integrity.backup(cfg.db, tmp_path / "set")
    (tmp_path / "set" / "pub").mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "appshelf.env").write_text("X=1", encoding="utf-8")
    return ["restore-report", "--db", str(snapshot), "--archive", str(tmp_path / "set" / "pub"),
            "--secrets-dir", str(protected), "--cookie-key", str(protected / "cookie-key")], protected


def test_restore_report_rejects_cookie_key_service_would_not_load(monkeypatch, tmp_path, capsys):
    args, protected = restore_set(monkeypatch, tmp_path)
    (protected / "cookie-key").write_bytes(b"k" * 32 + b"\n")  # перевод строки после ручного копирования
    with pytest.raises(ValueError):
        webauth.load_key(protected / "cookie-key")               # appshelf-web с таким не стартует
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out)["protected_files"] == {"appshelf.env": True, "cookie-key": False}


def test_restore_report_needs_sources_ca_when_configured(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APPSHELF_SOURCES_CA", str(tmp_path / "etc" / "sources-ca.pem"))
    args, protected = restore_set(monkeypatch, tmp_path)
    (protected / "cookie-key").write_bytes(b"k" * 32)
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out)["protected_files"]["APPSHELF_SOURCES_CA"] is False
    (protected / "sources-ca.pem").write_text("-----BEGIN CERTIFICATE-----", encoding="ascii")
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["offline_ok"]

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


def test_android_pending_at_predictable_name(ctx, cfg, conn, clock, tmp_path):
    """Версия ждёт решения по подписи, а каталог — <пакет>-<versionCode> (скачана до случайных имён)."""
    shelf, root = android_shelf(ctx, cfg, conn, clock, tmp_path)
    conn.execute("UPDATE android_versions SET role='pending'")
    rep = integrity.check(cfg, conn)
    assert kinds(rep) == ["pending_name"] and rep.problems[0].path == "ru.example.app-1"
    assert "угадываемым именем" in rep.text()
    hidden = "ru.example.app-1-p" + "0" * 32
    (root / "ru.example.app-1").rename(root / hidden)
    conn.execute("UPDATE android_versions SET dir=?", (hidden,))
    assert integrity.check(cfg, conn).ok


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
