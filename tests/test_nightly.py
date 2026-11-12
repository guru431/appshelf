import socket
from dataclasses import replace

from appshelf import jobs, nightly, notify, people, store
from appshelf.android import db as android_db
from appshelf.config import GB
from appshelf.ipatool import SessionExpired
from helpers import FakeTool, assert_shelf_gone, make_account, owner, seed_app, set_session
from ipa_factory import make_ipa


def stamp(cfg) -> str:
    p = cfg.status_dir / nightly.STAMP
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def ready(conn, cfg, clock, versions=("1.0",), app_id=123, acct=None):
    acct = acct or owner(conn, clock)
    seed_app(conn, cfg, clock, app_id=app_id, versions=versions, make_dirs=True, acct=acct)
    for j in conn.execute("SELECT id FROM jobs WHERE status='queued'").fetchall():
        store.finish_job(conn, j["id"], "done", clock.iso())
    set_session(conn, acct.id, "ok")
    return acct


def test_new_version_becomes_current_previous_kept_older_removed(ctx, cfg, conn, clock, tmp_path):
    acct = ready(conn, cfg, clock, versions=("1.0", "1.1"))  # 1.1 — current (ext 901), 1.0 — previous
    ctx.tool.latest[123] = "902"
    ctx.tool.ipas[123] = make_ipa(tmp_path / "n.ipa", item_id=123, version="1.2", external_id="902")
    result = nightly.run(ctx.env, conn)
    assert {r["role"]: r["version"] for r in conn.execute("SELECT * FROM versions")} == \
        {"current": "1.2", "previous": "1.1"}
    assert not (cfg.shelf_root(acct) / "123-1.0").exists()
    assert result.startswith("owner@example: обновлено 1, без изменений 0, ошибок 0")
    assert stamp(cfg) == clock.iso() and store.get_state(conn, "nightly_result") == result
    assert conn.execute("SELECT status FROM jobs WHERE kind='update'").fetchone()["status"] == "done"


def test_same_version_downloads_nothing(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)  # ext 900
    ctx.tool.latest[123] = "900"
    assert nightly.run(ctx.env, conn).startswith("owner@example: обновлено 0, без изменений 1")
    assert [c[0] for c in ctx.tool.calls] == ["list_purchases", "latest_version_id"]


