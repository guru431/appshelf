# Findings archive — appshelf
Отклонённые находки (`wontfix`, `deferred`) — чтобы ревью не заводило их заново. Новые записи сверху, отсюда ничего не удаляется.

## 2026-10-17 · code-review role:review: В register для текста письма берётся первый Apple ID добавля... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/web/signin.py:152 (register), category=bug
**What:** В register для текста письма берётся первый Apple ID добавляющего: `people.accounts_of(c, adder.id)[0]` без проверки на пустой список. Сам adder проверяется на существование (комментарий прямо описывает гонку удаления/«Выйти везде»), но у ещё существующего человека к этому моменту могут быть удалены все Apple ID (remove_user удаляет аккаунты, потом строку пользователя) — IndexError → 500 на шаге кода 2FA после верного кода.
**Evidence:** `how = f"добавлен из панели участника «{adder.name}» ({people.accounts_of(c, adder.id)[0].email})"`
**Proposal:** Использовать `next((a.email for a in people.accounts_of(c, adder.id)), adder.name)` или не упоминать Apple ID в how вовсе.
**Status:** wontfix
**Resolved:** 2026-10-18 — тот же довод, что у отклонённой 2026-10-11 находки о запасной ссылке (ниже): человека без Apple ID в базе не бывает. Удаление последнего Apple ID удаляет и человека в той же транзакции (`jobs._remove`, общая для `remove_account` и `remove_user`), а `register` читает `adder` и его Apple ID внутри своей `BEGIN IMMEDIATE` — промежуточного состояния «человек есть, Apple ID нет» она не видит. Проверка на пустой список была бы обработкой невозможного случая.

## 2026-10-10 · code-review role:review: POST по запасной ссылке расходует её (use_login_link ставит ... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/web/signin.py:424-431, category=correctness
**What:** POST по запасной ссылке расходует её (use_login_link ставит used_at) до проверки, что человеку есть куда впустить: если у пользователя не осталось ни одного Apple ID (все удалены), вход невозможен, но ссылка уже сожжена — повторный клик даст «Ссылка недействительна», и владельцу придётся выдавать новую.
**Evidence:** `uid = people.use_login_link(c, token, w.now()) user = people.get_user(c, uid) if uid is not None else None accounts = people.accounts_of(c, uid) if user is not None else [] if not accounts: w.limiter.fail() return login_page(w, request, error=LINK_GONE, status_code=400)`
**Proposal:** Сначала проверить наличие аккаунтов (или хотя бы существование пользователя), и только затем расходовать ссылку в одной транзакции; либо не считать это ошибкой ссылки и показать понятный текст «у участника нет Apple ID — добавьте его заново», не сжигая ссылку. Сверено с кодом 2026-10-11: appshelf/web/signin.py:293-298 (GET /l/{token} в строках 274-286 проверяет только пользователя).
**Status:** wontfix
**Resolved:** 2026-10-11 — опровергнуто проверкой: человека без Apple ID в базе не бывает. Строки `users` и `accounts` создаются одной транзакцией (`signin.register`, `store._from_v1`), а удаление последнего Apple ID той же транзакцией удаляет и человека (`jobs.remove_account`; тест `test_remove_account_deletes_rows_dirs_and_last_user`). «Все Apple ID удалены» значит «человека нет»: GET и POST одинаково отвечают «Ссылка недействительна», а ссылка на удалённого человека и так мертва (`users.id` — AUTOINCREMENT, номер другому не достанется) — её расход ничего не меняет.

