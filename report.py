"""Generate the Phase 3 dual-issue experiment PDF from sweep JSON."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="results/dual_issue_sweep.json")
    parser.add_argument("--tex", default="results/dual-issue-experiments.tex")
    parser.add_argument("--no-pdf", action="store_true")
    args = parser.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    tex_path = Path(args.tex)
    tex_path.parent.mkdir(parents=True, exist_ok=True)
    tex_path.write_text(render(data), encoding="utf-8")
    if not args.no_pdf:
        texmfvar = tex_path.parent / ".texmf-var"
        texmfvar.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["TEXMFVAR"] = str(texmfvar.resolve())
        for _ in range(2):
            subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", tex_path.name],
                cwd=tex_path.parent,
                env=env,
                check=True,
            )
    return 0


def render(data: dict[str, Any]) -> str:
    coremark = data["ground_truth"]["coremark"]
    dhrystone = data["ground_truth"]["dhrystone"]
    experiments = data["experiments"]
    extra = data.get("extra_combinations", [])
    sensitivity = data["sensitivity"]
    holdout_dhry = _load_json(Path("holdout/runs/validation-dhrystone.json"))
    holdout_core = _load_json(Path("holdout/runs/validation-coremark.json"))
    baseline = experiments[0]
    dual_validation = data.get("dual_issue_validation") or baseline
    completed = [
        item
        for item in experiments + extra
        if item["name"] != "J_quad_issue" and item.get("model_ipc") is not None
    ]
    best_dual = max(completed, key=lambda item: item["model_ipc"])
    quad = next((item for item in experiments if item["name"] == "J_quad_issue"), None)
    quad_text = (
        f"The quad approximation completed with IPC {quad['model_ipc']:.4f}."
        if quad and quad.get("model_ipc") is not None
        else "The quad approximation did not complete/drain in the full-trace sweep and is reported as not completed, not as a measured null."
    )

    return rf"""\documentclass[10pt]{{article}}
\usepackage[a4paper,margin=0.65in]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage{{booktabs}}
\usepackage{{longtable}}
\usepackage{{array}}
\usepackage{{hyperref}}
\usepackage{{xcolor}}

\title{{SHAKTI C-Class Dual-Issue Performance Model Experiments}}
\author{{perf-model}}
\date{{\today}}

\begin{{document}}
\maketitle

\section*{{Executive Summary}}

The current dual-issue SHAKTI model predicts {fmt_int(baseline['model_cycles'])}
cycles and IPC {baseline['model_ipc']:.4f} on the generated dual CoreMark
trace window. The clean dual RTL ground truth is {fmt_int(coremark['dual_cycles'])}
cycles and IPC {coremark['dual_ipc']:.5f}; the cycle-stamped run reproduced
those app-log counters exactly, so the trace instrumentation is non-perturbing.

\textbf{{Validation gate status: not passed.}} The dual baseline now has a real
per-instruction $\Delta t$ comparison against RTL, not a proxy. Its accuracy is
{pct(dual_validation.get('dt_accuracy'))}
({fmt_int(dual_validation.get('dt_matches'))}/{fmt_int(dual_validation.get('dt_compared'))}
matching inter-commit deltas), with aggregate cycle error
{pct(baseline['cycle_error_vs_dual_ground_truth'])}. This is below the 97\%
Phase-1 gate, so the architectural experiments below are useful directional
signals but still carry the measured residual timing error.

The backpressure blind spot that motivated this round is fixed for aggregate
cycle deltas: the depth-1 s3/s4, s0/s1, s1/s2, and s4/s5 RTL slowdowns are now
predicted within a few percent. However, local $\Delta t$ cadence is still weak
for the front-end depth-1 Dhrystone points, so this is not a full mechanism-level
closure.

The second memory port alone helps only modestly in this model:
{delta_for(experiments, 'A_second_memory_port')}. Intra-bundle ALU forwarding is
the largest individual gain. The best draining two-issue combination found is
{esc(best_dual['label'])}, with IPC {best_dual['model_ipc']:.4f}
({pct(best_dual.get('delta_ipc_pct_vs_baseline', 0.0), signed_value=True)}
versus baseline). {quad_text} The model-owned branch+branch
opportunity count is much smaller than the defective RTL event-52 story suggests.

