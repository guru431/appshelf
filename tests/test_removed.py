import pytest

from appshelf import jobs, nightly, removed, store
from appshelf.ipatool import SessionExpired
from helpers import make_account, owner, set_session, to_weekday
from ipa_factory import make_ipa

CATALOG = [{"id": 1, "name": "СберБанк Онлайн (Бюджет Онлайн)"}, {"id": 2, "name": "Т-Банк (GlossFlow)"},
           {"id": 3, "name": "ВКонтакте"}]


def test_catalog_has_unique_ids_and_known_originals():
    apps = removed.load()
    assert len(apps) == len({a["id"] for a in apps}) > 150
    by_id = {a["id"]: a["name"] for a in apps}
    assert by_id[492224193] == "СберБанк Онлайн" and by_id[564177498] == "ВКонтакте"
    assert all(a["name"].strip() for a in apps)


def test_check_adds_only_owned_apps_to_history(ctx, conn, clock):
    acct = owner(conn, clock)
    store.upsert_purchases(conn, 1, [{"id": 3, "bundleId": "com.vk.vkclient", "name": "VK"}], clock.iso())
    ctx.tool.latest[1] = "900"
    ctx.tool.no_license.add(2)
    assert removed.check(ctx.env, conn, acct, CATALOG) == (1, 1, 0)
    rows = {r["app_id"]: (r["name"], r["bundle_id"]) for r in store.list_purchases(conn, 1)}
    assert rows == {1: ("СберБанк Онлайн (Бюджет Онлайн)", ""), 3: ("VK", "com.vk.vkclient")}
    assert ("latest_version_id", 3) not in ctx.tool.calls  # уже в истории — не проверяем


def test_check_stops_on_expired_session(ctx, conn, clock):
    acct = owner(conn, clock)
    ctx.tool.errors["latest_version_id"] = SessionExpired("session_expired", "")
    with pytest.raises(SessionExpired):
        removed.check(ctx.env, conn, acct, CATALOG)


def test_worker_runs_check_job(ctx, cfg, conn, clock, monkeypatch):
    monkeypatch.setattr(removed, "load", lambda: CATALOG[:1])
    owner(conn, clock)
    ctx.tool.latest[1] = "900"
    set_session(conn, 1, "ok")
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    jobs.Worker(ctx.env, lambda: store.connect(cfg.db)).tick()
    assert [r["app_id"] for r in store.list_purchases(conn, 1)] == [1]
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "done"


def test_missing_licenses_are_remembered_until_full_check(ctx, conn, clock):  # кнопка — секунды, а не минуты
    acct = owner(conn, clock)
    ctx.tool.latest[1] = "900"
    ctx.tool.no_license.update({2, 3})
    assert removed.check(ctx.env, conn, acct, CATALOG) == (1, 2, 0)
    ctx.tool.calls.clear()
    assert removed.check(ctx.env, conn, acct, CATALOG) == (0, 0, 0)
    assert ctx.tool.calls == []
    ctx.tool.no_license.discard(3)
    ctx.tool.latest[3] = "901"
    assert removed.check(ctx.env, conn, acct, CATALOG, full=True) == (1, 1, 0)


def test_nightly_full_recheck_only_on_own_weekday(ctx, cfg, conn, clock, monkeypatch):
    # ~480 запросов к Apple на Apple ID: всё заново — раз в неделю, в «свой» день (id % 7)
    monkeypatch.setattr(removed, "load", lambda: CATALOG[:1])
    acct = owner(conn, clock)
    set_session(conn, 1, "ok")
    ctx.tool.no_license.add(1)
    removed.check(ctx.env, conn, acct)                  # кнопка: нет лицензии — запомнили
    ctx.tool.no_license.discard(1)
    ctx.tool.latest[1] = "900"
    to_weekday(clock, (acct.id + 1) % 7)                # не его день — «нет лицензии» не перепроверяется
    nightly.run(ctx.env, conn)
    assert store.list_purchases(conn, 1) == []
    to_weekday(clock, acct.id % 7)                      # его день — весь справочник
    nightly.run(ctx.env, conn)
    assert [r["app_id"] for r in store.list_purchases(conn, 1)] == [1]


