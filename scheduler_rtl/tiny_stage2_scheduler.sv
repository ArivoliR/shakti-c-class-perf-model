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

    function automatic logic [REG_BITS-1:0] src0(input int idx);
        src0 = src0_i[idx*REG_BITS +: REG_BITS];
    endfunction

    function automatic logic [REG_BITS-1:0] src1(input int idx);
        src1 = src1_i[idx*REG_BITS +: REG_BITS];
    endfunction

    function automatic logic [REG_BITS-1:0] src2(input int idx);
        src2 = src2_i[idx*REG_BITS +: REG_BITS];
    endfunction

    function automatic logic [REG_BITS-1:0] dst(input int idx);
        dst = dst_i[idx*REG_BITS +: REG_BITS];
    endfunction

    function automatic logic [FU_BITS-1:0] fu(input int idx);
        fu = fu_i[idx*FU_BITS +: FU_BITS];
    endfunction

    function automatic logic reads_dst(input int reader, input int writer);
        reads_dst = dst_valid_i[writer] &&
            ((src0_valid_i[reader] && (src0(reader) == dst(writer))) ||
             (src1_valid_i[reader] && (src1(reader) == dst(writer))) ||
             (src2_valid_i[reader] && (src2(reader) == dst(writer))));
    endfunction

    function automatic logic writes_source(input int writer, input int reader);
        writes_source = dst_valid_i[writer] &&
            ((src0_valid_i[reader] && (dst(writer) == src0(reader))) ||
             (src1_valid_i[reader] && (dst(writer) == src1(reader))) ||
             (src2_valid_i[reader] && (dst(writer) == src2(reader))));
    endfunction

    function automatic logic writes_same_dst(input int a, input int b);
        writes_same_dst = dst_valid_i[a] && dst_valid_i[b] && (dst(a) == dst(b));
    endfunction

    function automatic logic has_dependency(input int older, input int younger);
        has_dependency =
            reads_dst(younger, older) ||       // RAW
            writes_source(younger, older) ||   // WAR without rename
            writes_same_dst(older, younger);   // WAW without rename
    endfunction

    function automatic logic is_barrier(input int idx);
        is_barrier =
            is_control_i[idx] ||
            is_system_i[idx] ||
            is_store_i[idx] ||
            (fu(idx) == FU_SYSTEM) ||
            (fu(idx) == FU_TRAP);
    endfunction

    function automatic logic fu_available(input int idx, input logic mem_used,
                                          input logic mul_used, input logic fp_used,
                                          input logic ctrl_used);
        unique case (fu(idx))
            FU_ALU:    fu_available = fu_ready_i[0];
            FU_MEMORY: fu_available = fu_ready_i[1] && !mem_used;
            FU_MULDIV: fu_available = fu_ready_i[2] && !mul_used;
            FU_FLOAT:  fu_available = fu_ready_i[3] && !fp_used;
            FU_CTRL:   fu_available = fu_ready_i[4] && !ctrl_used;
            default:   fu_available = 1'b0;
        endcase
    endfunction

    function automatic logic can_skip_older(input int cand, input int older);
        if (!valid_i[older] || issued_i[older]) begin
            can_skip_older = 1'b1;
        end else if (is_barrier(older)) begin
            can_skip_older = 1'b0;
        end else if (is_memory_i[cand] && is_memory_i[older]) begin
            can_skip_older = 1'b0;
        end else begin
            can_skip_older = !has_dependency(older, cand);
        end
    endfunction

    function automatic logic can_select(
        input int cand,
        input logic [WINDOW-1:0] selected,
        input logic mem_used,
        input logic mul_used,
        input logic fp_used,
        input logic ctrl_used
    );
        logic ok;
        ok = valid_i[cand] && !issued_i[cand] && operand_ready_i[cand] &&
             downstream_ready_i && !selected[cand] &&
             fu_available(cand, mem_used, mul_used, fp_used, ctrl_used);

        for (int older = 0; older < WINDOW; older++) begin
            if (older < cand && !can_skip_older(cand, older)) begin
                ok = 1'b0;
            end
        end

        for (int other = 0; other < WINDOW; other++) begin
            if (selected[other] && has_dependency(other, cand)) begin
                ok = 1'b0;
            end
            if (selected[other] && has_dependency(cand, other)) begin
                ok = 1'b0;
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

        selected = '0;
        mem_used = 1'b0;
        mul_used = 1'b0;
        fp_used = 1'b0;
        ctrl_used = 1'b0;
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
                        mem_used = mem_used || (fu(cand) == FU_MEMORY);
                        mul_used = mul_used || (fu(cand) == FU_MULDIV);
                        fp_used = fp_used || (fu(cand) == FU_FLOAT);
                        ctrl_used = ctrl_used || (fu(cand) == FU_CTRL);
                    end
                end
            end
        end
    end
endmodule

`default_nettype wire
