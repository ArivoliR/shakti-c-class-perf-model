// Restricted ALU-to-ALU forwarding variants.
//
// Full forwarding measured 1.84x the baseline depth (236 vs 128), against a
// budget of 1.056x. The question is whether a restricted form keeps most of
// the IPC for far less delay.
//
// The depth of a 64-bit ALU is dominated by the ADDER carry chain. Logic ops
// (and/or/xor) are ~1 gate deep. So the lever is not "which ops are allowed"
// in general -- it is specifically whether an ADDER sits on both ends of the
// forward path.
`default_nettype none

// Shallow ALU: logic operations only. No carry chain.
module alu_logic #(parameter int XLEN = 64) (
    input  wire [XLEN-1:0] a, b,
    input  wire [3:0] op,
    output reg  [XLEN-1:0] y
);
    always @* case (op)
        4'd2: y = a & b;
        4'd3: y = a | b;
        4'd4: y = a ^ b;
        default: y = a & b;
    endcase
endmodule

// V2: forward only when the CONSUMER is a logic op.
//     path = full ALU0 -> mux -> shallow ALU1
module fwd_logic_consumer #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] rs1_0, rs2_0, rs1_1, rs2_1,
    input  wire [3:0] op0, op1,
    input  wire [4:0] rd0, rs1a_1, rs2a_1,
    input  wire rd0_writes,
    output reg  [XLEN-1:0] res0, res1
);
    wire [XLEN-1:0] y0, y1_logic, y1_full;
    alu64  #(XLEN) u0 (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0));
    wire fa = rd0_writes && (rd0 != 5'd0) && (rd0 == rs1a_1);
    wire fb = rd0_writes && (rd0 != 5'd0) && (rd0 == rs2a_1);
    wire [XLEN-1:0] a1 = fa ? y0 : rs1_1;
    wire [XLEN-1:0] b1 = fb ? y0 : rs2_1;
    alu_logic #(XLEN) u1l (.a(a1), .b(b1), .op(op1), .y(y1_logic));
    alu64     #(XLEN) u1f (.a(rs1_1), .b(rs2_1), .op(op1), .y(y1_full));
    wire is_logic = (op1 == 4'd2) || (op1 == 4'd3) || (op1 == 4'd4);
    always @(posedge clk) begin
        res0 <= y0;
        res1 <= is_logic ? y1_logic : y1_full;
    end
endmodule

// V3: forward only when the PRODUCER is a logic op.
//     path = shallow ALU0 -> mux -> full ALU1
module fwd_logic_producer #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] rs1_0, rs2_0, rs1_1, rs2_1,
    input  wire [3:0] op0, op1,
    input  wire [4:0] rd0, rs1a_1, rs2a_1,
    input  wire rd0_writes,
    output reg  [XLEN-1:0] res0, res1
);
    wire [XLEN-1:0] y0_full, y0_logic, y1;
    alu64     #(XLEN) u0f (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0_full));
    alu_logic #(XLEN) u0l (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0_logic));
    wire p_logic = (op0 == 4'd2) || (op0 == 4'd3) || (op0 == 4'd4);
    wire fa = rd0_writes && p_logic && (rd0 != 5'd0) && (rd0 == rs1a_1);
    wire fb = rd0_writes && p_logic && (rd0 != 5'd0) && (rd0 == rs2a_1);
    wire [XLEN-1:0] a1 = fa ? y0_logic : rs1_1;   // only the SHALLOW result forwards
    wire [XLEN-1:0] b1 = fb ? y0_logic : rs2_1;
    alu64 #(XLEN) u1 (.a(a1), .b(b1), .op(op1), .y(y1));
    always @(posedge clk) begin
        res0 <= y0_full;
        res1 <= y1;
    end
endmodule

// V4: forward to ONE operand port only (rs1), full ops both ends.
module fwd_one_port #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] rs1_0, rs2_0, rs1_1, rs2_1,
    input  wire [3:0] op0, op1,
    input  wire [4:0] rd0, rs1a_1,
    input  wire rd0_writes,
    output reg  [XLEN-1:0] res0, res1
);
    wire [XLEN-1:0] y0, y1;
    alu64 #(XLEN) u0 (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0));
    wire fa = rd0_writes && (rd0 != 5'd0) && (rd0 == rs1a_1);
    wire [XLEN-1:0] a1 = fa ? y0 : rs1_1;
    alu64 #(XLEN) u1 (.a(a1), .b(rs2_1), .op(op1), .y(y1));
    always @(posedge clk) begin
        res0 <= y0;
        res1 <= y1;
    end
endmodule
`default_nettype wire

