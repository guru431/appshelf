#!/bin/bash
# Сборка bin/ipatool с машины разработчика, после deploy/deploy.sh: bash deploy/build-ipatool.sh
# Собирается выложенное на сервер (/var/_sh/appshelf/ipatool) — с ним deploy/install.sh сверяет bin/ipatool.src-sha256.
# Сборка — deploy/build-ipatool-docker.sh на хосте с Docker и sudo без пароля: APPSHELF_BUILD_SSH=user@host в .env
# (необязательные APPSHELF_BUILD_SSH_PORT, APPSHELF_BUILD_SSH_KEY), без него — на самом сервере. Бинарник идёт через
# эту машину на сервер, проверяется там от appshelf (ldd, справка) и ставится от root. Сервер — APPSHELF_SSH*, как у deploy.sh.
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
# stdout SSH не должен превратиться в произвольный путь или команду, в том числе в trap от root.
if [[ ! "$W" =~ ^/tmp/appshelf-ipatool\.[[:alnum:]]{6}$ ]]; then
  echo "ОШИБКА: хост сборки не вернул ожидаемый временный каталог" >&2
  exit 1
fi
trap '"${BUILD[@]}" "sudo -n rm -rf -- $W" || true' EXIT
"${SSH[@]}" "tar -C $R -cf - ipatool" | "${BUILD[@]}" "tar -xf - -C $W"
"${BUILD[@]}" "mkdir $W/deploy && cat > $W/deploy/build-ipatool-docker.sh" < "$D/deploy/build-ipatool-docker.sh"
"${BUILD[@]}" "sudo -n bash $W/deploy/build-ipatool-docker.sh"

"${BUILD[@]}" "tar -C $W -cf - bin" | "${SSH[@]}" '
  set -e
  R=/var/_sh/appshelf; T="$R/bin/.incoming"; C="$R/bin/ipatool-auth-cache"; S="$R/bin/.ipatool-auth-cache"
  sudo rm -rf "$T" "$S" "$R/bin/.ipatool-auth-preparing"   # последнее — копия helper от прежних сборок
  # 0711: appshelf исполняет проверяемые копии, но не видит распакованного архива
  sudo install -d -o root -g root -m 0711 "$T"
  trap "sudo rm -rf $T $S" EXIT
  sudo tar --no-same-owner -xf - -C "$T"
  # root: appshelf бинарник только исполняет — подменённый ipatool перехватывал бы пароли Apple ID
  sudo install -o root -g root -m 0755 "$T/bin/ipatool" "$T/ipatool"
  sudo install -o root -g root -m 0755 "$T/bin/ipatool-auth" "$T/ipatool-auth"
  # ELF с хоста сборки — не от root: в работе их исполняет appshelf, а ldd на чужом ELF тоже исполняет его код.
  # Библиотеки — сервера, а не хоста сборки. ipatool без аргументов печатает справку, не создавая ~/.ipatool.
  if sudo -u appshelf ldd "$T/ipatool" | grep -q "not found"; then
    echo "ОШИБКА: на сервере не хватает библиотек (обычно: sudo apt-get install -y libunicorn2t64):" >&2
    sudo -u appshelf ldd "$T/ipatool" | grep "not found" >&2
    exit 1
  fi
  sudo -u appshelf "$T/ipatool" > /dev/null
  sudo -u appshelf "$T/ipatool-auth" --help > /dev/null
  # Кэш входа (фреймворки Apple, Unicorn) — root, рядом с бинарником: helper сверяет SHA-256 библиотеки, затем
  # загружает её — appshelf не должен успеть подменить файл. Готовит appshelf в копии, root забирает до chmod:
  # после chown -R каталоги не пополнить, а жёсткая ссылка на файл appshelf (база) не станет общедоступной.
  if [ -d "$C" ]; then sudo cp -a "$C" "$S"; else sudo install -d -m 0755 "$S"; fi
  sudo chown -R appshelf:appshelf "$S"
  sudo -u appshelf env XDG_CACHE_HOME="$S" "$T/ipatool-auth" --prepare
  sudo chown -R root:root "$S"
  if [ -n "$(sudo find "$S" \( ! -type d ! -type f \) -o \( -type f -links +1 \))" ]; then
    echo "ОШИБКА: в подготовленном кэше входа ссылки или особые файлы — не ставлю" >&2
    exit 1
  fi
  sudo chmod -R u=rwX,go=rX "$S"
  sudo rm -rf "$C.previous"
  if [ -d "$C" ]; then sudo mv "$C" "$C.previous"; fi
  sudo mv "$S" "$C"
  sudo rm -rf "$C.previous"
  sudo mv -f "$T/ipatool" "$R/bin/ipatool"   # rename: задание, запускающее ipatool, не застанет полузаписанный файл
  sudo install -o root -g root -m 0644 "$T/bin/ipatool.src-sha256" "$R/bin/ipatool.src-sha256"
  sudo mv -f "$T/ipatool-auth" "$R/bin/ipatool-auth"
  sudo install -o root -g root -m 0644 "$T/bin/ipatool-auth.src-sha256" "$R/bin/ipatool-auth.src-sha256"
  echo "OK: $R/bin/ipatool"'
