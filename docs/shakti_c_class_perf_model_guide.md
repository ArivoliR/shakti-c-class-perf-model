# SHAKTI C-Class Performance Model Guide

This document is the compact "everything you need to know" guide for the
SHAKTI C-Class performance-model work. It covers:

- what the SHAKTI C-Class single-issue core looks like;
- what changed in the dual-issue core;
- what the CVA6 team did with their performance model;
- what we built for SHAKTI;
- what accuracy means here;
- what we learned;
- what the model is useful for;
- what it cannot prove yet.

The short version:

> The model is useful because it lets us test architectural ideas before
> spending months in Bluespec RTL. But the useful question is not "does the
> model match one baseline run?" The useful question is "does the model predict
> the delta caused by a real design change?" That is why the holdout RTL sweep
> and error bars matter.

## 1. The Big Picture

### What We Are Trying To Do

We want to improve SHAKTI C-Class performance.

The single-issue C-Class is the baseline. The dual-issue C-Class is the first
superscalar attempt. The long-term question is:

> Which microarchitectural changes are worth implementing in RTL?

Candidate changes include:

- second memory port;
- intra-bundle forwarding;
- wider issue;
- better pairing logic;
- decoupled issue/execute/retire;
- branch-pair support;
- larger or smaller pipeline buffers;
- changed predictor sizes;
- changed functional-unit latencies.

Building these in RTL is expensive. A small Python timing model is cheap. So we
use the model to estimate which ideas are worth the RTL effort.

### What A Performance Model Is

A performance model predicts timing.

It is not an instruction-set simulator. It does not compute register values. It
does not check whether the program gets the right answer. It takes a trace of
instructions that the real RTL already committed, and predicts when those same
instructions should commit.

That is the core trick:

1. Run the benchmark on RTL.
2. Record the committed dynamic instruction stream.
3. Feed that committed stream to the model.
4. The model predicts cycles, stalls, hazards, and IPC.

This makes the model much smaller and faster than RTL simulation.

### Why This Is Useful

The model helps answer questions like:

- If we add a second memory port, how much IPC do we gain?
- If we allow intra-bundle ALU forwarding, how many RAW bubbles disappear?
- If we go quad issue without a lookahead window, is it worth it?
- If an ISB depth is reduced, does performance actually change?
- Which bottleneck dominates after the previous bottleneck is removed?

The model is useful when it can localize cause and effect. It is not useful if
it only matches total cycles by accident.

## 2. What The CVA6 Team Did

The reference methodology is from Allart et al., "Using a Performance Model to
Implement a Superscalar CVA6" (ACM CF Workshops 2024, arXiv:2410.01442).

The CVA6 team wanted to explore superscalar changes before committing to RTL.
They built a small performance model of the parts of the pipeline where
architectural decisions mattered.

Important ideas from their approach:

### Model Performance, Not Behavior

They did not build a full functional simulator. They consumed a committed
instruction trace and modeled timing.

That means:

- no need to execute ALU operations;
- no need to calculate load data;
- no need to emulate the entire architectural state;
- no need to simulate wrong program paths functionally.

The trace says what instruction actually committed next. The model only asks:

> How many cycles should have passed before this commit?

### Model Only The Important Stages

CVA6 did not model every wire. They modeled the stages where design choices
live: issue, execute, and commit.

The lesson is not "copy the CVA6 pipeline." The lesson is:

> Keep the model small, but include the mechanisms that create bubbles.

For SHAKTI C-Class, the important mechanisms are different. C-Class is in-order,
backpressure-driven, and has explicit pipeline ISBs, so our model includes more
front-end and queue behavior than the CVA6 issue/execute/commit-only sketch.

### Run Stages In Reverse Order

One software model cycle runs stage functions in reverse pipeline order.

For example:

```text
commit
stage4
execute
decode
fetch/decompress
fetch
```

This approximates synchronous hardware.

Why reverse order matters:

- in hardware, every stage sees state from the start of the clock edge;
- downstream stages can consume queue entries during the cycle;
- upstream stages may then see newly available space;
- a forward-order software loop would create false stalls.

This small detail matters a lot for cycle modeling.

### Call Issue And Commit In Loops

