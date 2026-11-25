#!/usr/bin/env python3
"""
elf_disasm.py — Pure-Python MIPS R5900 (PS2 Emotion Engine) disassembler,
symbol manager, and cross-reference analyzer for SLUS_213.51
(Need for Speed: Most Wanted - Black Edition, NTSC-U, CRC 0518D274).

SAFETY:
- Opens source ISOs and ELFs strictly read-only ('rb'). Never modifies disc images.
- Caches CC0 symbol maps from dbalatoni13/nfsmw strictly inside gitignored build/.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import struct
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from tools.iso_inspect import SECTOR_SIZE, parse_elf_header, parse_iso9660
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from iso_inspect import SECTOR_SIZE, parse_elf_header, parse_iso9660

DEFAULT_SYMBOLS_URL = (
    "https://raw.githubusercontent.com/dbalatoni13/nfsmw/main/"
    "config/SLUS-21351/symbols.txt"
)
DEFAULT_ELF_NAME = "SLUS_213.51"
DEFAULT_PT_LOAD_VADDR = 0x00100000
DEFAULT_PT_LOAD_OFFSET = 0x00001000
DEFAULT_TEXT_END_VADDR = 0x004A0000

GPR_NAMES: Tuple[str, ...] = (
    "$zero", "$at", "$v0", "$v1", "$a0", "$a1", "$a2", "$a3",
    "$t0", "$t1", "$t2", "$t3", "$t4", "$t5", "$t6", "$t7",
    "$s0", "$s1", "$s2", "$s3", "$s4", "$s5", "$s6", "$s7",
    "$t8", "$t9", "$k0", "$k1", "$gp", "$sp", "$fp", "$ra",
)

FPR_NAMES: Tuple[str, ...] = tuple(f"$f{i}" for i in range(32))

COP0_REGS: Dict[int, str] = {
    0: "Index", 1: "Random", 2: "EntryLo0", 3: "EntryLo1",
    4: "Context", 5: "PageMask", 6: "Wired", 8: "BadVAddr",
    9: "Count", 10: "EntryHi", 11: "Compare", 12: "Status",
    13: "Cause", 14: "EPC", 15: "PRId", 16: "Config",
    23: "BadPAddr", 24: "Debug", 25: "Perf", 28: "TagLo",
    29: "TagHi", 30: "ErrorEPC",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_symbols_path() -> Path:
    return repo_root() / "build" / "symbols_slus21351.txt"


def sign16(val: int) -> int:
    return val - 0x10000 if val & 0x8000 else val


def fmt_s16(val: int) -> str:
    s = sign16(val)
    if s < 0:
        return f"-0x{-s:x}"
    return f"0x{s:x}"


def vaddr_to_elf_offset(
    vaddr: int,
    pt_vaddr: int = DEFAULT_PT_LOAD_VADDR,
    pt_offset: int = DEFAULT_PT_LOAD_OFFSET,
) -> int:
    """Translate PS2 EE virtual address to ELF file offset."""
    return vaddr - pt_vaddr + pt_offset


def elf_offset_to_vaddr(
    offset: int,
    pt_vaddr: int = DEFAULT_PT_LOAD_VADDR,
    pt_offset: int = DEFAULT_PT_LOAD_OFFSET,
) -> int:
    """Translate ELF file offset to PS2 EE virtual address."""
    return offset - pt_offset + pt_vaddr


@dataclass(frozen=True)
class Symbol:
    name: str
    vaddr: int
    sym_type: str = "unknown"  # "func", "data", or "unknown"
    size: Optional[int] = None
    source_unit: str = ""


class SymbolTable:
    """Parsed splat/decomp symbols.txt table with fast address & regex lookup."""

    _SYM_RE = re.compile(
        r"^\s*([A-Za-z_.$?@0-9]+)\s*=\s*0x([0-9A-Fa-f]+)\s*;(?:\s*//\s*(.*))?$"
    )
    _TYPE_RE = re.compile(r"type:(\w+)")
    _SIZE_RE = re.compile(r"size:0x([0-9A-Fa-f]+)")

    def __init__(self, symbols: Sequence[Symbol] = ()) -> None:
        self.symbols: List[Symbol] = sorted(symbols, key=lambda s: (s.vaddr, s.name))
        self._addrs: List[int] = [s.vaddr for s in self.symbols]
        self._by_name: Dict[str, Symbol] = {}
        self._by_addr: Dict[int, List[Symbol]] = {}
        for s in self.symbols:
            self._by_name.setdefault(s.name, s)
            self._by_addr.setdefault(s.vaddr, []).append(s)

    @classmethod
    def from_file(cls, path: Path) -> "SymbolTable":
        text = path.read_text(encoding="utf-8", errors="replace")
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> "SymbolTable":
        syms: List[Symbol] = []
        current_unit = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("//") and not line.startswith("///"):
                candidate = line[2:].strip()
                if "/" in candidate or "." in candidate:
                    current_unit = candidate
                continue
            m = cls._SYM_RE.match(line)
            if not m:
                continue
            name, addr_hex, comment = m.group(1), m.group(2), m.group(3) or ""
            vaddr = int(addr_hex, 16)
            tm = cls._TYPE_RE.search(comment)
            sm = cls._SIZE_RE.search(comment)
            sym_type = tm.group(1) if tm else ("data" if sm else "unknown")
            size = int(sm.group(1), 16) if sm else None
            syms.append(
                Symbol(
                    name=name,
                    vaddr=vaddr,
                    sym_type=sym_type,
                    size=size,
                    source_unit=current_unit,
                )
            )
        return cls(syms)

    def __len__(self) -> int:
        return len(self.symbols)

    def get_by_name(self, name: str) -> Optional[Symbol]:
        return self._by_name.get(name)

    def exact(self, vaddr: int) -> List[Symbol]:
        return self._by_addr.get(vaddr, [])

    def nearest(self, vaddr: int, max_offset: int = 0x10000) -> Optional[Tuple[Symbol, int]]:
        if not self._addrs:
            return None
        idx = bisect.bisect_right(self._addrs, vaddr) - 1
        if idx < 0:
            return None
        sym = self.symbols[idx]
        diff = vaddr - sym.vaddr
        if diff <= max_offset:
            return sym, diff
        return None

    def format_addr(self, vaddr: int, max_offset: int = 0x4000) -> Optional[str]:
        ex = self.exact(vaddr)
        if ex:
            return ex[0].name
        near = self.nearest(vaddr, max_offset=max_offset)
        if near:
            sym, diff = near
            if sym.size is None or diff < sym.size:
                return f"{sym.name}+0x{diff:x}"
        return None

    def next_symbol_addr(self, vaddr: int) -> Optional[int]:
        idx = bisect.bisect_right(self._addrs, vaddr)
        if idx < len(self._addrs):
            return self._addrs[idx]
        return None

    def search(
        self,
        pattern: str,
        sym_type: Optional[str] = None,
        ignore_case: bool = True,
    ) -> List[Symbol]:
        flags = re.IGNORECASE if ignore_case else 0
        rx = re.compile(pattern, flags)
        out: List[Symbol] = []
        for s in self.symbols:
            if sym_type and s.sym_type != sym_type:
                continue
            if rx.search(s.name) or rx.search(s.source_unit):
                out.append(s)
        return out


def fetch_symbols(
    out_path: Optional[Path] = None,
    url: str = DEFAULT_SYMBOLS_URL,
    force: bool = False,
) -> Path:
    """Download CC0 SLUS-21351 symbols.txt into gitignored build/ directory."""
    target = out_path or default_symbols_path()
    if target.is_file() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": "NFSMW-Rockport2026-elf-disasm/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if len(data) < 1024 or b"0x100000" not in data:
        raise ValueError(f"Downloaded symbol file from {url} looks unexpected ({len(data)} bytes)")
    target.write_bytes(data)
    return target


class ElfImage:
    """Read-only view of a PS2 ELF executable (loaded directly from ISO or ELF file)."""

    def __init__(self, data: bytes, name: str = DEFAULT_ELF_NAME) -> None:
        self.data = data
        self.name = name
        self.header = parse_elf_header(data)
        load_segs = [s for s in self.header["segments"] if s["type"] == 1]
        if not load_segs:
            raise ValueError("ELF contains no PT_LOAD segments")
        seg = load_segs[0]
        self.pt_vaddr: int = seg["vaddr"]
        self.pt_offset: int = seg["offset"]
        self.pt_filesz: int = seg["filesz"]
        self.pt_memsz: int = seg["memsz"]
        self.vaddr_end: int = self.pt_vaddr + self.pt_filesz
        self.mem_end: int = self.pt_vaddr + self.pt_memsz

    @classmethod
    def from_iso(cls, iso_path: Path, elf_name: str = DEFAULT_ELF_NAME) -> "ElfImage":
        with iso_path.open("rb") as f:
            _, _, entries = parse_iso9660(f)
            target = elf_name.upper()
            for e in entries:
                if not e["is_dir"] and e["path"].upper() == target:
                    f.seek(e["lba"] * SECTOR_SIZE)
                    return cls(f.read(e["size"]), name=e["path"])
        raise FileNotFoundError(f"ELF {elf_name!r} not found in ISO {iso_path}")

    @classmethod
    def from_elf(cls, elf_path: Path) -> "ElfImage":
        return cls(elf_path.read_bytes(), name=elf_path.name)

    def vaddr_to_offset(self, vaddr: int) -> int:
        return vaddr_to_elf_offset(vaddr, self.pt_vaddr, self.pt_offset)

    def offset_to_vaddr(self, offset: int) -> int:
        return elf_offset_to_vaddr(offset, self.pt_vaddr, self.pt_offset)

    def contains_vaddr(self, vaddr: int, size: int = 4) -> bool:
        return self.pt_vaddr <= vaddr and (vaddr + size) <= self.vaddr_end

    def read_bytes(self, vaddr: int, size: int) -> bytes:
        if not self.contains_vaddr(vaddr, size):
            raise ValueError(
                f"vaddr 0x{vaddr:08X} (+{size}) outside ELF file range "
                f"[0x{self.pt_vaddr:08X}, 0x{self.vaddr_end:08X})"
            )
        off = self.vaddr_to_offset(vaddr)
        return self.data[off : off + size]

    def read_u8(self, vaddr: int) -> int:
        return self.read_bytes(vaddr, 1)[0]

    def read_u16(self, vaddr: int) -> int:
        return struct.unpack("<H", self.read_bytes(vaddr, 2))[0]

    def read_u32(self, vaddr: int) -> int:
        return struct.unpack("<I", self.read_bytes(vaddr, 4))[0]

    def read_s32(self, vaddr: int) -> int:
        return struct.unpack("<i", self.read_bytes(vaddr, 4))[0]

    def read_f32(self, vaddr: int) -> float:
        return struct.unpack("<f", self.read_bytes(vaddr, 4))[0]

    def read_cstring(self, vaddr: int, max_len: int = 128) -> Optional[str]:
        if not self.contains_vaddr(vaddr, 1):
            return None
        off = self.vaddr_to_offset(vaddr)
        end = min(len(self.data), off + max_len)
        chunk = self.data[off:end]
        nul = chunk.find(b"\x00")
        if nul > 0:
            raw = chunk[:nul]
            if all(32 <= b < 127 or b in (9, 10, 13) for b in raw):
                return raw.decode("ascii")
        return None


def _disasm_special(word: int) -> str:
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    rd = (word >> 11) & 0x1F
    sa = (word >> 6) & 0x1F
    fn = word & 0x3F
    r = GPR_NAMES

    if word == 0:
        return "nop"
    if fn == 0x00:
        return f"sll     {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x02:
        return f"srl     {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x03:
        return f"sra     {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x04:
        return f"sllv    {r[rd]}, {r[rt]}, {r[rs]}"
    if fn == 0x06:
        return f"srlv    {r[rd]}, {r[rt]}, {r[rs]}"
    if fn == 0x07:
        return f"srav    {r[rd]}, {r[rt]}, {r[rs]}"
    if fn == 0x08:
        return f"jr      {r[rs]}"
    if fn == 0x09:
        return f"jalr    {r[rs]}" if rd == 31 else f"jalr    {r[rd]}, {r[rs]}"
    if fn == 0x0A:
        return f"movz    {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x0B:
        return f"movn    {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x0C:
        return f"syscall 0x{(word >> 6) & 0xFFFFF:x}"
    if fn == 0x0D:
        return f"break   0x{(word >> 6) & 0xFFFFF:x}"
    if fn == 0x0F:
        return "sync.p" if sa == 0x10 else "sync.l"
    if fn == 0x10:
        return f"mfhi    {r[rd]}"
    if fn == 0x11:
        return f"mthi    {r[rs]}"
    if fn == 0x12:
        return f"mflo    {r[rd]}"
    if fn == 0x13:
        return f"mtlo    {r[rs]}"
    if fn == 0x14:
        return f"dsllv   {r[rd]}, {r[rt]}, {r[rs]}"
    if fn == 0x16:
        return f"dsrlv   {r[rd]}, {r[rt]}, {r[rs]}"
    if fn == 0x17:
        return f"dsrav   {r[rd]}, {r[rt]}, {r[rs]}"
    if fn == 0x18:
        # R5900 supports 3-operand mult rd, rs, rt
        return f"mult    {r[rd]}, {r[rs]}, {r[rt]}" if rd != 0 else f"mult    {r[rs]}, {r[rt]}"
    if fn == 0x19:
        return f"multu   {r[rd]}, {r[rs]}, {r[rt]}" if rd != 0 else f"multu   {r[rs]}, {r[rt]}"
    if fn == 0x1A:
        return f"div     {r[rs]}, {r[rt]}"
    if fn == 0x1B:
        return f"divu    {r[rs]}, {r[rt]}"
    if fn == 0x20:
        return f"add     {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x21:
        if rt == 0:
            return f"move    {r[rd]}, {r[rs]}"
        return f"addu    {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x22:
        return f"sub     {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x23:
        return f"subu    {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x24:
        return f"and     {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x25:
        if rt == 0:
            return f"move    {r[rd]}, {r[rs]}"
        return f"or      {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x26:
        return f"xor     {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x27:
        return f"nor     {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x28:
        return f"mfsa    {r[rd]}"
    if fn == 0x29:
        return f"mtsa    {r[rs]}"
    if fn == 0x2A:
        return f"slt     {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x2B:
        return f"sltu    {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x2C:
        return f"dadd    {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x2D:
        if rt == 0:
            return f"move    {r[rd]}, {r[rs]}"
        return f"daddu   {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x2E:
        return f"dsub    {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x2F:
        return f"dsubu   {r[rd]}, {r[rs]}, {r[rt]}"
    if fn in (0x30, 0x31, 0x32, 0x33, 0x34, 0x36):
        names = {0x30: "tge", 0x31: "tgeu", 0x32: "tlt", 0x33: "tltu", 0x34: "teq", 0x36: "tne"}
        return f"{names[fn]:<7s} {r[rs]}, {r[rt]}"
    if fn == 0x38:
        return f"dsll    {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3A:
        return f"dsrl    {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3B:
        return f"dsra    {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3C:
        return f"dsll32  {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3E:
        return f"dsrl32  {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3F:
        return f"dsra32  {r[rd]}, {r[rt]}, {sa}"
    return f".word   0x{word:08x}  # special fn=0x{fn:02x}"


def _disasm_mmi(word: int) -> str:
    """Decode R5900 Emotion Engine MMI (opcode 0x1C) instructions."""
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    rd = (word >> 11) & 0x1F
    sa = (word >> 6) & 0x1F
    fn = word & 0x3F
    r = GPR_NAMES

    if fn == 0x00:
        return f"madd    {r[rd]}, {r[rs]}, {r[rt]}" if rd else f"madd    {r[rs]}, {r[rt]}"
    if fn == 0x01:
        return f"maddu   {r[rd]}, {r[rs]}, {r[rt]}" if rd else f"maddu   {r[rs]}, {r[rt]}"
    if fn == 0x04:
        return f"plzcw   {r[rd]}, {r[rs]}"
    if fn == 0x08:  # MMI0
        mmi0 = {
            0x00: "paddw", 0x01: "psubw", 0x02: "pcgtw", 0x03: "pmaxw",
            0x04: "paddh", 0x05: "psubh", 0x06: "pcgth", 0x07: "pmaxh",
            0x08: "paddb", 0x09: "psubb", 0x0A: "pcgtb",
            0x10: "paddsw", 0x11: "psubsw", 0x12: "pextlw", 0x13: "ppacw",
            0x14: "paddsh", 0x15: "psubsh", 0x16: "pextlh", 0x17: "ppach",
            0x18: "paddsb", 0x19: "psubsb", 0x1A: "pextlb", 0x1B: "ppacb",
            0x1E: "pext5", 0x1F: "ppac5",
        }
        op = mmi0.get(sa)
        if op:
            return f"{op:<7s} {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x09:  # MMI2
        mmi2 = {
            0x00: "pmaddw", 0x02: "psllvw", 0x03: "psrlvw", 0x04: "pmsubw",
            0x08: "pmfhi", 0x09: "pmflo", 0x0A: "pinth", 0x0C: "pmultw",
            0x0D: "pdivw", 0x0E: "pcpyld", 0x10: "pmaddh", 0x11: "phmadh",
            0x12: "pand", 0x13: "pxor", 0x14: "pmsubh", 0x15: "phmsbh",
            0x1A: "pexeuh", 0x1B: "prevh", 0x1C: "pmulth", 0x1D: "pdivbw",
            0x1E: "pexew", 0x1F: "prot3w",
        }
        op = mmi2.get(sa)
        if op:
            return f"{op:<7s} {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x10:
        return f"mfhi1   {r[rd]}"
    if fn == 0x11:
        return f"mthi1   {r[rs]}"
    if fn == 0x12:
        return f"mflo1   {r[rd]}"
    if fn == 0x13:
        return f"mtlo1   {r[rs]}"
    if fn == 0x18:
        return f"mult1   {r[rd]}, {r[rs]}, {r[rt]}" if rd else f"mult1   {r[rs]}, {r[rt]}"
    if fn == 0x19:
        return f"multu1  {r[rd]}, {r[rs]}, {r[rt]}" if rd else f"multu1  {r[rs]}, {r[rt]}"
    if fn == 0x1A:
        return f"div1    {r[rs]}, {r[rt]}"
    if fn == 0x1B:
        return f"divu1   {r[rs]}, {r[rt]}"
    if fn == 0x20:
        return f"madd1   {r[rd]}, {r[rs]}, {r[rt]}" if rd else f"madd1   {r[rs]}, {r[rt]}"
    if fn == 0x21:
        return f"maddu1  {r[rd]}, {r[rs]}, {r[rt]}" if rd else f"maddu1  {r[rs]}, {r[rt]}"
    if fn == 0x28:  # MMI1
        mmi1 = {
            0x01: "pabsw", 0x02: "pceqw", 0x03: "pminw", 0x04: "padsbh",
            0x05: "pabsh", 0x06: "pceqh", 0x07: "pminh", 0x0A: "pceqb",
            0x10: "padduw", 0x11: "psubuw", 0x12: "pextuw",
            0x14: "padduh", 0x15: "psubuh", 0x16: "pextuh",
            0x18: "paddub", 0x19: "psubub", 0x1A: "pextub", 0x1B: "qfsrv",
        }
        op = mmi1.get(sa)
        if op:
            return f"{op:<7s} {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x29:  # MMI3
        mmi3 = {
            0x00: "pmadduw", 0x03: "psravw", 0x08: "pmthi", 0x09: "pmtlo",
            0x0A: "pinteh", 0x0C: "pmultuw", 0x0D: "pdivuw", 0x0E: "pcpyud",
            0x12: "por", 0x13: "pnor", 0x1A: "pexch", 0x1B: "pcpyh", 0x1E: "pexcw",
        }
        op = mmi3.get(sa)
        if op:
            if op in ("pcpyh", "pexch", "pexcw"):
                return f"{op:<7s} {r[rd]}, {r[rt]}"
            if op == "por" and rt == 0:
                return f"por     {r[rd]}, {r[rs]}, $zero"
            return f"{op:<7s} {r[rd]}, {r[rs]}, {r[rt]}"
    if fn == 0x30:
        return f"pmfhl   {r[rd]} (fmt={sa})"
    if fn == 0x31:
        return f"pmthl.lw {r[rs]}"
    if fn == 0x34:
        return f"psllh   {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x36:
        return f"psrlh   {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x37:
        return f"psrah   {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3C:
        return f"psllw   {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3E:
        return f"psrlw   {r[rd]}, {r[rt]}, {sa}"
    if fn == 0x3F:
        return f"psraw   {r[rd]}, {r[rt]}, {sa}"
    return f"mmi     0x{word:08x}  # fn=0x{fn:02x} sa=0x{sa:02x}"


def _disasm_cop1(word: int, pc: int) -> str:
    fmt = (word >> 21) & 0x1F
    ft = (word >> 16) & 0x1F
    fs = (word >> 11) & 0x1F
    fd = (word >> 6) & 0x1F
    fn = word & 0x3F
    r = GPR_NAMES
    f = FPR_NAMES

    if fmt == 0x00:
        return f"mfc1    {r[ft]}, {f[fs]}"
    if fmt == 0x02:
        return f"cfc1    {r[ft]}, ${fs}"
    if fmt == 0x04:
        return f"mtc1    {r[ft]}, {f[fs]}"
    if fmt == 0x06:
        return f"ctc1    {r[ft]}, ${fs}"
    if fmt == 0x08:
        target = (pc + 4 + (sign16(word & 0xFFFF) << 2)) & 0xFFFFFFFF
        bc = {0: "bc1f", 1: "bc1t", 2: "bc1fl", 3: "bc1tl"}.get(ft, f"bc1?{ft}")
        return f"{bc:<7s} 0x{target:08X}"
    if fmt == 0x10:  # single-precision .s (R5900 FPU)
        tri = {
            0x00: "add.s", 0x01: "sub.s", 0x02: "mul.s", 0x03: "div.s",
            0x1C: "madd.s", 0x1D: "msub.s", 0x28: "max.s", 0x29: "min.s",
        }
        if fn in tri:
            return f"{tri[fn]:<7s} {f[fd]}, {f[fs]}, {f[ft]}"
        acc_bin = {
            0x18: "adda.s", 0x19: "suba.s", 0x1A: "mula.s",
            0x1E: "madda.s", 0x1F: "msuba.s",
        }
        if fn in acc_bin:
            return f"{acc_bin[fn]:<7s} {f[fs]}, {f[ft]}"
        unary = {
            0x04: "sqrt.s", 0x05: "abs.s", 0x06: "mov.s", 0x07: "neg.s",
            0x24: "cvt.w.s",
        }
        if fn in unary:
            if fn == 0x04:
                return f"sqrt.s  {f[fd]}, {f[ft]}"
            return f"{unary[fn]:<7s} {f[fd]}, {f[fs]}"
        if fn == 0x16:
            return f"rsqrt.s {f[fd]}, {f[fs]}, {f[ft]}"
        cmp_ops = {
            0x30: "c.f.s", 0x32: "c.eq.s", 0x34: "c.lt.s", 0x36: "c.le.s",
        }
        if fn in cmp_ops:
            return f"{cmp_ops[fn]:<7s} {f[fs]}, {f[ft]}"
    if fmt == 0x14 and fn == 0x20:
        return f"cvt.s.w {f[fd]}, {f[fs]}"
    return f"cop1    0x{word:08x}  # fmt=0x{fmt:02x} fn=0x{fn:02x}"


def _disasm_cop0(word: int, pc: int) -> str:
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    rd = (word >> 11) & 0x1F
    fn = word & 0x3F
    r = GPR_NAMES
    c0 = COP0_REGS.get(rd, f"${rd}")
    if rs == 0x00:
        return f"mfc0    {r[rt]}, {c0}"
    if rs == 0x04:
        return f"mtc0    {r[rt]}, {c0}"
    if rs == 0x08:
        target = (pc + 4 + (sign16(word & 0xFFFF) << 2)) & 0xFFFFFFFF
        bc = {0: "bc0f", 1: "bc0t", 2: "bc0fl", 3: "bc0tl"}.get(rt, f"bc0?{rt}")
        return f"{bc:<7s} 0x{target:08X}"
    if rs == 0x10:
        ops = {0x01: "tlbr", 0x02: "tlbwi", 0x06: "tlbwr", 0x08: "tlbp", 0x18: "eret", 0x38: "ei", 0x39: "di"}
        if fn in ops:
            return ops[fn]
    return f"cop0    0x{word:08x}"


def _disasm_cop2(word: int, pc: int) -> str:
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    rd = (word >> 11) & 0x1F
    r = GPR_NAMES
    if rs == 0x01:
        return f"qmfc2   {r[rt]}, $vf{rd}"
    if rs == 0x02:
        return f"cfc2    {r[rt]}, $vi{rd}"
    if rs == 0x05:
        return f"qmtc2   {r[rt]}, $vf{rd}"
    if rs == 0x06:
        return f"ctc2    {r[rt]}, $vi{rd}"
    if rs == 0x08:
        target = (pc + 4 + (sign16(word & 0xFFFF) << 2)) & 0xFFFFFFFF
        bc = {0: "bc2f", 1: "bc2t", 2: "bc2fl", 3: "bc2tl"}.get(rt, f"bc2?{rt}")
        return f"{bc:<7s} 0x{target:08X}"
    return f"vu0     0x{word:08x}"


def disassemble_instruction(
    word: int,
    pc: int,
    symbols: Optional[SymbolTable] = None,
) -> str:
    """Disassemble a single 32-bit MIPS R5900 instruction."""
    op = (word >> 26) & 0x3F
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    imm = word & 0xFFFF
    r = GPR_NAMES
    f = FPR_NAMES

    if op == 0x00:
        return _disasm_special(word)
    if op == 0x01:
        target = (pc + 4 + (sign16(imm) << 2)) & 0xFFFFFFFF
        regimm_br = {
            0x00: "bltz", 0x01: "bgez", 0x02: "bltzl", 0x03: "bgezl",
            0x10: "bltzal", 0x11: "bgezal", 0x12: "bltzall", 0x13: "bgezall",
        }
        if rt in regimm_br:
            return f"{regimm_br[rt]:<7s} {r[rs]}, 0x{target:08X}"
        regimm_imm = {
            0x08: "tgei", 0x09: "tgeiu", 0x0A: "tlti", 0x0B: "tltiu",
            0x0C: "teqi", 0x0E: "tnei", 0x18: "mtsab", 0x19: "mtsah",
        }
        if rt in regimm_imm:
            return f"{regimm_imm[rt]:<7s} {r[rs]}, {fmt_s16(imm)}"
        return f"regimm  0x{word:08x}"
    if op in (0x02, 0x03):
        target = ((pc + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)
        mnem = "j" if op == 0x02 else "jal"
        sym = symbols.format_addr(target, max_offset=0) if symbols else None
        if sym:
            return f"{mnem:<7s} 0x{target:08X}  # {sym}"
        return f"{mnem:<7s} 0x{target:08X}"

    branches_2 = {0x04: "beq", 0x05: "bne", 0x14: "beql", 0x15: "bnel"}
    if op in branches_2:
        target = (pc + 4 + (sign16(imm) << 2)) & 0xFFFFFFFF
        if op == 0x04 and rs == 0 and rt == 0:
            return f"b       0x{target:08X}"
        return f"{branches_2[op]:<7s} {r[rs]}, {r[rt]}, 0x{target:08X}"

    branches_1 = {0x06: "blez", 0x07: "bgtz", 0x16: "blezl", 0x17: "bgtzl"}
    if op in branches_1:
        target = (pc + 4 + (sign16(imm) << 2)) & 0xFFFFFFFF
        return f"{branches_1[op]:<7s} {r[rs]}, 0x{target:08X}"

    alu_s = {0x08: "addi", 0x09: "addiu", 0x0A: "slti", 0x0B: "sltiu", 0x18: "daddi", 0x19: "daddiu"}
    if op in alu_s:
        if op == 0x09 and rs == 0:
            return f"li      {r[rt]}, {sign16(imm)}"
        return f"{alu_s[op]:<7s} {r[rt]}, {r[rs]}, {fmt_s16(imm)}"

    alu_u = {0x0C: "andi", 0x0D: "ori", 0x0E: "xori"}
    if op in alu_u:
        return f"{alu_u[op]:<7s} {r[rt]}, {r[rs]}, 0x{imm:x}"

    if op == 0x0F:
        return f"lui     {r[rt]}, 0x{imm:04x}"
    if op == 0x10:
        return _disasm_cop0(word, pc)
    if op == 0x11:
        return _disasm_cop1(word, pc)
    if op == 0x12:
        return _disasm_cop2(word, pc)
    if op == 0x1C:
        return _disasm_mmi(word)

    mem_gpr = {
        0x1A: "ldl", 0x1B: "ldr", 0x1E: "lq", 0x1F: "sq",
        0x20: "lb", 0x21: "lh", 0x22: "lwl", 0x23: "lw",
        0x24: "lbu", 0x25: "lhu", 0x26: "lwr", 0x27: "lwu",
        0x28: "sb", 0x29: "sh", 0x2A: "swl", 0x2B: "sw",
        0x2C: "sdl", 0x2D: "sdr", 0x2E: "swr",
        0x37: "ld", 0x3F: "sd",
    }
    if op in mem_gpr:
        return f"{mem_gpr[op]:<7s} {r[rt]}, {fmt_s16(imm)}({r[rs]})"
    if op == 0x2F:
        return f"cache   0x{rt:x}, {fmt_s16(imm)}({r[rs]})"
    if op == 0x33:
        return f"pref    0x{rt:x}, {fmt_s16(imm)}({r[rs]})"
    if op == 0x31:
        return f"lwc1    {f[rt]}, {fmt_s16(imm)}({r[rs]})"
    if op == 0x39:
        return f"swc1    {f[rt]}, {fmt_s16(imm)}({r[rs]})"
    if op == 0x36:
        return f"lqc2    $vf{rt}, {fmt_s16(imm)}({r[rs]})"
    if op == 0x3E:
        return f"sqc2    $vf{rt}, {fmt_s16(imm)}({r[rs]})"

    return f".word   0x{word:08x}"


def _format_ref_value(elf: ElfImage, vaddr: int, is_float: bool = False) -> str:
    if not elf.contains_vaddr(vaddr, 4):
        return ""
    if is_float and (vaddr & 3) == 0:
        fv = elf.read_f32(vaddr)
        uv = elf.read_u32(vaddr)
        if math.isfinite(fv):
            return f"f32={fv:g} (0x{uv:08X})"
        return f"u32=0x{uv:08X}"
    s = elf.read_cstring(vaddr)
    if s and len(s) >= 3:
        return f'str="{s[:40]}"'
    if (vaddr & 3) == 0:
        uv = elf.read_u32(vaddr)
        fv = elf.read_f32(vaddr)
        if 1e-4 <= abs(fv) <= 1e6 and math.isfinite(fv):
            return f"u32=0x{uv:08X} / f32={fv:g}"
        return f"u32=0x{uv:08X}"
    return f"u8=0x{elf.read_u8(vaddr):02X}"


def disassemble_range(
    elf: ElfImage,
    start_vaddr: int,
    count: int,
    symbols: Optional[SymbolTable] = None,
) -> List[str]:
    """Disassemble `count` instructions starting at `start_vaddr` with lui+offset tracking."""
    lines: List[str] = []
    # Track recent LUI loads per GPR: reg -> (pc, upper_val)
    lui_state: Dict[int, Tuple[int, int]] = {}

    for i in range(count):
        pc = start_vaddr + i * 4
        if not elf.contains_vaddr(pc, 4):
            break
        if symbols:
            for s in symbols.exact(pc):
                unit = f" ({s.source_unit})" if s.source_unit else ""
                lines.append(f"\n<{s.name}> @ 0x{pc:08X}{unit}:")
        word = elf.read_u32(pc)
        text = disassemble_instruction(word, pc, symbols=symbols)

        op = (word >> 26) & 0x3F
        rs = (word >> 21) & 0x1F
        rt = (word >> 16) & 0x1F
        rd = (word >> 11) & 0x1F
        imm = word & 0xFFFF

        note = ""
        if op == 0x0F:  # lui
            lui_state[rt] = (pc, imm << 16)
        elif op in (0x09, 0x0D, 0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x28, 0x29, 0x2B, 0x31, 0x39, 0x37, 0x3F):
            if rs in lui_state:
                lui_pc, upper = lui_state[rs]
                if pc - lui_pc <= 0x40:
                    if op == 0x0D:  # ori
                        eff = (upper | imm) & 0xFFFFFFFF
                    else:
                        eff = (upper + sign16(imm)) & 0xFFFFFFFF
                    if 0x00100000 <= eff < 0x02000000:
                        sym_name = symbols.format_addr(eff) if symbols else None
                        val_info = _format_ref_value(elf, eff, is_float=(op in (0x31, 0x39)))
                        parts = [f"-> 0x{eff:08X}"]
                        if sym_name:
                            parts.append(sym_name)
                        if val_info:
                            parts.append(val_info)
                        note = "  ; " + " | ".join(parts)
            # Clear overwritten GPR destination (except stores / float loads)
            if op in (0x09, 0x0D, 0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x37) and rt != rs:
                lui_state.pop(rt, None)
            elif op in (0x09, 0x0D, 0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x37) and rt == rs:
                lui_state.pop(rt, None)
        elif op == 0x00:
            if rd != 0:
                lui_state.pop(rd, None)
        elif op in (0x02, 0x03):
            lui_state.clear()
        elif op in (0x08, 0x0A, 0x0B, 0x0C, 0x0E):
            lui_state.pop(rt, None)

        lines.append(f"  0x{pc:08X}: {word:08X}   {text}{note}")
    return lines


def find_xrefs(
    elf: ElfImage,
    target_vaddr: int,
    symbols: Optional[SymbolTable] = None,
    text_end_vaddr: int = DEFAULT_TEXT_END_VADDR,
) -> List[Tuple[int, str, str]]:
    """
    Scan ELF code region for direct calls (j/jal) and lui+(addiu/ori/load/store)
    references to `target_vaddr`.
    Returns list of (pc, enclosing_symbol, instruction_summary).
    """
    results: List[Tuple[int, str, str]] = []
    end_vaddr = min(text_end_vaddr, elf.vaddr_end)
    start_off = elf.vaddr_to_offset(elf.pt_vaddr)
    word_count = (end_vaddr - elf.pt_vaddr) // 4
    words = struct.unpack_from(f"<{word_count}I", elf.data, start_off)

    # Expected upper halves for signed-offset addressing vs ori addressing
    target_hi_signed = ((target_vaddr + 0x8000) >> 16) & 0xFFFF
    target_lo_signed = target_vaddr & 0xFFFF
    target_hi_ori = (target_vaddr >> 16) & 0xFFFF
    target_lo_ori = target_vaddr & 0xFFFF
    j_target_bits = (target_vaddr >> 2) & 0x03FFFFFF

    # Track recent lui hits matching either hi_signed or hi_ori
    lui_hits: Dict[int, Tuple[int, int]] = {}  # reg -> (idx, upper_val)

    for i, w in enumerate(words):
        op = (w >> 26) & 0x3F
        if op in (0x02, 0x03):
            if (w & 0x03FFFFFF) == j_target_bits:
                pc = elf.pt_vaddr + i * 4
                enc = symbols.format_addr(pc, max_offset=0x20000) if symbols else f"0x{pc:08X}"
                results.append((pc, enc or f"0x{pc:08X}", disassemble_instruction(w, pc, symbols)))
            continue

        if op == 0x0F:  # lui
            imm = w & 0xFFFF
            rt = (w >> 16) & 0x1F
            if imm == target_hi_signed or imm == target_hi_ori:
                lui_hits[rt] = (i, imm << 16)
            else:
                lui_hits.pop(rt, None)
            continue

        if not lui_hits:
            continue

        if op in (0x09, 0x0D, 0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x28, 0x29, 0x2B, 0x31, 0x39, 0x37, 0x3F):
            rs = (w >> 21) & 0x1F
            rt = (w >> 16) & 0x1F
            imm = w & 0xFFFF
            hit = lui_hits.get(rs)
            if hit is not None:
                lui_i, upper = hit
                if i - lui_i <= 24:
                    eff = (upper | imm) if op == 0x0D else ((upper + sign16(imm)) & 0xFFFFFFFF)
                    if eff == target_vaddr:
                        pc = elf.pt_vaddr + i * 4
                        enc = symbols.format_addr(pc, max_offset=0x20000) if symbols else f"0x{pc:08X}"
                        results.append((pc, enc or f"0x{pc:08X}", disassemble_instruction(w, pc, symbols)))
                else:
                    lui_hits.pop(rs, None)
            if op in (0x09, 0x0D, 0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x37):
                lui_hits.pop(rt, None)
        elif op == 0x00:
            rd = (w >> 11) & 0x1F
            if rd in lui_hits:
                lui_hits.pop(rd, None)
        elif op in (0x08, 0x0A, 0x0B, 0x0C, 0x0E):
            rt = (w >> 16) & 0x1F
            lui_hits.pop(rt, None)

    return results


def resolve_target(spec: str, symbols: Optional[SymbolTable]) -> int:
    s = spec.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    if re.fullmatch(r"[0-9A-Fa-f]{6,8}", s) and (not symbols or symbols.get_by_name(s) is None):
        return int(s, 16)
    if symbols:
        sym = symbols.get_by_name(s)
        if sym:
            return sym.vaddr
        matches = symbols.search(f"^{re.escape(s)}$", ignore_case=True)
        if len(matches) == 1:
            return matches[0].vaddr
        matches = symbols.search(re.escape(s), ignore_case=True)
        if len(matches) == 1:
            return matches[0].vaddr
        if len(matches) > 1:
            preview = ", ".join(f"{m.name}@0x{m.vaddr:08X}" for m in matches[:5])
            raise ValueError(f"Ambiguous symbol {spec!r} ({len(matches)} matches: {preview}...)")
    raise ValueError(f"Could not resolve target {spec!r} as hex address or symbol")


def load_symbols_optional(path: Optional[Path]) -> Optional[SymbolTable]:
    p = path or default_symbols_path()
    if p.is_file():
        return SymbolTable.from_file(p)
    return None


def load_elf_from_args(args: argparse.Namespace) -> ElfImage:
    if getattr(args, "elf", None):
        return ElfImage.from_elf(args.elf)
    if getattr(args, "iso", None):
        return ElfImage.from_iso(args.iso, elf_name=args.elf_name)
    raise ValueError("Provide --iso <path.iso> or --elf <SLUS_213.51>")


def cmd_fetch_symbols(args: argparse.Namespace) -> int:
    p = fetch_symbols(out_path=args.out, url=args.url, force=args.force)
    tbl = SymbolTable.from_file(p)
    print(f"Cached {len(tbl):,} symbols -> {p}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    tbl = load_symbols_optional(args.symbols)
    if tbl is None:
        print(
            f"ERROR: Symbol file not found at {args.symbols or default_symbols_path()}. "
            f"Run `python3 tools/elf_disasm.py fetch-symbols` first.",
            file=sys.stderr,
        )
        return 1
    hits = tbl.search(args.pattern, sym_type=args.type)
    print(f"Matched {len(hits)} symbols (showing up to {args.limit}):")
    for s in hits[: args.limit]:
        size_s = f" size=0x{s.size:x}" if s.size is not None else ""
        unit_s = f" [{s.source_unit}]" if s.source_unit else ""
        print(f"  0x{s.vaddr:08X}  {s.sym_type:7s}{size_s:14s}  {s.name}{unit_s}")
    return 0


def cmd_addr(args: argparse.Namespace) -> int:
    tbl = load_symbols_optional(args.symbols)
    val = int(args.value, 16)
    if args.from_offset:
        off = val
        vaddr = elf_offset_to_vaddr(off)
    else:
        vaddr = val
        off = vaddr_to_elf_offset(vaddr)
    print(f"EE vaddr    : 0x{vaddr:08X}")
    print(f"ELF offset  : 0x{off:08X}")
    if tbl:
        near = tbl.nearest(vaddr, max_offset=0x100000)
        if near:
            sym, diff = near
            print(f"Nearest sym : {sym.name} + 0x{diff:x} (type={sym.sym_type}, unit={sym.source_unit})")
    if args.iso or args.elf:
        elf = load_elf_from_args(args)
        if elf.contains_vaddr(vaddr, 4) and (vaddr & 3) == 0:
            u32 = elf.read_u32(vaddr)
            f32 = elf.read_f32(vaddr)
            print(f"u32         : 0x{u32:08X} (s32={elf.read_s32(vaddr)})")
            print(f"f32         : {f32!r}")
            print(f"disasm      : {disassemble_instruction(u32, vaddr, tbl)}")
    return 0


def cmd_disasm(args: argparse.Namespace) -> int:
    tbl = load_symbols_optional(args.symbols)
    elf = load_elf_from_args(args)
    vaddr = resolve_target(args.target, tbl)
    count = args.count
    if count is None:
        if tbl:
            nxt = tbl.next_symbol_addr(vaddr)
            if nxt and 4 <= (nxt - vaddr) <= 0x800:
                count = (nxt - vaddr) // 4
        if count is None:
            count = 64
    lines = disassemble_range(elf, vaddr, count, symbols=tbl)
    print("\n".join(lines))
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    tbl = load_symbols_optional(args.symbols)
    elf = load_elf_from_args(args)
    vaddr = resolve_target(args.target, tbl)
    for i in range(args.words):
        a = vaddr + i * 4
        if not elf.contains_vaddr(a, 4):
            break
        u = elf.read_u32(a)
        f = elf.read_f32(a)
        b = elf.read_bytes(a, 4)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
        sym = tbl.exact(a)[0].name if (tbl and tbl.exact(a)) else ""
        f_str = f"{f:14.6g}" if math.isfinite(f) else "           nan"
        print(f"  0x{a:08X} (off 0x{elf.vaddr_to_offset(a):06X}): 0x{u:08X}  {f_str}  [{asc}]  {sym}")
    return 0


def cmd_xrefs(args: argparse.Namespace) -> int:
    tbl = load_symbols_optional(args.symbols)
    elf = load_elf_from_args(args)
    vaddr = resolve_target(args.target, tbl)
    hits = find_xrefs(elf, vaddr, symbols=tbl)
    sym_label = tbl.format_addr(vaddr) if tbl else None
    print(f"Found {len(hits)} xrefs to 0x{vaddr:08X}" + (f" ({sym_label})" if sym_label else "") + ":")
    for pc, enc, insn in hits:
        print(f"  0x{pc:08X}  [{enc}]  {insn}")
    return 0


def cmd_verify_patches(args: argparse.Namespace) -> int:
    tbl = load_symbols_optional(args.symbols)
    elf = load_elf_from_args(args)
    spec = json.loads(args.patches.read_text(encoding="utf-8"))
    failures = 0
    total = 0
    for p in spec["patches"]:
        print(f"\n[{p['id']}] {p['name']} (default={p.get('enabled_by_default', False)})")
        for w in p["writes"]:
            total += 1
            vaddr = int(w["vaddr"], 16)
            exp = int(w["expected_orig"], 16)
            pat = int(w["patched"], 16)
            actual = elf.read_u32(vaddr)
            ok = actual == exp
            if not ok:
                failures += 1
            status = "OK  " if ok else "FAIL"
            sym = tbl.format_addr(vaddr) if tbl else ""
            orig_d = disassemble_instruction(actual, vaddr, tbl) if vaddr < DEFAULT_TEXT_END_VADDR else f"f32={struct.unpack('<f', struct.pack('<I', actual))[0]:g}"
            new_d = disassemble_instruction(pat, vaddr, tbl) if vaddr < DEFAULT_TEXT_END_VADDR else f"f32={struct.unpack('<f', struct.pack('<I', pat))[0]:g}"
            print(
                f"  {status} 0x{vaddr:08X}: orig=0x{actual:08X} (exp 0x{exp:08X}) -> 0x{pat:08X} "
                f"| {orig_d} -> {new_d}  [{sym}]"
            )
    print(f"\nSummary: {total - failures}/{total} writes verified against {elf.name} (CRC {elf.header['pcsx2_crc']}).")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MIPS R5900 (PS2 EE) disassembler & symbol lookup for SLUS_213.51"
    )
    p.add_argument("--symbols", type=Path, default=None, help="Path to symbols.txt (default: build/symbols_slus21351.txt)")
    p.add_argument("--iso", type=Path, default=None, help="Path to PS2 ISO (opened strictly read-only)")
    p.add_argument("--elf", type=Path, default=None, help="Path to extracted SLUS_213.51 ELF (read-only)")
    p.add_argument("--elf-name", default=DEFAULT_ELF_NAME, help="Boot ELF filename inside ISO")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("fetch-symbols", help="Fetch and cache CC0 SLUS-21351 symbols.txt into build/")
    sp.add_argument("--url", default=DEFAULT_SYMBOLS_URL)
    sp.add_argument("--out", type=Path, default=None)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_fetch_symbols)

    sp = sub.add_parser("search", help="Search symbols by regex")
    sp.add_argument("pattern")
    sp.add_argument("--type", choices=["func", "data", "unknown"], default=None)
    sp.add_argument("--limit", type=int, default=60)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("addr", help="Translate EE vaddr <-> ELF file offset and inspect word")
    sp.add_argument("value", help="Hex address or offset (e.g. 0x00550D58)")
    sp.add_argument("--from-offset", action="store_true", help="Treat input as ELF file offset")
    sp.set_defaults(func=cmd_addr)

    sp = sub.add_parser("disasm", help="Disassemble MIPS R5900 instructions")
    sp.add_argument("target", help="Symbol name or hex vaddr")
    sp.add_argument("--count", "-n", type=int, default=None, help="Number of 32-bit instructions")
    sp.set_defaults(func=cmd_disasm)

    sp = sub.add_parser("dump", help="Dump 32-bit words / floats from ELF static data")
    sp.add_argument("target", help="Symbol name or hex vaddr")
    sp.add_argument("--words", "-n", type=int, default=32)
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("xrefs", help="Find code xrefs (calls & lui+offset refs) to a target vaddr/symbol")
    sp.add_argument("target", help="Symbol name or hex vaddr")
    sp.set_defaults(func=cmd_xrefs)

    sp = sub.add_parser("verify-patches", help="Verify all expected_orig words in an elf_patches JSON")
    sp.add_argument("patches", type=Path)
    sp.set_defaults(func=cmd_verify_patches)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
