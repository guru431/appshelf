# Форк ipatool-cpp для appshelf

Upstream и закреплённый коммит — `UPSTREAM`. Наши изменения — `patches/NN-*.patch`, применяются по
порядку (`git apply`). Собирает `deploy/build-ipatool.sh` с машины разработчика: выложенные на сервер
`UPSTREAM` и патчи — в одноразовом контейнере `debian:trixie` на хосте с Docker (`APPSHELF_BUILD_SSH` в `.env`,
не задан — на самом сервере). Результат — `/var/_sh/appshelf/bin/ipatool` (root) и рядом `ipatool.src-sha256` —
сумма `UPSTREAM` и патчей: `deploy/install.sh` сверяет её с выложенными и при расхождении просит пересборку.

Вход в Apple (патч 11) выполняет `bin/ipatool-auth`: закреплённый в `auth/UPSTREAM` Go-ipatool и адаптер
`auth/*.go`, собранный тем же скриптом (отпечаток — `bin/ipatool-auth.src-sha256`, сумма `auth/UPSTREAM` и
`auth/*.go`), и его кэш библиотек `bin/ipatool-auth-cache`. Устройство — [auth/README.md](auth/README.md).
Сборку обоих и их возраст показывает и `appshelf check` на сервере.

| Патч | Что меняет |
|---|---|
| `01-no-stored-password` | `save_account` не пишет `password`; `silent_relogin` — заглушка: на истёкший токен процесс завершается кодом 3; `auth info` печатает `passwordStored` |
| `02-password-stdin` | `auth login --password-stdin` — пароль из первой строки stdin, не из `-p` (виден в `/proc/<pid>/cmdline`) |
| `03-list-purchases` | `list-purchases [--format json]` → `[{"id","bundleId","name","version","purchaseDate"}]`; перенос `appstore_owned_apps.go` (ipatool v2.5.0, DAAP) на `SapSigner` и HTTP-клиент форка |
| `04-exit-codes` | Коды выхода `0`/`3` `session_expired`/`4` `auth_code_required`/`5` `license_not_found`/`1`; при `--format json` ошибка — `{"error","message"}`; `edge_rejected`, `invalid_credentials`, `not_logged_in`; `list-versions` печатает `latestExternalVersionID`. Исключение — `get-version-metadata`: без учётки печатает `Not logged in.` в stderr и выходит с `1` (appshelf любую её ошибку пропускает — версия в «Истории» необязательна) |
| `05-device-mac` | `IPATOOL_DEVICE_MAC` (12 hex, `:`/`-` допустимы) вместо MAC сетевой карты: из него `guid`, `kbsync`, `fserial` — свой «Mac» на каждый Apple ID; заглушка или мусор → выход `1`, `bad_device_mac`, к Apple не обращается |
| `06-atomic-account-save` | Учётка пишется во временный файл, `fsync` и `rename` поверх `account`: ENOSPC или SIGKILL посреди записи оставляют прежний файл, а не пустой («вход истёк»); `auth login`, не сохранивший учётку, — выход `1`, `save_failed`, а не успех |
| `07-curl-update` | curl, вшиваемый в бинарник, — 8.22.0 с `URL_HASH SHA256` (сумма — `curl.se/info`) вместо 8.7.1 без проверки |
| `08-login-transport-diagnostics` | Linux: этап подготовки входа, номера попыток, тип сервера, HTTP-статус, размер и тип ответа, наличие перенаправления, требования подготовки устройства и Retry-After — в syslog; только числа и фиксированные значения, без адреса Apple ID, пароля, cookies, подписи и тела ответа |
| `09-session-aware-bag` | Справочник для входа запрашивается с `ix=6`; конфигурация SAP читается и из вложенного `urlBag`, и из плоского ответа Apple. Запросы каталога сохраняют прежний формат |
| `10-current-store-auth-format` | User-Agent Configurator 2.21 для справочника входа и authenticate выбирает современный native endpoint Apple; тип XML-тела входа — `application/x-apple-plist`, как в текущем ipatool |
| `11-native-auth-adapter` | Linux-вход через stdin выполняет закреплённый современный ipatool; пароль не сохраняется, успешный результат импортируется в прежний шифрованный формат. Подробности — `auth/README.md` |

