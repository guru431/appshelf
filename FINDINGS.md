# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-11-12 · Пакет исправлений 2026-11-12 не выложен: выкладка и проверки на сервере ждут доступа по SSH [P1]
**Context:** закрытие FINDINGS.md и IDEAS.md 2026-11-12; `.env` → `APPSHELF_SSH_KEY` указывает на файл ключа, которого на машине нет, а другой имеющийся ключ сервер отклоняет (`Permission denied (publickey)`). Владелец решил отложить серверную часть.
**What:** исправления (`0a57715..HEAD`) закоммичены, но не выложены и не отправлены в remote. На сервере не сделано: (1) не проверены `android_jobs.error` и `android_apps.last_error` на путь архива с токеном личной полки (находка P1 про `_copy_to_public`); (2) объём памяти сервера не сверен с `MemoryMax=4G` (appshelf-web) и `3G` (appshelf-nightly); (3) в триггере мониторинга остался порог nightly.stamp 26 ч, а теперь нужно 30 ч (README, «Мониторинг»); (4) CI-job `verifiers` (debian:trixie, apksigner) ещё ни разу не запускался.
**Proposal:** вернуть ключ по пути из `.env` или поправить путь, затем `bash deploy/deploy.sh`. После выкладки: по (1) — SQL только на чтение, как в `tests/test_live.py`; если путь с токеном найден — `appshelf rotate-token --email <Apple ID владельца> --android`; по (2) — при памяти меньше 6–8 ГБ уменьшить пределы в юнитах; по (3) — поднять порог в мониторинге. Пуш: origin и cld — из сессии, GitHub — владелец; первый прогон `verifiers` смотреть там.
**Status:** open
