"""What would a 512 KB 16-way L2 buy?

Two quantities decide this, and only one of them is a modelling choice:

  1. How many L1 misses would an L2 catch?   -- measurable from the trace
  2. What does each caught miss save?        -- (L1 miss penalty) - (L2 latency)

(2) is where estimates usually go wrong, so this measures the miss penalty
empirically instead of assuming it. For every memory instruction the L1 model
says missed, we look at the actual inter-commit gap in the RTL trace and
compare it against the gap for hits. The difference is what a miss really costs
*in this system*, including whatever the simulated main memory happens to be.

That last point matters here: `test_soc/Soc.bsv` instantiates main memory as
`mkbram_axi4` over `mkBRAMCore2BE` -- a BRAM, not a DRAM model. If the measured
penalty is small, an L2 cannot help much no matter how high its hit rate,
because there is little latency to remove. Reporting an L2 speedup without
checking this would be measuring an assumption rather than the machine.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import argparse
import json
import re
import statistics
import sys

LINE = re.compile(rb"^cycle (\d+) core\s+0: \d+ 0x([0-9a-f]+) \(0x([0-9a-f]+)\)(.*)$")
MEM = re.compile(rb" mem 0x([0-9a-f]+)")


class Cache:
    def __init__(self, sets: int, ways: int, line: int = 64):
        self.sets, self.ways = sets, ways
        self.shift = line.bit_length() - 1
        self.tags = [OrderedDict() for _ in range(sets)]

    def access(self, addr: int) -> bool:
        blk = addr >> self.shift
        idx, tag = blk % self.sets, blk // self.sets
        way = self.tags[idx]
        if tag in way:
            way.move_to_end(tag)
            return True
        if len(way) >= self.ways:
            way.popitem(last=False)
        way[tag] = True
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace")
    ap.add_argument("--label", default=None)
    ap.add_argument("--limit", type=int, default=4_000_000)
    ap.add_argument("--l1-sets", type=int, default=64)
    ap.add_argument("--l1-ways", type=int, default=4)
    ap.add_argument("--l2-kb", type=int, default=512)
    ap.add_argument("--l2-ways", type=int, default=16)
    ap.add_argument("--l2-latency", type=int, default=8,
                    help="cycles for an L2 hit (assumption; swept in the output)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    l2_sets = (args.l2_kb * 1024) // (64 * args.l2_ways)
    l1 = Cache(args.l1_sets, args.l1_ways)
    l2 = Cache(l2_sets, args.l2_ways)

    n = 0
    prev_cycle = None
    hit_gaps: list[int] = []
    miss_gaps: list[int] = []
    l1_misses = l2_hits = l2_misses = 0
    total_cycles = 0

    with open(args.trace, "rb") as fh:
        for raw in fh:
            m = LINE.match(raw)
            if not m:
                continue
            n += 1
            cycle = int(m.group(1))
            gap = (cycle - prev_cycle) if prev_cycle is not None else None
            prev_cycle = cycle
            hit = MEM.search(m.group(4))
            if hit is not None:
                addr = int(hit.group(1), 16)
                if l1.access(addr):
                    if gap is not None:
                        hit_gaps.append(gap)
                else:
                    l1_misses += 1
                    if gap is not None:
                        miss_gaps.append(gap)
                    if l2.access(addr):
                        l2_hits += 1
                    else:
                        l2_misses += 1
            if n >= args.limit:
                break
    total_cycles = prev_cycle

    label = args.label or Path(args.trace).parent.name
    l1_kb = args.l1_sets * args.l1_ways * 64 // 1024
    print(f"=== {label} ===")
    print(f"{n:,} instructions over {total_cycles:,} cycles")
    print(f"L1 {l1_kb} KB {args.l1_ways}-way   L2 {args.l2_kb} KB {args.l2_ways}-way "
          f"({l2_sets} sets)\n")

    if not l1_misses:
        print("no L1 misses -- an L2 has nothing to catch here.")
        return 0

    med_hit = statistics.median(hit_gaps) if hit_gaps else 0
    med_miss = statistics.median(miss_gaps) if miss_gaps else 0
    mean_hit = statistics.mean(hit_gaps) if hit_gaps else 0
    mean_miss = statistics.mean(miss_gaps) if miss_gaps else 0
    penalty = mean_miss - mean_hit

    print(f"L1 misses            {l1_misses:>10,}")
    print(f"  caught by L2       {l2_hits:>10,}  ({l2_hits/l1_misses:.1%})")
    print(f"  still miss         {l2_misses:>10,}  ({l2_misses/l1_misses:.1%})")
    print()
    print("MEASURED miss penalty (inter-commit gap, from the RTL trace):")
    print(f"  gap after an L1 hit    mean {mean_hit:6.2f}   median {med_hit}")
    print(f"  gap after an L1 miss   mean {mean_miss:6.2f}   median {med_miss}")
    print(f"  --> observed penalty   {penalty:6.2f} cycles\n")

    print(f"  {'L2 hit latency':>15} {'cycles saved':>13} {'% of runtime':>13}")
    rows = {}
    for lat in (2, 4, 8, 12, 16):
        per = max(0.0, penalty - lat)
        saved = l2_hits * per
        rows[lat] = {"per_hit": per, "saved": saved,
                     "pct": saved / total_cycles if total_cycles else 0}
        print(f"  {lat:>15} {saved:>13,.0f} {saved/total_cycles:>12.2%}")

    print("\n  Upper bound: an L2 can only remove latency that is actually there.")
    print(f"  Observed penalty is {penalty:.1f} cycles, so no L2 latency below that")
    print("  can save more than that per caught miss.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "label": label, "instructions": n, "total_cycles": total_cycles,
            "l1_kb": l1_kb, "l2_kb": args.l2_kb, "l2_ways": args.l2_ways,
            "l1_misses": l1_misses, "l2_hits": l2_hits, "l2_misses": l2_misses,
            "l2_hit_rate": l2_hits / l1_misses,
            "mean_gap_hit": mean_hit, "mean_gap_miss": mean_miss,
            "observed_penalty": penalty,
            "savings_by_l2_latency": rows,
        }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
