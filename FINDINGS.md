# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-11-11 · Тесты вне бюджета: appshelf [P3]
**Context:** auto-cron `ClaudeTestSweep`, `appshelf`: уровень `fast` 85.5 с при бюджете 60 с, 2 ночи подряд
**What:** самые долгие части: `1.89s tests/test_android_pin.py::test_hundred_wrong_from_many_close_for_all (call)`; `1.08s tests/test_web_android_public.py::test_copy_history_makes_old_versions_available_to_public_guests[True] (call)`; `1.04s tests/test_migrate.py::test_v5_upgrades_to_v6_with_pin_attempts (call)`; `1.03s tests/test_migrate.py::test_v6_upgrades_to_v7 (call)`; `1.02s tests/test_web_android_public.py::test_copy_history_makes_old_versions_available_to_public_guests[False] (call)`
**Proposal:** тесты дольше 1 с — ускорить или маркировать `integration` по замеру (канон «Тесты — единая политика», правила 1–2); бюджет не поднимать. Запись снимется сама, когда набор вернётся в бюджет
**Status:** open

