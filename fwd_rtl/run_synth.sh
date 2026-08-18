#!/usr/bin/env bash
# Compare the combinational depth of the two ALU-pair variants.
#
# Needs yosys:  sudo pacman -S yosys
#
# Reports longest topological path (ltp) and cell count for each. The RATIO of
# the two ltp figures is the number that matters -- it says how much deeper the
# forwarding path is than today's. Absolute picoseconds would need a liberty
# file and still would not be comparable to the commercial flow the thesis used.
set -u
cd "$(dirname "$0")"
command -v yosys >/dev/null || { echo "yosys not found -- sudo pacman -S yosys"; exit 1; }

for top in alu_pair_baseline alu_pair_forward; do
  echo "=============== $top ==============="
  yosys -p "
    read_verilog -sv alu_fwd_path.sv
    hierarchy -top $top
    proc; opt; fsm; opt; memory; opt
    techmap; opt
    abc -g AND,OR,XOR,MUX,NAND,NOR
    opt_clean
    stat
    ltp -noff
  " 2>&1 | grep -E "Longest topological|Number of cells|Number of wires|^\s+\\\$?_?[A-Z]+_?\s" | head -20
  echo
done
