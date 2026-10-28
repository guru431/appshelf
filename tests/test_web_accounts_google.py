"""«Аккаунты» с Google-аккаунтами, удаление Google-аккаунта, «Связать», Google у участников (spec 2026-10-28 §5, §10)."""
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from appshelf import people
from appshelf.android import db
from helpers import HOST, make_account, sign_in
from test_web_google import signed_uids


def google_only(conn, clock, sub="g1", name="Мария"):
    uid = people.create_user(conn, name, people.MEMBER, None, clock.iso())
    gid = people.create_google(conn, uid, sub, f"{sub}@gmail.example", name, clock.iso())
    return uid, gid


@pytest.fixture
def gweb(make_web, cfg, conn, acct):
    """Владелец вошёл; вход Google настроен."""
    gcfg = replace(cfg, google_client_id="cid", google_client_secret="s3cret")
    wb = make_web(gcfg, conn)
    return wb


def test_accounts_page_lists_google_and_add_google(gweb, conn, cfg, clock):
    sign_in(gweb.client, cfg, conn, clock, 1)
    people.create_google(conn, 1, "g-own", "owner@gmail.example", "Иван", clock.iso())
    r = gweb.client.get("/apple")
    assert r.status_code == 200 and "owner@example" in r.text and "owner@gmail.example" in r.text
    assert "/google/start?add=1&amp;next=%2Fapple" in r.text and "Добавить Apple ID" in r.text
    assert "/google/1/delete" in r.text and "Связать" not in r.text            # в браузере один участник


def test_web_link_both_apple_400_and_cookie_after_link(gweb, conn, cfg, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False, name="Пётр")
    uid, gid = google_only(conn, clock)
    for u in (1, petr.user_id, uid):
        sign_in(gweb.client, cfg, conn, clock, u)
    page = gweb.client.get("/apple").text
    assert f"/apple/link?a=1&amp;b={uid}" in page and f"/apple/link?a={petr.user_id}&amp;b={uid}" in page
    assert f"b={petr.user_id}" not in page                                      # у обоих Apple ID — кнопки нет
    r = gweb.client.post("/apple/link", data={"a": 1, "b": petr.user_id})
    assert r.status_code == 400 and "У обоих есть Apple ID — их не связать" in r.text
    assert gweb.client.post("/apple/link", data={"a": uid, "b": uid}).status_code == 404
    confirm = gweb.client.get(f"/apple/link?a={uid}&b=1")
    assert "Связать «Мария» с «Иван Петров»?" in confirm.text and "Связку не отменить" in confirm.text
    r = gweb.client.post("/apple/link", data={"a": uid, "b": 1}, follow_redirects=False)   # порядок полей не важен
    assert (r.status_code, r.headers["location"]) == (303, "/apple")
    assert people.get_user(conn, uid) is None and people.get_google(conn, gid).user_id == 1
    assert signed_uids(gweb.client, cfg, conn, clock) == [1, petr.user_id] and r.cookies.get("appshelf_who") == "1"


def test_link_needs_both_signed_in(gweb, conn, cfg, clock):
    sign_in(gweb.client, cfg, conn, clock, 1)
    uid, _ = google_only(conn, clock)                                            # не вошёл в этом браузере
    assert gweb.client.get(f"/apple/link?a=1&b={uid}").status_code == 404
    assert gweb.client.post("/apple/link", data={"a": 1, "b": uid}).status_code == 404
    assert people.get_user(conn, uid) is not None


def test_delete_last_google_of_google_only_person_signs_out_only_him(gweb, conn, cfg, clock):
    uid, gid = google_only(conn, clock)
    shelf = db.ensure_shelf(conn, uid, clock.iso())
    sign_in(gweb.client, cfg, conn, clock, 1)
    sign_in(gweb.client, cfg, conn, clock, uid)
    r = gweb.client.get(f"/google/{gid}/delete")
    assert r.status_code == 200 and "g1@gmail.example" in r.text and "последний аккаунт" in r.text
    r = gweb.client.post(f"/google/{gid}/delete", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/apple")
    assert people.get_user(conn, uid) is None and db.get_shelf(conn, shelf.id) is None
    assert signed_uids(gweb.client, cfg, conn, clock) == [1]
    alone = TestClient(gweb.app, base_url=f"https://{HOST}")                    # в браузере был он один
    uid2, gid2 = google_only(conn, clock, "g2")
    sign_in(alone, cfg, conn, clock, uid2)
    r = alone.post(f"/google/{gid2}/delete", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login") and not alone.cookies.get("appshelf_auth")


def test_foreign_google_cannot_be_deleted(gweb, conn, cfg, clock):
    sign_in(gweb.client, cfg, conn, clock, 1)
    _, gid = google_only(conn, clock)
    assert gweb.client.get(f"/google/{gid}/delete").status_code == 404
    assert gweb.client.post(f"/google/{gid}/delete").status_code == 404 and people.get_google(conn, gid)


def test_admin_lists_google_and_deletes_google_only_person(gweb, conn, cfg, clock):
    sign_in(gweb.client, cfg, conn, clock, 1)
    uid, _ = google_only(conn, clock)
    page = gweb.client.get("/admin").text
    assert "g1@gmail.example" in page and f"/admin/users/{uid}/delete" in page
    assert "g1@gmail.example" in gweb.client.get(f"/admin/users/{uid}/delete").text
    r = gweb.client.post(f"/admin/users/{uid}/delete", follow_redirects=False)
    assert r.status_code == 303 and people.get_user(conn, uid) is None and people.google_of(conn, uid) == []
