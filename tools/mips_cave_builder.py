#!/usr/bin/env python3
"""
mips_cave_builder.py — Clean-room MIPS R5900 (PS2 Emotion Engine) mini-assembler,
code-cave compiler, elf_disasm round-trip verifier, and PCSX2 .pnach emitter
for SLUS_213.51 (Need for Speed: Most Wanted - Black Edition, NTSC-U, CRC 0518D274).

SAFETY:
- Opens source ISOs and ELFs strictly read-only ('rb'). Never modifies disc images.
- Verifies every compiled instruction against tools/elf_disasm.py, checks zero
  address overlap against patches/elf_patches.json, and enforces expected_orig.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    from tools import elf_disasm, lint_pnach
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import elf_disasm  # type: ignore
    import lint_pnach  # type: ignore

DEFAULT_ISO_REL = Path("Documents") / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
DEFAULT_TARGET_INFO: Dict[str, Any] = {
    "game": "Need for Speed: Most Wanted - Black Edition (NTSC-U)",
    "serial": "SLUS-21351",
    "elf_name": "SLUS_213.51",
    "elf_size": 4918740,
    "elf_sha1": "60289f3f8b643822a302720f9649ddc4ba55034b",
    "elf_pcsx2_crc": "0518D274",
    "pt_load_vaddr": "0x00100000",
    "pt_load_file_offset": "0x00001000",
}

DEFAULT_PT_VADDR = 0x00100000
DEFAULT_PT_FILESZ = 0x004ABBDC

# Register name -> 5-bit index maps
GPR_MAP: Dict[str, int] = {
    "$zero": 0, "$0": 0, "zero": 0,
    "$at": 1, "$1": 1, "at": 1,
    "$v0": 2, "$2": 2, "v0": 2,
    "$v1": 3, "$3": 3, "v1": 3,
    "$a0": 4, "$4": 4, "a0": 4,
    "$a1": 5, "$5": 5, "a1": 5,
    "$a2": 6, "$6": 6, "a2": 6,
    "$a3": 7, "$7": 7, "a3": 7,
    "$t0": 8, "$8": 8, "t0": 8,
    "$t1": 9, "$9": 9, "t1": 9,
    "$t2": 10, "$10": 10, "t2": 10,
    "$t3": 11, "$11": 11, "t3": 11,
    "$t4": 12, "$12": 12, "t4": 12,
    "$t5": 13, "$13": 13, "t5": 13,
    "$t6": 14, "$14": 14, "t6": 14,
    "$t7": 15, "$15": 15, "t7": 15,
    "$s0": 16, "$16": 16, "s0": 16,
    "$s1": 17, "$17": 17, "s1": 17,
    "$s2": 18, "$18": 18, "s2": 18,
    "$s3": 19, "$19": 19, "s3": 19,
    "$s4": 20, "$20": 20, "s4": 20,
    "$s5": 21, "$21": 21, "s5": 21,
    "$s6": 22, "$22": 22, "s6": 22,
    "$s7": 23, "$23": 23, "s7": 23,
    "$t8": 24, "$24": 24, "t8": 24,
    "$t9": 25, "$25": 25, "t9": 25,
    "$k0": 26, "$26": 26, "k0": 26,
    "$k1": 27, "$27": 27, "k1": 27,
    "$gp": 28, "$28": 28, "gp": 28,
    "$sp": 29, "$29": 29, "sp": 29,
    "$fp": 30, "$s8": 30, "$30": 30, "fp": 30, "s8": 30,
    "$ra": 31, "$31": 31, "ra": 31,
}

FPR_MAP: Dict[str, int] = {}
for _i in range(32):
    FPR_MAP[f"$f{_i}"] = _i
    FPR_MAP[f"f{_i}"] = _i

_LABEL_RE = re.compile(r"^([A-Za-z_.$][A-Za-z0-9_.$]*):(.*)$")
_MEM_OP_RE = re.compile(r"^(.*?)\(\s*([A-Za-z0-9_$]+)\s*\)$")
_HI_LO_RE = re.compile(r"^(hi|lo)\(\s*(.+?)\s*\)$", re.IGNORECASE)


class MipsAssembleError(ValueError):
    """Raised when MIPS R5900 assembly syntax or encoding constraints fail."""


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def find_local_iso(explicit: Optional[Path] = None) -> Optional[Path]:
    """Locate the local retail ISO strictly for read-only verification."""
    if explicit is not None:
        return explicit if explicit.is_file() else None
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso:
        p = Path(env_iso).expanduser()
        return p if p.is_file() else None
    default_iso = Path.home() / DEFAULT_ISO_REL
    return default_iso if default_iso.is_file() else None


def _strip_comments(line: str) -> str:
    """Strip //, #, or ; comments cleanly."""
    best = len(line)
    for tok in ("//", "#", ";"):
        idx = line.find(tok)
        if idx != -1 and idx < best:
            best = idx
    return line[:best].strip()