\section*{{Experiment Summary}}

{experiment_table(experiments)}

\section*{{Additional Combination Probes}}

{experiment_table(extra) if extra else 'No additional combination probes were run.'}

\section*{{Validation}}

\begin{{tabular}}{{@{{}}lr@{{}}}}
\toprule
Item & Value \\
\midrule
Dual $\Delta t$ accuracy on generated CoreMark trace & {pct(dual_validation.get('dt_accuracy'))} \\
Dual $\Delta t$ matches / compared & {fmt_int(dual_validation.get('dt_matches'))} / {fmt_int(dual_validation.get('dt_compared'))} \\
Dual RTL commit-span cycles from trace & {fmt_int(dual_validation.get('rtl_trace_cycles'))} \\
Dual baseline model cycles & {fmt_int(baseline['model_cycles'])} \\
Dual clean RTL ground-truth cycles & {fmt_int(coremark['dual_cycles'])} \\
Dual aggregate cycle error & {pct(baseline['cycle_error_vs_dual_ground_truth'])} \\
Dual baseline model IPC & {baseline['model_ipc']:.5f} \\
Dual clean RTL ground-truth IPC & {coremark['dual_ipc']:.5f} \\
Held-out Dhrystone clean dual RTL IPC & {dhrystone['dual_ipc']:.2f} aggregate only \\
\bottomrule
\end{{tabular}}

\paragraph{{Counter-profile validation.}}
The model emits the RTL-facing counters needed for validation. The local
cycle-stamped trace is CoreMark, while the supplied RTL counter profile is from
an instrumented Dhrystone run. Absolute counter counts across those benchmarks
are not a valid calibration target, so the mem+mem 5\% counter gate remains
open until either CoreMark RTL counters or a Dhrystone commit trace are produced.

The baseline CoreMark model counter profile is:

{counter_table(baseline['profile'])}

For reference, the supplied Dhrystone RTL profile was: dual-issued about 50\%
of cycles, RAW 12,051, one-instruction fetch 4,026, stage3 not firing 4,864,
mem+mem 22,510 (LL 6,010, LS 6,000, SS 10,499), and mispredict 2,034.

\subsection*{{Held-Out Design-Point Validation}}

{holdout_summary(holdout_dhry, holdout_core)}

{holdout_key_table(holdout_dhry)}

{holdout_key_table(holdout_core)}

\section*{{Calibration Log}}

\begin{{longtable}}{{@{{}}p{{0.23\linewidth}}p{{0.31\linewidth}}p{{0.34\linewidth}}@{{}}}}
\toprule
Change & Effect & Evidence \\
\midrule
\endhead
Dual trace instrumentation & Added a testbench-only cycle counter to both
dual commit-log slots; no core datapath files were changed. & The stamped
CoreMark run reproduced the clean dual app-log counters exactly:
{fmt_int(coremark['dual_cycles'])} cycles and {fmt_int(coremark['instructions'])}
instructions. \\
Multi-line \texttt{{app\_log}} parsing & Fixed parser support for SHAKTI logs
that print \texttt{{IPC\_MEASURE cycles}} and \texttt{{IPC\_MEASURE instret}}
on separate lines, including the space before the colon. & The selected
benchmark window now has {fmt_int(data['trace']['window_entries'])}
instructions, matching CoreMark ground truth. \\
WAW/WAR pair handling & Kept WAW and WAR co-issue enabled because
\texttt{{no\_wawstalls}} compiles out the checks. & Unit tests verify same-rd
and write-after-read pairs commit together. \\
Lockstep scoping & Kept lockstep only for real paired bundles; a single bundle
does not wait on slot 1. & Unit tests exercise dependent and independent
single/pair cases. \\
Dual bypass shape & Fixed bypass lookup to inspect slot 1 and slot 0 of both
downstream sources, matching \texttt{{bypass.bsv}} priority
\texttt{{src0/slot1 > src0/slot0 > src1/slot1 > src1/slot0}}. & Full CoreMark
dual baseline moved from IPC 1.0357 before this fix to IPC {baseline['model_ipc']:.4f}. \\
Memory pairing policy & Split the actual delivered branch
(\texttt{{memory\_pairing=none}}) from the proposed second-port experiment
(\texttt{{memory\_pairing=all}}). & Run A is now a true second-memory-port
experiment instead of the narrower, gated store-involving \texttt{{dual\_mem}}
path. \\
Guarded FIFO backpressure & Modelled \texttt{{mkSizedFIFOF}} as a guarded FIFO
where \texttt{{FULL\_N}} is based on cycle-start occupancy, while \texttt{{mkLFIFOF}}
remains loopy. Added split compressed fetch-word handling for the single-issue
front end. & Dhrystone \texttt{{s3s4\_1}} moved from a near-zero prediction to
{fmt_int(_point(holdout_dhry, 's3s4_1', 'model_cycles'))} model cycles versus
{fmt_int(_point(holdout_dhry, 's3s4_1', 'rtl_cycles'))} RTL; CoreMark
\texttt{{s0s1\_1}} is now predicted within 1\% aggregate. \\
CoreMark holdout windowing & Fixed \texttt{{holdout/predict.py}} so CoreMark
variants use the baseline app-log window when only the baseline CoreMark trace
is archived. & Removed false instruction-stream drift warnings and replaced
invalid variant $\Delta t$ values with aggregate-only CoreMark validation. \\
\bottomrule
\end{{longtable}}

