# Записанные ответы Google Play

Живая проверка 2026-10-29 (план Android, этап 4, Task 0): сервер в России, без прокси, профиль устройства —
Pixel 9a (Android 15, arm64-v8a). Ответы `/fdfe/*` — `ResponseWrapper` (protobuf) как есть, кроме вычищенного:
URL скачивания (запрос → `token=TEST-URL-TOKEN`), значение cookie скачивания, токены доставки и похожие
непрозрачные строки (→ `TTT…` той же длины), подписи лицензии и служебное поле 9 обёртки (→ нули), записи
библиотеки аккаунта в ответе `purchase` (поля 39 и 54 убраны). Хэши APK (SHA-1 и SHA-256 в base64url без `=`) и
SHA-256 сертификата — настоящие: они публичные.

| Файл | Запрос | Что внутри |
|---|---|---|
| `details_free_single.bin` | `GET details?doc=com.fsck.k9` | бесплатное, `appDetails.file` — один файл без `splitId` (17.4), versionCode 39046, offer: micros 0, RUB |
| `details_free_split.bin` | `GET details?doc=org.videolan.vlc` | бесплатное, файлы `config.arm64_v8a`, `config.xxhdpi` (splitId 17.4) |
| `details_paid.bin` | `GET details?doc=com.teslacoilsw.launcher.prime` | платное: micros 2399000000, `checkoutFlowRequired` 1; один файл |
| `details_removed.bin` | `GET details?doc=ru.sberbankmobile` | HTTP 404, только `commands.displayErrorMessage` = «Item not found.» |
| `details_unauthorized.bin` | `details` с неверным `Bearer` | HTTP 401, `commands.displayErrorMessage` = «Error retrieving information from server. DF-DFERH-01» |
| `purchase_free.bin` | `POST purchase` (ot, doc, vc) бесплатного | `buyResponse.encodedDeliveryToken` (55) |
| `delivery_single.bin` | `GET delivery?ot&doc&vc&dtok` K-9 | status 1, `downloadUrl` (3), cookie (5), без `splitDeliveryData` (15) и `additionalFile` (4); SHA-256 (19) = файлу |
| `delivery_split.bin` | то же, VLC | status 1, `splitDeliveryData` × 2 |
| `delivery_not_acquired.bin` | `delivery` бесплатного без `purchase` | status 3 |
| `delivery_paid_not_owned.bin` | `delivery` некупленного платного без `purchase` | status 2 |
| `exchange_ok.txt` | `POST /auth` с `oauth_token` | ключи настоящего ответа (первая строка — HTTP-статус), значения подставные |
| `exchange_error.txt` | `POST /auth` с неверным `oauth_token` | HTTP 403 `Error=BadAuthentication` |
| `auth_error.txt` | `POST /auth` за токеном Play с неверным AAS | HTTP 403 `Error=BadAuthentication` |

`purchaseHistory` и купленное приложение — после проверки G3 (зарубежный аккаунт через прокси своей страны).
