# Записанные ответы F-Droid

Живая проверка 2026-10-29 (план Android, этап 5, Task 0): сервер в России, ответы как есть.

| Файл | Запрос | Что внутри |
|---|---|---|
| `package.json` | `GET https://f-droid.org/api/v1/packages/org.videolan.vlc` | `suggestedVersionCode` 13070208, у версии 3.7.2 — четыре `versionCode` |
| `package_fdroid.json` | то же, `org.fdroid.fdroid` | `suggestedVersionCode` 1023052 (1.23.2), выше в списке — 2.0.x: рекомендуемая ≠ новейшая |

- Нет пакета — HTTP 404 `{"error":"NOT_FOUND"}`.
- `https://f-droid.org/repo/<пакет>_<versionCode>.apk` — 200, `application/vnd.android.package-archive`, `ETag`,
  `Last-Modified`, `Content-Length`.
- ABI четырёх сборок VLC 3.7.2 (из самих APK): 13070208 — `x86_64` (её и рекомендует API), 13070207 — `x86`,
  13070206 — `arm64-v8a`, 13070205 — `armeabi-v7a`; подпись у всех одна (VideoLAN). В RuStore VLC и F-Droid нет (404).
