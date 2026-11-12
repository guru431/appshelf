# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-11-12 · Выкладка не обновляет Python-зависимости на сервере; CI проверяет другие версии [P3]
**Context:** адверсариальный аудит 2026-11-12, линза «эксплуатация», `deploy/install.sh:54` (`pip install`), `pyproject.toml:10-21`, `.github/workflows/tests.yml:13`.
**What:** `pip install "${NEW}[fourpda]"` без `--upgrade` (стратегия only-if-needed), в `pyproject.toml` только нижние границы, lock-файла нет: уже установленные fastapi/starlette, python-multipart, Pillow, resvg-py, uvicorn, jinja2, playwright остаются в версии первой установки. Исправления Pillow (разбирает иконки недоверенных APK) и python-multipart (формы публичных маршрутов) до сервера не доходят, а CI каждый раз ставит свежие версии и проверяет не то, что работает на сервере.
**Proposal:** решение владельца: `--upgrade --upgrade-strategy eager` в install.sh (сервер получает то же, что CI) или lock-файл с хэшами (`pip-compile` / `uv pip compile`) для install.sh и CI. В обоих случаях — правило обновления в README, как для `bin/ipatool` (после CVE и раз в квартал).
**Status:** open
