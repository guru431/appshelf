#!/bin/bash
# Сборка bin/ipatool в Docker: upstream ipatool-cpp + патчи appshelf (spec §5). Шаг deploy/build-ipatool.sh на хосте
# сборки, от root: sudo bash <каталог>/deploy/build-ipatool-docker.sh, где <каталог>/ipatool — копия выложенного на сервер.
# Результат — <каталог>/bin/ipatool и ipatool.src-sha256; на сервер их ставит build-ipatool.sh.
# Пользователь appshelf в группу docker не входит (это равносильно root).
# OpenSSL вшивается из образа на день сборки — после DSA на openssl пересобрать (ipatool/README.md).
set -euo pipefail
shopt -s nullglob  # пустой каталог патчей допустим и при вычислении отпечатка
D="$(cd "$(dirname "$0")/.." && pwd)"
. "$D/ipatool/UPSTREAM"                       # URL, COMMIT
OUT="$D/bin/ipatool"
# без файла Docker создал бы на его месте каталог — и в /etc хоста сборки
if [ ! -s /etc/machine-id ]; then
  echo "ОШИБКА: на хосте сборки нет /etc/machine-id (systemd-machine-id-setup)" >&2
  exit 1
fi
WORK="$(mktemp -d)"
# хост сборки может быть чужим: образ, который скачала эта сборка, после неё удаляется
PULLED=1
if docker image inspect debian:trixie > /dev/null 2>&1; then PULLED=; fi
trap 'rm -rf "$WORK"; if [ -n "$PULLED" ]; then docker image rm debian:trixie > /dev/null || true; fi' EXIT
cp -r "$D/ipatool/patches" "$WORK/patches"
cp -r "$D/ipatool/tests" "$WORK/tests"
cp -r "$D/ipatool/auth" "$WORK/auth"
# --network host и machine-id хоста — как при проверке 2026-10-05: тот же MAC (GUID запросов) и machine-id
docker run --rm --network host -v /etc/machine-id:/etc/machine-id:ro -v "$WORK:/work" \
  -e URL="$URL" -e COMMIT="$COMMIT" debian:trixie bash -euo pipefail -c '
  shopt -s nullglob
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    ca-certificates git cmake build-essential perl pkg-config file \
    libssl-dev libminizip-dev zlib1g-dev libunicorn-dev nlohmann-json3-dev libzstd-dev \
    libcurl4-openssl-dev golang-go >/dev/null
  git clone -q "$URL" /work/src
  cd /work/src
  git checkout -q "$COMMIT"
  for p in /work/patches/*.patch; do git apply "$p"; done
  # Offline regression: flat native-client bags must retain SAP configuration,
  # and authentication routing must leave catalog requests unchanged.
  timeout 90 g++ -std=c++20 -O0 -ffunction-sections -fdata-sections -I/work/src \
    /work/tests/bag_contract.cpp appstore.cpp plist.cpp -Wl,--gc-sections -lz -o /work/bag-contract
  timeout 30 /work/bag-contract
  # libcrypto.a Debian собран с zstd: статический libzstd — в самый конец строки линковки
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DSTATIC_BUILD=ON -DCMAKE_CXX_STANDARD_LIBRARIES="-l:libzstd.a" \
    > /work/cmake.log 2>&1 || { tail -n 40 /work/cmake.log; exit 1; }
  grep -q "SAP assets: embedding via objcopy" /work/cmake.log   # иначе бинарник ищет sap_assets/ в текущем каталоге
  cmake --build build -j"$(nproc)" > /work/build.log 2>&1 || {
    grep -E "error|undefined reference" /work/build.log | sed "s/.*undefined reference to/undefined:/" | sort -u | head -n 60
    exit 1; }
  strip build/ipatool
  # Offline account-import contract under an unprivileged user, with synthetic data.
  CHECK="$(mktemp -d /tmp/ipatool-import.XXXXXX)"
  chmod 0755 "$CHECK"
  install -m 0755 build/ipatool "$CHECK/ipatool"
  install -d -o 65534 -g 65534 -m 0700 "$CHECK/home"
  fixture="{\"email\":\"owner@example.invalid\",\"name\":\"Fixture\",\"directoryServicesIdentifier\":\"1\",\"passwordToken\":\"fixture-token\",\"storeFront\":\"143441-1,29\",\"pod\":\"42\"}"
  printf "%s\n" "$fixture" | setpriv --reuid=65534 --regid=65534 --clear-groups env HOME="$CHECK/home" \
    "$CHECK/ipatool" --format json auth import > /work/import.txt
  grep -q "\"passwordStored\":false" /work/import.txt
  before="$(sha256sum "$CHECK/home/.ipatool/account")"
  bad_fixture="${fixture%?},\"password\":\"never-store\"}"
  set +e
  printf "%s\n" "$bad_fixture" | setpriv --reuid=65534 --regid=65534 --clear-groups env HOME="$CHECK/home" \
    "$CHECK/ipatool" --format json auth import >/dev/null; bad_password=$?
  printf "%s\n" "{\"email\":\"other@example.invalid\",\"directoryServicesIdentifier\":\"2\",\"passwordToken\":\"other-token\",\"storeFront\":\"143441\"}" | \
    setpriv --reuid=65534 --regid=65534 --clear-groups env HOME="$CHECK/home" "$CHECK/ipatool" --format json auth import >/dev/null; foreign=$?
  set -e
  test "$bad_password" = 1; test "$foreign" = 1
  test "$before" = "$(sha256sum "$CHECK/home/.ipatool/account")"
  # Administrative group write is allowed only when the application cannot write.
  printf "%s\n" "#!/bin/sh" "exit 77" > "$CHECK/ipatool-auth"
  chmod 0775 "$CHECK/ipatool-auth"
  set +e
  setpriv --reuid=65534 --regid=65534 --clear-groups env HOME="$CHECK/home" \
    "$CHECK/ipatool" --format json auth login --password-stdin </dev/null >/dev/null; safe_helper=$?
  chown 0:65534 "$CHECK/ipatool-auth"
  setpriv --reuid=65534 --regid=65534 --clear-groups env HOME="$CHECK/home" \
    "$CHECK/ipatool" --format json auth login --password-stdin </dev/null >/dev/null; writable_helper=$?
  set -e
  test "$safe_helper" = 77; test "$writable_helper" = 1
  # проверки без обращения к Apple
  ./build/ipatool help > /dev/null
  HOME=/work/h1 ./build/ipatool --format json kbsync --dsid 1 | grep -q "\"success\":true"
  # коды выхода патча 04 — без обращения к Apple: учётки нет (на list-versions держится справочник удалённых)
  set +e
  HOME=/work/h2 ./build/ipatool --format json list-purchases > /work/lp.txt; rc1=$?
  HOME=/work/h2 ./build/ipatool --format json download -i 1 -o /work > /work/dl.txt; rc2=$?
  printf "pw\n" | HOME=/work/h2 ./build/ipatool --format json auth login --password-stdin > /work/li.txt; rc3=$?
  HOME=/work/h2 ./build/ipatool --format json list-versions -i 1 > /work/lv.txt; rc4=$?
  set -e
  test "$rc1" = 3; grep -q "\"error\":\"not_logged_in\"" /work/lp.txt
  test "$rc2" = 3; grep -q "\"error\":\"not_logged_in\"" /work/dl.txt
  test "$rc3" = 1; grep -q "\"error\":\"usage\"" /work/li.txt
  test "$rc4" = 3; grep -q "\"error\":\"not_logged_in\"" /work/lv.txt
  # патч 05: MAC из IPATOOL_DEVICE_MAC — без обращения к Apple
  IPATOOL_DEVICE_MAC=00:03:93:12:34:56 HOME=/work/h3 ./build/ipatool --format json kbsync --dsid 1 --debug \
    2> /work/mac.txt | grep -q "\"success\":true"
  grep -q "device ID: OK (IPATOOL_DEVICE_MAC)" /work/mac.txt
  set +e
  IPATOOL_DEVICE_MAC=00:00:00:00:00:00 HOME=/work/h3 ./build/ipatool --format json kbsync --dsid 1 > /work/bad.txt; rc5=$?
  set -e
  test "$rc5" = 1; grep -q "\"error\":\"bad_device_mac\"" /work/bad.txt
  ldd build/ipatool | tee /work/ldd.txt
  if grep -v -E "linux-vdso|ld-linux|libc\.so|libm\.so|libstdc\+\+|libgcc_s|libunicorn\.so" /work/ldd.txt | grep -q .; then
    echo "ОШИБКА: в ldd несистемные библиотеки" >&2; exit 1
  fi
  cp build/ipatool /work/ipatool
  # Use upstream native Store authentication with the Appshelf stdin/storage adapter.
  . /work/auth/UPSTREAM
  git clone -q "$URL" /work/native-auth
  git -C /work/native-auth checkout -q "$COMMIT"
  mkdir -p /work/native-auth/cmd/appshelf-auth
  cp /work/auth/*.go /work/native-auth/cmd/appshelf-auth/
  (cd /work/native-auth && gofmt -w cmd/appshelf-auth/*.go && \
    timeout 600 go test -timeout 30s ./cmd/appshelf-auth && \
    timeout 600 go build -trimpath -ldflags="-s -w" -o /work/ipatool-auth ./cmd/appshelf-auth)
  /work/ipatool-auth --help >/dev/null
'
install -D -m 0755 "$WORK/ipatool" "$OUT"
install -D -m 0755 "$WORK/ipatool-auth" "$D/bin/ipatool-auth"
cat "$D/ipatool/auth/UPSTREAM" "$D"/ipatool/auth/*.go | sha256sum | cut -d' ' -f1 > "$D/bin/ipatool-auth.src-sha256"
# из каких UPSTREAM и патчей собран — deploy/install.sh сверяет с выложенными и просит пересборку
cat "$D/ipatool/UPSTREAM" "$D"/ipatool/patches/*.patch | sha256sum | cut -d' ' -f1 > "$OUT.src-sha256"
echo "OK: собран $OUT"
