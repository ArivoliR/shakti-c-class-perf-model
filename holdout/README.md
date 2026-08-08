# Held-out design-point validation

The model is 99.2% cycle-accurate on CoreMark and 99.7% on Dhrystone. That
measures how well it reproduces **one** core. It is used for something else
entirely: predicting how much a *change* to the core would buy. Those are
different properties, and baseline accuracy is weak evidence for the second —
a model can match aggregate cycle counts while attributing the stalls to the
wrong mechanisms, and every proposed experiment perturbs exactly one mechanism.

This directory measures the property we actually depend on.

## The idea

`makefile.inc` `BSC_DEFINES` is a set of real, buildable design changes. Each
one gives ground truth for a *delta*, for the price of one RTL build. The model
is then asked to predict that delta with **no per-point tuning**, and the
scatter of predicted-vs-measured across the whole set is the model's error bar.

That error bar is the deliverable. Without it, "the second memory port buys
+0.758%" is a number with no defensible precision attached.

## The protocol, and the one rule that matters

For every design point:

1. Change exactly one knob in `BSC_DEFINES`.
2. Rebuild the RTL from clean and measure it. That is ground truth.
3. Build the model with `Model.from_makefile(<that variant's makefile.inc>)`
   and **nothing else**.

Rule 3 is the whole experiment. Every calibrated constant — `load_hit_latency`,
`wb_flush_penalty`, `branch_mispredict_penalty`, `predictor_train_delay`,
`match_btb_hi`, `bsv_hash_truncate` — keeps the value it was given during
CoreMark calibration. If a prediction only lands after re-fitting a constant
for that point, the model has not predicted anything, and the point must be
reported as a miss.

## Running it

```sh
cd c-class/perf-model/holdout

# 0. Pre-flight (~5 min, no RTL). Does the model even respond to each knob?
../../.venv/bin/python3 sensitivity.py ../../benchmarks/output/rtl.dump --limit 300000

# 1. See the plan
../../.venv/bin/python3 rtl_sweep.py --dry-run

# 2. Build and measure. Long; resumable; safe to Ctrl-C.
../../.venv/bin/python3 rtl_sweep.py

# 3. Predict, with no tuning
../../.venv/bin/python3 predict.py --benchmark dhrystone

# 4. Score the deltas and derive the error bar
../../.venv/bin/python3 score.py --benchmark dhrystone
```

Step 4 writes `runs/validation-dhrystone.{md,tex,json}`. The `.tex` is a table
fragment to `\input` straight into the report.

Subsets and resumption:

```sh
../../.venv/bin/python3 rtl_sweep.py --points byp1 btb8      # just these two
../../.venv/bin/python3 rtl_sweep.py                         # skips completed points
../../.venv/bin/python3 rtl_sweep.py --no-resume             # force rebuild
```

## Always run the pre-flight first

`sensitivity.py` exists because of a failure this project has already hit:
experiments D, F and G each returned a cycle count byte-identical to baseline
and were nearly reported as "no effect", when in fact the configuration flag
never reached any logic. An exact zero is almost never a physical result.

The pre-flight classifies each point as `OK`, `DEAD` (parameters change, cycles
do not) or `UNMAPPED` (the define changes no model parameter at all), and it
runs in minutes against a trace prefix. Never spend an RTL build on a point
that has not passed it.

## The design points

Chosen to (a) exercise the mechanisms the dual-issue experiments depend on and
(b) span a wide range of effect sizes, with deliberate density in the sub-2%
band where experiments A, B and C live. Predicted values below are from the
pre-flight on a 300k-instruction CoreMark prefix, before any RTL was built.

| point | knob | mechanism | model predicts |
|---|---|---|---:|
| `mul4` | `MULSTAGES 2/2/4` | FU latency | +5.4% |
| `btb8` | `btbdepth=8` | BTB capacity | +3.9% |
| `bht64` | `bhtdepth=64` | direction prediction | +2.7% |
| `btb16` | `btbdepth=16` | BTB capacity, half the perturbation | +1.5% |
| `bht128` | `bhtdepth=128` | direction prediction, mild | +0.8% |
| `s3s4_1` | `isb_s3s4=1` | guarded FIFO backpressure | +78.6% |
| `s0s1_1` | `isb_s0s1=1` | guarded FIFO backpressure | +73.5% |
| `s1s2_1` | `isb_s1s2=1` | guarded FIFO backpressure | +75.0% |
| `s4s5_1` | `isb_s4s5=1` | guarded FIFO backpressure | +72.4% |
| `div16` | `DIVSTAGES=16` | FU latency | −0.005% |

