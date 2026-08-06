"""RV64IMAFDC decode metadata for the SHAKTI C-Class performance model.

The decoder intentionally computes only information needed for timing:
register operands, destination, functional-unit class, control-transfer
kind, instruction length, and immediates. It does not execute instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


IRF = "x"
FRF = "f"


def bits(value: int, hi: int, lo: int) -> int:
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)


def bit(value: int, idx: int) -> int:
    return (value >> idx) & 1


def sign_extend(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return (value & (sign - 1)) - (value & sign)


def _rdp(v: int) -> int:
    return 8 + v


@dataclass(slots=True)
class Instruction:
    encoding: int
    pc: int = 0
    name: str = "unknown"
    fu: str = "ALU"
    length: int = 4
    rd: int = 0
    rs1: int = 0
    rs2: int = 0
    rs3: int = 0
    rd_type: str = IRF
    rs1_type: str = IRF
    rs2_type: str = IRF
    rs3_type: str = IRF
    uses_rs1: bool = False
    uses_rs2: bool = False
    uses_rs3: bool = False
    writes_rd: bool = False
    imm: int = 0
    branch_funct3: Optional[int] = None
    csr: Optional[int] = None
    is_branch: bool = False
    is_jal: bool = False
    is_jalr: bool = False
    is_load: bool = False
    is_store: bool = False
    is_atomic: bool = False
    is_fence: bool = False
    is_fence_i: bool = False
    is_csr: bool = False
    is_wfi: bool = False
    is_trap: bool = False
    is_mul: bool = False
    is_div: bool = False
    is_float: bool = False
    is_compressed: bool = False

    @property
    def is_control(self) -> bool:
        return self.is_branch or self.is_jal or self.is_jalr

    @property
    def fallthrough_pc(self) -> int:
        return self.pc + self.length

    @property
    def has_int_rd(self) -> bool:
        return self.writes_rd and self.rd_type == IRF and self.rd != 0

    @property
    def has_fp_rd(self) -> bool:
        return self.writes_rd and self.rd_type == FRF

    @property
    def writes_scoreboard(self) -> bool:
        if not self.writes_rd:
            return False
        if self.rd_type == IRF:
            return self.rd != 0
        return True

    def source_regs(self) -> list[tuple[str, int]]:
        regs: list[tuple[str, int]] = []
        if self.uses_rs1 and not (self.rs1_type == IRF and self.rs1 == 0):
            regs.append((self.rs1_type, self.rs1))
        if self.uses_rs2 and not (self.rs2_type == IRF and self.rs2 == 0):
            regs.append((self.rs2_type, self.rs2))
        if self.uses_rs3 and not (self.rs3_type == IRF and self.rs3 == 0):
            regs.append((self.rs3_type, self.rs3))
        return regs


def decode(encoding: int, pc: int = 0) -> Instruction:
    if encoding & 0b11 != 0b11:
        return _decode_compressed(encoding & 0xFFFF, pc)
    return _decode_32(encoding & 0xFFFFFFFF, pc)


def _base(enc: int, pc: int, name: str, fu: str = "ALU") -> Instruction:
    return Instruction(encoding=enc, pc=pc, name=name, fu=fu, length=4)


def _decode_32(enc: int, pc: int) -> Instruction:
    opcode = bits(enc, 6, 0)
    rd = bits(enc, 11, 7)
    funct3 = bits(enc, 14, 12)
    rs1 = bits(enc, 19, 15)
    rs2 = bits(enc, 24, 20)
    funct7 = bits(enc, 31, 25)

    if opcode == 0x37:
        return _with_rd(_base(enc, pc, "lui"), rd)
    if opcode == 0x17:
        inst = _with_rd(_base(enc, pc, "auipc"), rd)
        inst.imm = sign_extend(enc & 0xFFFFF000, 32)
        return inst
    if opcode == 0x6F:
        imm = (
            (bit(enc, 31) << 20)
            | (bits(enc, 19, 12) << 12)
            | (bit(enc, 20) << 11)
            | (bits(enc, 30, 21) << 1)
        )
        inst = _with_rd(_base(enc, pc, "jal", "JAL"), rd)
        inst.imm = sign_extend(imm, 21)
        inst.is_jal = True
        return inst
    if opcode == 0x67:
        inst = _with_rd(_base(enc, pc, "jalr", "JALR"), rd)
        inst.rs1 = rs1
        inst.uses_rs1 = True
        inst.imm = sign_extend(bits(enc, 31, 20), 12)
        inst.is_jalr = True
        return inst
    if opcode == 0x63:
        imm = (
            (bit(enc, 31) << 12)
            | (bit(enc, 7) << 11)
            | (bits(enc, 30, 25) << 5)
            | (bits(enc, 11, 8) << 1)
        )
        inst = _base(enc, pc, _branch_name(funct3), "BRANCH")
        inst.rs1 = rs1
        inst.rs2 = rs2
        inst.uses_rs1 = True
        inst.uses_rs2 = True
        inst.imm = sign_extend(imm, 13)
        inst.branch_funct3 = funct3
        inst.is_branch = True
        return inst
    if opcode == 0x03:
        inst = _with_rd(_base(enc, pc, _load_name(funct3), "MEMORY"), rd)
        inst.rs1 = rs1
        inst.uses_rs1 = True
        inst.imm = sign_extend(bits(enc, 31, 20), 12)
        inst.is_load = True
        return inst
    if opcode == 0x23:
        imm = (bits(enc, 31, 25) << 5) | rd
        inst = _base(enc, pc, _store_name(funct3), "MEMORY")
        inst.rs1 = rs1
        inst.rs2 = rs2
        inst.uses_rs1 = True
        inst.uses_rs2 = True
        inst.imm = sign_extend(imm, 12)
        inst.is_store = True
        return inst
    if opcode in (0x13, 0x1B):
        name = _opimm_name(funct3, funct7, word=(opcode == 0x1B))
        inst = _with_rd(_base(enc, pc, name), rd)
        inst.rs1 = rs1
        inst.uses_rs1 = True
        inst.imm = sign_extend(bits(enc, 31, 20), 12)
        return inst
    if opcode in (0x33, 0x3B):
        if funct7 == 0x01:
            is_div = funct3 >= 4
            inst = _with_rd(_base(enc, pc, _muldiv_name(funct3, opcode == 0x3B), "MULDIV"), rd)
            inst.is_mul = not is_div
            inst.is_div = is_div
        else:
            inst = _with_rd(_base(enc, pc, _op_name(funct3, funct7, word=(opcode == 0x3B))), rd)
        inst.rs1 = rs1
        inst.rs2 = rs2
        inst.uses_rs1 = True
        inst.uses_rs2 = True
        return inst
    if opcode == 0x0F:
        inst = _base(enc, pc, "fence.i" if funct3 == 1 else "fence", "MEMORY")
        inst.is_fence_i = funct3 == 1
        inst.is_fence = funct3 != 1
        return inst
    if opcode == 0x73:
        return _decode_system(enc, pc, rd, funct3, rs1)
    if opcode == 0x2F:
        inst = _with_rd(_base(enc, pc, "amo", "MEMORY"), rd)
        inst.rs1 = rs1
        inst.rs2 = rs2
        inst.uses_rs1 = True
        inst.uses_rs2 = True
        inst.is_atomic = True
        inst.is_load = True
        inst.is_store = True
        return inst
    if opcode == 0x07:
        inst = _with_rd(_base(enc, pc, "flw/fld", "MEMORY"), rd, FRF)
        inst.rs1 = rs1
        inst.uses_rs1 = True
        inst.imm = sign_extend(bits(enc, 31, 20), 12)
        inst.is_load = True
        return inst
    if opcode == 0x27:
        imm = (bits(enc, 31, 25) << 5) | rd
        inst = _base(enc, pc, "fsw/fsd", "MEMORY")
        inst.rs1 = rs1
        inst.rs2 = rs2
        inst.rs2_type = FRF
        inst.uses_rs1 = True
        inst.uses_rs2 = True
        inst.imm = sign_extend(imm, 12)
        inst.is_store = True
        return inst
    if opcode in (0x43, 0x47, 0x4B, 0x4F):
        inst = _with_rd(_base(enc, pc, "fmadd", "FLOAT"), rd, FRF)
        inst.rs1 = rs1
        inst.rs2 = rs2
        inst.rs3 = bits(enc, 31, 27)
        inst.rs1_type = FRF
        inst.rs2_type = FRF
        inst.rs3_type = FRF
        inst.uses_rs1 = True
        inst.uses_rs2 = True
        inst.uses_rs3 = True
        inst.is_float = True
        return inst
    if opcode == 0x53:
        return _decode_float(enc, pc, rd, rs1, rs2, funct3, funct7)

    inst = _base(enc, pc, f"unknown_{opcode:02x}")
    inst.is_trap = True
    inst.fu = "TRAP"
    return inst


def _decode_system(enc: int, pc: int, rd: int, funct3: int, rs1: int) -> Instruction:
    if funct3 == 0:
        if enc == 0x10500073:
            inst = _base(enc, pc, "wfi", "WFI")
            inst.is_wfi = True
            return inst
        if enc in (0x10200073, 0x30200073, 0x00200073):
            inst = _base(enc, pc, "xret", "SYSTEM")
            inst.is_csr = False
            return inst
        inst = _base(enc, pc, "system_trap", "TRAP")
        inst.is_trap = True
        return inst

    inst = _with_rd(_base(enc, pc, _csr_name(funct3), "SYSTEM"), rd)
    inst.rs1 = rs1
    inst.uses_rs1 = funct3 in (1, 2, 3) and rs1 != 0
    inst.csr = bits(enc, 31, 20)
    inst.is_csr = True
    return inst


def _decode_float(enc: int, pc: int, rd: int, rs1: int, rs2: int, funct3: int, funct7: int) -> Instruction:
    inst = _with_rd(_base(enc, pc, "fpu", "FLOAT"), rd, FRF)
    inst.rs1 = rs1
    inst.rs2 = rs2
    inst.rs1_type = FRF
    inst.rs2_type = FRF
    inst.uses_rs1 = True
    inst.uses_rs2 = True
    inst.is_float = True

    major = funct7 >> 2
    if major in (0b11000, 0b11010):  # fcvt.* to integer
        inst.rd_type = IRF
    elif major in (0b11100,):  # fmv.x/fclass
        if funct3 in (0, 1):
            inst.rd_type = IRF
    elif major in (0b11110,):  # fmv.w.x/fmv.d.x
        inst.rs1_type = IRF
        inst.rs2_type = IRF
        inst.uses_rs2 = False
    return inst


def _decode_compressed(enc: int, pc: int) -> Instruction:
    op = enc & 0b11
    funct3 = bits(enc, 15, 13)

    def ci_imm() -> int:
        return sign_extend((bit(enc, 12) << 5) | bits(enc, 6, 2), 6)

    if op == 0:
        rdp = _rdp(bits(enc, 4, 2))
        rs1p = _rdp(bits(enc, 9, 7))
        rs2p = _rdp(bits(enc, 4, 2))
        if funct3 == 0:
            inst = _with_rd(_cbase(enc, pc, "c.addi4spn"), rdp)
            inst.rs1 = 2
            inst.uses_rs1 = True
            return inst
        if funct3 == 1:
            inst = _with_rd(_cbase(enc, pc, "c.fld", "MEMORY"), rdp, FRF)
            inst.rs1 = rs1p
            inst.uses_rs1 = True
            inst.is_load = True
            return inst
        if funct3 == 3:
            inst = _with_rd(_cbase(enc, pc, "c.ld", "MEMORY"), rdp)
            inst.rs1 = rs1p
            inst.uses_rs1 = True
            inst.is_load = True
            return inst
        if funct3 == 2:
            inst = _with_rd(_cbase(enc, pc, "c.lw", "MEMORY"), rdp)
            inst.rs1 = rs1p
            inst.uses_rs1 = True
            inst.is_load = True
            return inst
        if funct3 == 5:
            inst = _cbase(enc, pc, "c.fsd", "MEMORY")
            inst.rs1 = rs1p
            inst.rs2 = rs2p
            inst.rs2_type = FRF
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            inst.is_store = True
            return inst
        if funct3 == 7:
            inst = _cbase(enc, pc, "c.sd", "MEMORY")
            inst.rs1 = rs1p
            inst.rs2 = rs2p
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            inst.is_store = True
            return inst
        if funct3 == 6:
            inst = _cbase(enc, pc, "c.sw", "MEMORY")
            inst.rs1 = rs1p
            inst.rs2 = rs2p
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            inst.is_store = True
            return inst
        return _illegal_c(enc, pc)

    if op == 1:
        rd = bits(enc, 11, 7)
        if funct3 == 0:
            inst = _with_rd(_cbase(enc, pc, "c.nop" if rd == 0 else "c.addi"), rd)
            inst.rs1 = rd
            inst.uses_rs1 = rd != 0
            inst.imm = ci_imm()
            return inst
        if funct3 == 1:
            inst = _with_rd(_cbase(enc, pc, "c.addiw"), rd)
            inst.rs1 = rd
            inst.uses_rs1 = True
            inst.imm = ci_imm()
            return inst
        if funct3 == 2:
            inst = _with_rd(_cbase(enc, pc, "c.li"), rd)
            inst.imm = ci_imm()
            return inst
        if funct3 == 3:
            inst = _with_rd(_cbase(enc, pc, "c.addi16sp" if rd == 2 else "c.lui"), rd)
            inst.rs1 = rd if rd == 2 else 0
            inst.uses_rs1 = rd == 2
            return inst
        if funct3 == 4:
            subop = bits(enc, 11, 10)
            rd_rs1p = _rdp(bits(enc, 9, 7))
            rs2p = _rdp(bits(enc, 4, 2))
            if subop in (0, 1, 2):
                name = ("c.srli", "c.srai", "c.andi")[subop]
                inst = _with_rd(_cbase(enc, pc, name), rd_rs1p)
                inst.rs1 = rd_rs1p
                inst.uses_rs1 = True
                return inst
            name = ("c.sub", "c.xor", "c.or", "c.and", "c.subw", "c.addw", "c.res", "c.res")[
                (bit(enc, 12) << 2) | bits(enc, 6, 5)
            ]
            inst = _with_rd(_cbase(enc, pc, name), rd_rs1p)
            inst.rs1 = rd_rs1p
            inst.rs2 = rs2p
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            return inst
        if funct3 == 5:
            inst = _cbase(enc, pc, "c.j", "JAL")
            inst.is_jal = True
            inst.imm = _cj_imm(enc)
            return inst
        if funct3 in (6, 7):
            inst = _cbase(enc, pc, "c.beqz" if funct3 == 6 else "c.bnez", "BRANCH")
            inst.rs1 = _rdp(bits(enc, 9, 7))
            inst.rs2 = 0
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            inst.is_branch = True
            inst.branch_funct3 = 0 if funct3 == 6 else 1
            inst.imm = _cb_imm(enc)
            return inst
        return _illegal_c(enc, pc)

    if op == 2:
        rd = bits(enc, 11, 7)
        rs2 = bits(enc, 6, 2)
        if funct3 == 0:
            inst = _with_rd(_cbase(enc, pc, "c.slli"), rd)
            inst.rs1 = rd
            inst.uses_rs1 = rd != 0
            return inst
        if funct3 == 1:
            inst = _with_rd(_cbase(enc, pc, "c.fldsp", "MEMORY"), rd, FRF)
            inst.rs1 = 2
            inst.uses_rs1 = True
            inst.is_load = True
            return inst
        if funct3 == 3:
            inst = _with_rd(_cbase(enc, pc, "c.ldsp", "MEMORY"), rd)
            inst.rs1 = 2
            inst.uses_rs1 = True
            inst.is_load = True
            return inst
        if funct3 == 2:
            inst = _with_rd(_cbase(enc, pc, "c.lwsp", "MEMORY"), rd)
            inst.rs1 = 2
            inst.uses_rs1 = True
            inst.is_load = True
            return inst
        if funct3 == 4:
            if bit(enc, 12) == 0 and rs2 == 0:
                inst = _cbase(enc, pc, "c.jr", "JALR")
                inst.rs1 = rd
                inst.uses_rs1 = True
                inst.is_jalr = True
                return inst
            if bit(enc, 12) == 0:
                inst = _with_rd(_cbase(enc, pc, "c.mv"), rd)
                inst.rs2 = rs2
                inst.uses_rs2 = True
                return inst
            if rs2 == 0 and rd == 0:
                inst = _cbase(enc, pc, "c.ebreak", "TRAP")
                inst.is_trap = True
                return inst
            if rs2 == 0:
                inst = _with_rd(_cbase(enc, pc, "c.jalr", "JALR"), 1)
                inst.rs1 = rd
                inst.uses_rs1 = True
                inst.is_jalr = True
                return inst
            inst = _with_rd(_cbase(enc, pc, "c.add"), rd)
            inst.rs1 = rd
            inst.rs2 = rs2
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            return inst
        if funct3 == 5:
            inst = _cbase(enc, pc, "c.fsdsp", "MEMORY")
            inst.rs1 = 2
            inst.rs2 = rs2
            inst.rs2_type = FRF
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            inst.is_store = True
            return inst
        if funct3 == 7:
            inst = _cbase(enc, pc, "c.sdsp", "MEMORY")
            inst.rs1 = 2
            inst.rs2 = rs2
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            inst.is_store = True
            return inst
        if funct3 == 6:
            inst = _cbase(enc, pc, "c.swsp", "MEMORY")
            inst.rs1 = 2
            inst.rs2 = rs2
            inst.uses_rs1 = True
            inst.uses_rs2 = True
            inst.is_store = True
            return inst
        return _illegal_c(enc, pc)

    return _illegal_c(enc, pc)


def _with_rd(inst: Instruction, rd: int, rd_type: str = IRF) -> Instruction:
    inst.rd = rd
    inst.rd_type = rd_type
    inst.writes_rd = not (rd_type == IRF and rd == 0)
    return inst


def _cbase(enc: int, pc: int, name: str, fu: str = "ALU") -> Instruction:
    inst = Instruction(encoding=enc, pc=pc, name=name, fu=fu, length=2)
    inst.is_compressed = True
    return inst


def _illegal_c(enc: int, pc: int) -> Instruction:
    inst = _cbase(enc, pc, "c.illegal", "TRAP")
    inst.is_trap = True
    return inst


def _cj_imm(enc: int) -> int:
    imm = (
        (bit(enc, 12) << 11)
        | (bit(enc, 11) << 4)
        | (bits(enc, 10, 9) << 8)
        | (bit(enc, 8) << 10)
        | (bit(enc, 7) << 6)
        | (bit(enc, 6) << 7)
        | (bits(enc, 5, 3) << 1)
        | (bit(enc, 2) << 5)
    )
    return sign_extend(imm, 12)


def _cb_imm(enc: int) -> int:
    imm = (
        (bit(enc, 12) << 8)
        | (bits(enc, 11, 10) << 3)
        | (bits(enc, 6, 5) << 6)
        | (bits(enc, 4, 3) << 1)
        | (bit(enc, 2) << 5)
    )
    return sign_extend(imm, 9)


def _branch_name(funct3: int) -> str:
    return {
        0: "beq",
        1: "bne",
        4: "blt",
        5: "bge",
        6: "bltu",
        7: "bgeu",
    }.get(funct3, "branch")


def _load_name(funct3: int) -> str:
    return {0: "lb", 1: "lh", 2: "lw", 3: "ld", 4: "lbu", 5: "lhu", 6: "lwu"}.get(funct3, "load")


def _store_name(funct3: int) -> str:
    return {0: "sb", 1: "sh", 2: "sw", 3: "sd"}.get(funct3, "store")


def _opimm_name(funct3: int, funct7: int, word: bool) -> str:
    prefix = "w" if word else ""
    if funct3 == 0:
        return f"addi{prefix}"
    if funct3 == 1:
        return f"slli{prefix}"
    if funct3 == 5:
        return f"srai{prefix}" if funct7 in (0x20, 0x30) else f"srli{prefix}"
    return {2: "slti", 3: "sltiu", 4: "xori", 6: "ori", 7: "andi"}.get(funct3, "opimm")


def _op_name(funct3: int, funct7: int, word: bool) -> str:
    suffix = "w" if word else ""
    if funct3 == 0:
        return ("sub" if funct7 == 0x20 else "add") + suffix
    if funct3 == 5:
        return ("sra" if funct7 == 0x20 else "srl") + suffix
    return {1: "sll", 2: "slt", 3: "sltu", 4: "xor", 6: "or", 7: "and"}.get(funct3, "op") + suffix


def _muldiv_name(funct3: int, word: bool) -> str:
    suffix = "w" if word else ""
    return {
        0: "mul",
        1: "mulh",
        2: "mulhsu",
        3: "mulhu",
        4: "div",
        5: "divu",
        6: "rem",
        7: "remu",
    }.get(funct3, "muldiv") + suffix


def _csr_name(funct3: int) -> str:
    return {
        1: "csrrw",
        2: "csrrs",
        3: "csrrc",
        5: "csrrwi",
        6: "csrrsi",
        7: "csrrci",
    }.get(funct3, "csr")
