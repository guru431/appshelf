"""Shelf mutations retain the page target when another tab changes the cookie."""
import html
import json
import re

import pytest

from appshelf import store
from appshelf.android import db, fetch
from helpers import make_account, seed_app, set_session, sign_in

PKG = "ru.example.shared"


def form_action(page, path):
    return html.unescape(next(a for a in re.findall(r'<form[^>]*action="([^"]+)"', page) if path in a))


def android_app(conn, cfg, clock, uid):
    shelf = db.ensure_shelf(conn, uid, clock.iso())
    db.put_app(conn, shelf.id, PKG, "Shared app", "link", "https://example.org/app.apk", clock.iso())
    root = fetch.shelf_root(cfg, shelf)
    for code, signer in ((1, "a" * 64), (2, "b" * 64)):
        db.add_version(conn, root, shelf.id, PKG, db.NewApk(str(code), code, 24, 35, "", signer,
                       "link", 10, str(code) * 64, f"{PKG}-{code}", True), clock.iso())
    return shelf


def test_old_apple_tab_unpublishes_its_own_shelf(web, conn, cfg, clock, acct):
    other = make_account(conn, clock, email="member@example.org", role="member", legacy=False)
    sign_in(web.client, cfg, conn, clock, other.user_id)
    for target in (acct, other):
        seed_app(conn, cfg, clock, acct=target)
    action = form_action(web.client.get("/?os=apple").text, "/apps/123/unpublish")
    web.client.get(f"/acct/{other.id}", follow_redirects=False)
    response = web.client.request("POST", action, follow_redirects=False)
    assert response.status_code == 303
    assert not store.has_app(conn, acct.id, 123)
    assert store.has_app(conn, other.id, 123)


@pytest.mark.parametrize("operation", ["remove", "source", "accept-signer", "reject-signer"])
def test_old_android_tab_keeps_target_for_all_mutations(web, conn, cfg, clock, acct, operation):
    other = make_account(conn, clock, email="member@example.org", role="member", legacy=False)
    sign_in(web.client, cfg, conn, clock, other.user_id)
    mine = android_app(conn, cfg, clock, acct.user_id)
    theirs = android_app(conn, cfg, clock, other.user_id)
    action = form_action(web.client.get("/android").text, f"/apps/{PKG}/{operation}")
    pending = db.list_apps(conn, mine.id)[0].pending
    web.client.get(f"/who/{other.user_id}", follow_redirects=False)
    response = web.client.request("POST", action, follow_redirects=False,
                                  data={"source": "fdroid", "version_id": pending["id"], "confirm": "1"})
    assert response.status_code == 303
    original = db.get_app(conn, mine.id, PKG)
    untouched = db.list_apps(conn, theirs.id)[0]
    assert untouched.source == "link" and untouched.current["version_code"] == 1 and untouched.pending
    if operation == "remove":
        assert original is None
    elif operation == "source":
        assert original["source"] == "fdroid"
    elif operation == "accept-signer":
        assert db.current_version(conn, mine.id, PKG)["version_code"] == 2
    else:
        assert db.list_apps(conn, mine.id)[0].pending is None