CVA6's model called `try_issue()` N times per cycle for an N-issue machine and
`try_commit()` M times for M commit ports.

Even if the current machine is single-issue, keeping those loops makes it easier
to extend the model later.

For SHAKTI, this mattered when extending the model from single-issue to
dual-issue.

### Model Three Hazard Classes

CVA6 explicitly modeled:

- data hazards;
- structural hazards;
- control hazards.

For SHAKTI, those become:

- RAW register dependencies;
- scoreboard locks;
- WAW/WAR behavior where relevant;
- functional-unit busy conditions;
- writeback/commit coupling;
- full inter-stage buffers;
- branch misprediction and redirect refill.

### Use Multiple Validation Levels

The CVA6 team used progressively deeper comparison:

1. Total cycles.
2. Cycle-annotated trace diff.
3. Internal event/counter comparison.

This is important because total cycles can lie. A model can be too slow on
loads, too fast on branches, and still land on the right final cycle count.

Our SHAKTI work follows the same principle:

- aggregate cycle count;
- inter-commit delta accuracy;
- per-counter/mechanism validation;
- held-out RTL design-point deltas.

## 3. SHAKTI C-Class Single-Issue Core

The SHAKTI C-Class single-issue core is not CVA6. It has its own structure and
must be modeled from its own RTL.

### High-Level Structure

The single-issue C-Class is:

- RV64IMAFDC;
- in-order;
- single issue;
- six-stage;
- Bluespec RTL;
- backpressure-driven through inter-stage buffers;
- scoreboard-based for register hazards;
- branch-predicted with gshare/BTB/BHT/RAS when enabled.

The six stages are:

| Stage | Role |
|---|---|
| Stage 0 | PC generation and branch prediction |
| Stage 1 | instruction fetch response and decompression |
| Stage 2 | decode and register read |
| Stage 3 | execute |
| Stage 4 | memory and late result routing |
| Stage 5 | writeback, CSR, trap, and commit |

The relevant RTL files are:

| File | Role |
|---|---|
| `src/stage0.bsv` | PC generation / predictor |
| `src/stage1.bsv` | fetch response / decompression |
| `src/stage2.bsv` | decode / register read |
| `src/stage3.bsv` | execute |
| `src/stage4.bsv` | memory / result routing |
| `src/stage5.bsv` | writeback / commit |
| `src/riscv.bsv` | pipeline wiring |
| `src/scoreboard.bsv` | register locks |
| `src/bypass.bsv` | bypass network |
| `test_soc/c64_c32/TbSoc.bsv` | testbench / commit log |

### In-Order Means Simple, But Not Trivial

"In-order" means instructions do not freely execute and retire out of program
order. But it does not mean every instruction takes one cycle.

Timing still depends on:

- full pipeline queues;
- load/store latency;
- multiply/divide latency;
- FPU latency;
- branch redirects;
- scoreboard locks;
- bypass availability;
- CSR/trap flushing;
- compressed instruction alignment.

So a single-issue model still needs real pipeline timing.

### Inter-Stage Buffers

The core has inter-stage buffers between pipeline stages:

```text
s0 -> s1
s1 -> s2
s2 -> s3
s3 -> s4
s4 -> s5
```

The model calls these:

```text
q_s0s1
q_s1s2
q_s2s3
q_s3s4
q_s4s5
```

Each stage fires only when:

- its input queue is not empty;
- its output queue is not full;
- its local hazard guards pass.

This is structural backpressure. There is no separate global stall network that
we can model independently. The queues are the stall network.

### FIFO Semantics Matter

One major bug in the early model was that queues were treated as simple Python
`deque`s. That lets a queue dequeue and enqueue in the same cycle even when the
RTL FIFO primitive would not.

The RTL uses different FIFO styles:

- `mkSizedFIFOF(n)` lowers to a guarded FIFO where a producer does not see space
  created by a same-cycle dequeue from a full queue;
- `mkLFIFOF()` is loopy and can pass through more freely.

This distinction became visible in held-out RTL points:

| Point | RTL slowdown | Old model |
|---|---:|---:|
| `isb_s3s4=1` | +91.75% | ~0% |
| `isb_s0s1=1` | +75.68% | ~0% |

The old model was blind to backpressure. After adding guarded FIFO semantics,
the aggregate deltas matched within a few percent.

