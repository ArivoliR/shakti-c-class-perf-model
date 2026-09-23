// Standalone timing cone for a coupling-aware tiny SHAKTI stage2 scheduler.
//
// This is intentionally not wired into the C-Class RTL.  It exists so the
// window-2/4/8 issue-selection logic can be synthesized by itself before any
// full Bluespec datapath work is attempted.

`default_nettype none

module tiny_stage2_scheduler #(
    parameter int WINDOW = 4,
    parameter int WIDTH = 2,
    parameter int REG_BITS = 6,
    parameter int FU_BITS = 3,
    parameter int IDX_BITS = (WINDOW <= 2) ? 1 : $clog2(WINDOW)
) (
    input  logic [WINDOW-1:0] valid_i,
    input  logic [WINDOW-1:0] issued_i,
    input  logic [WINDOW-1:0] operand_ready_i,
    input  logic [WINDOW-1:0] src0_valid_i,
    input  logic [WINDOW-1:0] src1_valid_i,
    input  logic [WINDOW-1:0] src2_valid_i,
    input  logic [WINDOW*REG_BITS-1:0] src0_i,
    input  logic [WINDOW*REG_BITS-1:0] src1_i,
    input  logic [WINDOW*REG_BITS-1:0] src2_i,
    input  logic [WINDOW-1:0] dst_valid_i,
    input  logic [WINDOW*REG_BITS-1:0] dst_i,
    input  logic [WINDOW*FU_BITS-1:0] fu_i,
    input  logic [WINDOW-1:0] is_memory_i,
    input  logic [WINDOW-1:0] is_store_i,
    input  logic [WINDOW-1:0] is_control_i,
    input  logic [WINDOW-1:0] is_system_i,
    input  logic [4:0] fu_ready_i,          // ALU, MEM, MULDIV, FLOAT, CTRL
    input  logic downstream_ready_i,
    output logic [WIDTH-1:0] sel_valid_o,
    output logic [WIDTH*IDX_BITS-1:0] sel_idx_o,
    output logic [WINDOW-1:0] issue_mask_o,
    output logic [31:0] candidate_checks_o
);
    localparam logic [FU_BITS-1:0] FU_ALU    = 3'd0;
    localparam logic [FU_BITS-1:0] FU_MEMORY = 3'd1;
    localparam logic [FU_BITS-1:0] FU_MULDIV = 3'd2;
    localparam logic [FU_BITS-1:0] FU_FLOAT  = 3'd3;
    localparam logic [FU_BITS-1:0] FU_CTRL   = 3'd4;
    localparam logic [FU_BITS-1:0] FU_SYSTEM = 3'd5;
    localparam logic [FU_BITS-1:0] FU_TRAP   = 3'd6;

    function automatic logic can_select(
        input int cand,
        input logic [WINDOW-1:0] selected,
        input logic mem_used,
        input logic mul_used,
        input logic fp_used,
        input logic ctrl_used
    );
        logic ok;
        logic fu_ok;
        logic older_barrier;
        logic dependency;
        logic [FU_BITS-1:0] cand_fu;
        logic [FU_BITS-1:0] older_fu;
        logic [REG_BITS-1:0] cand_src0;
        logic [REG_BITS-1:0] cand_src1;
        logic [REG_BITS-1:0] cand_src2;
        logic [REG_BITS-1:0] cand_dst;
        logic [REG_BITS-1:0] other_src0;
        logic [REG_BITS-1:0] other_src1;
        logic [REG_BITS-1:0] other_src2;
        logic [REG_BITS-1:0] other_dst;

        cand_fu = fu_i >> (cand * FU_BITS);
        cand_src0 = src0_i >> (cand * REG_BITS);
        cand_src1 = src1_i >> (cand * REG_BITS);
        cand_src2 = src2_i >> (cand * REG_BITS);
        cand_dst = dst_i >> (cand * REG_BITS);
        unique case (cand_fu)
            FU_ALU:    fu_ok = fu_ready_i[0];
            FU_MEMORY: fu_ok = fu_ready_i[1] && !mem_used;
            FU_MULDIV: fu_ok = fu_ready_i[2] && !mul_used;
            FU_FLOAT:  fu_ok = fu_ready_i[3] && !fp_used;
            FU_CTRL:   fu_ok = fu_ready_i[4] && !ctrl_used;
            default:   fu_ok = 1'b0;
        endcase
        ok = valid_i[cand] && !issued_i[cand] && operand_ready_i[cand] &&
             downstream_ready_i && !selected[cand] && fu_ok;

        for (int older = 0; older < WINDOW; older++) begin
            if (older < cand && valid_i[older] && !issued_i[older]) begin
                older_fu = fu_i >> (older * FU_BITS);
                other_src0 = src0_i >> (older * REG_BITS);
                other_src1 = src1_i >> (older * REG_BITS);
                other_src2 = src2_i >> (older * REG_BITS);
                other_dst = dst_i >> (older * REG_BITS);
                older_barrier = is_control_i[older] || is_system_i[older] ||
                                is_store_i[older] || (older_fu == FU_SYSTEM) ||
                                (older_fu == FU_TRAP);
                dependency =
                    (dst_valid_i[older] &&
                     ((src0_valid_i[cand] && cand_src0 == other_dst) ||
                      (src1_valid_i[cand] && cand_src1 == other_dst) ||
                      (src2_valid_i[cand] && cand_src2 == other_dst))) ||
                    (dst_valid_i[cand] &&
                     ((src0_valid_i[older] && other_src0 == cand_dst) ||
                      (src1_valid_i[older] && other_src1 == cand_dst) ||
                      (src2_valid_i[older] && other_src2 == cand_dst))) ||
                    (dst_valid_i[older] && dst_valid_i[cand] && other_dst == cand_dst);
                if (older_barrier ||
                    (is_memory_i[cand] && is_memory_i[older]) || dependency) begin
                    ok = 1'b0;
                end
            end
        end

        for (int other = 0; other < WINDOW; other++) begin
            if (selected[other]) begin
                other_src0 = src0_i >> (other * REG_BITS);
                other_src1 = src1_i >> (other * REG_BITS);
                other_src2 = src2_i >> (other * REG_BITS);
                other_dst = dst_i >> (other * REG_BITS);
                dependency =
                    (dst_valid_i[other] &&
                     ((src0_valid_i[cand] && cand_src0 == other_dst) ||
                      (src1_valid_i[cand] && cand_src1 == other_dst) ||
                      (src2_valid_i[cand] && cand_src2 == other_dst))) ||
                    (dst_valid_i[cand] &&
                     ((src0_valid_i[other] && other_src0 == cand_dst) ||
                      (src1_valid_i[other] && other_src1 == cand_dst) ||
                      (src2_valid_i[other] && other_src2 == cand_dst))) ||
                    (dst_valid_i[other] && dst_valid_i[cand] && other_dst == cand_dst);
                if (dependency) begin
                    ok = 1'b0;
                end
            end
        end
        can_select = ok;
    endfunction

    always_comb begin
        logic [WINDOW-1:0] selected;
        logic mem_used;
        logic mul_used;
        logic fp_used;
        logic ctrl_used;
        logic [FU_BITS-1:0] selected_fu;

        selected = '0;
        mem_used = 1'b0;
        mul_used = 1'b0;
        fp_used = 1'b0;
        ctrl_used = 1'b0;
        selected_fu = '0;
        sel_valid_o = '0;
        sel_idx_o = '0;
        issue_mask_o = '0;
        candidate_checks_o = 32'd0;

        for (int slot = 0; slot < WIDTH; slot++) begin
            logic found;
            found = 1'b0;
            for (int cand = 0; cand < WINDOW; cand++) begin
                if (!found) begin
                    candidate_checks_o = candidate_checks_o + 32'd1;
                    if (can_select(cand, selected, mem_used, mul_used, fp_used, ctrl_used)) begin
                        selected[cand] = 1'b1;
                        issue_mask_o[cand] = 1'b1;
                        sel_valid_o[slot] = 1'b1;
                        sel_idx_o[slot*IDX_BITS +: IDX_BITS] = cand[IDX_BITS-1:0];
                        found = 1'b1;
                        selected_fu = fu_i >> (cand * FU_BITS);
                        mem_used = mem_used || (selected_fu == FU_MEMORY);
                        mul_used = mul_used || (selected_fu == FU_MULDIV);
                        fp_used = fp_used || (selected_fu == FU_FLOAT);
                        ctrl_used = ctrl_used || (selected_fu == FU_CTRL);
                    end
                end
            end
        end
    end
endmodule

`default_nettype wire
