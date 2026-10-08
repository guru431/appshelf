#!/bin/bash
# Сборка bin/ipatool: upstream ipatool-cpp + патчи appshelf (spec §5). Запуск на сервере:
#   sudo bash /var/_sh/appshelf/deploy/build-ipatool.sh
# Docker — только здесь: пользователь appshelf в группу docker не входит (это равносильно root).
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"
. "$D/ipatool/UPSTREAM"                       # URL, COMMIT
OUT="$D/bin/ipatool"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -r "$D/ipatool/patches" "$WORK/patches"
# --network host и machine-id хоста — как при проверке 2026-10-05: тот же MAC (GUID запросов) и machine-id
docker run --rm --network host -v /etc/machine-id:/etc/machine-id:ro -v "$WORK:/work" \
  -e URL="$URL" -e COMMIT="$COMMIT" debian:trixie bash -euo pipefail -c '
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    ca-certificates git cmake build-essential perl pkg-config file \
    libssl-dev libminizip-dev zlib1g-dev libunicorn-dev nlohmann-json3-dev libzstd-dev \
    libcurl4-openssl-dev >/dev/null   # только заголовки для find_package(CURL); линкуется curl, собранный из исходников
  git clone -q "$URL" /work/src
  cd /work/src
  git checkout -q "$COMMIT"
  for p in /work/patches/*.patch; do git apply "$p"; done
  # libcrypto.a Debian собран с zstd: статический libzstd — в самый конец строки линковки
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DSTATIC_BUILD=ON -DCMAKE_CXX_STANDARD_LIBRARIES="-l:libzstd.a" \
    > /work/cmake.log 2>&1 || { tail -n 40 /work/cmake.log; exit 1; }
  grep -q "SAP assets: embedding via objcopy" /work/cmake.log   # иначе бинарник ищет sap_assets/ в текущем каталоге
  cmake --build build -j"$(nproc)" > /work/build.log 2>&1 || {
    grep -E "error|undefined reference" /work/build.log | sed "s/.*undefined reference to/undefined:/" | sort -u | head -n 60
    exit 1; }
  strip build/ipatool
  # проверки без обращения к Apple
  ./build/ipatool help > /dev/null
  HOME=/work/h1 ./build/ipatool --format json kbsync --dsid 1 | grep -q "\"success\":true"
  # коды выхода патча 04 — без обращения к Apple: учётки нет
  set +e
  HOME=/work/h2 ./build/ipatool --format json list-purchases > /work/lp.txt; rc1=$?
  HOME=/work/h2 ./build/ipatool --format json download -i 1 -o /work > /work/dl.txt; rc2=$?
  printf "pw\n" | HOME=/work/h2 ./build/ipatool --format json auth login --password-stdin > /work/li.txt; rc3=$?
  set -e
  test "$rc1" = 3; grep -q "\"error\":\"not_logged_in\"" /work/lp.txt
  test "$rc2" = 3; grep -q "\"error\":\"not_logged_in\"" /work/dl.txt
  test "$rc3" = 1; grep -q "\"error\":\"usage\"" /work/li.txt
  # патч 05: MAC из IPATOOL_DEVICE_MAC — без обращения к Apple
  IPATOOL_DEVICE_MAC=00:03:93:12:34:56 HOME=/work/h3 ./build/ipatool --format json kbsync --dsid 1 --debug \
    2> /work/mac.txt | grep -q "\"success\":true"
  grep -q "device ID: OK (IPATOOL_DEVICE_MAC)" /work/mac.txt
  set +e
  IPATOOL_DEVICE_MAC=00:00:00:00:00:00 HOME=/work/h3 ./build/ipatool --format json kbsync --dsid 1 > /work/bad.txt; rc4=$?
  set -e
  test "$rc4" = 1; grep -q "\"error\":\"bad_device_mac\"" /work/bad.txt
  ldd build/ipatool | tee /work/ldd.txt
  if grep -v -E "linux-vdso|ld-linux|libc\.so|libm\.so|libstdc\+\+|libgcc_s|libunicorn\.so" /work/ldd.txt | grep -q .; then
    echo "ОШИБКА: в ldd несистемные библиотеки" >&2; exit 1
  fi
  cp build/ipatool /work/ipatool
'
install -o appshelf -g appshelf -m 0755 "$WORK/ipatool" "$OUT"
if ldd "$OUT" | grep -q "not found"; then
  echo "ОШИБКА: на хосте не хватает библиотек (обычно: sudo apt-get install -y libunicorn2t64):" >&2
  ldd "$OUT" | grep "not found" >&2
  exit 1
fi
"$OUT" help > /dev/null
echo "OK: $OUT"