This is the key lesson:

> A queue depth is not only a capacity. It also has scheduling semantics.

### Scoreboard

The C-Class scoreboard is a per-register lock array.

The basic behavior:

1. An instruction with a destination register reaches execute.
2. The destination register is locked.
3. Younger instructions that need that value must wait unless bypass can satisfy
   them.
4. The destination register is unlocked when the writer commits.

This differs from CVA6. CVA6 has a more out-of-order scoreboard/commit
structure. C-Class is simpler and stricter.

The model therefore uses a register-lock scoreboard, not a CVA6-style
scoreboard FIFO.

### RAW, WAW, And WAR

RAW means "read after write."

Example:

```text
add  x5, x1, x2
sub  x6, x5, x3
```

The `sub` needs the result of the `add`. If the value is not ready or bypassable,
the `sub` stalls.

WAW means "write after write."

```text
add  x5, x1, x2
sub  x5, x3, x4
```

WAR means "write after read."

```text
add  x6, x5, x1
sub  x5, x2, x3
```

In the single-issue in-order core, RAW is the main dependency issue. In the
dual-issue core, pair-level WAW/WAR behavior matters because two instructions
can be considered together.

### Bypass Network

Bypass is forwarding a value before it is written back to the register file.

Without bypass:

```text
add  x5, x1, x2
add  x6, x5, x3
```

The second instruction might wait until `x5` is committed.

With bypass, the second instruction can use the value from a later pipeline
stage.

The SHAKTI bypass network forwards from the heads of the two later inter-stage
buffers. The priority is:

```text
source0/slot1 > source0/slot0 > source1/slot1 > source1/slot0 > register file
```

Getting this wrong created large timing errors in the dual model.

### Branch Predictor

The core uses a gshare-style predictor with:

- BTB: branch target buffer;
- BHT: branch history table;
- global history;
- RAS: return-address stack;
- compressed-instruction halfword awareness.

The parameters come from `makefile.inc`:

- `btbdepth`;
- `bhtdepth`;
- `histlen`;
- `histbits`;
- `rasdepth`.

The model had to reproduce subtle RTL behavior:

- BTB `hi` bit matching for lower/upper compressed halfword;
- BSV bit-width truncation in the gshare hash;
- predictor training delay;
- history restore after mispredict;
- RAS enable/disable from build defines.

Branch prediction dominated many early accuracy errors.

### Functional-Unit Latency

The model derives latencies from RTL/build files:

- multiplier latency from `MULSTAGES_TOTAL`;
- divider latency from `DIVSTAGES`;
- hardfloat FPU latencies from `src/fpu/fpu.defines`;
- active `bsv_float` latency from the actual BSV FPU state machine.

The FPU path was initially a validation hole because Dhrystone and CoreMark have
zero dynamic floating-point instructions. We added `fpbench`, where 28.575% of
instructions are FLOAT, and fixed FP timing.

Before FP timing:

| FP model | Cycle error | Delta-t accuracy |
|---|---:|---:|
| before FP latency | -50.622% | 64.612% |
| after FP latency | -0.063% | 99.863% |

This is a good example of why "99% on CoreMark" is not enough.

## 4. SHAKTI Dual-Issue Core

The dual-issue C-Class is not just "two copies of single issue."

It has many asymmetric and coupled behaviors. Those behaviors are exactly what
the performance model must capture.

### Ground Truth Numbers

CoreMark 1.0, 40 iterations:

| Core | Cycles | Instructions | IPC |
|---|---:|---:|---:|
| Single issue | 14,910,941 | 12,771,408 | 0.857 |
| Dual issue RTL | 10,398,758 | 12,771,408 | 1.228 |

Dhrystone 2.1, 500 iterations:

| Core | Cycles | Instructions | IPC |
|---|---:|---:|---:|
| Single issue | 167,827 | 159,518 | 0.950 |
| Dual issue RTL | 119,827 | 159,518 | 1.330 |

The dual core improves performance, but it is far from ideal 2.0 IPC.

### Dual-Issue Configuration

The extracted delivered dual-issue configuration is:

