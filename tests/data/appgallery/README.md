# Записанные ответы AppGallery

Живая проверка 2026-10-29 (план Android, этап 5, Task 0): сервер в России. `POST
https://store-drru.hispace.dbankcloud.ru/hwmarket/api/clientApi`, форма клиента UpdateSDK с `method=client.updateCheck`
и `pkgInfo` = `[{"package": <пакет>, "versionCode": 1, "oldVersion": "1.0", …}]`, заголовок `User-Agent:
UpdateSDK##4.0.1.300##Android##Pixel 2##com.huawei.appmarket##12.0.1.301` — без входа и без `sign`. Вычищено: `hcrId`
в ссылках (идентификатор запроса → нули), текст «что нового». Хэши и ссылки — настоящие (публичные).

| Файл | Запрос | Что внутри |
|---|---|---|
| `update_check.json` | `updateCheck` `ru.vtb24.mobilebanking.android` | `list[0]`: `id` C102312137, `package`, `name`, `version` 20.19.0.6, `versionCode` 2090141590, `size` 189014452 (= скачанному файлу), `sha256` (= файлу), `fullDownUrl` (302 → CDN `*.dbankcdn.com`, сам APK), `icon` (webp) |
| `update_check_empty.json` | `updateCheck` несуществующего пакета | `rtnCode` 0, `count` 0, пустой `list` |

- Подпись скачанного APK ВТБ совпала с `signature` RuStore. `sSha2` в ответе — не SHA-256 сертификата: подпись
  сверяется только после скачивания.
- Зона: с российского адреса `serviceCountry` (RU, DE) на выдачу не влияет; хост `store-dre.hispace.dbankcloud.com`
  отвечает так же.
- Признака платного в ответе нет; четыре платные игры (Geometry Dash, BTD6, Terraria, GTA SA) — `count` 0.
- ID из ссылки (`appgallery.huawei.com/app/C…`) → пакет: `GET https://appgallery.cloud.huawei.com/appdl/<ID>` (HEAD —
  405) отвечает 302 на `…/<пакет>.<число>.apk`; неизвестный ID — 302 на `https://appgallery.cloud.huawei.com/`.
- `client.front2` отдаёт `sign` (gzip), но `client.appDetailById` с ним — `rtnCode` 1 «Failed to get path mapping»;
  веб-API `uowap/index` — 403 «InterfaceCode Verification failed». Не используются.