## 2026-10-10 · code-review role:review: Worker.recover() вызывается до цикла и вне try/except: любое... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/jobs.py:436-440, category=bug
**What:** Worker.recover() вызывается до цикла и вне try/except: любое исключение при старте потока (например, таймаут блокировки SQLite при ожившей ночной проверке, сбой шары при обходе accounts_dir) убивает поток обработчика навсегда — задания больше никогда не берутся, служба при этом выглядит работающей. Основной цикл такие сбои переживает (`except Exception` внутри while), а старт — нет.
**Evidence:** `def run_forever(self) -> None: self.recover() while True:`
**Proposal:** Обернуть recover() в try/except с traceback.print_exc() (и, возможно, повтором попытки в цикле), чтобы сбой восстановления не убивал обработчик навсегда. Сверено с кодом 2026-10-11: appshelf/jobs.py:339-340.
**Status:** wontfix
**Resolved:** 2026-10-11 — повтор отклонённого 2026-10-09: «`recover()` — в try с повтором» входило в идею «/healthz не видит смерти или зависания обработчика заданий» ([IDEAS-archive.md](IDEAS-archive.md)), решение прежнее. Примеры из находки не подтверждаются: `accounts_dir` и `locks_dir` — локальные каталоги appshelf, не шара; база к старту потока уже открыта и перенесена (`web/app.py`: `store.connect` и `move_shelves` до `create_app` — сбой там роняет службу, а не поток), запись ждёт блокировку 30 с (busy_timeout), `rmtree` — с `ignore_errors`. С 2026-10-09 в `recover()` добавились только чтение списка Apple ID и более широкие маски уборки — новых источников сбоя нет.

## 2026-10-10 · code-review role:review: В test-зависимостях указан пакет «httpx2» — это не стандартн... [P2]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/pyproject.toml:18, category=bug
**What:** В test-зависимостях указан пакет «httpx2» — это не стандартный httpx, который нужен для starlette/fastapi TestClient. Если это опечатка, `pip install -e ".[test]"` в CI (tests.yml ставит именно `.[test]`) либо упадёт на несуществующем пакете, либо установит посторонний пакет, а тесты упадут с ImportError на `import httpx`. На пакете «httpx2» нет поддерживаемой совместимости с httpx API.
**Proposal:** —
**Status:** wontfix
**Resolved:** 2026-10-11 — не опечатка: `httpx2` выбран сознательно, как в subs (решение в `.superpowers/sdd/2026-10-05-appshelf/progress.md`, Task 10): `starlette.testclient` с обычным httpx даёт DeprecationWarning, а вывод тестов должен быть чистым. В `.venv` стоит httpx2 2.13.1, тесты на нём проходят.

## 2026-10-09 · Службы: ProtectSystem=strict и NoNewPrivileges вместо ProtectSystem=full [P3]
**Context:** часть находки «Код, .venv, deploy/ и ipatool/ принадлежат appshelf, а root их исполняет» (аудит 2026-10-09); `deploy/appshelf-web.service`, `deploy/appshelf-nightly.service`.
**What:** предлагалось `ProtectSystem=strict`, `ReadWritePaths=<данные, accounts, архив>`, `NoNewPrivileges=yes`.
**Proposal:** —
**Status:** wontfix
**Resolved:** 2026-10-09 — письма уходят через setuid-sendmail (exim4): с `NoNewPrivileges` он не поднимет права, а со `strict` не запишет очередь в `/var/spool` — уведомления о входе и месте молча пропали бы. Сделано `ProtectSystem=full` + `ReadWritePaths=/etc/appshelf/accounts` + `PrivateTmp`; главное — код, `.venv` и `bin/` теперь root (`deploy/install.sh`). Вернуться, если письма перейдут на SMTP к localhost.

## 2026-10-09 · Сборка ipatool: образ debian:trixie по digest и зеркало upstream [P3]
**Context:** часть находки «bin/ipatool вшивает OpenSSL на день сборки и curl 8.7.1» (аудит 2026-10-09); `deploy/build-ipatool.sh`.
**What:** закрепить образ по digest ради воспроизводимой сборки, завести зеркало upstream ipatool-cpp.
**Proposal:** —
**Status:** wontfix
**Resolved:** 2026-10-09 — `apt-get` в контейнере всё равно берёт текущие пакеты, так что digest воспроизводимости не даёт, а исправления OpenSSL задерживал бы; upstream закреплён коммитом в `ipatool/UPSTREAM`, патчи лежат в репозитории. Сделано: curl 8.22.0 с `URL_HASH` (патч 07) и правило пересборки в `ipatool/README.md`.

