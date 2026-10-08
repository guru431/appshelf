from starlette.testclient import TestClient

from appshelf import people, store
from appshelf.ipatool import AuthCodeRequired
from helpers import HOST, FakeTool, make_account, seed_app, sign_in


def test_my_apple_ids_show_state_size_and_relogin(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0",))       # 291 МБ
    r = web.client.get("/apple")
    assert r.status_code == 200 and "owner@example" in r.text and "0.3 ГБ" in r.text and "нет входа" in r.text
    assert "/login?email=owner%40example&amp;next=%2Fapple" in r.text and "/apple/1/delete" in r.text


def test_add_second_apple_id_without_invite(web, conn, cfg):
    r = web.client.get("/apple/add")
    assert r.status_code == 200 and 'action="/apple/add"' in r.text and "добавить Apple ID" in r.text
    t = FakeTool()
    t.account = {"name": "Иван Петров", "storefront": "US"}
    web.queue.append(t)
    r = web.client.post("/apple/add", data={"email": "Ivan.US@example", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    added = people.account_by_email(conn, "ivan.us@example")
    assert added.user_id == 1 and added.storefront == "US" and len(people.list_users(conn)) == 1
    assert web.sent == [] and (cfg.accounts_dir / str(added.id)).is_dir()
    text = web.client.get("/").text
    assert "ivan.us@example" in text and "/acct/1" in text      # активна новая полка, прежняя — в переключателе


def test_add_with_2fa_goes_through_code_step(web, conn):
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.queue.append(t)
    r = web.client.post("/apple/add", data={"email": "second@example", "password": "pw"})
    assert r.status_code == 200 and 'action="/login/code"' in r.text
    r = web.client.post("/login/code", data={"code": "123456", "next": "/apple"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    assert people.account_by_email(conn, "second@example").user_id == 1


def test_delete_active_shelf_falls_back_to_remaining(web, conn, cfg, clock):  # Review Focus 4
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1)
    seed_app(conn, cfg, clock, app_id=5, name="Второе", acct=second, versions=("1.0",), make_dirs=True)
    (cfg.accounts_dir / str(second.id) / ".ipatool").mkdir(parents=True)
    web.client.get(f"/acct/{second.id}")
    assert "Удалить полку second@example" in web.client.get(f"/apple/{second.id}/delete").text
    r = web.client.post(f"/apple/{second.id}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    assert people.get_account(conn, second.id) is None and store.list_apps(conn, second.id) == []
    assert not cfg.shelf_root(second).exists() and not (cfg.accounts_dir / str(second.id)).exists()
    r = web.client.get("/", follow_redirects=False)       # cookie полки указывает на удалённый Apple ID
    assert r.status_code == 200 and "second@example" not in r.text


def test_owner_cannot_delete_last_apple_id(web, conn):
    r = web.client.get("/apple/1/delete")
    assert "Последний Apple ID владельца удалить нельзя" in r.text and 'action="/apple/1/delete"' not in r.text
    assert web.client.post("/apple/1/delete").status_code == 400 and people.get_account(conn, 1)


def test_member_deleting_last_apple_id_leaves(web, conn, cfg, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    member = TestClient(web.app, base_url=f"https://{HOST}")
    sign_in(member, cfg, conn, clock, petr.user_id)
    r = member.post(f"/apple/{petr.id}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert people.get_user(conn, petr.user_id) is None
    assert member.get("/", follow_redirects=False).status_code == 303


def test_delete_needs_archive(web, conn, cfg, clock):
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1)
    cfg.archive.rmdir()
    r = web.client.post(f"/apple/{second.id}/delete")
    assert r.status_code == 503 and "Архив недоступен" in r.text and people.get_account(conn, second.id)


def test_foreign_apple_id_cannot_be_deleted(web, conn, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    assert web.client.post(f"/apple/{petr.id}/delete").status_code == 404 and people.get_account(conn, petr.id)
