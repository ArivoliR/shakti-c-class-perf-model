"""Diagnose dual-issue pair-formation defects against the RTL trace.

The dual model commits 1.84% more cycles than the RTL, which means it forms
fewer pairs than the hardware does. Delta-t mismatch grouping points at
compressed/uncompressed boundaries (c.addiw->addw alone is 236k mismatches),
but "points at" is not a diagnosis.

This measures it directly. For every adjacent instruction pair it compares
"did these two retire in the same cycle" between RTL and model, then groups the
disagreements by instruction-width combination, PC alignment, and opcode pair.
A width/alignment class where the RTL pairs and the model does not is a
front-end fetch-alignment defect; one where both pair at similar rates is not.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import Model
from trace import detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files

DUAL = Path(__file__).resolve().parent.parent.parent / "c-class-dual-issue"
TRACE = DUAL / "benchmarks" / "output" / "coremark40"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1_000_000)
    ap.add_argument("--top", type=int, default=14)
    args = ap.parse_args()

    entries = load_or_parse_trace_files([str(TRACE / "rtl.dump")], limit=args.limit)
    metrics = parse_app_log_metrics(TRACE / "app_log")
    window = detect_benchmark_window(entries, metrics) if metrics else None
    if window is not None:
        sub = window.entries(entries)
        if sub:
            entries = sub
    print(f"{len(entries):,} instructions\n", flush=True)

    # Mirror exactly what the --dual-issue CLI flag sets. Setting only
    # num_issue/dual_policy leaves the stage widths and ISB depths at their
    # single-issue values, which deadlocks the model (it cannot drain).
    model = Model.from_repo(
        DUAL,
        num_issue=2,
        dual_policy="shakti",
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
        model_fetch_word_alignment=True,
        lockstep_bundles=True,
        atomic_pair_retire=True,
    )
    cycles = model.run(entries)

    rtl_pair = model_pair = 0
    by_width: dict[tuple, list[int]] = {}
    by_align: dict[tuple, list[int]] = {}
    opc: Counter = Counter()

    for i in range(1, len(entries)):
        r_same = entries[i].cycle == entries[i - 1].cycle
        m_same = cycles[i] == cycles[i - 1]
        rtl_pair += r_same
        model_pair += m_same

        prev, cur = entries[i - 1], entries[i]
        w = (prev.insn.length, cur.insn.length)
        # does the pair straddle a 64-bit (8-byte) fetch word?
        straddle = (prev.pc >> 3) != (cur.pc >> 3)
        key_a = (w, straddle)

        for d, k in ((by_width, w), (by_align, key_a)):
            s = d.setdefault(k, [0, 0, 0])
            s[0] += 1
            s[1] += r_same
            s[2] += m_same
        if r_same and not m_same:
            opc[(prev.insn.name, cur.insn.name)] += 1

    n = len(entries) - 1
    # The two metrics that actually decide whether a change is an improvement.
    dt_match = sum(
        1
        for i in range(1, len(entries))
        if (cycles[i] - cycles[i - 1]) == (entries[i].cycle - entries[i - 1].cycle)
    )
    rtl_span = entries[-1].cycle - entries[0].cycle + 1
    mdl_span = cycles[-1] - cycles[0] + 1
    print(f"dt accuracy:            {dt_match/n:.4%}")
    print(f"cycles:                 RTL {rtl_span:,}   model {mdl_span:,}"
          f"   ({(mdl_span-rtl_span)/rtl_span:+.3%})")
    print(f"same-cycle pair rate:   RTL {rtl_pair/n:6.2%}   model {model_pair/n:6.2%}"
          f"   (model forms {model_pair-rtl_pair:+,} vs RTL)\n")

    print("by instruction-width pair (bytes):")
    print(f"  {'widths':>10} {'count':>10} {'RTL pair':>9} {'model':>9} {'gap':>8}")
    for k in sorted(by_width, key=lambda x: -by_width[x][0]):
        c, r, m = by_width[k]
        print(f"  {str(k):>10} {c:>10,} {r/c:>8.1%} {m/c:>8.1%} {(m-r)/c:>+7.1%}")

    print("\nby width pair x straddles a 64-bit fetch word:")
    print(f"  {'widths':>10} {'straddle':>9} {'count':>10} {'RTL pair':>9} {'model':>9} {'gap':>8}")
    for k in sorted(by_align, key=lambda x: -by_align[x][0])[:10]:
        c, r, m = by_align[k]
        print(f"  {str(k[0]):>10} {str(k[1]):>9} {c:>10,} {r/c:>8.1%} {m/c:>8.1%} {(m-r)/c:>+7.1%}")

    print(f"\ntop opcode pairs where RTL pairs but model does not:")
    for (a, b), v in opc.most_common(args.top):
        print(f"  {a:>12} -> {b:<12} {v:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
