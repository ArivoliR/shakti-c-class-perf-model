"""Sweep lookahead window against the pair couplings, as a grid.

Measured on the dual model, a lookahead window on its own makes things *worse*
(window 4 = 10,712,299 cycles vs 10,589,781 baseline). The hypothesis is that
this is not a weak version of the mechanism but a different one: the selector
picks a non-adjacent second instruction, yet the pair still moves in lockstep
and retires atomically, so skipping an instruction defers it without letting
anything else proceed. You pay the lookahead and collect none of it.

If that is right, window and decoupling are one change, not two, and only the
combination should show a gain. A ladder cannot tell you this -- it has to be a
grid.

Each cell also reports its fraction of the corresponding dependence-limited
ceiling, because an absolute IPC without its ceiling is not interpretable.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import itertools
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import Model
from trace import detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files

DUAL = Path(__file__).resolve().parent.parent.parent / "c-class-dual-issue"
TRACE = DUAL / "benchmarks" / "output" / "coremark40"

#: RAW-limited ceilings for a 2-wide machine at each window, from ceiling.py.
CEILING = {2: 1.6765, 3: 1.9209, 4: 1.9648, 6: 2.0000, 8: 2.0000}


def build(window: int, decouple: bool, independent_retire: bool) -> Model:
    return Model.from_repo(
        DUAL,
        num_issue=2,
        dual_policy="shakti",
        model_fetch_word_alignment=True,
        fetch_width=2,
        fetch_decode_width=2,
        decode_width=1,
        issue_width=2 if decouple else 1,
        stage4_width=2 if decouple else 1,
        commit_width=1,
        memory_issue_width=1,
        control_issue_width=1,
        isb_s0s1=4,
        isb_s1s2=6,
        isb_s2s3=2,
        isb_s3s4=16,
        isb_s4s5=16,
        lockstep_bundles=not decouple,
        atomic_pair_retire=not independent_retire,
        pairing_window=window,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=300_000)
    ap.add_argument("--windows", type=int, nargs="*", default=[2, 4])
    ap.add_argument("--output", default="results/grid_window_coupling.json")
    args = ap.parse_args()

    entries = load_or_parse_trace_files([str(TRACE / "rtl.dump")], limit=args.limit)
    metrics = parse_app_log_metrics(TRACE / "app_log")
    win = detect_benchmark_window(entries, metrics) if metrics else None
    if win is not None:
        sub = win.entries(entries)
        if sub:
            entries = sub
    n = len(entries)
    rtl_span = entries[-1].cycle - entries[0].cycle + 1
    print(f"{n:,} instructions   RTL {rtl_span:,} cycles   IPC {n/rtl_span:.4f}\n", flush=True)

    rows = []
    base = None
    print(f"{'window':>6} {'decouple':>9} {'indep.retire':>13} {'cycles':>11} "
          f"{'IPC':>7} {'vs base':>9} {'% ceiling':>10} {'dt acc':>8}")
    for window, decouple, indep in itertools.product(args.windows, (False, True), (False, True)):
        t0 = time.time()
        model = build(window, decouple, indep)
        cycles = model.run(entries)
        span = cycles[-1] - cycles[0] + 1
        ipc = n / span
        if base is None:
            base = span
        dt = sum(
            1
            for i in range(1, n)
            if (cycles[i] - cycles[i - 1]) == (entries[i].cycle - entries[i - 1].cycle)
        ) / (n - 1)
        ceil = CEILING.get(window)
        rows.append(
            {
                "window": window,
                "decouple_lockstep": decouple,
                "independent_retire": indep,
                "cycles": span,
                "ipc": ipc,
                "vs_baseline_pct": (base - span) / base * 100.0,
                "pct_of_ceiling": (ipc / ceil * 100.0) if ceil else None,
                "dt_accuracy": dt,
                "seconds": round(time.time() - t0, 1),
            }
        )
        print(f"{window:>6} {str(decouple):>9} {str(indep):>13} {span:>11,} "
              f"{ipc:>7.4f} {(base-span)/base*100:>+8.2f}% "
              f"{ipc/ceil*100 if ceil else 0:>9.1f}% {dt:>7.2%}", flush=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"instructions": n, "rtl_cycles": rtl_span, "rows": rows}, indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote {out}")

    best = max(rows, key=lambda r: r["ipc"])
    print(f"best: window {best['window']} decouple={best['decouple_lockstep']} "
          f"indep_retire={best['independent_retire']} -> IPC {best['ipc']:.4f} "
          f"({best['pct_of_ceiling']:.1f}% of ceiling)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
