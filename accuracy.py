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


def summarize_control_discrepancies(
    entries: list[TraceEntry],
    mismatches: Iterable[tuple[int, int, int]],
    *,
    window: int = 6,
    limit: int = 12,
) -> str:
    """Group spacing mismatches by nearby control-flow instruction shape."""

    control_indices = [idx for idx, entry in enumerate(entries) if entry.insn.is_control]
    control_pos = 0
    last_control_idx: Optional[int] = None
    by_distance: Counter[str] = Counter()
    by_control: Counter[str] = Counter()
    by_control_shape: Counter[str] = Counter()
    by_delta: Counter[str] = Counter()
    examples: list[str] = []

    for idx, rtl_delta, model_delta in mismatches:
        while control_pos < len(control_indices) and control_indices[control_pos] <= idx:
            last_control_idx = control_indices[control_pos]
            control_pos += 1

        distance: Optional[int] = None
        control: Optional[TraceEntry] = None
        if last_control_idx is not None:
            distance = idx - last_control_idx
            if 0 <= distance <= window:
                control = entries[last_control_idx]

        if control is None or distance is None:
            by_distance[f">{window}"] += 1
            continue

        entry = entries[idx]
        taken = control.actual_next_pc is not None and control.actual_next_pc != control.insn.fallthrough_pc
        control_kind = "branch" if control.insn.is_branch else "jalr" if control.insn.is_jalr else "jal"
        shape = (
            f"{control.insn.name} {control_kind} "
            f"{'taken' if taken else 'fallthrough'} "
            f"{'hi' if control.pc & 0x2 else 'lo'} "
            f"{control.insn.length * 8}b"
        )
        by_distance[str(distance)] += 1
        by_control[control.insn.name] += 1
        by_control_shape[shape] += 1
        by_delta[f"{rtl_delta}->{model_delta}"] += 1
        if len(examples) < limit:
            examples.append(
                "idx={idx} pc=0x{pc:x} {name} after {distance} from "
                "0x{cpc:x} {cname}: rtl_delta={rtl_delta} model_delta={model_delta}".format(
                    idx=idx,
                    pc=entry.pc,
                    name=entry.insn.name,
                    distance=distance,
                    cpc=control.pc,
                    cname=control.insn.name,
                    rtl_delta=rtl_delta,
                    model_delta=model_delta,
                )
            )

    parts: list[str] = []
    if by_distance:
        parts.append(f"Distance from previous control (window={window}):")
        parts.extend(f"  {name}: {count}" for name, count in by_distance.most_common(limit))
    if by_control:
        parts.append("By control opcode:")
        parts.extend(f"  {name}: {count}" for name, count in by_control.most_common(limit))
    if by_control_shape:
        parts.append("By control shape:")
        parts.extend(f"  {name}: {count}" for name, count in by_control_shape.most_common(limit))
    if by_delta:
        parts.append("By RTL->model delta:")
        parts.extend(f"  {name}: {count}" for name, count in by_delta.most_common(limit))
    if examples:
        parts.append("Examples:")
        parts.extend(f"  {line}" for line in examples)
    return "\n".join(parts)
