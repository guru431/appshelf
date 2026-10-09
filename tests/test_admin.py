import re

from starlette.testclient import TestClient

from appshelf import people, store
from helpers import HOST, make_account, seed_app, set_session, sign_in

BASE = f"https://{HOST}"


def petr(conn, clock):
    return make_account(conn, clock, email="petr@example", role="member", legacy=False, name="Пётр")


def member_client(web, cfg, conn, clock):
    p = petr(conn, clock)
    client = TestClient(web.app, base_url=BASE)
    sign_in(client, cfg, conn, clock, p.user_id)
    return p, client


def test_admin_only_for_owner(web, conn, cfg, clock):
    _, member = member_client(web, cfg, conn, clock)
    assert member.get("/admin").status_code == 403
    assert member.post("/admin/invites", data={"label": "x"}).status_code == 403
    assert member.post("/admin/users/1/delete").status_code == 403 and people.get_user(conn, 1)
    assert "Участники" not in member.get("/").text and "Участники" in web.client.get("/").text


def test_invite_link_shown_once_and_stored_as_hash(web, conn):
    r = web.client.post("/admin/invites", data={"label": "коллеги"})
    [link] = re.findall(rf"{re.escape(BASE)}/join/[\w-]+", r.text)
    token = link.rsplit("/", 1)[1]
    assert people.active_invite(conn, token)["label"] == "коллеги"
    assert token not in "\n".join(conn.iterdump())
    page = web.client.get("/admin").text
    assert link not in page and "коллеги" in page and "пришло: 0" in page


def test_disabled_invite_stops_working(web, conn, clock):
    token = people.create_link(conn, people.INVITE, "все", None, clock.iso())
    web.client.post(f"/admin/invites/{people.list_invites(conn)[0]['id']}/disable")
    assert TestClient(web.app, base_url=BASE).get(f"/join/{token}").status_code == 400
    assert "отключена" in web.client.get("/admin").text


def test_login_link_for_member(web, conn, cfg, clock):
    p = petr(conn, clock)
    r = web.client.post(f"/admin/users/{p.user_id}/login-link")
    [link] = re.findall(rf"{re.escape(BASE)}/l/[\w-]+", r.text)
    anon = TestClient(web.app, base_url=BASE)
    assert "Войти как Пётр" in anon.get(link.removeprefix(BASE)).text
    assert anon.post(link.removeprefix(BASE), follow_redirects=False).status_code == 303
    assert "petr@example" in anon.get("/").text


def test_logout_everywhere_for_member(web, conn, cfg, clock):
    p, member = member_client(web, cfg, conn, clock)
    assert member.get("/", follow_redirects=False).status_code == 200
    web.client.post(f"/admin/users/{p.user_id}/logout-all")
    assert member.get("/", follow_redirects=False).status_code == 303


def test_delete_member_with_shelves(web, conn, cfg, clock):
    p = petr(conn, clock)
    seed_app(conn, cfg, clock, app_id=7, acct=p, versions=("1.0",), make_dirs=True)
    (cfg.accounts_dir / str(p.id)).mkdir(parents=True)
    assert "Удалить участника «Пётр» (petr@example)?" in web.client.get(f"/admin/users/{p.user_id}/delete").text
    assert web.client.post(f"/admin/users/{p.user_id}/delete", follow_redirects=False).status_code == 303
    assert people.get_user(conn, p.user_id) is None and people.get_account(conn, p.id) is None
    assert not cfg.shelf_root(p).exists() and not (cfg.accounts_dir / str(p.id)).exists()


def test_delete_member_can_disable_their_invite(web, conn, cfg, clock):
    # бессрочное приглашение пережило бы удаление: по той же ссылке удалённый зарегистрировался бы снова
    token = people.create_link(conn, people.INVITE, "Пете", None, clock.iso())
    invite = people.active_invite(conn, token)
    p = petr(conn, clock)
    conn.execute("UPDATE users SET invite_id=? WHERE id=?", (invite["id"], p.user_id))
    page = web.client.get(f"/admin/users/{p.user_id}/delete").text
    assert "Отключить приглашение «Пете»" in page and " checked>" in page   # пришёл им один — галочка сразу
    web.client.post(f"/admin/users/{p.user_id}/delete", data={"disable_invite": "1"})
    assert people.get_user(conn, p.user_id) is None and people.active_invite(conn, token) is None
    assert TestClient(web.app, base_url=BASE).get(f"/join/{token}").status_code == 400


def test_shared_invite_is_not_disabled_silently(web, conn, cfg, clock):
    token = people.create_link(conn, people.INVITE, "все", None, clock.iso())
    invite_id = people.active_invite(conn, token)["id"]
    p = petr(conn, clock)
    other = people.create_user(conn, "Анна", people.MEMBER, invite_id, clock.iso())
    conn.execute("UPDATE users SET invite_id=? WHERE id=?", (invite_id, p.user_id))
    page = web.client.get(f"/admin/users/{p.user_id}/delete").text
    assert "им пришли ещё 1" in page and " checked>" not in page           # ссылкой пользуются другие
    web.client.post(f"/admin/users/{p.user_id}/delete")
    assert people.active_invite(conn, token) is not None and people.get_user(conn, other)


def test_delete_member_refused_while_downloading(web, conn, cfg, clock):
    p = petr(conn, clock)
    seed_app(conn, cfg, clock, app_id=7, acct=p)
    set_session(conn, p.id, "ok")
    store.take_job(conn)                                   # публикация Петра качается
    r = web.client.post(f"/admin/users/{p.user_id}/delete")
    assert r.status_code == 409 and "Идёт скачивание" in r.text and people.get_user(conn, p.user_id)


def test_owner_cannot_delete_self(web, conn):
    assert "Себя удалить нельзя" in web.client.get("/admin/users/1/delete").text
    assert web.client.post("/admin/users/1/delete").status_code == 400 and people.get_user(conn, 1)


def test_people_list_shows_apple_ids_and_space(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0",))
    page = web.client.get("/admin").text
    assert "owner@example" in page and "владелец" in page and "0.3 ГБ" in page
