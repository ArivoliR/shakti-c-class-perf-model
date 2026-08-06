# SHAKTI C-Class Single-Issue Performance Model

This directory contains a Python timing model for the single-issue SHAKTI
C-Class core. The model consumes the dynamic committed instruction stream from
the RTL `rtldump` log and predicts commit cycles. It models performance, not
architectural behavior: it never computes register values.

## Files

- `trace.py`: parses legacy and cycle-stamped `rtl.dump` commit traces.
- `isa.py`: dependency-free RV64IMAFDC timing decoder.
- `model.py`: in-order six-stage timing model with ISB queues, scoreboard,
  bypass heads, FU latencies, fixed cache-hit latency, and branch predictor.
- `accuracy.py`: CVA6-style inter-commit spacing accuracy metric and mismatch
  grouping.
- `tests/`: unit tests for decode, trace parsing, and focused hazards.

## Regenerating A Cycle-Stamped Trace

The RTL instrumentation is intentionally confined to
`test_soc/c64_c32/TbSoc.bsv`. It prepends each commit line with:

```text
cycle <cycle_count> core   0: ...
```

After changing the testbench, rebuild the simulator:

```sh
PATH="$HOME/tools/bin:$HOME/tools/bsc/bin:$PATH" make generate_verilog
PATH="$HOME/tools/bin:$HOME/tools/bsc/bin:$PATH" make link_verilator generate_boot_files
```

Build and run a benchmark:

```sh
make -C benchmarks coremarks ITERATIONS=40
cd benchmarks/output
ln -sf ../../bin/* .
timeout 1800 ./out +rtldump
```

The simulator may time out after writing `app_log` and `rtl.dump`; read both
files after timeout. Do not commit generated traces, benchmark binaries, or
`code.mem`.

Long runs can split the commit trace every 10,000,000 instructions. Pass all
split files to the model in order:

```sh
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump ../benchmarks/output/rtl1.dump
```

## Running The Model

```sh
cd c-class/perf-model
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump
```

The command prints:

- committed instruction count
- model total cycles
- model IPC
- accuracy, when the trace has `cycle` stamps
- total-cycle delta against the stamped RTL trace
- grouped examples of spacing mismatches

For quick iteration:

```sh
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump --limit 100000
```

Current calibrated CoreMark result on the stamped 40-iteration run:

```text
accuracy: 99.233944%
model cycles: 14,854,777
RTL app_log cycles: 14,965,227
cycle delta: -110,450 (-0.738%)
```

Held-out Dhrystone result after CoreMark calibration:

```text
accuracy: 99.655836%
model cycles: 167,113
RTL app_log cycles: 167,827
cycle delta: -714 (-0.425%)
```

The main CoreMark accuracy jumps came from:

- decoding RV64 compressed `c.ld/c.sd/c.ldsp/c.sdsp` separately from floating
  `c.fld/c.fsd/c.fldsp/c.fsdsp`
- matching the BTB entry `hi` bit against the compressed halfword at prediction
  time
- reproducing the BSV `fn_hash` width behavior where `_h << shift` keeps
  `Bit#(histbits)` width before `zeroExtend`
- applying predictor training one model cycle after execute

## Accuracy Metric

For instruction `i`, the metric compares inter-commit spacing:

```text
delta_rtl(i) = rtl_cycle(i) - rtl_cycle(i-1)
delta_model(i) = model_cycle(i) - model_cycle(i-1)
accuracy = count(delta_model == delta_rtl) / number_of_compared_deltas
```

This avoids hiding a persistent offset after one early mistake.

## Microarchitecture Parameters

`Model.from_repo()` reads `../makefile.inc` and derives:

- ISB depths: `isb_s0s1`, `isb_s1s2`, `isb_s2s3`, `isb_s3s4`, `isb_s4s5`
- `MULSTAGES_TOTAL`, `DIVSTAGES`
- predictor sizing: `btbdepth`, `bhtdepth`, `histlen`, `histbits`, `rasdepth`
- `bypass_sources`, `wawid`
- `bpu` and `compressed`

The fixed hit latencies, flush penalties, and predictor timing switches are
constructor parameters:
`load_hit_latency`, `store_hit_latency`, `branch_mispredict_penalty`,
`wb_flush_penalty`, `csr_latency`, `predictor_train_delay`,
`match_btb_hi`, and `bsv_hash_truncate`.

## Current Limitations

The model does not capture:

- data-cache misses or cache line conflicts; load/store timing is a fixed
  cache-hit latency
- instruction-cache misses
- data-dependent divider early/late behavior; divider timing is fixed by
  `DIVSTAGES`
- traps or CSR effects that require data values
- full wrong-path instruction queue occupancy after a branch mispredict

These limits are deliberate for the first single-issue model and should be
revisited only when mismatch grouping shows they dominate the residual error.