def test_without_session_calls_nothing_but_stamps(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    set_session(conn, 1, "expired")
    assert nightly.run(ctx.env, conn) == "пропущена: нет входа ни в один Apple ID"
    assert ctx.tool.calls == [] and stamp(cfg) == clock.iso()


def test_session_expiry_mails_once(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    ctx.tool.errors["list_purchases"] = SessionExpired("session_expired", "")
    assert nightly.run(ctx.env, conn).startswith("owner@example: остановлена")
    clock.advance(86400)
    nightly.run(ctx.env, conn)
    assert ctx.sent == ["appshelf: нужен вход в Apple ID"]


def test_expired_apple_id_does_not_stop_others(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock)
    petr = ready(conn, cfg, clock, app_id=7, acct=make_account(conn, clock, email="petr@example", role="member",
                                                                legacy=False))
    ctx.tool.errors["list_purchases"] = SessionExpired("session_expired", "")
    petr_tool = ctx.tools.setdefault(petr.id, FakeTool())
    petr_tool.latest[7] = "901"
    petr_tool.ipas[7] = make_ipa(tmp_path / "p.ipa", item_id=7, version="1.1", external_id="901")
    result = nightly.run(ctx.env, conn)
    assert result == ("owner@example: остановлена: истёк вход в Apple ID | "
                      "petr@example: обновлено 1, без изменений 0, ошибок 0; новые версии: СберБанк Онлайн")
    assert people.get_account(conn, 1).session == "expired"
    assert store.current_version(conn, petr.id, 7)["version"] == "1.1"
    assert ctx.sent == ["appshelf: нужен вход в Apple ID", nightly.DIGEST_SUBJECT]   # Петру — о новой версии


def test_low_space_skips_download(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    ctx.tool.latest[123] = "901"
    ctx.free["bytes"] = 2 * GB
    assert "ошибок 1" in nightly.run(ctx.env, conn)
    app = store.list_apps(conn, 1)[0]
    assert app.status == "ok" and "мало места" in app.last_error   # прежняя версия ставится, «Повторить» не нужен
    assert store.current_version(conn, 1, 123)["external_version_id"] == "900"
    assert ctx.sent == [f"appshelf: мало места на {socket.gethostname()}"]


def test_failed_publish_is_retried(ctx, cfg, conn, clock, tmp_path):
    ready(conn, cfg, clock, versions=())
    store.set_app_status(conn, 1, 123, "error", "сбой")
    ctx.tool.latest[123] = "900"
    ctx.tool.ipas[123] = make_ipa(tmp_path / "r.ipa", item_id=123, version="1.0", external_id="900")
    nightly.run(ctx.env, conn)
    assert store.list_apps(conn, 1)[0].status == "ok"


def test_error_app_with_same_version_back_to_ok(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    store.set_app_status(conn, 1, 123, "error", "временный сбой")
    ctx.tool.latest[123] = "900"
    nightly.run(ctx.env, conn)
    assert store.list_apps(conn, 1)[0].status == "ok"


def test_result_for_shows_only_own_apple_id():
    both = "owner@example: обновлено 1, без изменений 0, ошибок 0 | petr@example: остановлена: истёк вход в Apple ID"
    assert nightly.result_for(both, "petr@example") == "остановлена: истёк вход в Apple ID"
    assert nightly.result_for(both, "ivan@example") == nightly.NOT_IN_RUN       # не было входа — в прогон не попал
    assert nightly.result_for(nightly.NO_LOGIN, "petr@example") == nightly.NO_LOGIN
    android = "Android: обновлено 0, без изменений 1, ошибок 0"
    assert nightly.result_for(nightly.NO_LOGIN + nightly.SEP + android, "petr@example") == nightly.NO_LOGIN
    v1 = "обновлено 0, без изменений 3, ошибок 0"                                # итог до перехода — без адресов
    assert nightly.result_for(v1, "owner@example") == v1


def android_update(conn, clock, shelf_id: int, package: str, name: str) -> None:
    """На Android-полке — карточка с предыдущей 1.0 и текущей 2.0, скачанной сейчас."""
    conn.execute("INSERT INTO android_apps (shelf_id, package, name, source, status, published_at) "
                 "VALUES (?, ?, ?, 'rustore', 'ok', ?)", (shelf_id, package, name, clock.iso()))
    for code, role in ((1, "previous"), (2, "current")):
        conn.execute("INSERT INTO android_versions (shelf_id, package, version_name, version_code, min_sdk, target_sdk, "
                     "signer, source, size, sha256, dir, downloaded_at, role) "
                     "VALUES (?, ?, ?, ?, 21, 34, 's', 'rustore', 1, 'x', ?, ?, ?)",
                     (shelf_id, package, f"{code}.0", code, f"{package}-{code}", clock.iso(), role))


def digest_env(ctx):
    mails = []
    return replace(ctx.env, send=lambda subject, body, to: mails.append((subject, body, to))), mails


def test_weekly_digest_lists_updates_of_own_shelves(ctx, cfg, conn, clock):
    ready(conn, cfg, clock, versions=("1.0", "1.1"))                     # у владельца обновился СберБанк
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    seed_app(conn, cfg, clock, app_id=7, name="ВТБ", versions=("2.0", "2.1"), acct=petr)
    seed_app(conn, cfg, clock, app_id=8, name="Первое", versions=("1.0",), acct=petr)  # новое на полке — не обновление
    people.create_google(conn, petr.user_id, "sub-petr", "petr@mail.example", "Пётр", clock.iso())
    android_update(conn, clock, android_db.ensure_shelf(conn, petr.user_id, clock.iso()).id, "ru.petr", "Петино")
    android_update(conn, clock, android_db.ensure_public_shelf(conn, clock.iso()).id, "ru.pub", "Общее")
    env, mails = digest_env(ctx)
    assert nightly.digests(env, conn) == 2
    (_, mine, to_owner), (subject, his, to_petr) = mails
    assert (subject, to_owner, to_petr) == (nightly.DIGEST_SUBJECT, cfg.mail_to, "petr@mail.example")
    assert "СберБанк Онлайн 1.1" in mine and "Публичная Android-полка:\n  Общее 2.0" in mine and "ВТБ" not in mine
    assert "Apple ID petr@example:\n  ВТБ 2.1" in his and "Android:\n  Петино 2.0" in his
    assert not any(x in his for x in ("Первое", "Общее", "СберБанк")) and "https://apps.example/settings" in his


def test_digest_at_most_weekly_and_never_empty(ctx, cfg, conn, clock):
    acct = ready(conn, cfg, clock, versions=("1.0", "1.1"))
    env, mails = digest_env(ctx)
    assert nightly.digests(env, conn) == 1
    clock.advance(86400)
    store.add_version(conn, 1, cfg.shelf_root(acct), 123, store.NewVersion(
        version="1.2", build="1", external_version_id="902", min_ios="13.0", device_family="iphone", size=1,
        dir="123-1.2"), clock.iso())
    assert nightly.digests(env, conn) == 0                              # не чаще раза в 7 дней
    clock.advance(6 * 86400)
    assert nightly.digests(env, conn) == 1 and "СберБанк Онлайн 1.2" in mails[-1][1]
    clock.advance(7 * 86400)
    assert nightly.digests(env, conn) == 0 and len(mails) == 2          # нового нет — письма нет


def test_digest_reminds_member_of_expired_logins_weekly(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    set_session(conn, 1, "expired")                     # владельцу об истечении пишет mark_expired — не дублируем
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, session="expired")
    make_account(conn, clock, email="petr.work@example", user_id=petr.user_id, legacy=False, session="ok")
    for sub, mail in (("sub-1", "petr@mail.example"), ("sub-2", "petr.play@mail.example")):
        people.set_play(conn, people.create_google(conn, petr.user_id, sub, mail, "Пётр", clock.iso()), "ok", clock.iso())
    people.set_play(conn, 2, "expired", clock.iso())
    env, mails = digest_env(ctx)
    assert nightly.digests(env, conn) == 1                              # новых версий нет — письмо всё равно есть
    (subject, body, to), = mails
    assert (subject, to) == (nightly.DIGEST_LOGIN_SUBJECT, "petr@mail.example")
    assert body.startswith("Требует входа — новые версии с этих аккаунтов не скачиваются:\n"
                           "  Apple ID petr@example — вход истёк\n"
                           "  Google Play petr.play@mail.example — подключение истекло\n"
                           "Войти заново: https://apps.example/apple\n")
    assert "petr.work@example" not in body and "petr@mail.example" not in body
    assert "https://apps.example/settings" in body
    clock.advance(86400)
    assert nightly.digests(env, conn) == 0                              # та же частота: не чаще раза в 7 дней
    clock.advance(6 * 86400)
    seed_app(conn, cfg, clock, app_id=7, name="ВТБ", versions=("2.0", "2.1"), acct=people.get_account(conn, 3))
    assert nightly.digests(env, conn) == 1                              # и новые версии, и раздел — одним письмом
    subject, body, _ = mails[-1]
    assert subject == nightly.DIGEST_SUBJECT and "Требует входа" in body and "Apple ID petr.work@example:\n  ВТБ 2.1" in body
    notify.set_digest(conn, petr.user_id, False)
    clock.advance(7 * 86400)
    assert nightly.digests(env, conn) == 0                              # «Отключить» отключает и напоминание


def test_digest_keeps_update_after_previous_version_removed(ctx, cfg, conn, clock):
    acct = ready(conn, cfg, clock)                                      # публикация скачала 1.0
    clock.advance(3600)
    store.add_version(conn, 1, cfg.shelf_root(acct), 123, store.NewVersion(
        version="1.1", build="1", external_version_id="901", min_ios="13.0", device_family="iphone", size=1,
        dir="123-1.1"), clock.iso())
    conn.execute("DELETE FROM versions WHERE role='previous'")          # «Удалить 1.0»
    env, mails = digest_env(ctx)
    assert nightly.digests(env, conn) == 1 and "СберБанк Онлайн 1.1" in mails[-1][1]


def test_digest_keeps_android_update_after_previous_version_removed(ctx, cfg, conn, clock):
    own = owner(conn, clock)
    shelf = android_db.ensure_shelf(conn, own.user_id, clock.iso())
    root = cfg.archive / shelf.shelf
    android_db.put_app(conn, shelf.id, "ru.own", "Своё", "rustore", "ru.own", clock.iso())
    for code in (1, 2):                                                 # 2.0 пришла обновлением — ротация
        android_db.add_version(conn, root, shelf.id, "ru.own", android_db.NewApk(
            f"{code}.0", code, 21, 34, "", "s", "rustore", 1, str(code) * 64, f"ru.own-{code}"), clock.iso())
    conn.execute("DELETE FROM android_versions WHERE role='previous'")  # «Удалить 1.0»
    env, mails = digest_env(ctx)
    assert nightly.digests(env, conn) == 1 and "Своё 2.0" in mails[-1][1]


def test_digest_turned_off_and_failed_mail_retried(ctx, cfg, conn, clock):
    ready(conn, cfg, clock, versions=("1.0", "1.1"))
    notify.set_digest(conn, 1, False)
    notify.set_digest(conn, 99, False)                                  # участника уже нет
    env, mails = digest_env(ctx)
    assert nightly.digests(env, conn) == 0 and not notify.digest_enabled(conn, 1)
    assert notify.digest_enabled(conn, 99)                              # метка удалённого — прочь
    notify.set_digest(conn, 1, True)

    def mail_down(*a):
        raise OSError("sendmail")

    assert nightly.digests(replace(ctx.env, send=mail_down), conn) == 0
    assert nightly.digests(env, conn) == 1                              # не ушло — повторили


def remove_on(cfg, conn, acct, call: str):
    """Хук подменённого ipatool: Apple ID удаляют, пока идёт вызов call (замок ipatool уже отпущен)."""
    def hook(name, *args):
        if name == call and people.get_account(conn, acct.id) is not None:
            jobs.remove_account(cfg, conn, acct)
    return hook


def test_apple_id_removed_while_history_loads_leaves_nothing(ctx, cfg, conn, clock):
    owner(conn, clock)
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, session="ok")
    tool = ctx.tools.setdefault(petr.id, FakeTool())
    tool.purchases = [{"id": 7, "bundleId": "com.vk.vkclient", "name": "VK"}]   # Apple ответил
    tool.on_call = remove_on(cfg, conn, petr, "list_purchases")
    assert nightly.run(ctx.env, conn) == "petr@example: остановлена: Apple ID удалён"
    assert_shelf_gone(conn, cfg, petr)


def test_apple_id_removed_before_update_starts_downloads_nothing(ctx, cfg, conn, clock, tmp_path):
    owner(conn, clock)
    petr = ready(conn, cfg, clock, app_id=7, acct=make_account(conn, clock, email="petr@example", role="member",
                                                                legacy=False))
    tool = ctx.tools.setdefault(petr.id, FakeTool())
    tool.latest[7] = "901"
    tool.ipas[7] = make_ipa(tmp_path / "p.ipa", item_id=7, version="1.1", external_id="901")
    tool.on_call = remove_on(cfg, conn, petr, "latest_version_id")   # между list-versions и start_job
    assert nightly.run(ctx.env, conn) == "petr@example: остановлена: Apple ID удалён (обновлено 0)"
    assert all(c[0] != "download" for c in tool.calls)
    assert_shelf_gone(conn, cfg, petr)


def test_second_nightly_does_nothing_while_first_runs(ctx, cfg, conn, clock):
    ready(conn, cfg, clock)
    with jobs.nightly_lock(cfg) as got:
        assert got
        assert nightly.run(ctx.env, conn) == "уже идёт другая ночная проверка"
    assert ctx.tool.calls == [] and stamp(cfg) == ""


REAL_INTEGRITY = nightly._integrity  # conftest подменяет сводку у всех тестов, кроме этих


def test_integrity_summary_reports_owner_once_per_episode(ctx, cfg, conn, clock, monkeypatch):
    monkeypatch.setattr(nightly, "_integrity", REAL_INTEGRITY)
    acct = ready(conn, cfg, clock)                   # каталог версии без файлов
    set_session(conn, acct.id, "expired")            # Apple ни при чём: сводка идёт и без входа
    result = nightly.run(ctx.env, conn)
    assert result == (f"{nightly.NO_LOGIN} | Целостность: проблем 3: нет app.ipa — 1, нет icon.png — 1, "
                      "нет manifest.plist — 1")
    assert store.get_state(conn, "nightly_result") == result and stamp(cfg) == clock.iso()
    assert nightly.result_for(result, acct.email) == nightly.NO_LOGIN      # участникам сводка не видна
    assert ctx.sent == [nightly.INTEGRITY_SUBJECT]
    assert (cfg.shelf_root(acct) / "123-1.0").is_dir() and store.current_version(conn, acct.id, 123)  # не удаляет
    clock.advance(86400)
    nightly.run(ctx.env, conn)
    assert ctx.sent == [nightly.INTEGRITY_SUBJECT]                         # тот же эпизод — без письма
    conn.execute("DELETE FROM versions")
    (cfg.shelf_root(acct) / "123-1.0").rmdir()
    assert nightly.run(ctx.env, conn).endswith(" | Целостность: проблем нет")
    (cfg.archive / ("beef" + "0" * 28)).mkdir()                           # новый эпизод — новое письмо
    assert nightly.run(ctx.env, conn).endswith(" | Целостность: проблем 1: каталог архива не принадлежит ни "
                                               "одному Apple ID — 1")
    assert ctx.sent == [nightly.INTEGRITY_SUBJECT] * 2


def test_integrity_summary_failure_keeps_stamp(ctx, cfg, conn, clock, monkeypatch):
    monkeypatch.setattr(nightly, "_integrity", REAL_INTEGRITY)
    cfg.archive.rmdir()
    assert nightly.run(ctx.env, conn).endswith(f" | Целостность: не проверена — архив {cfg.archive} недоступен — "
                                               "смонтирован ли он?")
    assert stamp(cfg) == clock.iso() and ctx.sent == []
