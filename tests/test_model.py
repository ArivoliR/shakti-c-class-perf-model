from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import BranchPredictor, FixedQueue, Model, PipeEntry
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
        load_hit_latency=2,
        mul_latency=4,
        div_latency=8,
    )
    params.update(kwargs)
    return Model(**params)


def dual_model(**kwargs):
    params = dict(
        isb_s0s1=4,
        isb_s1s2=6,
        isb_s2s3=2,
        isb_s3s4=16,
        isb_s4s5=16,
        enable_bpu=False,
        num_issue=2,
        dual_policy="shakti",
        fetch_width=2,
        fetch_decode_width=2,
        decode_width=1,
        issue_width=1,
        stage4_width=1,
        commit_width=1,
        memory_issue_width=1,
        load_hit_latency=0,
        mul_latency=4,
        div_latency=8,
    )
    params.update(kwargs)
    return Model(**params)


def _fifo_packet(index):
    return PipeEntry(entry(index, 0x1000 + 4 * index, 0x00100093))


def _producer_consumer_cycles(queue: FixedQueue, packets: int) -> int:
    produced = 0
    consumed = 0
    cycles = 0
    while consumed < packets:
        queue.begin_cycle()
        if not queue.empty():
            queue.pop()
            consumed += 1
        if produced < packets and not queue.full():
            queue.push(_fifo_packet(produced))
            produced += 1
        cycles += 1
    return cycles


def test_guarded_depth_one_fifo_cannot_enqueue_after_same_cycle_dequeue_from_full():
    queue = FixedQueue(1, allow_enq_after_deq_when_full=False)
    queue.begin_cycle()
    queue.push(_fifo_packet(0))

    queue.begin_cycle()
    assert queue.full_at_cycle_start()
    queue.pop()
    assert queue.empty()
    assert queue.full()
    with pytest.raises(RuntimeError):
        queue.push(_fifo_packet(1))


def test_depth_one_guarded_fifo_halves_streaming_throughput():
    packets = 8
    guarded = FixedQueue(1, allow_enq_after_deq_when_full=False)
    loopy = FixedQueue(1, allow_enq_after_deq_when_full=True)

    assert _producer_consumer_cycles(guarded, packets) == 2 * packets
    assert _producer_consumer_cycles(loopy, packets) == packets + 1


def test_single_issue_stage1_splits_two_compressed_instructions_from_one_fetch_word():
    entries = annotate(
        [
            entry(0, 0x1000, 0x0001),  # c.nop, lower half
            entry(1, 0x1002, 0x0001),  # c.nop, upper half from same fetch word
            entry(2, 0x1004, 0x00100093),  # addi x1, x0, 1
        ]
    )

    split = small_model(isb_s0s1=1).run(entries)
    no_split = small_model(isb_s0s1=1, stage1_split_compressed_fetch_words=False).run(entries)

    assert len(split) == len(no_split) == len(entries)
    assert split[-1] < no_split[-1]


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


def test_dual_issue_pairs_independent_alu_instructions():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
            entry(1, 0x1004, 0x00200113),  # addi x2, x0, 2
            entry(2, 0x1008, 0x00300193),  # addi x3, x0, 3
            entry(3, 0x100C, 0x00400213),  # addi x4, x0, 4
        ]
    )

    cycles = dual_model().run(entries)

    assert cycles == [5, 5, 6, 6]


def test_dual_issue_dependent_alu_waits_without_intra_bundle_forwarding():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
            entry(1, 0x1004, 0x00108113),  # addi x2, x1, 1
            entry(2, 0x1008, 0x00300193),  # addi x3, x0, 3
        ]
    )

    cycles = dual_model().run(entries)

    assert cycles == [5, 6, 6]


def test_shakti_dual_issue_blocks_intra_bundle_raw_by_default():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
            entry(1, 0x1004, 0x00108113),  # addi x2, x1, 1
            entry(2, 0x1008, 0x00300193),  # addi x3, x0, 3
        ]
    )

    cycles = dual_model().run(entries)

    assert cycles == [5, 6, 6]


def test_intra_bundle_forwarding_experiment_allows_alu_to_alu_raw_pair():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
            entry(1, 0x1004, 0x00108113),  # addi x2, x1, 1
            entry(2, 0x1008, 0x00300193),  # addi x3, x0, 3
        ]
    )

    assert dual_model(intra_bundle_forwarding=True).run(entries) == [5, 5, 6]


