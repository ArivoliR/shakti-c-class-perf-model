"""How much of the MEM+MEM hazard would a banked D-cache actually recover?

The dual-issue core cannot co-issue two memory operations: `dcache1rw` has a
single core port, and MEM+MEM is the largest pairing-reject class in several
benchmarks. A true second port means dual-ported SRAM, which is expensive.

The cheaper alternative is banking: keep one physical port per bank, split the
cache into N banks by address, and let two accesses proceed together whenever
they fall in *different* banks. That is a logical dual port built from
single-ported memories. Its whole value depends on one empirical quantity --
how often two adjacent memory operations land in different banks -- and that is
measurable from the commit trace, which already records effective addresses.
No RTL required.

Bank-selection functions matter and are not interchangeable. Word interleaving
(low address bits) distributes sequential access well but aliases on strides
that are a multiple of the bank stride; line interleaving does the opposite.
This sweeps several so the choice is made on data.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import argparse
import json
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

LINE = re.compile(rb"^cycle (\d+) core\s+0: \d+ 0x([0-9a-f]+) \(0x([0-9a-f]+)\)(.*)$")
MEM = re.compile(rb" mem 0x([0-9a-f]+)")

#: (name, shift, banks) -- bank = (addr >> shift) % banks
SCHEMES = [
    ("word-2banks  (addr[3])", 3, 2),
    ("2word-2banks (addr[4])", 4, 2),
    ("line-2banks  (addr[6])", 6, 2),
    ("word-4banks  (addr[4:3])", 3, 4),
    ("2word-4banks (addr[5:4])", 4, 4),
    ("line-4banks  (addr[7:6])", 6, 4),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace")
    ap.add_argument("--limit", type=int, default=3_000_000)
    ap.add_argument("--label", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    prev_addr = None
    prev_was_mem = False
    n = 0
    mem_ops = 0
    mem_mem_pairs = 0
    conflict = Counter()
    same_line = 0

    with open(args.trace, "rb") as fh:
        for raw in fh:
            m = LINE.match(raw)
            if not m:
                continue
            n += 1
            hit = MEM.search(m.group(4))
            addr = int(hit.group(1), 16) if hit else None
            if addr is not None:
                mem_ops += 1
            if prev_was_mem and addr is not None:
                mem_mem_pairs += 1
                if (prev_addr >> 6) == (addr >> 6):
                    same_line += 1
                same_line_pair = (prev_addr >> 6) == (addr >> 6)
                for name, shift, banks in SCHEMES:
                    if ((prev_addr >> shift) % banks) == ((addr >> shift) % banks):
                        conflict[name] += 1
                        # Two accesses to the SAME line always land in the same
                        # bank, so banking can never help them -- but one wide
                        # read can serve both. Line merging is therefore
                        # complementary to banking, not an alternative, and it
                        # covers exactly the pairs banking cannot.
                        if not same_line_pair:
                            conflict[name + "|merge"] += 1
            prev_addr, prev_was_mem = addr, addr is not None
            if n >= args.limit:
                break

    label = args.label or Path(args.trace).parent.name
    print(f"{label}: {n:,} instructions, {mem_ops:,} memory ops ({mem_ops/n:.1%})")
    print(f"adjacent MEM+MEM pairs: {mem_mem_pairs:,} ({mem_mem_pairs/n:.2%} of instructions)")
    if not mem_mem_pairs:
        return 0
    print(f"  ...of which hit the SAME 64 B line: {same_line:,} ({same_line/mem_mem_pairs:.1%})\n")

    print(f"  {'bank scheme':>26} {'conflict':>9} {'co-issuable':>12} {'+line-merge':>12}")
    rows = {}
    for name, _, _ in SCHEMES:
        c = conflict[name]
        ok = mem_mem_pairs - c
        cm = conflict[name + "|merge"]
        okm = mem_mem_pairs - cm
        rows[name] = {"conflicts": c, "co_issuable": ok, "rate": ok / mem_mem_pairs,
                      "rate_with_line_merge": okm / mem_mem_pairs}
        print(f"  {name:>26} {c/mem_mem_pairs:>8.1%} {ok/mem_mem_pairs:>11.1%} {okm/mem_mem_pairs:>11.1%}")

    best = max(rows.items(), key=lambda kv: kv[1]["rate_with_line_merge"])
    print(f"\nbest: {best[0].strip()} recovers {best[1]['rate']:.1%} of MEM+MEM pairs, "
          f"{best[1]['rate_with_line_merge']:.1%} with same-line merging")
    print("A true dual port would recover 100% by definition, so this is the")
    print("fraction of the second-port benefit a banked design captures.")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "label": label,
                    "instructions": n,
                    "memory_ops": mem_ops,
                    "mem_mem_pairs": mem_mem_pairs,
                    "same_line_pairs": same_line,
                    "schemes": rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
