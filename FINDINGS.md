# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-10 · code-review role:review: POST по запасной ссылке расходует её (use_login_link ставит ... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/web/signin.py:424-431, category=correctness
**What:** POST по запасной ссылке расходует её (use_login_link ставит used_at) до проверки, что человеку есть куда впустить: если у пользователя не осталось ни одного Apple ID (все удалены), вход невозможен, но ссылка уже сожжена — повторный клик даст «Ссылка недействительна», и владельцу придётся выдавать новую.
**Evidence:** `uid = people.use_login_link(c, token, w.now()) user = people.get_user(c, uid) if uid is not None else None accounts = people.accounts_of(c, uid) if user is not None else [] if not accounts: w.limiter.fail() return login_page(w, request, error=LINK_GONE, status_code=400)`
**Proposal:** Сначала проверить наличие аккаунтов (или хотя бы существование пользователя), и только затем расходовать ссылку в одной транзакции; либо не считать это ошибкой ссылки и показать понятный текст «у участника нет Apple ID — добавьте его заново», не сжигая ссылку. Сверено с кодом 2026-10-11: appshelf/web/signin.py:293-298 (GET /l/{token} в строках 274-286 проверяет только пользователя).
**Status:** open

## 2026-10-10 · code-review role:review: Regex валидации PUB_TOKEN использует `$`, который в Python с... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/config.py:17, category=correctness
**What:** Regex валидации PUB_TOKEN использует `$`, который в Python совпадает перед финальным переводом строки: значение из 32 hex-символов с хвостовым '\n' (например, при ручном редактировании appshelf.env или кривом экспорте) пройдёт проверку, и символ новой строки попадёт в pub_token — в имена каталогов архива, в HMAC и в ссылки установки.
**Evidence:** `TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")`
**Proposal:** Использовать `re.fullmatch(r"[0-9a-f]{32}", token)` или якорь `\Z` вместо `$`. Сверено с кодом 2026-10-11: appshelf/config.py:12 (проверка — from_env, строка 83).
**Status:** open

## 2026-10-10 · code-review role:review: Worker.recover() вызывается до цикла и вне try/except: любое... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/jobs.py:436-440, category=bug
**What:** Worker.recover() вызывается до цикла и вне try/except: любое исключение при старте потока (например, таймаут блокировки SQLite при ожившей ночной проверке, сбой шары при обходе accounts_dir) убивает поток обработчика навсегда — задания больше никогда не берутся, служба при этом выглядит работающей. Основной цикл такие сбои переживает (`except Exception` внутри while), а старт — нет.
**Evidence:** `def run_forever(self) -> None: self.recover() while True:`
**Proposal:** Обернуть recover() в try/except с traceback.print_exc() (и, возможно, повтором попытки в цикле), чтобы сбой восстановления не убивал обработчик навсегда. Сверено с кодом 2026-10-11: appshelf/jobs.py:339-340.
**Status:** open

