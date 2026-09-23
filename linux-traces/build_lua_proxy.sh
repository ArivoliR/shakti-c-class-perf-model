#!/usr/bin/env bash
# Cross-build an embedded, marker-instrumented Lua 5.4.6 proxy for Spike + pk.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VERSION=5.4.6
ARCHIVE=${LUA_ARCHIVE:-"$SCRIPT_DIR/tmp_lua-$VERSION.tar.gz"}
SOURCE_DIR=${LUA_SOURCE_DIR:-"$SCRIPT_DIR/tmp_lua-$VERSION"}
OUTPUT=${OUTPUT:-"$SCRIPT_DIR/tmp_android_lua_proxy"}
CC=${CC:-riscv64-linux-gnu-gcc}

if [[ ! -f "$SOURCE_DIR/src/lua.h" ]]; then
  if [[ ! -f "$ARCHIVE" ]]; then
    curl --fail --location --silent --show-error \
      "https://www.lua.org/ftp/lua-$VERSION.tar.gz" -o "$ARCHIVE"
  fi
  extract_root="$SCRIPT_DIR/tmp_lua-extract-$VERSION"
  mkdir -p "$extract_root"
  tar -xzf "$ARCHIVE" -C "$extract_root"
  mv "$extract_root/lua-$VERSION" "$SOURCE_DIR"
  rmdir "$extract_root"
fi

make -C "$SOURCE_DIR/src" -j"${JOBS:-$(nproc)}" \
  CC="$CC" AR="riscv64-linux-gnu-ar rcu" RANLIB=riscv64-linux-gnu-ranlib \
  MYCFLAGS='-O2 -march=rv64imafdc_zifencei -mabi=lp64d -fno-builtin' \
  liblua.a

"$CC" -O2 -static -march=rv64imafdc_zifencei -mabi=lp64d \
  -I"$SOURCE_DIR/src" "$SCRIPT_DIR/lua_proxy_driver.c" \
  "$SOURCE_DIR/src/liblua.a" -lm -ldl -o "$OUTPUT"
printf 'Built %s with Lua %s\n' "$OUTPUT" "$VERSION"
file "$OUTPUT"
