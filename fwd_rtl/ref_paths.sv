// Reference paths for calibration.
//
// Depth in generic gates is not picoseconds, so an isolated number means
// little. These give something to compare against: the thesis reports the
// integer multiplier as the critical path for C-Class-DI-32b at 0.95 ns, and
// branch-resolution-to-PC-update for the -mod designs at 1.00 ns.
`default_nettype none

// One stage of the 2-stage multiplier (MULSTAGES_TOTAL=2 in makefile.inc).
module mul_stage #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] a, b,
    output reg  [2*XLEN-1:0] p
);
    wire [2*XLEN-1:0] full = a * b;
    always @(posedge clk) p <= full;
endmodule

// A single 64-bit ALU, the unit of comparison.
module alu_single #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] a, b,
    input  wire [3:0] op,
    output reg  [XLEN-1:0] y
);
    wire [XLEN-1:0] r;
    alu64 #(XLEN) u (.a(a), .b(b), .op(op), .y(r));
    always @(posedge clk) y <= r;
endmodule
`default_nettype wire
