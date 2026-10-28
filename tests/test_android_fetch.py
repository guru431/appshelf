import json
from pathlib import Path

import pytest

from android_fakes import FakeSources
from apk_factory import cert_sha, make_apk, png_bytes
from appshelf import ipa, jobs, store
from appshelf.android import db, fetch, net
from appshelf.android.sources import Got, Latest
from appshelf.config import GB

PKG = "ru.example.app"


def shelf_of(conn, clock, uid=1):
    return db.ensure_shelf(conn, uid, clock.iso())


def run(ctx, conn, shelf, kind, clock, package="", **payload) -> dict:
    """Задание в очередь, обработчик его берёт и выполняет; итог — строка задания."""
    job_id = db.enqueue(conn, shelf.id, kind, clock.iso(), package=package, payload=payload or None)
    job = db.take_job(conn)
    assert job["id"] == job_id
    fetch.run_job(ctx.env, conn, job)
    return dict(conn.execute("SELECT * FROM android_jobs WHERE id=?", (job_id,)).fetchone())


def add_rustore(ctx, conn, clock, apk_path, signer=None, name="Пример"):
    """Как «Добавить» из поиска RuStore: карточка сразу, задание add."""
    shelf = shelf_of(conn, clock)
    ctx.sources.apks[PKG] = apk_path
    ctx.sources.got[PKG] = Got(name, cert_sha(b"CERT-A") if signer is None else signer, None)
    db.put_app(conn, shelf.id, PKG, name, "rustore", PKG, clock.iso())
    return shelf, run(ctx, conn, shelf, "add", clock, package=PKG, source="rustore", ref=PKG, name=name)


def on_shelf(cfg, shelf) -> list[str]:
    root = cfg.archive / shelf.shelf
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def versions(conn, shelf) -> dict:
    return {r["role"]: r["version_code"] for r in conn.execute(
        "SELECT role, version_code FROM android_versions WHERE shelf_id=?", (shelf.id,))}


def test_add_from_rustore_puts_current_version(ctx, cfg, conn, clock, tmp_path, monkeypatch):
    modes, real = {}, Path.chmod

    def chmod(self, mode, **kw):
        modes[self] = mode
        return real(self, mode, **kw)

    monkeypatch.setattr(Path, "chmod", chmod)
    src = make_apk(tmp_path / "src.apk", icon=png_bytes(96), abis=("arm64-v8a",))
    shelf, job = add_rustore(ctx, conn, clock, src)
    assert job["status"] == "done"
    app = db.get_app(conn, shelf.id, PKG)
    assert (app["status"], app["signer"], app["name"], app["last_error"]) == ("ok", cert_sha(b"CERT-A"), "Пример", "")
    root = cfg.archive / shelf.shelf
    d = root / f"{PKG}-1"
    assert sorted(p.name for p in d.iterdir()) == ["icon.png", f"{PKG}-1.0.apk"]
    assert (d / f"{PKG}-1.0.apk").read_bytes() == src.read_bytes()
    assert modes[root] == 0o711 and modes[root / f".{PKG}-1.partial"] == 0o755
    assert {p.name: m for p, m in modes.items() if p.parent.name == f".{PKG}-1.partial"} == \
        {"icon.png": 0o644, f"{PKG}-1.0.apk": 0o644}
    cur = db.list_apps(conn, shelf.id)[0].current
    assert (cur["version_name"], cur["min_sdk"], cur["abis"], cur["source"], cur["size"]) == \
        ("1.0", 26, "arm64-v8a", "rustore", src.stat().st_size)
    assert fetch.file_url(cfg, shelf, f"{PKG}-1", "icon.png") == \
        f"https://apps.example/d/{shelf.shelf}/{PKG}-1/icon.png"
    assert not (cfg.tmp_dir / "web" / f"android-{job['id']}").exists()
    assert not [p for p in root.iterdir() if p.name.startswith(".")]


