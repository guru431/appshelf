# appshelf — своя полка приложений iPhone

IPA из истории покупок основного Apple ID владельца скачиваются на сервер и ставятся на iPhone из
Safari по `itms-services` (OTA). Дизайн — `docs/superpowers/specs/2026-10-05-appshelf-design.md`.
Вход и несколько Apple ID — `docs/superpowers/specs/2026-10-08-multi-apple-id-design.md`.
Несколько участников в одном браузере — `docs/superpowers/specs/2026-10-12-multi-signin-design.md`.
Android-полки, вход Google, публичная полка — `docs/superpowers/specs/2026-10-28-android-design.md` (этапы 1–2 реализованы).

## Устройство
- `appshelf/` — пакет: `ipatool.py` (запуск bin/ipatool), `ipa.py` (IPA, иконка, manifest), `store.py` (SQLite),
  `people.py` (люди, Apple ID, ссылки), `jobs.py` (задания, обработчик, общий путь скачивания), `nightly.py`,
  `removed.py` (справочник удалённых из App Store), `integrity.py` (проверка целостности полок, снимки базы),
  `notify.py`, `webauth.py` (cookie вошедших людей), `web/` (вход — `signin.py`, раздел Android — `android.py`), `cli.py`.
- `apk.py` (разбор APK: манифест AXML, подпись v1/v2/v3, иконка), `android/` — `db.py` (таблицы android_*), `net.py`
  (HTTP источников: только https и глобальные адреса), `rustore.py`, `sources.py`, `fetch.py` (конвейер и задания),
  `pin.py` (PIN публичной полки, перебор, cookie гостя); гостевая страница `/p` — в `web/android.py`.
- `ipatool/` — `UPSTREAM` и `patches/*.patch` форка ipatool-cpp; правка и обновление — `ipatool/README.md`.
- `deploy/` — юниты systemd, образец vhost Apache и `appshelf.env`, `deploy.sh` (git archive HEAD → сервер),
  `install.sh` (установка на сервере от root), `build-ipatool.sh` (сборка ipatool → сервер) и его шаг в Docker
  `build-ipatool-docker.sh`.
- Сервер: код `/var/_sh/appshelf`, данные `/var/lib/appshelf` (ссылка `data`), секреты `/etc/appshelf`;
  архив IPA — `APPSHELF_PUB` (сетевая шара, CIFS). База и `tmp/` — только локально: SQLite на CIFS портится,
  а `os.replace` из `tmp/` в архив — EXDEV.
- Боевые значения (хост, домен, путь архива, мониторинг, откат) — не в репозитории: `.env` (не в git,
  образец `.env.example`) и вики проекта.

## Команды
- Тесты (Windows): `.venv/Scripts/python -m pytest -q`; живые на сервере: `.venv/Scripts/python -m pytest -q -m integration`.
- Выкладка: `bash deploy/deploy.sh` (куда — `.env`). Сборка ipatool — после выкладки, тоже со своей машины:
  `bash deploy/build-ipatool.sh` (Docker на сервере нет — сборка на хосте `APPSHELF_BUILD_SSH` из `.env`).
- Запасная ссылка входа: `appshelf login-link --email <Apple ID>`, новый токен полки: `appshelf rotate-token --email
  <Apple ID>` (Android-полки участника — `--email <Apple ID> --android`, публичной — `--public`), проверка
  целостности: `appshelf check [--deep]` (только чтение), снимок базы: `appshelf backup --out <каталог>` (ночной —
  `appshelf-backup.timer`) — на сервере, от `appshelf`, с `appshelf.env`; от root CLI откажется.

## Правила
- Репозиторий публикуется на GitHub: внутренних имён, адресов, путей, почты и учётных данных в git нет нигде
  (код, тесты, документы, сообщения коммитов). Публикация — только `/github-push`.
- Пароль Apple ID — только stdin ipatool и память appshelf-web (≤ 10 мин); не в файлы, логи, `jobs.error`, argv.
- ipatool запускать только от `appshelf` с `HOME=/etc/appshelf/accounts/<id>` (и `IPATOOL_DEVICE_MAC` из
  `accounts.device_mac`, если не пуст): HOME выбирает учётку Apple ID (путь в ключ не входит), ключ — от
  machine-id, а у root ещё и от product_uuid: файл, сохранённый root'ом, служба не расшифрует.
- appshelf-web — строго один процесс uvicorn: незавершённые входы, счётчики лимитов и обработчик заданий живут в его памяти.
- PUB_TOKEN — только в `/etc/appshelf/appshelf.env` и имени каталога Apple ID, перенесённого из первой версии;
  каталоги остальных Apple ID — свои случайные токены (`accounts.shelf`); не в git, конфиге Apache, выводе команд.
- Вход в панель — Apple ID (пароля страниц нет); новый Apple ID — только по приглашению или из «Добавить» уже
  вошедшего участника. `APPSHELF_OWNER` и адреса участников — только в `/etc/appshelf/appshelf.env` и базе, не в git.
- Вход добавляет участника к вошедшим в браузере, а не заменяет их (cookie — список записей, по одной на
  участника). Каждый Apple ID — отдельный участник: общий — тоже, новый из «Добавить» — тоже (решение 2026-10-13).
  К чужому участнику Apple ID не привязывать и не переносить, иначе вход им откроет и личные полки.
- Каталог кода на сервере получает групповые права по расписанию — данные и секреты там не держать. Код, `.venv`
  и `bin/` — root (`deploy/install.sh`): appshelf только читает и исполняет, иначе его код стал бы root при выкладке.
- Патчи форка — LF (`*.patch -text`); правка — в `.work/ipatool-src` и перегенерация (`ipatool/README.md`).
- Сеть к источникам APK — только через `android/net.py`: адрес проверяется перед каждым запросом и после каждого
  редиректа, проверка сертификата не отключается (доп. корни НУЦ — файл `APPSHELF_SOURCES_CA`).
- Android-полка — у участника (`android_shelves.user_id`); удаление участника удаляет и её (`jobs._remove`).
- Публичная Android-полка — одна (`user_id IS NULL`), менять её — только владельцу (проверка на сервере).
- PIN публичной полки — только соль и хэш в `state`; сам PIN не в git, базе, журнале, `jobs.error`, письмах. Адрес
  гостя для счётчиков перебора — последний элемент `X-Forwarded-For` (его дописывает Apache), не первый.