def test_shakti_dual_issue_memory_pairs_are_disabled_without_dual_mem():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00003083),  # ld x1, 0(x0)
            entry(1, 0x1004, 0x00003103),  # ld x2, 0(x0)
            entry(2, 0x1008, 0x00300193),  # addi x3, x0, 3
        ]
    )

    assert dual_model().run(entries) == [5, 6, 6]
    assert dual_model(memory_issue_width=2).run(entries) == [5, 6, 6]


def test_shakti_dual_issue_allows_waw_and_war_pairs():
    waw = annotate(
        [
            entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
            entry(1, 0x1004, 0x00200093),  # addi x1, x0, 2
        ]
    )
    war = annotate(
        [
            entry(0, 0x1000, 0x00108113),  # addi x2, x1, 1
            entry(1, 0x1004, 0x00100093),  # addi x1, x0, 1
        ]
    )

    assert dual_model().run(waw) == [5, 5]
    assert dual_model().run(war) == [5, 5]


def test_shakti_dual_issue_pairing_whitelist_is_exact():
    model = dual_model()
    alu = entry(0, 0x1000, 0x00100093).insn
    load = entry(1, 0x1004, 0x00003103).insn
    store = entry(2, 0x1008, 0x00203023).insn
    mul = entry(3, 0x100C, 0x023100B3).insn
    branch = entry(4, 0x1010, 0x00000463).insn  # beq x0, x0, +8
    trap = entry(5, 0x1014, 0x00000073).insn  # ecall

    assert model._can_pair_shakti(alu, load)
    assert model._can_pair_shakti(load, alu)
    assert model._can_pair_shakti(branch, load)
    assert model._can_pair_shakti(load, branch)
    assert not model._can_pair_shakti(load, load)
    assert not model._can_pair_shakti(load, store)
    assert not model._can_pair_shakti(load, mul)
    assert not model._can_pair_shakti(branch, branch)
    assert not model._can_pair_shakti(alu, trap)


def test_symmetric_slots_experiment_enables_non_alu_scarce_fu_pairs():
    model = dual_model()
    symmetric = dual_model(symmetric_slots=True)
    store = entry(2, 0x1008, 0x00203023).insn
    mul = entry(3, 0x100C, 0x023100B3).insn

    assert not model._can_pair_shakti(store, mul)
    assert symmetric._can_pair_shakti(store, mul)


def test_shakti_pair_decision_reports_branch_branch():
    model = dual_model()
    branch = entry(0, 0x1000, 0x00000463).insn  # beq x0, x0, +8
    other_branch = entry(1, 0x1004, 0x00000463).insn

    assert model._shakti_pair_decision(branch, other_branch) == (False, "CONTROL+CONTROL")


def test_branch_branch_experiment_still_needs_two_control_slots():
    one_control = dual_model(allow_branch_branch=True, control_issue_width=1)
    two_control = dual_model(allow_branch_branch=True, control_issue_width=2)
    branch0 = PipeEntry(entry(0, 0x1000, 0x00000463))  # beq x0, x0, +8
    branch1 = PipeEntry(entry(1, 0x1004, 0x00000463))

    assert one_control._can_pair_shakti(branch0.insn, branch1.insn)
    assert not one_control._bundle_fu_ready([branch0, branch1])
    assert two_control._bundle_fu_ready([branch0, branch1])


def test_shakti_dual_mem_only_enables_load_store_memory_pairs():
    model = dual_model(dual_mem=True)
    load = entry(0, 0x1000, 0x00003083).insn
    other_load = entry(1, 0x1004, 0x00003103).insn
    store = entry(2, 0x1008, 0x00203023).insn

    assert not model._can_pair_shakti(load, other_load)
    assert model._can_pair_shakti(load, store)
    assert model._can_pair_shakti(store, load)


def test_shakti_dual_issue_stage4_and_commit_are_atomic_for_pairs():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
            entry(1, 0x1004, 0x00003103),  # ld x2, 0(x0)
        ]
    )

    assert dual_model(load_hit_latency=2).run(entries) == [7, 7]