\section*{{Analysis}}

Run A, the second memory port, improves IPC by {delta_for(experiments, 'A_second_memory_port')}.
It removes the modeled non-RAW mem+mem reject counter, but the speedup is much
smaller than a first-order lost-slot estimate. The measured interaction is
sub-additive, not lockstep-gated: A+B is smaller than A plus B, so those changes
remove overlapping bubbles rather than unlocking one another.

Run B improves IPC by {delta_for(experiments, 'B_decouple_lockstep')}, confirming
that stage3/stage4 coupling is a real limiter. Run C is sub-additive relative to
A+B, so the second memory port and decoupling overlap in the bubbles they remove.

Run E is the largest individual improvement at {delta_for(experiments, 'E_intra_alu_forwarding')}.
It directly attacks the RAW rejection class and roughly halves the modeled RAW
reject count. This is the strongest candidate for RTL work if its implementation
cost is manageable.

Run H gives only {delta_for(experiments, 'H_second_branch_unit')}. The model's
independent branch+branch opportunity count is {fmt_int(baseline['profile']['branch_branch_opportunities'])}
on CoreMark, so branch+branch is not the dominant issue in this trace. This is
also a useful independent replacement for the defective RTL event 52.

The automatic stacked run I reached IPC {value_for(experiments, 'I_best_combination', 'model_ipc')}.
The extra probes show why it is not the actual best: A+E reached IPC
{value_for(extra, 'combo_AE_memory_plus_intra_forwarding', 'model_ipc')}, while
E+H reached IPC {value_for(extra, 'combo_EH_intra_forwarding_plus_branch', 'model_ipc')}.
So the second memory port interferes with the intra-forwarding win in this trace,
whereas branch+branch is mildly complementary with intra-forwarding.

\section*{{Sensitivity}}

{sensitivity_table(sensitivity)}

\section*{{What Did Not Work}}

\begin{{itemize}}
\item Independent retire alone had no effect because actual lockstep stage4
already delivers paired slots to stage5 together.
\item The branch next-PC relaxation is unresolved: the named F config remains
an exact no-op in this model and should not be treated as RTL evidence.
\item Symmetric slots remains an exact no-op in this trace/model. Treat it as an
unexpressed hypothesis, not proof that the RTL change is worthless.
\item Combining decoupled lockstep with intra-bundle forwarding did not drain on
a 100k-instruction diagnostic run. The final best combination therefore excludes
B when E is selected; this is reported as a model limitation/negative
interaction, not hidden.
\item Quad issue did not complete/drain in the full-trace sweep. It is reported
as not completed rather than as a measured null.
\end{{itemize}}

\section*{{Experiment Validation Coverage}}

{experiment_validation_table(experiments, extra, holdout_dhry, holdout_core)}

\section*{{Recommendations}}

