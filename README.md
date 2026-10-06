# appshelf

[English](README.en.md)

Своя полка приложений iPhone. IPA из истории покупок вашего Apple ID скачиваются на ваш Linux-сервер,
а на iPhone и iPad ставятся прямо из Safari по системной ссылке `itms-services`: без компьютера,
кабеля и iMazing. Главный сценарий — приложения, удалённые из App Store (российские банки, VK и
другие). Apple их больше не показывает, но у кого они есть в покупках, тот по-прежнему может их скачать.

> appshelf работает только с вашими собственными покупками и не снимает DRM: IPA привязаны к вашему
> Apple ID и запускаются на устройствах, где выполнен вход тем же Apple ID в «Контент и покупки».
> Проект не связан с Apple.

## Возможности

- Вход в Apple ID на странице сайта (пароль и код 2FA); пароль нигде не сохраняется.
- История покупок: публикуете только то, что выбрали. Удалённое из App Store добавляется по ссылке
  `apps.apple.com/…/id…` или по ID.
- Справочник ~480 удалённых из App Store приложений (банки и их клоны, VK, Почта Mail.ru и другие):
  appshelf проверяет, на какие из них у вашего Apple ID есть лицензия.
- Каталог с кнопкой «Установить»; хранятся текущая и предыдущая версии.
- Ночная проверка новых версий; одно письмо, если истёк вход в Apple ID или кончается место.
- PWA: полка на экране «Домой».

## Как устроено

    iPhone (Safari) ─► Apache (HTTPS), vhost apps.example.com
                         ├─ /        → unix:/run/appshelf/web.sock (appshelf-web: вход формой /login)
                         ├─ /healthz → без пароля (мониторинг)
                         ├─ /pwa/    → без пароля (манифест PWA, иконка)
                         └─ /d/      → <архив>/ без пароля (manifest, IPA, иконки; путь с секретным токеном)
    appshelf-web (обработчик заданий) ─┐
    appshelf-nightly (04:30)           ┴─► bin/ipatool (HOME=/etc/appshelf, flock ipatool.lock) ─► Apple