| Item | Value |
|---|---|
| issue width | 2 |
| fetch width | 64 bits/cycle |
| instruction queue | 6 entries |
| `s2->s3` queue | vector bundle, effectively depth 1 in RTL |
| `s3->s4` queue | 8 vector entries |
| `s4->s5` queue | 8 vector entries |
| ALUs | 2 |
| branch/control units | 1 shared |
| memory issue paths | 1 |
| mul/div units | 1 |
| FPU units | 1 |
| MEM+MEM pairing | disabled |
| WAW/WAR pair stalls | disabled by `no_wawstalls` |

Two important gotchas:

1. The stage1-to-stage2 instruction queue is 6 entries, not the misleading
   `isb_s1s2=2` path.
2. `dual_mem` is not compiled in and the slot-1 memory path is not wired to the
   D-cache, so the delivered core has one memory port.

### Pairing Whitelist

Stage 2 decides whether two fetched instructions may issue as a pair.

Allowed pairs include:

- ALU + ALU;
- ALU + MULDIV;
- MULDIV + ALU;
- ALU + FLOAT;
- FLOAT + ALU;
- ALU + MEMORY;
- MEMORY + ALU;
- ALU + CONTROL;
- CONTROL + ALU;
- CONTROL + MULDIV;
- MULDIV + CONTROL;
- CONTROL + MEMORY;
- MEMORY + CONTROL;
- CONTROL + FLOAT;
- FLOAT + CONTROL.

Rejected pairs include:

- MEM + MEM in the delivered build;
- MULDIV + MULDIV;
- FLOAT + FLOAT;
- MEMORY + MULDIV;
- MEMORY + FLOAT;
- MULDIV + FLOAT;
- CONTROL + CONTROL unless experimentally enabled;
- anything involving SYSTEM/TRAP/WFI.

CONTROL means branch, `jal`, or `jalr`.

### Pair-Level RAW

The dual core has no intra-bundle forwarding.

So this pair is rejected:

```text
add  x5, x1, x2
add  x6, x5, x3
```

The second instruction needs the first instruction's result in the same bundle.
There is no same-bundle forwarding path, so the pair must be split.

But WAW and WAR pairs can co-issue because `no_wawstalls` is defined and the RTL
compiles those pair checks out.

This matters because it is easy to over-stall the model by incorrectly blocking
WAW/WAR pairs.

### Reversal

The dual-issue RTL can reverse a pair.

If the first instruction is ALU or control and the second uses a scarce
slot-0-only resource, the bundle may be reversed so the scarce instruction lands
in slot 0.

This means:

> Slot index is not always program order.

The model must keep track of true program order separately from physical slot
position.

### Lockstep

After decode, paired instructions move as a bundle.

If either slot is not ready, neither slot advances.

Examples:

- slot 0 has a ready ALU op;
- slot 1 waits on a register;
- the whole bundle stalls.

This is a major coupling point. It means a "lost slot" can become a lost cycle.

### Atomic Pair Retire

Stage 5 commits paired instructions atomically.

Both slots leave together, or neither leaves.

This matters for long-latency operations. A slow slot can hold its partner even
if the partner is otherwise ready.

### Memory Port Limitation

The delivered dual core has one memory issue path.

MEM+MEM pairs are rejected. The reference RTL counters suggested mem+mem was a
large lost-slot source, but the current model cannot price memory-port changes
with high confidence because memory latency/cache behavior is still not modeled
well enough in dual issue.

This is why the second-memory-port result is treated cautiously.

### Dual Model Accuracy

Dual-issue model baseline on CoreMark:

| Metric | Value |
|---|---:|
| RTL cycles | 10,398,758 |
| model cycles | 10,589,781 |
| aggregate error | +1.837% |
| delta-t accuracy | 93.302% |

That 93.302% is not directly comparable to single-issue 99% because dual issue
has many same-cycle commits. A small pairing disagreement can create two
delta-t mismatches.

Still, it means:

> Dual results below about 1.84% should be treated as below model resolution.

## 5. What We Built

The perf-model directory contains the model and validation tools.

