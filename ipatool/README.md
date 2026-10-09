# Форк ipatool-cpp для appshelf

Upstream и закреплённый коммит — `UPSTREAM`. Наши изменения — `patches/NN-*.patch`, применяются по
порядку (`git apply`). Собирает `deploy/build-ipatool.sh` на сервере в одноразовом контейнере
`debian:trixie`, результат — `/var/_sh/appshelf/bin/ipatool` (root) и рядом `ipatool.src-sha256` — сумма
`UPSTREAM` и патчей: `deploy/install.sh` сверяет её с выложенными и при расхождении просит пересборку.

| Патч | Что меняет |
|---|---|
| `01-no-stored-password` | `save_account` не пишет `password`; `silent_relogin` — заглушка: на истёкший токен процесс завершается кодом 3; `auth info` печатает `passwordStored` |
| `02-password-stdin` | `auth login --password-stdin` — пароль из первой строки stdin, не из `-p` (виден в `/proc/<pid>/cmdline`) |
| `03-list-purchases` | `list-purchases [--format json]` → `[{"id","bundleId","name","version","purchaseDate"}]`; перенос `appstore_owned_apps.go` (ipatool v2.5.0, DAAP) на `SapSigner` и HTTP-клиент форка |
| `04-exit-codes` | Коды выхода `0`/`3` `session_expired`/`4` `auth_code_required`/`5` `license_not_found`/`1`; при `--format json` ошибка — `{"error","message"}`; `edge_rejected`, `invalid_credentials`, `not_logged_in`; `list-versions` печатает `latestExternalVersionID`. Исключение — `get-version-metadata`: без учётки печатает `Not logged in.` в stderr и выходит с `1` (appshelf любую её ошибку пропускает — версия в «Истории» необязательна) |
| `05-device-mac` | `IPATOOL_DEVICE_MAC` (12 hex, `:`/`-` допустимы) вместо MAC сетевой карты: из него `guid`, `kbsync`, `fserial` — свой «Mac» на каждый Apple ID; заглушка или мусор → выход `1`, `bad_device_mac`, к Apple не обращается |
| `06-atomic-account-save` | Учётка пишется во временный файл, `fsync` и `rename` поверх `account`: ENOSPC или SIGKILL посреди записи оставляют прежний файл, а не пустой («вход истёк»); `auth login`, не сохранивший учётку, — выход `1`, `save_failed`, а не успех |
| `07-curl-update` | curl, вшиваемый в бинарник, — 8.22.0 с `URL_HASH SHA256` (сумма — `curl.se/info`) вместо 8.7.1 без проверки |

## Когда пересобирать

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
3. Новый коммит — в `UPSTREAM`, перегенерация патчей, сборка и проверки (`deploy/build-ipatool.sh`),
   живая проверка `auth info` / `list-purchases` / `list-versions` от `appshelf` — и `list-versions` по
   приложению, которого нет в покупках Apple ID: выход `5`, `license_not_found` (на этом коде держится
   справочник удалённых, офлайн его не проверить).
