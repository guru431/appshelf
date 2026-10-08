# appshelf — своя полка приложений iPhone

IPA из истории покупок основного Apple ID владельца скачиваются на сервер и ставятся на iPhone из
Safari по `itms-services` (OTA). Дизайн — `docs/superpowers/specs/2026-10-05-appshelf-design.md`.
Вход и несколько Apple ID — `docs/superpowers/specs/2026-10-08-multi-apple-id-design.md`.

## Устройство
- `appshelf/` — пакет: `ipatool.py` (запуск bin/ipatool), `ipa.py` (IPA, иконка, manifest), `store.py` (SQLite),
  `people.py` (люди, Apple ID, ссылки), `jobs.py` (задания, обработчик, общий путь скачивания), `nightly.py`,
  `notify.py`, `webauth.py` (cookie человека), `web/` (вход — `signin.py`), `cli.py`.
- `ipatool/` — `UPSTREAM` и `patches/*.patch` форка ipatool-cpp; правка и обновление — `ipatool/README.md`.
- `deploy/` — юниты systemd, образец vhost Apache и `appshelf.env`, `deploy.sh`, `build-ipatool.sh`.
- Сервер: код `/var/_sh/appshelf`, данные `/var/lib/appshelf` (ссылка `data`), секреты `/etc/appshelf`;
  архив IPA — `APPSHELF_PUB` (сетевая шара, CIFS). База и `tmp/` — только локально: SQLite на CIFS портится,
  а `os.replace` из `tmp/` в архив — EXDEV.
- Боевые значения (хост, домен, путь архива, мониторинг, откат) — не в репозитории: `.env` (не в git,
  образец `.env.example`) и вики проекта.

## Команды
- Тесты (Windows): `.venv/Scripts/python -m pytest -q`; живые на сервере: `.venv/Scripts/python -m pytest -q -m integration`.
- Выкладка: `bash deploy/deploy.sh` (куда — `.env`). Сборка ipatool — на сервере: `sudo bash /var/_sh/appshelf/deploy/build-ipatool.sh`.
- Запасная ссылка входа: `appshelf login-link --email <Apple ID>` (на сервере, от `appshelf`, с `appshelf.env`).

## Правила
- Репозиторий публикуется на GitHub: внутренних имён, адресов, путей, почты и учётных данных в git нет нигде
  (код, тесты, документы, сообщения коммитов). Публикация — только `/github-push`.
- Пароль Apple ID — только stdin ipatool и память appshelf-web (≤ 10 мин); не в файлы, логи, `jobs.error`, argv.
- ipatool запускать только от `appshelf` с `HOME=/etc/appshelf/accounts/<id>` (и `IPATOOL_DEVICE_MAC` из
  `accounts.device_mac`, если не пуст): учётка зашифрована ключом от machine-id (у root — ещё product_uuid),
  файл, созданный root'ом или с другим HOME, служба не прочитает.
- appshelf-web — строго один процесс uvicorn: незавершённые входы, счётчики лимитов и обработчик заданий живут в его памяти.
- PUB_TOKEN — только в `/etc/appshelf/appshelf.env` и имени каталога Apple ID, перенесённого из первой версии;
  каталоги остальных Apple ID — HMAC от него; не в git, конфиге Apache, выводе команд.
- Вход в панель — Apple ID (пароля страниц нет); новый Apple ID — только по приглашению. `APPSHELF_OWNER` и адреса
  участников — только в `/etc/appshelf/appshelf.env` и базе, не в git.
- Каталог кода на сервере получает групповые права по расписанию — данные и секреты там не держать.
- Патчи форка — LF (`*.patch -text`); правка — в `.work/ipatool-src` и перегенерация (`ipatool/README.md`).
