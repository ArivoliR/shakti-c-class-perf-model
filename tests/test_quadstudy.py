from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadstudy import design_points


def test_quad_proposal_has_four_wide_ordered_completion_shape():
    proposal = next(point for point in design_points() if point.name == "quad_banked4_merge_w4")
    params = proposal.overrides

    assert params["num_issue"] == 4
    assert params["fetch_word_bytes"] == 16
    assert params["tiny_scheduler_window"] == 4
    assert params["stage4_width"] == 4
    assert params["lockstep_bundles"] is False
    assert params["atomic_pair_retire"] is False
    assert params["memory_issue_width"] == 2
    assert params["memory_pairing"] == "banked"
    assert params["mem_banks"] == 4
    assert params["bank_merge_same_line"] is True
    assert params["isb_s3s4"] == 8
    assert params["isb_s4s5"] == 8


def test_quad_ladder_changes_resource_gates_explicitly():
    points = {point.name: point for point in design_points()}

    assert points["quad_64b_w4"].overrides["fetch_word_bytes"] == 8
    assert points["quad_128b_w4"].overrides["fetch_word_bytes"] == 16
    assert points["quad_dual_mem_ideal_w4"].overrides["memory_pairing"] == "all"
    assert points["quad_banked4_merge_w4"].overrides["bank_merge_same_line"] is True
    assert points["quad_banked4_w8"].overrides["tiny_scheduler_window"] == 8
    assert points["quad_banked4_shadow_w8"].overrides["tiny_scheduler_fast_window"] == 4
    assert points["quad_four_mem_w4"].overrides["memory_issue_width"] == 4


def test_completion_capacity_sweep_changes_only_post_issue_capacity():
    points = {point.name: point for point in design_points()}
    base = points["quad_banked4_merge_w4"].overrides

    assert base["isb_s3s4"] == 8
    assert base["isb_s4s5"] == 8
    for entries in (12, 16, 32):
        point = points[f"quad_banked4_merge_w4_c{entries}"].overrides
        assert point["isb_s3s4"] == entries
        assert point["isb_s4s5"] == entries
        assert point["tiny_scheduler_window"] == base["tiny_scheduler_window"]
        assert point["memory_issue_width"] == base["memory_issue_width"]
        assert point["bank_merge_same_line"] == base["bank_merge_same_line"]
