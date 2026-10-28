from contextlib import closing

from starlette.testclient import TestClient

from appshelf import jobs, people, store
from appshelf.ipatool import AuthCodeRequired
from helpers import HOST, FakeTool, make_account, seed_app, sign_in


def test_my_apple_ids_show_state_size_and_relogin(web, conn, cfg, clock):
    seed_app(conn, cfg, clock, versions=("1.0",))       # 291 МБ
    r = web.client.get("/apple")
    assert r.status_code == 200 and "owner@example" in r.text and "0.3 ГБ" in r.text and "нет входа" in r.text
    assert "/login?email=owner%40example&amp;next=%2Fapple" in r.text and "/apple/1/delete" in r.text


def test_new_apple_id_from_add_is_a_separate_person(web, conn, cfg):
    # 2026-10-13: новый Apple ID никому не достаётся — отдельный участник без приглашения, браузер входит и как он
    r = web.client.get("/apple/add")
    assert r.status_code == 200 and 'action="/apple/add"' in r.text and "добавить Apple ID" in r.text
    assert "станет отдельным участником" in r.text
    t = FakeTool()
    t.account = {"name": "Иван Второй", "storefront": "US"}
    web.queue.append(t)
    r = web.client.post("/apple/add", data={"email": "Ivan.US@example", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    added = people.account_by_email(conn, "ivan.us@example")
    person = people.get_user(conn, added.user_id)
    assert added.user_id != 1 and (person.name, person.role) == ("Иван Второй", "member") and added.storefront == "US"
    assert [a.id for a in people.accounts_of(conn, 1)] == [1] and (cfg.accounts_dir / str(added.id)).is_dir()
    [(subject, body)] = web.sent                                # владельцу — письмо: кто и из чьей панели
    assert subject == "appshelf: новый участник" and "ivan.us@example" in body
    assert "«Иван Петров» (owner@example)" in body
    text = web.client.get("/").text
    assert "ivan.us@example" in text and "/acct/1" in text      # активна новая полка, прежняя — в переключателе
    other = TestClient(web.app, base_url=f"https://{HOST}")     # другое устройство: вход прежним Apple ID
    other.post("/login", data={"email": "owner@example", "password": "pw"})
    page = other.get("/").text
    assert "owner@example" in page and "ivan.us@example" not in page   # новую полку он не открывает


def test_add_with_2fa_goes_through_code_step(web, conn):
    t = FakeTool()
    t.errors["login"] = AuthCodeRequired("auth_code_required", "")
    web.queue.append(t)
    r = web.client.post("/apple/add", data={"email": "second@example", "password": "pw"})
    assert r.status_code == 200 and 'action="/login/code"' in r.text
    r = web.client.post("/login/code", data={"code": "123456", "next": "/apple"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"
    assert people.account_by_email(conn, "second@example").user_id != 1


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


def test_two_deletes_at_once_keep_owner_last_apple_id(web, conn, cfg, clock, monkeypatch):
    # оба запроса видели у владельца два Apple ID: второе удаление перепроверяет под транзакцией
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1)
    real = jobs.remove_account

    def raced(cfg_, c, a):  # соседний запрос успел удалить другую полку (запрос идёт в потоке приложения)
        with closing(store.connect(cfg.db)) as other:
            real(cfg_, other, people.get_account(other, 1))
        return real(cfg_, c, a)

    monkeypatch.setattr(jobs, "remove_account", raced)
    r = web.client.post(f"/apple/{second.id}/delete")
    assert r.status_code == 400 and "Последний Apple ID владельца удалить нельзя" in r.text
    assert people.get_account(conn, second.id) and people.get_user(conn, 1)


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


def test_delete_refused_while_downloading(web, conn, cfg, clock):
    second = make_account(conn, clock, email="second@example", legacy=False, user_id=1, session="ok")
    seed_app(conn, cfg, clock, app_id=5, acct=second)
    store.take_job(conn)                                   # публикация второго Apple ID качается
    r = web.client.post(f"/apple/{second.id}/delete")
    assert r.status_code == 409 and "Идёт скачивание" in r.text and people.get_account(conn, second.id)


def test_foreign_apple_id_cannot_be_deleted(web, conn, clock):
    petr = make_account(conn, clock, email="petr@example", role="member", legacy=False)
    assert web.client.post(f"/apple/{petr.id}/delete").status_code == 404 and people.get_account(conn, petr.id)


def test_apple_ids_grouped_by_person(web, conn, cfg, clock):
    page = web.client.get("/apple").text                   # один человек — как раньше: без групп и без «Убрать»
    assert "Мои аккаунты" in page and 'action="/logout-all/1"' in page and "Удалить полку" in page
    assert "Убрать с этого устройства" not in page and '<span class="tag">' not in page
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    sign_in(web.client, cfg, conn, clock, shared.user_id)
    main = web.client.get("/apple").text.split("<main>")[1]            # в шапке адреса тоже есть
    assert "Аккаунты на этом устройстве" in main
    assert main.index("Иван Петров") < main.index("owner@example") < main.index("Общий") < main.index("shared@example")
    assert '<span class="tag">владелец</span>' in main and '<span class="tag">участник</span>' in main
    for uid in (1, shared.user_id):
        assert f'action="/logout/{uid}"' in main and f'action="/logout-all/{uid}"' in main


def test_owner_last_apple_id_is_judged_by_the_shelf_person(web, conn, cfg, clock):  # 2026-10-12, Review Focus 2
    # полок в браузере две, но у владельца Apple ID один: его удалить нельзя, полку Общего — можно
    shared = make_account(conn, clock, email="shared@example", role="member", legacy=False, name="Общий")
    sign_in(web.client, cfg, conn, clock, shared.user_id)
    assert "Последний Apple ID владельца удалить нельзя" in web.client.get("/apple/1/delete").text
    assert web.client.post("/apple/1/delete").status_code == 400 and people.get_account(conn, 1)
    r = web.client.post(f"/apple/{shared.id}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/apple"  # в браузере остался владелец
    assert people.get_user(conn, shared.user_id) is None
    page = web.client.get("/apple").text
    assert "owner@example" in page and "shared@example" not in page
