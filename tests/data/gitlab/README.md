# Записанные ответы GitLab.com

Живая проверка 2026-11-09 (запись IDEAS «Источники Codeberg / Forgejo / GitLab Releases»). Ответы урезаны: у проекта —
`id`, путь, адрес и видимость; у выпуска — без `author`, `commit`, `evidences`, `_links` и архивов исходников.

| Файл | Запрос | Что внутри |
|---|---|---|
| `project_aurora.json` | `GET https://gitlab.com/api/v4/projects/AuroraOSS%2FAuroraStore` | `id` 6922885 |
| `releases_aurora.json` | `GET https://gitlab.com/api/v4/projects/6922885/releases?per_page=5` (первый) | `4.8.4`: `assets.links` пуст, три APK — загрузки в `description` (`[имя](/uploads/<32 hex>/<файл>)`), строки кончаются `\` |
| `project_exif.json` | `GET https://gitlab.com/api/v4/projects/juanitobananas%2Fscrambled-exif` | `id` 5252730 |
| `releases_exif.json` | `GET https://gitlab.com/api/v4/projects/5252730/releases?per_page=5` (первый) | `v1.8.3`: тот же APK и ссылкой выпуска, и загрузкой в описании |

- Выпуски — по `released_at`, новые первыми; `upcoming_release: true` — ещё не вышел. Пререлизов у GitLab нет.
- `releases/permalink/latest` — 302 на `/projects/<id>/releases/<tag>`.
- Загрузка из описания отдаётся по `https://gitlab.com/-/project/<id>/uploads/<hash>/<файл>` (206 на Range);
  прежний вид `https://gitlab.com/<путь>/uploads/<hash>/<файл>` отвечает 404 — поэтому сначала запрос проекта.
- Ссылки выпуска (`assets.links[].url`) бывают загрузкой того же вида, пакетом
  `https://gitlab.com/<путь>/-/package_files/<id>/download` (F-Droid client) или любым внешним адресом — внешние не
  принимаются.
