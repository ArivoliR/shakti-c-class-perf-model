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
    baseline = experiments[0]
    dual_validation = data.get("dual_issue_validation") or baseline
    best_dual = max([item for item in experiments + extra if item["name"] != "J_quad_issue"], key=lambda item: item["model_ipc"])
    quad = next((item for item in experiments if item["name"] == "J_quad_issue"), None)

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

The delivered dual-issue SHAKTI model predicts {fmt_int(baseline['model_cycles'])}
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

The most important result is that the second memory port alone helps only
modestly in this model. Intra-bundle ALU forwarding is the largest individual
gain. The best draining two-issue combination found is {esc(best_dual['label'])},
with IPC {best_dual['model_ipc']:.4f}
({pct(best_dual.get('delta_ipc_pct_vs_baseline', 0.0), signed_value=True)}
versus baseline). The quad approximation is separate and reaches IPC
{quad['model_ipc']:.4f}. The model-owned branch+branch
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
\bottomrule
\end{{longtable}}

\section*{{Analysis}}

Run A, the second memory port, improves IPC by {delta_for(experiments, 'A_second_memory_port')}.
It removes the modeled non-RAW mem+mem reject counter, but the speedup is much
smaller than a first-order lost-slot estimate because many newly legal memory
pairs still contend with RAW dependencies, branch redirects, and lockstep
movement through later stages.

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

The automatic stacked run I (A+E+H) reached IPC {value_for(experiments, 'I_best_combination', 'model_ipc')}.
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
\item Symmetric slots alone had no effect because the current whitelist already
captures the useful mixed-FU pairs visible in the trace.
\item The branch next-PC relaxation is unresolved: the current model does not
include a separate queue-head next-PC stall beyond predictor redirect timing, so
the named F config is a no-op and should not be treated as RTL evidence.
\item Combining decoupled lockstep with intra-bundle forwarding did not drain on
a 100k-instruction diagnostic run. The final best combination therefore excludes
B when E is selected; this is reported as a model limitation/negative
interaction, not hidden.
\end{{itemize}}

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
\item Do not commit to quad issue from this evidence alone. The quad result is
an optimistic adjacent-pair approximation, not a validated 4-wide front end.
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
        rows.append(
            f"{esc(exp['label'])} & {fmt_int(exp['model_cycles'])} & {exp['model_ipc']:.4f} & "
            f"{signed(exp.get('delta_ipc_vs_baseline', 0.0), digits=4)} & "
            f"{pct(exp.get('delta_ipc_pct_vs_baseline', 0.0), signed_value=True)} & "
            f"{esc(exp.get('verdict', 'baseline'))} \\\\"
        )
    rows.extend([r"\bottomrule", r"\end{longtable}"])
    return "\n".join(rows)


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
            if isinstance(value, float):
                return f"{value:.4f}"
            return str(value)
    return "n/a"


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
