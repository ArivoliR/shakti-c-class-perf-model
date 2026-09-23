#!/usr/bin/env python3
"""Screen a buildable four-wide C-Class architecture before changing RTL.

This is an architectural exploration, not quad-issue RTL validation.  It uses
the validated parts of ``model.py`` (decode metadata, scoreboard, FU latency,
branch prediction, cache geometry) and the model-only ordered issue/completion
path.  Every result is therefore labelled predicted and is reported against
the dual model on the same trace windows.

The ladder changes one structural gate at a time:

* four-wide back end behind the existing 64-bit fetch path;
* 128-bit fetch/decompress bandwidth;
* two memory ports, first ideal and then four-bank constrained;
* a second control unit;
* a deeper issue window;
* four banked memory issue slots (an intentional cost/benefit rejection test).

The proposed implementation point is ``quad_banked4_merge_w4``: a four-entry,
four-select age-bounded issue buffer, eight-entry independent completion,
in-order four-wide retirement, one MULDIV, one FPU, one control unit, and two
requests to a four-bank D-cache with same-line coalescing.  Larger windows are
kept as sensitivity points rather than silently folded into the proposal.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from statistics import fmean, pstdev
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ceiling import dependence_ceiling
from model import Model
from trace_cache import load_or_parse_trace_files


ROOT = Path(__file__).resolve().parent
DUAL = ROOT.parent.parent / "c-class-dual-issue"
TRACE_ROOT = DUAL / "benchmarks" / "output"


@dataclass(frozen=True)
class DesignPoint:
    name: str
    label: str
    hypothesis: str
    overrides: dict[str, Any]


def _dual_reference(*, model_dcache: bool) -> dict[str, Any]:
    return {
        "num_issue": 2,
        "dual_policy": "shakti",
        "model_fetch_word_alignment": True,
        "fetch_word_bytes": 8,
        "fetch_residue_bytes": 6,
        "fetch_width": 2,
        "fetch_decode_width": 2,
        "decode_width": 1,
        "issue_width": 1,
        "stage4_width": 1,
        "commit_width": 1,
        "memory_issue_width": 1,
        "control_issue_width": 1,
        "memory_pairing": "none",
        "mem_banks": 1,
        "isb_s0s1": 4,
        "isb_s1s2": 6,
        "isb_s2s3": 2,
        "isb_s3s4": 16,
        "isb_s4s5": 16,
        "lockstep_bundles": True,
        "atomic_pair_retire": True,
        "mul_latency": 2,
        "div_latency": 32,
        "model_dcache": model_dcache,
    }


def _quad(
    *,
    window: int = 4,
    fetch_bytes: int = 16,
    memory_width: int = 1,
    memory_pairing: str = "none",
    mem_banks: int = 1,
    control_width: int = 1,
    allow_branch_branch: bool = False,
    completion_entries: int = 32,
    model_dcache: bool,
) -> dict[str, Any]:
    return {
        "num_issue": 4,
        "dual_policy": "shakti",
        "model_fetch_word_alignment": True,
        "fetch_word_bytes": fetch_bytes,
        "fetch_residue_bytes": max(0, fetch_bytes - 2),
        "fetch_width": 4,
        "fetch_decode_width": 4,
        # Stage2 and the scheduler each consume/produce one vector transaction
        # per cycle; the scheduler selects up to num_issue scalar entries.
        "decode_width": 1,
        "issue_width": 1,
        "stage4_width": 4,
        "commit_width": 1,
        "memory_issue_width": memory_width,
        "control_issue_width": control_width,
        "memory_pairing": memory_pairing,
        "mem_banks": mem_banks,
        "bank_shift": 6,
        "isb_s0s1": 8,
        "isb_s1s2": max(8, 2 * window),
        "isb_s2s3": window,
        "isb_s3s4": completion_entries,
        "isb_s4s5": completion_entries,
        "lockstep_bundles": False,
        "atomic_pair_retire": False,
        "symmetric_slots": True,
        "tiny_scheduler_window": window,
        "tiny_scheduler_strict_memory_order": True,
        "tiny_scheduler_respect_side_effect_order": True,
        "allow_branch_branch": allow_branch_branch,
        "mul_latency": 2,
        "div_latency": 32,
        "model_dcache": model_dcache,
    }


def design_points(*, model_dcache: bool = False) -> list[DesignPoint]:
    return [
        DesignPoint(
            "dual_reference",
            "dual RTL policy",
            "Measured dual topology represented by the model.",
            _dual_reference(model_dcache=model_dcache),
        ),
        DesignPoint(
            "quad_64b_w4",
            "quad, 64b fetch, 1 mem",
            "Shows whether a four-wide back end is starved by the existing fetch path.",
            _quad(fetch_bytes=8, model_dcache=model_dcache),
        ),
        DesignPoint(
            "quad_128b_w4",
            "quad, 128b fetch, 1 mem",
            "Prices the generic 16-byte align/decompress front end.",
            _quad(fetch_bytes=16, model_dcache=model_dcache),
        ),
        DesignPoint(
            "quad_dual_mem_ideal_w4",
            "quad, ideal 2 mem",
            "Upper bound for two memory request lanes before bank conflicts.",
            _quad(
                fetch_bytes=16,
                memory_width=2,
                memory_pairing="all",
                mem_banks=2,
                model_dcache=model_dcache,
            ),
        ),
        DesignPoint(
            "quad_banked4_w4",
            "quad, 2 mem / 4 banks",
            "Buildable banked baseline: two request lanes and a four-bank line-interleaved D-cache.",
            _quad(
                fetch_bytes=16,
                memory_width=2,
                memory_pairing="banked",
                mem_banks=4,
                model_dcache=model_dcache,
            ),
        ),
        DesignPoint(
            "quad_banked4_merge_w4",
            "quad, 4 banks + line merge",
            "Proposed point: coalesce two same-line requests after bank selection to recover same-line conflicts.",
            {
                **_quad(
                    fetch_bytes=16,
                    memory_width=2,
                    memory_pairing="banked",
                    mem_banks=4,
                    completion_entries=8,
                    model_dcache=model_dcache,
                ),
                "bank_merge_same_line": True,
            },
        ),
        DesignPoint(
            "quad_banked4_merge_w4_c12",
            "quad, merge, 12 completion entries",
            "Candidate small completion capacity: three maximum-width issue groups.",
            {
                **_quad(
                    fetch_bytes=16,
                    memory_width=2,
                    memory_pairing="banked",
                    mem_banks=4,
                    completion_entries=12,
                    model_dcache=model_dcache,
                ),
                "bank_merge_same_line": True,
            },
        ),
        DesignPoint(
            "quad_banked4_merge_w4_c16",
            "quad, merge, 16 completion entries",
            "Sensitivity between the 12-entry candidate and inherited 32-entry capacity.",
            {
                **_quad(
                    fetch_bytes=16,
                    memory_width=2,
                    memory_pairing="banked",
                    mem_banks=4,
                    completion_entries=16,
                    model_dcache=model_dcache,
                ),
                "bank_merge_same_line": True,
            },
        ),
        DesignPoint(
            "quad_banked4_merge_w4_c32",
            "quad, merge, 32 completion entries",
            "Inherited-capacity upper sensitivity for the eight-entry proposal.",
            {
                **_quad(
                    fetch_bytes=16,
                    memory_width=2,
                    memory_pairing="banked",
                    mem_banks=4,
                    completion_entries=32,
                    model_dcache=model_dcache,
                ),
                "bank_merge_same_line": True,
            },
        ),
        DesignPoint(
            "quad_banked4_2ctrl_w4",
            "quad, + second control",
            "Tests whether branch bandwidth is worth another control unit.",
            _quad(
                fetch_bytes=16,
                memory_width=2,
                memory_pairing="banked",
                mem_banks=4,
                control_width=2,
                allow_branch_branch=True,
                model_dcache=model_dcache,
            ),
        ),
        DesignPoint(
            "quad_banked4_w8",
            "quad, window 8",
            "Tests whether selector depth beyond one four-instruction group converts into IPC.",
            _quad(
                window=8,
                fetch_bytes=16,
                memory_width=2,
                memory_pairing="banked",
                mem_banks=4,
                model_dcache=model_dcache,
            ),
        ),
        DesignPoint(
            "quad_banked4_shadow_w8",
            "quad, W4 + miss shadow W8",
            "Novel point: keep four candidates on the normal path and expose four shadow entries only while a cache bank is occupied.",
            {
                **_quad(
                    window=8,
                    fetch_bytes=16,
                    memory_width=2,
                    memory_pairing="banked",
                    mem_banks=4,
                    model_dcache=model_dcache,
                ),
                "tiny_scheduler_fast_window": 4,
            },
        ),
        DesignPoint(
            "quad_banked4_w12",
            "quad, window 12",
            "Diminishing-return and timing-risk sensitivity point.",
            _quad(
                window=12,
                fetch_bytes=16,
                memory_width=2,
                memory_pairing="banked",
                mem_banks=4,
                model_dcache=model_dcache,
            ),
        ),
        DesignPoint(
            "quad_four_mem_w4",
            "quad, 4 mem / 4 banks",
            "Reject-or-justify test for fully matching memory width to issue width.",
            _quad(
                fetch_bytes=16,
                memory_width=4,
                memory_pairing="banked",
                mem_banks=4,
                model_dcache=model_dcache,
            ),
        ),
    ]


def _chunks(entries: list[Any], *, window: int, count: int) -> list[list[Any]]:
    result = []
    for index in range(count):
        chunk = entries[index * window : (index + 1) * window]
        if len(chunk) < max(4, window // 2):
            break
        result.append(chunk)
    if not result and entries:
        result.append(entries)
    return result


def _span(cycles: list[int]) -> int:
    return cycles[-1] - cycles[0] + 1 if cycles else 0


def _merge_counter(target: Counter[Any], source: dict[Any, Any]) -> None:
    for key, value in source.items():
        target[key] += int(value)


def _run_point(point: DesignPoint, chunks: list[list[Any]]) -> dict[str, Any]:
    spans: list[int] = []
    issue_histogram: Counter[int] = Counter()
    cycle_account: Counter[str] = Counter()
    candidate_checks = 0
    non_head_issues = 0
    order_blocks = 0
    shadow_cycles = 0
    started = time.perf_counter()
    for chunk in chunks:
        model = Model.from_repo(DUAL, **point.overrides)
        cycles = model.run(chunk)
        spans.append(_span(cycles))
        _merge_counter(issue_histogram, model.counter_profile().get("issue_width_histogram", {}))
        _merge_counter(cycle_account, dict(model.cycle_account))
        candidate_checks += model.tiny_scheduler_candidate_checks
        non_head_issues += model.tiny_scheduler_non_head_issues
        order_blocks += model.tiny_scheduler_order_blocks
        shadow_cycles += model.tiny_scheduler_shadow_cycles
    instructions = sum(len(chunk) for chunk in chunks)
    cycles = sum(spans)
    issue_instructions = sum(width * count for width, count in issue_histogram.items())
    issue_cycles = sum(issue_histogram.values())
    return {
        "name": point.name,
        "label": point.label,
        "hypothesis": point.hypothesis,
        "overrides": point.overrides,
        "instructions": instructions,
        "model_cycles": cycles,
        "model_ipc": instructions / cycles if cycles else 0.0,
        "window_spans": spans,
        "issue_width_histogram": dict(sorted(issue_histogram.items())),
        "average_issue_width_when_active": issue_instructions / issue_cycles if issue_cycles else None,
        "full_width_issue_share": issue_histogram[point.overrides["num_issue"]] / issue_cycles if issue_cycles else None,
        "candidate_checks": candidate_checks,
        "candidate_checks_per_model_cycle": candidate_checks / cycles if cycles else 0.0,
        "non_head_issues": non_head_issues,
        "order_blocks": order_blocks,
        "shadow_window_cycles": shadow_cycles,
        "cycle_account": dict(cycle_account),
        "runtime_seconds": time.perf_counter() - started,
        "status": "predicted_not_rtl_validated" if point.name != "dual_reference" else "model_of_measured_dual_topology",
    }


def _ceiling_across_chunks(chunks: list[list[Any]], *, width: int, window: int) -> dict[str, Any]:
    results = [
        dependence_ceiling(chunk, issue_width=width, lookahead_window=window)
        for chunk in chunks
    ]
    instructions = sum(result["instructions"] for result in results)
    cycles = sum(result["cycles"] for result in results)
    checks = sum(result["candidate_checks"] for result in results)
    return {
        "issue_width": width,
        "lookahead_window": window,
        "instructions": instructions,
        "cycles": cycles,
        "ipc": instructions / cycles if cycles else 0.0,
        "candidate_checks_per_cycle": checks / cycles if cycles else 0.0,
    }


def _render(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"{'design point':<29} {'IPC':>8} {'vs dual':>9} {'full':>8} {'checks/cy':>10}",
    ]
    baseline = rows[0]["model_cycles"]
    for row in rows:
        gain = (baseline - row["model_cycles"]) / baseline if baseline else 0.0
        full = row["full_width_issue_share"]
        lines.append(
            f"{row['name']:<29} {row['model_ipc']:>8.4f} {gain:>+8.2%} "
            f"{(full if full is not None else 0.0):>7.2%} "
            f"{row['candidate_checks_per_model_cycle']:>10.2f}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmarks",
        nargs="*",
        default=["coremark40", "dhry500", "fpbench64"],
        help="Directory names under c-class-dual-issue/benchmarks/output.",
    )
    parser.add_argument("--window", type=int, default=300_000)
    parser.add_argument("--windows", type=int, default=3, help="Maximum disjoint windows per benchmark")
    parser.add_argument("--model-dcache", action="store_true", help="Enable exploratory cache miss/occupancy timing")
    parser.add_argument("--only", help="Comma-separated design point names")
    parser.add_argument("--output", default="results/quad_issue_study.json")
    args = parser.parse_args()

    selected = set(part.strip() for part in args.only.split(",")) if args.only else set()
    points = [point for point in design_points(model_dcache=args.model_dcache) if not selected or point.name in selected]
    if not points or points[0].name != "dual_reference":
        raise SystemExit("selection must include dual_reference so deltas have a baseline")

    output: dict[str, Any] = {
        "method": "trace-driven timing prediction; quad points have no RTL validation",
        "model_dcache": args.model_dcache,
        "warnings": [
            "Score predicted-vs-measured deltas once quad RTL exists; aggregate baseline accuracy is not validation.",
            "The forwarding topology and four-wide front end are proposals, not held-out validated mechanisms.",
            "Frequency, area, power, I-cache misses, and physical register-file timing are not included in IPC.",
            "Dhrystone and CoreMark are cache-resident integer workloads; fpbench is the only FP/capacity signal.",
        ],
        "benchmarks": {},
    }
    for benchmark in args.benchmarks:
        trace_path = TRACE_ROOT / benchmark / "rtl.dump"
        if not trace_path.exists():
            print(f"{benchmark}: missing {trace_path}", file=sys.stderr)
            continue
        limit = args.window * args.windows
        print(f"{benchmark}: loading up to {limit:,} instructions", file=sys.stderr, flush=True)
        entries = load_or_parse_trace_files([str(trace_path)], limit=limit)
        chunks = _chunks(entries, window=args.window, count=args.windows)
        if not chunks:
            continue
        rows = []
        for point in points:
            print(f"  {point.name}", file=sys.stderr, flush=True)
            rows.append(_run_point(point, chunks))
        baseline_cycles = rows[0]["model_cycles"]
        for row in rows:
            row["cycle_delta_vs_dual"] = row["model_cycles"] - baseline_cycles
            row["speedup_vs_dual"] = baseline_cycles / row["model_cycles"] if row["model_cycles"] else None
            row["ipc_gain_vs_dual"] = (
                (baseline_cycles - row["model_cycles"]) / row["model_cycles"]
                if row["model_cycles"]
                else None
            )
            row["cycle_reduction_vs_dual"] = (
                (baseline_cycles - row["model_cycles"]) / baseline_cycles
                if baseline_cycles
                else None
            )
            per_window = [
                (base - value) / base
                for base, value in zip(rows[0]["window_spans"], row["window_spans"])
            ]
            row["per_window_cycle_reduction_vs_dual"] = per_window
            row["window_scatter"] = {
                "mean": fmean(per_window),
                "minimum": min(per_window),
                "maximum": max(per_window),
                "population_sigma": pstdev(per_window) if len(per_window) > 1 else None,
                "resolution_floor_2sigma": 2 * pstdev(per_window) if len(per_window) > 1 else None,
                "warning": "trace-window scatter only; not a predicted-vs-measured RTL error bar",
            }
        ceilings = [
            _ceiling_across_chunks(chunks, width=2, window=2),
            _ceiling_across_chunks(chunks, width=4, window=4),
            _ceiling_across_chunks(chunks, width=4, window=8),
            _ceiling_across_chunks(chunks, width=4, window=12),
        ]
        output["benchmarks"][benchmark] = {
            "trace": str(trace_path.resolve()),
            "instructions": sum(len(chunk) for chunk in chunks),
            "window_lengths": [len(chunk) for chunk in chunks],
            "ceilings": ceilings,
            "design_points": rows,
        }
        print(_render(rows), flush=True)
        print(flush=True)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
