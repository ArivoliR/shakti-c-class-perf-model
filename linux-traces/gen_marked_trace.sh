#!/usr/bin/env bash
# Run a marker-instrumented riscv64-linux binary and retain its workload only.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SPIKE=${SPIKE:-"$SCRIPT_DIR/tmp_official-spike-release/bin/spike"}
PK=${PK:-/home/blazevfx/tracework/install/riscv64-linux-gnu/bin/pk}
BIN=${1:?usage: gen_marked_trace.sh BINARY OUT.log [program arguments...]}
OUT=${2:?usage: gen_marked_trace.sh BINARY OUT.log [program arguments...]}
shift 2
PROGRAM_OUT=${PROGRAM_OUT:-"${OUT%.log}.out"}
TRACE_TAIL=${TRACE_TAIL:-0}

[[ -x "$SPIKE" ]] || { printf 'missing Spike: %s\n' "$SPIKE" >&2; exit 2; }
[[ -x "$PK" ]] || { printf 'missing pk: %s\n' "$PK" >&2; exit 2; }
[[ -x "$BIN" ]] || { printf 'missing binary: %s\n' "$BIN" >&2; exit 2; }

# Commit logging is stderr.  This redirection order sends it through the
# parser/filter while leaving the target program's stdout in PROGRAM_OUT.
filter_args=(--start 0x12345037 --end 0x54321037)
if (( TRACE_TAIL > 0 )); then
  filter_args+=(--tail "$TRACE_TAIL")
fi

"$SPIKE" --isa=rv64imafdc_zifencei --log-commits "$PK" "$BIN" "$@" \
  2>&1 > "$PROGRAM_OUT" | \
  python3 "$SCRIPT_DIR/filter_trace.py" "${filter_args[@]}" > "$OUT"

instructions=$(wc -l < "$OUT")
printf '%s: %s parser-ready instructions\n' "$OUT" "$instructions"
printf 'program output: %s\n' "$PROGRAM_OUT"
