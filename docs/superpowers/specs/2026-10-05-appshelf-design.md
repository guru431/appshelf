# Своя полка приложений iPhone (`appshelf`) — дизайн

**Дата:** 2026-10-05 · **Статус:** реализован; вход и несколько Apple ID — [2026-10-08-multi-apple-id-design.md](2026-10-08-multi-apple-id-design.md)

## 1. Зачем

Банки, VK и другие российские приложения удалены из App Store. Скачать их может только тот
Apple ID, в истории покупок которого они уже есть, и только сторонними инструментами (iMazing,
ipatool), а Apple регулярно ломает эти инструменты. Сервисы вроде maxapp.ru отдают такие
приложения со своей страницы: системная установка из Safari по ссылке `itms-services`, без
компьютера и кабеля.

Нужна такая же страница для себя:

- приложения скачиваются с **основного Apple ID владельца** на сервер;
- владелец сам выбирает из истории покупок, что опубликовать;
- опубликованное ставится на iPhone из Safari одной кнопкой: после удаления, на новый телефон,
  на iPad;
- новые версии подтягиваются сами, предыдущая версия остаётся на случай отката.

Пользователь один — владелец. Пароль Apple ID на сервере не хранится.

### Критерии успеха

1. На `/apple` выполнен вход (Apple ID + пароль → код 2FA) → на сервере лежит только токен
   магазина, пароля в файлах нет (`grep` по `/etc/appshelf` и `/var/lib/appshelf`).
2. На `/history` видны приложения из истории покупок, включая удалённые из App Store.
   *Уточнение 2026-10-05:* Apple не отдаёт удалённые приложения в истории
   покупок DAAP (проверено: СберБанк, VK, банки отсутствуют в сыром ответе, хотя по id скачиваются),
   поэтому на `/history` их добавляют по ссылке App Store или ID — дальше как обычная публикация.
3. «Опубликовать» у удалённого приложения (например, СберБанк Онлайн) → через несколько минут
   оно в каталоге → «Установить» в Safari на iPhone → приложение ставится и запускается без
   запроса пароля Apple ID.
4. Ночная проверка находит новую версию опубликованного приложения → в каталоге новая версия,
   прошлая доступна как «предыдущая», более старая удалена с диска.
5. Токен истёк → ровно одно письмо и баннер на сайте; после входа через `/apple` всё работает
   без SSH.

### Вне рамок первой версии

Доступ для семьи и друзей (несколько Apple ID, персональные ссылки), архив всех версий,
отправка `list-purchases` в upstream, иконки на экране «История».

## 2. Решения, принятые при обсуждении

| Вопрос | Решение | Почему |
|---|---|---|
| Проект | Отдельный проект со своим доменом | Требование владельца |
| Чей Apple ID | Основной Apple ID владельца | Пока только для себя; на iPhone уже выполнен вход этим аккаунтом в «Контент и покупки», поэтому приложение запускается без донорского аккаунта и 2FA на телефоне |
| Источник IPA | `ipatool-cpp` (Sorvigolova, C++), собранный нами из исходников, с нашими патчами | Единственный проверенный путь без Mac (§3); iMazing для Windows не работает, Go-ipatool не входит |
| Как наполняется каталог | Из истории покупок, но **только по выбору владельца** | Требование владельца |
| Доступ к страницам | Пароль проверяет приложение: форма `/login` + cookie на год или заголовок Basic (до 2026-10-05 — ещё и Basic Auth Apache: веб-приложение с экрана «Домой» не показывает его окно); manifest и IPA — без пароля, по неугадываемому пути | iOS не проходит авторизацию при OTA-установке |
| Обновления | Ночная проверка; хранятся текущая и предыдущая версии | Выбор владельца |
| Пароль Apple ID | **Не хранится.** Истёк токен → письмо и форма на сайте (пароль + код 2FA) | Шифрование `ipatool-cpp` привязано к `machine-id` — root на сервере расшифровал бы пароль основного Apple ID |
| Раскладка на сервере | Код в `/var/_sh/appshelf`, данные в `/var/lib/appshelf`, секреты в `/etc/appshelf` | Как у остальных проектов владельца |

