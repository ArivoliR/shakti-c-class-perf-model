#!/usr/bin/env bash
# Fetch exactly the scalar sources selected by RISC-V Bionic immediately before
# Android removed its non-V path.  Downloads land in a tmp_-prefixed directory.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REVISION=${BIONIC_REVISION:-f971dc6b4ae58ba05450ce096c543b2bf4709912}
DEST=${1:-"$SCRIPT_DIR/tmp_bionic-src"}
BASE_URL="https://android.googlesource.com/platform/bionic/+/$REVISION"

files=(
  libc/upstream-freebsd/lib/libc/string/bcopy.c
  libc/upstream-freebsd/lib/libc/string/memcmp.c
  libc/upstream-freebsd/lib/libc/string/memcpy.c
  libc/upstream-freebsd/lib/libc/string/memmove.c
  libc/upstream-freebsd/lib/libc/string/memset.c
)

for relative in "${files[@]}"; do
  target="$DEST/$relative"
  mkdir -p "$(dirname -- "$target")"
  curl --fail --location --silent --show-error \
    "$BASE_URL/$relative?format=TEXT" | base64 --decode > "$target"
done
printf '%s\n' "$REVISION" > "$DEST/REVISION"
printf 'Fetched %d files from Bionic %s into %s\n' \
  "${#files[@]}" "$REVISION" "$DEST"
