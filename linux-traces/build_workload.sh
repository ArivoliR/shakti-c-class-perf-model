#!/usr/bin/env bash
# Compile a workload for riscv64-linux. No vector: standard riscv64 distros
# target rv64gc, so nothing here emits instructions the model cannot decode.
set -eu
riscv64-linux-gnu-gcc -O2 -static -march=rv64imafdc -mabi=lp64d "$@"