`byp1` matters most for Experiment E (+6.1%, intra-bundle forwarding), which is
the most credible dual-issue result and rests entirely on the bypass mechanism.
`bht128` and `btb16` matter most for A, B and C, because they sit at the same
magnitude as those claims and therefore set the error bar that decides whether
those claims can be quoted as numbers at all.

`btb8`/`btb16` and `bht64`/`bht128` are deliberately paired: the same mechanism
at two perturbation sizes, which tests linearity as well as accuracy.

## Findings from the backpressure round

**The model now has guarded FIFO backpressure.** The missing mechanism was not
capacity alone; it was FIFO scheduling. `mkSizedFIFOF` lowers to the guarded
`SizedFIFO` primitive, where `FULL_N = not_ring_full`, so a producer does not
see space created by a same-cycle dequeue. `mkLFIFOF` remains loopy. The model
now records cycle-start occupancy and applies those two policies separately.

This closed the large aggregate blind spots. On Dhrystone, `s3s4_1` is now
322,593 model cycles versus 321,804 RTL, `s0s1_1` is 291,602 versus 294,842,
`s1s2_1` is 320,099 versus 324,833, and `s4s5_1` is 319,045 versus 319,196.
On CoreMark, the same large depth-1 slowdowns are predicted within a few
percent. The remaining caveat is local cadence: Dhrystone Δt for `s0s1_1` and
`s1s2_1` is still only about 67-68%, even though the aggregate deltas are close.

**`isb_s2s3` is inert in the single-issue RTL.** The source path uses
`mkLFIFOF()` for stage2→stage3 rather than `mkSizedFIFOF(isb_s2s3)`, so
`s2s3_2` and `s2s3_4` are intentionally neutral in both RTL and model.

**`enable_bpu=False` did not mean what the sweep needed it to mean.** It was a
diagnostic that suppressed all modelled control stalls — with it set, the model
reported *zero* mispredicts, so removing the branch predictor made the modelled
core 5.4% *faster*. A core actually built without `bpu` has no BTB, BHT or RAS,
walks sequentially, and eats a redirect on every taken control transfer.
`BranchPredictor.static_not_taken_when_disabled` (default on) now models that;
the `--no-bpu` CLI flag keeps the old diagnostic behaviour. With the fix,
`nobpu` predicts +20.7%, which is the right sign and a plausible magnitude.

Note the RTL's next-PC stall at `stage3.bsv:726` is itself `ifdef bpu`-gated,
so the model dropping that stall when the BPU is absent is faithful.

## What is excluded, and why

**`bypass_sources` is not a parameter, so the bypass path has no design point.**
`riscv.bsv:245-246` assigns exactly `lv_bypass[0]` and `lv_bypass[1]`, and
`bypass.bsv:42-46` hardcodes `choice[1:0]` and a three-way case ending in
`fwd[2]`, whatever the define says. `bypass_sources=1` fails elaboration with
an out-of-range bit extraction; `=3` would leave `lv_bypass[2]` unassigned. The
value must be exactly 2.

This is the sweep's most consequential negative result. The model responds
strongly to the knob (+16.6% predicted for a single source), so it will produce
a confident number for any forwarding change — and there is no core that can
contradict it. Experiment E (+6.115%, intra-bundle ALU forwarding) is the most
credible dual-issue result *and* the one whose mechanism cannot be validated
this way. It has to be reported as carrying no held-out validation. Recorded in
`design_points.UNBUILDABLE`.

**`no_wawstalls` removed.** The model has no WAW-stall mechanism at all —
`wawid` is mapped only to a rename-tag width, and dropping the define changes
no model parameter. Building it would produce a ground-truth delta with nothing
to compare against. Recorded in `design_points.UNMODELLABLE`.

**`bpu` and `bpu_ras` removal are not in the default sweep.** Removing
`bpu_ras` unbalances predictor control flow in `gshare_fa.bsv`. Removing `bpu`
hits conditional-compilation rot around `Stage3Meta.compressed`. These remain
useful possible RTL cleanup tasks, but they are not treated as routine
hold-out design points.

## The one RTL source change