| File | Purpose |
|---|---|
| `trace.py` | parse RTL commit logs and app logs |
| `trace_cache.py` | compact cache for large parsed traces |
| `isa.py` | RV64IMAFDC timing decoder |
| `model.py` | timing model |
| `accuracy.py` | delta-t metric and mismatch grouping |
| `experiment_configs.py` | named dual-issue experiment configs |
| `sweep.py` | A-J dual-issue sweep |
| `ceiling.py` | dependence-limited IPC ceiling analysis |
| `ipc17.py` | route sweep for IPC 1.7 question |
| `holdout/` | real RTL design-point validation harness |
| `tests/` | unit tests for decoder, parser, hazards, queues |
| `results/` | reports and JSON experiment outputs |

### Trace Parser

The original RTL commit log had no cycle stamps. We added a testbench-only cycle
stamp in `TbSoc.bsv`, not in the core datapath.

Each committed instruction line now includes:

```text
cycle <N> core 0: ...
```

The parser turns that into dynamic instruction objects.

The parser also finds benchmark windows using `mcycle` and `minstret` markers
from `app_log`. This is necessary because the simulator prints results and then
spins until timeout.

### Decoder

The decoder does not implement full RISC-V semantics. It implements timing
metadata:

- instruction length;
- opcode name;
- source registers;
- destination register;
- integer vs floating register file;
- load/store/control flags;
- branch/jump immediate;
- functional-unit class;
- scoreboard write behavior.

This is enough to model hazards without computing values.

### Model

The model simulates a clock cycle by running stage functions in reverse order.

For single issue it models:

- front-end fetch/decompress;
- decode;
- execute;
- memory;
- commit;
- ISB backpressure;
- scoreboard;
- bypass;
- predictor;
- FU latency;
- FP latency;
- CSR/trap flushing.

For dual issue it additionally models:

- pairing whitelist;
- pair reversal;
- no intra-bundle forwarding;
- WAW/WAR co-issue;
- lockstep paired movement;
- atomic pair retire;
- one memory issue path;
- one shared branch unit;
- optional experimental knobs.

### Accuracy Tool

The accuracy metric compares inter-commit spacing, not only total cycles.

For instruction `i`:

```text
rtl_delta   = rtl_cycle[i]   - rtl_cycle[i-1]
model_delta = model_cycle[i] - model_cycle[i-1]
```

The delta is correct if:

```text
rtl_delta == model_delta
```

Accuracy is:

```text
correct_deltas / total_deltas
```

This avoids hiding one early error as a permanent offset.

### Holdout Harness

The holdout harness is the most important validation infrastructure.

It:

1. changes one `BSC_DEFINES` knob;
2. rebuilds a real RTL core;
3. runs benchmarks;
4. runs the model with the variant makefile;
5. compares predicted vs measured deltas.

This tells us whether the model predicts design changes, not just one baseline.

That distinction is crucial.

Example:

- The model was 99.2% accurate on CoreMark.
- But before guarded FIFO semantics, it predicted ~0% for `isb_s3s4=1`.
- RTL measured +91.75%.

So baseline accuracy alone was not enough.

## 6. Current Results

### Single-Issue Baselines

Current calibrated single-issue results:

| Benchmark | Delta-t accuracy | Model cycles | RTL cycles | Cycle error |
|---|---:|---:|---:|---:|
| CoreMark 40 | 99.15-99.23% depending trace | ~14.85M | ~14.91-14.97M | within 1% |
| Dhrystone 500 | 99.655% | 167,113 | 167,827 | -0.425% |
| fpbench | 99.863% | 1,645,141 | 1,646,171 | -0.063% |

The exact CoreMark number depends on which generated baseline trace/report is
being quoted, but the important point is: single-issue timing is around 99%+
delta-t accuracy and within 1% aggregate cycle error.

### Held-Out Mechanism Validation

Validated mechanisms:

| Mechanism | Status |
|---|---|
| branch predictor sizing | validated with BTB/BHT points |
| mul/div latency | validated with FU latency points |
| ISB backpressure | validated after guarded FIFO fix |
| FP latency | validated with `fpbench` |

Unvalidated or weak mechanisms:

| Mechanism | Status |
|---|---|
| memory latency/cache behavior | not modeled well enough |
| bypass/forwarding changes | not buildable as a clean RTL define point |
| WAW-stall changes | model has no corresponding mechanism |
| dual FP behavior | no dynamic dual FP trace yet |

### Dual-Issue A-J Experiments

Current full dual CoreMark model:

