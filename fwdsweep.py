"""Does restricted ALU-to-ALU forwarding keep the IPC that full forwarding buys?

Full forwarding measured 1.84x logic depth in synthesis, against a frequency
budget of 1.056x -- so it cannot be built at 1.00 ns. Restricting it to pairs
where at least one end has no carry chain measured 1.10x. That is only worth
anything if the restriction is cheap in IPC, which this measures.

Run over four disjoint windows rather than one, because a single window's delta
on a 6% effect is not separable from where the window happens to land.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import Model
from trace_cache import load_or_parse_trace_files

DUAL = Path(__file__).resolve().parent.parent.parent / "c-class-dual-issue"


def build(**kw) -> Model:
    return Model.from_repo(
        DUAL, num_issue=2, dual_policy="shakti",
        model_fetch_word_alignment=True,
        fetch_width=2, fetch_decode_width=2, decode_width=1,
        issue_width=1, stage4_width=1, commit_width=1,
        memory_issue_width=1, control_issue_width=1,
        isb_s0s1=4, isb_s1s2=6, isb_s2s3=2, isb_s3s4=16, isb_s4s5=16,
        lockstep_bundles=True, atomic_pair_retire=True, **kw)


CONFIGS = [
    ("baseline",              {}),
    ("full forwarding",       dict(intra_bundle_forwarding=True)),
    ("restricted (no add>add)", dict(intra_bundle_forwarding=True,
                                     forwarding_restrict_double_add=True)),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", default="coremark40")
    ap.add_argument("--window", type=int, default=400_000)
    ap.add_argument("--nwindows", type=int, default=4)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    trace = DUAL / "benchmarks" / "output" / args.bench / "rtl.dump"
    total = args.window * args.nwindows
    entries = load_or_parse_trace_files([str(trace)], limit=total)
    print(f"{args.bench}: {len(entries):,} instructions, "
          f"{args.nwindows} x {args.window:,}\n", flush=True)

    print(f"{'window':>7} " + " ".join(f"{n:>24}" for n, _ in CONFIGS))
    acc = {n: [] for n, _ in CONFIGS}
    for w in range(args.nwindows):
        chunk = entries[w * args.window:(w + 1) * args.window]
        if len(chunk) < args.window // 2:
            break
        spans = {}
        for name, kw in CONFIGS:
            c = build(**kw).run(chunk)
            spans[name] = c[-1] - c[0] + 1
        base = spans["baseline"]
        cells = []
        for name, _ in CONFIGS:
            gain = (base - spans[name]) / base * 100.0
            acc[name].append(gain)
            cells.append(f"{spans[name]:>10,} {gain:>+6.2f}%" if name != "baseline"
                         else f"{spans[name]:>10,}        ")
        print(f"{w:>7} " + " ".join(f"{c:>24}" for c in cells), flush=True)

    print()
    out = {}
    for name, _ in CONFIGS:
        if name == "baseline":
            continue
        v = acc[name]
        mean = sum(v) / len(v)
        out[name] = {"per_window": v, "mean_pct": mean}
        print(f"  {name:<26} mean {mean:>+6.3f}%   "
              f"(min {min(v):+.2f}  max {max(v):+.2f})")
    if out.get("full forwarding", {}).get("mean_pct"):
        keep = out["restricted (no add>add)"]["mean_pct"] / out["full forwarding"]["mean_pct"]
        print(f"\n  restricted keeps {keep:.1%} of the full-forwarding gain, "
              f"for 1.10x depth instead of 1.84x")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
