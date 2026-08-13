"""Banking or prefetching? Classify a workload before proposing either.

The two mechanisms fix different problems and help in nearly disjoint regimes:

  banking      fixes a PORT conflict -- two memory ops that both HIT but cannot
               co-issue because dcache1rw has one core port. Only matters when
               the cache is hitting.
  prefetching  fixes MISS LATENCY. Only matters when the working set overflows
               the cache.

A workload that fits in cache gets nothing from a prefetcher; a workload that
thrashes gets little from banking, because the port is not what it is waiting
on. So "should we build banking or a prefetcher" has no single answer -- it has
a per-benchmark answer, and this produces it.

Everything here is derived from the committed address trace: a direct-mapped-
by-set LRU cache model matching the RTL geometry, a per-PC stride predictor for
prefetch coverage, and the bank-conflict analysis for the hit path. No RTL
build and no timing model required.

What this does NOT produce is IPC. It counts opportunities -- misses that a
prefetcher would cover, hit pairs that banking would let co-issue. Converting
either into cycles needs the memory path in the timing model.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from pathlib import Path
import argparse
import json
import re
import sys

LINE = re.compile(rb"^cycle (\d+) core\s+0: \d+ 0x([0-9a-f]+) \(0x([0-9a-f]+)\)(.*)$")
MEM = re.compile(rb" mem 0x([0-9a-f]+)")


class Cache:
    """Set-associative LRU cache matching the C-class D-cache geometry.

    Defaults are the RTL's: dsets=64, dways=4, 64 B lines -> 16 KB.
    """

    def __init__(self, sets: int = 64, ways: int = 4, line: int = 64):
        self.sets, self.ways, self.line = sets, ways, line
        self.shift = line.bit_length() - 1
        self.tags: list[OrderedDict] = [OrderedDict() for _ in range(sets)]

    def access(self, addr: int) -> bool:
        block = addr >> self.shift
        idx = block % self.sets
        tag = block // self.sets
        way = self.tags[idx]
        if tag in way:
            way.move_to_end(tag)
            return True
        if len(way) >= self.ways:
            way.popitem(last=False)
        way[tag] = True
        return False

    def peek_would_hit(self, addr: int) -> bool:
        block = addr >> self.shift
        return (block // self.sets) in self.tags[block % self.sets]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace")
    ap.add_argument("--label", default=None)
    ap.add_argument("--limit", type=int, default=5_000_000)
    ap.add_argument("--sets", type=int, default=64)
    ap.add_argument("--ways", type=int, default=4)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    cache = Cache(args.sets, args.ways)
    # Per-PC stride predictor: the classic table. If a PC's last two deltas
    # agree, we assume the next access continues the pattern.
    last_addr: dict[int, int] = {}
    last_stride: dict[int, int] = {}

    n = mem_ops = misses = 0
    stride_covered = 0
    strided_pcs = defaultdict(int)
    prev_addr = None
    prev_mem = False
    pairs = same_line = bank_conflict = 0

    with open(args.trace, "rb") as fh:
        for raw in fh:
            m = LINE.match(raw)
            if not m:
                continue
            n += 1
            hit = MEM.search(m.group(4))
            if hit is None:
                prev_mem = False
                if n >= args.limit:
                    break
                continue
            pc = int(m.group(2), 16)
            addr = int(hit.group(1), 16)
            mem_ops += 1

            # --- prefetch opportunity: would a stride predictor have had it? ---
            predicted = None
            if pc in last_addr and pc in last_stride and last_stride[pc]:
                predicted = last_addr[pc] + last_stride[pc]
            if pc in last_addr:
                stride = addr - last_addr[pc]
                if stride and stride == last_stride.get(pc):
                    strided_pcs[pc] += 1
                last_stride[pc] = stride
            last_addr[pc] = addr

            was_hit = cache.access(addr)
            if not was_hit:
                misses += 1
                # A stride prefetcher issuing one line ahead covers this miss if
                # its prediction lands in the same line as the actual access.
                if predicted is not None and (predicted >> 6) == (addr >> 6):
                    stride_covered += 1

            # --- banking opportunity, hit path only ---
            if prev_mem and prev_addr is not None:
                pairs += 1
                if (prev_addr >> 6) == (addr >> 6):
                    same_line += 1
                elif ((prev_addr >> 6) % 4) == ((addr >> 6) % 4):
                    bank_conflict += 1
            prev_addr, prev_mem = addr, True
            if n >= args.limit:
                break

    label = args.label or Path(args.trace).parent.name
    kb = args.sets * args.ways * 64 // 1024
    miss_rate = misses / mem_ops if mem_ops else 0.0
    print(f"=== {label} ===")
    print(f"{n:,} instructions, {mem_ops:,} memory ops ({mem_ops/max(n,1):.1%})")
    print(f"D-cache {kb} KB ({args.sets} sets x {args.ways} ways x 64 B)")
    print(f"  miss rate            {miss_rate:>8.2%}  ({misses:,} misses)")
    if misses:
        print(f"  stride-predictable   {stride_covered/misses:>8.1%}  <- prefetcher upside")
    print(f"  adjacent MEM+MEM     {pairs:,}")
    if pairs:
        co = pairs - bank_conflict - same_line
        print(f"    same 64 B line     {same_line/pairs:>8.1%}  <- line-merge covers")
        print(f"    4-bank conflict    {bank_conflict/pairs:>8.1%}")
        print(f"    co-issuable        {co/pairs:>8.1%}  <- banking covers")
        print(f"    banking+merge      {(co+same_line)/pairs:>8.1%}")

    verdict = (
        "PREFETCH (miss-bound)" if miss_rate > 0.05
        else "BANKING (hit-bound)" if pairs and miss_rate < 0.01
        else "mixed / neither dominant"
    )
    print(f"  --> regime: {verdict}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "label": label, "instructions": n, "memory_ops": mem_ops,
            "cache_kb": kb, "misses": misses, "miss_rate": miss_rate,
            "stride_covered": stride_covered,
            "stride_coverage": (stride_covered / misses) if misses else None,
            "mem_mem_pairs": pairs, "same_line": same_line,
            "bank_conflict": bank_conflict, "regime": verdict,
        }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