def _parse_gpr(tok: str) -> int:
    key = tok.strip().lower()
    if key not in GPR_MAP:
        raise MipsAssembleError(f"Invalid GPR register: {tok!r}")
    return GPR_MAP[key]


def _parse_fpr(tok: str) -> int:
    key = tok.strip().lower()
    if key not in FPR_MAP:
        raise MipsAssembleError(f"Invalid FPR register: {tok!r}")
    return FPR_MAP[key]


def _parse_int_expr(tok: str, labels: Optional[Dict[str, int]] = None) -> int:
    s = tok.strip()
    m = _HI_LO_RE.match(s)
    if m:
        kind = m.group(1).lower()
        val = _parse_int_expr(m.group(2), labels=labels)
        if kind == "hi":
            return ((val + 0x8000) >> 16) & 0xFFFF
        lo = val & 0xFFFF
        return lo - 0x10000 if (lo & 0x8000) else lo

    if labels and s in labels:
        return labels[s]
    try:
        return int(s, 0)
    except ValueError as exc:
        raise MipsAssembleError(f"Invalid integer/label expression: {tok!r}") from exc


def _parse_f32_bits(tok: str) -> int:
    s = tok.strip()
    if s.lower().endswith("f") and not s.lower().startswith("0x"):
        s = s[:-1]
    if s.lower().startswith("0x"):
        val = int(s, 16)
        if not (0 <= val <= 0xFFFFFFFF):
            raise MipsAssembleError(f"32-bit float hex out of range: {tok!r}")
        return val
    try:
        fv = float(s)
    except ValueError as exc:
        raise MipsAssembleError(f"Invalid float literal: {tok!r}") from exc
    return struct.unpack("<I", struct.pack("<f", fv))[0]


def _parse_mem_operand(tok: str, labels: Optional[Dict[str, int]] = None) -> Tuple[int, int]:
    s = tok.strip()
    m = _MEM_OP_RE.match(s)
    if not m:
        raise MipsAssembleError(f"Expected offset(base) operand, got {tok!r}")
    off_str = m.group(1).strip()
    base_str = m.group(2).strip()
    offset = 0 if not off_str else _parse_int_expr(off_str, labels=labels)
    base = _parse_gpr(base_str)
    return offset, base


def _encode_s16(val: int, ctx: str) -> int:
    if not (-0x8000 <= val <= 0xFFFF):
        raise MipsAssembleError(f"{ctx}: immediate 0x{val:X} ({val}) out of 16-bit range")
    return val & 0xFFFF


def _encode_u16(val: int, ctx: str) -> int:
    if not (0 <= val <= 0xFFFF):
        raise MipsAssembleError(f"{ctx}: unsigned immediate 0x{val:X} ({val}) out of 16-bit range")
    return val & 0xFFFF


def _encode_sa(val: int, ctx: str) -> int:
    if not (0 <= val <= 31):
        raise MipsAssembleError(f"{ctx}: shift amount {val} out of 0..31 range")
    return val & 0x1F


def _encode_rtype(op: int, rs: int, rt: int, rd: int, sa: int, fn: int) -> int:
    return (
        ((op & 0x3F) << 26)
        | ((rs & 0x1F) << 21)
        | ((rt & 0x1F) << 16)
        | ((rd & 0x1F) << 11)
        | ((sa & 0x1F) << 6)
        | (fn & 0x3F)
    )


def _encode_itype(op: int, rs: int, rt: int, imm16: int) -> int:
    return ((op & 0x3F) << 26) | ((rs & 0x1F) << 21) | ((rt & 0x1F) << 16) | (imm16 & 0xFFFF)


def _encode_jtype(op: int, pc: int, target: int, mnem: str) -> int:
    if target & 3:
        raise MipsAssembleError(f"{mnem}: target 0x{target:08X} is not 4-byte aligned")
    if ((pc + 4) & 0xF0000000) != (target & 0xF0000000):
        raise MipsAssembleError(
            f"{mnem} @ 0x{pc:08X}: target 0x{target:08X} crosses 256 MiB jump region"
        )
    return ((op & 0x3F) << 26) | ((target >> 2) & 0x03FFFFFF)


def _encode_branch_offset(pc: int, target: int, mnem: str) -> int:
    if target & 3:
        raise MipsAssembleError(f"{mnem}: branch target 0x{target:08X} is not 4-byte aligned")
    delta_words = (target - (pc + 4)) >> 2
    if not (-0x8000 <= delta_words <= 0x7FFF):
        raise MipsAssembleError(
            f"{mnem} @ 0x{pc:08X}: branch target 0x{target:08X} out of signed 16-bit word range"
        )
    return delta_words & 0xFFFF


