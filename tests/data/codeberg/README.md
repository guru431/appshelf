# Записанные ответы Codeberg (Forgejo)

Живая проверка 2026-11-09 (запись IDEAS «Источники Codeberg / Forgejo / GitLab Releases»). Ответ урезан: без
`author`, `url`, `html_url`, архивов и счётчиков.

| Файл | Запрос | Что внутри |
|---|---|---|
| `release_latest.json` | `GET https://codeberg.org/api/v1/repos/gitnex/GitNex/releases/latest` | выпуск `15.1.0`, один файл `15.1.0.apk`, текст выпуска в `body` (Markdown) |

- `releases/latest` — последний выпуск без `draft` и `prerelease`; нет репозитория или выпуска — 404.
- Файл: `browser_download_url` вида `https://codeberg.org/<owner>/<repo>/releases/download/<tag>/<имя>` отдаётся
  сразу (200/206, `application/vnd.android.package-archive`, `ETag` — `uuid` файла), без редиректа.
- У файла есть `id` и `uuid`: заменённый файл выпуска получает новые — метка `cb:<release id>:<asset id>:<uuid>`.
