# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-08 · Каталог показывает участникам адреса чужих Apple ID (nightly_result) [P2]
**Context:** финальное ревью ветки `multi-apple-id`; `appshelf/web/templates/catalog.html:34`, `nightly.py::_run`.
**What:** `nightly_result` теперь — `"<email>: итог | <email>: итог"` по всем Apple ID, а каталог выводит его целиком любому вошедшему. Участник видит Apple ID владельца и других участников (это же их логины для входа) — полки разных людей должны не смешиваться (spec 2026-10-08 §1, §11).
**Proposal:** в `app.py::catalog` показывать только часть строки активного Apple ID (сегмент `f"{acct.email}: "` из split по `" | "`), общую строку без сегментов («пропущена…», «уже идёт…») — как есть; тест: участник не видит адрес владельца в каталоге. Сделать до выкладки (задача 12 плана).
**Status:** open

## 2026-10-08 · deploy.sh проверяет APPSHELF_OWNER уже после установки кода [P2]
**Context:** финальное ревью ветки `multi-apple-id`; `deploy/deploy.sh` (блок «spec 2026-10-08 §6»).
**What:** проверка `APPSHELF_OWNER` стоит после `tar -x` и `pip install`: если переменной нет, скрипт выходит, но новый код уже установлен, а служба работает на старом в памяти. Ближайший перезапуск appshelf-web или ночная проверка упадут на `MigrationError`.
**Proposal:** перенести проверку `grep -qE "^APPSHELF_OWNER=.+"` в начало удалённого скрипта, до `sudo tar -xzf`. Сделать до выкладки (задача 12 плана).
**Status:** open

## 2026-10-08 · Остатки после удаления Apple ID посреди его операций и после перезапуска [P3]
**Context:** финальное ревью ветки `multi-apple-id`; `jobs.remove_account`, `web/signin.py::complete`, `web/login.py`.
**What:** (1) удаление Apple ID во время его скачивания: обработчик заново создаёт пустой каталог полки, ipatool может записать `cookies`/`account` в уже удалённый HOME; (2) Apple ID или человека удалили между шагами входа — `complete()` падает в 500 (`get_account(...)` / `get_user(...)` → None); (3) перезапуск appshelf-web посреди входа нового Apple ID оставляет `accounts/.new-*`; файлы `locks/.new-*.lock` и `locks/<id>.lock` удалённых Apple ID не убираются.
**Proposal:** отказывать в удалении, пока у Apple ID есть задание `running` («идёт скачивание — попробуйте позже»); в `complete()` при отсутствии строки — страница «войдите заново»; в `Worker.recover()` удалять `accounts/.new-*` и `locks/.new-*.lock`.
**Status:** open

## 2026-10-08 · Пропал тест на чужой next после входа (open redirect) [P3]
**Context:** финальное ревью ветки `multi-apple-id`; прежний `tests/test_web.py::test_login_wrong_password_and_foreign_next` удалён при переписывании тестов входа (задача 8 плана).
**What:** `common.safe_next` по-прежнему отсекает `//host`, `https://host` и `\`, но тестом это больше не закреплено.
**Proposal:** тест в `tests/test_signin.py`: вход с `next=https://evil.example` и `next=//evil.example` → редирект на `/`.
**Status:** open
