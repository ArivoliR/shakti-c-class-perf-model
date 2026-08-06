from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from accuracy import summarize_control_discrepancies
from trace import parse_trace_lines


def test_summarize_control_discrepancies_groups_nearby_branch():
    entries = parse_trace_lines(
        [
            "cycle 10 core   0: 3 0x0000000080001000 (0x00000463)\n",  # beq +8
            "cycle 15 core   0: 3 0x0000000080001008 (0x00000013)\n",
        ]
    )

    report = summarize_control_discrepancies(entries, [(1, 5, 4)], limit=4)

    assert "beq branch taken lo 32b: 1" in report
    assert "5->4: 1" in report