IPA скачивает `bin/ipatool` — форк [ipatool-cpp](https://github.com/Sorvigolova/ipatool) с нашими патчами
(`ipatool/`): пароль не хранится и передаётся только через stdin, есть команда `list-purchases`, коды
выхода для обёртки. Веб-часть — FastAPI и Jinja2, данные — SQLite. Дизайн —
[docs/superpowers/specs/2026-10-05-appshelf-design.md](docs/superpowers/specs/2026-10-05-appshelf-design.md).

| Что | Где |
|---|---|
| Код, `.venv`, `bin/ipatool` | `/var/_sh/appshelf` (путь зашит в `deploy/*.service` и скриптах `deploy/`) |
| Данные | `/var/lib/appshelf`: `appshelf.db`, `tmp/`, `status/nightly.stamp`, `web-auth`, `cookie-key` |
| Архив IPA | `<архив>/<PUB_TOKEN>/`; `<архив>` — `APPSHELF_PUB` или `/var/lib/appshelf/pub`. Может быть сетевой шарой; база и `tmp/` — только локально (SQLite на CIFS портится) |
| Настройки | `/etc/appshelf/appshelf.env` (образец — `deploy/appshelf.env.example`), токен App Store — `/etc/appshelf/.ipatool/` |
| Службы | `appshelf-web.service`, `appshelf-nightly.service` + `.timer` |

## Требования

- Linux-сервер (проверено на Debian 13), Python ≥ 3.11 с `venv`.
- Apache 2.4 с `mod_ssl`, `mod_proxy`, `mod_proxy_http`, `mod_headers` и домен с действительным
  HTTPS-сертификатом: iOS ставит по `itms-services` только с доверенного HTTPS.
- Docker — только для сборки `bin/ipatool`; на хосте нужен пакет `libunicorn2t64`.
- Необязательно: локальный `sendmail` (например, exim4) для писем.

## Установка

Команды на сервере — от пользователя с sudo.

1. Пользователь и каталоги:

       sudo useradd --system --home /var/_sh/appshelf --shell /usr/sbin/nologin appshelf
       sudo install -d -o appshelf -g appshelf -m 0755 /var/_sh/appshelf /var/_sh/appshelf/bin
       sudo install -d -o appshelf -g appshelf -m 0711 /var/lib/appshelf /var/lib/appshelf/pub /var/lib/appshelf/status
       sudo install -d -o appshelf -g appshelf -m 0700 /var/lib/appshelf/tmp
       sudo install -d -o root -g appshelf -m 0711 /etc/appshelf
       sudo install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/.ipatool

2. Код и зависимости. На своей машине:

       tar -czf - pyproject.toml appshelf deploy ipatool | ssh user@server 'sudo tar -xzf - -C /var/_sh/appshelf && sudo chown -R appshelf:appshelf /var/_sh/appshelf'

   На сервере:

       sudo -u appshelf python3 -m venv /var/_sh/appshelf/.venv
       sudo -u appshelf /var/_sh/appshelf/.venv/bin/pip install /var/_sh/appshelf

3. Настройки: `deploy/appshelf.env.example` → `/etc/appshelf/appshelf.env` (`root:appshelf 0640`),
   заполнить `PUB_TOKEN` (`python3 -c "import secrets; print(secrets.token_hex(16))"`),
   `APPSHELF_PUBLIC_BASE`, `MAIL_TO`. Каталог публикации — от `appshelf`:
   `sudo -u appshelf install -d -m 0711 /var/lib/appshelf/pub/<PUB_TOKEN>` (или внутри `APPSHELF_PUB`).

4. `bin/ipatool`: `sudo bash /var/_sh/appshelf/deploy/build-ipatool.sh` → `OK: /var/_sh/appshelf/bin/ipatool`.

5. Службы: на своей машине `.env` по образцу `.env.example` и `bash deploy/deploy.sh`, затем на сервере
   `sudo systemctl enable appshelf-web && sudo systemctl enable --now appshelf-nightly.timer`.

6. Пароль страниц (логин `admin`, пароль — первой строкой stdin):

       sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf set-web-password'

7. Apache: vhost по образцу `deploy/apache-appshelf.conf` (домен, сертификат, путь архива), затем
   `sudo a2enmod ssl proxy proxy_http headers && sudo apache2ctl configtest && sudo systemctl reload apache2`.
   Проверка: `https://<домен>/healthz` → 200, `/` → форма входа.

**Права.** `/var/lib/appshelf` и `status/` — `0711`: Apache и мониторинг проходят, не читая список.
`/etc/appshelf` — `root:appshelf 0711`, Apache не читает `appshelf.env`. **www-data не входит в группу
`appshelf`**: к сокету Apache пускает группа `www-data` на `/run/appshelf` (`ExecStartPost` в юните),
иначе RCE в любом PHP-сайте на сервере читало бы `PUB_TOKEN` (`deploy.sh` это проверяет). Архив на шаре
открыт тем, кому его открывает монтирование; от интернета его закрывают `-Indexes` и токен в ссылке.

## Выкладка и сборка

    bash deploy/deploy.sh                                          # код + перезапуск; куда — .env (.env.example)
    ssh … 'sudo bash /var/_sh/appshelf/deploy/build-ipatool.sh'   # bin/ipatool (после правки ipatool/)

## Вход в Apple ID

Только на странице `/apple`: Apple ID и пароль → код 2FA. Пароль не сохраняется: в ipatool он идёт через
stdin, в appshelf-web живёт в памяти не дольше 10 минут. Истёк токен — одно письмо и баннер; задания ждут входа.

Консольный `auth login` годится лишь для проверки форка: сайт о нём не узнаёт, поэтому задания и ночная
проверка стоят, пока не выполнен вход на `/apple`. Если всё же нужен — только от `appshelf` и под общей
блокировкой (иначе служба не прочитает учётку или столкнётся с идущим скачиванием):

    sudo -u appshelf env HOME=/etc/appshelf flock /var/lib/appshelf/ipatool.lock /var/_sh/appshelf/bin/ipatool auth login -e <Apple ID>

Учётка ipatool зашифрована ключом от `machine-id`: файл, созданный другим пользователем или с другим
`HOME`, служба не прочитает.

## Удалённые из App Store приложения

В истории покупок, которую отдаёт Apple (DAAP, как у Apple Configurator), их нет — даже если лицензия
есть и по id приложение скачивается. Банки к тому же выпускали десятки клонов под чужими названиями.
Поэтому «Обновить историю» ещё и проверяет справочник `appshelf/data/removed_apps.json` (источники — в поле
`source`) по Apple ID: свои добавляются в «Историю» с меткой «удалено из App Store». Кнопка не перепроверяет
то, на что лицензии не было, — это делает ночная проверка, она же дозаполняет версии. Весь справочник с
отметкой «есть / нет в аккаунте» — страница «Удалённые» (`/blocked`); там же поле «Добавить»: ссылка
`apps.apple.com/…/id492224193` или ID, имя подставится из IPA. Пополнить справочник — дописать
`{"id", "name", "aliases"}` в JSON (алиасы — для поиска латиницей: vk, sber…). Снят ли ID с RU App Store —
`itunes.apple.com/lookup?id=…&country=ru` (пусто — снят).

В каталоге у версии — дата сборки: время `Info.plist` в IPA. `releaseDate` Apple (и в
`get-version-metadata`, и в `iTunesMetadata.plist`) — дата первого выпуска приложения, не версии.

## На экран «Домой» (PWA)

Safari → «Поделиться» → «На экран „Домой“»: полка открывается без адресной строки. Манифест и иконка
(`/pwa/`, рисует `web/pwa.py`) отдаются без пароля — в приложении и в Apache (`<Location /pwa/>`).
Окно Basic Auth веб-приложение с экрана «Домой» не показывает (401 → чёрный экран), поэтому вход —
формой `/login` с cookie на год. Service worker нет: iOS ставит веб-приложение и без него, а без сети
полка бесполезна.

## Смена PUB_TOKEN

Ссылки установки и manifest содержат токен: снять все приложения с публикации, записать новый токен
в `appshelf.env`, создать `<архив>/<новый токен>` (от `appshelf`), перезапустить `appshelf-web`,
опубликовать заново.

## Разработка

    python -m venv .venv && .venv/bin/pip install -e ".[test]"   # Windows: .venv\Scripts\pip
    .venv/bin/python -m pytest -q                                 # быстрый набор, без сети
    .venv/bin/python -m pytest -q -m integration                  # живые проверки bin/ipatool по SSH (.env)

Правка и обновление патчей ipatool-cpp — [ipatool/README.md](ipatool/README.md).

## Лицензия

[MIT](LICENSE). ipatool-cpp, к которому относятся патчи в `ipatool/patches/`, — тоже MIT.
