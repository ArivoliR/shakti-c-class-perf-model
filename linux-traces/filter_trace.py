#!/usr/bin/env python3
"""Keep parser-ready commit records, optionally between two marker opcodes."""

from __future__ import annotations

import argparse
from collections import deque
import re
import sys


TRACE_RE = re.compile(
    r"^(?:cycle\s+\d+\s+)?core\s+\d+:\s+\S+\s+"
    r"0x[0-9a-fA-F]+\s+\((?P<inst>0x[0-9a-fA-F]+)\).*$"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=lambda value: int(value, 0))
    parser.add_argument("--end", type=lambda value: int(value, 0))
    parser.add_argument(
        "--tail",
        type=int,
        default=0,
        help="write only the final N commit records from the marked region",
    )
    args = parser.parse_args()
    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be specified together")

    active = args.start is None
    saw_start = active
    saw_end = active
    kept = 0
    tail = deque(maxlen=args.tail) if args.tail > 0 else None
    for line in sys.stdin:
        match = TRACE_RE.match(line.strip())
        if not match:
            continue
        encoding = int(match.group("inst"), 16)
        if not active:
            if encoding == args.start:
                active = True
                saw_start = True
            continue
        if args.end is not None and encoding == args.end:
            active = False
            saw_end = True
            continue
        if tail is None:
            print(line, end="")
        else:
            tail.append(line)
        kept += 1

    if tail is not None:
        sys.stdout.writelines(tail)

    if not saw_start or not saw_end:
        missing = "start" if not saw_start else "end"
        print(f"filter_trace: missing {missing} marker", file=sys.stderr)
        return 2
    written = len(tail) if tail is not None else kept
    print(
        f"filter_trace: observed {kept} marked instructions, wrote {written}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
