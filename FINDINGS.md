# Findings — appshelf
Побочные находки, только `open`. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.
Новые записи сверху. Выполненные — удаляются (след в `git log`), отклонённые — переносятся в [FINDINGS-archive.md](FINDINGS-archive.md).

## 2026-10-24 · code-review role:review: Комментарий утверждает, что fullmatch не пропускает хвостово... [P2]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/config.py:94-96, category=bug
**What:** Комментарий утверждает, что fullmatch не пропускает хвостовой перевод строки, но это верно лишь наполовину: re.fullmatch якорит конец строки, однако '$' в исходном regex-комментарии — не главное. Главное: TOKEN_RE без якорей с fullmatch действительно отклоняет 'abc...\n', но при этом fullmatch на 32-hex пропускает строку с заглавными A-F? Нет — класс [0-9a-f] их отклоняет, это ок. Реальная проблема: PUB_TOKEN из EnvironmentFile systemd не имеет перевода строки, а вот при чтении env вручную (например, из .env через set -a в deploy-скриптах) значение может прийти с \r на Windows-строке; fullmatch его отклонит с понятной ошибкой — поведение корректное. Понижено до P3-замечания: валидация фактически корректна, но комментарий вводит в заблуждение относительно '$'.
**Evidence:** `if not TOKEN_RE.fullmatch(token): # не match с «$»: тот пропускает хвостовой перевод строки`
**Proposal:** Переписать комментарий: fullmatch якорит оба конца; «$» без re.MULTILINE тоже не пропустил бы «\n» только в режиме, где «$» — перед ним. Фактического бага нет.
**Status:** open

## 2026-10-24 · code-review role:review: Обмен кодом не атомарен по всему набору PARTS: если mv очере... [P2]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/deploy/install.sh:57-61, category=bug
**What:** Обмен кодом не атомарен по всему набору PARTS: если mv очередного «$NEW/$p» упадёт (например, обрыв по питанию/диску между mv OLD и mv NEW), set -e прервёт скрипт, а часть частей уже лежит в $OLD, часть — прежняя в $R; при этом $OLD удаляется только при успешном health-check, так что восстановить вручную можно, но сервис перезапущен не будет и сервер остаётся в смешанном состоянии без автоматического отката.
**Evidence:** `if [ -e "$R/$p" ]; then mv "$R/$p" "$OLD/"; fi mv "$NEW/$p" "$R/$p"`
**Proposal:** Сначала mv всех частей в $OLD, затем всех из $NEW в $R (два цикла), либо делать swap через rename каталога целиком (mv $R/appshelf $OLD/ && mv $NEW/appshelf $R/), чтобы сбой оставлял состояние «всё в OLD».
**Status:** open

## 2026-10-24 · code-review role:review: Проверка «www-data не читает ENV_FILE» выполняется после sys... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/deploy/install.sh:86-89, category=bug
**What:** Проверка «www-data не читает ENV_FILE» выполняется после systemctl restart appshelf-web: если проверка ловит проблему и делает exit 1, новый код уже запущен, при этом секретоносный .env остаётся читаемым www-data, а скрипт выходит с ошибкой, которую деплой-обёртка может интерпретировать как «выкладка не прошла» (хотя код сменился).
**Evidence:** `systemctl restart appshelf-web if sudo -u www-data test -r "$ENV_FILE"; then`
**Proposal:** Перенести проверку доступности $ENV_FILE для www-data до systemctl restart appshelf-web.
**Status:** open

## 2026-10-24 · code-review role:review: printf '%q' даёт bash-специфичное экранирование (например, $... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/deploy/deploy.sh:31-40, category=portability
**What:** printf '%q' даёт bash-специфичное экранирование (например, $'…', \ для пробелов). Итоговая строка вставляется внутрь удалённой команды, которую парсит login-shell удалённого пользователя; если это не bash (dash/sh, некоторые минимальные образы), конструкции вида $'…' или «a\ b» внутри уже стоящих двойных кавычек разберутся неправильно: «\ » внутри двойных кавычек сохранит бэкслэш, и имя группы уйдёт в install.sh искажённым.
**Evidence:** `GROUP="$(printf '%q' "${APPSHELF_CODE_GROUP:-root}")"`
**Proposal:** Ограничить APPSHELF_CODE_GROUP проверкой regex ^[A-Za-z_][A-Za-z0-9_-]*$ на стороне deploy.sh (имя группы POSIX и так не содержит ничего экзотического) и вставлять как есть, либо передавать группу через файл/аргумент с base64, а не через shell-кавычки.
**Status:** open

## 2026-10-24 · code-review role:review: trap на EXIT выполняет удаление временного каталога на хосте... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/deploy/build-ipatool.sh:22-23, category=bug
**What:** trap на EXIT выполняет удаление временного каталога на хосте сборки через ssh; если соединение с хостом сборки уже оборвалось (именно это часто и есть причина выхода), команда в trap тоже упадёт — она перехвачена «|| true», но тогда /tmp/appshelf-ipatool.* на хосте сборки накапливаются. Кроме того, при одновременном запуске двух сборок имена уникальны (mktemp -d), это ок.
**Evidence:** `trap '"${BUILD[@]}" "sudo -n rm -rf $W" || true' EXIT`
**Proposal:** Приемлемо как есть; для чистоты можно добавить в build-ipatool-docker.sh очистку старых /tmp/appshelf-ipatool.* старше N дней, либо выполнять rm локально, когда BUILD — это тот же сервер.
**Status:** open

