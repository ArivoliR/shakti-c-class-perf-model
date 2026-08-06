"""Run named dual-issue experiments and write JSON results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

from accuracy import compute_accuracy
from experiment_configs import (
    Experiment,
    PHASE2_EXPERIMENTS,
    build_best_combo,
    quad_from,
    sensitivity_experiments,
)
from model import Model
from trace import BenchmarkWindow, detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files


COREMARK_GROUND_TRUTH = {
    "benchmark": "CoreMark 1.0",
    "iterations": 40,
    "instructions": 12_771_408,
    "single_cycles": 14_910_941,
    "single_ipc": 0.85657,
    "dual_cycles": 10_398_758,
    "dual_ipc": 1.22817,
}

DHRYSTONE_GROUND_TRUTH = {
    "benchmark": "Dhrystone 2.1",
    "iterations": 500,
    "instructions": 159_518,
    "single_cycles": 167_827,
    "single_ipc": 0.95,
    "dual_cycles": 119_827,
    "dual_ipc": 1.33,
}

DHRYSTONE_COUNTER_REFERENCE = {
    "source": "instrumented dual RTL run, 115349 cycles / 159518 instructions",
    "cycles": 115_349,
    "instructions": 159_518,
    "dual_issued_pct_cycles": 0.50,
    "raw_hazard": 12_051,
    "one_instr": 4_026,
    "st3_not_firing": 4_864,
    "mem_mem_hazard": 22_510,
    "mem_mem_ll": 6_010,
    "mem_mem_ls": 6_000,
    "mem_mem_ss": 10_499,
    "mispredict": 2_034,
    "branch_branch_note": "RTL event 52 is defective; use model branch_branch_opportunities instead.",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_files", nargs="+")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--trace-cache", default=".trace-cache")
    parser.add_argument("--output", default="results/dual_issue_sweep.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-auto-window", action="store_true")
    parser.add_argument("--include-sensitivity", action="store_true", default=True)
    parser.add_argument("--skip-sensitivity", dest="include_sensitivity", action="store_false")
    parser.add_argument("--only", nargs="*", help="Run only experiment names containing any of these substrings")
    args = parser.parse_args()

    started = time.perf_counter()
    print("loading trace...", file=sys.stderr, flush=True)
    parsed_entries = load_or_parse_trace_files(args.trace_files, cache_dir=args.trace_cache, limit=args.limit)
    entries = parsed_entries
    window: BenchmarkWindow | None = None
    if not args.no_auto_window and args.limit is None:
        metrics = parse_app_log_metrics(Path(args.trace_files[0]).with_name("app_log"))
        if metrics is not None:
            window = detect_benchmark_window(parsed_entries, metrics)
            if window is not None:
                entries = window.entries(parsed_entries)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "generated_at_unix": time.time(),
        "trace_files": [str(Path(p).resolve()) for p in args.trace_files],
        "trace": {
            "parsed_entries": len(parsed_entries),
            "window_entries": len(entries),
            "window": _window_dict(window),
            "limit": args.limit,
            "note": (
                "The local CoreMark trace window length differs from the supplied ground-truth "
                "instruction count; aggregate comparisons use the supplied ground truth, while "
                "model IPC uses the trace window length."
            ),
        },
        "ground_truth": {
            "coremark": COREMARK_GROUND_TRUTH,
            "dhrystone": DHRYSTONE_GROUND_TRUTH,
            "dhrystone_counter_reference": DHRYSTONE_COUNTER_REFERENCE,
        },
        "single_issue_validation": {},
        "experiments": [],
        "sensitivity": [],
    }

    print("running single-issue validation...", file=sys.stderr, flush=True)
    single = Model.from_repo(args.repo_root)
    single_cycles = single.run(entries)
    single_total = _total_cycles(single_cycles)
    single_accuracy = compute_accuracy(entries, single_cycles)
    results["single_issue_validation"] = {
        "instructions": len(entries),
        "model_cycles": single_total,
        "model_ipc": len(entries) / single_total if single_total else 0.0,
        "dt_accuracy": single_accuracy.accuracy,
        "dt_matches": single_accuracy.matches,
        "dt_compared": single_accuracy.compared,
        "rtl_trace_cycles": single_accuracy.rtl_total_cycles,
        "cycle_error_vs_supplied_single": _pct_error(single_total, COREMARK_GROUND_TRUTH["single_cycles"]),
        "cycle_error_vs_local_trace": _pct_error(single_total, single_accuracy.rtl_total_cycles)
        if single_accuracy.rtl_total_cycles
        else None,
    }

    phase2 = _filter(PHASE2_EXPERIMENTS, args.only)
    phase2_results = []
    baseline_ipc = 0.0
    baseline_cycles = 0
    for experiment in phase2:
        result = _run_experiment(experiment, entries, args.repo_root)
        if experiment.name == "0_baseline":
            baseline_ipc = result["model_ipc"]
            baseline_cycles = result["model_cycles"]
        if baseline_ipc:
            result["delta_ipc_vs_baseline"] = result["model_ipc"] - baseline_ipc
            result["delta_ipc_pct_vs_baseline"] = result["delta_ipc_vs_baseline"] / baseline_ipc
            result["delta_cycles_vs_baseline"] = result["model_cycles"] - baseline_cycles
            result["verdict"] = _verdict(result["delta_ipc_pct_vs_baseline"])
        phase2_results.append(result)
        _write_json(output_path, results | {"experiments": phase2_results})
    results["experiments"] = phase2_results

    if baseline_ipc:
        helpful = {
            result["name"]
            for result in phase2_results
            if result.get("delta_ipc_pct_vs_baseline", 0.0) > 0.002 and result["name"] != "0_baseline"
        }
        best_cfg = build_best_combo(helpful)
        best = _run_experiment(
            Experiment(
                "I_best_combination",
                "I: best combination",
                "best",
                best_cfg,
                f"Stacked positive individual switches: {', '.join(sorted(helpful)) or 'none'}.",
            ),
            entries,
            args.repo_root,
        )
        best["delta_ipc_vs_baseline"] = best["model_ipc"] - baseline_ipc
        best["delta_ipc_pct_vs_baseline"] = best["delta_ipc_vs_baseline"] / baseline_ipc
        best["delta_cycles_vs_baseline"] = best["model_cycles"] - baseline_cycles
        best["verdict"] = _verdict(best["delta_ipc_pct_vs_baseline"])
        results["experiments"].append(best)

        quad = _run_experiment(
            Experiment(
                "J_quad_issue",
                "J: quad issue on best",
                "quad",
                quad_from(best_cfg),
                "Approximate quad issue as two adjacent SHAKTI-style pairs per cycle.",
            ),
            entries,
            args.repo_root,
        )
        quad["delta_ipc_vs_baseline"] = quad["model_ipc"] - baseline_ipc
        quad["delta_ipc_pct_vs_baseline"] = quad["delta_ipc_vs_baseline"] / baseline_ipc
        quad["delta_cycles_vs_baseline"] = quad["model_cycles"] - baseline_cycles
        quad["verdict"] = _verdict(quad["delta_ipc_pct_vs_baseline"])
        results["experiments"].append(quad)

    if args.include_sensitivity:
        sensitivity_results = []
        for experiment in _filter(sensitivity_experiments(), args.only):
            result = _run_experiment(experiment, entries, args.repo_root)
            if baseline_ipc:
                result["delta_ipc_vs_baseline"] = result["model_ipc"] - baseline_ipc
                result["delta_ipc_pct_vs_baseline"] = result["delta_ipc_vs_baseline"] / baseline_ipc
                result["delta_cycles_vs_baseline"] = result["model_cycles"] - baseline_cycles
                result["verdict"] = _verdict(result["delta_ipc_pct_vs_baseline"])
            sensitivity_results.append(result)
            results["sensitivity"] = sensitivity_results
            _write_json(output_path, results)

    results["elapsed_seconds"] = time.perf_counter() - started
    _write_json(output_path, results)
    print(f"wrote {output_path}", file=sys.stderr)
    return 0


def _run_experiment(experiment: Experiment, entries: list[Any], repo_root: str | Path) -> dict[str, Any]:
    print(f"running {experiment.name}...", file=sys.stderr, flush=True)
    started = time.perf_counter()
    model = Model.from_repo(repo_root, **experiment.overrides)
    cycles = model.run(entries)
    total_cycles = _total_cycles(cycles)
    ipc = len(entries) / total_cycles if total_cycles else 0.0
    profile = model.counter_profile(total_cycles)
    return {
        "name": experiment.name,
        "label": experiment.label,
        "hypothesis": experiment.hypothesis,
        "overrides": experiment.overrides,
        "instructions": len(entries),
        "model_cycles": total_cycles,
        "model_ipc": ipc,
        "cycle_error_vs_dual_ground_truth": _pct_error(total_cycles, COREMARK_GROUND_TRUTH["dual_cycles"]),
        "ipc_error_vs_dual_ground_truth": _pct_error(ipc, COREMARK_GROUND_TRUTH["dual_ipc"]),
        "profile": profile,
        "elapsed_seconds": time.perf_counter() - started,
    }


def _total_cycles(cycles: list[int]) -> int:
    return cycles[-1] - cycles[0] + 1 if cycles else 0


def _pct_error(value: float, reference: float | None) -> float | None:
    if reference in (None, 0):
        return None
    return (value - reference) / reference


def _verdict(delta_pct: float) -> str:
    if delta_pct > 0.002:
        return "helped"
    if delta_pct < -0.002:
        return "regressed"
    return "no effect"


def _window_dict(window: BenchmarkWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "start_index": window.start_index,
        "end_index": window.end_index,
        "measured_cycles": window.measured_cycles,
        "measured_instructions": window.measured_instructions,
        "runs": window.runs,
    }


def _filter(experiments: list[Experiment], filters: list[str] | None) -> list[Experiment]:
    if not filters:
        return experiments
    return [experiment for experiment in experiments if any(token in experiment.name for token in filters)]


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