`src/ccore_types.bsv` declares `Stage3Meta.compressed` under `` `ifdef bpu ``
plus `` `ifdef compressed ``, but its `FShow` instance guarded the same field on
`` `ifdef compressed `` alone. Any core built without a branch predictor
therefore failed to elaborate on a debug format string. The guard now matches
the struct.

This is a conditional-compilation fix to a display function, not a datapath
change: for every `bpu`-enabled configuration it compiles to exactly what it did
before, which is why the baseline point still reproduces the reference cycle
counts to the cycle. The no-BPU datapath itself still has additional upstream
conditional-compilation issues, so it is excluded from the default sweep.

## Benchmarks

Dhrystone (500 iterations) is the primary sweep workload: the RTL run is
seconds, the trace is ~10 MB, so every point gets a full cycle-stamped trace and
therefore its own Δt accuracy figure alongside the cycle delta. CoreMark (40
iterations) runs per point too, but `app_log` only — a per-point CoreMark trace
is ~1 GB.

Using Dhrystone as the primary is deliberate: it is the *held-out* benchmark for
the original calibration, so design-point errors measured on it are not
flattered by having been tuned on the same workload.

To score against CoreMark instead, pass the baseline CoreMark trace explicitly,
since per-point CoreMark traces are not archived:

```sh
../../.venv/bin/python3 predict.py --benchmark coremark \
    --coremark-trace runs/.baseline/coremark-trace/rtl.dump \
                     runs/.baseline/coremark-trace/rtl1.dump
../../.venv/bin/python3 score.py --benchmark coremark
```

## Reading the output

`score.py` reports three numbers, answering different questions:

- **bias** — mean signed error. A systematic lean; correctable by construction.
- **scatter** — RMS error after removing bias. Not correctable. This sets the
  resolution floor.
- **slope** — regression of predicted on measured. Slope < 1 means the model
  systematically *understates* how much changes are worth, which is precisely
  the failure mode that matters when pricing an optimisation. For reference,
  the single→dual-issue point already shows this: the model predicts a 1.4008×
  speedup against a measured 1.4339×, understating the cycles saved by 5.8%.

The reported **resolution floor is 2 × scatter**, given both overall and
restricted to the small-effect points. Use the small-effect floor for small
claims — an error bar derived from a +20% design change does not transfer to a
+1% one. Any predicted effect below the floor is reported as *below model
resolution*, never as a number.

## Safety and caveats

- `rtl_sweep.py` saves `makefile.inc` and `bin/out` before touching anything and
  restores `makefile.inc` on exit, including on Ctrl-C. The baseline simulator
  is kept at `runs/.baseline/out.orig`; `build/` and `bin/out` are left holding
  whichever point was built last. Rebuild the baseline with
  `rtl_sweep.py --points baseline`.
- Any pre-existing `benchmarks/output/rtl*.dump` is **moved** to
  `runs/.baseline/coremark-trace/` on first run, because each benchmark run
  clears that directory and the CoreMark trace is expensive to regenerate.
- Every point forces a clean bsc elaboration. `bsc` caches `.bo` files by source
  hash, not by `-D` defines, so without this a variant silently reuses the
  baseline's elaborated modules and is not actually the design point it claims
  to be. This project has been bitten by it before.
- Every variant makefile gets `-Wno-MULTIDRIVEN` added to `VERILATOR_FLAGS`.
  The core builds with `-DBSV_ASYNC_RESET`, which makes the BSV library FIFO
  primitives drive state from both a clocked and an async-reset block;
  Verilator 5.050 calls that MULTIDRIVEN and escalates it to an error, so a
  clean build of *any* configuration fails without it — the unmodified baseline
  included. It is a lint suppression with no semantic effect, applied
  identically to every point, and the baseline point re-measuring the reference
  cycle counts is the check that it changed nothing.
- The simulator never self-terminates: the testbench only calls `$finish` on a
  `j .` self-loop, which the benchmarks do not reach. Rather than burn the full
  timeout twice per point, the sweep polls `app_log` for each benchmark's final
  line and stops the simulator a few seconds later. `result.json` records
  `stopped_by` as `marker`, `exited` or `timeout`; a `timeout` means the
  benchmark never printed its result and that point's numbers are suspect.
- `makefile.inc` is generated by `soc_config`. Do not re-run `soc_config` while
  a sweep is in flight; it will clobber the variant defines.
- Build failures are recorded and the sweep continues. Check
  `runs/<point>/build-*.log`.
- Traces under `runs/` are gitignored.

## What this does not establish

The design points here are all microarchitectural knobs on the single-issue
core. They give an error bar for predictions *of that kind*. They say nothing
about:

- **Cache behaviour.** The model has no data cache; load/store timing is a fixed
  hit latency. Neither benchmark misses meaningfully, so no point here perturbs
  memory. A memory-port claim validated only on these workloads is validated on
  workloads where memory does not bind.
- **Front-end wrong-path effects.** The model consumes the committed stream, so
  it never sees wrong-path fetch consuming bandwidth. Experiments F and G touch
  the front end.
- **Issue width.** Single→dual-issue is a structural change of a different
  character. It is a genuine held-out point and should be scored the same way
  once a cycle-stamped dual trace exists, but it is not covered by this sweep.
