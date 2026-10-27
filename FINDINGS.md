# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-26 · code-review role:review-dsflash: Решение о выходе человека принимается по общему числу Apple ... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=dsflash), file appshelf/appshelf/web/accounts.py:87-91, category=correctness
**What:** Решение о выходе человека принимается по общему числу Apple ID в браузере (request.state.accounts — это полки ВСЕХ вошедших), а не по остатку полок конкретного человека. В общем браузере, где вошли двое, удаление единственной полки одного человека при наличии полки другого даёт len>1 → cookie не чистится, и человек остаётся аутентифицированным без единой полки (виден в /apple с пустым списком), хотя по комментарию «был последний Apple ID — человека больше нет» его предполагалось разлогинить. Нужно снимать именно его запись (webauth.without_user по a.user_id), если у него больше нет полок.
**Evidence:** `if len(request.state.accounts) > 1:`
**Proposal:** Проверять остаток полок конкретного человека (например, any(x.user_id == a.user_id and x.id != a.id for x in request.state.accounts)); если их не осталось — убрать только его запись из cookie через webauth.without_user(request.cookies[webauth.COOKIE], a.user_id) и сохранить остальных, а не оставлять его вошедшим без полок.
**Status:** open

## 2026-10-26 · code-review role:review-dsflash: Размер полки в шаблонах считается по захардкоженному 1073741... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=dsflash), file appshelf/appshelf/web/templates/apple.html:12, category=inconsistency
**What:** Размер полки в шаблонах считается по захардкоженному 1073741824 вместо общей константы GB (config.GB = 1024**3), которая уже импортируется и используется в accounts.py (confirm: size / GB). То же дублирование в admin.html. Это второй источник истины для коэффициента GiB — при изменении GB расчёты в шаблонах молча разойдутся с серверной логикой.
**Evidence:** `{%- if r.a.session_since %} с {{ r.a.session_since[:16] | replace('T', ' ') }} UTC{% endif %} · {{ (r.size / 1073741824) | round(1) }} ГБ</div></div>`
**Proposal:** Передавать GB (или уже посчитанное значение size_gb) в контекст шаблона из common.page/маршрутов и делить на него, либо вычислять размер в GB на бэкенде; убрать литерал 1073741824 из apple.html и admin.html.
**Status:** open

## 2026-10-26 · code-review role:review-dsflash: Модуль обещает (шапка файла), что переход схемы, лишь добавл... [P2]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=dsflash), file appshelf/appshelf/store.py:290-300, category=inconsistency
**What:** Модуль обещает (шапка файла), что переход схемы, лишь добавляющий столбцы с умолчаниями, записывает в state ключ schema_readable_by (прежний SCHEMA_VERSION), чтобы откат кода оставался рабочим. Но _upgrade нигде не вызывает set_state(READABLE_BY_KEY, ...): оба аддитивных пути (version 2/3 и _from_v1) завершаются только записью PRAGMA user_version. _readable_by читает так и не записанный ключ и возвращает текущую версию базы. В результате после миграции v2/v3→v4 откат на прежний код, открывающий базу версии 4, получит version > SCHEMA_VERSION и _readable_by > SCHEMA_VERSION → MigrationError и служба не поднимется, хотя переход был чисто аддитивным.
**Evidence:** `return int(get_state(c, READABLE_BY_KEY) or version)`
**Proposal:** В аддитивных ветках _upgrade (и, если применимо, для _from_v1) явно фиксировать совместимость: c.execute("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING", (READABLE_BY_KEY, str(version))) до записи PRAGMA user_version — тогда, например, v4, поднятая из v3, оставит schema_readable_by=3 и откат на код SCHEMA_VERSION=3 пройдёт проверку в connect.
**Status:** open

## 2026-10-26 · code-review role:review-dsflash: При вычислении метки «новая версия» условие проверяет x.prev... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=dsflash), file appshelf/appshelf/web/app.py:168-169, category=correctness
**What:** При вычислении метки «новая версия» условие проверяет x.previous is not None, но затем разыменовывает x.current["downloaded_at"]. Инвариант «previous есть только при наличии current» сейчас держится (add_version всегда оставляет current), но условие защищает не тот объект, к которому обращается: при нарушении инварианта (миграция, ручная правка, будущая логика ротации) страница каталога упадёт с TypeError вместо мягкой деградации.
**Evidence:** `if x.previous is not None and datetime.fromisoformat(x.current["downloaded_at"]) >= since}`
**Proposal:** Проверять именно тот объект, к которому идёт обращение: x.current is not None and datetime.fromisoformat(x.current["downloaded_at"]) >= since.
**Status:** open

