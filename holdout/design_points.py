"""Held-out RTL design points for validating the model's *predictive* power.

Baseline cycle accuracy measures how well the model reproduces one core. It says
almost nothing about whether the model can price a design change, which is the
only thing it is actually used for. This module defines a set of single-knob
changes to ``BSC_DEFINES``. Each one is a real, buildable core, so the RTL gives
ground truth for the *change* in cycles.

Protocol rule, and the whole point of the exercise:

    The model is run with parameters derived from the variant's makefile.inc and
    nothing else. No calibrated constant (load_hit_latency, wb_flush_penalty,
    branch_mispredict_penalty, predictor_train_delay, ...) may be re-fitted per
    design point. A prediction that only lands after tuning is not evidence of
    anything.

Knobs were chosen to (a) exercise the mechanisms the dual-issue experiments
depend on and (b) span a wide range of effect sizes, so the resulting error bar
covers the magnitudes we want to make claims about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

DEFINES_RE = re.compile(r"^(?P<prefix>BSC_DEFINES\s*:?=\s*)(?P<defs>.*)$", re.MULTILINE)
VERILATOR_FLAGS_RE = re.compile(r"^(?P<prefix>VERILATOR_FLAGS\s*:?=\s*)(?P<flags>.*)$", re.MULTILINE)

#: Verilator lint waivers the sweep needs in order to link at all.
#:
#: The core is built with -DBSV_ASYNC_RESET, which makes the BSV library FIFO
#: primitives (FIFOL1.v and friends) drive their state from both a clocked and
#: an asynchronous-reset block. Verilator 5.050 reports that as MULTIDRIVEN and
#: escalates it to an error, so a clean build of *any* configuration fails
#: without the waiver -- including the unmodified baseline.
#:
#: This is a lint suppression, not a semantic change: it silences a warning
#: about primitives the build has always used. It is applied identically to the
#: baseline and to every variant, so predicted-vs-measured deltas are unaffected
#: either way. The baseline point re-measures the reference cycle counts, which
#: is the check that this changed nothing.
VERILATOR_WAIVERS = ("-Wno-MULTIDRIVEN",)

# A define edit maps a token name to:
#   str/int -> set "name=value"
#   True    -> set bare flag "name"
#   None    -> remove the token entirely
Edit = dict[str, "str | int | bool | None"]


@dataclass(frozen=True)
class DesignPoint:
    name: str
    edits: Edit
    mechanism: str
    why: str
    #: rough prior on |Δ cycles| so the sweep can be ordered informative-first
    expect: str
    #: points that may legitimately fail to elaborate; excluded, not fatal
    may_fail: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_baseline(self) -> bool:
        return not self.edits


BASELINE = DesignPoint(
    name="baseline",
    edits={},
    mechanism="reference",
    why="Re-measured with the same instrumented testbench as every variant, so "
    "the deltas are apples-to-apples.",
    expect="0",
)


DESIGN_POINTS: tuple[DesignPoint, ...] = (
    BASELINE,
    # ---- large effects: these anchor the top of the regression fit ----
    DesignPoint(
        name="nobpu",
        edits={"bpu": None},
        mechanism="control / branch prediction, entirely removed",
        why="Largest available lever. If the model cannot price the whole "
        "predictor it cannot price any control-flow change, which is what "
        "experiments F and G touch.\n\n"
        "Only `bpu` is removed. Dropping `gshare` and `bpu_ras` as well is the "
        "obvious formulation and it does not build: the `ifdef bpu_ras` block "
        "at gshare_fa.bsv:274-296 opens an `if (...) begin` inside the guard "
        "and closes it outside, so removing the define unbalances begin/end "
        "and the prediction method loses its return (P0071). With `bpu` off "
        "the predictor is never instantiated, so `gshare`, `bpu_ras`, "
        "`btbdepth` and friends are left as inert defines. The model keys "
        "enable_bpu on `bpu` alone, so it sees the same core.",
        expect="+20.7%",
        tags=("control",),
    ),
    # ---- medium effects ----
    DesignPoint(
        name="mul4",
        edits={"MULSTAGES_IN": 2, "MULSTAGES_OUT": 2, "MULSTAGES_TOTAL": 4},
        mechanism="multiplier latency 2 -> 4 cycles",
        why="Variable-latency FU path, independent of both control and "
        "bypass. MULSTAGES_TOTAL only sizes an ordering FIFO; the pipeline "
        "depth is MULSTAGES_IN + MULSTAGES_OUT, so all three move together.",
        expect="+5.4%",
        tags=("fu-latency",),
    ),
    DesignPoint(
        name="btb8",
        edits={"btbdepth": 8},
        mechanism="BTB capacity 32 -> 8 entries",
        why="Capacity misses in the BTB, without changing direction "
        "prediction. Tests the model's BTB allocation/replacement behaviour.",
        expect="+3.9%",
        tags=("control",),
    ),
    DesignPoint(
        name="bht64",
        edits={"bhtdepth": 64},
        mechanism="BHT 512 -> 64 counters (heavy aliasing)",
        why="Direction-prediction quality without touching the BTB. Separates "
        "target prediction from direction prediction in the error budget.",
        expect="+2.7%",
        tags=("control",),
    ),
    # ---- the band that matters: experiments A/B/C claim +0.76% / +1.16% /
    #      +1.56%, so the error bar has to be calibrated *here* or those
    #      numbers cannot be defended at all ----
    DesignPoint(
        name="btb16",
        edits={"btbdepth": 16},
        mechanism="BTB capacity 32 -> 16 entries",
        why="Sits at the magnitude of Experiment C (+1.56%). Same mechanism "
        "as btb8, half the perturbation, so it also tests linearity.",
        expect="+1.5%",
        tags=("control", "resolution-band"),
    ),
    DesignPoint(
        name="bht128",
        edits={"bhtdepth": 128},
        mechanism="BHT 512 -> 128 counters (mild aliasing)",
        why="Sits at the magnitude of Experiments A and B (+0.76%/+1.16%). "
        "The single most important point in the sweep for deciding whether "
        "those results can be reported as numbers at all.",
        expect="+0.8%",
        tags=("control", "resolution-band"),
    ),
    # ---- near-nulls: can the model correctly predict "almost nothing"? ----
    DesignPoint(
        name="noras",
        edits={"bpu_ras": None},
        mechanism="return-address stack removed",
        why="Already run once by hand (99.209% accuracy retained), so it "
        "doubles as a consistency check on the harness.",
        expect="+0.06%",
        tags=("control",),
    ),
    DesignPoint(
        name="div16",
        edits={"DIVSTAGES": 16},
        mechanism="divider latency 32 -> 16 steps (4 bits/step)",
        why="Deliberate near-null. Restoring divider is power-of-two "
        "constrained and 16 <= xlen, so this elaborates. Checks that the "
        "model reports near-null rather than manufacturing an effect.",
        expect="-0.005%",
        tags=("fu-latency",),
    ),
    # ---- backpressure ladder: s3/s4 is the calibration queue. The other ISBs
    #      are held-out checks of whether the FIFO scheduling mechanism
    #      generalises rather than fitting one queue. ----
    DesignPoint(
        name="s3s4_1",
        edits={"isb_s3s4": 1},
        mechanism="execute->memory buffer depth 8 -> 1",
        why="Calibration point for BSV FIFO scheduling semantics. A depth-1 "
        "guarded mkSizedFIFOF exposes registered notFull, so a producer cannot "
        "refill a full FIFO in the same cycle that the consumer dequeues.",
        expect="+91.75% measured before the fix",
        tags=("backpressure", "calibration"),
    ),
    DesignPoint(
        name="s3s4_2",
        edits={"isb_s3s4": 2},
        mechanism="execute->memory buffer depth 8 -> 2",
        why="Backpressure ladder point above the depth-1 cliff; should show "
        "whether the slowdown is specific to one-entry FIFO scheduling.",
        expect="near-null",
        tags=("backpressure", "calibration"),
    ),
    DesignPoint(
        name="s3s4_3",
        edits={"isb_s3s4": 3},
        mechanism="execute->memory buffer depth 8 -> 3",
        why="Backpressure ladder point above the measured peak occupancy in "
        "the model prefix.",
        expect="near-null",
        tags=("backpressure", "calibration"),
    ),
    DesignPoint(
        name="s3s4_4",
        edits={"isb_s3s4": 4},
        mechanism="execute->memory buffer depth 8 -> 4",
        why="Backpressure ladder point; verifies that reducing an oversized "
        "FIFO without hitting the one-entry scheduling cliff is harmless.",
        expect="near-null",
        tags=("backpressure", "calibration"),
    ),
    DesignPoint(
        name="s0s1_1",
        edits={"isb_s0s1": 1},
        mechanism="PC-gen->fetch buffer depth 2 -> 1",
        why="Held-out front-end backpressure check. This queue was one of the "
        "original blind spots: the model kept streaming through depth 1 even "
        "though RTL slowed dramatically.",
        expect="+75.68% measured before the fix",
        tags=("backpressure", "heldout"),
    ),
    DesignPoint(
        name="s0s1_3",
        edits={"isb_s0s1": 3},
        mechanism="PC-gen->fetch buffer depth 2 -> 3",
        why="Held-out front-end capacity check in the non-depth-1 direction.",
        expect="small or near-null",
        tags=("backpressure", "heldout", "resolution-band"),
    ),
    DesignPoint(
        name="s1s2_1",
        edits={"isb_s1s2": 1},
        mechanism="fetch/decompress->decode buffer depth 2 -> 1",
        why="Held-out decode-side backpressure check using the same "
        "mkSizedFIFOF scheduling mechanism as s0/s1.",
        expect="unknown held-out",
        tags=("backpressure", "heldout"),
    ),
    DesignPoint(
        name="s1s2_3",
        edits={"isb_s1s2": 3},
        mechanism="fetch/decompress->decode buffer depth 2 -> 3",
        why="Held-out decode-side capacity check in the non-depth-1 direction.",
        expect="small or near-null",
        tags=("backpressure", "heldout", "resolution-band"),
    ),
    DesignPoint(
        name="s4s5_1",
        edits={"isb_s4s5": 1},
        mechanism="memory->writeback buffer depth 8 -> 1",
        why="Held-out late-pipe backpressure check; the model prefix showed "
        "this queue peaking at one entry, so depth 1 is the binding edge.",
        expect="unknown held-out",
        tags=("backpressure", "heldout"),
    ),
    DesignPoint(
        name="s4s5_2",
        edits={"isb_s4s5": 2},
        mechanism="memory->writeback buffer depth 8 -> 2",
        why="Held-out late-pipe capacity check above the observed peak.",
        expect="near-null",
        tags=("backpressure", "heldout"),
    ),
    DesignPoint(
        name="s2s3_2",
        edits={"isb_s2s3": 2},
        mechanism="decode->execute define 1 -> 2 (single-issue RTL appears hardwired)",
        why="Source audit shows single-issue pipe_ifcs.bsv uses mkLFIFOF() for "
        "s2/s3 rather than mkSizedFIFOF(`isb_s2s3). This point checks that the "
        "define is indeed inert instead of making the model invent an effect.",
        expect="unmapped or RTL near-null",
        tags=("backpressure", "heldout", "inert-define"),
    ),
    DesignPoint(
        name="s2s3_4",
        edits={"isb_s2s3": 4},
        mechanism="decode->execute define 1 -> 4 (single-issue RTL appears hardwired)",
        why="Same inert-define check as s2s3_2 at a larger nominal setting.",
        expect="unmapped or RTL near-null",
        tags=("backpressure", "heldout", "inert-define"),
    ),
)

BY_NAME = {p.name: p for p in DESIGN_POINTS}

#: Points the model structurally cannot represent, with the reason. Kept here
#: rather than deleted so the report can state what was excluded and why.
UNMODELLABLE: dict[str, str] = {
    "no_wawstalls removed": (
        "The model has no WAW-stall mechanism: `wawid` is mapped only to a "
        "rename-tag width, and dropping `no_wawstalls` from BSC_DEFINES "
        "changes no Model parameter. Building this variant would produce a "
        "ground-truth delta with nothing to compare it against."
    ),
}

#: Knobs that look like design points but produce no buildable core, with the
#: RTL evidence. These are worse than an excluded point: the model responds to
#: them, so it will happily produce a prediction that can never be checked.
UNBUILDABLE: dict[str, str] = {
    "bypass_sources=1 (and =3)": (
        "`bypass_sources` is not actually a parameter. `riscv.bsv:245-246` "
        "assigns exactly `lv_bypass[0]` and `lv_bypass[1]`, and "
        "`bypass.bsv:42-46` hardcodes `choice[1:0]` and a three-way case ending "
        "in `fwd[2]` regardless of the define. Setting it to 1 fails "
        "elaboration with an out-of-range bit extraction; 3 would leave "
        "`lv_bypass[2]` unassigned. The value must be exactly 2.\n\n"
        "Consequence: the bypass network has NO buildable design point, so the "
        "mechanism Experiment E (+6.115%, intra-bundle ALU forwarding) rests on "
        "is the one mechanism this sweep cannot validate. The model responds "
        "strongly to the knob (+16.6% predicted for a single source), which "
        "makes the absence of ground truth easy to miss. Any forwarding claim "
        "must be reported as carrying no held-out validation."
    ),
}

#: Default sweep order: largest and lowest-risk effects first, so a partial run
#: is still a usable regression fit, with the resolution-band points early
#: enough to survive an interrupted sweep.
DEFAULT_ORDER = (
    "baseline",
    "nobpu",
    "btb8",
    "bht128",
    "mul4",
    "btb16",
    "bht64",
    "noras",
    "div16",
    "s3s4_1",
    "s3s4_2",
    "s3s4_3",
    "s3s4_4",
    "s0s1_1",
    "s0s1_3",
    "s1s2_1",
    "s1s2_3",
    "s4s5_1",
    "s4s5_2",
    "s2s3_2",
    "s2s3_4",
)


# --------------------------------------------------------------------------
# makefile.inc BSC_DEFINES editing
# --------------------------------------------------------------------------


def read_defines_line(makefile_inc: str | Path) -> str:
    text = Path(makefile_inc).read_text(encoding="utf-8")
    match = DEFINES_RE.search(text)
    if not match:
        raise ValueError(f"BSC_DEFINES not found in {makefile_inc}")
    return match.group("defs")


def apply_edits(defines: str, edits: Edit) -> str:
    """Apply token edits to a BSC_DEFINES value, preserving existing order."""
    tokens = defines.split()
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.split("=", 1)[0]
        if key in edits:
            seen.add(key)
            value = edits[key]
            if value is None:
                continue
            out.append(key if value is True else f"{key}={value}")
        else:
            out.append(token)
    for key, value in edits.items():
        if key in seen or value is None:
            continue
        out.append(key if value is True else f"{key}={value}")
    return " ".join(out)


def write_defines_line(makefile_inc: str | Path, defines: str) -> None:
    path = Path(makefile_inc)
    text = path.read_text(encoding="utf-8")
    new_text, count = DEFINES_RE.subn(
        lambda m: m.group("prefix") + defines, text, count=1
    )
    if count != 1:
        raise ValueError(f"BSC_DEFINES not found in {path}")
    path.write_text(new_text, encoding="utf-8")


def ensure_verilator_waivers(text: str) -> str:
    """Add the lint waivers the sweep needs, if the makefile lacks them."""
    match = VERILATOR_FLAGS_RE.search(text)
    if not match:
        return text
    flags = match.group("flags")
    missing = [w for w in VERILATOR_WAIVERS if w not in flags]
    if not missing:
        return text
    patched = flags.rstrip() + " " + " ".join(missing)
    return VERILATOR_FLAGS_RE.sub(lambda m: m.group("prefix") + patched, text, count=1)


def render_variant(baseline_makefile: str | Path, point: DesignPoint) -> str:
    """Return the full makefile.inc text for a design point."""
    path = Path(baseline_makefile)
    text = path.read_text(encoding="utf-8")
    defines = apply_edits(read_defines_line(path), point.edits)
    new_text, count = DEFINES_RE.subn(
        lambda m: m.group("prefix") + defines, text, count=1
    )
    if count != 1:
        raise ValueError(f"BSC_DEFINES not found in {path}")
    return ensure_verilator_waivers(new_text)


def describe(point: DesignPoint) -> str:
    if point.is_baseline:
        return "unmodified BSC_DEFINES"
    parts = []
    for key, value in point.edits.items():
        if value is None:
            parts.append(f"-{key}")
        elif value is True:
            parts.append(f"+{key}")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)
