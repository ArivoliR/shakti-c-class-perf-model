#!/usr/bin/env bash
# Generate a compact, parser-ready trace containing only the marked workload.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BIN=${1:-"$SCRIPT_DIR/tmp_android_bionic_proxy"}
OUT=${2:-"$SCRIPT_DIR/tmp_android_bionic_proxy.log"}
[[ -x "$BIN" ]] || "$SCRIPT_DIR/build_android_proxy.sh"
exec "$SCRIPT_DIR/gen_marked_trace.sh" "$BIN" "$OUT"
