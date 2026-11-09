# AGENTS.md — appshelf

> Глобальные правила — в `~/.codex/AGENTS.md` / `~/.claude/CLAUDE.md`. Полные инструкции — в `CLAUDE.md` рядом.

Своя полка приложений iPhone: IPA из истории покупок Apple ID → OTA-установка из Safari с собственного сервера;
Android-полки участников и публичная полка APK.

## Точки входа
- Дизайн `docs/superpowers/specs/2026-10-05-appshelf-design.md` (в начале — что изменилось позже)
- Вход и несколько Apple ID: docs/superpowers/specs/2026-10-08-multi-apple-id-design.md
- Несколько участников в одном браузере: docs/superpowers/specs/2026-10-12-multi-signin-design.md
- Android-полки, вход Google, публичная полка: docs/superpowers/specs/2026-10-28-android-design.md
- Эксплуатация — `README.md` (английский — `README.en.md`, менять обе вместе)
- Пакет `appshelf/` (FastAPI `web/app.py:main`, ночная проверка `python -m appshelf nightly`)
- Форк ipatool-cpp: `ipatool/UPSTREAM`, `ipatool/patches/`; клиент входа Apple `bin/ipatool-auth` — `ipatool/auth/`;
  сборка обоих `deploy/build-ipatool.sh`, обновление — `ipatool/README.md`
- Боевые значения (хост, домен, архив, мониторинг) — `.env` (не в git) и вики проекта

## Gotchas
- Репозиторий публичный (GitHub): внутренних имён, адресов, путей и учётных данных в git нет.
- Пароль Apple ID не сохраняется нигде: в ipatool — только stdin (`--password-stdin`), в appshelf-web — память ≤ 10 минут.
- ipatool — только от пользователя `appshelf` с `HOME=/etc/appshelf/accounts/<id>` (и `IPATOOL_DEVICE_MAC` из `accounts.device_mac`, если не пуст): HOME выбирает учётку, шифрование привязано к machine-id, у root — ещё к product_uuid.
- Вход Apple без `--password-stdin` идёт прежним C++-путём, который Apple отклоняет; новый путь — `bin/ipatool-auth`, его кэш `bin/ipatool-auth-cache` — root.
- Вход в панель — Apple ID или Google-аккаунт; новые — по приглашению владельца или из «Добавить» вошедшего участника, всегда отдельным участником; у каждого Apple ID своя полка и свой HOME ipatool. Google-аккаунт к другому участнику — только «Связать»; Apple ID не переходит никогда. `GOOGLE_CLIENT_SECRET` — только в `/etc/appshelf/appshelf.env`.
- Google Play: мастер-токен — только файлом `/etc/appshelf/google/<id>/aas` (0600), `oauth_token` — только в обработке формы; прокси (`APPSHELF_PROXIES`) — только для Play; купленное — только на полку того, чей аккаунт купил (проверка на сервере).
- Сеть к источникам APK — только `android/net.py`; публикация APK — только после `apksigner verify`.
- Cookie с состоянием — через `WebCtx` (`web/common.py`): на https префикс `__Host-`.
- appshelf-web — один процесс uvicorn; второй потеряет пароль между шагами входа.
- PUB_TOKEN не печатать и не коммитить.
- Тесты — на Windows (`.venv/Scripts/python -m pytest -q`); `integration` — живые проверки на сервере по SSH (`.env`) и локальные наборы без внешней сети (CLAUDE.md, «Команды»).