def test_independent_retire_can_commit_partial_pair_at_stage5():
    atomic = dual_model()
    independent = dual_model(atomic_pair_retire=False)
    for model in (atomic, independent):
        pipe_entry = PipeEntry(entry(0, 0x1000, 0x00100093))
        pipe_entry.bundle_id = 3
        pipe_entry.bundle_pos = 0
        pipe_entry.bundle_size = 2
        model.q_s4s5.push(pipe_entry)

    assert not atomic.try_commit()
    assert atomic.commits == []

    assert independent.try_commit()
    assert independent.commits == [0]


def test_memory_latency_zero_and_one_are_distinct():
    entries = annotate(
        [
            entry(0, 0x1000, 0x00003083),  # ld x1, 0(x0)
            entry(1, 0x1004, 0x00108113),  # addi x2, x1, 1
        ]
    )

    assert dual_model(load_hit_latency=0).run(entries) == [5, 7]
    assert dual_model(load_hit_latency=1).run(entries) == [6, 8]


def test_branch_next_pc_stall_can_be_relaxed_for_in_bundle_successor():
    branch = PipeEntry(entry(0, 0x1000, 0x00209463))  # bne x1, x2, +8
    successor = PipeEntry(entry(1, 0x1004, 0x00100113))  # addi x2, x0, 1
    branch.trace.actual_next_pc = successor.trace.pc

    baseline = dual_model(enable_bpu=True)
    relaxed = dual_model(enable_bpu=True, relax_branch_next_pc_stall=True)
    for model in (baseline, relaxed):
        for pos, pipe_entry in enumerate((branch, successor)):
            cloned = PipeEntry(pipe_entry.trace)
            cloned.bundle_id = 13
            cloned.bundle_pos = pos
            cloned.bundle_size = 2
            model.q_s2s3.push(cloned)

    assert not baseline.try_execute()
    assert len(baseline.q_s2s3) == 2

    assert relaxed.try_execute()
    assert len(relaxed.q_s3s4) == 2


def test_decoupled_lockstep_experiment_can_issue_ready_older_slot():
    locked = dual_model()
    decoupled = dual_model(lockstep_bundles=False)
    for model in (locked, decoupled):
        model.cycle = 2
        model.div_busy_until = 6
        alu = PipeEntry(entry(0, 0x1000, 0x00100093))  # addi x1, x0, 1
        div = PipeEntry(entry(1, 0x1004, 0x023140B3))  # div x1, x2, x3
        for pos, pipe_entry in enumerate((alu, div)):
            pipe_entry.bundle_id = 11
            pipe_entry.bundle_pos = pos
            pipe_entry.bundle_size = 2
            model.q_s2s3.push(pipe_entry)

    assert not locked.try_execute()
    assert locked.q_s2s3.first().trace.index == 0

    assert decoupled.try_execute()
    assert len(decoupled.q_s2s3) == 1
    assert decoupled.q_s2s3.first().trace.index == 1


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


def test_shakti_dual_bypass_sees_second_slot_of_downstream_bundle():
    model = dual_model()
    first = PipeEntry(entry(0, 0x1000, 0x00100093))  # addi x1, x0, 1
    second = PipeEntry(entry(1, 0x1004, 0x00200113))  # addi x2, x0, 2
    for pos, pipe_entry in enumerate((first, second)):
        pipe_entry.bundle_id = 7
        pipe_entry.bundle_pos = pos
        pipe_entry.bundle_size = 2
        pipe_entry.issued_cycle = 9
        pipe_entry.bypassable = True
        pipe_entry.bypass_ready_cycle = 10
    second.scoreboard_id = 5
    model.cycle = 10
    model.scoreboard[("x", 2)] = 5
    model.q_s3s4.push(first)
    model.q_s3s4.push(second)

    consumer = entry(2, 0x1008, 0x00110193).insn  # addi x3, x2, 1
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


def test_fpu_fma_latency_controls_result_ready_cycle():
    model = small_model(fpu_fma_latency=5)
    model.cycle = 7
    fmul = entry(1, 0x1004, 0x12B77753).insn  # fmul.d fa4, fa4, fa1
    itof = entry(2, 0x1008, 0xD2068753).insn  # fcvt.d.w fa4, a3

    assert fmul.fp_op == "fma"
    assert model._result_ready_cycle(fmul) == 12
    assert itof.fp_op == "itof"
    assert model._result_ready_cycle(itof) == 8


def test_fpu_busy_is_structural_for_multicycle_float_ops():
    model = small_model(fpu_fma_latency=5)
    model.cycle = 10
    model.fpu_busy_until = 15
    fmul = entry(1, 0x1004, 0x12B77753).insn
    addi = entry(2, 0x1008, 0x00108113).insn

    assert not model._fu_ready(fmul)
    assert model._fu_ready(addi)
    model.cycle = 15
    assert model._fu_ready(fmul)


