# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-17 · code-review role:review: Гонка move_shelf ↔ remove_account: remove_account держит тол... [P2]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/jobs.py:369-376 (move_shelf) против 246-262 (remove_account), category=bug
**What:** Гонка move_shelf ↔ remove_account: remove_account держит только ipatool-замок Apple ID и НЕ берёт download_lock, а move_shelf после транзакции перечитывает аккаунт вне транзакции и разыменовывает его без проверки на None. Если remove_account удаляет Apple ID между коммитом tx (set_shelf_next) и повторным чтением — people.get_account вернёт None и cfg.shelf_token(acct)/acct.shelf_next бросит AttributeError. В jobs.move_shelves это исключение не ловится (ловятся только ArchiveUnavailable/AccountBusy/OSError) — ночная проверка упадёт на середине, а в web.app.main() move_shelves вызывается до create_app без try — appshelf-web вообще не поднимется. Кроме того, remove_account может удалить строку аккаунта, пока move_shelf держит download_lock и переименовывает каталог/правит manifest — старый каталог полки с IPA останется на шаре без строки в базе (rmtree по устаревшему shelf_root(acct) из remove_account пройдёт мимо переименованного каталога).
**Evidence:** `acct = people.get_account(c, acct.id) old, new = cfg.shelf_token(acct), acct.shelf_next`
**Proposal:** В move_shelf после повторного чтения проверять `if acct is None: return` (аккаунт уже удалён — делать нечего), а remove_account дополнительно брать hold(cfg.download_lock, REMOVE_LOCK_WAIT) (или проверять account_running + download_lock), чтобы перенос и удаление каталога полки не шли одновременно; в move_shelves/main() добавить AttributeError в перечень ловимого либо чинить корень.
**Status:** open

## 2026-10-17 · code-review role:review: remove_user проверяет занятость всех Apple ID заранее, но ме... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/jobs.py:299-303 (remove_user), category=bug
**What:** remove_user проверяет занятость всех Apple ID заранее, но между проверкой и вызовами remove_account задание может стартовать: remove_account в середине цикла бросит AccountBusy, при этом часть Apple ID человека уже удалена (строки, каталоги, HOME). Человек остаётся в базе с урезанным набором полок — частичное удаление без отката.
**Evidence:** `for acct in accounts: remove_account(cfg, c, acct)`
**Proposal:** Ловить AccountBusy от remove_account в цикле и либо продолжать удалять остальные (собирая список незанятых), либо документировать/возвращать состояние «удалено N из M»; надёжнее — взять блокировки всех Apple ID до первого удаления.
**Status:** open

## 2026-10-17 · code-review role:review: CSRF-проверка сверяет Origin с хостом, взятым в первую очере... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/web/app.py:80-81 (csrf_guard), category=security
**What:** CSRF-проверка сверяет Origin с хостом, взятым в первую очередь из X-Forwarded-Host — а это заголовок, который клиент может прислать сам и который Apache-прокси по умолчанию передаёт на бэкенд без очистки. Атакующий шлёт POST с Origin: https://evil.com и X-Forwarded-Host: evil.com — проверка проходит. Практически риск снижен SameSite=Lax у cookie (кросс-сайтовый POST cookie не несёт), поэтому это обход резервного слоя, а не полный CSRF.
**Evidence:** `host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")`
**Proposal:** Не доверять клиентскому X-Forwarded-Host: либо сравнивать Origin с фиксированным cfg.public_base (netloc), либо принимать Host только от Apache (удалить/перезаписать X-Forwarded-Host на прокси: RequestHeader unset X-Forwarded-Host + RequestHeader set …).
**Status:** open

## 2026-10-17 · code-review role:review: В register для текста письма берётся первый Apple ID добавля... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/web/signin.py:152 (register), category=bug
**What:** В register для текста письма берётся первый Apple ID добавляющего: `people.accounts_of(c, adder.id)[0]` без проверки на пустой список. Сам adder проверяется на существование (комментарий прямо описывает гонку удаления/«Выйти везде»), но у ещё существующего человека к этому моменту могут быть удалены все Apple ID (remove_user удаляет аккаунты, потом строку пользователя) — IndexError → 500 на шаге кода 2FA после верного кода.
**Evidence:** `how = f"добавлен из панели участника «{adder.name}» ({people.accounts_of(c, adder.id)[0].email})"`
**Proposal:** Использовать `next((a.email for a in people.accounts_of(c, adder.id)), adder.name)` или не упоминать Apple ID в how вовсе.
**Status:** open

## 2026-10-17 · code-review role:review: removed.load() читает и парсит data/removed_apps.json с диск... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/removed.py:24-27 (load) + web/app.py:299 (history), category=optimization
**What:** removed.load() читает и парсит data/removed_apps.json с диска при каждом вызове; aliases() и ids() вызывают load() заново. На горячем пути — поиск в /history: каждый запрос с q= перечитывает и заново парсит весь справочник (~480 записей); jobs.stub() аналогично зовёт removed.ids() при каждой ошибке задания.
**Evidence:** `doc = json.loads(resources.files("appshelf").joinpath("data/removed_apps.json").read_text(encoding="utf-8"))`
**Proposal:** Кэшировать разобранный документ (functools.lru_cache на load() — данные в пакете неизменяемы между запусками процесса).
**Status:** open

