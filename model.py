"""Cycle-level performance model for the single-issue SHAKTI C-Class core.

The model consumes the committed dynamic instruction stream produced by RTL
rtldump. It models timing and hazards, not architectural data values.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import argparse
import math
import re
from typing import Deque, Optional

from accuracy import compute_accuracy, rank_discrepancies
from isa import FRF, IRF, Instruction
from trace import (
    BenchmarkWindow,
    TraceEntry,
    detect_benchmark_window,
    has_cycle_stamps,
    parse_app_log_metrics,
    parse_trace_files,
)


def parse_makefile_defines(path: str | Path) -> dict[str, int | bool | str]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^BSC_DEFINES\s*:?=\s*(?P<defs>.*)$", text, re.MULTILINE)
    if not match:
        raise ValueError(f"BSC_DEFINES not found in {path}")
    params: dict[str, int | bool | str] = {}
    for token in match.group("defs").split():
        if "=" not in token:
            params[token] = True
            continue
        key, value = token.split("=", 1)
        try:
            params[key] = int(value, 0)
        except ValueError:
            params[key] = value
    return params


@dataclass(slots=True)
class PipeEntry:
    trace: TraceEntry
    prev_trace: Optional[TraceEntry] = None
    next_trace: Optional[TraceEntry] = None
    predicted_next_pc: Optional[int] = None
    pred_state: int = 1
    pred_btb_hit: bool = False
    pred_history: int = 0
    pred_ci: str = "Branch"
    pred_mispredict: bool = False
    frontend_waited_cycles: int = 0
    scoreboard_id: Optional[int] = None
    result_ready_cycle: int = 0
    bypassable: bool = False
    bypass_ready_cycle: int = 0
    wb_kind: str = "BASE"

    @property
    def insn(self) -> Instruction:
        return self.trace.insn

    @property
    def rd_key(self) -> Optional[tuple[str, int]]:
        if not self.insn.writes_scoreboard:
            return None
        return (self.insn.rd_type, self.insn.rd)


@dataclass(slots=True)
class PredictorTraining:
    apply_cycle: int
    trace: TraceEntry
    state: int
    btbhit: bool
    history: int


class FixedQueue:
    def __init__(self, capacity: int):
        self.capacity = max(1, int(capacity))
        self.items: Deque[PipeEntry] = deque()

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def full(self) -> bool:
        return len(self.items) >= self.capacity

    def empty(self) -> bool:
        return not self.items

    def first(self) -> PipeEntry:
        return self.items[0]

    def push(self, entry: PipeEntry) -> None:
        if self.full():
            raise RuntimeError("queue full")
        self.items.append(entry)

    def pop(self) -> PipeEntry:
        return self.items.popleft()


class BranchPredictor:
    def __init__(
        self,
        *,
        btbdepth: int,
        bhtdepth: int,
        histlen: int,
        histbits: int,
        rasdepth: int,
        compressed: bool = True,
        enabled: bool = True,
        match_btb_hi: bool = True,
        bsv_hash_truncate: bool = True,
    ) -> None:
        self.btbdepth = btbdepth
        self.bhtdepth = bhtdepth
        self.histlen = histlen
        self.histbits = histbits
        self.rasdepth = rasdepth
        self.compressed = compressed
        self.enabled = enabled
        self.match_btb_hi = match_btb_hi
        self.bsv_hash_truncate = bsv_hash_truncate
        self.bhtcols = 2 if compressed else 1
        self.bht_rows = max(1, bhtdepth // self.bhtcols)
        self.bht = [[1 for _ in range(self.bht_rows)] for _ in range(self.bhtcols)]
        self.btb: list[Optional[dict[str, int | str | bool]]] = [None for _ in range(btbdepth)]
        self.allocate = 0
        self.ghr = 0
        self.ras: list[int] = []

    def predict(self, entry: TraceEntry) -> tuple[Optional[int], int, bool, int, str, bool]:
        insn = entry.insn
        pc = entry.pc & ~0x3
        if not self.enabled:
            return None, 1, False, self.ghr, "Branch", False

        idx = self._lookup(pc, hi=bool(entry.pc & 0x2) if self.compressed and self.match_btb_hi else None)
        state = 1
        hit = idx is not None
        ci = "Branch"
        target = insn.fallthrough_pc
        history_after = self.ghr
        should_redirect = False

        if hit:
            btb_entry = self.btb[idx]
            assert btb_entry is not None
            ci = str(btb_entry["ci"])
            state = self.bht[1 if bool(entry.pc & 0x2) else 0][self._hash(self.ghr, pc)] if ci == "Branch" else 3
            target = int(btb_entry["target"])
            if ci == "Ret" and self.ras:
                target = self.ras[-1]
                self.ras.pop()
            elif ci == "Call":
                self._ras_push(insn.fallthrough_pc)
            should_redirect = state >= 2 or ci in ("JAL", "Call", "Ret")
            if ci == "Branch":
                history_after = self._insert_history(1 if state >= 2 else 0, self.ghr)
                self.ghr = history_after

        predicted_next = target if should_redirect else insn.fallthrough_pc
        actual_next = entry.actual_next_pc
        mispredict = bool(actual_next is not None and predicted_next != actual_next and insn.is_control)
        return predicted_next, state, hit, history_after, ci, mispredict

    def train(self, entry: TraceEntry, state: int, btbhit: bool, history: int) -> None:
        insn = entry.insn
        if not self.enabled or not insn.is_control:
            return
        actual_next = entry.actual_next_pc
        if actual_next is None:
            return
        taken = actual_next != insn.fallthrough_pc if insn.is_branch else True
        target = entry.pc + insn.imm if insn.is_branch else actual_next
        ci = self._control_insn(entry)

        tag = (entry.pc & ~0x3) >> 2
        idx = self._lookup(entry.pc & ~0x3)
        update_idx = idx if idx is not None else self.allocate
        self.btb[update_idx] = {
            "tag": tag,
            "target": target,
            "ci": ci,
            "instr16": insn.length == 2,
            "hi": bool(entry.pc & 0x2),
        }
        if idx is None:
            self.allocate = (self.allocate + 1) % self.btbdepth

        if insn.is_branch and btbhit:
            new_state = state
            if taken:
                if new_state == 0:
                    new_state = 1
                elif new_state in (1, 2):
                    new_state = 3
            else:
                if new_state in (1, 2):
                    new_state = 0
                elif new_state == 3:
                    new_state = 2
            bank = 1 if bool(entry.pc & 0x2) else 0
            self.bht[bank][self._hash((history << 1) & self._hist_mask, entry.pc & ~0x3)] = new_state

    def restore_after_mispredict(self, btbhit: bool, history: int) -> None:
        if btbhit:
            history ^= 1 << (self.histlen - 1)
        self.ghr = history & self._hist_mask

    @property
    def _hist_mask(self) -> int:
        return (1 << self.histlen) - 1

    def _hash(self, history: int, pc: int) -> int:
        rows = self.bht_rows
        row_bits = int(math.log2(rows)) if rows > 1 else 0
        pc_hash = ((pc >> 2) ^ ((pc >> (2 + row_bits)) & 0b11)) & (rows - 1)
        hist = (history >> max(0, self.histlen - self.histbits)) & ((1 << self.histbits) - 1)
        hist_shift = max(0, int(math.log2(self.bhtdepth)) - self.histbits)
        if self.bsv_hash_truncate:
            # In BSV, shifting a Bit#(histbits) keeps the same result width
            # before zeroExtend() widens it to the BHT row index.
            hist_hash = (hist << hist_shift) & ((1 << self.histbits) - 1)
        else:
            hist_hash = (hist << hist_shift) & (rows - 1)
        return (pc_hash ^ hist_hash) & (rows - 1)

    def _lookup(self, aligned_pc: int, hi: Optional[bool] = None) -> Optional[int]:
        tag = aligned_pc >> 2
        for idx, entry in enumerate(self.btb):
            if entry is None or entry["tag"] != tag:
                continue
            if hi is not None and bool(entry.get("hi", False)) != hi:
                continue
            return idx
        return None

    def _insert_history(self, bit_value: int, history: int) -> int:
        return ((bit_value << (self.histlen - 1)) | (history >> 1)) & self._hist_mask

    def _ras_push(self, pc: int) -> None:
        if self.rasdepth <= 0:
            return
        self.ras.append(pc)
        if len(self.ras) > self.rasdepth:
            self.ras.pop(0)

    def _control_insn(self, entry: TraceEntry) -> str:
        insn = entry.insn
        if insn.is_jal or insn.is_jalr:
            if insn.rd == 1:
                return "Call"
            if insn.is_jalr and insn.rs1 in (1, 5):
                return "Ret"
            return "JAL"
        return "Branch"


class Model:
    """Single-issue C-Class timing model.

    All timing knobs are constructor parameters. Defaults are loaded from
    makefile.inc where possible and conservative fixed hit latencies otherwise.
    """

    def __init__(
        self,
        *,
        isb_s0s1: int = 2,
        isb_s1s2: int = 2,
        isb_s2s3: int = 1,
        isb_s3s4: int = 8,
        isb_s4s5: int = 8,
        bypass_sources: int = 2,
        wawid: int = 4,
        mul_latency: int = 2,
        div_latency: int = 32,
        load_hit_latency: int = 1,
        store_hit_latency: int = 1,
        csr_latency: int = 1,
        branch_mispredict_penalty: int = 1,
        upper_half_32b_target_penalty: int = 1,
        upper_half_32b_mispredict_penalty: int = 0,
        load_to_store_data_release_penalty: int = 0,
        mul_latency_adjust: int = 0,
        predictor_train_delay: int = 1,
        wb_flush_penalty: int = 4,
        btbdepth: int = 32,
        bhtdepth: int = 512,
        histlen: int = 8,
        histbits: int = 5,
        rasdepth: int = 8,
        enable_bpu: bool = True,
        compressed: bool = True,
        match_btb_hi: bool = True,
        bsv_hash_truncate: bool = True,
        issue_width: int = 1,
        commit_width: int = 1,
    ) -> None:
        self.params = dict(locals())
        self.q_s0s1 = FixedQueue(isb_s0s1)
        self.q_s1s2 = FixedQueue(isb_s1s2)
        self.q_s2s3 = FixedQueue(isb_s2s3)
        self.q_s3s4 = FixedQueue(isb_s3s4)
        self.q_s4s5 = FixedQueue(isb_s4s5)
        self.bypass_sources = bypass_sources
        self.wawid_mask = (1 << wawid) - 1
        self.mul_latency = mul_latency
        self.div_latency = div_latency
        self.load_hit_latency = load_hit_latency
        self.store_hit_latency = store_hit_latency
        self.csr_latency = csr_latency
        self.branch_mispredict_penalty = branch_mispredict_penalty
        self.upper_half_32b_target_penalty = upper_half_32b_target_penalty
        self.upper_half_32b_mispredict_penalty = upper_half_32b_mispredict_penalty
        self.load_to_store_data_release_penalty = load_to_store_data_release_penalty
        self.mul_latency_adjust = mul_latency_adjust
        self.predictor_train_delay = predictor_train_delay
        self.wb_flush_penalty = wb_flush_penalty
        self.compressed = compressed
        self.issue_width = issue_width
        self.commit_width = commit_width
        self.predictor = BranchPredictor(
            btbdepth=btbdepth,
            bhtdepth=bhtdepth,
            histlen=histlen,
            histbits=histbits,
            rasdepth=rasdepth,
            compressed=compressed,
            enabled=enable_bpu,
            match_btb_hi=match_btb_hi,
            bsv_hash_truncate=bsv_hash_truncate,
        )
        self.scoreboard: dict[tuple[str, int], int] = {}
        self.next_wawid = 0
        self.fetch_index = 0
        self.commits: list[int] = []
        self.cycle = 0
        self.flush_countdown = 0
        self.fetch_blocked_by: Optional[int] = None
        self.div_busy_until = 0
        self.load_release_cycle: dict[tuple[str, int], int] = {}
        self.pending_predictor_training: Deque[PredictorTraining] = deque()

    @classmethod
    def from_repo(cls, repo_root: str | Path, **overrides: int | bool) -> "Model":
        defines = parse_makefile_defines(Path(repo_root) / "makefile.inc")
        kwargs = {
            "isb_s0s1": int(defines.get("isb_s0s1", 2)),
            "isb_s1s2": int(defines.get("isb_s1s2", 2)),
            "isb_s2s3": int(defines.get("isb_s2s3", 1)),
            "isb_s3s4": int(defines.get("isb_s3s4", 8)),
            "isb_s4s5": int(defines.get("isb_s4s5", 8)),
            "bypass_sources": int(defines.get("bypass_sources", 2)),
            "wawid": int(defines.get("wawid", 4)),
            "mul_latency": int(defines.get("MULSTAGES_TOTAL", 2)),
            "div_latency": int(defines.get("DIVSTAGES", 32)),
            "btbdepth": int(defines.get("btbdepth", 32)),
            "bhtdepth": int(defines.get("bhtdepth", 512)),
            "histlen": int(defines.get("histlen", 8)),
            "histbits": int(defines.get("histbits", 5)),
            "rasdepth": int(defines.get("rasdepth", 8)),
            "enable_bpu": bool(defines.get("bpu", False)),
            "compressed": bool(defines.get("compressed", False)),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    def run(self, entries: list[TraceEntry]) -> list[int]:
        self._reset_runtime()
        max_cycles = max(1000, len(entries) * 80 + 1000)
        while len(self.commits) < len(entries):
            if self.cycle > max_cycles:
                raise RuntimeError(f"model did not drain after {max_cycles} cycles")
            for _ in range(self.commit_width):
                self.try_commit()
            self.try_stage4()
            for _ in range(self.issue_width):
                self.try_execute()
            self.try_decode()
            self.try_fetch_decode()
            self.try_fetch(entries)
            self.cycle += 1
        return self.commits

    def _reset_runtime(self) -> None:
        self.q_s0s1.items.clear()
        self.q_s1s2.items.clear()
        self.q_s2s3.items.clear()
        self.q_s3s4.items.clear()
        self.q_s4s5.items.clear()
        self.scoreboard.clear()
        self.next_wawid = 0
        self.fetch_index = 0
        self.commits = []
        self.cycle = 0
        self.flush_countdown = 0
        self.fetch_blocked_by = None
        self.div_busy_until = 0
        self.load_release_cycle.clear()
        self.pending_predictor_training.clear()

    def try_commit(self) -> bool:
        if self.q_s4s5.empty():
            return False
        entry = self.q_s4s5.pop()
        if entry.insn.is_load and entry.rd_key is not None:
            self.load_release_cycle[entry.rd_key] = self.cycle
        self._release_scoreboard(entry)
        if entry.wb_kind == "SYSTEM" and (entry.insn.is_csr or entry.insn.name == "xret"):
            # CSR responses are normally single-cycle in this CSRBox, but the
            # constructor keeps this explicit for architectural experiments.
            pass
        self.commits.append(self.cycle)
        return True

    def try_stage4(self) -> bool:
        if self.q_s3s4.empty() or self.q_s4s5.full():
            return False
        entry = self.q_s3s4.first()
        if entry.result_ready_cycle > self.cycle:
            return False
        entry = self.q_s3s4.pop()
        entry.bypassable = entry.insn.writes_scoreboard
        if entry.insn.is_load or entry.insn.is_mul or entry.insn.is_div or entry.insn.is_float:
            entry.bypass_ready_cycle = self.cycle + 1
        else:
            entry.bypass_ready_cycle = self.cycle
        if entry.insn.is_load or entry.insn.is_mul or entry.insn.is_div or entry.insn.is_float:
            entry.wb_kind = "BASE"
        elif entry.insn.is_store or entry.insn.is_fence or entry.insn.is_fence_i:
            entry.wb_kind = "MEMORY"
        elif entry.insn.fu == "SYSTEM":
            entry.wb_kind = "SYSTEM"
        elif entry.insn.fu == "TRAP":
            entry.wb_kind = "TRAP"
        else:
            entry.wb_kind = "BASE"
        self.q_s4s5.push(entry)
        return True

    def try_execute(self) -> bool:
        if self.q_s2s3.empty() or self.q_s3s4.full():
            return False
        entry = self.q_s2s3.first()
        insn = entry.insn
        if not self._fu_ready(insn):
            return False
        if not self._operands_available(insn):
            return False
        frontend_penalty = self._frontend_redirect_penalty(entry)
        if entry.frontend_waited_cycles < frontend_penalty:
            entry.frontend_waited_cycles += 1
            return False

        entry = self.q_s2s3.pop()
        self._lock_scoreboard(entry)
        entry.result_ready_cycle = self._result_ready_cycle(insn)
        entry.bypassable = insn.writes_scoreboard and self._is_base_result_available_in_s3s4(insn)
        entry.bypass_ready_cycle = self.cycle
        entry.wb_kind = self._initial_wb_kind(insn)
        self.q_s3s4.push(entry)

        if insn.is_control:
            if entry.pred_mispredict:
                self.predictor.restore_after_mispredict(entry.pred_btb_hit, entry.pred_history)
                penalty = self.branch_mispredict_penalty + self._upper_half_32b_mispredict_extra(entry)
                self.flush_countdown = max(self.flush_countdown, penalty)
                if self.fetch_blocked_by == entry.trace.index:
                    self.fetch_blocked_by = None
            self._schedule_predictor_training(entry)
        if insn.is_div:
            self.div_busy_until = self.cycle + self.div_latency
        if insn.fu == "TRAP" or insn.name == "xret" or insn.is_fence_i:
            self.flush_countdown = max(self.flush_countdown, self.wb_flush_penalty)
        return True

    def try_decode(self) -> bool:
        if self.q_s1s2.empty() or self.q_s2s3.full():
            return False
        self.q_s2s3.push(self.q_s1s2.pop())
        return True

    def try_fetch_decode(self) -> bool:
        if self.q_s0s1.empty() or self.q_s1s2.full():
            return False
        self.q_s1s2.push(self.q_s0s1.pop())
        return True

    def try_fetch(self, entries: list[TraceEntry]) -> bool:
        self._apply_pending_predictor_training()
        if self.fetch_index >= len(entries) or self.q_s0s1.full():
            return False
        if self.flush_countdown > 0:
            self.flush_countdown -= 1
            return False
        if self.fetch_blocked_by is not None:
            return False

        trace_entry = entries[self.fetch_index]
        pipe_entry = PipeEntry(
            trace_entry,
            prev_trace=entries[self.fetch_index - 1] if self.fetch_index > 0 else None,
            next_trace=entries[self.fetch_index + 1] if self.fetch_index + 1 < len(entries) else None,
        )
        if trace_entry.insn.is_control:
            (
                pipe_entry.predicted_next_pc,
                pipe_entry.pred_state,
                pipe_entry.pred_btb_hit,
                pipe_entry.pred_history,
                pipe_entry.pred_ci,
                pipe_entry.pred_mispredict,
            ) = self.predictor.predict(trace_entry)
            if pipe_entry.pred_mispredict:
                self.fetch_blocked_by = trace_entry.index
        self.q_s0s1.push(pipe_entry)
        self.fetch_index += 1
        return True

    def _schedule_predictor_training(self, entry: PipeEntry) -> None:
        self.pending_predictor_training.append(
            PredictorTraining(
                apply_cycle=self.cycle + self.predictor_train_delay,
                trace=entry.trace,
                state=entry.pred_state,
                btbhit=entry.pred_btb_hit,
                history=entry.pred_history,
            )
        )
        self._apply_pending_predictor_training()

    def _apply_pending_predictor_training(self) -> None:
        while self.pending_predictor_training and self.pending_predictor_training[0].apply_cycle <= self.cycle:
            training = self.pending_predictor_training.popleft()
            self.predictor.train(training.trace, training.state, training.btbhit, training.history)

    def _fu_ready(self, insn: Instruction) -> bool:
        if insn.is_div:
            return self.cycle >= self.div_busy_until
        return True

    def _operands_available(self, insn: Instruction) -> bool:
        if self._store_data_waits_for_load_release(insn):
            return False
        for key in insn.source_regs():
            locked_id = self.scoreboard.get(key)
            if locked_id is None:
                continue
            if not self._bypass_available(key, locked_id):
                return False
        return True

    def _bypass_available(self, key: tuple[str, int], locked_id: int) -> bool:
        sources = []
        if self.bypass_sources >= 1 and not self.q_s3s4.empty():
            sources.append(self.q_s3s4.first())
        if self.bypass_sources >= 2 and not self.q_s4s5.empty():
            sources.append(self.q_s4s5.first())
        for entry in sources:
            if not entry.bypassable:
                continue
            if entry.bypass_ready_cycle > self.cycle:
                continue
            if entry.rd_key != key:
                continue
            if entry.scoreboard_id == locked_id:
                return True
        return False

    def _store_data_waits_for_load_release(self, insn: Instruction) -> bool:
        if self.load_to_store_data_release_penalty <= 0:
            return False
        if not insn.is_store or not insn.uses_rs2:
            return False
        key = (insn.rs2_type, insn.rs2)
        release_cycle = self.load_release_cycle.get(key)
        if release_cycle is None:
            return False
        return self.cycle - release_cycle < self.load_to_store_data_release_penalty

    def _frontend_redirect_penalty(self, entry: PipeEntry) -> int:
        if not self.compressed or self.upper_half_32b_target_penalty <= 0:
            return 0
        if not entry.insn.is_control or entry.pred_mispredict or not entry.pred_btb_hit:
            return 0
        actual_next = entry.trace.actual_next_pc
        if actual_next is None or actual_next == entry.insn.fallthrough_pc:
            return 0
        if actual_next & 0x2 == 0:
            return 0
        if entry.next_trace is None or entry.next_trace.pc != actual_next or entry.next_trace.insn.length != 4:
            return 0
        if self._previous_load_feeds(entry):
            return 0
        return self.upper_half_32b_target_penalty

    def _upper_half_32b_mispredict_extra(self, entry: PipeEntry) -> int:
        if not self.compressed or self.upper_half_32b_mispredict_penalty <= 0:
            return 0
        if not entry.pred_btb_hit or entry.insn.length != 4 or not (entry.trace.pc & 0x2):
            return 0
        if entry.predicted_next_pc is None or entry.predicted_next_pc == entry.insn.fallthrough_pc:
            return 0
        return self.upper_half_32b_mispredict_penalty

    def _previous_load_feeds(self, entry: PipeEntry) -> bool:
        prev = entry.prev_trace
        if prev is None or not prev.insn.is_load or not prev.insn.writes_scoreboard:
            return False
        return (prev.insn.rd_type, prev.insn.rd) in entry.insn.source_regs()

    def _lock_scoreboard(self, entry: PipeEntry) -> None:
        key = entry.rd_key
        if key is None:
            return
        entry.scoreboard_id = self.next_wawid
        self.next_wawid = (self.next_wawid + 1) & self.wawid_mask
        self.scoreboard[key] = entry.scoreboard_id

    def _release_scoreboard(self, entry: PipeEntry) -> None:
        key = entry.rd_key
        if key is None:
            return
        if self.scoreboard.get(key) == entry.scoreboard_id:
            del self.scoreboard[key]

    def _result_ready_cycle(self, insn: Instruction) -> int:
        if insn.is_load or insn.is_atomic:
            return self.cycle + self.load_hit_latency
        if insn.is_store or insn.is_fence or insn.is_fence_i:
            return self.cycle + self.store_hit_latency
        if insn.is_mul:
            return self.cycle + self.mul_latency + self.mul_latency_adjust
        if insn.is_div:
            return self.cycle + self.div_latency
        if insn.is_csr or insn.fu == "SYSTEM":
            return self.cycle + self.csr_latency
        return self.cycle + 1

    def _is_base_result_available_in_s3s4(self, insn: Instruction) -> bool:
        return not (insn.is_load or insn.is_mul or insn.is_div or insn.is_float or insn.fu in ("SYSTEM", "TRAP"))

    def _initial_wb_kind(self, insn: Instruction) -> str:
        if insn.is_load or insn.is_mul or insn.is_div or insn.is_float:
            return "BASE"
        if insn.is_store or insn.is_fence or insn.is_fence_i:
            return "MEMORY"
        if insn.fu == "SYSTEM":
            return "SYSTEM"
        if insn.fu == "TRAP":
            return "TRAP"
        return "BASE"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SHAKTI C-Class performance model")
    parser.add_argument("trace_files", nargs="+", help="RTL rtldump file(s), optionally cycle-stamped")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--limit", type=int, default=None, help="Only parse the first N committed instructions")
    parser.add_argument("--no-auto-window", action="store_true", help="Use the whole parsed trace instead of the app_log IPC window")
    parser.add_argument("--window", metavar="START:END", help="Use an explicit 0-based trace index window, END exclusive")
    parser.add_argument("--mispredict-penalty", type=int, default=1)
    parser.add_argument("--load-hit-latency", type=int, default=1)
    parser.add_argument("--store-hit-latency", type=int, default=1)
    parser.add_argument("--upper-half-32b-target-penalty", type=int, default=1)
    parser.add_argument("--upper-half-32b-mispredict-penalty", type=int, default=0)
    parser.add_argument("--load-to-store-data-release-penalty", type=int, default=0)
    parser.add_argument("--mul-latency-adjust", type=int, default=0)
    parser.add_argument("--predictor-train-delay", type=int, default=1)
    parser.add_argument("--no-match-btb-hi", action="store_true")
    parser.add_argument("--no-bsv-hash-truncate", dest="bsv_hash_truncate", action="store_false")
    parser.set_defaults(bsv_hash_truncate=True)
    parser.add_argument("--show-discrepancies", type=int, default=12)
    args = parser.parse_args()

    parsed_entries = parse_trace_files(args.trace_files, limit=args.limit)
    entries = parsed_entries
    window: Optional[BenchmarkWindow] = None
    if args.window:
        try:
            start, end = _parse_window(args.window)
        except ValueError as exc:
            parser.error(str(exc))
        entries = parsed_entries[start:end]
    elif not args.no_auto_window and args.limit is None:
        metrics = parse_app_log_metrics(Path(args.trace_files[0]).with_name("app_log"))
        if metrics is not None:
            window = detect_benchmark_window(parsed_entries, metrics)
            if window is not None:
                entries = window.entries(parsed_entries)

    model = Model.from_repo(
        args.repo_root,
        branch_mispredict_penalty=args.mispredict_penalty,
        load_hit_latency=args.load_hit_latency,
        store_hit_latency=args.store_hit_latency,
        upper_half_32b_target_penalty=args.upper_half_32b_target_penalty,
        upper_half_32b_mispredict_penalty=args.upper_half_32b_mispredict_penalty,
        load_to_store_data_release_penalty=args.load_to_store_data_release_penalty,
        mul_latency_adjust=args.mul_latency_adjust,
        predictor_train_delay=args.predictor_train_delay,
        match_btb_hi=not args.no_match_btb_hi,
        bsv_hash_truncate=args.bsv_hash_truncate,
    )
    cycles = model.run(entries)
    result = compute_accuracy(entries, cycles)

    total_cycles = cycles[-1] - cycles[0] + 1 if cycles else 0
    ipc = (len(entries) / total_cycles) if total_cycles else 0.0
    if window is not None:
        runs = f" runs={window.runs}" if window.runs is not None else ""
        print(
            "window: app_log "
            f"indices=[{window.start_index}:{window.end_index}) "
            f"mcycle={window.measured_cycles} "
            f"instructions={window.measured_instructions}{runs}"
        )
    elif args.window:
        print(f"window: explicit {args.window}")
    else:
        print("window: full parsed trace")
    print(f"instructions: {len(entries)}")
    print(f"total_cycles: {total_cycles}")
    print(f"ipc: {ipc:.6f}")
    if result.accuracy is None:
        print("accuracy: unavailable (trace has no cycle stamps)")
    else:
        print(f"accuracy: {result.accuracy:.6%} ({result.matches}/{result.compared})")
        if window is not None:
            app_delta = result.model_total_cycles - window.measured_cycles
            app_pct = (app_delta / window.measured_cycles * 100.0) if window.measured_cycles else 0.0
            print(f"rtl_measured_cycles_from_app_log: {window.measured_cycles}")
            print(f"cycle_delta_vs_app_log: {app_delta} ({app_pct:+.3f}%)")
        if result.rtl_total_cycles is not None:
            delta = result.model_total_cycles - result.rtl_total_cycles
            pct = (delta / result.rtl_total_cycles * 100.0) if result.rtl_total_cycles else 0.0
            print(f"rtl_commit_span_from_trace: {result.rtl_total_cycles}")
            print(f"cycle_delta_vs_rtl_trace: {delta} ({pct:+.3f}%)")
        if result.mismatches and args.show_discrepancies:
            print()
            print(rank_discrepancies(entries, result.mismatches, limit=args.show_discrepancies))
    if not has_cycle_stamps(entries):
        return 2
    return 0


def _parse_window(value: str) -> tuple[int, int]:
    try:
        start_s, end_s = value.split(":", 1)
        start = int(start_s, 0)
        end = int(end_s, 0)
    except ValueError as exc:
        raise ValueError("window must be START:END") from exc
    if start < 0 or end < start:
        raise ValueError("window must satisfy 0 <= START <= END")
    return start, end


if __name__ == "__main__":
    raise SystemExit(main())
