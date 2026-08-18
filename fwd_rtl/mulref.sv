`default_nettype none
module mul53 (input wire clk, input wire [52:0] a, b, output reg [105:0] y);
    always @(posedge clk) y <= a * b;
endmodule
module add64ref (input wire clk, input wire [63:0] a, b, output reg [63:0] y);
    always @(posedge clk) y <= a + b;
endmodule
`default_nettype wire