def test_fpu_ready_lags_result_by_one_cycle():
    model = small_model(fpu_fma_latency=5, fpu_result_to_ready_delay=1)
    model.cycle = 10
    fmul = entry(1, 0x1004, 0x12B77753).insn
    ready_cycle = model._result_ready_cycle(fmul)

    model._reserve_multicycle_unit(fmul, ready_cycle)

    assert ready_cycle == 15
    assert model.fpu_busy_until == 16


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


def test_wrong_path_frontend_fetches_stale_entries_while_redirect_is_unresolved():
    model = small_model(wrong_path_frontend=True)
    model.fetch_blocked_by = 99
    entries = [entry(0, 0x1000, 0x00100093)]

    assert model.try_fetch(entries)
    assert model.fetch_index == 0
    assert len(model.q_s0s1) == 1
    assert model.q_s0s1.first().stale_frontend


def test_wrong_path_frontend_can_be_disabled():
    model = small_model(wrong_path_frontend=False)
    model.fetch_blocked_by = 99
    entries = [entry(0, 0x1000, 0x00100093)]

    assert not model.try_fetch(entries)
    assert model.fetch_index == 0
    assert model.q_s0s1.empty()


def test_flushed_stale_frontend_entries_drop_before_execute():
    model = small_model(wrong_path_frontend=True)
    model.stale_frontend_flushed = True

    model.q_s0s1.push(model._make_stale_frontend_entry())
    assert model.try_fetch_decode()
    assert model.q_s0s1.empty()
    assert model.q_s1s2.empty()
    assert model.frontend_drop_fetch_hold == 1

    model.q_s1s2.push(model._make_stale_frontend_entry())
    assert model.try_decode()
    assert model.q_s1s2.empty()
    assert model.q_s2s3.empty()


def test_s0s1_stale_drop_can_hold_target_fetch_one_cycle():
    model = small_model(wrong_path_frontend=True)
    model.stale_frontend_flushed = True
    model.q_s0s1.push(model._make_stale_frontend_entry())
    entries = [entry(0, 0x1000, 0x00100093)]

    assert model.try_fetch_decode()
    assert not model.try_fetch(entries)
    assert model.fetch_index == 0

    model.cycle += 1
    assert model.try_fetch(entries)
    assert model.fetch_index == 1


def test_stale_frontend_entries_drop_in_execute_even_when_writeback_queue_is_full():
    model = small_model(isb_s3s4=1, wrong_path_frontend=True)
    model.q_s3s4.push(PipeEntry(entry(0, 0x1000, 0x00100093)))
    model.q_s2s3.push(model._make_stale_frontend_entry())

    assert model.try_execute()
    assert model.q_s2s3.empty()
    assert len(model.q_s3s4) == 1


def test_mispredict_resolution_starts_stale_frontend_flush():
    model = small_model(enable_bpu=True, wrong_path_frontend=True)
    control = entry(1, 0x1000, 0x0080006F)  # jal x0, +8
    control.actual_next_pc = 0x1008
    pipe_entry = PipeEntry(control, pred_mispredict=True, pred_btb_hit=False, pred_history=0)
    model.fetch_blocked_by = control.index
    model.q_s2s3.push(pipe_entry)

    assert model.try_execute()
    assert model.fetch_blocked_by is None
    assert model.stale_frontend_flushed


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


def test_from_repo_disables_ras_when_bpu_ras_define_is_absent(tmp_path):
    (tmp_path / "makefile.inc").write_text(
        "BSC_DEFINES:=bpu gshare rasdepth=8 btbdepth=32 bhtdepth=512 histlen=8 histbits=5 compressed\n",
        encoding="utf-8",
    )

    assert Model.from_repo(tmp_path).predictor.rasdepth == 0


def test_from_repo_uses_rasdepth_when_bpu_ras_define_is_present(tmp_path):
    (tmp_path / "makefile.inc").write_text(
        "BSC_DEFINES:=bpu gshare rasdepth=8 bpu_ras btbdepth=32 bhtdepth=512 histlen=8 histbits=5 compressed\n",
        encoding="utf-8",
    )

    assert Model.from_repo(tmp_path).predictor.rasdepth == 8
