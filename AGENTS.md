# AGENTS.md — appshelf

> Глобальные правила — в `~/.codex/AGENTS.md` / `~/.claude/CLAUDE.md`. Полные инструкции — в `CLAUDE.md` рядом.

Своя полка приложений iPhone: IPA из истории покупок Apple ID → OTA-установка из Safari с собственного сервера.

## Точки входа
- Дизайн `docs/superpowers/specs/2026-10-05-appshelf-design.md`
- Пакет `appshelf/` (FastAPI `web/app.py:main`, ночная проверка `python -m appshelf nightly`)
- Форк ipatool-cpp: `ipatool/UPSTREAM`, `ipatool/patches/`, сборка `deploy/build-ipatool.sh`
- Боевые значения (хост, домен, архив, мониторинг) — `.env` (не в git) и вики проекта

## Gotchas
- Репозиторий публичный (GitHub): внутренних имён, адресов, путей и учётных данных в git нет.
- Пароль Apple ID не сохраняется нигде: в ipatool — только stdin, в appshelf-web — память ≤ 10 минут.
- ipatool — только от пользователя `appshelf` с `HOME=/etc/appshelf` (шифрование учётки привязано к machine-id).
- appshelf-web — один процесс uvicorn; второй потеряет пароль между шагами входа.
- PUB_TOKEN не печатать и не коммитить.
- Тесты — на Windows (`.venv/Scripts/python -m pytest -q`), `integration` ходит на сервер по SSH (`.env`).
