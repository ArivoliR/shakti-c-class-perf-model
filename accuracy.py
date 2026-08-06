"""Accuracy metric and discrepancy reporting for model-vs-RTL commit spacing."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from trace import TraceEntry


@dataclass(slots=True)
class AccuracyResult:
    accuracy: Optional[float]
    compared: int
    matches: int
    rtl_total_cycles: Optional[int]
    model_total_cycles: int
    mismatches: list[tuple[int, int, int]]


def compute_accuracy(entries: list[TraceEntry], model_cycles: list[int]) -> AccuracyResult:
    if len(entries) != len(model_cycles):
        raise ValueError(f"entry/model length mismatch: {len(entries)} trace entries vs {len(model_cycles)} model commits")

    if not entries or entries[0].cycle is None:
        return AccuracyResult(
            accuracy=None,
            compared=0,
            matches=0,
            rtl_total_cycles=None,
            model_total_cycles=model_cycles[-1] if model_cycles else 0,
            mismatches=[],
        )

    rtl_cycles = [int(e.cycle) for e in entries if e.cycle is not None]
    mismatches: list[tuple[int, int, int]] = []
    matches = 0
    compared = max(0, len(entries) - 1)
    for idx in range(1, len(entries)):
        rtl_delta = rtl_cycles[idx] - rtl_cycles[idx - 1]
        model_delta = model_cycles[idx] - model_cycles[idx - 1]
        if rtl_delta == model_delta:
            matches += 1
        else:
            mismatches.append((idx, rtl_delta, model_delta))

    return AccuracyResult(
        accuracy=(matches / compared) if compared else 1.0,
        compared=compared,
        matches=matches,
        rtl_total_cycles=rtl_cycles[-1] - rtl_cycles[0] + 1 if rtl_cycles else None,
        model_total_cycles=model_cycles[-1] - model_cycles[0] + 1 if model_cycles else 0,
        mismatches=mismatches,
    )


def rank_discrepancies(
    entries: list[TraceEntry],
    mismatches: Iterable[tuple[int, int, int]],
    limit: int = 12,
) -> str:
    by_opcode: Counter[str] = Counter()
    by_prev_opcode: Counter[str] = Counter()
    by_pair: Counter[str] = Counter()
    examples: list[str] = []

    for idx, rtl_delta, model_delta in mismatches:
        entry = entries[idx]
        prev = entries[idx - 1] if idx > 0 else None
        by_opcode[entry.insn.name] += 1
        if prev:
            by_prev_opcode[prev.insn.name] += 1
            by_pair[f"{prev.insn.name} -> {entry.insn.name}"] += 1
        if len(examples) < limit:
            examples.append(
                f"#{idx} pc=0x{entry.pc:x} {entry.insn.name}: rtl_delta={rtl_delta} model_delta={model_delta}"
            )

    parts = []
    if by_opcode:
        parts.append("By current opcode:")
        parts.extend(f"  {name}: {count}" for name, count in by_opcode.most_common(limit))
    if by_prev_opcode:
        parts.append("By previous opcode:")
        parts.extend(f"  {name}: {count}" for name, count in by_prev_opcode.most_common(limit))
    if by_pair:
        parts.append("By opcode pair:")
        parts.extend(f"  {name}: {count}" for name, count in by_pair.most_common(limit))
    if examples:
        parts.append("Examples:")
        parts.extend(f"  {line}" for line in examples)
    return "\n".join(parts)