`default_nettype none
// Shallow ALU: logic + shift. A 64-bit barrel shifter is 6 mux levels; the
// carry chain is ~128. Neither logic nor shift contains an adder.
module alu_shallow #(parameter int XLEN = 64) (
    input  wire [XLEN-1:0] a, b, input wire [3:0] op, output reg [XLEN-1:0] y
);
    always @* case (op)
        4'd2: y = a & b;
        4'd3: y = a | b;
        4'd4: y = a ^ b;
        4'd5: y = a << b[5:0];
        4'd6: y = a >> b[5:0];
        4'd7: y = $signed(a) >>> b[5:0];
        default: y = a & b;
    endcase
endmodule

// V5: forward unless BOTH ends are adder ops.
//
// Two SEPARATE forward buses, deliberately not merged. Merging them creates a
// structural path y0_full -> shared mux -> deep ALU1 that is logically
// unreachable but that static timing still counts, so the tool reports 1.86x
// for a design that never sensitizes it. Keeping the buses apart makes the
// restriction structural rather than a promise about operand values.
//
//   shallow producer result -> either consumer  (6 + 128 = ~134)
//   deep    producer result -> shallow consumer (128 + 6 = ~134)
//   deep -> deep                                 not forwarded
module fwd_no_double_add #(parameter int XLEN = 64) (
    input  wire clk,
    input  wire [XLEN-1:0] rs1_0, rs2_0, rs1_1, rs2_1,
    input  wire [3:0] op0, op1,
    input  wire [4:0] rd0, rs1a_1, rs2a_1,
    input  wire rd0_writes,
    output reg  [XLEN-1:0] res0, res1
);
    wire p_shallow = (op0 >= 4'd2) && (op0 <= 4'd7);
    wire c_shallow = (op1 >= 4'd2) && (op1 <= 4'd7);
    wire [XLEN-1:0] y0_full, y0_sh;
    alu64      #(XLEN) u0f (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0_full));
    alu_shallow#(XLEN) u0s (.a(rs1_0), .b(rs2_0), .op(op0), .y(y0_sh));

    wire base = rd0_writes && (rd0 != 5'd0);
    wire ma = base && (rd0 == rs1a_1);
    wire mb = base && (rd0 == rs2a_1);

    // Bus A: shallow producer only. Safe into the deep ALU.
    wire fa_s = ma && p_shallow, fb_s = mb && p_shallow;
    wire [XLEN-1:0] a_deep = fa_s ? y0_sh : rs1_1;
    wire [XLEN-1:0] b_deep = fb_s ? y0_sh : rs2_1;
    wire [XLEN-1:0] y1_full;
    alu64 #(XLEN) u1f (.a(a_deep), .b(b_deep), .op(op1), .y(y1_full));

    // Bus B: any producer. Only ever reaches the shallow ALU.
    wire [XLEN-1:0] fwd_any = p_shallow ? y0_sh : y0_full;
    wire [XLEN-1:0] a_sh = ma ? fwd_any : rs1_1;
    wire [XLEN-1:0] b_sh = mb ? fwd_any : rs2_1;
    wire [XLEN-1:0] y1_sh;
    alu_shallow #(XLEN) u1s (.a(a_sh), .b(b_sh), .op(op1), .y(y1_sh));

    always @(posedge clk) begin
        res0 <= y0_full;
        res1 <= c_shallow ? y1_sh : y1_full;
    end
endmodule
`default_nettype wire