def test_old_android_add_form_uses_page_owner(web, conn, cfg, clock, acct):
    other = make_account(conn, clock, email="member@example.org", role="member", legacy=False)
    sign_in(web.client, cfg, conn, clock, other.user_id)
    action = form_action(web.client.get("/android/add").text, "/android/add/link")
    web.client.get(f"/who/{other.user_id}", follow_redirects=False)
    response = web.client.request("POST", action, data={"url": "https://example.org/new.apk"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.shelf_for_user(conn, acct.user_id) is not None
    assert db.shelf_for_user(conn, other.user_id) is None


def test_old_copy_to_public_keeps_explicit_owner_source(web, conn, cfg, clock, acct):
    other = make_account(conn, clock, email="member@example.org", role="member", legacy=False)
    sign_in(web.client, cfg, conn, clock, other.user_id)
    mine = android_app(conn, cfg, clock, acct.user_id)
    android_app(conn, cfg, clock, other.user_id)
    link = html.unescape(next(h for h in re.findall(r'<a[^>]*href="([^"]+)"', web.client.get('/android').text)
                              if '/android/public/add/from-shelf' in h))
    preview = web.client.get(link)
    action = form_action(preview.text, "/android/public/add/from-shelf")
    web.client.get(f"/who/{other.user_id}", follow_redirects=False)
    assert f"person={acct.user_id}" in next(h for h in re.findall(r'<a[^>]*href="([^"]+)"',
                                                                  web.client.get('/android/public/add').text)
                                          if '/android/public/add/from-shelf' in h)
    response = web.client.request("POST", action, data={"package": PKG, 'snapshot': preview.context['plan']['snapshot']},
                                   follow_redirects=False)
    assert response.status_code == 303
    public = db.public_shelf(conn)
    job = conn.execute("SELECT payload FROM android_jobs WHERE shelf_id=? AND kind='copy'", (public.id,)).fetchone()
    assert json.loads(job["payload"])["from_shelf"] == mine.id


def test_unbound_or_unauthorized_context_changes_nothing(web, conn, cfg, clock, acct):
    seed_app(conn, cfg, clock)
    assert web.client.request("POST", "/apps/123/unpublish").status_code == 409
    assert web.client.request("POST", "/apps/123/unpublish?account=999").status_code == 403
    assert store.has_app(conn, acct.id, 123)
    assert web.client.request("POST", "/android/add/link", data={"url": "https://example.org/new.apk"}).status_code == 409
    assert db.shelf_for_user(conn, acct.user_id) is None


def test_pending_accept_requires_confirmation_and_exact_version(web, conn, cfg, clock, acct):
    shelf = android_app(conn, cfg, clock, acct.user_id)
    pending = db.list_apps(conn, shelf.id)[0].pending
    endpoint = f"/android/apps/{PKG}/accept-signer?person={acct.user_id}"
    assert web.client.request("POST", endpoint, data={"version_id": pending["id"]}).status_code == 404
    assert web.client.request("POST", endpoint, data={"version_id": pending["id"] + 1, "confirm": "1"}).status_code == 404
    assert db.current_version(conn, shelf.id, PKG)["version_code"] == 1


def test_saved_shelf_filters_keep_busy_state(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, app_id=123, name="Bank", bundle_id="org.example.bank")
    seed_app(conn, cfg, clock, app_id=456, name="Reader", bundle_id="org.example.reader")
    store.enqueue_once(conn, 1, "publish", 456, clock.iso())
    set_session(conn, 1, "ok")
    response = web.client.get("/?os=apple&q=EXAMPLE.BANK")
    assert [a.app_id for a in response.context["apps"]] == [123]
    assert response.context["total"] == 2 and response.context["busy"]
    response = web.client.get("/?os=apple&filter=errors")
    assert response.context["apps"] == [] and response.context["total"] == 2


def test_active_jobs_and_signature_decisions_are_independent_filters(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, app_id=123, name="Ready")
    seed_app(conn, cfg, clock, app_id=456, name="Loading")
    store.set_app_status(conn, 1, 123, "ok")
    store.set_app_status(conn, 1, 456, "downloading")
    page = web.client.get('/?os=apple&filter=active')
    assert [a.app_id for a in page.context['apps']] == [456]
    assert 'pending' not in dict(page.context['filter_options'])
    shelf = android_app(conn, cfg, clock, 1)
    db.put_app(conn, shelf.id, 'ru.queued', 'Queued', 'rustore', 'ru.queued', clock.iso())
    db.set_status(conn, shelf.id, PKG, 'ok')
    page = web.client.get('/android?filter=pending')
    assert [a.package for a in page.context['apps']] == [PKG]
    page = web.client.get('/android?filter=active')
    assert [a.package for a in page.context['apps']] == ['ru.queued']


def test_platform_and_public_theme_work_without_apple_selection(web):
    web.client.get("/android")
    assert web.client.get("/apple").context["section"] == "android"
    web.client.get("/?os=apple")
    assert web.client.get("/apple").context["section"] == "apple"
    web.client.post("/logout")
    response = web.client.get("/theme?set=dark&back=/login", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert web.client.get("/login").context["theme"] == "dark"
