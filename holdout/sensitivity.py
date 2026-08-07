"""Pre-flight check: does the model actually respond to each design point?

Run this before the RTL sweep. It is cheap (a trace prefix, pure Python) and it
catches the failure mode that already burned experiments D, F and G: a
configuration knob that never reaches any logic, so the model returns a
byte-identical cycle count and the result is silently reported as "no effect".

Three outcomes per point:

  UNMAPPED  the define does not change any Model parameter, so from_makefile
            drops it. The model structurally cannot predict this point.
  DEAD      parameters change but the cycle count does not move at all. The
            parameter is not wired into the timing logic, or it is clamped.
  OK        parameters change and cycles move.

A DEAD or UNMAPPED point must be fixed or removed from the sweep; building its
RTL would produce a ground-truth delta with nothing to compare against.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from design_points import DEFAULT_ORDER, BY_NAME, DesignPoint, describe, render_variant  # noqa: E402
from model import Model  # noqa: E402
from trace_cache import load_or_parse_trace_files  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_MAKEFILE = REPO_ROOT / "makefile.inc"


def _model_for(point: DesignPoint, baseline_makefile: Path) -> tuple[Model, dict]:
    text = render_variant(baseline_makefile, point)
    with tempfile.NamedTemporaryFile("w", suffix=".inc", delete=False) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    try:
        model = Model.from_makefile(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return model, dict(model.params)


def _param_diff(base: dict, variant: dict) -> dict[str, tuple]:
    keys = set(base) | set(variant)
    return {
        key: (base.get(key), variant.get(key))
        for key in sorted(keys)
        if base.get(key) != variant.get(key) and key != "self"
    }


def _allows_exact_zero(point: DesignPoint) -> bool:
    """Return True for intentional near-null points.

    ``sensitivity.py`` is designed to catch flags that never reach timing
    logic, but the backpressure ladder deliberately includes capacities that
    should be oversized. A byte-identical prediction is useful there: it says
    the point is below the model's resolution, not that the parameter is
    disconnected.
    """
    if "backpressure" not in point.tags:
        return False
    if "inert-define" in point.tags:
        return False
    return not any(value == 1 for value in point.edits.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_files", nargs="+", help="RTL commit trace(s) to use as the instruction stream")
    parser.add_argument("--limit", type=int, default=400_000, help="instructions to model (default 400k)")
    parser.add_argument("--points", nargs="*", default=None, help="subset of design points")
    parser.add_argument("--baseline-makefile", default=str(BASELINE_MAKEFILE))
    parser.add_argument("--json", default=None, help="write results here")
    args = parser.parse_args()

    baseline_makefile = Path(args.baseline_makefile)
    names = args.points or list(DEFAULT_ORDER)
    points = [BY_NAME[name] for name in names]

    print(f"parsing {args.limit:,} instructions from {args.trace_files[0]} ...", flush=True)
    entries = load_or_parse_trace_files(args.trace_files, limit=args.limit)
    print(f"parsed {len(entries):,} instructions\n", flush=True)

    base_point = BY_NAME["baseline"]
    base_model, base_params = _model_for(base_point, baseline_makefile)
    base_cycles_list = base_model.run(entries)
    base_cycles = base_cycles_list[-1] - base_cycles_list[0] + 1
    print(f"baseline model cycles over prefix: {base_cycles:,}\n", flush=True)

    results = []
    width = max(len(p.name) for p in points)
    for point in points:
        if point.is_baseline:
            continue
        model, params = _model_for(point, baseline_makefile)
        diff = _param_diff(base_params, params)
        if not diff:
            if "inert-define" in point.tags:
                status = "OK_INERT_DEFINE"
                cycles = base_cycles
                delta = 0
            else:
                status = "UNMAPPED"
                cycles = None
                delta = None
        else:
            run = model.run(entries)
            cycles = run[-1] - run[0] + 1
            delta = cycles - base_cycles
            if delta == 0 and _allows_exact_zero(point):
                status = "OK_NEAR_NULL"
            else:
                status = "DEAD" if delta == 0 else "OK"
        pct = (delta / base_cycles * 100.0) if delta is not None and base_cycles else None
        results.append(
            {
                "name": point.name,
                "status": status,
                "edits": describe(point),
                "mechanism": point.mechanism,
                "model_params_changed": {k: list(v) for k, v in diff.items()},
                "prefix_cycles": cycles,
                "prefix_delta": delta,
                "prefix_delta_pct": pct,
            }
        )
        pct_text = f"{pct:+7.3f}%" if pct is not None else "    n/a"
        params_text = ", ".join(f"{k}:{v[0]}->{v[1]}" for k, v in diff.items()) or "(none)"
        print(f"{point.name:<{width}}  {status:<8}  {pct_text}   {params_text}", flush=True)

    bad = [r for r in results if r["status"] not in ("OK", "OK_NEAR_NULL", "OK_INERT_DEFINE")]
    print()
    if bad:
        print(f"{len(bad)} point(s) cannot be validated as written:")
        for item in bad:
            print(f"  - {item['name']}: {item['status']} ({item['edits']})")
        print("\nFix the model mapping or drop these before running the RTL sweep.")
    else:
        print("all points respond; safe to build RTL")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "limit": args.limit,
                    "baseline_prefix_cycles": base_cycles,
                    "points": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
