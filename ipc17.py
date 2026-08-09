"""Sweep model configurations that could plausibly route dual issue toward IPC 1.7."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from typing import Any

from ceiling import dependence_ceiling
from experiment_configs import (
    A_SECOND_MEMORY,
    B_DECOUPLE_LOCKSTEP,
    D_INDEPENDENT_RETIRE,
    E_INTRA_ALU_FORWARDING,
    F_RELAX_BRANCH_NEXT_PC,
    G_SYMMETRIC_SLOTS,
    H_SECOND_BRANCH,
    merged,
)
from model import Model
from trace import BenchmarkWindow, detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files


COREMARK_DUAL_CYCLES = 10_398_758
COREMARK_DUAL_IPC = 1.22817
MODEL_BASELINE_ERROR = 0.0184


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_files", nargs="+")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--trace-cache", default=".trace-cache")
    parser.add_argument("--app-log")
    parser.add_argument(
        "--ceilings-json",
        help="Reuse ceiling.py JSON rows and its trace window instead of recomputing the grid.",
    )
    parser.add_argument("--output", default="results/ipc17_route.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--only",
        help="Comma-separated experiment names to run. By default all route specs run.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_existing(output_path) if args.resume else {"experiments": []}

    print("loading trace...", file=sys.stderr, flush=True)
    parsed = load_or_parse_trace_files(args.trace_files, cache_dir=args.trace_cache, limit=args.limit)
    entries = parsed
    window: BenchmarkWindow | None = None
    ceiling_json = _load_ceiling_json(args.ceilings_json) if args.ceilings_json else None
    if args.limit is None and ceiling_json is not None and ceiling_json.get("window"):
        window = _window_from_dict(ceiling_json["window"])
        entries = window.entries(parsed)
    elif args.limit is None:
        app_log = Path(args.app_log) if args.app_log else Path(args.trace_files[0]).with_name("app_log")
        metrics = parse_app_log_metrics(app_log)
        if metrics is not None:
            window = detect_benchmark_window(parsed, metrics)
            if window is not None:
                entries = window.entries(parsed)

    ceiling_rows = ceiling_json["rows"] if ceiling_json is not None else _ceiling_rows(entries)
    ceilings = {
        (row["issue_width"], row["lookahead_window"], row["same_cycle_forwarding"]): row
        for row in ceiling_rows
    }
    data.update(
        {
            "generated_at_unix": time.time(),
            "trace_files": [str(Path(path).resolve()) for path in args.trace_files],
            "instructions": len(entries),
            "window": _window_dict(window),
            "rtl_dual_cycles": COREMARK_DUAL_CYCLES,
            "rtl_dual_ipc": COREMARK_DUAL_IPC,
            "model_baseline_error_floor": MODEL_BASELINE_ERROR,
            "ceilings": list(ceilings.values()),
        }
    )
    existing = {row["name"]: row for row in data.get("experiments", [])}
    results: list[dict[str, Any]] = []
    baseline_cycles = 0
    baseline_ipc = 0.0
    only = set(_split_csv(args.only))
    for spec in _route_specs():
        if only and spec["name"] not in only:
            continue
        if args.resume and spec["name"] in existing:
            result = existing[spec["name"]]
            print(f"skipping {spec['name']} (resume)", file=sys.stderr, flush=True)
        else:
            result = _run_one(spec, entries, args.repo_root)
        if spec["name"] == "baseline":
            baseline_cycles = int(result.get("model_cycles") or 0)
            baseline_ipc = float(result.get("model_ipc") or 0.0)
        if baseline_ipc and result.get("model_ipc") is not None:
            result["delta_ipc"] = result["model_ipc"] - baseline_ipc
            result["delta_ipc_pct"] = result["delta_ipc"] / baseline_ipc
            result["delta_cycles"] = result["model_cycles"] - baseline_cycles
            result["below_model_resolution"] = abs(result["delta_ipc_pct"]) < MODEL_BASELINE_ERROR
        width = int(spec.get("ceiling_width", 2))
        lookahead = int(spec.get("ceiling_window", spec["overrides"].get("pairing_window", 2)))
        ceiling = ceilings.get((width, lookahead, False))
        if ceiling and result.get("model_ipc") is not None:
            result["ceiling_ipc"] = ceiling["ipc"]
            result["fraction_of_ceiling"] = result["model_ipc"] / ceiling["ipc"]
        results.append(result)
        data["experiments"] = results
        output_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _ceiling_rows(entries: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for width in (2, 3, 4):
        for window in (2, 3, 4, 6, 8):
            if window < width:
                continue
            for forwarding in (False, True):
                row = dependence_ceiling(
                    entries,
                    issue_width=width,
                    lookahead_window=window,
                    same_cycle_forwarding=forwarding,
                )
                row.update(
                    {
                        "issue_width": width,
                        "lookahead_window": window,
                        "same_cycle_forwarding": forwarding,
                    }
                )
                rows.append(row)
    return rows


def _route_specs() -> list[dict[str, Any]]:
    specs = [
        _spec("baseline", "Baseline", merged(), "Delivered dual model"),
        _spec("A_intra_alu_forwarding", "A: intra-bundle ALU forwarding", merged(E_INTRA_ALU_FORWARDING), "RAW-pair relief"),
        _spec("B_decouple_lockstep", "B: decouple lockstep readiness", merged(B_DECOUPLE_LOCKSTEP), "Per-slot readiness"),
        _spec("C_independent_retire", "C: independent retire", merged(D_INDEPENDENT_RETIRE), "Retire slots independently"),
        _spec("D_symmetric_slots", "D: symmetric slots", merged(G_SYMMETRIC_SLOTS), "Remove slot-0 scarce-FU placement"),
        _spec("E_second_memory_port", "E: second memory port", merged(A_SECOND_MEMORY), "Allow MEM+MEM"),
        _spec("branch_relax", "Relax branch next-PC stall", merged(F_RELAX_BRANCH_NEXT_PC), "Front-end control relaxation"),
        _spec("second_branch", "Second branch unit", merged(H_SECOND_BRANCH), "Allow branch+branch"),
    ]
    for window in (3, 4, 6, 8):
        cfg = merged({"pairing_window": window, "isb_s1s2": max(6, window)})
        specs.append(_spec(f"F_lookahead_w{window}", f"F: lookahead window {window}", cfg, "Pick second slot from a window"))

    stacks = {
        "stack_w4_intra": merged({"pairing_window": 4}, E_INTRA_ALU_FORWARDING),
        "stack_w4_intra_branch": merged({"pairing_window": 4}, E_INTRA_ALU_FORWARDING, H_SECOND_BRANCH),
        "stack_w4_intra_mem_branch": merged({"pairing_window": 4}, E_INTRA_ALU_FORWARDING, A_SECOND_MEMORY, H_SECOND_BRANCH),
        "stack_w4_decoupled_intra_branch": merged(
            {"pairing_window": 4}, B_DECOUPLE_LOCKSTEP, E_INTRA_ALU_FORWARDING, H_SECOND_BRANCH
        ),
        "stack_w8_intra_branch": merged(
            {"pairing_window": 8, "isb_s1s2": 8}, E_INTRA_ALU_FORWARDING, H_SECOND_BRANCH
        ),
        "stack_w8_full": merged(
            {"pairing_window": 8, "isb_s1s2": 8},
            E_INTRA_ALU_FORWARDING,
            H_SECOND_BRANCH,
            A_SECOND_MEMORY,
            G_SYMMETRIC_SLOTS,
            B_DECOUPLE_LOCKSTEP,
            D_INDEPENDENT_RETIRE,
        ),
    }
    for name, cfg in stacks.items():
        specs.append(_spec(name, name.replace("_", " "), cfg, "Measured stack, not summed"))
    return specs


def _spec(name: str, label: str, overrides: dict[str, Any], hypothesis: str) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "overrides": overrides,
        "hypothesis": hypothesis,
        "ceiling_width": 2,
        "ceiling_window": int(overrides.get("pairing_window", 2)),
    }


def _run_one(spec: dict[str, Any], entries: list[Any], repo_root: str | Path) -> dict[str, Any]:
    print(f"running {spec['name']}...", file=sys.stderr, flush=True)
    started = time.perf_counter()
    try:
        model = Model.from_repo(repo_root, **spec["overrides"])
        cycles = model.run(entries)
        total_cycles = cycles[-1] - cycles[0] + 1 if cycles else 0
        ipc = len(entries) / total_cycles if total_cycles else 0.0
        profile = model.counter_profile(total_cycles)
        return {
            "name": spec["name"],
            "label": spec["label"],
            "hypothesis": spec["hypothesis"],
            "overrides": spec["overrides"],
            "instructions": len(entries),
            "model_cycles": total_cycles,
            "model_ipc": ipc,
            "profile": profile,
            "elapsed_seconds": time.perf_counter() - started,
            "status": "ok",
        }
    except Exception as exc:  # noqa: BLE001 - the report needs non-draining configs recorded.
        return {
            "name": spec["name"],
            "label": spec["label"],
            "hypothesis": spec["hypothesis"],
            "overrides": spec["overrides"],
            "model_cycles": None,
            "model_ipc": None,
            "profile": {},
            "elapsed_seconds": time.perf_counter() - started,
            "status": "failed",
            "error": str(exc),
        }


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"experiments": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_ceiling_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _window_from_dict(data: dict[str, Any]) -> BenchmarkWindow:
    return BenchmarkWindow(
        start_index=int(data["start_index"]),
        end_index=int(data["end_index"]),
        start_mcycle_index=-1,
        end_mcycle_index=-1,
        start_minstret_index=None,
        end_minstret_index=None,
        measured_cycles=int(data["measured_cycles"]),
        measured_instructions=int(data["measured_instructions"]),
        runs=data.get("runs"),
    )


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


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
