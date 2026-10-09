#!/bin/bash
# Установка выкладки на сервере — её запускает deploy/deploy.sh: sudo bash /var/_sh/appshelf/.incoming/deploy/install.sh <группа>.
# Код, .venv и bin/ — root, группе кода — только чтение: appshelf их читает и исполняет, но не правит. Иначе любое
# выполнение кода от appshelf (Pillow на иконке из IPA, ipatool) стало бы root при ближайшей выкладке или сборке.
set -euo pipefail
R=/var/_sh/appshelf
NEW="$R/.incoming"
OLD="$R/.previous"
GROUP="${1:-root}"
ENV_FILE=/etc/appshelf/appshelf.env
PARTS=(pyproject.toml appshelf deploy ipatool)

# spec 2026-10-08 §6: без Apple ID владельца новый код не стартует (MigrationError) — не ставим его вовсе
if ! grep -qE "^APPSHELF_OWNER=.+" "$ENV_FILE"; then
  echo "ОШИБКА: в $ENV_FILE нет APPSHELF_OWNER=<Apple ID владельца>" >&2
  exit 1
fi
# пустая точка монтирования — тоже каталог: при несмонтированной шаре IPA легли бы на локальный диск
PUB="$(sed -n 's/^APPSHELF_PUB=//p' "$ENV_FILE" | tail -n 1 | tr -d "\"'")"
if [ -n "$PUB" ] && mountpoint -q "$PUB"; then
  echo "ОШИБКА: APPSHELF_PUB=$PUB — сама точка монтирования; нужен подкаталог внутри шары (README, «Что и где»)" >&2
  exit 1
fi
if id -nG appshelf | tr ' ' '\n' | grep -qx "$GROUP"; then
  echo "ОШИБКА: appshelf входит в группу кода $GROUP — APPSHELF_CODE_GROUP в .env должна быть другой" >&2
  exit 1
fi

chown -R root:"$GROUP" "$NEW"
chmod -R u=rwX,go=rX "$NEW"
# до 2026-10-09 каталогом кода владел appshelf: код, .venv, bin/, следы pip — всё теперь root
find "$R" -xdev -user appshelf -exec chown -h root:root {} +
for d in "$R/.venv" "$R/bin"; do
  if [ -e "$d" ]; then chmod -R go-w "$d"; fi
done
# зависимости и пакет — до подмены кода: упади pip, работать останется прежний код
"$R/.venv/bin/pip" install --quiet --root-user-action=ignore "$NEW"
"$R/.venv/bin/pip" install --quiet --root-user-action=ignore --no-deps --force-reinstall "$NEW"
"$R/.venv/bin/python" -m compileall -q "$NEW/appshelf"   # __pycache__ appshelf сам не запишет
rm -rf "$OLD"
install -d -o root -g root -m 0700 "$OLD"
for p in "${PARTS[@]}"; do  # убранные из репозитория файлы уходят вместе с прежним каталогом
  if [ -e "$R/$p" ]; then mv "$R/$p" "$OLD/"; fi
  mv "$NEW/$p" "$R/$p"
done
rm -rf "$NEW"

for u in appshelf-web.service appshelf-nightly.service appshelf-nightly.timer; do
  install -m 0644 "$R/deploy/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
# spec 2026-10-08 §6: HOME ipatool на каждый Apple ID
install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts /var/lib/appshelf/locks
if [ -d /etc/appshelf/.ipatool ] && [ ! -e /etc/appshelf/accounts/1 ]; then
  systemctl stop appshelf-web       # учётка ipatool переезжает — служба её в это время не трогает
  install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts/1
  mv /etc/appshelf/.ipatool /etc/appshelf/accounts/1/.ipatool
fi
# остатки версии 1 (spec 2026-10-08 §6): хэш пароля страниц и общая блокировка ipatool
rm -f /var/lib/appshelf/web-auth /var/lib/appshelf/ipatool.lock

# bin/ipatool собран из выложенных UPSTREAM и патчей? Сборка — отдельно: Docker, минуты
want="$(cat "$R/ipatool/UPSTREAM" "$R"/ipatool/patches/*.patch | sha256sum | cut -d' ' -f1)"
if [ "$want" != "$(cat "$R/bin/ipatool.src-sha256" 2>/dev/null || true)" ]; then
  echo "ВНИМАНИЕ: bin/ipatool собран не из выложенных ipatool/UPSTREAM и патчей — нужна пересборка:" >&2
  echo "  на своей машине: bash deploy/build-ipatool.sh" >&2
fi

systemctl restart appshelf-web
if sudo -u www-data test -r "$ENV_FILE"; then
  echo "ОШИБКА: www-data читает $ENV_FILE (PUB_TOKEN): убрать www-data из группы appshelf, /etc/appshelf — 0711" >&2
  exit 1
fi
# Type=simple: restart возвращается раньше, чем uvicorn загрузил приложение — ждём ответа.
# От www-data — так к сокету ходит Apache; /healthz без пароля.
code=000
for _ in $(seq 1 20); do
  code=$(sudo -u www-data curl -s -o /dev/null -w "%{http_code}" --unix-socket /run/appshelf/web.sock http://localhost/healthz 2>/dev/null || true)
  if [ "$code" = 200 ]; then
    rm -rf "$OLD"
    echo "OK: appshelf-web отвечает на /run/appshelf/web.sock"
    exit 0
  fi
  sleep 0.5
done
echo "ОШИБКА: appshelf-web не ответил 200 на /healthz за 10 с (от www-data, последний код $code)" >&2
systemctl status appshelf-web --no-pager 2>&1 | tail -n 15 >&2
journalctl -u appshelf-web -n 20 --no-pager >&2
echo "Прежний код — $OLD: for p in ${PARTS[*]}; do rm -rf $R/\$p; mv $OLD/\$p $R/; done; systemctl restart appshelf-web" >&2
exit 1
