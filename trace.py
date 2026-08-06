"""Trace parsing for SHAKTI C-Class rtldump commit logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Iterable, Optional, Sequence

from isa import Instruction, decode


TRACE_RE = re.compile(
    r"^(?:cycle\s+(?P<cycle>\d+)\s+)?core\s+\d+:\s+"
    r"(?P<mode>\S+)\s+"
    r"(?P<pc>0x[0-9a-fA-F]+)\s+"
    r"\((?P<inst>0x[0-9a-fA-F]+)\)"
    r"(?P<rest>.*)$"
)
REG_RE = re.compile(r"\s(?P<kind>[xf])(?P<reg>\d+)\s+0x(?P<value>[0-9a-fA-F]+)")
CSR_RE = re.compile(r"\sc(?P<num>\d+)_([A-Za-z0-9_]+)\s+0x(?P<value>[0-9a-fA-F]+)")
MEM_RE = re.compile(r"\smem\s+(?P<addr>0x[0-9a-fA-F]+)")
APP_LOG_RE = re.compile(
    r"IPC_MEASURE\s+cycles:\s+(?P<cycles>\d+)\s+"
    r"instret:\s+(?P<instret>\d+)"
    r"(?:\s+runs:\s+(?P<runs>\d+))?"
)
COREMARK_TICKS_RE = re.compile(r"Total ticks\s+:\s+(?P<cycles>\d+)")
COREMARK_ITERATIONS_RE = re.compile(r"Iterations\s+:\s+(?P<runs>\d+)")
CSR_MCYCLE = 0xB00
CSR_MINSTRET = 0xB02


@dataclass(slots=True)
class RegWrite:
    kind: str
    reg: int
    value: int


@dataclass(slots=True)
class CSRWrite:
    number: int
    name: str
    value: int


@dataclass(slots=True)
class TraceEntry:
    index: int
    pc: int
    encoding: int
    mode: str
    cycle: Optional[int] = None
    raw: str = ""
    reg_writes: list[RegWrite] = field(default_factory=list)
    csr_writes: list[CSRWrite] = field(default_factory=list)
    mem_addresses: list[int] = field(default_factory=list)
    insn: Instruction = field(init=False)
    actual_next_pc: Optional[int] = None

    def __post_init__(self) -> None:
        self.insn = decode(self.encoding, self.pc)


@dataclass(frozen=True, slots=True)
class AppLogMetrics:
    cycles: int
    instret: Optional[int] = None
    runs: Optional[int] = None


@dataclass(frozen=True, slots=True)
class BenchmarkWindow:
    start_index: int
    end_index: int
    start_mcycle_index: int
    end_mcycle_index: int
    start_minstret_index: Optional[int]
    end_minstret_index: Optional[int]
    measured_cycles: int
    measured_instructions: int
    runs: Optional[int] = None

    def entries(self, trace_entries: list[TraceEntry]) -> list[TraceEntry]:
        return trace_entries[self.start_index : self.end_index]


def parse_line(line: str, index: int = 0, *, keep_raw: bool = False) -> Optional[TraceEntry]:
    line = line.rstrip("\n")
    match = TRACE_RE.match(line.strip())
    if not match:
        return None
    rest = match.group("rest")
    entry = TraceEntry(
        index=index,
        cycle=int(match.group("cycle")) if match.group("cycle") is not None else None,
        mode=match.group("mode"),
        pc=int(match.group("pc"), 16),
        encoding=int(match.group("inst"), 16),
        raw=line if keep_raw else "",
    )
    for reg in REG_RE.finditer(rest):
        entry.reg_writes.append(RegWrite(reg.group("kind"), int(reg.group("reg")), int(reg.group("value"), 16)))
    for csr in CSR_RE.finditer(rest):
        entry.csr_writes.append(CSRWrite(int(csr.group("num")), csr.group(2), int(csr.group("value"), 16)))
    for mem in MEM_RE.finditer(rest):
        entry.mem_addresses.append(int(mem.group("addr"), 16))
    return entry


def parse_trace_lines(lines: Iterable[str], limit: Optional[int] = None, *, keep_raw: bool = False) -> list[TraceEntry]:
    entries: list[TraceEntry] = []
    for line in lines:
        entry = parse_line(line, len(entries), keep_raw=keep_raw)
        if entry is None:
            continue
        entries.append(entry)
        if limit is not None and len(entries) >= limit:
            break
    _annotate_next_pcs(entries)
    return entries


def parse_trace(path: str | Path, limit: Optional[int] = None, *, keep_raw: bool = False) -> list[TraceEntry]:
    return parse_trace_files([path], limit=limit, keep_raw=keep_raw)


def parse_trace_files(
    paths: Sequence[str | Path],
    limit: Optional[int] = None,
    *,
    keep_raw: bool = False,
) -> list[TraceEntry]:
    entries: list[TraceEntry] = []
    for path in paths:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                entry = parse_line(line, len(entries), keep_raw=keep_raw)
                if entry is None:
                    continue
                entries.append(entry)
                if limit is not None and len(entries) >= limit:
                    _annotate_next_pcs(entries)
                    return entries
    _annotate_next_pcs(entries)
    return entries


def has_cycle_stamps(entries: list[TraceEntry]) -> bool:
    return bool(entries) and all(e.cycle is not None for e in entries)


def parse_app_log_metrics(path: str | Path) -> Optional[AppLogMetrics]:
    app_log = Path(path)
    if not app_log.exists():
        return None
    text = app_log.read_text(encoding="utf-8", errors="replace")
    match = APP_LOG_RE.search(text)
    if not match:
        ticks = COREMARK_TICKS_RE.search(text)
        if not ticks:
            return None
        iterations = COREMARK_ITERATIONS_RE.search(text)
        return AppLogMetrics(
            cycles=int(ticks.group("cycles")),
            runs=int(iterations.group("runs")) if iterations is not None else None,
        )
    return AppLogMetrics(
        cycles=int(match.group("cycles")),
        instret=int(match.group("instret")),
        runs=int(match.group("runs")) if match.group("runs") is not None else None,
    )


def detect_benchmark_window(entries: list[TraceEntry], metrics: AppLogMetrics) -> Optional[BenchmarkWindow]:
    csr_reads = _csr_read_values(entries)
    mcycles = csr_reads.get(CSR_MCYCLE, [])
    minstrets = csr_reads.get(CSR_MINSTRET, [])
    mcycle_by_value: dict[int, list[tuple[int, int]]] = {}
    minstret_by_value: dict[int, list[tuple[int, int]]] = {}
    for item in mcycles:
        mcycle_by_value.setdefault(item[1], []).append(item)
    for item in minstrets:
        minstret_by_value.setdefault(item[1], []).append(item)

    if metrics.instret is None:
        return _detect_mcycle_only_window(mcycles, mcycle_by_value, metrics)

    best: tuple[int, BenchmarkWindow] | None = None
    for start_min_idx, start_min_value in minstrets:
        end_min_value = start_min_value + metrics.instret
        for end_min_idx, _ in minstret_by_value.get(end_min_value, []):
            if end_min_idx <= start_min_idx:
                continue
            if end_min_idx - start_min_idx != metrics.instret:
                continue
            for start_cycle_idx, start_cycle_value in mcycles:
                if not (start_min_idx < start_cycle_idx < end_min_idx):
                    continue
                end_cycle_value = start_cycle_value + metrics.cycles
                for end_cycle_idx, _ in mcycle_by_value.get(end_cycle_value, []):
                    if not (start_cycle_idx < end_cycle_idx < end_min_idx):
                        continue
                    csr_gap_score = (start_cycle_idx - start_min_idx) + (end_min_idx - end_cycle_idx)
                    window = BenchmarkWindow(
                        start_index=start_min_idx,
                        end_index=end_min_idx,
                        start_mcycle_index=start_cycle_idx,
                        end_mcycle_index=end_cycle_idx,
                        start_minstret_index=start_min_idx,
                        end_minstret_index=end_min_idx,
                        measured_cycles=metrics.cycles,
                        measured_instructions=metrics.instret,
                        runs=metrics.runs,
                    )
                    if best is None or csr_gap_score < best[0]:
                        best = (csr_gap_score, window)
    return best[1] if best is not None else None


def _detect_mcycle_only_window(
    mcycles: list[tuple[int, int]],
    mcycle_by_value: dict[int, list[tuple[int, int]]],
    metrics: AppLogMetrics,
) -> Optional[BenchmarkWindow]:
    best: BenchmarkWindow | None = None
    for start_cycle_idx, start_cycle_value in mcycles:
        end_cycle_value = start_cycle_value + metrics.cycles
        for end_cycle_idx, _ in mcycle_by_value.get(end_cycle_value, []):
            if end_cycle_idx <= start_cycle_idx:
                continue
            candidate = BenchmarkWindow(
                start_index=start_cycle_idx,
                end_index=end_cycle_idx + 1,
                start_mcycle_index=start_cycle_idx,
                end_mcycle_index=end_cycle_idx,
                start_minstret_index=None,
                end_minstret_index=None,
                measured_cycles=metrics.cycles,
                measured_instructions=end_cycle_idx - start_cycle_idx + 1,
                runs=metrics.runs,
            )
            if best is None or candidate.measured_instructions > best.measured_instructions:
                best = candidate
    return best


def _csr_read_values(entries: list[TraceEntry]) -> dict[int, list[tuple[int, int]]]:
    reads: dict[int, list[tuple[int, int]]] = {}
    for idx, entry in enumerate(entries):
        if not entry.insn.is_csr or entry.insn.csr is None or not entry.reg_writes:
            continue
        reads.setdefault(entry.insn.csr, []).append((idx, entry.reg_writes[0].value))
    return reads


def _annotate_next_pcs(entries: list[TraceEntry]) -> None:
    for idx, entry in enumerate(entries[:-1]):
        entry.actual_next_pc = entries[idx + 1].pc
    if entries:
        entries[-1].actual_next_pc = None
