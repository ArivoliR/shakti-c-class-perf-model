from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import BranchPredictor, Model, PipeEntry
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


def test_store_data_can_wait_for_same_cycle_load_release():
    model = small_model(load_to_store_data_release_penalty=1)
    model.cycle = 10
    model.load_release_cycle[("x", 1)] = 10
    store = entry(1, 0x1004, 0x00103023).insn  # sd x1, 0(x0)

    assert not model._operands_available(store)

    model.cycle = 11
    assert model._operands_available(store)


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


def test_mul_latency_adjust_extends_result_ready_cycle():
    model = small_model(mul_latency=2, mul_latency_adjust=1)
    model.cycle = 7
    mul = entry(1, 0x1004, 0x023100B3).insn

    assert model._result_ready_cycle(mul) == 10


def test_upper_half_32b_target_waits_for_frontend_visibility():
    model = small_model(enable_bpu=True)
    prev = entry(0, 0x80001C1C, 0x0C4018513)  # addi
    control = entry(1, 0x80001C20, 0xD82FF0EF)  # jal to upper-half 32-bit target
    target = entry(2, 0x800011A2, 0x0056071B)  # addiw, 32-bit at pc[1] == 1
    control.actual_next_pc = target.pc
    pipe_entry = PipeEntry(control, prev_trace=prev, next_trace=target, pred_btb_hit=True)

    assert model._frontend_redirect_penalty(pipe_entry) == 1


def test_upper_half_compressed_target_has_no_frontend_wait():
    model = small_model(enable_bpu=True)
    prev = entry(0, 0x8000119E, 0xC19C)  # c.sw
    control = entry(1, 0x800011A0, 0x8082)  # c.jr
    target = entry(2, 0x800010A2, 0x4808)  # c.lw, compressed at pc[1] == 1
    control.actual_next_pc = target.pc
    pipe_entry = PipeEntry(control, prev_trace=prev, next_trace=target, pred_btb_hit=True)

    assert model._frontend_redirect_penalty(pipe_entry) == 0


def test_load_use_branch_stall_suppresses_frontend_wait():
    model = small_model(enable_bpu=True)
    prev = entry(0, 0x80001C5E, 0x47B2)  # c.lwsp x15
    control = entry(1, 0x80001C60, 0xFEA793E3)  # bne x15, x10, upper-half target
    target = entry(2, 0x80001C46, 0xBB41C783)  # lbu, 32-bit at pc[1] == 1
    control.actual_next_pc = target.pc
    pipe_entry = PipeEntry(control, prev_trace=prev, next_trace=target, pred_btb_hit=True)

    assert model._previous_load_feeds(pipe_entry)
    assert model._frontend_redirect_penalty(pipe_entry) == 0


def test_upper_half_32b_mispredict_extra_requires_predicted_redirect():
    model = small_model(enable_bpu=True, upper_half_32b_mispredict_penalty=1)
    control = entry(1, 0x80002986, 0xFE039BE3)  # bnez, 32-bit at pc[1] == 1
    pipe_entry = PipeEntry(
        control,
        pred_btb_hit=True,
        pred_mispredict=True,
        predicted_next_pc=0x8000297C,
    )

    assert model._upper_half_32b_mispredict_extra(pipe_entry) == 1

    pipe_entry.predicted_next_pc = control.insn.fallthrough_pc
    assert model._upper_half_32b_mispredict_extra(pipe_entry) == 0


def test_predictor_training_can_be_delayed_one_cycle():
    model = small_model(enable_bpu=True, predictor_train_delay=1)
    control = entry(0, 0x80001000, 0x0080006F)  # jal x0, +8
    control.actual_next_pc = 0x80001008
    pipe_entry = PipeEntry(control, pred_state=3, pred_btb_hit=False, pred_history=0)
    model.cycle = 4

    model._schedule_predictor_training(pipe_entry)
    assert model.predictor._lookup(control.pc & ~0x3) is None

    model.cycle = 5
    model._apply_pending_predictor_training()
    assert model.predictor._lookup(control.pc & ~0x3) is not None


def test_predictor_btb_hit_can_require_compressed_hi_match():
    predictor = BranchPredictor(
        btbdepth=4,
        bhtdepth=8,
        histlen=4,
        histbits=2,
        rasdepth=2,
        compressed=True,
        match_btb_hi=True,
    )
    predictor.btb[0] = {"tag": 0x80001000 >> 2, "target": 0x80002000, "ci": "Branch", "instr16": True, "hi": True}

    assert predictor._lookup(0x80001000, hi=True) == 0
    assert predictor._lookup(0x80001000, hi=False) is None
    assert predictor._lookup(0x80001000) == 0


def test_bsv_hash_truncates_shifted_history_width_before_zero_extend():
    predictor = BranchPredictor(
        btbdepth=4,
        bhtdepth=512,
        histlen=8,
        histbits=5,
        rasdepth=2,
        compressed=True,
        bsv_hash_truncate=True,
    )
    non_truncated = BranchPredictor(
        btbdepth=4,
        bhtdepth=512,
        histlen=8,
        histbits=5,
        rasdepth=2,
        compressed=True,
        bsv_hash_truncate=False,
    )
    history = 0b10101000

    assert predictor._hash(history, 0) == 0b10000
    assert non_truncated._hash(history, 0) == 0b01010000
