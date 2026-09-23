#!/usr/bin/env bash
# Generate a perf-model trace from any riscv64-linux binary, via Spike + pk.
#
# The model's parser (perf-model/trace.py, TRACE_RE) makes the "cycle N" prefix
# optional, so Spike's --log-commits output is consumed as-is. No conversion.
#
# Prerequisites:
#   spike   -- /home/blazevfx/Documents/projects/cva6/tools/spike/bin/spike
#   pk      -- build with:
#       git clone --depth 1 https://github.com/riscv-software-src/riscv-pk
#       cd riscv-pk && mkdir build && cd build
#       ../configure --prefix=$PREFIX --host=riscv64-linux-gnu \
#                    --with-arch=rv64imafdc_zifencei --with-abi=lp64d
#       make -j8 && make install
#     NOTE: --with-arch MUST include _zifencei, else mentry.S fails on `fence.i`
#           ("unrecognized opcode ... extension `zifencei' required").
set -eu
SPIKE=${SPIKE:-/home/blazevfx/Documents/projects/cva6/tools/spike/bin/spike}
PK=${PK:-/home/blazevfx/tracework/install/riscv64-linux-gnu/bin/pk}
BIN=${1:?usage: gen_trace.sh <riscv64-linux-binary> [out.log]}
OUT=${2:-$(basename "$BIN").log}

# The commit log goes to STDERR, not stdout. stdout is the program's own output.
"$SPIKE" --isa=rv64imafdc_zifencei --log-commits "$PK" "$BIN" 2> "$OUT" > "${OUT%.log}.out"
echo "$OUT: $(wc -l < "$OUT") lines, $(grep -c ' mem 0x' "$OUT") memory refs"