\begin{{enumerate}}
\item Prototype intra-bundle ALU forwarding first. Estimated standalone gain:
{delta_for(experiments, 'E_intra_alu_forwarding')}; rough RTL cost: medium
(operand muxing, bypass select, verification of RAW edge cases).
\item Combine intra-bundle ALU forwarding with a second branch unit only if the
branch hardware is cheap. Best measured combination: E+H at IPC
{value_for(extra, 'combo_EH_intra_forwarding_plus_branch', 'model_ipc')}.
\item Treat the second memory port as a secondary change, not the headline by
itself. Estimated standalone gain: {delta_for(experiments, 'A_second_memory_port')};
rough RTL cost: high (D-cache/store-buffer/arbiter integration). It regresses
the E combination, so do not prioritize it before validating with a dual trace.
\item Investigate lockstep decoupling separately from intra-forwarding.
Estimated standalone gain: {delta_for(experiments, 'B_decouple_lockstep')};
rough RTL cost: medium-high because it touches stage3/stage4 valid/ready and
commit bookkeeping.
\item Deprioritize branch+branch hardware until a corrected RTL counter or dual
trace says otherwise. Estimated gain: {delta_for(experiments, 'H_second_branch_unit')};
rough RTL cost: medium.
\item Do not commit to quad issue from this evidence. The current approximation
did not drain on the full trace and is not a validated 4-wide front end.
\end{{enumerate}}

\section*{{Limitations}}

The model is a timing model over an already committed dynamic trace. It does not
compute data values. It does not model data-cache misses, cache conflicts,
instruction-cache misses, data-dependent divider timing, or CSR/trap behavior
that depends on values. The dual CoreMark trace now provides per-instruction RTL
commit cycles, but the baseline model only reaches the accuracy reported above;
the residual mismatches are concentrated around multiply-heavy and control-flow
neighborhoods and should be diagnosed before treating the experiment deltas as
RTL-commitment evidence. A Dhrystone commit trace is still missing for held-out
$\Delta t$ validation.

\end{{document}}
"""


def experiment_table(experiments: list[dict[str, Any]]) -> str:
    rows = [
        r"\begin{longtable}{@{}p{0.27\linewidth}rrrrp{0.13\linewidth}@{}}",
        r"\toprule",
        r"Experiment & Cycles & IPC & $\Delta$ IPC & $\Delta$ & Verdict \\",
        r"\midrule",
        r"\endhead",
    ]
    for exp in experiments:
        if exp.get("model_cycles") is None:
            rows.append(
                f"{esc(exp['label'])} & n/a & n/a & n/a & n/a & "
                f"{esc(exp.get('verdict', exp.get('status', 'not completed')))} \\\\"
            )
            continue
        rows.append(
            f"{esc(exp['label'])} & {fmt_int(exp['model_cycles'])} & {exp['model_ipc']:.4f} & "
            f"{signed(exp.get('delta_ipc_vs_baseline', 0.0), digits=4)} & "
            f"{pct(exp.get('delta_ipc_pct_vs_baseline', 0.0), signed_value=True)} & "
            f"{esc(exp.get('verdict', 'baseline'))} \\\\"
        )
    rows.extend([r"\bottomrule", r"\end{longtable}"])
    return "\n".join(rows)


def holdout_summary(dhrystone: dict[str, Any] | None, coremark: dict[str, Any] | None) -> str:
    if not dhrystone or not coremark:
        return "Held-out validation files were not available when this report was generated."
    dhry_sum = dhrystone["summary"]
    core_sum = coremark["summary"]
    dhry_base = dhrystone["baseline"]
    core_base = coremark["baseline"]
    return rf"""
\begin{{tabular}}{{@{{}}lrrrr@{{}}}}
\toprule
Benchmark & Baseline error & Baseline $\Delta t$ & Overall floor & Small-effect floor \\
\midrule
Dhrystone & {dhry_base['absolute_error_pct']:+.3f}\% & {dhry_base['delta_t_accuracy']*100:.3f}\% & {dhry_sum['resolution_floor_pp']:.2f} pp & {dhry_sum['small_effect_resolution_floor_pp']:.2f} pp \\
CoreMark & {core_base['absolute_error_pct']:+.3f}\% & {core_base['delta_t_accuracy']*100:.3f}\% & {core_sum['resolution_floor_pp']:.2f} pp & {core_sum['small_effect_resolution_floor_pp']:.2f} pp \\
\bottomrule
\end{{tabular}}

