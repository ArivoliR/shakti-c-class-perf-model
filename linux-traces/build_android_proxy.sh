#!/usr/bin/env bash
# Build the non-vector Bionic subset and its cache-stressing driver for pk.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=${BIONIC_SOURCE_ROOT:-"$SCRIPT_DIR/tmp_bionic-src"}
FREEBSD="$SOURCE_ROOT/libc/upstream-freebsd/lib/libc/string"
BUILD_DIR=${BUILD_DIR:-"$SCRIPT_DIR/tmp_bionic-build"}
OUTPUT=${OUTPUT:-"$SCRIPT_DIR/tmp_android_bionic_proxy"}
CC=${CC:-riscv64-linux-gnu-gcc}

if [[ ! -f "$FREEBSD/memcpy.c" ]]; then
  "$SCRIPT_DIR/fetch_bionic_subset.sh" "$SOURCE_ROOT"
fi

mkdir -p "$BUILD_DIR"
common_flags=(
  -O2 -fno-builtin -fno-tree-loop-distribute-patterns
  -march=rv64imafdc_zifencei -mabi=lp64d
  -include stdint.h
  '-D__FBSDID(x)='
)

"$CC" "${common_flags[@]}" -Dmemcpy=bionic_memcpy \
  -c "$FREEBSD/memcpy.c" -o "$BUILD_DIR/memcpy.o"
"$CC" "${common_flags[@]}" -Dmemmove=bionic_memmove \
  -c "$FREEBSD/memmove.c" -o "$BUILD_DIR/memmove.o"
"$CC" "${common_flags[@]}" -Dmemset=bionic_memset \
  -c "$FREEBSD/memset.c" -o "$BUILD_DIR/memset.o"
"$CC" "${common_flags[@]}" -Dmemcmp=bionic_memcmp \
  -c "$FREEBSD/memcmp.c" -o "$BUILD_DIR/memcmp.o"

"$CC" "${common_flags[@]}" -static \
  ${EXTRA_CFLAGS:-} "$SCRIPT_DIR/android_bionic_proxy.c" \
  "$BUILD_DIR/memcpy.o" "$BUILD_DIR/memmove.o" \
  "$BUILD_DIR/memset.o" "$BUILD_DIR/memcmp.o" \
  -o "$OUTPUT"

printf 'Built %s from Bionic %s\n' "$OUTPUT" "$(<"$SOURCE_ROOT/REVISION")"
"$CC" -dumpmachine
file "$OUTPUT"
