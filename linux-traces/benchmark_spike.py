#!/usr/bin/env python3
"""Measure commit-log throughput on reproducible target program runs."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
import time


TRACE_RE = re.compile(
    rb"^(?:cycle\s+\d+\s+)?core\s+\d+:\s+\S+\s+"
    rb"0x[0-9a-fA-F]+\s+\(0x[0-9a-fA-F]+\)"
)


def run(spike: Path, pk: Path, binary: Path) -> tuple[int, float, str]:
    with tempfile.TemporaryFile() as commits, tempfile.TemporaryFile() as stdout:
        start = time.perf_counter()
        completed = subprocess.run(
            [str(spike), "--isa=rv64imafdc_zifencei", "--log-commits", str(pk), str(binary)],
            stdout=stdout,
            stderr=commits,
            check=False,
        )
        elapsed = time.perf_counter() - start
        if completed.returncode != 0:
            raise RuntimeError(f"{spike} exited {completed.returncode}")
        commits.seek(0)
        instructions = sum(bool(TRACE_RE.match(line)) for line in commits)
        stdout.seek(0)
        # The CVA6 fork writes simulator diagnostics to stdout.  The target
        # program's final non-empty line is the portable equivalence check.
        lines = [line for line in stdout.read().splitlines() if line.strip()]
        digest = hashlib.sha256(lines[-1] if lines else b"").hexdigest()
    return instructions, elapsed, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spike", action="append", required=True, metavar="NAME=PATH")
    parser.add_argument("--pk", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    spikes = [(item.split("=", 1)[0], Path(item.split("=", 1)[1])) for item in args.spike]

    samples: dict[str, list[float]] = {name: [] for name, _ in spikes}
    expected_counts: dict[str, int] = {}
    expected_digests: dict[str, str] = {}
    for sample in range(args.runs):
        for name, spike in spikes:
            count, elapsed, digest = run(spike, args.pk, args.binary)
            rate = count / elapsed
            print(f"{name} run {sample + 1}: {count} instructions, {elapsed:.3f} s, {rate:.0f} inst/s")
            if name not in expected_counts:
                expected_counts[name] = count
                expected_digests[name] = digest
            if count != expected_counts[name] or digest != expected_digests[name]:
                raise RuntimeError(
                    f"non-reproducible target run for {name}: "
                    f"count {count} vs {expected_counts[name]}, "
                    f"stdout sha256 {digest} vs {expected_digests[name]}"
                )
            samples[name].append(rate)

    print("\n| Spike | instructions | median inst/s | samples |")
    print("|---|---:|---:|---|")
    for name, _ in spikes:
        rates = samples[name]
        print(f"| {name} | {expected_counts[name]} | {statistics.median(rates):.0f} | "
              f"{', '.join(f'{rate:.0f}' for rate in rates)} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