Dhrystone keeps the original held-out baseline accuracy target. CoreMark
baseline $\Delta t$ is {core_base['delta_t_accuracy']*100:.3f}\%, slightly below
the requested 99.2\% target, while aggregate cycle error remains within 1\%.
Variant CoreMark $\Delta t$ is not reported because only the baseline CoreMark
commit trace is archived; CoreMark variant rows are aggregate delta checks.
"""


def holdout_key_table(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    wanted = [
        "s3s4_1",
        "s0s1_1",
        "s1s2_1",
        "s4s5_1",
        "s3s4_2",
        "s0s1_3",
        "s1s2_3",
    ]
    rows = [
        rf"\paragraph{{{esc(result['benchmark']).capitalize()} key backpressure points.}}",
        r"\begin{tabular}{@{}lrrrr@{}}",
        r"\toprule",
        r"Point & Measured & Predicted & Error & $\Delta t$ \\",
        r"\midrule",
    ]
    by_name = {row["name"]: row for row in result.get("rows", [])}
    for name in wanted:
        row = by_name.get(name)
        if row is None:
            continue
        acc = row.get("delta_t_accuracy")
        acc_text = f"{acc*100:.2f}\\%" if acc is not None else "--"
        tex_name = name.replace("_", r"\_")
        rows.append(
            f"\\texttt{{{tex_name}}} & "
            f"{row['measured_pct']:+.2f}\\% & {row['predicted_pct']:+.2f}\\% & "
            f"{row['error_pp']:+.2f} pp & {acc_text} \\\\"
        )
    rows.extend([r"\bottomrule", r"\end{tabular}"])
    return "\n".join(rows)


def experiment_validation_table(
    experiments: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    dhrystone: dict[str, Any] | None,
    coremark: dict[str, Any] | None,
) -> str:
    core_floor = None
    dhry_floor = None
    if coremark:
        core_floor = coremark["summary"]["small_effect_resolution_floor_pp"]
    if dhrystone:
        dhry_floor = dhrystone["summary"]["small_effect_resolution_floor_pp"]
    rows = [
        r"\begin{longtable}{@{}p{0.25\linewidth}p{0.16\linewidth}p{0.19\linewidth}p{0.26\linewidth}@{}}",
        r"\toprule",
        r"Experiment & Predicted gain & Validation status & Notes \\",
        r"\midrule",
        r"\endhead",
    ]
    coverage = {
        "A_second_memory_port": ("below/near floor", "No direct dual memory-port RTL point; fixed memory-latency sensitivity makes the knob active."),
        "B_decouple_lockstep": ("below/near floor", "Coupling logic is model-only; effect is near the CoreMark small-effect floor."),
        "C_memory_plus_decouple": ("near floor", "Sub-additive; above small-effect floors but below overall mechanism floor."),
        "D_independent_retire": ("below resolution", "Implemented flag has exact zero effect because upstream delivery remains paired."),
        "E_intra_alu_forwarding": ("no ground truth", "Bypass/forwarding has no buildable single-issue design point."),
        "F_relax_branch_next_pc": ("unimplemented/no-op", "Exact zero; do not report as an RTL null."),
        "G_symmetric_slots": ("unimplemented/no-op", "Exact zero in this trace/model; do not report as an RTL null."),
        "H_second_branch_unit": ("near/below floor", "Small model-owned branch-pair opportunity result."),
        "I_best_combination": ("partial", "Two-issue stack; inherits E's no-ground-truth forwarding limitation."),
        "J_quad_issue": ("not completed", "Full-trace quad approximation did not drain/complete."),
        "combo_AE_memory_plus_intra_forwarding": ("partial", "Shows memory port interferes with E."),
        "combo_EH_intra_forwarding_plus_branch": ("partial", "Best draining two-issue model result; inherits E limitation."),
    }
    for exp in experiments + extra:
        name = exp["name"]
        status, note = coverage.get(name, ("model-only", "No specific validation mapping recorded."))
        rows.append(
            f"{esc(exp['label'])} & {exp_gain(exp)} & {esc(status)} & {esc(note)} \\\\"
        )
    rows.extend([r"\bottomrule", r"\end{longtable}"])
    floor_note = ""
    if core_floor is not None and dhry_floor is not None:
        floor_note = (
            f"Small-effect floors are {dhry_floor:.2f} pp on Dhrystone and "
            f"{core_floor:.2f} pp on CoreMark; claims at or below that scale "
            "should be read as below model resolution."
        )
    return "\n".join(rows) + "\n\n" + esc(floor_note)


def counter_table(profile: dict[str, Any]) -> str:
    rows = [
        r"\begin{tabular}{@{}lr@{}}",
        r"\toprule",
        r"Counter & Model value \\",
        r"\midrule",
        f"dual\\_issued & {fmt_int(profile['dual_issued'])} ({pct(profile['dual_issued_pct_cycles'])} of cycles) \\\\",
        f"raw\\_hazard & {fmt_int(profile['raw_hazard'])} \\\\",
        f"one\\_instr & {fmt_int(profile['one_instr'])} \\\\",
        f"st3\\_not\\_firing & {fmt_int(profile['st3_not_firing'])} \\\\",
        f"mem\\_mem\\_hazard & {fmt_int(profile['mem_mem_hazard'])} \\\\",
        f"mem LL / LS / SS & {fmt_int(profile['mem_mem_ll'])} / {fmt_int(profile['mem_mem_ls'])} / {fmt_int(profile['mem_mem_ss'])} \\\\",
        f"branch+branch opportunities & {fmt_int(profile['branch_branch_opportunities'])} \\\\",
        f"mispredict & {fmt_int(profile['mispredict'])} \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    return "\n".join(rows)


def sensitivity_table(sensitivity: list[dict[str, Any]]) -> str:
    rows = [
        r"\begin{longtable}{@{}p{0.42\linewidth}rrr@{}}",
        r"\toprule",
        r"Config & Cycles & IPC & $\Delta$ IPC \\",
        r"\midrule",
        r"\endhead",
    ]
    for result in sensitivity:
        if result.get("model_cycles") is None:
            rows.append(f"{esc(result['label'])} & n/a & n/a & n/a \\\\")
            continue
        rows.append(
            f"{esc(result['label'])} & {fmt_int(result['model_cycles'])} & "
            f"{result['model_ipc']:.4f} & {pct(result.get('delta_ipc_pct_vs_baseline', 0.0), signed_value=True)} \\\\"
        )
    rows.extend([r"\bottomrule", r"\end{longtable}"])
    return "\n".join(rows)


def delta_for(experiments: list[dict[str, Any]], name: str) -> str:
    for exp in experiments:
        if exp["name"] == name:
            return pct(exp.get("delta_ipc_pct_vs_baseline", 0.0), signed_value=True)
    return "n/a"


def value_for(experiments: list[dict[str, Any]], name: str, key: str) -> str:
    for exp in experiments:
        if exp["name"] == name:
            value = exp.get(key)
            if value is None:
                return "n/a"
            if isinstance(value, float):
                return f"{value:.4f}"
            return str(value)
    return "n/a"


def exp_gain(exp: dict[str, Any]) -> str:
    value = exp.get("delta_ipc_pct_vs_baseline")
    if value is None:
        return "n/a"
    return pct(value, signed_value=True)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _point(result: dict[str, Any] | None, name: str, key: str) -> Any:
    if not result:
        return None
    for row in result.get("rows", []):
        if row.get("name") == name:
            if key == "model_cycles":
                return row.get("model_cycles")
            if key == "rtl_cycles":
                return row.get("rtl_cycles")
            return row.get(key)
    return None


def fmt_int(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{int(value):,}"


def pct(value: Any, signed_value: bool = False) -> str:
    if value is None:
        return "n/a"
    prefix = "+" if signed_value and value >= 0 else ""
    return f"{prefix}{float(value) * 100:.3f}\\%"


def signed(value: float, digits: int = 3) -> str:
    return f"{value:+.{digits}f}"


def esc(value: Any) -> str:
    text = str(value)
    return (
        text.replace("\\", r"\textbackslash{}")
        .replace("_", r"\_")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("#", r"\#")
    )


if __name__ == "__main__":
    raise SystemExit(main())