@dataclass(frozen=True)
class AssembledInsn:
    vaddr: int
    word: int
    source: str
    disasm: str


@dataclass
class AssembledProgram:
    base_vaddr: int
    words: List[int] = field(default_factory=list)
    labels: Dict[str, int] = field(default_factory=dict)
    instructions: List[AssembledInsn] = field(default_factory=list)

    @property
    def byte_size(self) -> int:
        return len(self.words) * 4

    def to_writes(self, expected_orig_words: Optional[Sequence[int]] = None) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for idx, insn in enumerate(self.instructions):
            orig = expected_orig_words[idx] if expected_orig_words is not None else 0
            out.append(
                {
                    "vaddr": f"0x{insn.vaddr:08X}",
                    "expected_orig": f"0x{orig:08X}",
                    "patched": f"0x{insn.word:08X}",
                    "note": insn.disasm,
                }
            )
        return out


def _split_mnemonic_args(stmt: str) -> Tuple[str, List[str]]:
    parts = stmt.strip().split(None, 1)
    mnem = parts[0].lower()
    if len(parts) == 1:
        return mnem, []
    args = [a.strip() for a in parts[1].split(",")]
    if any(not a for a in args):
        raise MipsAssembleError(f"Empty operand in statement: {stmt!r}")
    return mnem, args


def _expand_word_count(mnem: str, args: List[str]) -> int:
    if mnem == "li.s":
        if len(args) != 2:
            raise MipsAssembleError("li.s expects 2 operands: li.s $fX, float")
        bits = _parse_f32_bits(args[1])
        return 2 if (bits & 0xFFFF) == 0 else 3
    if mnem == "li":
        if len(args) != 2:
            raise MipsAssembleError("li expects 2 operands: li $reg, imm")
        try:
            val = int(args[1], 0)
        except ValueError:
            return 2  # label / hi-lo resolved as 32-bit if needed; conservative 2 words? Wait: labels rarely used with li
        if -0x8000 <= val <= 0xFFFF:
            return 1
        return 2
    return 1


