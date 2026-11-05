# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-11-05 · Тесты вне бюджета: appshelf [P3]
**Context:** auto-cron `ClaudeTestSweep`, `appshelf`: уровень `fast` 65.5 с при бюджете 60 с, 2 ночи подряд
**What:** самые долгие части: `1.37s tests/test_migrate.py::test_v6_upgrades_to_v7 (call)`; `1.36s tests/test_web_android_public.py::test_reopen_button_only_for_owner (call)`; `1.35s tests/test_migrate.py::test_v7_upgrades_to_current_schema (call)`; `1.29s tests/test_android_pin.py::test_hundred_wrong_from_many_close_for_all (call)`; `0.94s tests/test_web_android_public.py::test_copy_history_makes_old_versions_available_to_public_guests[True] (call)`
**Proposal:** тесты дольше 1 с — ускорить или маркировать `integration` по замеру (канон «Тесты — единая политика», правила 1–2); бюджет не поднимать. Запись снимется сама, когда набор вернётся в бюджет
**Status:** open

