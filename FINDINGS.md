# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-28 · appshelf check не сообщает о незавершённой смене токена Android-полки [P3]
**Context:** финальная самопроверка Android этапа 2, `appshelf/integrity.py::check` (цикл по `android_db.all_shelves`).
**What:** у Apple ID незавершённый перенос (`accounts.shelf_next`) — проблема `shelf_next` в отчёте, у Android-полки
`shelf_next` только добавляется в известные каталоги. С этапа 2 смена токена Android-полки реальна (`rotate-token
--public/--android`, смена PIN): прерванная смена до следующего старта службы не видна в `appshelf check`.
**Proposal:** в цикле Android-полок `rep.add("shelf_next", None, shelf=shelf.id)` при непустом `shelf.shelf_next`;
тест — полка с `shelf_next` → проблема в отчёте.
**Status:** open

## 2026-10-28 · Строки pin_attempts адресов не удаляются [P3]
**Context:** финальная самопроверка Android этапа 2, `appshelf/android/pin.py::try_pin`.
**What:** строка счётчика адреса удаляется только при верном PIN, смене PIN; истёкшие окна остаются навсегда. Рост
ограничен общим пределом (не больше ~100 новых строк в сутки, пока ввод открыт), но таблица копится годами.
**Proposal:** в `try_pin` (в той же транзакции) `DELETE FROM pin_attempts WHERE key != 'all' AND blocked_until <
now AND first_at < now − IP_WINDOW`; тест — старая строка адреса исчезает после новой попытки.
**Status:** open