def _assemble_statement(
    mnem: str,
    args: List[str],
    pc: int,
    labels: Dict[str, int],
) -> List[int]:
    """Assemble one mnemonic (possibly a 2-3 word pseudo-op) into 32-bit R5900 words."""
    # Pseudo-instructions & directives
    if mnem == ".word":
        if len(args) != 1:
            raise MipsAssembleError(".word expects exactly 1 operand")
        return [_parse_int_expr(args[0], labels) & 0xFFFFFFFF]

    if mnem == "nop":
        if args:
            raise MipsAssembleError("nop takes no operands")
        return [0x00000000]

    if mnem == "li.s":
        if len(args) != 2:
            raise MipsAssembleError("li.s expects 2 operands")
        ft = _parse_fpr(args[0])
        bits = _parse_f32_bits(args[1])
        hi = (bits >> 16) & 0xFFFF
        lo = bits & 0xFFFF
        at = 1
        words = [_encode_itype(0x0F, 0, at, hi)]  # lui $at, hi
        if lo != 0:
            words.append(_encode_itype(0x0D, at, at, lo))  # ori $at, $at, lo
        words.append(_encode_rtype(0x11, 0x04, at, ft, 0, 0x00))  # mtc1 $at, ft
        return words

    if mnem == "li":
        if len(args) != 2:
            raise MipsAssembleError("li expects 2 operands")
        rt = _parse_gpr(args[0])
        val = _parse_int_expr(args[1], labels)
        if -0x8000 <= val <= 0x7FFF:
            return [_encode_itype(0x09, 0, rt, val & 0xFFFF)]  # addiu rt, $zero, imm
        if 0x8000 <= val <= 0xFFFF:
            return [_encode_itype(0x0D, 0, rt, val & 0xFFFF)]  # ori rt, $zero, imm
        u32 = val & 0xFFFFFFFF
        return [
            _encode_itype(0x0F, 0, rt, (u32 >> 16) & 0xFFFF),
            _encode_itype(0x0D, rt, rt, u32 & 0xFFFF),
        ]

    if mnem == "move":
        if len(args) != 2:
            raise MipsAssembleError("move expects 2 operands: move rd, rs")
        rd = _parse_gpr(args[0])
        rs = _parse_gpr(args[1])
        return [_encode_rtype(0x00, rs, 0, rd, 0, 0x2D)]  # daddu rd, rs, $zero

    # Jumps
    if mnem in ("j", "jal"):
        if len(args) != 1:
            raise MipsAssembleError(f"{mnem} expects 1 operand")
        op = 0x02 if mnem == "j" else 0x03
        target = _parse_int_expr(args[0], labels)
        return [_encode_jtype(op, pc, target, mnem)]

    if mnem == "jr":
        if len(args) != 1:
            raise MipsAssembleError("jr expects 1 operand")
        rs = _parse_gpr(args[0])
        return [_encode_rtype(0x00, rs, 0, 0, 0, 0x08)]

    if mnem == "jalr":
        if len(args) == 1:
            rd, rs = 31, _parse_gpr(args[0])
        elif len(args) == 2:
            rd, rs = _parse_gpr(args[0]), _parse_gpr(args[1])
        else:
            raise MipsAssembleError("jalr expects 1 or 2 operands")
        return [_encode_rtype(0x00, rs, 0, rd, 0, 0x09)]

    # Branches
    if mnem == "b":
        if len(args) != 1:
            raise MipsAssembleError("b expects 1 operand")
        target = _parse_int_expr(args[0], labels)
        return [_encode_itype(0x04, 0, 0, _encode_branch_offset(pc, target, "b"))]

    br2_ops = {"beq": 0x04, "bne": 0x05, "beql": 0x14, "bnel": 0x15}
    if mnem in br2_ops:
        if len(args) != 3:
            raise MipsAssembleError(f"{mnem} expects 3 operands")
        rs = _parse_gpr(args[0])
        rt = _parse_gpr(args[1])
        target = _parse_int_expr(args[2], labels)
        return [_encode_itype(br2_ops[mnem], rs, rt, _encode_branch_offset(pc, target, mnem))]

    br1_ops = {"blez": (0x06, 0), "bgtz": (0x07, 0), "bltz": (0x01, 0), "bgez": (0x01, 1)}
    if mnem in br1_ops:
        if len(args) != 2:
            raise MipsAssembleError(f"{mnem} expects 2 operands")
        op, rt_code = br1_ops[mnem]
        rs = _parse_gpr(args[0])
        target = _parse_int_expr(args[1], labels)
        return [_encode_itype(op, rs, rt_code, _encode_branch_offset(pc, target, mnem))]

    if mnem in ("bc1f", "bc1t", "bc1fl", "bc1tl"):
        if len(args) != 1:
            raise MipsAssembleError(f"{mnem} expects 1 operand")
        ft_code = {"bc1f": 0, "bc1t": 1, "bc1fl": 2, "bc1tl": 3}[mnem]
        target = _parse_int_expr(args[0], labels)
        return [_encode_itype(0x11, 0x08, ft_code, _encode_branch_offset(pc, target, mnem))]

    # LUI
    if mnem == "lui":
        if len(args) != 2:
            raise MipsAssembleError("lui expects 2 operands: lui rt, imm16")
        rt = _parse_gpr(args[0])
        imm = _encode_u16(_parse_int_expr(args[1], labels), "lui")
        return [_encode_itype(0x0F, 0, rt, imm)]

    # Signed-immediate ALU
    ialu_s = {
        "addi": 0x08, "addiu": 0x09, "slti": 0x0A, "sltiu": 0x0B,
        "daddi": 0x18, "daddiu": 0x19,
    }
    if mnem in ialu_s:
        if len(args) != 3:
            raise MipsAssembleError(f"{mnem} expects 3 operands")
        rt = _parse_gpr(args[0])
        rs = _parse_gpr(args[1])
        imm = _encode_s16(_parse_int_expr(args[2], labels), mnem)
        return [_encode_itype(ialu_s[mnem], rs, rt, imm)]

    # Unsigned-immediate ALU
    ialu_u = {"andi": 0x0C, "ori": 0x0D, "xori": 0x0E}
    if mnem in ialu_u:
        if len(args) != 3:
            raise MipsAssembleError(f"{mnem} expects 3 operands")
        rt = _parse_gpr(args[0])
        rs = _parse_gpr(args[1])
        imm = _encode_u16(_parse_int_expr(args[2], labels), mnem)
        return [_encode_itype(ialu_u[mnem], rs, rt, imm)]

    # 3-register ALU
    ralu = {
        "add": 0x20, "addu": 0x21, "sub": 0x22, "subu": 0x23,
        "and": 0x24, "or": 0x25, "xor": 0x26, "nor": 0x27,
        "slt": 0x2A, "sltu": 0x2B,
        "dadd": 0x2C, "daddu": 0x2D, "dsub": 0x2E, "dsubu": 0x2F,
    }
    if mnem in ralu:
        if len(args) != 3:
            raise MipsAssembleError(f"{mnem} expects 3 operands")
        rd = _parse_gpr(args[0])
        rs = _parse_gpr(args[1])
        rt = _parse_gpr(args[2])
        return [_encode_rtype(0x00, rs, rt, rd, 0, ralu[mnem])]

    # Shifts
    sh_imm = {"sll": 0x00, "srl": 0x02, "sra": 0x03, "dsll": 0x38, "dsrl": 0x3A, "dsra": 0x3B}
    if mnem in sh_imm:
        if len(args) != 3:
            raise MipsAssembleError(f"{mnem} expects 3 operands")
        rd = _parse_gpr(args[0])
        rt = _parse_gpr(args[1])
        sa = _encode_sa(_parse_int_expr(args[2], labels), mnem)
        return [_encode_rtype(0x00, 0, rt, rd, sa, sh_imm[mnem])]

    sh_var = {"sllv": 0x04, "srlv": 0x06, "srav": 0x07}
    if mnem in sh_var:
        if len(args) != 3:
            raise MipsAssembleError(f"{mnem} expects 3 operands")
        rd = _parse_gpr(args[0])
        rt = _parse_gpr(args[1])
        rs = _parse_gpr(args[2])
        return [_encode_rtype(0x00, rs, rt, rd, 0, sh_var[mnem])]

    # Memory loads/stores (GPR)
    mem_gpr = {
        "lb": 0x20, "lh": 0x21, "lw": 0x23, "lbu": 0x24, "lhu": 0x25, "lwu": 0x27,
        "sb": 0x28, "sh": 0x29, "sw": 0x2B, "ld": 0x37, "sd": 0x3F,
    }
    if mnem in mem_gpr:
        if len(args) != 2:
            raise MipsAssembleError(f"{mnem} expects 2 operands: {mnem} rt, off(rs)")
        rt = _parse_gpr(args[0])
        off, base = _parse_mem_operand(args[1], labels)
        return [_encode_itype(mem_gpr[mnem], base, rt, _encode_s16(off, mnem))]

    # Memory loads/stores (COP1 FPU)
    if mnem in ("lwc1", "swc1"):
        if len(args) != 2:
            raise MipsAssembleError(f"{mnem} expects 2 operands: {mnem} ft, off(rs)")
        op = 0x31 if mnem == "lwc1" else 0x39
        ft = _parse_fpr(args[0])
        off, base = _parse_mem_operand(args[1], labels)
        return [_encode_itype(op, base, ft, _encode_s16(off, mnem))]

    # COP1 FPU moves & arithmetic
    if mnem in ("mfc1", "mtc1"):
        if len(args) != 2:
            raise MipsAssembleError(f"{mnem} expects 2 operands: {mnem} rt, fs")
        fmt = 0x00 if mnem == "mfc1" else 0x04
        rt = _parse_gpr(args[0])
        fs = _parse_fpr(args[1])
        return [_encode_rtype(0x11, fmt, rt, fs, 0, 0x00)]

    fpu_tri = {
        "add.s": 0x00, "sub.s": 0x01, "mul.s": 0x02, "div.s": 0x03,
        "madd.s": 0x1C, "msub.s": 0x1D, "max.s": 0x28, "min.s": 0x29,
    }
    if mnem in fpu_tri:
        if len(args) != 3:
            raise MipsAssembleError(f"{mnem} expects 3 operands")
        fd = _parse_fpr(args[0])
        fs = _parse_fpr(args[1])
        ft = _parse_fpr(args[2])
        return [_encode_rtype(0x11, 0x10, ft, fs, fd, fpu_tri[mnem])]

    fpu_unary = {"abs.s": 0x05, "mov.s": 0x06, "neg.s": 0x07, "cvt.w.s": 0x24}
    if mnem in fpu_unary:
        if len(args) != 2:
            raise MipsAssembleError(f"{mnem} expects 2 operands")
        fd = _parse_fpr(args[0])
        fs = _parse_fpr(args[1])
        return [_encode_rtype(0x11, 0x10, 0, fs, fd, fpu_unary[mnem])]

    if mnem == "sqrt.s":
        if len(args) != 2:
            raise MipsAssembleError("sqrt.s expects 2 operands")
        fd = _parse_fpr(args[0])
        ft = _parse_fpr(args[1])
        return [_encode_rtype(0x11, 0x10, ft, 0, fd, 0x04)]

    if mnem == "cvt.s.w":
        if len(args) != 2:
            raise MipsAssembleError("cvt.s.w expects 2 operands")
        fd = _parse_fpr(args[0])
        fs = _parse_fpr(args[1])
        return [_encode_rtype(0x11, 0x14, 0, fs, fd, 0x20)]

    fpu_cmp = {"c.f.s": 0x30, "c.eq.s": 0x32, "c.lt.s": 0x34, "c.le.s": 0x36}
    if mnem in fpu_cmp:
        if len(args) != 2:
            raise MipsAssembleError(f"{mnem} expects 2 operands")
        fs = _parse_fpr(args[0])
        ft = _parse_fpr(args[1])
        return [_encode_rtype(0x11, 0x10, ft, fs, 0, fpu_cmp[mnem])]

    raise MipsAssembleError(f"Unsupported MIPS R5900 mnemonic: {mnem!r}")


