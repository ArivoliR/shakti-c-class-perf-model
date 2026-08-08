"""Run the model once per design point, with no per-point tuning.

Each prediction is produced by ``Model.from_makefile(<variant snapshot>)`` and
nothing else. Every calibrated constant keeps the value it was given during
CoreMark calibration. That restriction is the entire experiment: a model whose
constants are re-fitted per design point has not predicted anything.

The instruction stream is invariant across these design points (they are all
microarchitectural), so a point's own Dhrystone trace serves as both the model
input and the Delta-t ground truth. The harness asserts stream identity against
the baseline and reports any point where it does not hold.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from accuracy import compute_accuracy  # noqa: E402
from model import Model  # noqa: E402
from trace import detect_benchmark_window, has_cycle_stamps, parse_app_log_metrics  # noqa: E402
from trace_cache import load_or_parse_trace_files  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def stream_fingerprint(entries) -> str:
    """Hash the architectural instruction stream, ignoring cycle stamps."""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(f"{entry.pc:x}:{entry.encoding:x}\n".encode())
    return digest.hexdigest()[:16]


def predict_point(name: str, benchmark: str, coremark_trace: list[str] | None) -> dict | None:
    point_dir = RUNS / name
    result_path = point_dir / "result.json"
    if not result_path.exists():
        return None
    measured = json.loads(result_path.read_text())
    if measured.get("status") != "ok":
        return {"name": name, "status": measured.get("status", "missing")}

    bench = measured.get("benchmarks", {}).get(benchmark)
    if not bench or bench.get("status") != "ok":
        return {"name": name, "status": f"{benchmark}_missing"}

    makefile = point_dir / "makefile.inc"
    record: dict = {
        "name": name,
        "status": "ok",
        "benchmark": benchmark,
        "rtl_cycles": bench["cycles"],
        "rtl_instret": bench.get("instret"),
    }

    if benchmark == "dhrystone" and bench.get("traces"):
        trace_files = [str(point_dir / t) for t in bench["traces"]]
    elif coremark_trace:
        trace_files = coremark_trace
    else:
        return {"name": name, "status": "no_trace"}

    entries = load_or_parse_trace_files(trace_files)
    metrics_dir = RUNS / "baseline" if benchmark == "coremark" and coremark_trace else point_dir
    metrics = parse_app_log_metrics(metrics_dir / f"{benchmark}-app_log")
    window = detect_benchmark_window(entries, metrics) if metrics else None
    if window is not None:
        entries = window.entries(entries)
        record["window"] = [window.start_index, window.end_index]
    record["instructions"] = len(entries)
    record["stream_fingerprint"] = stream_fingerprint(entries)

    model = Model.from_makefile(makefile)
    cycles = model.run(entries)
    record["model_cycles"] = cycles[-1] - cycles[0] + 1
    record["model_params"] = {
        k: v for k, v in model.params.items() if k != "self" and not isinstance(v, dict)
    }

    if has_cycle_stamps(entries) and (benchmark != "coremark" or name == "baseline"):
        accuracy = compute_accuracy(entries, cycles)
        if accuracy.accuracy is not None:
            record["delta_t_accuracy"] = accuracy.accuracy
            record["delta_t_matches"] = accuracy.matches
            record["delta_t_compared"] = accuracy.compared
    elif benchmark == "coremark" and coremark_trace and name != "baseline":
        record["delta_t_accuracy_note"] = (
            "not computed: CoreMark variants use the baseline archived trace; "
            "per-point CoreMark commit traces are not archived"
        )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", default="dhrystone", choices=("dhrystone", "coremark"))
    parser.add_argument(
        "--coremark-trace",
        nargs="*",
        default=None,
        help="baseline CoreMark trace files; required for --benchmark coremark, since "
        "per-point CoreMark traces are not archived (~1 GB each)",
    )
    parser.add_argument("--points", nargs="*", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    names = args.points or sorted(p.name for p in RUNS.iterdir() if (p / "result.json").exists())
    if not names:
        print(f"no completed design points under {RUNS}; run rtl_sweep.py first")
        return 1

    records = []
    baseline_fp = None
    for name in names:
        record = predict_point(name, args.benchmark, args.coremark_trace)
        if record is None:
            continue
        if record["status"] != "ok":
            print(f"{name:<12} skipped ({record['status']})", flush=True)
            records.append(record)
            continue
        if name == "baseline":
            baseline_fp = record["stream_fingerprint"]
        accuracy = record.get("delta_t_accuracy")
        acc_text = f"  dt_acc={accuracy:.4%}" if accuracy is not None else ""
        print(
            f"{name:<12} rtl={record['rtl_cycles']:>12,}  model={record['model_cycles']:>12,}"
            f"  err={(record['model_cycles']-record['rtl_cycles'])/record['rtl_cycles']*100:+7.3f}%"
            f"{acc_text}",
            flush=True,
        )
        records.append(record)

    if baseline_fp:
        drifted = [
            r["name"]
            for r in records
            if r.get("stream_fingerprint") and r["stream_fingerprint"] != baseline_fp
        ]
        if drifted:
            print(
                "\nWARNING: instruction stream differs from baseline for: "
                + ", ".join(drifted)
                + "\nThese points changed architectural behaviour, not just timing. "
                "Their deltas are not pure microarchitecture measurements."
            )

    output = Path(args.output or RUNS / f"predictions-{args.benchmark}.json")
    output.write_text(
        json.dumps({"benchmark": args.benchmark, "points": records}, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
