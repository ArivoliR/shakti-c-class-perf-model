#!/usr/bin/env bash
# Logic depth of every block in the dual-issue core, from the generated Verilog.
#
# Answers "which block sets the clock, and how much headroom does execute have"
# without a PDK. `ltp -noff` reports the longest topological path between flops,
# so the numbers are register-to-register combinational depth in generic gates.
# They are comparable to each other because every block goes through the same
# flow; they are NOT picoseconds and do not survive translation to a real
# library unchanged.
set -u
V=${1:-$(dirname "$0")/../../../c-class-dual-issue/build/hw/verilog}
cd "$V" || { echo "no generated verilog at $V -- run 'make generate_verilog'"; exit 1; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

deps() { grep -ohE "^\s+mk[A-Za-z_0-9]+ " "$@" 2>/dev/null | tr -d ' ' | sort -u; }
srcfor() {                       # transitively collect local .v dependencies
  local out="$1.v" add
  for _ in 1 2 3 4; do
    add=$(deps $out 2>/dev/null | while read -r m; do [ -f "$m.v" ] && echo "$m.v"; done)
    out=$(echo "$out $add" | tr ' ' '\n' | sort -u | tr '\n' ' ')
  done
  echo "$out"
}

printf "%-26s %-24s %8s %9s\n" module role depth cells
for m in mkfpu_fm_add_sub64 mkfpu mkstage3 mkdcache mkfpu_fm_add_sub32 mkstage5 \
         mkbpu mkstage0 mkconvert mkstage2 mkicache mkscoreboard mkstage1 mkstage4; do
  [ -f "$m.v" ] || continue
  case $m in
    mkstage0) r="pc-gen";;            mkstage1) r="fetch";;
    mkstage2) r="decode/operand";;    mkstage3) r="EXECUTE";;
    mkstage4) r="memory";;            mkstage5) r="writeback";;
    mkbpu) r="branch predictor";;     mkdcache) r="D-cache";;
    mkicache) r="I-cache";;           mkscoreboard) r="scoreboard";;
    mkfpu) r="FPU (whole)";;          mkfpu_fm_add_sub64) r="FP add/mul, double";;
    mkfpu_fm_add_sub32) r="FP add/mul, single";; *) r="";;
  esac
  timeout 1500 yosys -p "read_verilog -sv $(srcfor $m); hierarchy -top $m; flatten
    proc; opt -fast; techmap; opt -fast
    abc -g AND,OR,XOR,MUX,NAND,NOR; opt_clean; stat; ltp -noff" > "$TMP/$m.log" 2>&1
  d=$(grep -ioP 'length=\K[0-9]+' "$TMP/$m.log" | tail -1)
  c=$(grep -oP '^\s+\K[0-9]+(?= cells)' "$TMP/$m.log" | tail -1)
  printf "%-26s %-24s %8s %9s\n" "$m" "$r" "${d:-FAIL}" "${c:--}"
done
