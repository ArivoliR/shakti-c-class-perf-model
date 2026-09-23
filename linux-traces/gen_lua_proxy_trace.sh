#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BIN=${1:-"$SCRIPT_DIR/tmp_android_lua_proxy"}
OUT=${2:-"$SCRIPT_DIR/tmp_android_lua_proxy.log"}
[[ -x "$BIN" ]] || "$SCRIPT_DIR/build_lua_proxy.sh"
export TRACE_TAIL=${TRACE_TAIL:-250000}
exec "$SCRIPT_DIR/gen_marked_trace.sh" \
  "$BIN" "$OUT" "$SCRIPT_DIR/android_memory_proxy.lua"
