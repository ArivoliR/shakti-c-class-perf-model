"""Attribute every dual-issue cycle to exactly one bucket.

The core reaches only 69.6% of its dependence-limited ceiling at window 2, and
widening the issue window barely helps (+0.87%). That says the binding
constraint is not pair selection. But "69.6% of ceiling" does not tell you what
to build; a budget that sums to 100% does.

Buckets, in pipeline order so a cycle is blamed on the earliest thing that
explains it:

  retired_full      retired num_issue instructions -- the machine did its job
  retired_partial   retired some but not all slots
  flush_redirect    nothing retired, a redirect or flush was in progress
  frontend_starved  nothing retired, execute had no input and neither did the
                    front-end queues
  decode_blocked    nothing retired, execute had no input but the front end did
  backpressure      nothing retired, a downstream ISB was full
  execute_blocked   nothing retired, execute had work it could not start
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import Model
from trace import detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files

DUAL = Path(__file__).resolve().parent.parent.parent / "c-class-dual-issue"
TRACE = DUAL / "benchmarks" / "output" / "coremark40"
CEILING_W2 = 1.6765


def build(**over) -> Model:
    base = dict(
        num_issue=2,
        dual_policy="shakti",
        model_fetch_word_alignment=True,
        fetch_width=2,
        fetch_decode_width=2,
        decode_width=1,
        issue_width=1,
        stage4_width=1,
        commit_width=1,
        memory_issue_width=1,
        control_issue_width=1,
        isb_s0s1=4,
        isb_s1s2=6,
        isb_s2s3=2,
        isb_s3s4=16,
        isb_s4s5=16,
        lockstep_bundles=True,
        atomic_pair_retire=True,
    )
    base.update(over)
    return Model.from_repo(DUAL, **base)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=300_000)
    ap.add_argument("--output", default="results/cycle_account.json")
    args = ap.parse_args()

    entries = load_or_parse_trace_files([str(TRACE / "rtl.dump")], limit=args.limit)
    metrics = parse_app_log_metrics(TRACE / "app_log")
    win = detect_benchmark_window(entries, metrics) if metrics else None
    if win is not None:
        sub = win.entries(entries)
        if sub:
            entries = sub
    n = len(entries)

    model = build()
    cycles = model.run(entries)
    span = cycles[-1] - cycles[0] + 1
    ipc = n / span

    print(f"{n:,} instructions   model {span:,} cycles   IPC {ipc:.4f}"
          f"   {ipc/CEILING_W2:.1%} of the window-2 ceiling ({CEILING_W2})\n")

    rows = model.cycle_account_table()
    total = sum(v for _, v, _ in rows)
    print(f"{'bucket':>18} {'cycles':>12} {'share':>8}")
    for name, count, share in rows:
        print(f"{name:>18} {count:>12,} {share:>7.2%}")
    print(f"{'TOTAL':>18} {total:>12,} {'100.00%':>8}")
    if total != span:
        print(f"\nNOTE: accounted cycles {total:,} != span {span:,} "
              f"(difference {total-span:+,}; the model counts loop iterations, "
              f"the span is first-to-last commit)")

    prof = model.counter_profile(span)
    print("\nrelated counters:")
    for k in ("st3_not_firing", "st3_blocked", "raw_hazard", "mem_mem_hazard",
              "one_instr", "mispredict", "dual_issued"):
        if k in prof:
            print(f"  {k:>16} {prof[k]:>12,}")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(
            {
                "instructions": n,
                "model_cycles": span,
                "ipc": ipc,
                "pct_of_window2_ceiling": ipc / CEILING_W2,
                "buckets": {name: count for name, count, _ in rows},
                "counters": {k: v for k, v in prof.items() if isinstance(v, (int, float))},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