## 2026-10-09 · Блокировать кнопку «Продолжить» по onsubmit [P3]
**Context:** часть находки «Двойное «Продолжить» на шаге 1» (аудит 2026-10-09); `appshelf/web/templates/login.html`.
**What:** JS, выключающий кнопку после первого нажатия.
**Proposal:** —
**Status:** wontfix
**Resolved:** 2026-10-09 — двойное нажатие решено на сервере (`LoginFlow.start`: второй запрос с тем же паролем ждёт первый вход и получает его шаг кода, к Apple не идёт), а правило spec 2026-10-08 §10 — «JavaScript — только опрос /api/status».

## 2026-10-09 · Лимит неверных паролей без эскалации: аноним шлёт ~10 неудач в час в Apple на Apple ID [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «безопасность»; `appshelf/web/login.py:131-170`, `appshelf/web/signin.py:153-177`.
**What:** окно 5 неудач в час на Apple ID скользящее, без суточного предела; ipatool на неверный пароль шлёт в Apple два запроса; адрес зарегистрированного Apple ID различим по ответу формы.
**Proposal:** эскалация на сутки и суточный предел, письмо владельцу при срабатывании.
**Status:** wontfix
**Resolved:** 2026-10-09 — опровергнуто верификатором: лимит 5/час и различимость «не зарегистрирован»/«неверный пароль» — решения spec 2026-10-08 §4 (стр. 134-136) и §11 (стр. 279-280); порог блокировки у Apple неизвестен, а зная адрес, атакующий перебирает пароли у Apple напрямую, без этого сервера.

## 2026-10-09 · Access-лог Apache хранит PUB_TOKEN из URL полки владельца [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «эксплуатация»; `deploy/apache-appshelf.conf:6,14`, `appshelf/config.py:60-65`.
**What:** combined-лог пишет `GET /d/<PUB_TOKEN>/…` при каждой установке с перенесённой полки; читающий журналы вычисляет каталоги всех участников.
**Proposal:** не писать `/d/`, `/join/`, `/l/` в access-лог (`SetEnvIf` + `env=!…`).
**Status:** wontfix
**Resolved:** 2026-10-09 — опровергнуто верификатором: журналы Apache на Debian — root:adm 0640, www-data их не читает, остаются администраторы с root; каталог владельца = PUB_TOKEN — решение spec 2026-10-08 (стр. 162-164). Корень — PUB_TOKEN как ключ HMAC — заведён в FINDINGS отдельной находкой.

## 2026-10-09 · Ночная проверка пишет nightly.stamp и выходит с кодом 0, даже если всё упало [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «эксплуатация»; `appshelf/nightly.py:39-40,50-60,87-94,108-114`, `appshelf/cli.py:29-30`.
**What:** мониторинг ловит только возраст nightly.stamp, а `_finish` пишет его всегда; при сломанном bin/ipatool каждую ночь «ошибок N», а Zabbix и systemd молчат.
**Proposal:** ненулевой код при полном провале, `OnFailure=` с письмом или отдельный `status/nightly.errors`.
**Status:** wontfix
**Resolved:** 2026-10-09 — опровергнуто верификатором: осознанно — мониторинг поднимается, только если проверка не отработала (nightly.py:4-5, spec 2026-10-05 §9-§10); ошибки по приложениям видны в каталоге, об истёкшем входе приходит письмо, сломанный ipatool проявится и в веб-публикации.

## 2026-10-09 · Реальные часы в переходе схемы (store._from_v1) и таймере TTL веб-приложения [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «тесты и документы»; `appshelf/store.py:198`, `appshelf/web/app.py:53`, `appshelf/web/login.py:20-25`.
**What:** `_from_v1` берёт `created_at` из `datetime.now`, `create_app` не даёт подменить schedule у LoginFlow — формально против правила «время только подменой».
**Proposal:** передавать `now` в `store.connect`, параметр `schedule` в `create_app`.
**Status:** wontfix
**Resolved:** 2026-10-09 — опровергнуто верификатором: тесты от реального времени не зависят — `created_at` переноса одноразовый и ни на что не влияет; TTL проверен на уровне LoginFlow с подменой schedule (test_login.py:15,76), истечение через веб — подменённым clock (test_signin.py:130-139). Быстрый набор: 200 тестов за ~7 с, медленнее 1 с — ни одного.
