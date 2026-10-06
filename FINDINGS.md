# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-06 · Имя хоста `debian` зашито в письма [P3]
**Context:** подготовка к публикации на GitHub, `appshelf/notify.py`, `appshelf/jobs.py`
**What:** `MAIL_FROM = '"[debian] appshelf" <appshelf@debian>'` и тема «appshelf: мало места на debian» — у чужой
установки (и у нас при переезде) отправитель и тема будут неверными; тесты (`test_jobs`, `test_nightly`) сверяют тему дословно.
**Proposal:** имя хоста — `socket.gethostname()` или `MAIL_FROM` из `appshelf.env` с нынешним значением по умолчанию.
**Status:** open
