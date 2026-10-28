# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-28 · Битый APPSHELF_SOURCES_CA роняет appshelf-web целиком [P2]
**Context:** выкладка Android этапа 1 (2026-10-28): первый собранный файл сертификатов был склеен без перевода строки
(`-----END CERTIFICATE----------BEGIN…`), curl — 77; проверено: `load_verify_locations` на таком файле бросает
`ssl.SSLError` — до выкладки файл пересобран.
**What:** `web.app.create_app` и `cli nightly` создают `net.Http(cfg.sources_ca)` при старте; ошибка в необязательном
файле Android-загрузчика не дала бы подняться всей службе и ночной проверке, Apple-часть тоже встала бы.
**Proposal:** в `net.Http` ловить `ssl.SSLError`/`OSError` при загрузке доп. сертификатов — строка в журнал и работа
без них (скачивания с CDN под НУЦ упадут с ошибкой TLS у карточки); тест — битый PEM → `create_app` поднимается.
**Status:** open

## 2026-10-28 · Иконка из APK читается без предела размера [P3]
**Context:** финальная самопроверка Android этапа 1, `appshelf/apk.py::extract_icon`.
**What:** кандидаты `res/mipmap-*/ic_launcher.*` и `res/drawable-*/ic_launcher*` читаются `z.read(name)` целиком, без
проверки `file_size`: участник может загрузить APK, где «иконка» — zip-бомба на гигабайты, и appshelf-web съест память.
Манифест ограничен (16 МБ), иконка — нет.
**Proposal:** пропускать записи с `getinfo(name).file_size` больше 8 МБ; тест — APK с огромной «иконкой» → `None`.
**Status:** open