def assemble(
    source: str | Sequence[str],
    base_vaddr: int,
    external_labels: Optional[Dict[str, int]] = None,
    symbols: Optional[elf_disasm.SymbolTable] = None,
) -> AssembledProgram:
    """
    Two-pass MIPS R5900 assembler returning encoded words and elf_disasm round-trip text.
    """
    if base_vaddr & 3:
        raise MipsAssembleError(f"base_vaddr 0x{base_vaddr:08X} must be 4-byte aligned")

    raw_lines = source.splitlines() if isinstance(source, str) else list(source)
    labels: Dict[str, int] = dict(external_labels or {})
    parsed_stmts: List[Tuple[int, str, List[str], str]] = []

    pc = base_vaddr
    for raw in raw_lines:
        clean = _strip_comments(raw)
        if not clean:
            continue
        while True:
            lm = _LABEL_RE.match(clean)
            if not lm:
                break
            lbl = lm.group(1)
            if lbl in labels:
                raise MipsAssembleError(f"Duplicate label {lbl!r}")
            labels[lbl] = pc
            clean = lm.group(2).strip()
            if not clean:
                break
        if not clean:
            continue
        mnem, args = _split_mnemonic_args(clean)
        nwords = _expand_word_count(mnem, args)
        parsed_stmts.append((pc, mnem, args, clean))
        pc += nwords * 4

    prog = AssembledProgram(base_vaddr=base_vaddr, labels=labels)
    for stmt_pc, mnem, args, clean in parsed_stmts:
        words = _assemble_statement(mnem, args, stmt_pc, labels)
        for idx, w in enumerate(words):
            cur_pc = stmt_pc + idx * 4
            dis = elf_disasm.disassemble_instruction(w, cur_pc, symbols=symbols)
            if dis.startswith(".word") and mnem != ".word":
                raise MipsAssembleError(
                    f"Internal encoder verification failed at 0x{cur_pc:08X} ({clean}): decoded {dis}"
                )
            prog.words.append(w)
            prog.instructions.append(
                AssembledInsn(vaddr=cur_pc, word=w, source=clean, disasm=dis)
            )
    return prog