## 2026-10-24 · code-review role:review: Монтирование /etc/machine-id внутрь контейнера: если на хост... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/deploy/build-ipatool-docker.sh:17-19, category=portability
**What:** Монтирование /etc/machine-id внутрь контейнера: если на хосте сборки файл отсутствует (например, контейнеры-«чужие» хосты без systemd или неинициализированный machine-id), Docker создаст на его месте каталог, и сборка/проверки GUID пойдут не так, как на сервере, молча.
**Evidence:** `docker run --rm --network host -v /etc/machine-id:/etc/machine-id:ro -v "$WORK:/work" \`
**Proposal:** Перед docker run проверить [ -s /etc/machine-id ] и падать с внятной ошибкой, либо передавать machine-id через -e и записывать внутри контейнера.
**Status:** open

## 2026-10-24 · code-review role:review: После удаления последнего Apple ID человека cookie удаляется... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/web/accounts.py:100-106, category=correctness
**What:** После удаления последнего Apple ID человека cookie удаляется целиком (resp.delete_cookie(webauth.COOKIE)), хотя в cookie могут быть записи других людей, у которых аккаунтов в этом браузере нет (например, их полки были удалены ранее с другого устройства, а записи в cookie остались и прошли signed_users как «люди без аккаунтов» — request.state.users может их содержать). Их сессии на этом устройстве теряются вместе с записями.
**Evidence:** `resp.delete_cookie(webauth.COOKIE, path="/") resp.delete_cookie(ACCT_COOKIE, path="/")`
**Proposal:** Вместо удаления всего cookie использовать webauth.without_user(signed, user.id) и выставить оставшееся значение (пустое — тогда удалять).
**Status:** open

## 2026-10-24 · code-review role:review: SSLCertificateChainFile удалён/deprecated начиная с Apache 2... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/deploy/apache-appshelf.conf:13-15, category=portability
**What:** SSLCertificateChainFile удалён/deprecated начиная с Apache 2.4.8 (объединяется в SSLCertificateFile); на свежем Debian 13 (согласно tests.yml — сервер Debian 13) директива либо игнорируется с предупреждением, либо в будущих версиях перестанет существовать, и цепочка не отдастся, если cert.pem без промежуточных.
**Evidence:** `SSLCertificateChainFile /etc/letsencrypt/live/apps.example.com/chain.pem`
**Proposal:** Убрать SSLCertificateChainFile и указать fullchain.pem в SSLCertificateFile (стандарт для certbot).
**Status:** open

## 2026-10-24 · code-review role:review: В /api/status вызов people.get_account(c, aid).session не пр... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/web/app.py:примерно 232-236 (route /api/status), category=bug
**What:** В /api/status вызов people.get_account(c, aid).session не проверяет None. Аккаунт может быть удалён (admin/owner) между выполнением auth-мiddleware (где request.state.acct вычислен) и открытием соединения в хендлере — тогда get_account вернёт None и разыменование .session даст AttributeError, клиент увидит 500 вместо аккуратного ответа; polling-скрипт base.html молча глотает это (.catch), но статус зависает.
**Evidence:** `"session": people.get_account(c, aid).session,`
**Proposal:** acct = people.get_account(c, aid); если None — вернуть {"busy": false, "session": "none", "apps": {}} (или 404), а не разыменовывать.
**Status:** open

## 2026-10-24 · code-review role:review: Неаутентифицированные GET /join/<неверный токен> и /l/<невер... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/web/signin.py:route /join/{token} и /l/{token} (GET-хендлеры), category=security
**What:** Неаутентифицированные GET /join/<неверный токен> и /l/<неверный токен> вызывают w.limiter.fail(), который пополняет тот же глобальный счётчик (10 за минуту), что и неверные пароли. Аноним может тривиально (10 запросов в минуту к публичному эндпоинту) держать blocked() = True постоянно и полностью закрыть вход в панель всем пользователям, включая владельца, — стойкий DoS логина без каких-либо кредов.
**Evidence:** `if invite is None: w.limiter.fail() return login_page(w, request, error=INVITE_GONE, status_code=400)`
**Proposal:** Не считать промахи по публичным токен-ссылкам в общий лимит: либо отдельный счётчик на IP/токен, либо не звать fail() для /join и GET /l (там нет перебора пароля, перебор токенов дешевле отсекать по IP в Apache).
**Status:** open

## 2026-10-24 · code-review role:review: Словарь self._emails чистится только в email_wait() и только... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/web/login.py:класс Limiter, _emails, category=bug
**What:** Словарь self._emails чистится только в email_wait() и только для того адреса, который кто-то пытается войти. Адреса, добавленные email_fail() и больше никогда не проверяемые через email_wait (например, после того как владелец отключил Apple ID, или при спуфинге произвольных адресов через форму входа), остаются в памяти навсегда — неограниченный рост dict в долгоживущем процессе uvicorn.
**Evidence:** `self._emails: dict[str, list[float]] = {}`
**Proposal:** Периодическая чистка (в blocked()/fail() пробегать и выкидывать записи старше email_window) или ограничить размер словаря (например, не добавлять новый адрес, если их уже > N, либо LRU).
**Status:** open

## 2026-10-24 · code-review role:review: Проверяется только отставание схемы (_version < SCHEMA_VERSI... [P3]
**Context:** auto-cron `ClaudeCodeReviewWeekly` (provider=ocg), file appshelf/appshelf/store.py:connect(), category=correctness
**What:** Проверяется только отставание схемы (_version < SCHEMA_VERSION). Если база создана более новой версией appshelf (откат бинарника/образа), код молча работает со схемой, о которой не знает: новые NOT NULL-столбцы без default, изменённые статусы и т.п. приведут к трудно диагностируемым сбоям вместо ясной ошибки при старте.
**Evidence:** `if _version(c) < SCHEMA_VERSION:`
**Proposal:** При _version(c) > SCHEMA_VERSION бросать понятную ошибку («база версии N, у appshelf версия M — откат не поддерживается»), как это сделано в integrity.open_ro.
**Status:** open

