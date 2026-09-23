#!/usr/bin/env python3
"""Count trace PCs that fall inside selected ELF symbols."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess


PC_RE = re.compile(
    r"^(?:cycle\s+\d+\s+)?core\s+\d+:\s+\S+\s+"
    r"(?P<pc>0x[0-9a-fA-F]+)\s+\(0x[0-9a-fA-F]+\)"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--prefix", default="bionic_")
    parser.add_argument("--nm", default="riscv64-linux-gnu-nm")
    args = parser.parse_args()

    output = subprocess.check_output(
        [args.nm, "-S", "--defined-only", str(args.elf)], text=True
    )
    ranges: list[tuple[int, int, str]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[3].startswith(args.prefix):
            continue
        start, size = int(fields[0], 16), int(fields[1], 16)
        ranges.append((start, start + size, fields[3]))

    by_symbol = {name: 0 for _, _, name in ranges}
    total = selected = 0
    with args.trace.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = PC_RE.match(line.strip())
            if not match:
                continue
            total += 1
            pc = int(match.group("pc"), 16)
            for start, end, name in ranges:
                if start <= pc < end:
                    by_symbol[name] += 1
                    selected += 1
                    break

    print(f"trace instructions: {total}")
    print(f"{args.prefix} instructions: {selected} ({100.0 * selected / max(1, total):.2f}%)")
    for name, count in sorted(by_symbol.items()):
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
