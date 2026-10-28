# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-28 · Иконка из APK читается без предела размера [P3]
**Context:** финальная самопроверка Android этапа 1, `appshelf/apk.py::extract_icon`.
**What:** кандидаты `res/mipmap-*/ic_launcher.*` и `res/drawable-*/ic_launcher*` читаются `z.read(name)` целиком, без
проверки `file_size`: участник может загрузить APK, где «иконка» — zip-бомба на гигабайты, и appshelf-web съест память.
Манифест ограничен (16 МБ), иконка — нет.
**Proposal:** пропускать записи с `getinfo(name).file_size` больше 8 МБ; тест — APK с огромной «иконкой» → `None`.
**Status:** open
