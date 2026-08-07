"""Named dual-issue experiment configurations.

Each entry is a set of Model constructor overrides. The baseline matches the
delivered SHAKTI dual-issue branch with dual_mem absent from BSC_DEFINES.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Experiment:
    name: str
    label: str
    verdict_hint: str
    overrides: dict[str, Any]
    hypothesis: str


BASELINE_DUAL: dict[str, Any] = {
    "num_issue": 2,
    "dual_policy": "shakti",
    "fetch_width": 2,
    "fetch_decode_width": 2,
    "decode_width": 1,
    "issue_width": 1,
    "stage4_width": 1,
    "commit_width": 1,
    "memory_issue_width": 1,
    "control_issue_width": 1,
    "isb_s0s1": 4,
    "isb_s1s2": 6,
    "isb_s2s3": 2,
    "isb_s3s4": 16,
    "isb_s4s5": 16,
    "memory_pairing": "none",
    "lockstep_bundles": True,
    "atomic_pair_retire": True,
    "allow_branch_branch": False,
    "symmetric_slots": False,
    "intra_bundle_forwarding": False,
}


def merged(*updates: dict[str, Any]) -> dict[str, Any]:
    cfg = deepcopy(BASELINE_DUAL)
    for update in updates:
        cfg.update(update)
    return cfg


A_SECOND_MEMORY = {
    "memory_issue_width": 2,
    "memory_pairing": "all",
}

B_DECOUPLE_LOCKSTEP = {
    "lockstep_bundles": False,
    "issue_width": 2,
    "stage4_width": 2,
}

D_INDEPENDENT_RETIRE = {
    "atomic_pair_retire": False,
    "commit_width": 2,
}

E_INTRA_ALU_FORWARDING = {
    "intra_bundle_forwarding": True,
}

F_RELAX_BRANCH_NEXT_PC = {
    "relax_branch_next_pc_stall": True,
}

G_SYMMETRIC_SLOTS = {
    "symmetric_slots": True,
}

H_SECOND_BRANCH = {
    "allow_branch_branch": True,
    "control_issue_width": 2,
}


EXTRA_COMBINATIONS: list[Experiment] = [
    Experiment(
        "combo_AE_memory_plus_intra_forwarding",
        "A+E: memory + intra forwarding",
        "probe",
        merged(A_SECOND_MEMORY, E_INTRA_ALU_FORWARDING),
        "Check whether the second memory port composes with intra-bundle ALU forwarding.",
    ),
    Experiment(
        "combo_EH_intra_forwarding_plus_branch",
        "E+H: intra forwarding + branch",
        "probe",
        merged(E_INTRA_ALU_FORWARDING, H_SECOND_BRANCH),
        "Check whether branch+branch pairing is complementary with intra-bundle ALU forwarding.",
    ),
]


PHASE2_EXPERIMENTS: list[Experiment] = [
    Experiment(
        "0_baseline",
        "Baseline",
        "validation",
        merged(),
        "Delivered dual-issue branch: no MEM+MEM, lockstep stage3/stage4, atomic pair retire.",
    ),
    Experiment(
        "A_second_memory_port",
        "A: second memory port",
        "headline",
        merged(A_SECOND_MEMORY),
        "Allow all MEM+MEM pairs and provide two memory issue slots.",
    ),
    Experiment(
        "B_decouple_lockstep",
        "B: decouple lockstep",
        "coupling",
        merged(B_DECOUPLE_LOCKSTEP),
        "Let paired slots leave execute/stage4 independently while keeping atomic pair retire.",
    ),
    Experiment(
        "C_memory_plus_decouple",
        "C: A+B",
        "interaction",
        merged(A_SECOND_MEMORY, B_DECOUPLE_LOCKSTEP),
        "Test whether a second memory path and decoupled readiness are complementary.",
    ),
    Experiment(
        "D_independent_retire",
        "D: independent retire",
        "commit",
        merged(D_INDEPENDENT_RETIRE),
        "Allow stage5 slots to retire independently.",
    ),
    Experiment(
        "E_intra_alu_forwarding",
        "E: intra-bundle ALU forwarding",
        "raw",
        merged(E_INTRA_ALU_FORWARDING),
        "Permit RAW pairs only when a one-cycle ALU producer feeds a following ALU.",
    ),
    Experiment(
        "F_relax_branch_next_pc",
        "F: relax branch next-PC stall",
        "frontend",
        merged(F_RELAX_BRANCH_NEXT_PC),
        "Allow a control bundle to execute without the queue-head next-PC when the actual successor is already in-bundle.",
    ),
    Experiment(
        "G_symmetric_slots",
        "G: symmetric slots",
        "slotting",
        merged(G_SYMMETRIC_SLOTS),
        "Allow different scarce FUs to pair regardless of original slot-0-only placement restrictions.",
    ),
    Experiment(
        "H_second_branch_unit",
        "H: second branch unit",
        "control",
        merged(H_SECOND_BRANCH),
        "Allow CONTROL+CONTROL pairing and provide two control issue slots.",
    ),
]


def build_best_combo(helpful_names: set[str]) -> dict[str, Any]:
    # In the current scalar-internal queue model, decoupled lockstep and
    # intra-bundle forwarding together create a non-draining dependency
    # interaction. Prefer E over B because E is the larger individual gain.
    helpful_names = set(helpful_names)
    if "E_intra_alu_forwarding" in helpful_names and "B_decouple_lockstep" in helpful_names:
        helpful_names.remove("B_decouple_lockstep")

    cfg = merged()
    if "A_second_memory_port" in helpful_names:
        cfg.update(A_SECOND_MEMORY)
    if "B_decouple_lockstep" in helpful_names:
        cfg.update(B_DECOUPLE_LOCKSTEP)
    if "D_independent_retire" in helpful_names:
        cfg.update(D_INDEPENDENT_RETIRE)
    if "E_intra_alu_forwarding" in helpful_names:
        cfg.update(E_INTRA_ALU_FORWARDING)
    if "F_relax_branch_next_pc" in helpful_names:
        cfg.update(F_RELAX_BRANCH_NEXT_PC)
    if "G_symmetric_slots" in helpful_names:
        cfg.update(G_SYMMETRIC_SLOTS)
    if "H_second_branch_unit" in helpful_names:
        cfg.update(H_SECOND_BRANCH)
    return cfg


def quad_from(best_cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = deepcopy(best_cfg)
    cfg.update(
        {
            "num_issue": 4,
            "fetch_width": 4,
            "fetch_decode_width": 4,
            "decode_width": 2,
            "issue_width": max(2, int(cfg.get("issue_width", 1)) * 2),
            "stage4_width": max(2, int(cfg.get("stage4_width", 1)) * 2),
            "commit_width": max(2, int(cfg.get("commit_width", 1))),
            "isb_s0s1": 8,
            "isb_s1s2": 12,
            "isb_s2s3": 8,
            "isb_s3s4": 32,
            "isb_s4s5": 32,
        }
    )
    return cfg


def sensitivity_experiments() -> list[Experiment]:
    experiments: list[Experiment] = []
    for depth in (2, 4, 6, 8, 12):
        experiments.append(
            Experiment(
                f"sens_instr_queue_{depth}",
                f"instr_queue={depth}",
                "sensitivity",
                merged({"isb_s1s2": depth}),
                "Instruction queue depth sensitivity.",
            )
        )
    for penalty in (0, 1, 2, 3):
        experiments.append(
            Experiment(
                f"sens_mispredict_penalty_{penalty}",
                f"mispredict penalty={penalty}",
                "sensitivity",
                merged({"branch_mispredict_penalty": penalty}),
                "Branch mispredict refill penalty sensitivity.",
            )
        )
    for latency in (0, 1, 2, 3):
        experiments.append(
            Experiment(
                f"sens_memory_latency_{latency}",
                f"memory latency={latency}",
                "sensitivity",
                merged({"load_hit_latency": latency, "store_hit_latency": latency}),
                "Fixed cache-hit latency sensitivity.",
            )
        )
    return experiments
