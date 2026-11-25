#!/usr/bin/env python3
"""
zdir_tool.py — Pure-Python inspector & extractor for PS2 NFSMW ZDIR.BIN + ZZDATA0..3.BIN.

Reads directly from an untouched PS2 ISO (strictly read-only 'rb') without requiring
external unpackers. Resolves virtual paths via EA Black Box bStringHash (init=0xFFFFFFFF,
h = h*33 + byte) using embedded ELF strings + a curated NFSMW filename dictionary.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional

from iso_inspect import SECTOR_SIZE, parse_iso9660


def bstringhash(path_str: str) -> int:
    """EA Black Box standard 32-bit string hash (case-sensitive, usually UPPERCASE)."""
    h = 0xFFFFFFFF
    for b in path_str.encode("ascii"):
        h = (h * 33 + b) & 0xFFFFFFFF
    return h


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


KNOWN_PATHS = [
    r"GLOBAL\ATTRIBUTES.BIN",
    r"GLOBAL\FE_ATTRIB.BIN",
    r"GLOBAL\GAMEPLAY.BIN",
    r"GLOBAL\GLOBALA.BUN",
    r"GLOBAL\GLOBALB.BUN",
    r"GLOBAL\GLOBALMEMORYFILE.BIN",
    r"GLOBAL\DYNTEX.BIN",
    r"GLOBAL\INGAMEA.BUN",
    r"GLOBAL\INGAMEB.BUN",
    r"GLOBAL\HUDTEXRACE.BIN",
    r"GLOBAL\HUDTEXDRAG.BIN",
    r"GLOBAL\HUDTEXSPLIT.BIN",
    r"GLOBAL\HUDTEXDRAGSPLIT.BIN",
    r"GLOBAL\HUDTEXTURESPHOTOFINISH.BIN",
    r"GLOBAL\DEMO_MARKETING.BUN",
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
    r"FRONTEND\PLATFORMS\CAREER_SAFEHOUSE.BIN",
    r"FRONTEND\PLATFORMS\CAR_LOT.BIN",
    r"FRONTEND\PLATFORMS\CUSTOMIZATION_SHOP.BIN",
    r"FRONTEND\PLATFORMS\CUSTOMIZATION_SHOP_BACKROOM.BIN",
    r"FRONTEND\PLATFORMS\PLATFORMCRIB.BIN",
    r"TRACKS\L2RA\TRACKMAPS.BIN",
    r"TRACKS\STREAML2RA.BUN",
    r"LANGUAGES\ENGLISH.BIN",
    r"LANGUAGES\FRENCH.BIN",
    r"LANGUAGES\SPANISH.BIN",
    r"LANGUAGES\GERMAN.BIN",
    r"LANGUAGES\ITALIAN.BIN",
]


class PS2NFSMWArchive:
    """Read-only interface to ZDIR.BIN and ZZDATA0..3.BIN inside a PS2 NFSMW ISO."""

    def __init__(self, iso_path: Path):
        self.iso_path = iso_path
        self.iso_entries: Dict[str, dict] = {}
        self.zz_lbas: List[int] = []
        self.zz_sizes: List[int] = []
        self.entries: List[ZDirEntry] = []
        self.by_hash: Dict[int, ZDirEntry] = {}
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
            for p in KNOWN_PATHS:
                hash_map[bstringhash(p.upper())] = p.upper()
                hash_map[bstringhash(p)] = p

            for elf_key in ("SLUS_213.51", "SLUS_212.67", "SLES_538.57", "SLES_535.57"):
                if elf_key in self.iso_entries:
                    ee = self.iso_entries[elf_key]
                    f.seek(ee["lba"] * SECTOR_SIZE)
                    elf = f.read(ee["size"])
                    cands = set(
                        re.findall(
                            rb"[A-Za-z0-9_\\/\.\-]{4,80}\.(?:BIN|BUN|TPK|ABK|GIN|VP6|VIV|FNG|DAT|CFG|MUS|CSI|MAP|INI|TXT|MPC|LZC)",
                            elf,
                            re.I,
                        )
                    )
                    for raw in cands:
                        s = raw.decode("ascii", errors="ignore").replace("/", "\\")
                        for variant in (s, s.upper(), s.lower()):
                            h = bstringhash(variant)
                            hash_map.setdefault(h, variant)
                    break

            count = len(zdir_raw) // 24
            for idx in range(count):
                h1, zz_idx, sec_rel, sec_cum, fsize, h2 = struct.unpack_from(
                    "<6I", zdir_raw, idx * 24
                )
                abs_lba = self.zz_lbas[zz_idx] + sec_rel
                f.seek(abs_lba * SECTOR_SIZE)
                magic = f.read(4).hex()
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
                self.by_hash[h1] = entry

    def find(self, path_or_hash: str) -> ZDirEntry:
        s = path_or_hash.strip()
        if s.lower().startswith("0x"):
            h = int(s, 16)
        elif re.fullmatch(r"[0-9a-fA-F]{8}", s):
            h = int(s, 16)
        else:
            norm = s.replace("/", "\\").upper()
            h = bstringhash(norm)
        if h not in self.by_hash:
            raise KeyError(
                f"Virtual file not found in ZDIR.BIN: {path_or_hash!r} (hash=0x{h:08X})"
            )
        return self.by_hash[h]

    def read_entry(self, entry: ZDirEntry) -> bytes:
        abs_lba = self.zz_lbas[entry.zz_index] + entry.sector_in_zz
        with self.iso_path.open("rb") as f:
            f.seek(abs_lba * SECTOR_SIZE)
            return f.read(entry.byte_size)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or extract virtual files from PS2 NFSMW ZDIR.BIN / ZZDATA0..3.BIN"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List entries in ZDIR.BIN")
    p_list.add_argument("iso", type=Path)
    p_list.add_argument("--resolved-only", action="store_true", help="Only show entries with resolved paths")
    p_list.add_argument("--filter", type=str, default="", help="Substring filter on path or magic hex")
    p_list.add_argument("--json", action="store_true")

    p_ext = sub.add_parser("extract", help="Extract a single virtual file by path or 8-hex hash")
    p_ext.add_argument("iso", type=Path)
    p_ext.add_argument("target", type=str, help="e.g. 'GLOBAL\\ATTRIBUTES.BIN' or 0x9775379a")
    p_ext.add_argument("-o", "--output", type=Path, required=True)

    args = parser.parse_args()
    arc = PS2NFSMWArchive(args.iso)

    if args.cmd == "list":
        rows = arc.entries
        if args.resolved_only:
            rows = [e for e in rows if e.resolved_path]
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
            print(
                f"Total ZDIR entries: {len(arc.entries)} "
                f"(resolved paths: {resolved_cnt}, shown: {len(rows)})"
            )
            for e in rows:
                name = e.resolved_path or f"<hash:0x{e.name_hash:08x}>"
                print(
                    f"  [{e.index:4d}] 0x{e.name_hash:08x}  ZZ{e.zz_index} sec={e.sector_in_zz:6d}  "
                    f"{e.byte_size:11,d} B  magic={e.magic_hex}  {name}"
                )
        return 0

    if args.cmd == "extract":
        entry = arc.find(args.target)
        data = arc.read_entry(entry)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(data)
        print(
            f"Extracted {entry.resolved_path or hex(entry.name_hash)} -> {args.output} "
            f"({len(data):,} bytes from ZZDATA{entry.zz_index}.BIN sector {entry.sector_in_zz})"
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