def verify_disasm_roundtrip(
    writes: Sequence[Dict[str, Any]],
    symbols: Optional[elf_disasm.SymbolTable] = None,
    text_end_vaddr: int = 0x004BE880,
) -> List[str]:
    """
    Disassemble every instruction write in a cave/patch through tools/elf_disasm.py
    to confirm valid MIPS R5900 encoding and resolved jump/branch targets.
    """
    notes: List[str] = []
    for w in writes:
        vaddr = int(w["vaddr"], 16)
        patched = int(w["patched"], 16)
        is_code = DEFAULT_PT_VADDR <= vaddr < text_end_vaddr
        if is_code:
            dis = elf_disasm.disassemble_instruction(patched, vaddr, symbols=symbols)
            if dis.startswith(".word") or dis.startswith("cop") or dis.startswith("regimm"):
                raise MipsAssembleError(
                    f"Write at 0x{vaddr:08X} (0x{patched:08X}) did not decode cleanly: {dis}"
                )
            notes.append(f"0x{vaddr:08X}: 0x{patched:08X}  {dis}")
        else:
            notes.append(f"0x{vaddr:08X}: 0x{patched:08X}  [data]")
    return notes


def collect_write_addresses(spec: Dict[str, Any]) -> Dict[int, str]:
    """Return mapping of 4-byte vaddr -> patch_id across all entries in a spec."""
    out: Dict[int, str] = {}
    for p in spec.get("patches", []):
        pid = str(p["id"])
        for w in p.get("writes", []):
            vaddr = int(w["vaddr"], 16)
            out[vaddr] = pid
    return out


