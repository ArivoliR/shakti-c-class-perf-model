from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ceiling import dependence_ceiling
from trace import TraceEntry


def entry(index, pc, enc):
    return TraceEntry(index=index, pc=pc, encoding=enc, mode="3")


def test_same_cycle_forwarding_ceiling_equals_issue_width():
    entries = [
        entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
        entry(1, 0x1004, 0x00108113),  # addi x2, x1, 1
        entry(2, 0x1008, 0x00210193),  # addi x3, x2, 2
        entry(3, 0x100C, 0x00318213),  # addi x4, x3, 3
        entry(4, 0x1010, 0x00420293),  # addi x5, x4, 4
        entry(5, 0x1014, 0x00528313),  # addi x6, x5, 5
    ]

    result = dependence_ceiling(
        entries,
        issue_width=3,
        lookahead_window=3,
        same_cycle_forwarding=True,
    )

    assert result["cycles"] == 2
    assert result["ipc"] == 3.0


def test_lookahead_raises_dependence_limited_ceiling():
    entries = [
        entry(0, 0x1000, 0x00100093),  # addi x1, x0, 1
        entry(1, 0x1004, 0x00108113),  # addi x2, x1, 1
        entry(2, 0x1008, 0x00300193),  # addi x3, x0, 3
        entry(3, 0x100C, 0x00400213),  # addi x4, x0, 4
    ]

    adjacent = dependence_ceiling(entries, issue_width=2, lookahead_window=2)
    lookahead = dependence_ceiling(entries, issue_width=2, lookahead_window=3)

    assert adjacent["cycles"] == 3
    assert lookahead["cycles"] == 2
    assert lookahead["ipc"] > adjacent["ipc"]
