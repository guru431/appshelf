#!/bin/bash
# Выкладка appshelf на сервер: код, пакет в .venv, юниты, перезапуск appshelf-web, проверка сокета.
# Первичная установка — README «Установка»; bin/ipatool собирает deploy/build-ipatool.sh.
# Куда — .env в корне проекта (не в git, образец .env.example): APPSHELF_SSH=user@host,
# необязательные APPSHELF_SSH_PORT, APPSHELF_SSH_KEY, APPSHELF_CODE_GROUP (группа каталога кода).
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$D/.env" ]; then set -a; . "$D/.env"; set +a; fi
: "${APPSHELF_SSH:?нет APPSHELF_SSH=user@host в .env (образец — .env.example)}"
SSH=(ssh -o BatchMode=yes -p "${APPSHELF_SSH_PORT:-22}")
if [ -n "${APPSHELF_SSH_KEY:-}" ]; then SSH+=(-i "$APPSHELF_SSH_KEY" -o IdentitiesOnly=yes); fi
SSH+=("$APPSHELF_SSH")
tar -C "$D" --exclude=__pycache__ -czf - pyproject.toml appshelf deploy ipatool | "${SSH[@]}" '
  set -e
  sudo tar -xzf - -C /var/_sh/appshelf
  sudo chown -R appshelf:'"${APPSHELF_CODE_GROUP:-appshelf}"' /var/_sh/appshelf/appshelf /var/_sh/appshelf/deploy /var/_sh/appshelf/ipatool /var/_sh/appshelf/pyproject.toml
  sudo -u appshelf /var/_sh/appshelf/.venv/bin/pip install --quiet --no-deps --force-reinstall /var/_sh/appshelf
  for u in appshelf-web.service appshelf-nightly.service appshelf-nightly.timer; do
    sudo install -m 0644 "/var/_sh/appshelf/deploy/$u" "/etc/systemd/system/$u"
  done
  sudo systemctl daemon-reload
  # spec 2026-10-08 §6: Apple ID владельца, HOME ipatool на каждый Apple ID
  if ! sudo grep -qE "^APPSHELF_OWNER=.+" /etc/appshelf/appshelf.env; then
    echo "ОШИБКА: в /etc/appshelf/appshelf.env нет APPSHELF_OWNER=<Apple ID владельца>" >&2
    exit 1
  fi
  sudo install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts /var/lib/appshelf/locks
  if sudo test -d /etc/appshelf/.ipatool && ! sudo test -e /etc/appshelf/accounts/1; then
    sudo systemctl stop appshelf-web       # учётка ipatool переезжает — служба её в это время не трогает
    sudo install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts/1
    sudo mv /etc/appshelf/.ipatool /etc/appshelf/accounts/1/.ipatool
  fi
  sudo systemctl restart appshelf-web
  if sudo -u www-data test -r /etc/appshelf/appshelf.env; then
    echo "ОШИБКА: www-data читает /etc/appshelf/appshelf.env (PUB_TOKEN): убрать www-data из группы appshelf, /etc/appshelf — 0711" >&2
    exit 1
  fi
  # Type=simple: restart возвращается раньше, чем uvicorn загрузил приложение — ждём ответа.
  # От www-data — так к сокету ходит Apache; /healthz без пароля.
  code=000
  for i in $(seq 1 20); do
    code=$(sudo -u www-data curl -s -o /dev/null -w "%{http_code}" --unix-socket /run/appshelf/web.sock http://localhost/healthz 2>/dev/null || true)
    if [ "$code" = 200 ]; then echo "OK: appshelf-web отвечает на /run/appshelf/web.sock"; exit 0; fi
    sleep 0.5
  done
  echo "ОШИБКА: appshelf-web не ответил 200 на /healthz за 10 с (от www-data, последний код $code)" >&2
  systemctl status appshelf-web --no-pager 2>&1 | tail -n 15 >&2
  sudo journalctl -u appshelf-web -n 20 --no-pager >&2
  exit 1'