def validate_caves_spec(
    caves_spec: Dict[str, Any],
    base_patches_spec: Optional[Dict[str, Any]] = None,
    elf: Optional[elf_disasm.ElfImage] = None,
    symbols: Optional[elf_disasm.SymbolTable] = None,
) -> Dict[str, Any]:
    """
    Validate schema, alignment, PT_LOAD bounds, elf_disasm round-trip,
    non-overlap with elf_patches.json, and optional live ISO expected_orig.
    """
    target = caves_spec.get("target", {})
    pt_vaddr = int(target.get("pt_load_vaddr", f"0x{DEFAULT_PT_VADDR:08X}"), 16)
    max_end = pt_vaddr + DEFAULT_PT_FILESZ

    base_vaddrs: Dict[int, str] = (
        collect_write_addresses(base_patches_spec) if base_patches_spec else {}
    )
    seen_caves_vaddrs: Dict[int, str] = {}
    seen_ids: Set[str] = set()
    total_writes = 0
    verified_iso_writes = 0
    disasm_summary: Dict[str, List[str]] = {}

    for patch in caves_spec.get("patches", []):
        pid = patch["id"]
        if pid in seen_ids:
            raise ValueError(f"Duplicate cave patch id: {pid}")
        seen_ids.add(pid)

        writes = patch.get("writes", [])
        if not writes:
            raise ValueError(f"{pid}: writes list must not be empty")

        # If cave_asm metadata is present, re-assemble and confirm exact word-for-word match!
        cave_asm = patch.get("cave_asm")
        if isinstance(cave_asm, dict):
            cave_base = int(cave_asm["cave_base"], 16)
            asm_lines = cave_asm["instructions"]
            prog = assemble(asm_lines, base_vaddr=cave_base, symbols=symbols)
            cave_write_map = {
                int(w["vaddr"], 16): int(w["patched"], 16) for w in writes
            }
            for insn in prog.instructions:
                if insn.vaddr not in cave_write_map:
                    raise ValueError(
                        f"{pid}: assembled cave word @ 0x{insn.vaddr:08X} missing from writes"
                    )
                if cave_write_map[insn.vaddr] != insn.word:
                    raise ValueError(
                        f"{pid}: cave word mismatch @ 0x{insn.vaddr:08X}: "
                        f"spec=0x{cave_write_map[insn.vaddr]:08X} != asm=0x{insn.word:08X}"
                    )

        # Verify elf_disasm decoding of every code word
        disasm_summary[pid] = verify_disasm_roundtrip(writes, symbols=symbols)

        for w in writes:
            total_writes += 1
            vaddr = int(w["vaddr"], 16)
            orig = int(w["expected_orig"], 16)
            patched = int(w["patched"], 16)

            if vaddr % 4 != 0:
                raise ValueError(f"{pid}: unaligned vaddr 0x{vaddr:08X}")
            if vaddr < pt_vaddr or (vaddr + 4) > max_end:
                raise ValueError(f"{pid}: vaddr 0x{vaddr:08X} outside PT_LOAD")
            if orig == patched:
                raise ValueError(f"{pid}: no-op write at 0x{vaddr:08X}")
            if vaddr in base_vaddrs:
                raise ValueError(
                    f"{pid}: collision at 0x{vaddr:08X} with elf_patches.json:{base_vaddrs[vaddr]}"
                )
            if vaddr in seen_caves_vaddrs:
                raise ValueError(
                    f"{pid}: collision at 0x{vaddr:08X} with elf_caves.json:{seen_caves_vaddrs[vaddr]}"
                )
            seen_caves_vaddrs[vaddr] = pid

            if elf is not None:
                actual = elf.read_u32(vaddr)
                if actual != orig:
                    raise ValueError(
                        f"{pid}: expected_orig mismatch at 0x{vaddr:08X}: "
                        f"actual=0x{actual:08X} expected=0x{orig:08X}"
                    )
                verified_iso_writes += 1

    return {
        "patch_count": len(seen_ids),
        "total_writes": total_writes,
        "verified_iso_writes": verified_iso_writes,
        "disasm": disasm_summary,
    }


def emit_pnach_text(
    caves_spec: Dict[str, Any],
    *,
    fmt: str = "extended",
    conditional_gate: Optional[Tuple[int, int]] = None,
    include_disabled: bool = True,
) -> str:
    """
    Emit a PCSX2 v2 .pnach string from an elf_caves.json spec.

    fmt="extended" emits PS2 extended 32-bit writes (`patch=1,EE,2xxxxxxx,extended,xxxxxxxx`).
    fmt="word" emits PCSX2 typed word writes (`patch=0,EE,00xxxxxx,word,xxxxxxxx`).
    conditional_gate=(addr, mask16) optionally prefixes writes with a verified D0xxxxxx
    pad gate (`patch=1,EE,Dxxxxxxx,extended,0000xxxx`) when supplied explicitly.
    """
    if fmt not in ("extended", "word"):
        raise ValueError(f"Unsupported pnach format {fmt!r}")

    target = caves_spec.get("target", {})
    crc = target.get("elf_pcsx2_crc", "0518D274")
    serial = target.get("serial", "SLUS-21351")

    lines: List[str] = [
        "gametitle=Need for Speed - Most Wanted [Black Edition] (NTSC-U) [SLUS-21351]",
        f"comment=NFSMW: Rockport 2026 — Verified MIPS Code Caves for {serial} (CRC: {crc})",
        "",
    ]

    for patch in caves_spec.get("patches", []):
        if not include_disabled and not patch.get("enabled_by_default", False):
            continue
        lines.append(f"[{patch['name']}]")
        lines.append(f"author={patch.get('author', 'NFSMW: Rockport 2026')}")
        lines.append(f"description={patch.get('description', '')}")

        writes = patch.get("writes", [])
        if conditional_gate is not None:
            gate_addr, gate_mask = conditional_gate
            # E0 count word: 0xE0<count:2hex><mask:4hex>  addr = gate_addr
            if fmt != "extended":
                raise ValueError("conditional_gate requires fmt='extended'")
            count = len(writes) & 0xFF
            lines.append(
                f"patch=1,EE,E0{count:02X}{gate_mask & 0xFFFF:04X},extended,{gate_addr & 0x0FFFFFFF:08X}"
            )

        for w in writes:
            vaddr = int(w["vaddr"], 16)
            patched = int(w["patched"], 16)
            note = w.get("note", "")
            comment_suffix = f" // {note}" if note else ""
            if fmt == "extended":
                lines.append(
                    f"patch=1,EE,{(0x20000000 | (vaddr & 0x0FFFFFFF)):08X},"
                    f"extended,{patched:08X}{comment_suffix}"
                )
            else:
                lines.append(
                    f"patch=0,EE,{vaddr:08X},word,{patched:08X}{comment_suffix}"
                )
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    lint_errors = lint_pnach.lint_pnach_text(text, source_name="<generated-caves.pnach>")
    if lint_errors:
        joined = "; ".join(e.format_human() for e in lint_errors)
        raise ValueError(f"Generated .pnach failed lint_pnach: {joined}")
    return text