Отвергнуто: Mac + iMazing (ручной GUI, нет подходящего Mac), Go-ipatool 2.6.0 (Apple отклоняет вход,
см. §3), docker compose для веб-части (unix-сокет через volume, второй способ развёртывания), статический
сайт без веб-приложения (нужны форма входа и выбор из истории).

## 3. Проверенные факты (2026-10-05)

- **OTA-установка App Store-IPA работает.** IPA СберБанк Онлайн 12.15.0 (`ru.sberbank.onlineiphone`,
  291 МБ, iOS 13+, только iPhone), скачанный с основного Apple ID, выложен на HTTPS-сайт во временный
  каталог с токеном: `manifest.plist` (`text/xml`), IPA (`application/octet-stream`) и иконка. На iPhone
  (iOS 27, Safari): системное окно → установка → приложение запустилось без запроса пароля.
- **В IPA от `ipatool-cpp`** есть `SC_Info/*.sinf`, `iTunesMetadata.plist` (данные покупателя,
  `itemId`, `softwareVersionExternalIdentifier`) и иконки `AppIcon*.png` (формат CgBI).
- **Go-ipatool 2.5.0/2.6.0 не входит**: с домашнего адреса — 301 без `Location`, через зарубежный
  HTTP-прокси — 403, через мобильный SOCKS-прокси — 301. Это отказ на edge Apple до проверки пароля,
  общий с 27.09 (majd/ipatool#598 открыт на 03.10).
- **`ipatool-cpp` с коммита `7fae0e2` входит с домашнего адреса с первой попытки** и скачивает
  удалённое приложение за 7 с. Собран в контейнере `debian:trixie` (cmake + `libunicorn-dev`,
  сборка 22 с), запускался с `--network host` и `machine-id` хоста.
- **Аудит `7fae0e2`:** в коде только адреса Apple (`buy.itunes`, `p*-buy`, `auth.itunes`,
  `downloaddispatch`, `uclient-api`); `system()`/`popen()` и сырых IP нет; зависимости сборки — с
  официальных источников (OpenSSL, zlib, curl.se, nlohmann/json); `sap_assets/` — бинарники Apple
  (`CoreFP`, `CommerceKit`, `CommerceCore`, `storeagent`), исполняются в эмуляторе Unicorn.
- **Поведение `ipatool-cpp`:** учётка в `$HOME/.ipatool/account`, AES-256-GCM, ключ
  `PBKDF2(SHA256(machine-id + product_uuid) + "nice_key_is_nice" + passphrase)`; `product_uuid`
  (читает только root) при недоступности просто не участвует. **Пароль сохраняется**, при истечении токена — тихий повторный вход
  (`silent_relogin`), код 2FA ждёт со stdin. Команды `list-purchases` нет.
- **Go-ipatool `list-purchases`** (`pkg/appstore/appstore_owned_apps.go`, v2.5.0): DAAP
  `login` → `update` → `databases/<rev>/items`, запросы `update` и `items` SAP-подписаны; заголовки
  `X-Token`, `X-Dsid`, `X-Guid`, `X-Apple-Store-Front`, `Client-DAAP-Version: 3.12`; ответ — DMAP,
  приложения — `mlit` с `aeSI` (id), `aeBI` (bundle), `aeLN` (имя), `aePd` (версия), `asdp` (дата).

## 4. Архитектура

```
iPhone (Safari) ──https──► Apache, vhost apps.example.com
                             ├─ /    → ProxyPass unix:/run/appshelf/web.sock (вход — /login)
                             └─ /d/  → Alias <архив>/ (без пароля, -Indexes)
appshelf-web (FastAPI, uvicorn) ─┐
appshelf-nightly (systemd timer) ┴─► /var/_sh/appshelf/bin/ipatool (HOME=/etc/appshelf) ──► Apple
```

### Размещение

| Что | Где |
|---|---|
| Код, `.venv`, `deploy/`, `bin/ipatool` | `/var/_sh/appshelf/`; с 2026-10-09 владелец root, `appshelf` только читает и исполняет |
| Данные | `/var/lib/appshelf/` (`appshelf.db`, `tmp/`, `status/`) |
| Архив IPA | `APPSHELF_PUB` в `appshelf.env` (по умолчанию `/var/lib/appshelf/pub`); может быть сетевой шарой (CIFS из fstab). SQLite и `tmp/` — всегда локально: на CIFS база портится, а перенос из `tmp/` в архив — копированием (EXDEV) |
| Секреты | `/etc/appshelf/appshelf.env` (`root:appshelf 0640`): адрес писем, `PUB_TOKEN`, `APPSHELF_PUBLIC_BASE`, `IPATOOL_PROXY` (необязательно); `/etc/appshelf/.ipatool/` (`appshelf 0700`) — токен магазина |
| Vhost | образец — `deploy/apache-appshelf.conf` |
| Службы | `appshelf-web.service` (uvicorn, **один процесс**, только unix-сокет `/run/appshelf/web.sock`; один процесс обязателен — пароль между шагами входа и обработчик заданий живут в его памяти), `appshelf-nightly.service` + `appshelf-nightly.timer` (04:30) |

Docker нужен только для сборки бинарника: `appshelf` не входит в группу `docker` (это равносильно
root).

### Компоненты (Python-пакет `appshelf`)

| Модуль | Назначение |
|---|---|
| `ipatool.py` | Запуск `bin/ipatool` (HOME, прокси, таймаут, файловая блокировка), разбор JSON и кодов выхода → исключения `SessionExpired`, `AuthCodeRequired`, `LicenseNotFound`, `IpatoolError` |
| `ipa.py` | Разбор IPA: `Info.plist`, `iTunesMetadata.plist`, иконка; генерация `manifest.plist` |
| `store.py` | SQLite: схема, запросы, смена версий |
| `jobs.py` | Очередь заданий и обработчик (один поток в `appshelf-web`) |
| `nightly.py` | Ночная проверка (`python -m appshelf nightly`) |
| `notify.py` | Письма с дедупликацией по эпизоду |
| `web/` | FastAPI, шаблоны Jinja2, статика |

## 5. Форк `ipatool-cpp`

Upstream `https://github.com/Sorvigolova/ipatool`, закреплённый коммит `7fae0e2`. В репозитории
`appshelf` лежат `ipatool/UPSTREAM` (URL + коммит) и `ipatool/patches/*.patch`.

| Патч | Что меняет |
|---|---|
| `01-no-stored-password` | `save_account` не пишет поле `password`; `silent_relogin` удалён: на `PasswordTokenExpired` программа завершается с кодом `3` |
| `02-password-stdin` | `auth login --password-stdin` — пароль из stdin (не из `-p`, иначе виден в `/proc/<pid>/cmdline`) |
| `03-list-purchases` | Команда `list-purchases [--format json]` → `[{"id","bundleId","name","version","purchaseDate"}]`; перенос `appstore_owned_apps.go` (§3) на `SapSigner` и HTTP-клиент форка |
| `04-exit-codes` | `0` успех, `3` `session_expired`, `4` `auth_code_required`, `5` `license_not_found`, `1` прочее; при `--format json` ошибка печатается как `{"error": "<код>", "message": "…"}` |

**Сборка** — `deploy/build-ipatool.sh`: одноразовый контейнер `debian:trixie` → `git clone` upstream
→ `git checkout <коммит>` → `git apply patches/*.patch` → `cmake -DCMAKE_BUILD_TYPE=Release
-DSTATIC_BUILD=ON` → проверки `ipatool --help` и `ipatool kbsync --dsid 1` (без обращения к Apple)
→ `ldd` (только системные библиотеки) → копия в `/var/_sh/appshelf/bin/ipatool`.

**Обновление upstream** (`ipatool/README.md`): дифф новых коммитов, в первую очередь новые адреса и вызовы
процессов → перенос патчей → смена коммита в `UPSTREAM` → пересборка.

## 6. Данные

SQLite `/var/lib/appshelf/appshelf.db` (локальный диск):

| Таблица | Поля |
|---|---|
| `purchases` | `app_id` PK, `bundle_id`, `name`, `purchase_date`, `refreshed_at` |
| `apps` | `app_id` PK, `name`, `bundle_id`, `published_at`, `status` (`queued`/`downloading`/`ok`/`error`), `last_error`, `checked_at` |
| `versions` | `id` PK, `app_id`, `version`, `build`, `external_version_id`, `min_ios`, `device_family`, `size`, `dir`, `downloaded_at`, `role` (`current`/`previous`) |
| `jobs` | `id` PK, `kind` (`publish`/`update`/`refresh_history`), `app_id`, `status`, `created_at`, `finished_at`, `error` |
| `state` | ключ-значение: `session` (`ok`/`expired`/`none`), `session_since`, `account_name`, `storefront`, `expired_mail_sent`, `nightly_last`, `nightly_result` |

Файлы версии: `<архив>/<PUB_TOKEN>/<app_id>-<version>/{app.ipa, manifest.plist, icon.png}`.
`PUB_TOKEN` — 32 hex, только в `appshelf.env` и в имени каталога; в конфигурации Apache его нет.
Сменить токен = переименовать каталог и пересобрать все manifest. *Устарело 2026-10-09:* у каждой полки свой
случайный токен, смена — `appshelf rotate-token` (README, «Новый токен полки»).

`manifest.plist`: `software-package` = `<APPSHELF_PUBLIC_BASE>/d/<токен>/<app_id>-<version>/app.ipa`,
`display-image` = `…/icon.png`, `metadata`: `bundle-identifier`, `bundle-version`
(= `CFBundleShortVersionString`), `kind=software`, `title` (= `CFBundleDisplayName`). Иконка —
`iTunesArtwork` из IPA, приведённая к PNG; если её нет — `AppIcon*` с конвертацией из CgBI; если
и это не вышло — нейтральная заглушка.

## 7. Потоки

1. **История.** Кнопка «Обновить историю» и ночная проверка: `list-purchases --format json` →
   upsert в `purchases`. Ничего не публикует. Удалённые из магазина приложения в этом списке Apple
   не отдаёт — их добавляют по ссылке или ID App Store (`/history/add`): строка-заглушка в
   `purchases`, публикация, имя и bundle id — из IPA при первом скачивании.
2. **Публикация.** «Опубликовать» → строка в `apps` (`queued`) и задание `publish`. Обработчик
   (поток в `appshelf-web`) проверяет место — свободно `≥ 4 ГБ` и на разделе `/var/lib/appshelf`, и в
   архиве (3 ГБ неприкосновенного запаса + 1 ГБ на файл: размер IPA до скачивания неизвестен); каталога
   архива нет (шара не смонтирована) — задание `error`, под пустую точку монтирования не пишет.
   Выполняет `download -i <id> -o tmp/` → `ipa.py` → перенос в `<архив>/.<dir>.partial` → `manifest.plist`
   → переименование в каталог версии → `role=current`, `status=ok`. Временный файл удаляется в любом исходе.
3. **Снять с публикации.** Удаляются каталоги версий и строка `apps`; `purchases` не трогается.
4. **Ночная проверка** (04:30): обновить историю; для каждого `apps.status in (ok, error)` —
   `list-versions -i <id>`, последний `external_version_id` ≠ текущего → ночная проверка сама, в
   своём процессе, скачивает (та же проверка места, строка `jobs` с `kind=update` — для истории) →
   новая версия `current`, прежняя `current` → `previous`, прежняя `previous` удаляется с диска. В
   конце — запись `status/nightly.stamp` и `nightly_result`. Если `session != ok` — проверка ничего
   не вызывает и лишь обновляет отметку.
5. **Вход.** Шаг 1: Apple ID + пароль → `auth login -e <email> --password-stdin --format json`.
   Код `4` → пароль хранится в памяти `appshelf-web` (не на диске, не в логах) не дольше 10 минут,
   страница просит код. Шаг 2: тот же вызов с `--auth-code` → код `0` → `session=ok`, задания в
   ожидании продолжаются. Пароль удаляется из памяти сразу после шага 2 или по TTL.
6. **Истечение сессии.** Любой вызов с кодом `3` → `session=expired`, `session_since`; если
   `expired_mail_sent` пуст — одно письмо и отметка. Задания остаются `queued`. Успешный вход
   сбрасывает отметку.

Одновременно работает один процесс `ipatool` (`flock` на `/var/lib/appshelf/ipatool.lock`):
веб-обработчик и ночная проверка не пересекаются, общий файл cookies не портится.

## 8. Экраны

Шаблоны Jinja2, вёрстка под iPhone. Всё за паролем. JavaScript — только опрос
`/api/status` раз в 3 с, пока есть задания в работе.

1. **Каталог `/`.** Шапка: состояние сессии (имя, витрина). Баннер, если сессия истекла, или
   место меньше 3 ГБ. Карточки: иконка, название, версия, «iOS N+», размер, дата; кнопка
   «Установить» (`itms-services://?action=download-manifest&url=<manifest>`); ссылка «предыдущая:
   X.Y»; состояния «скачивается» и «ошибка: …» с кнопкой «Повторить»; меню «⋯» → «Снять с
   публикации». Внизу — подсказки (открывать в Safari, подтвердить системное окно, выключить
   «Сгружать неиспользуемые») и итог ночной проверки.
2. **История `/history`.** «Обновить историю» и время обновления; поиск по названию; строки
   (название, bundle id, дата покупки) с кнопкой «Опубликовать» или меткой «в каталоге». Без иконок.
3. **Вход `/apple`.** Состояние сессии; шаг 1 (Apple ID + пароль), шаг 2 (код 2FA); понятные тексты
   ошибок: отказ Apple на edge («Apple отклонил вход с этого адреса — попробуйте позже»), неверный
   пароль или код, истёк срок шага.

## 9. Ошибки и безопасность

| Ситуация | Поведение |
|---|---|
| Отказ Apple на edge при входе (204/301/403/404 без тела) | Текст на `/apple`. `IPATOOL_PROXY` в `appshelf.env` (например, `socks5h://proxy.example:1080`) передаётся в окружение ipatool как `https_proxy`; по умолчанию пуст |
| Ошибка скачивания / `license_not_found` | `apps.status=error`, текст у карточки, «Повторить»; ночная проверка повторит |
| Мало места | Задание `error`, письмо (одно на эпизод) |
| Ночная проверка не отработала | Не обновился `nightly.stamp` → триггер мониторинга (§10) |
| Зависание ipatool | Таймаут вызова: вход и история — 120 с, скачивание — 30 мин; по таймауту процесс убивается, задание `error` |

- Пароль Apple ID: не пишется на диск, в логи, в `jobs.error`; в ipatool — через stdin.
- `/d/` открыт без пароля, но путь содержит 32-символьный токен; `Options -Indexes`.
- `appshelf-web` слушает только unix-сокет (loopback сервера бывает доступен VPN-клиентам).
- Пароль — в приложении для всего, кроме `/d/`, `/healthz`, `/pwa/`, `/login` (Basic Auth Apache снят 2026-10-05
  ради PWA); 10 неудачных проверок за минуту — минуту пароль не проверяется (PBKDF2 ~0,2 с CPU).

## 10. Мониторинг

- `/healthz` — без пароля, для внешней проверки сайта.
- `status/nightly.stamp` — отметка ночной проверки; триггер мониторинга (например, Zabbix
  `vfs.file.time[/var/lib/appshelf/status/nightly.stamp,modify]`) — «не обновлялась больше 26 ч».

## 11. Тестирование

pytest: быстрый набор ≤ 60 с, `timeout = 30`, время только через подмену; `integration` и `manual`
исключены в `addopts`.

- `ipa.py`: zip, собранный в тесте (`Info.plist`, `iTunesMetadata.plist`, иконка) → поля;
  `manifest.plist` — ключи, URL, `bundle-version` (контракт с iOS).
- `store.py`: смена версий `current → previous`, удаление старых каталогов.
- `ipatool.py`: подставной исполняемый скрипт возвращает записанные JSON и коды `0/3/4/5`,
  проверяются исключения, блокировка, таймаут, передача пароля только через stdin.
- `nightly.py`: новая версия против той же; `session != ok` — без вызовов; одно письмо на эпизод;
  защита по месту.
- Вход в два шага: пароль удаляется по TTL (подменённые часы) и после успеха.
- `integration`: `bin/ipatool kbsync --dsid 1`; живой `list-purchases` (по SSH на сервер).
- `manual`: установка на iPhone после деплоя (критерий успеха 3).