| Experiment | IPC | Gain | Interpretation |
|---|---:|---:|---|
| baseline | 1.2060 | 0 | model is +1.84% pessimistic vs RTL |
| second memory port | 1.2144 | +0.698% | below model resolution |
| decouple lockstep | 1.2143 | +0.689% | below model resolution |
| independent retire | 1.2060 | 0 | no modeled opportunity |
| symmetric slots | 1.2060 | 0 | reversal already handles adjacent pairs |
| intra-bundle ALU forwarding | 1.2812 | +6.237% | largest clear single gain, but unvalidated |
| relax branch next-PC stall | 1.2060 | 0 | no modeled stall cycles |
| second branch unit | 1.2118 | +0.480% | below model resolution |
| intra forwarding + branch | 1.2953 | +7.406% | best adjacent two-wide result |

Important:

> Do not rank changes below the 1.84% dual baseline error as real wins.

### IPC 1.7 Question

Target:

```text
dual CoreMark IPC 1.228 -> IPC 1.700
```

That is a +38.4% improvement over RTL dual issue.

Dependence ceiling analysis says:

| Configuration | CoreMark ceiling IPC |
|---|---:|
| 2-wide adjacent pairing | 1.6765 |
| 2-wide, 4-entry lookahead | 1.9648 |
| 2-wide, 8-entry lookahead | ~2.0000 |
| 3-wide adjacent | 2.0697 |
| 4-wide adjacent | 2.1568 |

Conclusion:

> IPC 1.7 is impossible with the current adjacent-pair two-wide structure. A
> perfect version of that structure only reaches 1.6765.

Lookahead raises the ceiling, but the timed model did not convert it:

| Route | IPC | Gain |
|---|---:|---:|
| lookahead window 3 | 1.2144 | +0.695%, below resolution |
| lookahead window 4 | 1.1922 | -1.144% |
| lookahead window 8 | 1.1924 | -1.132% |
| window 4 + intra + memory + branch | 1.2554 | +4.096% |

Meaning:

> A shallow "pick a better second instruction" selector is not enough. To use
> lookahead profitably, the core likely needs a real issue queue / scheduler /
> retire structure that can handle skipped instructions without creating new
> backpressure.

### Quad Issue

Adjacent 4-wide has ceiling IPC 2.1568 on CoreMark.

That sounds good, but compare it properly:

- adjacent 2-wide ceiling: 1.6765;
- adjacent 4-wide ceiling: 2.1568;
- width doubled;
- ceiling improves only 28.6%.

Two-wide lookahead window 4 reaches 1.9648, close to adjacent 4-wide, with less
issue width.

Recommendation:

> Do not pursue quad issue without lookahead. Width alone is a weak use of RTL
> effort.

## 7. Why The Model Is Helpful

### It Makes RTL Roadmap Decisions Cheaper

RTL work is expensive. A second memory port, for example, may involve:

- D-cache porting;
- store buffer changes;
- arbitration;
- pipeline interface changes;
- verification;
- timing closure risk.

The model lets us ask whether the change is even likely to pay.

If the modeled gain is below the model's error floor, it is not a strong RTL
candidate.

### It Shows Which Bottleneck Appears Next

Performance changes are not additive.

Example:

- intra-bundle forwarding alone: +6.237%;
- second memory port alone: +0.698%;
- memory + intra forwarding was worse than simply adding forwarding in earlier
  stacked results.

This means:

> You cannot sum individual improvements. You must measure stacks.

The model lets us do that cheaply.

### It Prevents Misleading Counter Interpretations

The dual RTL had a defective branch+branch counter. The model produced its own
branch-pair opportunity count, which was much smaller than the bad counter
implied.

That prevented over-prioritizing a second branch unit.

### It Found Real Blind Spots

The model initially missed:

- guarded FIFO backpressure;
- FP latency;
- dual bypass shape;
- memory-latency weakness;
- trace-window mismatch;
- no-op experimental flags.

Each miss improved the validation discipline.

This is valuable because a performance model should not only produce numbers.
It should also expose which numbers are not trustworthy yet.

### It Gives Ceilings

The dependence ceiling analysis is especially useful.

It separates:

- "there is not enough ILP";
- from "there is ILP, but the pipeline cannot extract it."

