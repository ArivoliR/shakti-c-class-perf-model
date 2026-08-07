"""Cycle-level performance model for the single-issue SHAKTI C-Class core.

The model consumes the committed dynamic instruction stream produced by RTL
rtldump. It models timing and hazards, not architectural data values.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
import argparse
import math
import re
from typing import Any, Deque, Optional

from accuracy import compute_accuracy, rank_discrepancies, summarize_control_discrepancies
from isa import FRF, IRF, Instruction
from trace import (
    BenchmarkWindow,
    TraceEntry,
    detect_benchmark_window,
    has_cycle_stamps,
    parse_app_log_metrics,
    parse_trace_files,
)
from trace_cache import load_or_parse_trace_files


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
    stale_frontend: bool = False
    issued_cycle: int = -1
    bundle_id: int = -1
    bundle_pos: int = 0
    bundle_size: int = 1

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


@dataclass(slots=True)
class ControlEvent:
    local_index: int
    trace_index: int
    pc: int
    name: str
    predicted_next_pc: Optional[int]
    pred_state: int
    pred_btb_hit: bool
    pred_history: int
    pred_ci: str
    pred_mispredict: bool


class FixedQueue:
    def __init__(
        self,
        capacity: int,
        *,
        allow_enq_after_deq_when_full: bool,
    ):
        self.capacity = max(1, int(capacity))
        self.items: Deque[PipeEntry] = deque()
        self.allow_enq_after_deq_when_full = allow_enq_after_deq_when_full
        self._cycle_start_len = 0
        self._enqueues_this_cycle = 0
        self._dequeues_this_cycle = 0
        self.max_occupancy = 0

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def begin_cycle(self) -> None:
        self._cycle_start_len = len(self.items)
        self._enqueues_this_cycle = 0
        self._dequeues_this_cycle = 0
        self.max_occupancy = max(self.max_occupancy, len(self.items))

    def clear(self) -> None:
        self.items.clear()
        self._cycle_start_len = 0
        self._enqueues_this_cycle = 0
        self._dequeues_this_cycle = 0
        self.max_occupancy = 0

    def can_push(self, count: int = 1) -> bool:
        if count <= 0:
            return True
        if self.allow_enq_after_deq_when_full:
            return len(self.items) + count <= self.capacity
        return self._cycle_start_len + self._enqueues_this_cycle + count <= self.capacity

    def full(self) -> bool:
        return not self.can_push()

    def space(self) -> int:
        if self.allow_enq_after_deq_when_full:
            return max(0, self.capacity - len(self.items))
        return max(0, self.capacity - self._cycle_start_len - self._enqueues_this_cycle)

    def full_at_cycle_start(self) -> bool:
        return self._cycle_start_len >= self.capacity

    def occupancy_at_cycle_start(self) -> int:
        return self._cycle_start_len

    def empty(self) -> bool:
        return not self.items

    def first(self) -> PipeEntry:
        return self.items[0]

    def peek(self, index: int) -> PipeEntry:
        return self.items[index]

    def push(self, entry: PipeEntry) -> None:
        if not self.can_push():
            raise RuntimeError("queue full")
        self.items.append(entry)
        self._enqueues_this_cycle += 1
        self.max_occupancy = max(self.max_occupancy, len(self.items))

    def pop(self) -> PipeEntry:
        self._dequeues_this_cycle += 1
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
        static_not_taken_when_disabled: bool = True,
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
        self.static_not_taken_when_disabled = static_not_taken_when_disabled
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
            if not self.static_not_taken_when_disabled:
                # Diagnostic mode (--no-bpu): suppress all modelled control
                # stalls. Does not correspond to any buildable core.
                return None, 1, False, self.ghr, "Branch", False
            # A core built without `bpu` has no BTB/BHT/RAS: stage0 just walks
            # sequentially, so every taken control transfer is resolved and
            # redirected at execute.
            fallthrough = insn.fallthrough_pc
            actual_next = entry.actual_next_pc
            mispredict = bool(
                insn.is_control and actual_next is not None and actual_next != fallthrough
            )
            return fallthrough, 1, False, self.ghr, "Branch", mispredict

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
        num_issue: int = 1,
        dual_policy: str = "single",
        isb_s0s1: int = 2,
        isb_s1s2: int = 2,
        isb_s2s3: int = 1,
        isb_s3s4: int = 8,
        isb_s4s5: int = 8,
        bypass_sources: int = 2,
        wawid: int = 4,
        mul_latency: int = 2,
        div_latency: int = 32,
        load_hit_latency: int = 0,
        store_hit_latency: int = 0,
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
        static_not_taken_when_disabled: bool = True,
        fetch_width: int = 1,
        fetch_decode_width: int = 1,
        decode_width: int = 1,
        issue_width: int = 1,
        stage4_width: int = 1,
        commit_width: int = 1,
        memory_issue_width: int = 1,
        control_issue_width: int = 1,
        dual_mem: bool = False,
        memory_pairing: str = "none",
        lockstep_bundles: bool = True,
        atomic_pair_retire: bool = True,
        allow_branch_branch: bool = False,
        symmetric_slots: bool = False,
        intra_bundle_forwarding: bool = False,
        branch_next_pc_stall: bool = True,
        relax_branch_next_pc_stall: bool = False,
        wrong_path_frontend: bool = False,
        stale_drop_fetch_penalty: int = 1,
        sized_fifo_allows_enq_after_deq_when_full: bool = False,
        lfifo_allows_enq_after_deq_when_full: bool = True,
    ) -> None:
        self.params = dict(locals())
        if dual_policy not in ("single", "generic", "shakti"):
            raise ValueError("dual_policy must be 'single', 'generic', or 'shakti'")
        if memory_pairing not in ("none", "store_involving", "all"):
            raise ValueError("memory_pairing must be 'none', 'store_involving', or 'all'")
        self.q_s0s1 = FixedQueue(
            isb_s0s1,
            allow_enq_after_deq_when_full=sized_fifo_allows_enq_after_deq_when_full,
        )
        self.q_s1s2 = FixedQueue(
            isb_s1s2,
            allow_enq_after_deq_when_full=sized_fifo_allows_enq_after_deq_when_full,
        )
        self.q_s2s3 = FixedQueue(
            isb_s2s3,
            allow_enq_after_deq_when_full=lfifo_allows_enq_after_deq_when_full,
        )
        self.q_s3s4 = FixedQueue(
            isb_s3s4,
            allow_enq_after_deq_when_full=sized_fifo_allows_enq_after_deq_when_full,
        )
        self.q_s4s5 = FixedQueue(
            isb_s4s5,
            allow_enq_after_deq_when_full=sized_fifo_allows_enq_after_deq_when_full,
        )
        self._queues = {
            "s0s1": self.q_s0s1,
            "s1s2": self.q_s1s2,
            "s2s3": self.q_s2s3,
            "s3s4": self.q_s3s4,
            "s4s5": self.q_s4s5,
        }
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
        self.enable_bpu = enable_bpu
        self.num_issue = num_issue
        self.dual_policy = dual_policy
        self.fetch_width = fetch_width
        self.fetch_decode_width = fetch_decode_width
        self.decode_width = decode_width
        self.issue_width = issue_width
        self.stage4_width = stage4_width
        self.commit_width = commit_width
        self.memory_issue_width = memory_issue_width
        self.control_issue_width = control_issue_width
        self.dual_mem = dual_mem
        self.memory_pairing = "store_involving" if dual_mem and memory_pairing == "none" else memory_pairing
        self.lockstep_bundles = lockstep_bundles
        self.atomic_pair_retire = atomic_pair_retire
        self.allow_branch_branch = allow_branch_branch
        self.symmetric_slots = symmetric_slots
        self.intra_bundle_forwarding = intra_bundle_forwarding
        self.branch_next_pc_stall = branch_next_pc_stall
        self.relax_branch_next_pc_stall = relax_branch_next_pc_stall
        self.wrong_path_frontend = wrong_path_frontend
        self.stale_drop_fetch_penalty = stale_drop_fetch_penalty
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
            static_not_taken_when_disabled=static_not_taken_when_disabled,
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
        self.control_events: list[ControlEvent] = []
        self.stale_frontend_flushed = False
        self.frontend_drop_fetch_hold = 0
        self.fetch_hold_blocked_cycle = -1
        self.next_bundle_id = 0
        self.dual_pair_bundles = 0
        self.dual_single_bundles = 0
        self.dual_one_instr_bundles = 0
        self.stage3_stall_cycles = 0
        self.queue_full_cycles: Counter[str] = Counter()
        self.dual_pair_accept_counts: Counter[str] = Counter()
        self.dual_pair_reject_counts: Counter[str] = Counter()
        self.memory_issues_this_cycle = 0
        self.control_issues_this_cycle = 0
        self.redirect_this_cycle = False
        self._stale_trace = TraceEntry(index=-1, pc=0, encoding=0x00000013, mode="0")

    @classmethod
    def from_repo(cls, repo_root: str | Path, **overrides: int | bool | str) -> "Model":
        return cls.from_makefile(Path(repo_root) / "makefile.inc", **overrides)

    @classmethod
    def from_makefile(cls, makefile_inc: str | Path, **overrides: int | bool | str) -> "Model":
        """Build a model from any makefile.inc, including an archived variant snapshot.

        Used by the held-out design-point sweep, which needs one model per RTL
        configuration without mutating the working tree.
        """
        defines = parse_makefile_defines(Path(makefile_inc))
        rasdepth = int(defines.get("rasdepth", 8)) if bool(defines.get("bpu_ras", False)) else 0
        num_issue = int(defines.get("num_issue", 1))
        s1s2_depth = int(defines.get("instr_queue", defines.get("isb_s1s2", 2))) if num_issue > 1 else int(
            defines.get("isb_s1s2", 2)
        )
        # Single-issue pipe_ifcs.bsv wires s2->s3 with mkLFIFOF(), not
        # mkSizedFIFOF(`isb_s2s3); the BSC define is inert on that path.
        s2s3_depth = int(defines.get("isb_s2s3", 1)) if num_issue > 1 else 1
        kwargs = {
            "num_issue": num_issue,
            "isb_s0s1": int(defines.get("isb_s0s1", 2)),
            "isb_s1s2": s1s2_depth,
            "isb_s2s3": s2s3_depth,
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
            "rasdepth": rasdepth,
            "enable_bpu": bool(defines.get("bpu", False)),
            "compressed": bool(defines.get("compressed", False)),
            "dual_mem": bool(defines.get("dual_mem", False)),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    def run(self, entries: list[TraceEntry]) -> list[int]:
        self._reset_runtime()
        max_cycles = max(1000, len(entries) * 80 + 1000)
        while len(self.commits) < len(entries):
            if self.cycle > max_cycles:
                raise RuntimeError(f"model did not drain after {max_cycles} cycles")
            self._begin_cycle()
            for _ in range(self.commit_width):
                self.try_commit()
            for _ in range(self.stage4_width):
                self.try_stage4()
            stage3_before = len(self.q_s2s3)
            for _ in range(self.issue_width):
                self.try_execute()
            if (
                self.dual_policy == "shakti"
                and stage3_before > 0
                and len(self.q_s2s3) == stage3_before
                and not self.q_s2s3.first().stale_frontend
            ):
                self.stage3_stall_cycles += 1
            for _ in range(self.decode_width):
                self.try_decode()
            for _ in range(self.fetch_decode_width):
                self.try_fetch_decode()
            for _ in range(self.fetch_width):
                self.try_fetch(entries)
            self.cycle += 1
        return self.commits

    def counter_profile(self, total_cycles: Optional[int] = None) -> dict[str, Any]:
        cycles = total_cycles if total_cycles is not None else (self.commits[-1] - self.commits[0] + 1 if self.commits else 0)
        raw_hazard = sum(count for name, count in self.dual_pair_reject_counts.items() if name.startswith("RAW:"))
        mem_mem_hazard = self._counter_prefix(self.dual_pair_reject_counts, "MEMORY+MEMORY")
        mem_mem_ll = self.dual_pair_reject_counts.get("MEMORY+MEMORY:LL", 0)
        mem_mem_ls = self.dual_pair_reject_counts.get("MEMORY+MEMORY:LS", 0)
        mem_mem_ss = self.dual_pair_reject_counts.get("MEMORY+MEMORY:SS", 0)
        branch_branch_rejected = self.dual_pair_reject_counts.get("CONTROL+CONTROL", 0)
        branch_branch_accepted = self.dual_pair_accept_counts.get("CONTROL+CONTROL", 0)
        dual_issued = self.dual_pair_bundles
        return {
            "cycles": cycles,
            "dual_issued": dual_issued,
            "dual_issued_pct_cycles": dual_issued / cycles if cycles else 0.0,
            "raw_hazard": raw_hazard,
            "one_instr": self.dual_one_instr_bundles,
            "st3_not_firing": self.stage3_stall_cycles,
            "mem_mem_hazard": mem_mem_hazard,
            "mem_mem_ll": mem_mem_ll,
            "mem_mem_ls": mem_mem_ls,
            "mem_mem_ss": mem_mem_ss,
            "branch_branch_rejected": branch_branch_rejected,
            "branch_branch_accepted": branch_branch_accepted,
            "branch_branch_opportunities": branch_branch_rejected + branch_branch_accepted,
            "mispredict": sum(1 for event in self.control_events if event.pred_mispredict),
            "pair_bundles": self.dual_pair_bundles,
            "single_bundles": self.dual_single_bundles,
            "paired_instructions_pct": (2 * self.dual_pair_bundles)
            / (2 * self.dual_pair_bundles + self.dual_single_bundles)
            if self.dual_pair_bundles or self.dual_single_bundles
            else 0.0,
            "accepted_pair_classes": dict(self.dual_pair_accept_counts),
            "rejected_pair_classes": dict(self.dual_pair_reject_counts),
            "queue_full_cycles": dict(self.queue_full_cycles),
            "queue_max_occupancy": {name: queue.max_occupancy for name, queue in self._queues.items()},
            "queue_capacity": {name: queue.capacity for name, queue in self._queues.items()},
        }

    @staticmethod
    def _counter_prefix(counter: Counter[str], prefix: str) -> int:
        return sum(count for name, count in counter.items() if name.startswith(prefix))

    def _begin_cycle(self) -> None:
        for name, queue in self._queues.items():
            queue.begin_cycle()
            if queue.full_at_cycle_start():
                self.queue_full_cycles[name] += 1
        self.memory_issues_this_cycle = 0
        self.control_issues_this_cycle = 0
        self.redirect_this_cycle = False

    def _reset_runtime(self) -> None:
        for queue in self._queues.values():
            queue.clear()
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
        self.control_events.clear()
        self.stale_frontend_flushed = False
        self.frontend_drop_fetch_hold = 0
        self.fetch_hold_blocked_cycle = -1
        self.next_bundle_id = 0
        self.dual_pair_bundles = 0
        self.dual_single_bundles = 0
        self.dual_one_instr_bundles = 0
        self.stage3_stall_cycles = 0
        self.queue_full_cycles.clear()
        self.dual_pair_accept_counts.clear()
        self.dual_pair_reject_counts.clear()
        self.memory_issues_this_cycle = 0
        self.control_issues_this_cycle = 0
        self.redirect_this_cycle = False

    def try_commit(self) -> bool:
        if self.dual_policy == "shakti":
            return self._try_commit_shakti()
        if self.q_s4s5.empty():
            return False
        entry = self.q_s4s5.pop()
        self._commit_entry(entry)
        return True

    def try_stage4(self) -> bool:
        if self.dual_policy == "shakti":
            return self._try_stage4_shakti()
        if self.q_s3s4.empty() or self.q_s4s5.full():
            return False
        entry = self.q_s3s4.first()
        if entry.stale_frontend:
            self.q_s3s4.pop()
            return True
        if entry.result_ready_cycle > self.cycle:
            return False
        entry = self.q_s3s4.pop()
        self._prepare_stage5_entry(entry)
        self.q_s4s5.push(entry)
        return True

    def try_execute(self) -> bool:
        if self.dual_policy == "shakti":
            return self._try_execute_shakti()
        if self.redirect_this_cycle:
            return False
        if self.q_s2s3.empty():
            return False
        entry = self.q_s2s3.first()
        if entry.stale_frontend:
            self.q_s2s3.pop()
            return True
        if self.q_s3s4.full():
            return False
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
        self._reserve_issue_resource(insn)
        entry.issued_cycle = self.cycle
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
                self.stale_frontend_flushed = True
                self.redirect_this_cycle = True
            self._schedule_predictor_training(entry)
        if insn.is_div:
            self.div_busy_until = self.cycle + self.div_latency
        if insn.fu == "TRAP" or insn.name == "xret" or insn.is_fence_i:
            self.flush_countdown = max(self.flush_countdown, self.wb_flush_penalty)
            self.redirect_this_cycle = True
        return True

    def try_decode(self) -> bool:
        if self.dual_policy == "shakti":
            return self._try_decode_shakti()
        if self.q_s1s2.empty():
            return False
        if self.q_s1s2.first().stale_frontend and self.stale_frontend_flushed:
            self.q_s1s2.pop()
            return True
        if self.q_s2s3.full():
            return False
        self.q_s2s3.push(self.q_s1s2.pop())
        return True

    def try_fetch_decode(self) -> bool:
        if self.q_s0s1.empty():
            return False
        if self.q_s0s1.first().stale_frontend and self.stale_frontend_flushed:
            self.q_s0s1.pop()
            self.frontend_drop_fetch_hold = max(self.frontend_drop_fetch_hold, self.stale_drop_fetch_penalty)
            return True
        if self.q_s1s2.full():
            return False
        self.q_s1s2.push(self.q_s0s1.pop())
        return True

    def _try_decode_shakti(self) -> bool:
        if self.q_s1s2.empty():
            return False
        if self.q_s1s2.first().stale_frontend and self.stale_frontend_flushed:
            self.q_s1s2.pop()
            return True
        # s2->s3 is a one-entry vector FIFO. Even though the model stores
        # scalar trace entries internally, a new bundle can enter only when
        # the whole vector slot is free.
        if self.num_issue <= 2 and not self.q_s2s3.empty():
            return False

        first = self.q_s1s2.first()
        issue_two = False
        if len(self.q_s1s2) >= 2:
            second = self.q_s1s2.peek(1)
            if not second.stale_frontend:
                issue_two, reason = self._shakti_pair_decision(first.insn, second.insn)
                if issue_two:
                    self.dual_pair_accept_counts[reason] += 1
                else:
                    self.dual_pair_reject_counts[reason] += 1
        else:
            self.dual_one_instr_bundles += 1

        bundle_size = 2 if issue_two else 1
        if self.q_s2s3.space() < bundle_size:
            return False

        bundle_id = self.next_bundle_id
        self.next_bundle_id += 1
        if bundle_size == 2:
            self.dual_pair_bundles += 1
        else:
            self.dual_single_bundles += 1
        for pos in range(bundle_size):
            entry = self.q_s1s2.pop()
            entry.bundle_id = bundle_id
            entry.bundle_pos = pos
            entry.bundle_size = bundle_size
            self.q_s2s3.push(entry)
        return True

    def _try_execute_shakti(self) -> bool:
        if not self.lockstep_bundles:
            return self._try_execute_shakti_decoupled()
        if self.redirect_this_cycle or self.q_s2s3.empty():
            return False
        first = self.q_s2s3.first()
        if first.stale_frontend:
            self.q_s2s3.pop()
            return True

        bundle = self._head_bundle(self.q_s2s3)
        if not bundle:
            return False
        if self.q_s3s4.space() < len(bundle):
            return False
        if not self._bundle_fu_ready(bundle):
            return False
        if not self._branch_next_pc_ready(bundle):
            return False

        for entry in bundle:
            insn = entry.insn
            if not self._operands_available(insn):
                return False
            frontend_penalty = self._frontend_redirect_penalty(entry)
            if entry.frontend_waited_cycles < frontend_penalty:
                entry.frontend_waited_cycles += 1
                return False

        issued_entries = [self.q_s2s3.pop() for _ in bundle]
        for entry in issued_entries:
            insn = entry.insn
            self._reserve_issue_resource(insn)
            entry.issued_cycle = self.cycle
            self._lock_scoreboard(entry)
            entry.result_ready_cycle = self._result_ready_cycle(insn)
            entry.bypassable = insn.writes_scoreboard and self._is_base_result_available_in_s3s4(insn)
            entry.bypass_ready_cycle = self.cycle
            entry.wb_kind = self._initial_wb_kind(insn)
            self.q_s3s4.push(entry)

        for entry in issued_entries:
            insn = entry.insn
            if insn.is_control:
                if entry.pred_mispredict:
                    self.predictor.restore_after_mispredict(entry.pred_btb_hit, entry.pred_history)
                    penalty = self.branch_mispredict_penalty + self._upper_half_32b_mispredict_extra(entry)
                    self.flush_countdown = max(self.flush_countdown, penalty)
                    if self.fetch_blocked_by == entry.trace.index:
                        self.fetch_blocked_by = None
                    self.stale_frontend_flushed = True
                    self.redirect_this_cycle = True
                self._schedule_predictor_training(entry)
            if insn.is_div:
                self.div_busy_until = self.cycle + self.div_latency
            if insn.fu == "TRAP" or insn.name == "xret" or insn.is_fence_i:
                self.flush_countdown = max(self.flush_countdown, self.wb_flush_penalty)
                self.redirect_this_cycle = True
        return True

    def _try_execute_shakti_decoupled(self) -> bool:
        if self.redirect_this_cycle or self.q_s2s3.empty():
            return False
        entry = self.q_s2s3.first()
        if entry.stale_frontend:
            self.q_s2s3.pop()
            return True
        if self.q_s3s4.full():
            return False

        insn = entry.insn
        if not self._fu_ready(insn):
            return False
        if not self._branch_next_pc_ready([entry]):
            return False
        if not self._operands_available(insn):
            return False
        frontend_penalty = self._frontend_redirect_penalty(entry)
        if entry.frontend_waited_cycles < frontend_penalty:
            entry.frontend_waited_cycles += 1
            return False

        entry = self.q_s2s3.pop()
        self._reserve_issue_resource(insn)
        entry.issued_cycle = self.cycle
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
                self.stale_frontend_flushed = True
                self.redirect_this_cycle = True
            self._schedule_predictor_training(entry)
        if insn.is_div:
            self.div_busy_until = self.cycle + self.div_latency
        if insn.fu == "TRAP" or insn.name == "xret" or insn.is_fence_i:
            self.flush_countdown = max(self.flush_countdown, self.wb_flush_penalty)
            self.redirect_this_cycle = True
        return True

    def _try_stage4_shakti(self) -> bool:
        if not self.lockstep_bundles:
            return self._try_stage4_shakti_decoupled()
        if self.q_s3s4.empty():
            return False
        first = self.q_s3s4.first()
        if first.stale_frontend:
            self.q_s3s4.pop()
            return True

        bundle = self._head_bundle(self.q_s3s4)
        if not bundle:
            return False
        if self.q_s4s5.space() < len(bundle):
            return False
        if any(entry.result_ready_cycle > self.cycle for entry in bundle):
            return False

        moved_entries = [self.q_s3s4.pop() for _ in bundle]
        for entry in moved_entries:
            self._prepare_stage5_entry(entry)
            self.q_s4s5.push(entry)
        return True

    def _try_stage4_shakti_decoupled(self) -> bool:
        if self.q_s3s4.empty() or self.q_s4s5.full():
            return False
        entry = self.q_s3s4.first()
        if entry.stale_frontend:
            self.q_s3s4.pop()
            return True
        if entry.result_ready_cycle > self.cycle:
            return False
        entry = self.q_s3s4.pop()
        self._prepare_stage5_entry(entry)
        self.q_s4s5.push(entry)
        return True

    def _try_commit_shakti(self) -> bool:
        if self.q_s4s5.empty():
            return False
        first = self.q_s4s5.first()
        if first.stale_frontend:
            self.q_s4s5.pop()
            return True
        if not self.atomic_pair_retire:
            self._commit_entry(self.q_s4s5.pop())
            return True

        bundle = self._head_bundle(self.q_s4s5)
        if not bundle:
            return False

        committed_entries = [self.q_s4s5.pop() for _ in bundle]
        for entry in committed_entries:
            self._commit_entry(entry)
        return True

    def _head_bundle(self, queue: FixedQueue) -> list[PipeEntry]:
        if queue.empty():
            return []
        first = queue.first()
        if first.bundle_size <= 1 or first.bundle_id < 0:
            return [first]
        if len(queue) < first.bundle_size:
            return []
        bundle = [queue.peek(idx) for idx in range(first.bundle_size)]
        if any(entry.bundle_id != first.bundle_id for entry in bundle):
            return []
        return bundle

    def _prepare_stage5_entry(self, entry: PipeEntry) -> None:
        entry.bypassable = entry.insn.writes_scoreboard
        if entry.insn.is_load or entry.insn.is_mul or entry.insn.is_div or entry.insn.is_float:
            entry.bypass_ready_cycle = self.cycle + 1
        else:
            entry.bypass_ready_cycle = self.cycle
        entry.wb_kind = self._initial_wb_kind(entry.insn)

    def _commit_entry(self, entry: PipeEntry) -> None:
        if entry.stale_frontend:
            return
        if entry.insn.is_load and entry.rd_key is not None:
            self.load_release_cycle[entry.rd_key] = self.cycle
        self._release_scoreboard(entry)
        if entry.wb_kind == "SYSTEM" and (entry.insn.is_csr or entry.insn.name == "xret"):
            # CSR responses are normally single-cycle in this CSRBox, but the
            # constructor keeps this explicit for architectural experiments.
            pass
        self.commits.append(self.cycle)

    def try_fetch(self, entries: list[TraceEntry]) -> bool:
        self._apply_pending_predictor_training()
        if self.fetch_index >= len(entries) or self.q_s0s1.full():
            return False
        if self._fetch_hold_active():
            return False
        if self.fetch_blocked_by is not None:
            if self.wrong_path_frontend:
                self.q_s0s1.push(self._make_stale_frontend_entry())
                return True
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
            self.control_events.append(
                ControlEvent(
                    local_index=self.fetch_index,
                    trace_index=trace_entry.index,
                    pc=trace_entry.pc,
                    name=trace_entry.insn.name,
                    predicted_next_pc=pipe_entry.predicted_next_pc,
                    pred_state=pipe_entry.pred_state,
                    pred_btb_hit=pipe_entry.pred_btb_hit,
                    pred_history=pipe_entry.pred_history,
                    pred_ci=pipe_entry.pred_ci,
                    pred_mispredict=pipe_entry.pred_mispredict,
                )
            )
            if pipe_entry.pred_mispredict:
                self.fetch_blocked_by = trace_entry.index
                self.stale_frontend_flushed = False
        self.q_s0s1.push(pipe_entry)
        self.fetch_index += 1
        return True

    def _make_stale_frontend_entry(self) -> PipeEntry:
        return PipeEntry(self._stale_trace, stale_frontend=True)

    def _fetch_hold_active(self) -> bool:
        if self.fetch_hold_blocked_cycle == self.cycle:
            return True
        if self.flush_countdown > 0:
            self.flush_countdown -= 1
            self.fetch_hold_blocked_cycle = self.cycle
            return True
        if self.frontend_drop_fetch_hold > 0:
            self.frontend_drop_fetch_hold -= 1
            self.fetch_hold_blocked_cycle = self.cycle
            return True
        return False

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

    def _can_pair_shakti(self, first: Instruction, second: Instruction) -> bool:
        return self._shakti_pair_decision(first, second)[0]

    def _shakti_pair_decision(self, first: Instruction, second: Instruction) -> tuple[bool, str]:
        first_kind = self._shakti_pair_kind(first)
        second_kind = self._shakti_pair_kind(second)
        pair_name = f"{first_kind}+{second_kind}"
        if self._has_intra_bundle_raw(first, second):
            if not self._allows_intra_bundle_raw(first, second, first_kind, second_kind):
                return False, f"RAW:{pair_name}"
            pair_name = f"RAW_FWD:{pair_name}"
        if first_kind in ("SYSTEM", "TRAP", "WFI", "OTHER") or second_kind in ("SYSTEM", "TRAP", "WFI", "OTHER"):
            return False, pair_name
        if first_kind == "MEMORY" and second_kind == "MEMORY":
            memory_name = self._memory_pair_name(first, second)
            if self.memory_pairing == "all":
                return True, memory_name
            if self.memory_pairing == "store_involving":
                return (first.is_store or second.is_store), memory_name
            return False, memory_name
        if self.symmetric_slots and self._symmetric_slots_can_pair(first_kind, second_kind):
            return True, pair_name
        if first_kind == "ALU" and second_kind in ("ALU", "MULDIV", "FLOAT", "MEMORY", "CONTROL"):
            return True, pair_name
        if second_kind == "ALU" and first_kind in ("MULDIV", "FLOAT", "MEMORY", "CONTROL"):
            return True, pair_name
        if first_kind == "CONTROL" and second_kind in ("MULDIV", "FLOAT", "MEMORY"):
            return True, pair_name
        if second_kind == "CONTROL" and first_kind in ("MULDIV", "FLOAT", "MEMORY"):
            return True, pair_name
        if self.allow_branch_branch and first_kind == "CONTROL" and second_kind == "CONTROL":
            return True, pair_name
        return False, pair_name

    def _allows_intra_bundle_raw(
        self,
        first: Instruction,
        second: Instruction,
        first_kind: str,
        second_kind: str,
    ) -> bool:
        if not self.intra_bundle_forwarding:
            return False
        return first_kind == "ALU" and second_kind == "ALU" and first.writes_scoreboard

    def _symmetric_slots_can_pair(self, first_kind: str, second_kind: str) -> bool:
        if first_kind == "CONTROL" and second_kind == "CONTROL":
            return self.allow_branch_branch
        if first_kind == "MEMORY" and second_kind == "MEMORY":
            return self.memory_pairing != "none"
        if first_kind == second_kind and first_kind in ("MULDIV", "FLOAT"):
            return False
        return first_kind in ("ALU", "MULDIV", "FLOAT", "MEMORY", "CONTROL") and second_kind in (
            "ALU",
            "MULDIV",
            "FLOAT",
            "MEMORY",
            "CONTROL",
        )

    def _memory_pair_name(self, first: Instruction, second: Instruction) -> str:
        if first.is_load and second.is_load:
            return "MEMORY+MEMORY:LL"
        if first.is_store and second.is_store:
            return "MEMORY+MEMORY:SS"
        if (first.is_load and second.is_store) or (first.is_store and second.is_load):
            return "MEMORY+MEMORY:LS"
        return "MEMORY+MEMORY:OTHER"

    def _bundle_fu_ready(self, bundle: list[PipeEntry]) -> bool:
        memory_count = self.memory_issues_this_cycle
        control_count = self.control_issues_this_cycle
        for entry in bundle:
            insn = entry.insn
            if insn.is_div and self.cycle < self.div_busy_until:
                return False
            if self._uses_memory_issue_resource(insn):
                memory_count += 1
                if memory_count > self.memory_issue_width:
                    return False
            if insn.is_control:
                control_count += 1
                if control_count > self.control_issue_width:
                    return False
        return True

    def _branch_next_pc_ready(self, bundle: list[PipeEntry]) -> bool:
        if not self.branch_next_pc_stall or not self.enable_bpu:
            return True
        controls = [entry for entry in bundle if entry.insn.is_control]
        if not controls:
            return True
        if any(self.fetch_blocked_by == entry.trace.index for entry in controls):
            return True
        if not self.q_s1s2.empty():
            return True
        if self.relax_branch_next_pc_stall:
            return all(self._control_successor_in_bundle(entry, bundle) for entry in controls)
        return False

    def _control_successor_in_bundle(self, entry: PipeEntry, bundle: list[PipeEntry]) -> bool:
        actual_next = entry.trace.actual_next_pc
        if actual_next is None:
            return False
        return any(other is not entry and other.trace.pc == actual_next for other in bundle)

    def _has_intra_bundle_raw(self, first: Instruction, second: Instruction) -> bool:
        if not first.writes_scoreboard:
            return False
        key = (first.rd_type, first.rd)
        return key in second.source_regs()

    def _shakti_pair_kind(self, insn: Instruction) -> str:
        if insn.is_wfi:
            return "WFI"
        if insn.is_trap or insn.fu == "TRAP":
            return "TRAP"
        if insn.fu == "SYSTEM" or insn.is_csr:
            return "SYSTEM"
        if insn.is_control:
            return "CONTROL"
        if insn.is_load or insn.is_store or insn.is_atomic or insn.is_fence or insn.is_fence_i:
            return "MEMORY"
        if insn.is_mul or insn.is_div or insn.fu == "MULDIV":
            return "MULDIV"
        if insn.is_float or insn.fu == "FLOAT":
            return "FLOAT"
        if insn.fu == "ALU":
            return "ALU"
        return "OTHER"

    def _fu_ready(self, insn: Instruction) -> bool:
        if insn.is_div:
            return self.cycle >= self.div_busy_until
        if self._uses_memory_issue_resource(insn) and self.memory_issues_this_cycle >= self.memory_issue_width:
            return False
        if insn.is_control and self.control_issues_this_cycle >= self.control_issue_width:
            return False
        return True

    def _reserve_issue_resource(self, insn: Instruction) -> None:
        if self._uses_memory_issue_resource(insn):
            self.memory_issues_this_cycle += 1
        if insn.is_control:
            self.control_issues_this_cycle += 1

    def _uses_memory_issue_resource(self, insn: Instruction) -> bool:
        return insn.is_load or insn.is_store or insn.is_atomic or insn.is_fence or insn.is_fence_i

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
            sources.extend(self._bypass_entries(self.q_s3s4))
        if self.bypass_sources >= 2 and not self.q_s4s5.empty():
            sources.extend(self._bypass_entries(self.q_s4s5))
        for entry in sources:
            if not entry.bypassable:
                continue
            if entry.issued_cycle == self.cycle and not self.intra_bundle_forwarding:
                continue
            if entry.bypass_ready_cycle > self.cycle:
                continue
            if entry.rd_key != key:
                continue
            if entry.scoreboard_id == locked_id:
                return True
        return False

    def _bypass_entries(self, queue: FixedQueue) -> list[PipeEntry]:
        if self.dual_policy != "shakti":
            return [queue.first()]
        bundle = self._head_bundle(queue)
        if len(bundle) == 2:
            # RTL bypass priority within a source is slot1 before slot0. The
            # scoreboard id disambiguates WAW cases, so timing only requires
            # both lanes to be visible.
            return [bundle[1], bundle[0]]
        return bundle

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
        if key in self.scoreboard and self.scoreboard.get(key) == entry.scoreboard_id:
            del self.scoreboard[key]

    def _result_ready_cycle(self, insn: Instruction) -> int:
        if insn.is_load or insn.is_atomic:
            return self.cycle + 1 + self.load_hit_latency
        if insn.is_store or insn.is_fence or insn.is_fence_i:
            return self.cycle + 1 + self.store_hit_latency
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
    parser.add_argument("--trace-cache", default=".trace-cache", help="Directory for parsed trace cache")
    parser.add_argument("--no-trace-cache", action="store_true", help="Disable parsed trace caching")
    parser.add_argument("--predict-only", action="store_true", help="Skip RTL accuracy and delta comparison")
    parser.add_argument("--no-auto-window", action="store_true", help="Use the whole parsed trace instead of the app_log IPC window")
    parser.add_argument("--window", metavar="START:END", help="Use an explicit 0-based trace index window, END exclusive")
    parser.add_argument("--dual-issue", action="store_true", help="Use the actual SHAKTI dual-issue prediction preset")
    parser.add_argument("--generic-dual-issue", action="store_true", help="Use the older independent-lane two-wide experiment")
    parser.add_argument("--fetch-width", type=int)
    parser.add_argument("--fetch-decode-width", type=int)
    parser.add_argument("--decode-width", type=int)
    parser.add_argument("--issue-width", type=int)
    parser.add_argument("--stage4-width", type=int)
    parser.add_argument("--commit-width", type=int)
    parser.add_argument("--memory-issue-width", type=int)
    parser.add_argument("--control-issue-width", type=int)
    parser.add_argument("--dual-mem", action="store_true", help="Allow the gated dual_mem memory pairing rule")
    parser.add_argument(
        "--memory-pairing",
        choices=("none", "store_involving", "all"),
        default=None,
        help="Experimental MEM+MEM pairing policy",
    )
    parser.add_argument(
        "--decouple-lockstep",
        action="store_true",
        help="Experimental: let paired slots leave execute/stage4 independently while keeping atomic pair commit",
    )
    parser.add_argument(
        "--independent-retire",
        action="store_true",
        help="Experimental: allow stage5 to retire paired slots independently",
    )
    parser.add_argument(
        "--allow-branch-branch",
        action="store_true",
        help="Experimental: allow CONTROL+CONTROL decode pairing; use with --control-issue-width 2",
    )
    parser.add_argument("--symmetric-slots", action="store_true", help="Experimental: relax slot-0-only scarce-FU pairing")
    parser.add_argument("--intra-bundle-forwarding", action="store_true")
    parser.add_argument(
        "--relax-branch-next-pc-stall",
        action="store_true",
        help="Experimental: do not require the decoded queue head when a control successor is already in the bundle",
    )
    parser.add_argument("--isb-s0s1", type=int)
    parser.add_argument("--isb-s1s2", type=int)
    parser.add_argument("--isb-s2s3", type=int)
    parser.add_argument("--isb-s3s4", type=int)
    parser.add_argument("--isb-s4s5", type=int)
    parser.add_argument("--mispredict-penalty", type=int, default=1)
    parser.add_argument("--load-hit-latency", type=int, default=0)
    parser.add_argument("--store-hit-latency", type=int, default=0)
    parser.add_argument("--upper-half-32b-target-penalty", type=int, default=1)
    parser.add_argument("--upper-half-32b-mispredict-penalty", type=int, default=0)
    parser.add_argument("--load-to-store-data-release-penalty", type=int, default=0)
    parser.add_argument("--mul-latency-adjust", type=int, default=0)
    parser.add_argument("--predictor-train-delay", type=int, default=1)
    parser.add_argument(
        "--wrong-path-frontend",
        action="store_true",
        help="Model stale wrong-path front-end packets before an execute-stage redirect resolves",
    )
    parser.add_argument(
        "--stale-drop-fetch-penalty",
        type=int,
        default=1,
        help="Fetch hold cycles after a stale s0/s1 wrong-path packet is dropped",
    )
    parser.add_argument("--no-match-btb-hi", action="store_true")
    parser.add_argument("--no-bsv-hash-truncate", dest="bsv_hash_truncate", action="store_false")
    parser.add_argument("--no-bpu", action="store_true", help="Disable modeled BPU/control redirect stalls for diagnostics")
    parser.set_defaults(bsv_hash_truncate=True)
    parser.add_argument("--show-discrepancies", type=int, default=12)
    parser.add_argument("--show-control-discrepancies", type=int, default=0)
    args = parser.parse_args()

    if args.no_trace_cache:
        parsed_entries = parse_trace_files(args.trace_files, limit=args.limit)
    else:
        parsed_entries = load_or_parse_trace_files(args.trace_files, cache_dir=args.trace_cache, limit=args.limit)
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

    model = Model.from_repo(args.repo_root, **_model_overrides_from_args(args))
    cycles = model.run(entries)
    result = None if args.predict_only else compute_accuracy(entries, cycles)

    total_cycles = cycles[-1] - cycles[0] + 1 if cycles else 0
    ipc = (len(entries) / total_cycles) if total_cycles else 0.0
    if args.dual_issue or args.generic_dual_issue:
        print("mode: dual_issue_prediction")
        print(
            "prediction_assumptions: "
            f"policy={model.dual_policy} "
            f"fetch={model.fetch_width} "
            f"fetch_decode={model.fetch_decode_width} "
            f"decode={model.decode_width} "
            f"issue={model.issue_width} "
            f"stage4={model.stage4_width} "
            f"commit={model.commit_width} "
            f"mem_issue={model.memory_issue_width} "
            f"control_issue={model.control_issue_width} "
            f"memory_pairing={model.memory_pairing} "
            f"lockstep={int(model.lockstep_bundles)} "
            f"atomic_retire={int(model.atomic_pair_retire)} "
            f"branch_branch={int(model.allow_branch_branch)} "
            f"symmetric_slots={int(model.symmetric_slots)} "
            f"intra_bundle_forwarding={int(model.intra_bundle_forwarding)}"
        )
        if model.dual_pair_bundles or model.dual_single_bundles:
            paired_insts = model.dual_pair_bundles * 2
            issued_insts = paired_insts + model.dual_single_bundles
            pair_rate = paired_insts / issued_insts if issued_insts else 0.0
            print(
                "decode_bundles: "
                f"pairs={model.dual_pair_bundles} "
                f"singles={model.dual_single_bundles} "
                f"paired_instructions={pair_rate:.3%}"
            )
            if model.dual_pair_accept_counts:
                print("accepted_pair_classes: " + _format_counter(model.dual_pair_accept_counts, limit=8))
            if model.dual_pair_reject_counts:
                print("rejected_pair_classes: " + _format_counter(model.dual_pair_reject_counts, limit=8))
            profile = model.counter_profile()
            print(
                "counter_profile: "
                f"dual_issued={profile['dual_issued']} "
                f"dual_issued_pct_cycles={profile['dual_issued_pct_cycles']:.3%} "
                f"raw_hazard={profile['raw_hazard']} "
                f"one_instr={profile['one_instr']} "
                f"st3_not_firing={profile['st3_not_firing']} "
                f"mem_mem_hazard={profile['mem_mem_hazard']} "
                f"mem_ll={profile['mem_mem_ll']} "
                f"mem_ls={profile['mem_mem_ls']} "
                f"mem_ss={profile['mem_mem_ss']} "
                f"branch_branch={profile['branch_branch_opportunities']} "
                f"mispredict={profile['mispredict']}"
            )
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
    if result is None:
        print("accuracy: skipped (predict-only)")
    elif result.accuracy is None:
        print("accuracy: unavailable (trace has no cycle stamps)")
    else:
        label = "accuracy_vs_supplied_rtl" if args.dual_issue else "accuracy"
        print(f"{label}: {result.accuracy:.6%} ({result.matches}/{result.compared})")
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
        if result.mismatches and args.show_control_discrepancies:
            print()
            print(
                summarize_control_discrepancies(
                    entries,
                    result.mismatches,
                    limit=args.show_control_discrepancies,
                )
            )
    if result is None:
        return 0
    if not has_cycle_stamps(entries):
        return 2
    return 0


def _model_overrides_from_args(args: argparse.Namespace) -> dict[str, int | bool | str]:
    overrides: dict[str, int | bool | str] = {
        "branch_mispredict_penalty": args.mispredict_penalty,
        "load_hit_latency": args.load_hit_latency,
        "store_hit_latency": args.store_hit_latency,
        "upper_half_32b_target_penalty": args.upper_half_32b_target_penalty,
        "upper_half_32b_mispredict_penalty": args.upper_half_32b_mispredict_penalty,
        "load_to_store_data_release_penalty": args.load_to_store_data_release_penalty,
        "mul_latency_adjust": args.mul_latency_adjust,
        "predictor_train_delay": args.predictor_train_delay,
        "wrong_path_frontend": args.wrong_path_frontend,
        "stale_drop_fetch_penalty": args.stale_drop_fetch_penalty,
        "match_btb_hi": not args.no_match_btb_hi,
        "bsv_hash_truncate": args.bsv_hash_truncate,
        "enable_bpu": not args.no_bpu,
        # --no-bpu stays a diagnostic that removes control stalls entirely.
        # A core actually built without `bpu` is modelled by deriving
        # enable_bpu from makefile.inc, which keeps static-not-taken behaviour.
        "static_not_taken_when_disabled": False,
        "dual_mem": args.dual_mem,
        "memory_pairing": args.memory_pairing
        if args.memory_pairing is not None
        else ("store_involving" if args.dual_mem else "none"),
        "lockstep_bundles": not args.decouple_lockstep,
        "atomic_pair_retire": not args.independent_retire,
        "allow_branch_branch": args.allow_branch_branch,
        "symmetric_slots": args.symmetric_slots,
        "intra_bundle_forwarding": args.intra_bundle_forwarding,
        "relax_branch_next_pc_stall": args.relax_branch_next_pc_stall,
    }
    if args.dual_issue:
        overrides.update(
            {
                "num_issue": 2,
                "dual_policy": "shakti",
                "fetch_width": 2,
                "fetch_decode_width": 2,
                "decode_width": 1,
                "issue_width": 2 if args.decouple_lockstep else 1,
                "stage4_width": 2 if args.decouple_lockstep else 1,
                "commit_width": 1,
                "memory_issue_width": 1,
                "control_issue_width": 1,
                "isb_s0s1": 4,
                "isb_s1s2": 6,
                "isb_s2s3": 2,
                "isb_s3s4": 16,
                "isb_s4s5": 16,
                "dual_mem": args.dual_mem,
                "memory_pairing": args.memory_pairing
                if args.memory_pairing is not None
                else ("store_involving" if args.dual_mem else "none"),
                "lockstep_bundles": not args.decouple_lockstep,
                "atomic_pair_retire": not args.independent_retire,
                "allow_branch_branch": args.allow_branch_branch,
                "symmetric_slots": args.symmetric_slots,
                "intra_bundle_forwarding": args.intra_bundle_forwarding,
            }
        )
    elif args.generic_dual_issue:
        overrides.update(
            {
                "num_issue": 2,
                "dual_policy": "generic",
                "fetch_width": 2,
                "fetch_decode_width": 2,
                "decode_width": 2,
                "issue_width": 2,
                "stage4_width": 2,
                "commit_width": 2,
                "memory_issue_width": 1,
                "control_issue_width": 1,
                "isb_s0s1": 4,
                "isb_s1s2": 4,
                "isb_s2s3": 2,
            }
        )

    for arg_name, param_name in (
        ("fetch_width", "fetch_width"),
        ("fetch_decode_width", "fetch_decode_width"),
        ("decode_width", "decode_width"),
        ("issue_width", "issue_width"),
        ("stage4_width", "stage4_width"),
        ("commit_width", "commit_width"),
        ("memory_issue_width", "memory_issue_width"),
        ("control_issue_width", "control_issue_width"),
        ("isb_s0s1", "isb_s0s1"),
        ("isb_s1s2", "isb_s1s2"),
        ("isb_s2s3", "isb_s2s3"),
        ("isb_s3s4", "isb_s3s4"),
        ("isb_s4s5", "isb_s4s5"),
    ):
        value = getattr(args, arg_name)
        if value is not None:
            overrides[param_name] = value
    return overrides


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


def _format_counter(counter: Counter[str], *, limit: int) -> str:
    total = sum(counter.values())
    parts = []
    for name, count in counter.most_common(limit):
        pct = (count / total * 100.0) if total else 0.0
        parts.append(f"{name}={count}({pct:.1f}%)")
    return ", ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