def test_icon_from_source_then_placeholder(ctx, cfg, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    ctx.sources.apks["https://a.example/x.apk"] = make_apk(tmp_path / "x.apk")
    ctx.sources.got["https://a.example/x.apk"] = Got("", "", png_bytes(32))
    run(ctx, conn, shelf, "add", clock, source="link", ref="https://a.example/x.apk", name="")
    assert (cfg.archive / shelf.shelf / f"{PKG}-1" / "icon.png").read_bytes() == png_bytes(32)
    ctx.sources.apks["https://b.example/y.apk"] = make_apk(tmp_path / "y.apk", package="ru.other")
    run(ctx, conn, shelf, "add", clock, source="link", ref="https://b.example/y.apk", name="")
    assert (cfg.archive / shelf.shelf / "ru.other-1" / "icon.png").read_bytes() == ipa.placeholder_png()


def test_rustore_signer_mismatch_rejected(ctx, cfg, conn, clock, tmp_path):
    shelf, job = add_rustore(ctx, conn, clock, make_apk(tmp_path / "src.apk"), signer="f" * 64)
    assert (job["status"], job["error"]) == ("error", "подпись не совпала с RuStore")
    app = db.get_app(conn, shelf.id, PKG)
    assert (app["status"], app["last_error"]) == ("error", "подпись не совпала с RuStore")
    assert versions(conn, shelf) == {} and on_shelf(cfg, shelf) == []


def test_link_package_mismatch_rejected(ctx, cfg, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    url = "https://bank.example/a.apk"
    ctx.sources.apks[url] = make_apk(tmp_path / "a.apk", package="ru.a")
    run(ctx, conn, shelf, "add", clock, source="link", ref=url, name="А")
    ctx.sources.apks[url] = make_apk(tmp_path / "b.apk", package="ru.b", version_code=2)
    ctx.sources._latest[url] = Latest(None, "new||", True)
    job = run(ctx, conn, shelf, "check", clock, package="ru.a")
    assert job["status"] == "error" and "ru.b" in job["error"]
    app = db.get_app(conn, shelf.id, "ru.a")
    assert (app["status"], app["check_result"]) == ("ok", "error") and "ru.b" in app["last_error"]
    assert versions(conn, shelf) == {"current": 1} and db.get_app(conn, shelf.id, "ru.b") is None
    assert sorted(p.name for p in (cfg.archive / shelf.shelf).iterdir()) == ["ru.a-1"]


@pytest.mark.parametrize("code", [5, 4])
def test_same_or_lower_version_is_same(ctx, cfg, conn, clock, tmp_path, code):
    shelf, _ = add_rustore(ctx, conn, clock, make_apk(tmp_path / "v5.apk", version_code=5))
    ctx.sources.apks[PKG] = make_apk(tmp_path / "v.apk", version_code=code)
    ctx.sources._latest[PKG] = Latest(99, "", True)            # источник обещал новее — пришла та же или старее
    job = run(ctx, conn, shelf, "check", clock, package=PKG)
    assert job["status"] == "done"
    app = db.get_app(conn, shelf.id, PKG)
    assert (app["status"], app["check_result"], app["checked_at"]) == ("ok", "same", clock.iso())
    assert versions(conn, shelf) == {"current": 5}
    assert sorted(p.name for p in (cfg.archive / shelf.shelf).iterdir()) == [f"{PKG}-5"]


def test_rustore_check_without_newer_does_not_download(ctx, conn, clock, tmp_path):
    shelf, _ = add_rustore(ctx, conn, clock, make_apk(tmp_path / "v5.apk", version_code=5))
    ctx.sources._latest[PKG] = Latest(5, "", True)
    ctx.sources.calls.clear()
    assert run(ctx, conn, shelf, "check", clock, package=PKG)["status"] == "done"
    assert ctx.sources.calls == [("latest", "rustore", PKG)]
    assert db.get_app(conn, shelf.id, PKG)["check_result"] == "same"


def test_check_downloads_newer_and_rotates(ctx, cfg, conn, clock, tmp_path):
    shelf, _ = add_rustore(ctx, conn, clock, make_apk(tmp_path / "v1.apk", version_code=1))
    ctx.sources.apks[PKG] = make_apk(tmp_path / "v2.apk", version_code=2, version_name="2.0")
    ctx.sources._latest[PKG] = Latest(2, "", True)
    run(ctx, conn, shelf, "check", clock, package=PKG)
    assert versions(conn, shelf) == {"current": 2, "previous": 1}
    assert db.get_app(conn, shelf.id, PKG)["check_result"] == "updated"
    assert (cfg.archive / shelf.shelf / f"{PKG}-2" / f"{PKG}-2.0.apk").exists()


class Breaking(FakeSources):
    """Обрыв посередине: часть файла записана, затем FetchError."""

    def fetch(self, source, ref, dest):
        dest.write_bytes(b"PK\x03\x04part")
        raise net.FetchError("обрыв при скачивании: ConnectionResetError")


def test_failed_download_keeps_previous(ctx, cfg, conn, clock, tmp_path):
    shelf, _ = add_rustore(ctx, conn, clock, make_apk(tmp_path / "v1.apk"))
    ctx.env.sources = Breaking(latest={PKG: Latest(2, "", True)})
    job = run(ctx, conn, shelf, "check", clock, package=PKG)
    assert (job["status"], job["error"]) == ("error", "обрыв при скачивании: ConnectionResetError")
    app = db.get_app(conn, shelf.id, PKG)
    assert (app["status"], app["check_result"]) == ("ok", "error")
    assert versions(conn, shelf) == {"current": 1}
    assert sorted(p.name for p in (cfg.archive / shelf.shelf).iterdir()) == [f"{PKG}-1"]
    assert not list((cfg.tmp_dir / "web").iterdir())


def test_low_space_refuses_before_download(ctx, cfg, conn, clock, tmp_path):
    ctx.free["bytes"] = jobs.FREE_MIN - 1
    shelf, job = add_rustore(ctx, conn, clock, make_apk(tmp_path / "src.apk"))
    assert job["status"] == "error" and job["error"].startswith("мало места")
    assert not [c for c in ctx.sources.calls if c[0] == "fetch"]
    assert ctx.sent == [jobs.LOW_SPACE_SUBJECT]
    assert db.get_app(conn, shelf.id, PKG)["status"] == "error"


def test_reserve_checked_with_known_size(ctx, cfg, conn, clock, tmp_path):
    src = make_apk(tmp_path / "src.apk")
    archive_free = iter([50 * GB, jobs.RESERVE + src.stat().st_size - 1])   # до скачивания и после: размер известен
    ctx.env.disk_free = lambda path: next(archive_free) if path == cfg.archive else 50 * GB
    shelf, job = add_rustore(ctx, conn, clock, src)
    assert job["status"] == "error" and "запас" in job["error"]
    assert versions(conn, shelf) == {} and on_shelf(cfg, shelf) == []


def test_other_signer_becomes_pending(ctx, cfg, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    url = "https://bank.example/a.apk"
    ctx.sources.apks[url] = make_apk(tmp_path / "a.apk", cert=b"CERT-A")
    run(ctx, conn, shelf, "add", clock, source="link", ref=url, name="")
    ctx.sources.apks[url] = make_apk(tmp_path / "b.apk", cert=b"CERT-B", version_code=2)
    ctx.sources._latest[url] = Latest(None, "e2||", True)
    run(ctx, conn, shelf, "check", clock, package=PKG)
    assert versions(conn, shelf) == {"current": 1, "pending": 2}
    app = db.get_app(conn, shelf.id, PKG)
    assert (app["status"], app["check_result"], app["signer"]) == ("ok", "pending", cert_sha(b"CERT-A"))
    ctx.sources._latest[url] = Latest(None, "e3||", True)          # та же pending снова — не новая
    run(ctx, conn, shelf, "check", clock, package=PKG)
    assert db.get_app(conn, shelf.id, PKG)["check_result"] == "same"


def test_name_falls_back_to_hint_then_package(ctx, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    for url, path, name in (("https://a.example/1.apk", make_apk(tmp_path / "1.apk", package="ru.one", label="@0x7f120001"), "Банк"),
                            ("https://a.example/2.apk", make_apk(tmp_path / "2.apk", package="ru.two", label="@0x7f120001"), ""),
                            ("https://a.example/3.apk", make_apk(tmp_path / "3.apk", package="ru.three", label="Три"), "Не то")):
        ctx.sources.apks[url] = path
        run(ctx, conn, shelf, "add", clock, source="link", ref=url, name=name)
    assert {a.package: a.name for a in db.list_apps(conn, shelf.id)} == \
        {"ru.one": "Банк", "ru.two": "ru.two", "ru.three": "Три"}
    app = db.get_app(conn, shelf.id, "ru.one")
    assert (app["source"], app["source_ref"], app["source_etag"]) == ("link", "https://a.example/1.apk", "")


def test_link_add_keeps_etag_from_download(ctx, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    url = "https://a.example/x.apk"
    ctx.sources.apks[url] = make_apk(tmp_path / "x.apk")
    ctx.sources.got[url] = Got("", "", None, '"e"||5')
    run(ctx, conn, shelf, "add", clock, source="link", ref=url, name="")
    assert db.get_app(conn, shelf.id, PKG)["source_etag"] == '"e"||5'


def test_upload_new_card_and_new_version(ctx, cfg, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    up = cfg.tmp_dir / "web" / "up-1.apk"
    up.parent.mkdir(parents=True)
    make_apk(up, label="")
    job = run(ctx, conn, shelf, "upload", clock, path=str(up), name="Своё")
    assert job["status"] == "done" and not up.exists()
    app = db.get_app(conn, shelf.id, PKG)
    assert (app["name"], app["source"], app["source_ref"]) == ("Своё", "upload", "")
    make_apk(up, package="ru.other", version_code=2)
    job = run(ctx, conn, shelf, "upload", clock, path=str(up), name="", package=PKG)
    assert job["status"] == "error" and "ru.other" in job["error"] and not up.exists()
    make_apk(up, version_code=2)
    run(ctx, conn, shelf, "upload", clock, path=str(up), name="", package=PKG)
    assert versions(conn, shelf) == {"current": 2, "previous": 1}


def test_garbage_upload_is_job_error_without_card(ctx, cfg, conn, clock):
    shelf = shelf_of(conn, clock)
    up = cfg.tmp_dir / "web" / "up-2.apk"
    up.parent.mkdir(parents=True)
    up.write_bytes(b"not an apk")
    job = run(ctx, conn, shelf, "upload", clock, path=str(up), name="Мусор")
    assert job["status"] == "error" and "не APK" in job["error"]
    assert db.list_apps(conn, shelf.id) == [] and not up.exists()


def test_removed_while_downloading_is_cancelled(ctx, cfg, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)

    class Removing(FakeSources):
        def fetch(self, source, ref, dest):
            db.remove_app(conn, cfg.archive / shelf.shelf, shelf.id, PKG)
            return super().fetch(source, ref, dest)

    ctx.env.sources = Removing({PKG: make_apk(tmp_path / "a.apk")})
    db.put_app(conn, shelf.id, PKG, "Пример", "rustore", PKG, clock.iso())
    job = run(ctx, conn, shelf, "add", clock, package=PKG, source="rustore", ref=PKG, name="Пример")
    assert job["status"] == "cancelled"
    assert db.list_apps(conn, shelf.id) == [] and on_shelf(cfg, shelf) == []


def test_worker_takes_android_job_when_no_apple_job(ctx, cfg, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    ctx.sources.apks["https://a.example/x.apk"] = make_apk(tmp_path / "x.apk")
    db.enqueue(conn, shelf.id, "add", clock.iso(), payload={"source": "link", "ref": "https://a.example/x.apk",
                                                            "name": ""})
    w = jobs.Worker(ctx.env, lambda: store.connect(cfg.db))
    assert w.tick() is True
    assert db.get_app(conn, shelf.id, PKG)["status"] == "ok"
    assert w.tick() is False


def test_recover_marks_lost_upload_as_error(ctx, cfg, conn, clock, tmp_path):
    shelf = shelf_of(conn, clock)
    up = cfg.tmp_dir / "web" / "up-3.apk"
    up.parent.mkdir(parents=True)
    make_apk(up)
    lost = db.enqueue(conn, shelf.id, "upload", clock.iso(), payload={"path": str(up), "name": ""})
    running = db.enqueue(conn, shelf.id, "add", clock.iso(), payload={"source": "link", "ref": "https://a.example/x",
                                                                      "name": ""})
    conn.execute("UPDATE android_jobs SET status='running' WHERE id=?", (running,))
    jobs.Worker(ctx.env, lambda: store.connect(cfg.db)).recover()
    rows = {r["id"]: (r["status"], r["error"]) for r in conn.execute("SELECT * FROM android_jobs")}
    assert rows[running] == ("queued", "")
    assert rows[lost][0] == "error" and "загрузите" in rows[lost][1]
    assert json.loads(conn.execute("SELECT payload FROM android_jobs WHERE id=?", (lost,)).fetchone()[0])["path"]


def test_apk_name_is_safe():
    assert fetch.apk_name("ru.x", "1.0 beta/2") == "ru.x-1.0_beta_2.apk"
    assert fetch.apk_name("ru.x", "") == "ru.x-v.apk"
