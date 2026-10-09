#!/bin/bash
# Выкладка appshelf на сервер: закоммиченный HEAD (git archive, не рабочее дерево) → /var/_sh/appshelf/.incoming
# → deploy/install.sh от root (код и .venv — root, юниты, перезапуск appshelf-web, проверка сокета).
# Откат — выложить прежний коммит. Первичная установка — README «Установка»; bin/ipatool собирает
# deploy/build-ipatool.sh. Куда — .env в корне проекта (не в git, образец .env.example): APPSHELF_SSH=user@host,
# необязательные APPSHELF_SSH_PORT, APPSHELF_SSH_KEY, APPSHELF_CODE_GROUP (группа каталога кода: только чтение).
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$D/.env" ]; then set -a; . "$D/.env"; set +a; fi
: "${APPSHELF_SSH:?нет APPSHELF_SSH=user@host в .env (образец — .env.example)}"
PARTS=(pyproject.toml appshelf deploy ipatool)
if [ -n "$(git -C "$D" status --porcelain -- "${PARTS[@]}")" ]; then
  echo "ОШИБКА: незакоммиченные изменения в ${PARTS[*]} — выкладывается только закоммиченный HEAD" >&2
  exit 1
fi
SSH=(ssh -o BatchMode=yes -p "${APPSHELF_SSH_PORT:-22}")
if [ -n "${APPSHELF_SSH_KEY:-}" ]; then SSH+=(-i "$APPSHELF_SSH_KEY" -o IdentitiesOnly=yes); fi
SSH+=("$APPSHELF_SSH")
git -C "$D" archive --format=tar.gz HEAD "${PARTS[@]}" | "${SSH[@]}" '
  set -e
  R=/var/_sh/appshelf
  # каталог кода — root: будь он у appshelf, тот подменил бы .incoming между распаковкой и запуском от root
  sudo chown root:root "$R"
  sudo chmod 0755 "$R"
  sudo rm -rf "$R/.incoming"
  sudo install -d -o root -g root -m 0700 "$R/.incoming"
  sudo tar -xzf - -C "$R/.incoming"
  sudo bash "$R/.incoming/deploy/install.sh" "'"${APPSHELF_CODE_GROUP:-root}"'"'