## Когда пересобирать

Сборка также выполняет офлайн-контракт `tests/bag_contract.cpp`: вложенный и плоский справочники Apple,
сохранение параметров SAP, изоляция настроек входа от каталога и отказ на пустой HTTP 204.

`STATIC_BUILD` вшивает в бинарник OpenSSL из образа `debian:trixie` на день сборки и curl из патча 07 —
`apt upgrade` на сервере до них не доходит:

- вышел DSA на `openssl` — пересобрать (`apt-get` в контейнере берёт исправленный `libssl-dev`);
- вышел выпуск curl с исправлением уязвимости — поднять версию и `URL_HASH` в патче 07, пересобрать;
- без поводов — раз в квартал.

Образ по digest не закрепляется: пакеты сборки `apt-get` всё равно берёт текущие, а закреплённый образ
только задерживал бы исправления.

## Правка патчей

Рабочая копия — `.work/ipatool-src` (в `.gitignore`): ветка `appshelf` от коммита из `UPSTREAM`,
по коммиту на патч, тема коммита = имя патча без `.patch`.

    . ipatool/UPSTREAM; W=.work/ipatool-src
    git clone -c core.autocrlf=false "$URL" "$W" && git -C "$W" checkout -b appshelf "$COMMIT"
    for p in ipatool/patches/*.patch; do git -C "$W" apply "$PWD/$p" && git -C "$W" commit -qam "$(basename "$p" .patch)"; done

Исправление в патче N: правка → `git -C "$W" commit -a --fixup=<коммит патча N>` →
`git -C "$W" rebase --autosquash "$COMMIT"` (git ≥ 2.44). Перегенерация файлов:

    for c in $(git -C "$W" rev-list --reverse "$COMMIT"..appshelf); do
      git -C "$W" diff "$c~1" "$c" > "ipatool/patches/$(git -C "$W" log -1 --format=%s "$c").patch"
    done

## Обновление upstream

1. `git -C "$W" fetch origin` и дифф `git -C "$W" diff "$COMMIT"..origin/main`: в первую очередь новые
   адреса (`https://`, сырые IP), вызовы процессов (`system`, `popen`, `exec`), новые зависимости сборки.
2. `git -C "$W" rebase --onto origin/main "$COMMIT" appshelf`; конфликты решаются в коммитах патчей.
3. Новый коммит — в `UPSTREAM`, перегенерация патчей, выкладка (`deploy/deploy.sh`), сборка и проверки
   (`deploy/build-ipatool.sh`),
   живая проверка `auth info` / `list-purchases` / `list-versions` от `appshelf` — и `list-versions` по
   приложению, которого нет в покупках Apple ID: выход `5`, `license_not_found` (на этом коде держится
   справочник удалённых, офлайн его не проверить).

## Обновление клиента входа (`auth/UPSTREAM`)

Адаптер копируется в `cmd/appshelf-auth` клона upstream и импортирует его внутренние пакеты (`internal/sap`,
`internal/store/profile`, `pkg/appstore`), поэтому обновление — вместе с проверкой адаптера.

1. Клон `URL` из `auth/UPSTREAM` и дифф `"$COMMIT"..<новый коммит>`: новые адреса и вызовы процессов
   (`exec`), `go.mod`/`go.sum`, `internal/sap` (закреплённые SHA-256 библиотек Apple и Unicorn, кэш),
   сигнатуры пакетов, которые вызывает адаптер; лицензия — сверить с `auth/LICENSE.upstream`.
2. Новый коммит — в `auth/UPSTREAM`; правки адаптера — в `auth/*.go`. Офлайн-проверка — сборка:
   `deploy/build-ipatool-docker.sh` гоняет `go test` адаптера и собирает бинарник.
3. Выкладка (`deploy/deploy.sh`), сборка (`deploy/build-ipatool.sh`: новый `--prepare` пополнит
   `bin/ipatool-auth-cache`), затем живой вход в панели (или консольный — README, «Вход и участники») и
   `auth info` / `list-purchases` от `appshelf`.
