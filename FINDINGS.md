# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-09 · Чужие скиллы crm в .claude/skills/ [P3]
**Context:** сверка манифеста не-git файлов при добавлении appshelf в `sync-two-machines`.
**What:** `.claude/skills/{bank-statement-import,excel-journal-fix,link-acceptance,rule-dryrun}/SKILL.md` побайтово совпадают с одноимёнными скиллами проекта `crm` и к appshelf отношения не имеют; лежат на обеих машинах синхронизации, `.claude/` в `.gitignore`. В сессиях appshelf discovery предлагает банковские выписки и Excel-журналы.
**Proposal:** если копия случайная — удалить четыре каталога на обеих машинах (в `crm` они остаются).
**Status:** open
