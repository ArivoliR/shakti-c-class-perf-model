// Isolate the combinational path that ALU-to-ALU forwarding would add.
//
// The question this answers is not "how much IPC does forwarding buy" -- the
// model says ~6% -- but "what does it cost in clock period", because at +5.98%
// IPC the break-even frequency loss is 5.64%, which at DI-64b-mod's 1.00 ns
// period is a budget of 56 ps. For scale, merely permitting BRANCH+MEMORY
// pairs cost 150 ps in Mouna's synthesis.
//
// Two variants, identical except for the forwarding path:
//
//   alu_pair_baseline  two independent ALUs, operands from the register file.
//                      This is today's design: a dependent pair cannot issue
//                      together, so slot 1 always reads architectural state.
//
//   alu_pair_forward   slot 1 may take slot 0's freshly computed result. The
//                      added path is  ALU0 -> dependence compare -> mux -> ALU1
//                      all within one cycle, which is two 64-bit ALUs in
//                      series rather than one.
//
// Compare the longest topological path of the two. The ratio is the useful
// number and it is largely PDK-independent; absolute picoseconds need a
// liberty file and would not be comparable to Mouna's commercial flow anyway.

`default_nettype none

module alu64 #(parameter int XLEN = 64) (
    input  wire [XLEN-1:0] a,
    input  wire [XLEN-1:0] b,
    input  wire [3:0]      op,
    output reg  [XLEN-1:0] y
);
    // Representative RV64I ALU. The adder carry chain dominates the delay,
    // which is the whole point -- chaining two of these is what costs.
    always @* begin
        case (op)
            4'd0: y = a + b;
            4'd1: y = a - b;
            4'd2: y = a & b;
            4'd3: y = a | b;
            4'd4: y = a ^ b;
            4'd5: y = a << b[5:0];
            4'd6: y = a >> b[5:0];
            4'd7: y = $signed(a) >>> b[5:0];
            4'd8: y = {{(XLEN-1){1'b0}}, ($signed(a) < $signed(b))};
            4'd9: y = {{(XLEN-1){1'b0}}, (a < b)};
            default: y = a + b;
        endcase
    end
endmodule

// ---------------------------------------------------------------- baseline
module alu_pair_baseline #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] rs1_0, rs2_0, rs1_1, rs2_1,
    input  wire [3:0]      op0, op1,
    output reg  [XLEN-1:0] res0, res1
);
    wire [XLEN-1:0] y0, y1;
    alu64 #(XLEN) u0 (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0));
    alu64 #(XLEN) u1 (.a(rs1_1), .b(rs2_1), .op(op1), .y(y1));
    always @(posedge clk) begin
        res0 <= y0;
        res1 <= y1;
    end
endmodule

// ------------------------------------------------- with ALU->ALU forwarding
module alu_pair_forward #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] rs1_0, rs2_0, rs1_1, rs2_1,
    input  wire [3:0]      op0, op1,
    input  wire [4:0]      rd0, rs1a_1, rs2a_1,
    input  wire            rd0_writes,
    output reg  [XLEN-1:0] res0, res1
);
    wire [XLEN-1:0] y0, y1;
    alu64 #(XLEN) u0 (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0));

    // Dependence check. Cheap on its own, but it gates the mux select, so it
    // sits between the two ALUs on the critical path.
    wire fwd_a = rd0_writes && (rd0 != 5'd0) && (rd0 == rs1a_1);
    wire fwd_b = rd0_writes && (rd0 != 5'd0) && (rd0 == rs2a_1);

    wire [XLEN-1:0] a1 = fwd_a ? y0 : rs1_1;   // <-- y0 is ALU0's live output
    wire [XLEN-1:0] b1 = fwd_b ? y0 : rs2_1;

    alu64 #(XLEN) u1 (.a(a1), .b(b1), .op(op1), .y(y1));
    always @(posedge clk) begin
        res0 <= y0;
        res1 <= y1;
    end
endmodule

`default_nettype wire
