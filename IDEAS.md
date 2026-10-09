# Ideas — appshelf
Идеи и feature-запросы (не баги — те в [FINDINGS.md](FINDINGS.md)), только `proposed`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Реализованные — удаляются (след в `git log`), отклонённые — переносятся в [IDEAS-archive.md](IDEAS-archive.md).

## 2026-10-09 · Загружать историю и справочник удалённых сразу после первого входа нового Apple ID [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «UX»; `appshelf/web/signin.py:89-138`, `appshelf/web/app.py:223,240-241`, `appshelf/web/templates/history.html:32`, spec 2026-10-08, критерий 1.
**What:** после входа новый человек видит «Каталог пуст», на Истории — «нажмите «Обновить историю»», и только после ручного нажатия ждёт историю и ещё ~7 минут проверки справочника (484 id). `register()` и `complete()` заданий не ставят; иначе историю загрузит лишь ночная проверка.
**Proposal:** в `register()` после `create_account` и в `complete()`, если у Apple ID нет строк в purchases, — `store.enqueue_once` для `refresh_history` и `check_removed`; на пустой Истории во время загрузки — «загружаю историю…».
**Status:** proposed

## 2026-10-09 · Отмечать в каталоге версии, скачанные ночью: на iPhone их ставят вручную [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «UX»; `appshelf/web/templates/catalog.html:12-14`, `appshelf/nightly.py:95`, `appshelf/store.py:72-86`.
**What:** приложения, удалённые из App Store, сам App Store не обновит — новую версию человек ставит с полки, но каталог не отличает свежую версию от прежней (`versions.downloaded_at` не выводится), а итог ночи — только счётчики без названий.
**Proposal:** метка «новая версия · дата» на карточке, если `current.downloaded_at` моложе 7 дней и есть previous; в итоге ночи для своей полки (`result_for`) — названия обновлённых приложений.
**Status:** proposed

## 2026-10-09 · Подсказывать на /login и в приглашении: входить в Safari, а не во встроенном браузере [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «UX»; `appshelf/web/templates/login.html:5,22-24`, `appshelf/web/templates/catalog.html:30`.
**What:** подсказка «Открывайте в Safari: из Telegram и Chrome установка не запускается» есть только в каталоге, то есть после входа. Приглашение открывают во встроенном браузере мессенджера, проходят там вход с 2FA, а потом в Safari — ещё раз (cookie не общие): второй запрос Apple и второй код.
**Proposal:** строка в плашке приглашения и на шаге 1 `/login`: «Откройте эту ссылку в Safari: во встроенном браузере Telegram или WhatsApp установка не работает, а вход придётся повторить».
**Status:** proposed

## 2026-10-09 · Раздел README «Резервная копия»: база в WAL, PUB_TOKEN, учётки по machine-id [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «эксплуатация»; `README.md:44-50,134-135`, `appshelf/store.py:146-149`, `appshelf/config.py:60-65`.
**What:** нигде не описано, что и как копировать. Копия appshelf.db «на ходу» без `.backup` может быть несогласованной (WAL); без PUB_TOKEN каталоги архива не сопоставить с Apple ID; учётки ipatool зашифрованы ключом от machine-id и на новой машине не читаются.
**Proposal:** короткий раздел README ru/en: `sqlite3 … ".backup …"` (или `VACUUM INTO`) по таймеру, копия appshelf.env и cookie-key в защищённое место; при переносе на другую машину — повторный вход всех Apple ID и перенос архива вместе с PUB_TOKEN.
**Status:** proposed

## 2026-10-09 · Быстрый набор тестов и на Linux: flock, killpg и права файлов сейчас не проверяются [P3]
**Context:** адверсариальный аудит 2026-10-09, линза «тесты и документы»; `appshelf/ipatool.py:47-57,84-91`, `appshelf/jobs.py:161-163,197-200`, `tests/test_jobs.py:51-66`.
**What:** сервер — только Linux, тесты — только на Windows, CI нет: `fcntl.flock` блокировок Apple ID, `os.killpg` по таймауту и права 0644/0755 под `UMask=0077` не исполняет ни один тест. Убери `f.chmod(0o644)` — Apache отдаст 403 на каждую установку, а набор останется зелёным.
**Proposal:** сразу и без Linux: в test_jobs.py, где `Path.chmod` уже перехвачен, проверить 0o644 у app.ipa, icon.png, manifest.plist и 0o755 у каталога версии. Затем workflow на ubuntu в публичном репозитории (`pip install -e .[test] && pytest -q`) и тесты `skipif(os.name != "posix")` для flock и killpg.
**Status:** proposed
