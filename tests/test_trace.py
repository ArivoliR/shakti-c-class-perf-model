from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trace import AppLogMetrics, detect_benchmark_window, parse_app_log_metrics, parse_trace_lines


def test_parse_legacy_trace_line():
    entries = parse_trace_lines(
        [
            "core   0: 3 0x000000000000100c (0x0182b283) x5 0x0000000080000000 mem 0x0000000000001018\n"
        ]
    )
    assert len(entries) == 1
    assert entries[0].cycle is None
    assert entries[0].pc == 0x100C
    assert entries[0].encoding == 0x0182B283
    assert entries[0].reg_writes[0].reg == 5
    assert entries[0].mem_addresses == [0x1018]
    assert entries[0].insn.is_load


def test_parse_cycle_stamped_trace_line():
    entries = parse_trace_lines(
        [
            "cycle 42 core   0: 3 0x0000000080000002 (0x3002a073) c768_mstatus 0x8000000a00006000\n",
            "cycle 46 core   0: 3 0x0000000080000006 (0x4285) x5 0x0000000000000001\n",
        ]
    )
    assert entries[0].cycle == 42
    assert entries[1].cycle == 46
    assert entries[0].csr_writes[0].number == 768
    assert entries[0].actual_next_pc == entries[1].pc


def test_parse_app_log_metrics(tmp_path):
    app_log = tmp_path / "app_log"
    app_log.write_text("IPC_MEASURE cycles: 167827 instret: 159518 runs: 500\n", encoding="utf-8")

    metrics = parse_app_log_metrics(app_log)

    assert metrics == AppLogMetrics(cycles=167827, instret=159518, runs=500)


def test_detect_benchmark_window_from_csr_reads():
    entries = parse_trace_lines(
        [
            "cycle 10 core   0: 3 0x0000000080001000 (0xb02027f3) x15 0x0000000000000064\n",
            "cycle 11 core   0: 3 0x0000000080001004 (0xb00027f3) x15 0x00000000000000c8\n",
            "cycle 12 core   0: 3 0x0000000080001008 (0x00000013)\n",
            "cycle 13 core   0: 3 0x000000008000100c (0x00000013)\n",
            "cycle 14 core   0: 3 0x0000000080001010 (0x00000013)\n",
            "cycle 30 core   0: 3 0x0000000080001014 (0xb00027f3) x15 0x00000000000000dc\n",
            "cycle 31 core   0: 3 0x0000000080001018 (0xb02027f3) x15 0x000000000000006a\n",
        ]
    )

    window = detect_benchmark_window(entries, AppLogMetrics(cycles=20, instret=6, runs=1))

    assert window is not None
    assert window.start_index == 0
    assert window.end_index == 6
    assert window.start_mcycle_index == 1
    assert window.end_mcycle_index == 5
    assert len(window.entries(entries)) == 6
