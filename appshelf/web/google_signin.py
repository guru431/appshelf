"""Вход через Google и «Добавить Google» (spec 2026-10-28 §5). Известный Google-аккаунт (sub) — вход его участником;
новый — только по приглашению (cookie после /join/) или из панели вошедшего («Добавить Google»: тот должен быть ещё
вошедшим к ответу Google) — и всегда новым участником. Вход добавляет участника к вошедшим в этом браузере, как Apple ID.
Пароль Google вводится на странице Google: сервер получает только sub, адрес и имя (appshelf/google.py)."""
from __future__ import annotations

import hmac
import sqlite3
import sys

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .. import google, jobs, notify, people, store, webauth
from .common import INVITE_COOKIE, WHO_COOKIE, WebCtx, back, safe_next
from .signin import INVITE_GONE, JOINED_SUBJECT, label, login_page, signed_in

NOT_REGISTERED = "Этот Google-аккаунт на сервере не зарегистрирован — нужна ссылка-приглашение"
SIGNED_OUT = "Вход участника на этом устройстве завершён — войдите и добавьте Google-аккаунт заново"
TAKEN = "Этот Google-аккаунт уже зарегистрирован — войдите ещё раз"
EXPIRED = "Вход через Google не завершён или устарел — начните его заново"
CANCELLED = "Вход через Google отменён"
NO_ANSWER = "Google не ответил — попробуйте позже"
MAX_NEXT = 1000  # адрес возврата живёт в cookie потока: длинный раздул бы её больше 4 КБ, и браузер её не сохранил бы


class Refused(Exception):
    pass


def register_routes(app: FastAPI, w: WebCtx, http) -> None:
    cfg = w.cfg
    redirect_uri = f"{cfg.public_base}/google/callback"

    def not_found():
        return PlainTextResponse("not found", status_code=404)

    @app.get("/google/start")
    def start(request: Request, next: str = "/", add: int = 0):
        """Поток — в cookie, браузер — на страницу Google. add=1 — «Добавить Google» из панели: маршрут открыт без
        входа, вошедших ищем сами; никого нет или ссылка пришла с чужого сайта (Sec-Fetch-Site ставит браузер) —
        обычный вход: иначе чужая страница заводила бы участников от имени вошедшего."""
        if not cfg.google_enabled:
            return not_found()
        add_uid = None
        if add and request.headers.get("sec-fetch-site") != "cross-site":
            found = w.person(request)
            add_uid = found[4].id if found is not None else None
        flow = google.new_flow(safe_next(next) if len(next) <= MAX_NEXT else "/", add_uid, w.epoch())
        resp = RedirectResponse(google.auth_url(cfg.google_client_id, redirect_uri, flow), status_code=303)
        w.set_cookie(resp, google.COOKIE, google.flow_cookie(w.key, flow), google.FLOW_TTL)
        return resp

    def decide(c, request: Request, flow: google.Flow, cl: google.Claims, now: str) -> tuple[int, str]:
        """В транзакции: (участник, откуда новый — для письма; "" — известный аккаунт). Отказ — Refused."""
        g = people.google_by_sub(c, cl.sub)
        if g is not None and people.get_user(c, g.user_id) is None:
            # участника удалил код этапа 2 после отката (его строки google_accounts он не знает) — аккаунт новый
            # Каталоги и полку убирает remove_user перед транзакцией; новый остаток
            # не удаляем одной строкой: регистрация откажет, следующий вход повторит уборку.
            g = None
        if g is not None:
            people.mark_google_login(c, g.id, cl.email, cl.name, now)
            return g.user_id, ""
        adder = next((u for u, _ in w.signed(c, request) if u.id == flow.add_uid), None) \
            if flow.add_uid is not None else None
        invite = people.active_invite(c, request.cookies.get(INVITE_COOKIE, "")) if adder is None else None
        if adder is None and invite is None:
            # добавлявшего погасили («Выйти», «Выйти на всех устройствах») или удалили, пока он был у Google
            raise Refused(SIGNED_OUT if flow.add_uid is not None else
                          INVITE_GONE if request.cookies.get(INVITE_COOKIE) else NOT_REGISTERED)
        uid = people.create_user(c, cl.name or cl.email, people.MEMBER, invite["id"] if invite else None, now)
        people.create_google(c, uid, cl.sub, cl.email, cl.name, now)
        return uid, (f"добавлен из панели участника {label(c, adder)}" if adder is not None
                     else f"приглашение «{invite['label']}»")

    @app.get("/google/callback", response_class=HTMLResponse)
    def callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if not cfg.google_enabled:
            return not_found()
        flow = google.read_flow(w.key, request.cookies.get(google.COOKIE, ""), w.epoch())

        def refuse(text: str, status_code: int):
            resp = login_page(w, request, error=text, status_code=status_code, next_=flow.next if flow else "/")
            resp.delete_cookie(google.COOKIE, path="/")
            return resp

        # ответ Google в чужом браузере, повтор, подмена state — потока нет или он не тот: к Google не идём
        if flow is None or not hmac.compare_digest(state.encode(), flow.state.encode()):
            return refuse(EXPIRED, 400)
        if error or not code:
            return refuse(CANCELLED if error == "access_denied" else NO_ANSWER, 400)
        try:
            token = google.exchange(http, cfg.google_client_id, cfg.google_client_secret, redirect_uri, code,
                                    flow.verifier)
            cl = google.claims(token, cfg.google_client_id, flow.nonce, w.epoch())
        except google.GoogleError as e:
            print(f"appshelf: вход через Google не удался: {e}", file=sys.stderr)  # без кода, токенов и секрета
            return refuse(NO_ANSWER, 502)
        now = w.now()
        with w.conn() as c:
            orphan = people.google_by_sub(c, cl.sub)
            if orphan is not None and people.get_user(c, orphan.user_id) is None:
                adder = any(u.id == flow.add_uid for u, _ in w.signed(c, request))
                invite = people.active_invite(c, request.cookies.get(INVITE_COOKIE, ""))
                if adder or invite is not None:
                    try:
                        jobs.remove_user(cfg, c, orphan.user_id)
                    except (jobs.ArchiveUnavailable, jobs.AccountBusy):
                        return refuse("Прежняя полка занята или архив недоступен — повторите вход позже", 503)
            try:
                with store.tx(c):
                    uid, how = decide(c, request, flow, cl, now)
            except Refused as e:
                return refuse(str(e), 403)
            except sqlite3.IntegrityError:  # тот же новый Google-аккаунт успел войти из другого браузера
                return refuse(TAKEN, 403)
            user = people.get_user(c, uid)
            accounts = people.accounts_of(c, uid)
        if how:
            notify.send_quietly(w.env.send, JOINED_SUBJECT,
                                f"{user.name}, Google {cl.email}, {how}.\nУчастники: {cfg.public_base}/admin\n",
                                cfg.mail_to)
        resp = signed_in(w, request, back(flow.next), user, accounts[0].id if accounts else None)
        w.set_cookie(resp, WHO_COOKIE, str(uid), webauth.COOKIE_AGE)
        resp.delete_cookie(google.COOKIE, path="/")
        resp.delete_cookie(INVITE_COOKIE, path="/")
        return resp
