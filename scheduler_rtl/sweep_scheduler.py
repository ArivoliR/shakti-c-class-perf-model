#!/usr/bin/env python3
"""Lint and synthesize the standalone tiny scheduler for window sizes 2/4/8.

This script is deliberately independent of the C-Class build.  With only
Verilator installed it performs syntax/lint checks.  If Yosys is installed it
also reports generic cell statistics, and `--liberty path.lib` asks ABC for a
technology-mapped timing estimate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
RTL = ROOT / "tiny_stage2_scheduler.sv"
BUILD = ROOT / "build"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", default="2,4,8", help="Comma-separated scheduler window sizes")
    parser.add_argument("--width", type=int, default=2, help="Issue width")
    parser.add_argument("--liberty", help="Optional liberty file for ABC delay estimation")
    parser.add_argument("--json-output", default=str(ROOT / "scheduler_sweep.json"))
    args = parser.parse_args()

    BUILD.mkdir(parents=True, exist_ok=True)
    windows = [int(part) for part in args.windows.split(",") if part.strip()]
    results = []
    for window in windows:
        top = _write_top(window=window, width=args.width)
        row: dict[str, Any] = {"window": window, "width": args.width, "top": top.name}
        row["verilator_lint"] = _run_verilator(top)
        if shutil.which("yosys"):
            row.update(_run_yosys(top, liberty=args.liberty))
        else:
            row["yosys"] = "not-found"
        results.append(row)

    output = Path(args.json_output)
    output.write_text(json.dumps({"results": results}, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {output}")
    for row in results:
        status = row["verilator_lint"]["status"]
        cells = row.get("cells", "n/a")
        delay = row.get("abc_delay", "n/a")
        print(f"window={row['window']} lint={status} cells={cells} abc_delay={delay}")
    return 0 if all(row["verilator_lint"]["status"] == "ok" for row in results) else 1


def _write_top(*, window: int, width: int) -> Path:
    idx_bits = 1 if window <= 2 else (window - 1).bit_length()
    path = BUILD / f"tiny_stage2_scheduler_w{window}.sv"
    body = f"""\
`default_nettype none
module tiny_stage2_scheduler_w{window} (
    input  logic [{window - 1}:0] valid_i,
    input  logic [{window - 1}:0] issued_i,
    input  logic [{window - 1}:0] operand_ready_i,
    input  logic [{window - 1}:0] src0_valid_i,
    input  logic [{window - 1}:0] src1_valid_i,
    input  logic [{window - 1}:0] src2_valid_i,
    input  logic [{window * 6 - 1}:0] src0_i,
    input  logic [{window * 6 - 1}:0] src1_i,
    input  logic [{window * 6 - 1}:0] src2_i,
    input  logic [{window - 1}:0] dst_valid_i,
    input  logic [{window * 6 - 1}:0] dst_i,
    input  logic [{window * 3 - 1}:0] fu_i,
    input  logic [{window - 1}:0] is_memory_i,
    input  logic [{window - 1}:0] is_store_i,
    input  logic [{window - 1}:0] is_control_i,
    input  logic [{window - 1}:0] is_system_i,
    input  logic [4:0] fu_ready_i,
    input  logic downstream_ready_i,
    output logic [{width - 1}:0] sel_valid_o,
    output logic [{width * idx_bits - 1}:0] sel_idx_o,
    output logic [{window - 1}:0] issue_mask_o,
    output logic [31:0] candidate_checks_o
);
    tiny_stage2_scheduler #(
        .WINDOW({window}),
        .WIDTH({width}),
        .REG_BITS(6),
        .FU_BITS(3),
        .IDX_BITS({idx_bits})
    ) dut (
        .valid_i(valid_i),
        .issued_i(issued_i),
        .operand_ready_i(operand_ready_i),
        .src0_valid_i(src0_valid_i),
        .src1_valid_i(src1_valid_i),
        .src2_valid_i(src2_valid_i),
        .src0_i(src0_i),
        .src1_i(src1_i),
        .src2_i(src2_i),
        .dst_valid_i(dst_valid_i),
        .dst_i(dst_i),
        .fu_i(fu_i),
        .is_memory_i(is_memory_i),
        .is_store_i(is_store_i),
        .is_control_i(is_control_i),
        .is_system_i(is_system_i),
        .fu_ready_i(fu_ready_i),
        .downstream_ready_i(downstream_ready_i),
        .sel_valid_o(sel_valid_o),
        .sel_idx_o(sel_idx_o),
        .issue_mask_o(issue_mask_o),
        .candidate_checks_o(candidate_checks_o)
    );
endmodule
`default_nettype wire
"""
    path.write_text(body, encoding="utf-8")
    return path


def _run_verilator(top: Path) -> dict[str, Any]:
    command = [
        "verilator",
        "--lint-only",
        "-Wall",
        "-Wno-DECLFILENAME",
        "-Wno-UNUSEDSIGNAL",
        "-Wno-WIDTH",
        "-Wno-UNOPTFLAT",
        str(RTL),
        str(top),
        "--top-module",
        top.stem,
    ]
    return _run(command)


def _run_yosys(top: Path, *, liberty: str | None) -> dict[str, Any]:
    stat_file = BUILD / f"{top.stem}.stat"
    script = [
        f"read_verilog -sv {RTL} {top}",
        f"hierarchy -check -top {top.stem}",
        "proc",
        "opt",
        "techmap",
        "opt",
    ]
    if liberty:
        script.extend([f"abc -liberty {liberty}", f"stat -liberty {liberty}"])
    else:
        script.append("stat")
    command = ["yosys", "-q", "-p", "; ".join(script)]
    run = _run(command)
    row: dict[str, Any] = {"yosys": run}
    output = run["stdout"] + "\n" + run["stderr"]
    stat_file.write_text(output, encoding="utf-8")
    cell_match = re.search(r"Number of cells:\s+(\d+)", output)
    if cell_match:
        row["cells"] = int(cell_match.group(1))
    delay_match = re.search(r"Delay\s*=\s*([0-9.]+)", output)
    if delay_match:
        row["abc_delay"] = float(delay_match.group(1))
    return row


def _run(command: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        return {"status": "not-found", "command": command, "error": str(exc)}
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "command": command,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


if __name__ == "__main__":
    sys.exit(main())
