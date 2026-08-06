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
- `trace_cache.py`: compact parsed-trace cache for large benchmark dumps.
- `docs/perf_model_notes.tex`: LaTeX notes explaining the model construction.
- `docs/perf_model_notes.pdf`: generated PDF copy of those notes.
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

For architectural prediction where no matching RTL trace exists, skip the
accuracy comparison:

```sh
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump ../benchmarks/output/rtl1.dump \
  --dual-issue --predict-only --show-discrepancies 0
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

No-RAS apples-to-apples experiment, with `bpu_ras` removed from RTL and the
model deriving `rasdepth=0` from `makefile.inc`:

```text
CoreMark accuracy: 99.209439%
model cycles: 14,862,816
RTL app_log cycles: 14,965,227
cycle delta: -102,411 (-0.684%)
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
- `bpu`, `bpu_ras`, and `compressed`

The fixed hit latencies, flush penalties, and predictor timing switches are
constructor parameters:
`load_hit_latency`, `store_hit_latency`, `branch_mispredict_penalty`,
`wb_flush_penalty`, `csr_latency`, `predictor_train_delay`,
`match_btb_hi`, and `bsv_hash_truncate`.

## Dual-Issue Prediction Mode

`--dual-issue` models the actual SHAKTI dual-issue branch structure, not an
ideal independent-lane machine. It uses the extracted dual-issue configuration:

- `num_issue=2`
- 64-bit fetch path, modeled as up to two committed instructions entering the
  front end per cycle
- `instr_queue=6` for the stage1-to-stage2 MIMO queue; this overrides the
  misleading `isb_s1s2=2` define on that path
- vector bundle queues represented internally as scalar entries:
  `s2->s3=2`, `s3->s4=16`, `s4->s5=16`
- one decode bundle, one execute bundle, one stage4 bundle, and one commit
  bundle per cycle
- two ALUs, one shared branch/control unit, one memory issue path, one mul/div,
  and one FPU
- no intra-bundle forwarding
- WAW/WAR pairs allowed because `no_wawstalls` is defined
- MEM+MEM disabled because `dual_mem` is not in `makefile.inc` and the slot-1
  memory path is not wired to the D-cache

The pairing whitelist is taken from `stage2.bsv`: ALU can pair with ALU,
MULDIV, FLOAT, MEMORY, or CONTROL; CONTROL can pair with MULDIV, FLOAT, or
MEMORY; all other non-`dual_mem` scarce-FU pairs are single-issued. Intra-bundle
RAW from instruction 0 to instruction 1 blocks pairing; WAW and WAR do not.

Paired instructions are handled as lockstep bundles after decode. If either
slot's operands, functional unit, or stage4 result are not ready, neither slot
advances. Stage5 commits paired instructions atomically in the same model cycle.
Bypass is modeled as two downstream sources with two slots per source; the
slot-1 producer in a bundle is visible to dependent instructions, matching
`bypass.bsv`.

Run the actual dual-issue prediction:

```sh
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump ../benchmarks/output/rtl1.dump \
  --dual-issue --predict-only
```

For comparison with the earlier independent-lane experiment:

```sh
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump ../benchmarks/output/rtl1.dump \
  --generic-dual-issue --predict-only
```

Exploratory knobs still exist, but they are not part of the current compiled
dual branch unless the RTL is changed:

```sh
# Enable the gated pair rule for load/store memory pairs.
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump ../benchmarks/output/rtl1.dump \
  --dual-issue --dual-mem --memory-issue-width 2 --predict-only

# Let paired slots leave execute/stage4 independently while preserving atomic commit.
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump ../benchmarks/output/rtl1.dump \
  --dual-issue --decouple-lockstep --predict-only

# Explore branch+branch pairing with two control issue slots.
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump ../benchmarks/output/rtl1.dump \
  --dual-issue --allow-branch-branch --control-issue-width 2 --predict-only
```

Because the input trace is still the single-issue committed instruction stream,
dual-issue outputs are predicted cycle counts and IPC, not cycle-accuracy
validation results.

Current CoreMark dual prediction using the single-issue trace:

```text
Baseline actual-policy dual model:
  cycles: 10,604,215
  IPC: 1.203705
  paired instructions: 76.227%

Second memory path only (--dual-mem --memory-issue-width 2):
  cycles: 10,798,473
  IPC: 1.182051
  result vs baseline: -1.8% IPC
```

The saved dual-issue RTL `app_log` reports 10,398,758 cycles for 12,771,408
instructions, or IPC 1.228. This is close to the model baseline, but not a strict
validation because the saved dual binary differs from the single-issue trace
binary. A cycle-stamped dual RTL trace is required for a real dual accuracy
number.

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