For IPC 1.7:

- adjacent two-wide does not have enough ceiling;
- two-wide lookahead has enough ceiling;
- current timed lookahead does not extract it.

That is a clear architectural direction.

### It Gives Error Bars

A prediction without an error bar is just a guess.

The holdout design-point sweep gives a model resolution floor. For dual
CoreMark, the baseline error is 1.84%, so changes smaller than that should not
be quoted as real gains.

This prevents overclaiming.

## 8. How To Use The Model Correctly

### For Baseline Accuracy

Run:

```sh
cd c-class/perf-model
../.venv/bin/python3 model.py ../benchmarks/output/rtl.dump
```

With a cycle-stamped trace, it prints:

- instruction count;
- model cycles;
- IPC;
- delta-t accuracy;
- cycle error;
- mismatch groups.

### For Dual Prediction

Run:

```sh
../.venv/bin/python3 model.py <rtl.dump> <rtl1.dump> \
  --dual-issue --predict-only
```

Use `--predict-only` when there is no matching RTL timing for the hypothetical
configuration.

### For Design-Point Validation

Use the holdout harness:

```sh
cd c-class/perf-model/holdout
../../.venv/bin/python3 sensitivity.py ../../benchmarks/output/rtl.dump --limit 300000
../../.venv/bin/python3 rtl_sweep.py --dry-run
../../.venv/bin/python3 rtl_sweep.py
../../.venv/bin/python3 predict.py --benchmark dhrystone
../../.venv/bin/python3 score.py --benchmark dhrystone
```

Always run `sensitivity.py` before spending RTL build time. If the model does
not respond to a knob, either the knob is dead or the model does not contain the
mechanism.

### For IPC Ceilings

Run:

```sh
../.venv/bin/python3 ceiling.py <trace files> --app-log <app_log>
```

This reports dependence-limited IPC for grids of:

- issue width;
- lookahead window;
- same-cycle forwarding on/off.

### For The IPC 1.7 Route

Run:

```sh
../.venv/bin/python3 ipc17.py <dual coremark trace files> \
  --ceilings-json results/ceiling-coremark.json \
  --output results/ipc17_route.json
```

This measures the route candidates and reports IPC as a fraction of the relevant
ceiling.

## 9. How To Interpret Accuracy

### Total Cycle Error

Total cycle error is:

```text
(model_cycles - rtl_cycles) / rtl_cycles
```

This is useful but weak.

A model can have low total-cycle error while getting the mechanism attribution
wrong.

### Delta-t Accuracy

Delta-t accuracy compares spacing between consecutive commits.

This is stronger than total cycles because one early offset cannot hide later
local timing mistakes.

### Counter Agreement

Counter agreement asks:

- did the model count the same kind of stalls as RTL?
- are RAW stalls close?
- are mem+mem rejections close?
- are mispredicts close?
- are queue-full cycles close?

This is stronger than delta-t for mechanism debugging.

### Delta Prediction

Delta prediction is the most important for architectural exploration.

It asks:

> If RTL design point X changes cycles by +5%, does the model predict about
> +5% without retuning?

That is what tells us whether the model can price future changes.

## 10. What We Should Trust Right Now

Trust with relatively high confidence:

- single-issue integer baseline timing;
- single-issue FP FPU timing after `fpbench`;
- ISB depth/backpressure aggregate deltas;
- branch predictor and FU-latency direction;
- dependence ceiling analysis;
- the conclusion that adjacent two-wide cannot reach IPC 1.7.

Trust with caution:

- dual-issue aggregate predictions above the 1.84% floor;
- intra-bundle ALU forwarding gain;
- lookahead experiments, because they are model-only structures;
- branch+branch opportunity estimates.

Do not overtrust:

- second memory port numbers;
- memory-latency-sensitive predictions;
- changes below 1.84% in dual CoreMark;
- bypass/forwarding validation, because the RTL knob is not buildable;
- quad-issue timing model results until a proper 4-wide model drains and is
  validated.

## 11. Current RTL Recommendations

### 1. Intra-Bundle ALU Forwarding

Best current single change:

```text
IPC 1.2060 -> 1.2812
gain +6.237%
```

Why it helps:

- attacks RAW pair rejection;
- directly improves dual-issue pairing efficiency;
- works on a mechanism known to matter.

