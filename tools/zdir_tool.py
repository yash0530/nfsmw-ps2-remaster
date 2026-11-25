#!/usr/bin/env python3
"""
zdir_tool.py — Pure-Python inspector, verifier & extractor for PS2 NFSMW
`NFS/ZDIR.BIN` + `NFS/ZZDATA0..3.BIN` (Disculator giant-file archive).

Reads directly from an untouched PS2 ISO (strictly read-only 'rb') without
requiring external unpackers.

Verified SLUS-21351 algorithms:
- Path hash: EA Black Box `bStringHash` (`init=0xFFFFFFFF`, `h = (h * 33 + b) & 0xFFFFFFFF`,
  applied to uppercase backslash-delimited paths relative to `NFS\\`).
- Entry checksum (6th u32 field `bFileDirectoryEntry.Checksum`):
  EA `bCalculateCrc32(data, init=0xFFFFFFFF)` — CRC-32/MPEG-2 (poly `0x04C11DB7`,
  MSB-first, non-reflected) with POSIX `cksum`-style LSB-first byte-length suffix
  and final bitwise complement; 0-byte entries store the sentinel `0xFFFFFFFF`.
- Virtual path dictionary: automatically loads `tools/zdir_names.txt` (1,357 verified
  SLUS-21351 paths resolving 1,363 / 1,447 ZDIR rows) plus ELF string extraction.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from iso_inspect import SECTOR_SIZE, parse_iso9660


def _build_crc32_mpeg2_table() -> Tuple[int, ...]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            if c & 0x80000000:
                c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                c = (c << 1) & 0xFFFFFFFF
        table.append(c)
    return tuple(table)


_CRC32_MPEG2_TABLE: Tuple[int, ...] = _build_crc32_mpeg2_table()


def bstringhash(path_str: str) -> int:
    """EA Black Box standard 32-bit string hash (case-sensitive, usually UPPERCASE)."""
    h = 0xFFFFFFFF
    for b in path_str.encode("ascii"):
        h = (h * 33 + b) & 0xFFFFFFFF
    return h


def normalize_virtual_path(path_str: str) -> str:
    """Normalize a virtual archive path to EA Disculator canonical uppercase form."""
    s = path_str.strip().replace("/", "\\")
    if s.upper().startswith("DISCU:"):
        s = s[6:]
    return s.lstrip("\\").upper()


def bcalculate_crc32(data: bytes, prev: int = 0xFFFFFFFF) -> int:
    """EA Black Box `bCalculateCrc32` (bWare `bCrc32.cpp`).

    MSB-first CRC-32 (polynomial 0x04C11DB7, non-reflected) over `data`,
    followed by `len(data)` fed LSB-first while > 0 (POSIX cksum style),
    then bitwise complement (`~crc & 0xFFFFFFFF`).
    """
    crc = prev & 0xFFFFFFFF
    tbl = _CRC32_MPEG2_TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ tbl[(crc >> 24) ^ b]
    n = len(data)
    while n > 0:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ tbl[(crc >> 24) ^ (n & 0xFF)]
        n >>= 8
    return ~crc & 0xFFFFFFFF


def compute_zdir_checksum(data: bytes) -> int:
    """Compute the 6th u32 field (`bFileDirectoryEntry.Checksum`) in PS2 `ZDIR.BIN`.

    - Empty (0-byte) entries store `0xFFFFFFFF` in retail `SLUS-21351` `ZDIR.BIN`.
    - Non-empty entries store `bcalculate_crc32(data, prev=0xFFFFFFFF)`.
    """
    if not data:
        return 0xFFFFFFFF
    return bcalculate_crc32(data, prev=0xFFFFFFFF)


@dataclasses.dataclass
class ZDirEntry:
    index: int
    name_hash: int
    zz_index: int
    sector_in_zz: int
    sector_cumulative: int
    byte_size: int
    secondary_hash: int
    resolved_path: Optional[str] = None
    magic_hex: str = ""

    @property
    def checksum(self) -> int:
        """Alias for the verified `bFileDirectoryEntry.Checksum` (6th u32 field)."""
        return self.secondary_hash

    def to_bytes(self) -> bytes:
        return struct.pack(
            "<6I",
            self.name_hash,
            self.zz_index,
            self.sector_in_zz,
            self.sector_cumulative,
            self.byte_size,
            self.secondary_hash,
        )


DEFAULT_NAMES_FILE = Path(__file__).resolve().parent / "zdir_names.txt"

KNOWN_PATHS = [
    r"GLOBAL\ATTRIBUTES.BIN",
    r"GLOBAL\FE_ATTRIB.BIN",
    r"GLOBAL\GAMEPLAY.BIN",
    r"GLOBAL\GAMEPLAY.LZC",
    r"GLOBAL\GLOBALA.BUN",
    r"GLOBAL\GLOBALB.BUN",
    r"GLOBAL\GLOBALB.LZC",
    r"GLOBAL\GLOBALMEMORYFILE.BIN",
    r"GLOBAL\DYNTEX.BIN",
    r"GLOBAL\INGAMEA.BUN",
    r"GLOBAL\INGAMEB.BUN",
    r"GLOBAL\INGAMEB.LZC",
    r"GLOBAL\INGAMEMEMORYFILE.BIN",
    r"GLOBAL\FRONTENDMEMORYFILE.BIN",
    r"GLOBAL\HUDTEXRACE.BIN",
    r"GLOBAL\HUDTEXDRAG.BIN",
    r"GLOBAL\HUDTEXSPLIT.BIN",
    r"GLOBAL\HUDTEXDRAGSPLIT.BIN",
    r"GLOBAL\HUDTEXTURESPHOTOFINISH.BIN",
    r"GLOBAL\DEMO_MARKETING.BUN",
    r"GLOBAL\WIDESCREEN_GLOBAL.BUN",
    r"GLOBAL\THINSCREEN_GLOBAL.BUN",
    r"CARS\TEXTURES.BIN",
    r"CARS\WHEELS\GEOMETRY.BIN",
    r"CARS\WHEELS\TEXTURES.BIN",
    r"CARS\BRAKES\GEOMETRY.BIN",
    r"CARS\BRAKES\TEXTURES.BIN",
    r"CARS\ROOF\GEOMETRY.BIN",
    r"CARS\SPOILER\GEOMETRY.BIN",
    r"CARS\SPOILER_CARRERA\GEOMETRY.BIN",
    r"CARS\SPOILER_HATCH\GEOMETRY.BIN",
    r"CARS\SPOILER_PORSCHES\GEOMETRY.BIN",
    r"FRONTEND\FRONTA.BUN",
    r"FRONTEND\FRONTB.BUN",
    r"FRONTEND\FRONTB.LZC",
    r"FRONTEND\CODEOVERLAYRELEASE.BIN",
    r"FRONTEND\PLATFORMS\CAREER_SAFEHOUSE.BIN",
    r"FRONTEND\PLATFORMS\CAR_LOT.BIN",
    r"FRONTEND\PLATFORMS\CUSTOMIZATION_SHOP.BIN",
    r"FRONTEND\PLATFORMS\CUSTOMIZATION_SHOP_BACKROOM.BIN",
    r"FRONTEND\PLATFORMS\PLATFORMCRIB.BIN",
    r"TRACKS\L2RA.BUN",
    r"TRACKS\L2RA\TRACKMAPS.BIN",
    r"TRACKS\STREAML2RA.BUN",
    r"TRACKS\L2RA_SUNSET.BUN",
    r"TRACKS\STREAML2RA_SUNSET.BUN",
    r"LANGUAGES\ENGLISH.BIN",
    r"LANGUAGES\FRENCH.BIN",
    r"LANGUAGES\SPANISH.BIN",
    r"LANGUAGES\GERMAN.BIN",
    r"LANGUAGES\ITALIAN.BIN",
    r"LANGUAGES\LANGUAGETEXTURES.BIN",
]


def load_zdir_names(names_file: Optional[Path] = None) -> List[str]:
    """Load verified uppercase relative virtual paths from `tools/zdir_names.txt`."""
    path = names_file or DEFAULT_NAMES_FILE
    seen = set()
    out: List[str] = []
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            norm = normalize_virtual_path(line)
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
    for p in KNOWN_PATHS:
        norm = normalize_virtual_path(p)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


class PS2NFSMWArchive:
    """Read-only interface to `ZDIR.BIN` and `ZZDATA0..3.BIN` inside a PS2 NFSMW ISO."""

    def __init__(self, iso_path: Path, names_file: Optional[Path] = None):
        self.iso_path = Path(iso_path)
        self.names_file = names_file
        self.iso_entries: Dict[str, dict] = {}
        self.zz_lbas: List[int] = []
        self.zz_sizes: List[int] = []
        self.entries: List[ZDirEntry] = []
        self.by_hash: Dict[int, ZDirEntry] = {}
        self.by_hash_all: Dict[int, List[ZDirEntry]] = {}
        self._load()

    def _load(self) -> None:
        with self.iso_path.open("rb") as f:
            _, _, iso_entries = parse_iso9660(f)
            self.iso_entries = {
                e["path"].upper(): e for e in iso_entries if not e["is_dir"]
            }
            for i in range(4):
                key = f"NFS/ZZDATA{i}.BIN"
                if key not in self.iso_entries:
                    raise FileNotFoundError(f"Missing {key} in ISO")
                self.zz_lbas.append(self.iso_entries[key]["lba"])
                self.zz_sizes.append(self.iso_entries[key]["size"])

            zdir_meta = self.iso_entries["NFS/ZDIR.BIN"]
            f.seek(zdir_meta["lba"] * SECTOR_SIZE)
            zdir_raw = f.read(zdir_meta["size"])

            hash_map: Dict[int, str] = {}
            for p in load_zdir_names(self.names_file):
                hash_map[bstringhash(p)] = p

            for elf_key in ("SLUS_213.51", "SLUS_212.67", "SLES_538.57", "SLES_535.57"):
                if elf_key in self.iso_entries:
                    ee = self.iso_entries[elf_key]
                    f.seek(ee["lba"] * SECTOR_SIZE)
                    elf = f.read(ee["size"])
                    cands = set(
                        re.findall(
                            rb"[A-Za-z0-9_\\/\.\-]{4,80}\.(?:BIN|BUN|TPK|ABK|GIN|VP6|VIV|FNG|DAT|CFG|MUS|CSI|MAP|INI|TXT|MPC|LZC|MXB|DYN|LOC|MPF|BIG)",
                            elf,
                            re.I,
                        )
                    )
                    for raw in cands:
                        s = raw.decode("ascii", errors="ignore").replace("/", "\\")
                        for variant in (s.upper(), s, s.lower()):
                            h = bstringhash(variant)
                            hash_map.setdefault(h, variant)
                    break

            count = len(zdir_raw) // 24
            for idx in range(count):
                h1, zz_idx, sec_rel, sec_cum, fsize, h2 = struct.unpack_from(
                    "<6I", zdir_raw, idx * 24
                )
                abs_lba = self.zz_lbas[zz_idx] + sec_rel
                if fsize > 0:
                    f.seek(abs_lba * SECTOR_SIZE)
                    magic = f.read(min(4, fsize)).hex()
                else:
                    magic = ""
                entry = ZDirEntry(
                    index=idx,
                    name_hash=h1,
                    zz_index=zz_idx,
                    sector_in_zz=sec_rel,
                    sector_cumulative=sec_cum,
                    byte_size=fsize,
                    secondary_hash=h2,
                    resolved_path=hash_map.get(h1),
                    magic_hex=magic,
                )
                self.entries.append(entry)
                self.by_hash.setdefault(h1, entry)
                self.by_hash_all.setdefault(h1, []).append(entry)

    @staticmethod
    def _parse_hash(path_or_hash: str) -> int:
        s = path_or_hash.strip()
        if s.lower().startswith("0x"):
            return int(s, 16)
        if re.fullmatch(r"[0-9a-fA-F]{8}", s):
            return int(s, 16)
        return bstringhash(normalize_virtual_path(s))

    def find(self, path_or_hash: str) -> ZDirEntry:
        h = self._parse_hash(path_or_hash)
        if h not in self.by_hash:
            raise KeyError(
                f"Virtual file not found in ZDIR.BIN: {path_or_hash!r} (hash=0x{h:08X})"
            )
        return self.by_hash[h]

    def find_all(self, path_or_hash: str) -> List[ZDirEntry]:
        """Return ALL ZDIR records sharing the target hash (for duplicated disc assets)."""
        h = self._parse_hash(path_or_hash)
        if h not in self.by_hash_all:
            raise KeyError(
                f"Virtual file not found in ZDIR.BIN: {path_or_hash!r} (hash=0x{h:08X})"
            )
        return list(self.by_hash_all[h])

    def read_entry(self, entry: ZDirEntry) -> bytes:
        if entry.byte_size == 0:
            return b""
        abs_lba = self.zz_lbas[entry.zz_index] + entry.sector_in_zz
        with self.iso_path.open("rb") as f:
            f.seek(abs_lba * SECTOR_SIZE)
            return f.read(entry.byte_size)

    def verify_entry_checksum(self, entry: ZDirEntry) -> Tuple[bool, int, int]:
        """Verify an entry's stored ZDIR checksum against its live ZZDATA payload."""
        data = self.read_entry(entry)
        calc = compute_zdir_checksum(data)
        return (calc == entry.secondary_hash, entry.secondary_hash, calc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect, verify, or extract virtual files from PS2 NFSMW ZDIR.BIN / ZZDATA0..3.BIN"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List entries in ZDIR.BIN")
    p_list.add_argument("iso", type=Path)
    p_list.add_argument("--resolved-only", action="store_true", help="Only show entries with resolved paths")
    p_list.add_argument("--unresolved-only", action="store_true", help="Only show entries without resolved paths")
    p_list.add_argument("--duplicates-only", action="store_true", help="Only show hashes that appear more than once")
    p_list.add_argument("--filter", type=str, default="", help="Substring filter on path or magic hex")
    p_list.add_argument("--json", action="store_true")

    p_ext = sub.add_parser("extract", help="Extract a single virtual file by path or 8-hex hash")
    p_ext.add_argument("iso", type=Path)
    p_ext.add_argument("target", type=str, help="e.g. 'GLOBAL\\ATTRIBUTES.BIN' or 0x9775379a")
    p_ext.add_argument("-o", "--output", type=Path, required=True)
    p_ext.add_argument("--decompress", action="store_true", help="Decompress JDLZ/RAWW payload after extracting")

    p_ver = sub.add_parser("verify-checksums", help="Verify ZDIR.BIN checksums against ZZDATA payloads")
    p_ver.add_argument("iso", type=Path)
    p_ver.add_argument("--target", type=str, default=None, help="Verify a single path or 8-hex hash")
    p_ver.add_argument("--max-bytes", type=int, default=1_000_000, help="Skip entries larger than this (0 = check all)")

    args = parser.parse_args()
    arc = PS2NFSMWArchive(args.iso)

    if args.cmd == "list":
        rows = arc.entries
        if args.resolved_only:
            rows = [e for e in rows if e.resolved_path]
        if args.unresolved_only:
            rows = [e for e in rows if not e.resolved_path]
        if args.duplicates_only:
            rows = [e for e in rows if len(arc.by_hash_all[e.name_hash]) > 1]
        if args.filter:
            needle = args.filter.lower()
            rows = [
                e
                for e in rows
                if needle in (e.resolved_path or "").lower()
                or needle in e.magic_hex.lower()
                or needle in f"{e.name_hash:08x}"
            ]
        if args.json:
            print(json.dumps([dataclasses.asdict(e) for e in rows], indent=2))
        else:
            resolved_cnt = sum(1 for e in arc.entries if e.resolved_path)
            unique_total = len(arc.by_hash_all)
            unique_resolved = sum(1 for h, es in arc.by_hash_all.items() if es[0].resolved_path)
            print(
                f"Total ZDIR entries: {len(arc.entries)} ({unique_total} unique hashes) | "
                f"Resolved: {resolved_cnt}/{len(arc.entries)} rows ({unique_resolved}/{unique_total} unique) | "
                f"Shown: {len(rows)}"
            )
            for e in rows:
                name = e.resolved_path or f"<hash:0x{e.name_hash:08x}>"
                dup_cnt = len(arc.by_hash_all[e.name_hash])
                dup_tag = f" [x{dup_cnt}]" if dup_cnt > 1 else ""
                print(
                    f"  [{e.index:4d}] 0x{e.name_hash:08x}  ZZ{e.zz_index} sec={e.sector_in_zz:6d}  "
                    f"{e.byte_size:11,d} B  chk=0x{e.secondary_hash:08x}  magic={e.magic_hex:8s}  {name}{dup_tag}"
                )
        return 0

    if args.cmd == "extract":
        entry = arc.find(args.target)
        data = arc.read_entry(entry)
        if args.decompress:
            from jdlz import lz_decompress

            data = lz_decompress(data)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(data)
        print(
            f"Extracted {entry.resolved_path or hex(entry.name_hash)} -> {args.output} "
            f"({len(data):,} bytes from ZZDATA{entry.zz_index}.BIN sector {entry.sector_in_zz})"
        )
        return 0

    if args.cmd == "verify-checksums":
        if args.target:
            targets = arc.find_all(args.target)
        else:
            targets = [
                e for e in arc.entries
                if args.max_bytes <= 0 or e.byte_size <= args.max_bytes
            ]
        passed = 0
        failed = 0
        for e in targets:
            ok, stored, calc = arc.verify_entry_checksum(e)
            if ok:
                passed += 1
            else:
                failed += 1
                print(
                    f"MISMATCH [{e.index}] {e.resolved_path or hex(e.name_hash)}: "
                    f"stored=0x{stored:08X} calc=0x{calc:08X}",
                    file=sys.stderr,
                )
        print(f"Verified {passed + failed} ZDIR entries: {passed} passed, {failed} failed")
        return 0 if failed == 0 else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