def _cmd_asm(args: argparse.Namespace) -> int:
    base = int(args.base, 16)
    src = args.asm_file.read_text(encoding="utf-8") if args.asm_file else args.code
    tbl = elf_disasm.load_symbols_optional(args.symbols)
    prog = assemble(src, base_vaddr=base, symbols=tbl)
    for insn in prog.instructions:
        print(f"  0x{insn.vaddr:08X}: {insn.word:08X}   {insn.disasm}")
    print(f"Assembled {len(prog.words)} words ({prog.byte_size} bytes) @ 0x{base:08X}.")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    caves_spec = json.loads(args.caves.read_text(encoding="utf-8"))
    base_spec = (
        json.loads(args.base_patches.read_text(encoding="utf-8"))
        if args.base_patches and args.base_patches.is_file()
        else None
    )
    tbl = elf_disasm.load_symbols_optional(args.symbols)
    elf: Optional[elf_disasm.ElfImage] = None
    if args.elf:
        elf = elf_disasm.ElfImage.from_elf(args.elf)
    else:
        iso_path = find_local_iso(args.iso)
        if iso_path is not None:
            elf = elf_disasm.ElfImage.from_iso(iso_path)

    report = validate_caves_spec(
        caves_spec,
        base_patches_spec=base_spec,
        elf=elf,
        symbols=tbl,
    )
    for pid, lines in report["disasm"].items():
        print(f"\n[{pid}]")
        for line in lines:
            print(f"  {line}")
    iso_note = (
        f"{report['verified_iso_writes']}/{report['total_writes']} live SLUS_213.51 words verified"
        if elf is not None
        else "offline mode (no ISO provided)"
    )
    print(
        f"\nOK: {report['patch_count']} caves, {report['total_writes']} writes, "
        f"zero overlap with elf_patches.json, {iso_note}."
    )
    return 0


def _cmd_emit_pnach(args: argparse.Namespace) -> int:
    caves_spec = json.loads(args.caves.read_text(encoding="utf-8"))
    text = emit_pnach_text(
        caves_spec,
        fmt=args.format,
        include_disabled=not args.defaults_only,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote validated .pnach -> {args.out}")
    else:
        print(text, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MIPS R5900 (PS2 EE) mini-assembler, cave compiler & verifier"
    )
    p.add_argument("--symbols", type=Path, default=None)
    p.add_argument("--iso", type=Path, default=None)
    p.add_argument("--elf", type=Path, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("asm", help="Assemble MIPS R5900 instructions and verify via elf_disasm")
    sp.add_argument("--base", default="0x003037A4", help="Base EE virtual address (hex)")
    sp.add_argument("--file", dest="asm_file", type=Path, default=None, help="Assembly source file")
    sp.add_argument("code", nargs="?", default="", help="Inline assembly (semicolon/newline separated)")
    sp.set_defaults(func=_cmd_asm)

    sp = sub.add_parser("verify", help="Verify elf_caves.json vs elf_disasm, elf_patches.json, and ISO")
    sp.add_argument("caves", type=Path, nargs="?", default=repo_root() / "patches" / "elf_caves.json")
    sp.add_argument(
        "--base-patches",
        type=Path,
        default=repo_root() / "patches" / "elf_patches.json",
    )
    sp.set_defaults(func=_cmd_verify)

    sp = sub.add_parser("emit-pnach", help="Emit and lint PCSX2 .pnach from elf_caves.json")
    sp.add_argument("caves", type=Path, nargs="?", default=repo_root() / "patches" / "elf_caves.json")
    sp.add_argument("--format", choices=["extended", "word"], default="extended")
    sp.add_argument("--defaults-only", action="store_true")
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=_cmd_emit_pnach)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
