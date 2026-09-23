// Issue-selection logic: adjacent pairing vs a lookahead window.
//
// The question is not how many dependence comparators you need -- it is how
// deep the select logic becomes, because it sits in the issue stage. Each
// candidate must be checked against every OLDER un-issued instruction in the
// window (RAW, WAR, WAW), and then a priority select picks the oldest legal
// one. Adjacent pairing is one such check; a window of N is O(N^2).
`default_nettype none

// One instruction's register footprint.
//   v = valid, rd/rs1/rs2 = architectural regs, wr = writes rd
// Conflict if a younger instruction reads or writes what an older one writes,
// or writes what an older one reads (WAR).
module dep_check (
    input  wire        v_old, wr_old, v_new, wr_new,
    input  wire [4:0]  rd_old, rs1_old, rs2_old,
    input  wire [4:0]  rd_new, rs1_new, rs2_new,
    output wire        conflict
);
    wire nz_old = wr_old && (rd_old != 5'd0);
    wire nz_new = wr_new && (rd_new != 5'd0);
    wire raw = nz_old && ((rd_old == rs1_new) || (rd_old == rs2_new));
    wire waw = nz_old && nz_new && (rd_old == rd_new);
    wire war = nz_new && ((rd_new == rs1_old) || (rd_new == rs2_old));
    assign conflict = v_old && v_new && (raw || waw || war);
endmodule

// Baseline: pair instruction 0 with instruction 1 only.
module select_adjacent (
    input  wire clk,
    input  wire [3:0]  v, wr,
    input  wire [19:0] rd, rs1, rs2,          // 4 x 5 bits
    output reg  [1:0]  sel_second,
    output reg         issue_two
);
    wire c;
    dep_check u (.v_old(v[0]), .wr_old(wr[0]), .v_new(v[1]), .wr_new(wr[1]),
                 .rd_old(rd[4:0]),   .rs1_old(rs1[4:0]),   .rs2_old(rs2[4:0]),
                 .rd_new(rd[9:5]),   .rs1_new(rs1[9:5]),   .rs2_new(rs2[9:5]),
                 .conflict(c));
    always @(posedge clk) begin
        issue_two  <= v[0] && v[1] && !c;
        sel_second <= 2'd1;
    end
endmodule

// Lookahead window of 4: pick the OLDEST candidate among slots 1..3 that
// conflicts with neither instruction 0 nor any older skipped instruction.
module select_window4 (
    input  wire clk,
    input  wire [3:0]  v, wr,
    input  wire [19:0] rd, rs1, rs2,
    output reg  [1:0]  sel_second,
    output reg         issue_two
);
    // conflict[i][j] : candidate j against older instruction i
    wire c[0:3][0:3];
    genvar i, j;
    generate
      for (i = 0; i < 4; i = i + 1) begin : older
        for (j = 0; j < 4; j = j + 1) begin : younger
          if (j > i) begin
            dep_check u (.v_old(v[i]), .wr_old(wr[i]), .v_new(v[j]), .wr_new(wr[j]),
              .rd_old(rd[i*5+:5]), .rs1_old(rs1[i*5+:5]), .rs2_old(rs2[i*5+:5]),
              .rd_new(rd[j*5+:5]), .rs1_new(rs1[j*5+:5]), .rs2_new(rs2[j*5+:5]),
              .conflict(c[i][j]));
          end else begin
            assign c[i][j] = 1'b0;
          end
        end
      end
    endgenerate
    // A candidate is legal only if it clears instruction 0 AND every skipped
    // instruction between it and 0 -- that chain is what makes this O(N^2).
    wire ok1 = v[1] && !c[0][1];
    wire ok2 = v[2] && !c[0][2] && !c[1][2];
    wire ok3 = v[3] && !c[0][3] && !c[1][3] && !c[2][3];
    always @(posedge clk) begin
        issue_two  <= v[0] && (ok1 || ok2 || ok3);
        sel_second <= ok1 ? 2'd1 : ok2 ? 2'd2 : 2'd3;   // oldest-first priority
    end
endmodule
`default_nettype wire
