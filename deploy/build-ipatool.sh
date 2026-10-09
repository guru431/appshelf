#!/bin/bash
# Сборка bin/ipatool с машины разработчика, после deploy/deploy.sh: bash deploy/build-ipatool.sh
# Собирается выложенное на сервер (/var/_sh/appshelf/ipatool) — с ним deploy/install.sh сверяет bin/ipatool.src-sha256.
# Сборка — deploy/build-ipatool-docker.sh на хосте с Docker и sudo без пароля: APPSHELF_BUILD_SSH=user@host в .env
# (необязательные APPSHELF_BUILD_SSH_PORT, APPSHELF_BUILD_SSH_KEY), без него — на самом сервере. Бинарник идёт через
# эту машину на сервер, проверяется там (ldd, help) и ставится от root. Сервер — APPSHELF_SSH*, как у deploy.sh.
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$D/.env" ]; then set -a; . "$D/.env"; set +a; fi
: "${APPSHELF_SSH:?нет APPSHELF_SSH=user@host в .env (образец — .env.example)}"
SSH=(ssh -o BatchMode=yes -p "${APPSHELF_SSH_PORT:-22}")
if [ -n "${APPSHELF_SSH_KEY:-}" ]; then SSH+=(-i "$APPSHELF_SSH_KEY" -o IdentitiesOnly=yes); fi
SSH+=("$APPSHELF_SSH")
if [ -n "${APPSHELF_BUILD_SSH:-}" ]; then
  BUILD=(ssh -o BatchMode=yes -o ServerAliveInterval=30 -p "${APPSHELF_BUILD_SSH_PORT:-22}")
  if [ -n "${APPSHELF_BUILD_SSH_KEY:-}" ]; then BUILD+=(-i "$APPSHELF_BUILD_SSH_KEY" -o IdentitiesOnly=yes); fi
  BUILD+=("$APPSHELF_BUILD_SSH")
else
  BUILD=("${SSH[@]}")
fi
R=/var/_sh/appshelf

W="$("${BUILD[@]}" 'mktemp -d /tmp/appshelf-ipatool.XXXXXX')"
trap '"${BUILD[@]}" "sudo -n rm -rf $W" || true' EXIT
"${SSH[@]}" "tar -C $R -cf - ipatool" | "${BUILD[@]}" "tar -xf - -C $W"
"${BUILD[@]}" "mkdir $W/deploy && cat > $W/deploy/build-ipatool-docker.sh" < "$D/deploy/build-ipatool-docker.sh"
"${BUILD[@]}" "sudo -n bash $W/deploy/build-ipatool-docker.sh"

"${BUILD[@]}" "tar -C $W -cf - bin" | "${SSH[@]}" '
  set -e
  R=/var/_sh/appshelf; T="$R/bin/.incoming"
  sudo rm -rf "$T"
  sudo install -d -o root -g root -m 0700 "$T"
  trap "sudo rm -rf $T" EXIT
  sudo tar --no-same-owner -xf - -C "$T"
  # библиотеки сервера, а не хоста сборки
  if sudo ldd "$T/bin/ipatool" | grep -q "not found"; then
    echo "ОШИБКА: на сервере не хватает библиотек (обычно: sudo apt-get install -y libunicorn2t64):" >&2
    sudo ldd "$T/bin/ipatool" | grep "not found" >&2
    exit 1
  fi
  sudo "$T/bin/ipatool" help > /dev/null
  # root: appshelf бинарник только исполняет — подменённый ipatool перехватывал бы пароли Apple ID
  sudo install -o root -g root -m 0755 "$T/bin/ipatool" "$T/ipatool"
  sudo mv -f "$T/ipatool" "$R/bin/ipatool"   # rename: задание, запускающее ipatool, не застанет полузаписанный файл
  sudo install -o root -g root -m 0644 "$T/bin/ipatool.src-sha256" "$R/bin/ipatool.src-sha256"
  echo "OK: $R/bin/ipatool"'
