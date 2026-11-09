"""«Что нового»: текст источника хранится по версии, на карточке — только текущей и только как текст."""
import hashlib
import io
import json
import zipfile

from apk_factory import make_apk
from android_fakes import FakeHttp
from appshelf.android import db, fdroid_index as index, fetch
from appshelf.android.sources import Got, Latest

PKG = "ru.example.app"
URL = "https://codeberg.org/example/app"


def test_set_whats_new_cleans_limits_and_keeps_three_versions(conn, clock, acct):
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Пример", "codeberg", "example/app", clock.iso())
    def notes():
        return json.loads(conn.execute("SELECT value FROM state WHERE key=?", (f"android_whatsnew:{shelf.id}:{PKG}",)
                                       ).fetchone()["value"])

    db.set_whats_new(conn, shelf.id, PKG, 1, "a\r\n\r\n\r\n\r\nb\x00\u202e  \n")
    assert notes() == {"1": "a\n\nb"}
    for code in (2, 3, 4):
        db.set_whats_new(conn, shelf.id, PKG, code, "x" * (db.WHATS_NEW_MAX + 10))
    db.set_whats_new(conn, shelf.id, PKG, 5, " \x07 ")                  # пустой после очистки — не пишется
    assert sorted(notes()) == ["2", "3", "4"] and notes()["4"] == "x" * db.WHATS_NEW_MAX + "…"
    db.copy_whats_new(conn, shelf.id, shelf.id + 100, PKG)
    db.delete_shelf(conn, shelf.id)
    assert [r["key"] for r in conn.execute("SELECT key FROM state WHERE key LIKE 'android_whatsnew:%'")] == [
        f"android_whatsnew:{shelf.id + 100}:{PKG}"]


def test_download_stores_text_for_new_version_and_card_shows_current_only(ctx, cfg, conn, clock, acct, tmp_path):
    shelf = db.ensure_shelf(conn, acct.user_id, clock.iso())
    ctx.sources.apks["example/app"] = make_apk(tmp_path / "a.apk", version_code=5)
    ctx.sources.got["example/app"] = Got("", "", None, "cb:1", whats_new="Исправления")
    job = db.enqueue(conn, shelf.id, "add", clock.iso(), payload={"source": "codeberg", "ref": "example/app"})
    fetch.run_job(ctx.env, conn, db.take_job(conn))
    assert conn.execute("SELECT status FROM android_jobs WHERE id=?", (job,)).fetchone()["status"] == "done"
    assert db.list_apps(conn, shelf.id)[0].whats_new == "Исправления"
    # Новая версия без текста: прежний текст относится к прежней версии и на карточке не показывается.
    ctx.sources.apks["example/app"] = make_apk(tmp_path / "b.apk", version_code=6)
    ctx.sources.got["example/app"] = Got("", "", None, "cb:2")
    ctx.sources._latest["example/app"] = Latest(None, "cb:2", True)
    db.enqueue(conn, shelf.id, "check", clock.iso(), package=PKG)
    fetch.run_job(ctx.env, conn, db.take_job(conn))
    app = db.list_apps(conn, shelf.id)[0]
    assert app.current["version_code"] == 6 and app.whats_new == ""
    db.remove_app(conn, cfg.archive / shelf.shelf, shelf.id, PKG)
    assert conn.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'android_whatsnew:%'").fetchone()[0] == 0


def test_card_shows_text_escaped_and_collapsed(web, conn, tmp_path):
    web.sources.apks["example/app"] = make_apk(tmp_path / "a.apk", version_name="2.0")
    web.sources.got["example/app"] = Got("", "", None, "cb:1", whats_new="<script>alert(1)</script>\n**Новое**")
    assert web.client.post("/android/add/link", data={"url": URL}, follow_redirects=False).status_code == 303
    job = db.take_job(conn)
    assert json.loads(job["payload"])["source"] == "codeberg"
    fetch.run_job(web.app.state.w.env, conn, job)
    page = web.client.get("/android").text
    assert "<summary>Что нового в 2.0</summary>" in page and "Codeberg" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;<br>**Новое**" in page and "<script>alert(1)" not in page


def test_fdroid_v2_whats_new_prefers_russian_then_english_and_survives_cache(tmp_path):
    def version(code, name, notes):
        digest = hashlib.sha256(name.encode() + bytes([code])).hexdigest()
        return digest, {"file": {"name": f"/app_{code}.apk", "sha256": digest}, "whatsNew": notes,
                        "manifest": {"versionName": name, "versionCode": code, "signer": {"sha256": ["a" * 64]},
                                     "nativecode": ["arm64-v8a"] if code % 2 else ["x86_64"]}}
    versions = dict([version(1, "1.0", {"en-US": "old"}), version(2, "2.0", {"en-US": "new", "ru": "новое"}),
                     version(3, "2.0", {"en-US": "new", "ru": "новое"})])
    body = json.dumps({"packages": {
        PKG: {"metadata": {"name": {"en-US": "Example"}}, "versions": versions},
        "ru.other": {"metadata": {"name": {"en-US": "Other"}}, "versions": dict([version(4, "4.0", {"de": "neu"})])},
        "ru.odd": {"metadata": {"name": {"en-US": "Odd"}}, "versions": dict([version(5, "5.0", "не словарь")])},
    }}).encode()
    apps = index.parse_v2(body)
    app = apps[PKG]
    assert (app.whats_new, app.whats_new_version) == ("новое", "2.0")
    assert app.notes(app.select()) == "новое" and app.notes(next(b for b in app.builds if b.code == 1)) == ""
    assert apps["ru.other"].whats_new == apps["ru.odd"].whats_new == ""   # только ru или en; странный вид — без текста
    entry = {"timestamp": 1800000000000, "index": {"name": "/index-v2.json", "size": len(body),
                                                   "sha256": hashlib.sha256(body).hexdigest()}}
    jar = io.BytesIO()
    with zipfile.ZipFile(jar, "w") as z:
        z.comment = b"appshelf-test-index"
        z.writestr("entry.json", json.dumps(entry))
    repo = index.REPOSITORIES["fdroid_archive"][0]
    http = FakeHttp(raw={repo + "entry.jar": (200, {}, jar.getvalue()), repo + "index-v2.json": (200, {}, body)})
    index.Catalog(http, archive=True, cache_dir=tmp_path).info(PKG)
    cached = index.Catalog(FakeHttp(), archive=True, cache_dir=tmp_path).info(PKG)[0]
    assert (cached.whats_new, cached.whats_new_version) == ("новое", "2.0")
