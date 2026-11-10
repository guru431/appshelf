#!/bin/bash
# Установка выкладки на сервере — её запускает deploy/deploy.sh: sudo bash /var/_sh/appshelf/.incoming/deploy/install.sh <группа>.
# Код, .venv и bin/ — root, группе кода — только чтение: appshelf их читает и исполняет, но не правит. Иначе любое
# выполнение кода от appshelf (Pillow на иконке из IPA, ipatool) стало бы root при ближайшей выкладке или сборке.
set -euo pipefail
export LC_COLLATE=C  # глобы — побайтно, как sorted() в integrity.builds: отпечаток сборки не зависит от локали
shopt -s nullglob  # без патчей отпечаток содержит только UPSTREAM
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
if sudo -u www-data test -r "$ENV_FILE"; then
  echo "ОШИБКА: www-data читает $ENV_FILE (PUB_TOKEN): убрать www-data из группы appshelf, /etc/appshelf — 0711" >&2
  exit 1
fi
# ночная проверка (до 4 ч) держит прежний код в памяти и догружает модули лениво: после pip и подмены — смесь версий
if systemctl is-active --quiet appshelf-nightly.service; then
  echo "ОШИБКА: идёт ночная проверка — выложить после неё или прервать (прерванное доделает следующая ночь):" >&2
  echo "  sudo systemctl stop appshelf-nightly.service" >&2
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
# AOSP APK verification and JDK JAR verification must be available before switching code.
if ! command -v apksigner >/dev/null || ! command -v javac >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apksigner default-jdk-headless
fi
"$R/.venv/bin/pip" install --quiet --root-user-action=ignore "${NEW}[fourpda]"
"$R/.venv/bin/pip" install --quiet --root-user-action=ignore --no-deps --force-reinstall "$NEW"
"$R/.venv/bin/python" -m compileall -q "$NEW/appshelf"   # __pycache__ appshelf сам не запишет
rm -rf "$OLD"
install -d -o root -g root -m 0700 "$OLD"
restore_previous() {
  for p in "${PARTS[@]}"; do if [ -e "$OLD/$p" ]; then rm -rf -- "${R:?}/${p:?}"; mv "$OLD/$p" "$R/$p"; fi; done
}
install_units() {
  for u in appshelf-web.service appshelf-nightly.service appshelf-nightly.timer appshelf-backup.service appshelf-backup.timer; do
    install -m 0644 "$R/deploy/$u" "/etc/systemd/system/$u"
  done
  systemctl daemon-reload
}
# сбой посреди подмены — прежние части на место: иначе на диске смесь нового и прежнего кода
trap restore_previous ERR
for p in "${PARTS[@]}"; do  # убранные из репозитория файлы уходят вместе с прежним каталогом
  if [ -e "$R/$p" ]; then mv "$R/$p" "$OLD/"; fi
  mv "$NEW/$p" "$R/$p"
done
trap - ERR
rm -rf "$NEW"

install_units
# ночной снимок базы — когда задан его каталог (README «Резервная копия»)
if grep -qE "^APPSHELF_BACKUP_DIR=.+" "$ENV_FILE"; then
  systemctl enable --quiet --now appshelf-backup.timer
else
  systemctl disable --quiet --now appshelf-backup.timer 2>/dev/null || true
fi
# spec 2026-10-08 §6: HOME ipatool на каждый Apple ID; 2026-10-28 §6.4: мастер-токены Google Play
install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts /etc/appshelf/google /etc/appshelf/fourpda /var/lib/appshelf/locks
install -d -o appshelf -g appshelf -m 0700 /var/lib/appshelf/tmp    # TMPDIR appshelf-web: загрузки APK
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
want_auth="$(cat "$R/ipatool/auth/UPSTREAM" "$R"/ipatool/auth/*.go | sha256sum | cut -d' ' -f1)"
if [ "$want_auth" != "$(cat "$R/bin/ipatool-auth.src-sha256" 2>/dev/null || true)" ]; then
  echo "ВНИМАНИЕ: native-клиент входа требует сборки: bash deploy/build-ipatool.sh" >&2
fi

restart_and_check() {
  systemctl restart appshelf-web
  # Type=simple: restart возвращается раньше, чем uvicorn загрузил приложение — ждём ответа.
  # От www-data — так к сокету ходит Apache; /healthz без пароля.
  code=000
  for _ in $(seq 1 20); do
    code=$(sudo -u www-data curl -s -o /dev/null -w "%{http_code}" --unix-socket /run/appshelf/web.sock http://localhost/healthz 2>/dev/null || true)
    if [ "$code" = 200 ]; then return 0; fi
    sleep 0.5
  done
  return 1
}
if restart_and_check; then
  rm -rf "$OLD"
  echo "OK: appshelf-web отвечает на /run/appshelf/web.sock"
  exit 0
fi
echo "ОШИБКА: appshelf-web не ответил 200 на /healthz за 10 с (от www-data, последний код $code)" >&2
systemctl status appshelf-web --no-pager 2>&1 | tail -n 15 >&2
journalctl -u appshelf-web -n 20 --no-pager >&2
# Автооткат — только если база годится прежнему коду, по правилу store.connect: схема не новее его SCHEMA_VERSION
# или её переход записал в state schema_readable_by не новее (иначе прежний код не стартует: MigrationError).
DATA="$(sed -n 's/^APPSHELF_DATA=//p' "$ENV_FILE" | tail -n 1 | tr -d "\"'")"
old_schema="$(sed -n 's/^SCHEMA_VERSION = \([0-9][0-9]*\)$/\1/p' "$OLD/appshelf/store.py" 2>/dev/null || true)"
if [ -n "$old_schema" ] && sudo -u appshelf "$R/.venv/bin/python" -I - "${DATA:-/var/lib/appshelf}/appshelf.db" "$old_schema" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
version = db.execute("PRAGMA user_version").fetchone()[0]
row = db.execute("SELECT value FROM state WHERE key='schema_readable_by'").fetchone()
sys.exit(0 if min(version, int(row[0]) if row else version) <= int(sys.argv[2]) else 1)
PY
then
  restore_previous
  install_units
  if restart_and_check; then
    echo "Возвращён прежний код: appshelf-web отвечает. Новая версия НЕ выложена — причина выше." >&2
  else
    echo "ОШИБКА: прежний код тоже не ответил на /healthz (последний код $code) — смотреть journalctl -u appshelf-web" >&2
  fi
  exit 1
fi
echo "Автооткат невозможен (нет прежнего кода или база новее его). Прежний код — $OLD:" >&2
echo "  for p in ${PARTS[*]}; do rm -rf $R/\$p; mv $OLD/\$p $R/; done; systemctl restart appshelf-web" >&2
exit 1
