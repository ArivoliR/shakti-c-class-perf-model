from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trace_cache import load_or_parse_trace_files


def test_trace_cache_reuses_parsed_entries(tmp_path):
    trace_path = tmp_path / "rtl.dump"
    cache_dir = tmp_path / "cache"
    trace_path.write_text(
        "cycle 1 core   0: 3 0x0000000080001000 (0x00000013)\n"
        "cycle 2 core   0: 3 0x0000000080001004 (0x00000013)\n",
        encoding="utf-8",
    )

    first = load_or_parse_trace_files([trace_path], cache_dir=cache_dir)
    second = load_or_parse_trace_files([trace_path], cache_dir=cache_dir)

    assert [entry.pc for entry in first] == [0x80001000, 0x80001004]
    assert [entry.pc for entry in second] == [0x80001000, 0x80001004]
    assert len(list(cache_dir.glob("*.bin"))) == 1


def test_trace_cache_limit_bypasses_cache(tmp_path):
    trace_path = tmp_path / "rtl.dump"
    cache_dir = tmp_path / "cache"
    trace_path.write_text(
        "cycle 1 core   0: 3 0x0000000080001000 (0x00000013)\n"
        "cycle 2 core   0: 3 0x0000000080001004 (0x00000013)\n",
        encoding="utf-8",
    )

    entries = load_or_parse_trace_files([trace_path], cache_dir=cache_dir, limit=1)

    assert len(entries) == 1
    assert not cache_dir.exists()