def test_check_yields_to_publish_clicked_meanwhile(ctx, cfg, conn, clock, tmp_path, monkeypatch):
    # проверка справочника — минуты; «Опубликовать» посреди неё не ждёт до конца
    monkeypatch.setattr(removed, "load", lambda: CATALOG)
    owner(conn, clock)
    set_session(conn, 1, "ok")
    ctx.tool.no_license.update({1, 2, 3})
    store.upsert_purchases(conn, 1, [{"id": 7, "bundleId": "ru.example.app", "name": "Seven"}], clock.iso())
    ctx.tool.ipas[7] = make_ipa(tmp_path / "7.ipa", item_id=7)
    real = ctx.tool.latest_version_id

    def latest(app_id):
        if app_id == 1:
            store.publish(conn, 1, 7, clock.iso())  # нажали «Опубликовать»
        return real(app_id)

    ctx.tool.latest_version_id = latest
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    w = jobs.Worker(ctx.env, lambda: store.connect(cfg.db))
    w.tick()
    job = "SELECT status FROM jobs WHERE kind='check_removed'"
    assert conn.execute(job).fetchone()["status"] == "queued"           # уступила после id 1
    w.tick()
    assert store.list_apps(conn, 1)[0].status == "ok"                    # публикация прошла раньше
    ctx.tool.calls.clear()
    w.tick()
    assert [c[1] for c in ctx.tool.calls if c[0] == "latest_version_id"] == [2, 3]  # с того же места
    assert conn.execute(job).fetchone()["status"] == "done"


def test_check_ignores_publish_of_apple_id_without_login(ctx, cfg, conn, clock, monkeypatch):  # Review Focus 1
    monkeypatch.setattr(removed, "load", lambda: CATALOG)
    owner(conn, clock)
    set_session(conn, 1, "ok")
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, session="expired")
    store.upsert_purchases(conn, petr.id, [{"id": 7, "bundleId": "b", "name": "Seven"}], clock.iso())
    store.publish(conn, petr.id, 7, clock.iso())        # ждёт входа Петра — обработчик её не возьмёт
    ctx.tool.no_license.update({1, 2, 3})
    store.enqueue_once(conn, 1, "check_removed", None, clock.iso())
    jobs.Worker(ctx.env, lambda: store.connect(cfg.db)).tick()
    assert conn.execute("SELECT status FROM jobs WHERE kind='check_removed'").fetchone()["status"] == "done"


def test_catalog_aliases_for_latin_search():
    by_id = {a["id"]: a for a in removed.load()}
    assert "vk" in by_id[564177498]["aliases"] and "max" in by_id[6739530834]["aliases"]
    assert "sber" in by_id[492224193]["aliases"]
    assert by_id[6447614666]["name"] == "VK Видео" and "vk" in by_id[6447614666]["aliases"]
    assert "sber" in by_id[6769745089]["aliases"]  # «Семейный Онлайн» — клон Сбера 23.06.2026
    assert "vpn" in by_id[905953485]["aliases"]    # NordVPN — нет в RU App Store


def test_check_fills_versions_of_owned_removed_apps(ctx, conn, clock):
    acct = owner(conn, clock)
    ctx.tool.latest[1] = "900"
    ctx.tool.versions[1] = "12.15.0"
    removed.check(ctx.env, conn, acct, CATALOG[:1])
    assert [(r["app_id"], r["version"]) for r in store.list_purchases(conn, 1)] == [(1, "12.15.0")]
    ctx.tool.versions[3] = "8.1"
    ctx.tool.latest[3] = "801"
    store.add_known(conn, 1, 3, "ВКонтакте")          # раньше добавлено без версии
    removed.check(ctx.env, conn, acct, [CATALOG[2]])
    assert {r["app_id"]: r["version"] for r in store.list_purchases(conn, 1)}[3] == ""   # кнопка не дозаполняет
    removed.check(ctx.env, conn, acct, [CATALOG[2]], full=True)                         # ночь — дозаполняет
    assert {r["app_id"]: r["version"] for r in store.list_purchases(conn, 1)}[3] == "8.1"
