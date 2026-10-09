# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-09 · build-ipatool.sh требует Docker на сервере, а его там больше нет [P2]
**Context:** выкладка патчей 06 и 07: `sudo bash deploy/build-ipatool.sh` на сервере — `docker: command not found`.
**What:** Docker с сервера appshelf удалён 2026-10-09 (контейнеры переехали на отдельный Docker-хост). Сборка по `CLAUDE.md`, README и `ipatool/README.md` больше не работает; бинарь патчей 06–07 собран разово тем же скриптом на Docker-хосте и поставлен на сервер руками (root 0755, `ipatool.src-sha256` по выложенным UPSTREAM и патчам).
**Proposal:** разделить `build-ipatool.sh`: сборка в `debian:trixie` — на Docker-хосте из `.env` (`APPSHELF_BUILD_SSH`), установка, проверка `ldd` и `src-sha256` — на сервере; запуск с машины разработчика, как `deploy.sh`. Поправить `CLAUDE.md`, README, `ipatool/README.md`.
**Status:** open

## 2026-10-09 · Чужие скиллы crm в .claude/skills/ [P3]
**Context:** сверка манифеста не-git файлов при добавлении appshelf в `sync-two-machines`.
**What:** `.claude/skills/{bank-statement-import,excel-journal-fix,link-acceptance,rule-dryrun}/SKILL.md` побайтово совпадают с одноимёнными скиллами проекта `crm` и к appshelf отношения не имеют; лежат на обеих машинах синхронизации, `.claude/` в `.gitignore`. В сессиях appshelf discovery предлагает банковские выписки и Excel-журналы.
**Proposal:** если копия случайная — удалить четыре каталога на обеих машинах (в `crm` они остаются).
**Status:** open