Caveat:

- bypass changes are not validated by a buildable RTL parameter;
- this needs careful RTL verification.

### 2. Do Not Lead With Second Memory Port

The model's standalone result is below resolution:

```text
gain +0.698%
```

This does not prove the second memory port is worthless. It proves the current
model cannot defend it as a top priority.

Because memory latency/cache behavior is still weakly modeled, a second memory
port needs a sharper memory validation workload before making a roadmap
decision.

### 3. Do Not Pursue Quad Issue Without Lookahead

Adjacent 4-wide does not buy enough ceiling for the cost.

A small lookahead window raises the theoretical ceiling more efficiently than
blind width. But the current pipeline cannot exploit lookahead cheaply.

So the likely useful direction is:

```text
2-wide + real issue queue/lookahead + forwarding
```

not:

```text
4-wide adjacent pairing
```

### 4. Treat Small Gains As Below Resolution

The following are below the dual model's current resolution floor:

- second memory port;
- lockstep decoupling alone;
- second branch unit alone;
- branch next-PC relaxation;
- lookahead window 3 alone.

They should not be ranked as real RTL wins yet.

## 12. Limitations

The model still does not compute data values.

It does not model:

- data-cache misses in detail;
- instruction-cache misses in detail;
- wrong-path fetch bandwidth;
- data-dependent division behavior fully;
- all FP divide/sqrt special cases;
- memory ordering effects requiring values;
- full out-of-order scheduling;
- a validated real issue queue;
- a validated quad-issue front end.

The validation set now covers:

- small cache-resident integer code;
- CoreMark;
- Dhrystone;
- one dense FP/memory-ish kernel.

It still does not cover large memory-bound applications or OS-like workloads.

## 13. Glossary

### IPC

Instructions per cycle.

```text
IPC = retired instructions / cycles
```

Higher is better.

### Cycle Accuracy

How often the model predicts the same inter-commit spacing as RTL.

### Delta-t

The cycle gap between two consecutive committed instructions.

### RTL

Register-transfer-level hardware design. Here, the Bluespec implementation of
SHAKTI C-Class.

### Commit

The point where an instruction becomes architecturally visible and is counted as
retired.

### Retire

Often used interchangeably with commit in this project.

### ISB

Inter-stage buffer. A FIFO queue between pipeline stages.

### Backpressure

A stall caused because a downstream queue is full.

### Scoreboard

Hardware structure that tracks which registers are waiting for a producer.

### RAW

Read after write. A true data dependency.

### WAW

Write after write. Two instructions write the same destination.

### WAR

Write after read. A younger instruction writes a register that an older
instruction reads.

### Bypass / Forwarding

Sending a result directly from a pipeline stage to a dependent instruction
without waiting for register-file writeback.

### BTB

Branch target buffer. Predicts target addresses for control-flow instructions.

### BHT

Branch history table. Predicts taken/not-taken direction.

### RAS

Return-address stack. Predicts function returns.

### Gshare

Branch predictor style that hashes PC bits with global branch history.

### Functional Unit

Execution resource such as ALU, memory pipe, multiplier/divider, branch unit, or
FPU.

### Lookahead Window

A pairing/scheduling window that can choose an instruction beyond the immediate
next instruction as the second issued instruction.

### Ceiling IPC

The maximum IPC possible under a simplified idealization. A dependence ceiling
removes structural and control costs and keeps only true dependencies.

### Model Resolution Floor

The smallest predicted effect that should be treated as distinguishable from
model error. For the current dual CoreMark model, the practical floor is about
1.84%.

## 14. Final Mental Model

Think of the project in four layers:

1. **Single-issue model:** mostly accurate and well validated across integer,
   backpressure, FU-latency, branch, and FP mechanisms.
2. **Dual-issue model:** useful directionally, but lower delta-t accuracy and a
   1.84% resolution floor.
3. **Holdout validation:** the reason we know which mechanisms are trustworthy.
4. **Architecture exploration:** where we use the model to avoid wasting RTL
   effort.

The most important conclusion is:

> The model is not just a way to predict IPC. It is a way to prevent bad RTL
> bets by showing ceilings, bottlenecks, mechanism errors, and uncertainty.

