# Tiny Stage2 Scheduler Timing Artifact

This directory contains a standalone RTL timing cone for the proposed
coupling-aware scheduler. It is not wired into the SHAKTI core. The purpose is
to answer the first go/no-go question before touching the full Bluespec RTL:
does a 4-entry or 8-entry selection window make the issue path too slow?

## Interface

`tiny_stage2_scheduler.sv` takes a decoded instruction window and produces up to
two selected indexes:

- per-entry valid and issued bits
- three source register tags plus valid bits
- one destination register tag plus valid bit
- FU class and memory/control/system flags
- scoreboard-ready bits
- FU-ready bits
- downstream-ready bit

The selector is conservative. A younger entry may bypass older unissued entries
only when it has no RAW/WAR/WAW conflict with them and does not cross a
side-effect barrier. Memory operations do not pass older memory operations.

## Sweep

```sh
cd c-class/perf-model/scheduler_rtl
./sweep_scheduler.py --windows 2,4,8
```

With Verilator installed this lints the generated window-specific tops. If
Yosys is available, it also emits generic cell counts. With a standard-cell
library:

```sh
./sweep_scheduler.py --windows 2,4,8 --liberty /path/to/cell.lib
```

The JSON output is `scheduler_sweep.json`. Treat FPGA or generic-cell numbers
as screening evidence only; the decision point is ASIC Fmax and area for the
target library.
