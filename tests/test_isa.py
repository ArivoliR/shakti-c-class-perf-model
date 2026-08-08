from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isa import FRF, IRF, decode


def test_decode_integer_alu_sources_and_rd():
    inst = decode(0x00108113, pc=0x1000)  # addi x2, x1, 1
    assert inst.name == "addi"
    assert inst.rd == 2
    assert inst.rs1 == 1
    assert inst.uses_rs1
    assert not inst.uses_rs2
    assert inst.writes_rd
    assert inst.length == 4


def test_decode_load_store_and_muldiv():
    ld = decode(0x00003083, pc=0x1000)  # ld x1, 0(x0)
    assert ld.fu == "MEMORY"
    assert ld.is_load
    assert ld.rd == 1
    assert ld.rs1 == 0

    sd = decode(0x00103023, pc=0x1004)  # sd x1, 0(x0)
    assert sd.is_store
    assert sd.uses_rs1
    assert sd.uses_rs2
    assert sd.rs2 == 1

    mul = decode(0x023100B3, pc=0x1008)  # mul x1, x2, x3
    assert mul.fu == "MULDIV"
    assert mul.is_mul
    assert not mul.is_div
    assert mul.rs1 == 2
    assert mul.rs2 == 3

    div = decode(0x023140B3, pc=0x100C)  # div x1, x2, x3
    assert div.is_div


def test_decode_floating_point_timing_classes():
    fmadd = decode(0x72F6F743, pc=0x1000)  # fmadd.d fa4, fa3, fa5, fa4
    assert fmadd.name == "fmadd"
    assert fmadd.fu == "FLOAT"
    assert fmadd.fp_op == "fma"
    assert fmadd.fp_width == 64
    assert fmadd.rd_type == FRF
    assert fmadd.rs1_type == FRF
    assert fmadd.rs2_type == FRF
    assert fmadd.rs3_type == FRF
    assert fmadd.uses_rs1 and fmadd.uses_rs2 and fmadd.uses_rs3

    fmul = decode(0x12B77753, pc=0x1004)  # fmul.d fa4, fa4, fa1
    assert fmul.name == "fmul"
    assert fmul.fp_op == "fma"
    assert fmul.fp_width == 64

    itof = decode(0xD2068753, pc=0x1008)  # fcvt.d.w fa4, a3
    assert itof.name == "fcvt_f_i"
    assert itof.fp_op == "itof"
    assert itof.rd_type == FRF
    assert itof.rs1_type == IRF
    assert not itof.uses_rs2

    ftoi = decode(0xC20795D3, pc=0x100C)  # fcvt.w.d a1, fa5, rtz
    assert ftoi.name == "fcvt_i_f"
    assert ftoi.fp_op == "ftoi"
    assert ftoi.rd_type == IRF
    assert ftoi.rs1_type == FRF
    assert not ftoi.uses_rs2

    fsqrt = decode(0x5A070753, pc=0x1010)  # fsqrt.d fa4, fa4
    assert fsqrt.name == "fsqrt"
    assert fsqrt.fp_op == "sqrt"
    assert fsqrt.fp_width == 64
    assert not fsqrt.uses_rs2


def test_decode_control_immediates():
    beq = decode(0x00208463, pc=0x2000)  # beq x1, x2, +8
    assert beq.is_branch
    assert beq.imm == 8
    assert beq.rs1 == 1
    assert beq.rs2 == 2

    jal = decode(0x008000EF, pc=0x2000)  # jal x1, +8
    assert jal.is_jal
    assert jal.rd == 1
    assert jal.imm == 8


def test_decode_compressed_common_forms():
    c_li = decode(0x4501, pc=0x3000)  # c.li x10, 0
    assert c_li.is_compressed
    assert c_li.length == 2
    assert c_li.rd == 10
    assert c_li.writes_rd

    c_beqz = decode(0xC001, pc=0x3002)
    assert c_beqz.is_branch
    assert c_beqz.length == 2

    c_bnez = decode(0xF6F5, pc=0x80001A24)
    assert c_bnez.is_branch
    assert c_bnez.pc + c_bnez.imm == 0x80001A10

    c_fsdsp = decode(0xA006, pc=0x3004)
    assert c_fsdsp.fu == "MEMORY"
    assert c_fsdsp.is_store
    assert c_fsdsp.rs2_type == FRF

    c_sdsp = decode(0xF05A, pc=0x3006)
    assert c_sdsp.name == "c.sdsp"
    assert c_sdsp.fu == "MEMORY"
    assert c_sdsp.is_store
    assert c_sdsp.rs2 == 22
    assert c_sdsp.rs2_type == IRF
