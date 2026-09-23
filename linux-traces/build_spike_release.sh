#!/usr/bin/env bash
# Build a pinned, optimized upstream Spike with runtime commit logging.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REVISION=${SPIKE_REVISION:-4ffd6ba860f4190ceac2716fa3c2cf139e85538f}
SOURCE_DIR=${SPIKE_SOURCE_DIR:-"$SCRIPT_DIR/tmp_official-spike-src"}
BUILD_DIR=${SPIKE_BUILD_DIR:-"$SCRIPT_DIR/tmp_official-spike-build"}
PREFIX=${SPIKE_PREFIX:-"$SCRIPT_DIR/tmp_official-spike-release"}
JOBS=${JOBS:-$(nproc)}

if [[ ! -x "$SOURCE_DIR/configure" ]]; then
  mkdir -p "$SOURCE_DIR"
  git -C "$SOURCE_DIR" init
  git -C "$SOURCE_DIR" remote add origin \
    https://github.com/riscv-software-src/riscv-isa-sim
  git -C "$SOURCE_DIR" fetch --depth 1 origin "$REVISION"
  git -C "$SOURCE_DIR" checkout --detach FETCH_HEAD
fi

actual_revision=$(git -C "$SOURCE_DIR" rev-parse HEAD)
if [[ "$actual_revision" != "$REVISION" ]]; then
  printf 'Spike source is %s, expected %s\n' "$actual_revision" "$REVISION" >&2
  exit 2
fi

mkdir -p "$BUILD_DIR" "$PREFIX/bin"
if [[ ! -f "$BUILD_DIR/Makefile" ]]; then
  commitlog_option=()
  if "$SOURCE_DIR/configure" --help | grep -q -- '--enable-commitlog'; then
    commitlog_option+=(--enable-commitlog)
  fi
  (
    cd "$BUILD_DIR"
    "$SOURCE_DIR/configure" --prefix="$PREFIX" \
      "${commitlog_option[@]}" \
      CFLAGS='-O3 -DNDEBUG -march=native' \
      CXXFLAGS='-O3 -DNDEBUG -march=native'
  )
fi

make -C "$BUILD_DIR" -j"$JOBS" spike
install -m 0755 "$BUILD_DIR/spike" "$PREFIX/bin/spike"
strip --strip-unneeded "$PREFIX/bin/spike"
"$PREFIX/bin/spike" --help 2>&1 | grep -- '--log-commits'
printf 'Built Spike %s: %s\n' "$REVISION" "$PREFIX/bin/spike"
file "$PREFIX/bin/spike"
