"""Dependence-limited issue ceiling analysis.

This is intentionally independent of the cycle model. It answers a narrower
question: if functional units, queues, branch prediction and memory latency were
perfect, how much IPC remains available under true RAW dependences alone?
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from trace import BenchmarkWindow, TraceEntry, detect_benchmark_window, parse_app_log_metrics
from trace_cache import load_or_parse_trace_files


DEFAULT_WIDTHS = (2, 3, 4)
DEFAULT_WINDOWS = (2, 3, 4, 6, 8)


def dependence_ceiling(
    entries: list[TraceEntry],
    *,
    issue_width: int,
    lookahead_window: int,
    same_cycle_forwarding: bool = False,
) -> dict[str, Any]:
    if issue_width <= 0:
        raise ValueError("issue_width must be positive")
    if lookahead_window < issue_width:
        raise ValueError("lookahead_window must be >= issue_width")

    n = len(entries)
    if n == 0:
        return {
            "instructions": 0,
            "cycles": 0,
            "ipc": 0.0,
            "candidate_checks": 0,
            "candidate_checks_per_cycle": 0.0,
        }
    if same_cycle_forwarding:
        cycles = (n + issue_width - 1) // issue_width
        return {
            "instructions": n,
            "cycles": cycles,
            "ipc": n / cycles,
            "candidate_checks": n,
            "candidate_checks_per_cycle": issue_width,
        }

    sources = [tuple(entry.insn.source_regs()) for entry in entries]
    dests = [
        (entry.insn.rd_type, entry.insn.rd) if entry.insn.writes_scoreboard else None
        for entry in entries
    ]

    if lookahead_window == issue_width:
        cycles, checks = _schedule_contiguous_prefix(sources, dests, issue_width)
    else:
        cycles, checks = _schedule_lookahead(sources, dests, issue_width, lookahead_window)
    return {
        "instructions": n,
        "cycles": cycles,
        "ipc": n / cycles,
        "candidate_checks": checks,
        "candidate_checks_per_cycle": checks / cycles if cycles else 0.0,
    }


def ceiling_grid(
    entries: list[TraceEntry],
    *,
    widths: Iterable[int] = DEFAULT_WIDTHS,
    windows: Iterable[int] = DEFAULT_WINDOWS,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for width in widths:
        for window in windows:
            if window < width:
                continue
            for forwarding in (False, True):
                result = dependence_ceiling(
                    entries,
                    issue_width=width,
                    lookahead_window=window,
                    same_cycle_forwarding=forwarding,
                )
                result.update(
                    {
                        "issue_width": width,
                        "lookahead_window": window,
                        "same_cycle_forwarding": forwarding,
                    }
                )
                rows.append(result)
    return rows


def _schedule_contiguous_prefix(
    sources: list[tuple[tuple[str, int], ...]],
    dests: list[tuple[str, int] | None],
    width: int,
) -> tuple[int, int]:
    ready: dict[tuple[str, int], int] = {}
    cycle = 0
    index = 0
    checks = 0
    n = len(sources)
    while index < n:
        issued = 0
        produced: set[tuple[str, int]] = set()
        for offset in range(width):
            if index + offset >= n:
                break
            checks += 1
            srcs = sources[index + offset]
            if any(ready.get(src, 0) > cycle for src in srcs):
                break
            if any(src in produced for src in srcs):
                break
            issued += 1
            dest = dests[index + offset]
            if dest is not None:
                produced.add(dest)
        if issued == 0:
            cycle += 1
            continue
        for offset in range(issued):
            dest = dests[index + offset]
            if dest is not None:
                ready[dest] = cycle + 1
        index += issued
        cycle += 1
    return cycle, checks


def _schedule_lookahead(
    sources: list[tuple[tuple[str, int], ...]],
    dests: list[tuple[str, int] | None],
    width: int,
    window: int,
) -> tuple[int, int]:
    n = len(sources)
    next_idx = list(range(1, n)) + [-1]
    prev_idx = [-1] + list(range(n - 1))
    head = 0
    remaining = n
    ready: dict[tuple[str, int], int] = {}
    cycle = 0
    checks = 0

    while remaining:
        nodes: list[int] = []
        cursor = head
        for _ in range(min(window, remaining)):
            if cursor < 0:
                break
            nodes.append(cursor)
            cursor = next_idx[cursor]

        chosen: list[int] = []
        produced: set[tuple[str, int]] = set()
        for slot, index in enumerate(nodes):
            if len(chosen) >= width:
                break
            checks += 1
            srcs = sources[index]
            if any(ready.get(src, 0) > cycle for src in srcs):
                if slot == 0:
                    break
                continue
            if any(src in produced for src in srcs):
                continue
            if not chosen and index != head:
                continue
            chosen.append(index)
            dest = dests[index]
            if dest is not None:
                produced.add(dest)

        if not chosen:
            cycle += 1
            continue
        for index in chosen:
            before = prev_idx[index]
            after = next_idx[index]
            if before >= 0:
                next_idx[before] = after
            else:
                head = after
            if after >= 0:
                prev_idx[after] = before
        for index in chosen:
            dest = dests[index]
            if dest is not None:
                ready[dest] = cycle + 1
        remaining -= len(chosen)
        cycle += 1
    return cycle, checks


def load_window(
    trace_files: list[str],
    *,
    trace_cache: str = ".trace-cache",
    app_log: str | None = None,
    no_auto_window: bool = False,
    limit: int | None = None,
) -> tuple[list[TraceEntry], BenchmarkWindow | None]:
    parsed = load_or_parse_trace_files(trace_files, cache_dir=trace_cache, limit=limit)
    if no_auto_window or limit is not None:
        return parsed, None
    log_path = Path(app_log) if app_log is not None else Path(trace_files[0]).with_name("app_log")
    metrics = parse_app_log_metrics(log_path)
    if metrics is None:
        return parsed, None
    window = detect_benchmark_window(parsed, metrics)
    if window is None:
        return parsed, None
    return window.entries(parsed), window


def achieved_ipc(entries: list[TraceEntry], window: BenchmarkWindow | None, achieved_cycles: int | None) -> float | None:
    if achieved_cycles:
        return len(entries) / achieved_cycles
    if window is not None and window.measured_cycles:
        return len(entries) / window.measured_cycles
    stamped = [entry.cycle for entry in entries if entry.cycle is not None]
    if len(stamped) == len(entries) and stamped:
        cycles = stamped[-1] - stamped[0] + 1
        return len(entries) / cycles if cycles else None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_files", nargs="+")
    parser.add_argument("--trace-cache", default=".trace-cache")
    parser.add_argument("--app-log")
    parser.add_argument("--no-auto-window", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--widths", default="2,3,4")
    parser.add_argument("--windows", default="2,3,4,6,8")
    parser.add_argument("--achieved-cycles", type=int)
    parser.add_argument("--benchmark", default="trace")
    parser.add_argument("--json-output")
    args = parser.parse_args()

    entries, window = load_window(
        args.trace_files,
        trace_cache=args.trace_cache,
        app_log=args.app_log,
        no_auto_window=args.no_auto_window,
        limit=args.limit,
    )
    widths = _parse_csv_ints(args.widths)
    windows = _parse_csv_ints(args.windows)
    rows = ceiling_grid(entries, widths=widths, windows=windows)
    achieved = achieved_ipc(entries, window, args.achieved_cycles)
    for row in rows:
        row["achieved_ipc"] = achieved
        row["achieved_fraction_of_ceiling"] = (achieved / row["ipc"]) if achieved else None

    output = {
        "benchmark": args.benchmark,
        "trace_files": [str(Path(path).resolve()) for path in args.trace_files],
        "instructions": len(entries),
        "window": _window_dict(window),
        "achieved_ipc": achieved,
        "rows": rows,
    }
    if args.json_output:
        Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_output).write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(_render_text(output))
    return 0


def _parse_csv_ints(text: str) -> list[int]:
    return [int(part.strip()) for part in text.split(",") if part.strip()]


def _window_dict(window: BenchmarkWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "start_index": window.start_index,
        "end_index": window.end_index,
        "measured_cycles": window.measured_cycles,
        "measured_instructions": window.measured_instructions,
        "runs": window.runs,
    }


def _render_text(output: dict[str, Any]) -> str:
    lines = [
        f"benchmark: {output['benchmark']}",
        f"instructions: {output['instructions']:,}",
    ]
    if output["achieved_ipc"] is not None:
        lines.append(f"achieved_ipc: {output['achieved_ipc']:.6f}")
    lines.append("width window fwd ceiling_ipc achieved/ceiling checks/cycle")
    for row in output["rows"]:
        frac = row.get("achieved_fraction_of_ceiling")
        frac_text = f"{frac:.3%}" if frac is not None else "n/a"
        lines.append(
            f"{row['issue_width']:>5} {row['lookahead_window']:>6} "
            f"{int(row['same_cycle_forwarding']):>3} {row['ipc']:>11.6f} "
            f"{frac_text:>16} {row['candidate_checks_per_cycle']:>12.3f}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
