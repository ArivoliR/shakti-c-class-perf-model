from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import Model, PipeEntry
from trace import TraceEntry


def entry(index, pc, enc):
    return TraceEntry(index=index, pc=pc, encoding=enc, mode="3")


def annotate(entries):
    for idx, ent in enumerate(entries[:-1]):
        ent.actual_next_pc = entries[idx + 1].pc
    entries[-1].actual_next_pc = None
    return entries


def small_model(**kwargs):
    params = dict(
        isb_s0s1=2,
        isb_s1s2=2,
        isb_s2s3=1,
        isb_s3s4=4,
        isb_s4s5=4,
        enable_bpu=False,
        load_hit_latency=3,
        mul_latency=4,
        div_latency=8,
    )
    params.update(kwargs)
    return Model(**params)


def test_alu_result_bypasses_from_downstream_head():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
            entry(1, 0x1004, 0x00108113),  # addi x2, x1, 1
            entry(2, 0x1008, 0x00110193),  # addi x3, x2, 1
        ]
    )
    cycles = small_model().run(entries)
    assert cycles[1] - cycles[0] == 1
    assert cycles[2] - cycles[1] == 1


def test_load_result_is_not_bypassable_from_s3s4_memory_slot():
    model = small_model()
    load = PipeEntry(entry(0, 0x1000, 0x00003083))  # ld x1, 0(x0)
    load.scoreboard_id = 3
    load.bypassable = False
    model.scoreboard[("x", 1)] = 3
    model.q_s3s4.push(load)

    consumer = entry(1, 0x1004, 0x00108113).insn  # addi x2, x1, 1
    assert not model._operands_available(consumer)

    model.q_s3s4.pop()
    load.bypassable = True
    model.q_s4s5.push(load)
    assert model._operands_available(consumer)


def test_divider_ready_is_structural():
    model = small_model()
    model.cycle = 10
    model.div_busy_until = 18
    div = entry(0, 0x1000, 0x023140B3).insn
    mul = entry(1, 0x1004, 0x023100B3).insn
    assert not model._fu_ready(div)
    assert model._fu_ready(mul)
    model.cycle = 18
    assert model._fu_ready(div)
