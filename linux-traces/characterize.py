#!/usr/bin/env python3
"""Characterize the last N instructions using the model's decoder and D-cache."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from pathlib import Path
import sys

PERF_MODEL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERF_MODEL))

from model import Model  # noqa: E402
from trace import TraceEntry, parse_line  # noqa: E402


def load_tail(path: Path, window: int) -> tuple[list[TraceEntry], int, Counter[str], int]:
    entries: deque[TraceEntry] = deque(maxlen=window if window > 0 else None)
    decoded_count = 0
    unknown = Counter()
    vector_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            entry = parse_line(line, decoded_count)
            if entry is None:
                continue
            decoded_count += 1
            if entry.insn.name.startswith(("unknown_", "unsupported_")):
                unknown[entry.insn.name] += 1
            if entry.insn.is_vector:
                vector_count += 1
            entries.append(entry)
    result = list(entries)
    for index, entry in enumerate(result):
        entry.index = index
        entry.actual_next_pc = result[index + 1].pc if index + 1 < len(result) else None
    return result, decoded_count, unknown, vector_count


def profile(entries: list[TraceEntry]) -> dict[str, float]:
    counts = Counter()
    recent_load_rd: dict[int, int] = {}
    for index, entry in enumerate(entries):
        insn = entry.insn
        counts["load"] += int(insn.is_load)
        counts["store"] += int(insn.is_store)
        counts["branch"] += int(insn.is_branch)
        counts["indirect"] += int(insn.is_jalr)
        if insn.is_load and insn.rs1 in recent_load_rd:
            counts["depload"] += int(index - recent_load_rd[insn.rs1] <= 16)
        if insn.is_load and insn.rd:
            recent_load_rd[insn.rd] = index
    n = max(1, len(entries))
    loads = max(1, counts["load"])
    return {
        "load": 100.0 * counts["load"] / n,
        "store": 100.0 * counts["store"] / n,
        "branch": 100.0 * counts["branch"] / n,
        "indirect": 100.0 * counts["indirect"] / n,
        "depload": 100.0 * counts["depload"] / loads,
    }


def dcache_miss_rate(entries: list[TraceEntry]) -> float:
    root = Path(__file__).resolve().parents[3]
    dual_repo = root / "c-class-dual-issue"
    model = Model.from_repo(
        dual_repo,
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
        mul_latency=2,
        div_latency=32,
        model_dcache=True,
    )
    model.run(entries)
    accesses = model.dcache.hits + model.dcache.misses
    return 100.0 * model.dcache.misses / max(1, accesses)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path, nargs="+")
    parser.add_argument("--window", type=int, default=250_000)
    parser.add_argument("--allow-unsupported", action="store_true")
    args = parser.parse_args()

    print("| trace | instructions | window | load% | store% | br% | indirect% | depload% | miss% | undecoded | vector |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    failed = False
    for path in args.trace:
        entries, total, unknown, vectors = load_tail(path, args.window)
        mix = profile(entries)
        unsupported = sum(unknown.values())
        miss = float("nan")
        if entries and not vectors:
            miss = dcache_miss_rate(entries)
        print(
            f"| {path.stem} | {total} | {len(entries)} | {mix['load']:.2f} | "
            f"{mix['store']:.2f} | {mix['branch']:.2f} | {mix['indirect']:.2f} | "
            f"{mix['depload']:.2f} | {miss:.2f} | {unsupported} | {vectors} |"
        )
        if unknown:
            detail = ", ".join(f"{name}={count}" for name, count in unknown.most_common())
            print(f"undecoded detail for {path}: {detail}", file=sys.stderr)
        failed |= bool(unsupported or vectors)
    if failed and not args.allow_unsupported:
        print("refusing unsupported trace; pass --allow-unsupported only for diagnosis", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
