# Findings archive — appshelf
Отклонённые находки (`wontfix`, `deferred`) — чтобы ревью не заводило их заново. Новые записи сверху, отсюда ничего не удаляется.

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
