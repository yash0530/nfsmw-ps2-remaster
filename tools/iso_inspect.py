#!/usr/bin/env python3
"""
iso_inspect.py — Read-only inspector for PS2 Need for Speed: Most Wanted ISOs.

Strictly opens the source ISO in read-only ('rb') mode. Never modifies disc images.
Reports ISO9660 directory layout, SYSTEM.CNF, main ELF metadata & PCSX2 XOR CRC,
ZDIR.BIN archive index statistics, and optional full SHA-256 verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

SECTOR_SIZE = 2048


def parse_iso9660(f) -> Tuple[str, int, List[dict]]:
    f.seek(16 * SECTOR_SIZE)
    pvd = f.read(SECTOR_SIZE)
    if len(pvd) < SECTOR_SIZE or pvd[1:6] != b"CD001":
        raise ValueError("Not a valid ISO9660 image (missing CD001 PVD at sector 16)")

    vol_id = pvd[40:72].decode("ascii", errors="replace").strip()
    vol_sectors = struct.unpack_from("<I", pvd, 80)[0]
    root_rec = pvd[156:190]
    root_lba = struct.unpack_from("<I", root_rec, 2)[0]
    root_len = struct.unpack_from("<I", root_rec, 10)[0]

    def walk(lba: int, length: int, prefix: str = "") -> List[dict]:
        f.seek(lba * SECTOR_SIZE)
        data = f.read(length)
        pos = 0
        out: List[dict] = []
        while pos < len(data):
            rec_len = data[pos]
            if rec_len == 0:
                next_sec = ((pos // SECTOR_SIZE) + 1) * SECTOR_SIZE
                if next_sec >= len(data):
                    break
                pos = next_sec
                continue
            elba = struct.unpack_from("<I", data, pos + 2)[0]
            elen = struct.unpack_from("<I", data, pos + 10)[0]
            flags = data[pos + 25]
            name_len = data[pos + 32]
            name_raw = data[pos + 33 : pos + 33 + name_len]
            if name_raw not in (b"\x00", b"\x01"):
                name = name_raw.decode("ascii", errors="replace").split(";")[0]
                is_dir = bool(flags & 2)
                entry = {
                    "path": prefix + name,
                    "lba": elba,
                    "size": elen,
                    "is_dir": is_dir,
                    "dir_record_sector": lba + (pos // SECTOR_SIZE),
                    "dir_record_offset": pos % SECTOR_SIZE,
                }
                out.append(entry)
                if is_dir:
                    out.extend(walk(elba, elen, prefix + name + "/"))
            pos += rec_len
        return out

    entries = walk(root_lba, root_len)
    return vol_id, vol_sectors, sorted(entries, key=lambda e: e["lba"])


def compute_pcsx2_elf_crc(elf_bytes: bytes) -> int:
    count = len(elf_bytes) // 4
    words = struct.unpack(f"<{count}I", elf_bytes[: count * 4])
    crc = 0
    for w in words:
        crc ^= w
    return crc


def parse_elf_header(elf_bytes: bytes) -> dict:
    if elf_bytes[:4] != b"\x7fELF":
        raise ValueError("Invalid ELF magic")
    e_entry = struct.unpack_from("<I", elf_bytes, 24)[0]
    e_phoff = struct.unpack_from("<I", elf_bytes, 28)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", elf_bytes, 42)
    segments = []
    for i in range(e_phnum):
        p_type, p_off, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, p_align = (
            struct.unpack_from("<8I", elf_bytes, e_phoff + i * e_phentsize)
        )
        segments.append(
            {
                "index": i,
                "type": p_type,
                "offset": p_off,
                "vaddr": p_vaddr,
                "filesz": p_filesz,
                "memsz": p_memsz,
                "flags": p_flags,
            }
        )
    return {
        "entry": e_entry,
        "size": len(elf_bytes),
        "pcsx2_crc": f"{compute_pcsx2_elf_crc(elf_bytes):08X}",
        "segments": segments,
    }


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def inspect_iso(iso_path: Path, compute_sha256: bool = False) -> dict:
    with iso_path.open("rb") as f:
        vol_id, vol_sectors, entries = parse_iso9660(f)
        by_path: Dict[str, dict] = {
            e["path"].upper(): e for e in entries if not e["is_dir"]
        }

        sys_cnf_text = ""
        boot_elf_name = None
        elf_info = None
        if "SYSTEM.CNF" in by_path:
            e = by_path["SYSTEM.CNF"]
            f.seek(e["lba"] * SECTOR_SIZE)
            sys_cnf_text = f.read(e["size"]).decode("ascii", errors="replace").strip()
            for line in sys_cnf_text.splitlines():
                if "BOOT2" in line.upper():
                    boot_elf_name = (
                        line.split("\\")[-1].split(";")[0].strip().upper()
                    )
                    break

        if boot_elf_name and boot_elf_name in by_path:
            e = by_path[boot_elf_name]
            f.seek(e["lba"] * SECTOR_SIZE)
            elf_bytes = f.read(e["size"])
            elf_info = parse_elf_header(elf_bytes)
            elf_info["name"] = boot_elf_name
            elf_info["lba"] = e["lba"]

        zdir_summary = None
        if "NFS/ZDIR.BIN" in by_path:
            ze = by_path["NFS/ZDIR.BIN"]
            f.seek(ze["lba"] * SECTOR_SIZE)
            zdir_bytes = f.read(ze["size"])
            n = len(zdir_bytes) // 24
            zz_counts: Counter = Counter()
            total_payload = 0
            for i in range(n):
                _, zz_idx, _, _, fsize, _ = struct.unpack_from("<6I", zdir_bytes, i * 24)
                zz_counts[zz_idx] += 1
                total_payload += fsize
            zdir_summary = {
                "lba": ze["lba"],
                "size": ze["size"],
                "entry_count": n,
                "entries_per_zzdata": dict(sorted(zz_counts.items())),
                "total_indexed_bytes": total_payload,
            }

    report = {
        "iso_path": str(iso_path),
        "file_size_bytes": iso_path.stat().st_size,
        "volume_id": vol_id,
        "volume_sectors": vol_sectors,
        "entry_count": len(entries),
        "system_cnf": sys_cnf_text,
        "boot_elf": elf_info,
        "zdir": zdir_summary,
        "entries": entries,
    }
    if compute_sha256:
        report["sha256"] = sha256_file(iso_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only PS2 Need for Speed: Most Wanted ISO inspector"
    )
    parser.add_argument("iso", type=Path, help="Path to PS2 NFSMW .iso (opened read-only)")
    parser.add_argument("--sha256", action="store_true", help="Compute full SHA-256 of ISO")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    if not args.iso.is_file():
        print(f"ERROR: ISO not found: {args.iso}", file=sys.stderr)
        return 1

    report = inspect_iso(args.iso, compute_sha256=args.sha256)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"ISO         : {report['iso_path']}")
    print(f"Size        : {report['file_size_bytes']:,} bytes ({report['volume_sectors']:,} sectors)")
    if "sha256" in report:
        print(f"SHA-256     : {report['sha256']}")
    if report["boot_elf"]:
        be = report["boot_elf"]
        print(
            f"Boot ELF    : {be['name']} (LBA {be['lba']}, {be['size']:,} bytes, "
            f"PCSX2 CRC {be['pcsx2_crc']}, entry 0x{be['entry']:08X})"
        )
    if report["zdir"]:
        zd = report["zdir"]
        print(
            f"ZDIR.BIN    : {zd['entry_count']:,} virtual files across ZZDATA "
            f"{zd['entries_per_zzdata']} ({zd['total_indexed_bytes'] / (1024**3):.2f} GiB indexed)"
        )
    print("\nISO9660 Layout:")
    for e in report["entries"]:
        kind = "DIR " if e["is_dir"] else "FILE"
        print(f"  LBA {e['lba']:8d}  {e['size']:12,d} B  {kind}  {e['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
