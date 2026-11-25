#!/usr/bin/env python3
"""
vlt_ps2.py — Pure-Python parser & inspector for PS2 NFSMW VPAK/VLT vaults
(GLOBAL\\ATTRIBUTES.BIN, GLOBAL\\FE_ATTRIB.BIN, GLOBAL\\GAMEPLAY.BIN).

Standard PC tools (NFS-VltEd, OGVI) fail on PS2 VPAK vaults. This clean-room
parser reads PS2 VPAK containers directly from an untouched ISO (read-only)
or extracted .bin file, inspects all VLT chunks (DepN/StrN/DatN/ExpN/PtrN),
and resolves VLT32 (Jenkins lookup2) hashes for known Black Box vault classes.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from zdir_tool import PS2NFSMWArchive


def vlt32_hash(s: str, initval: int = 0xABCDEF00) -> int:
    """EA Black Box VLT32 (Bob Jenkins lookup2 hash, init=0xABCDEF00)."""
    data = s.encode("ascii")
    length = len(data)
    a = b = 0x9E3779B9
    c = initval
    pos = 0

    def mix(a: int, b: int, c: int) -> Tuple[int, int, int]:
        a = (a - b - c) & 0xFFFFFFFF
        a ^= (c >> 13)
        b = (b - c - a) & 0xFFFFFFFF
        b ^= (a << 8) & 0xFFFFFFFF
        c = (c - a - b) & 0xFFFFFFFF
        c ^= (b >> 13)
        a = (a - b - c) & 0xFFFFFFFF
        a ^= (c >> 12)
        b = (b - c - a) & 0xFFFFFFFF
        b ^= (a << 16) & 0xFFFFFFFF
        c = (c - a - b) & 0xFFFFFFFF
        c ^= (b >> 5)
        a = (a - b - c) & 0xFFFFFFFF
        a ^= (c >> 3)
        b = (b - c - a) & 0xFFFFFFFF
        b ^= (a << 10) & 0xFFFFFFFF
        c = (c - a - b) & 0xFFFFFFFF
        c ^= (b >> 15)
        return a, b, c

    rem = length
    while rem >= 12:
        a = (a + struct.unpack_from("<I", data, pos)[0]) & 0xFFFFFFFF
        b = (b + struct.unpack_from("<I", data, pos + 4)[0]) & 0xFFFFFFFF
        c = (c + struct.unpack_from("<I", data, pos + 8)[0]) & 0xFFFFFFFF
        a, b, c = mix(a, b, c)
        pos += 12
        rem -= 12

    c = (c + length) & 0xFFFFFFFF
    if rem >= 11:
        c = (c + (data[pos + 10] << 24)) & 0xFFFFFFFF
    if rem >= 10:
        c = (c + (data[pos + 9] << 16)) & 0xFFFFFFFF
    if rem >= 9:
        c = (c + (data[pos + 8] << 8)) & 0xFFFFFFFF
    if rem >= 8:
        b = (b + (data[pos + 7] << 24)) & 0xFFFFFFFF
    if rem >= 7:
        b = (b + (data[pos + 6] << 16)) & 0xFFFFFFFF
    if rem >= 6:
        b = (b + (data[pos + 5] << 8)) & 0xFFFFFFFF
    if rem >= 5:
        b = (b + data[pos + 4]) & 0xFFFFFFFF
    if rem >= 4:
        a = (a + (data[pos + 3] << 24)) & 0xFFFFFFFF
    if rem >= 3:
        a = (a + (data[pos + 2] << 16)) & 0xFFFFFFFF
    if rem >= 2:
        a = (a + (data[pos + 1] << 8)) & 0xFFFFFFFF
    if rem >= 1:
        a = (a + data[pos]) & 0xFFFFFFFF
    a, b, c = mix(a, b, c)
    return c


KNOWN_VLT_CLASSES = [
    "visualtreatment",
    "timeofdaylighting",
    "suninfo",
    "camera",
    "camerainfo",
    "pvehicle",
    "chassis",
    "engine",
    "transmission",
    "tires",
    "brakes",
    "induction",
    "nos",
    "ecar",
    "aicop",
    "aivehicle",
    "pursuit",
    "pursuitlevels",
    "pursuitescalation",
    "pursuitsupport",
    "trafficpattern",
    "fe_cars",
    "frontend",
    "gameplay",
    "milestones",
    "racers",
    "tracks",
    "weather",
    "emitterdata",
    "emittergroup",
    "effect",
    "world",
    "rigidbodyspecs",
    "collisions",
    "damage",
    "audio",
    "engineaudio",
    "chopper",
]

VLT_CLASS_HASHES: Dict[int, str] = {vlt32_hash(k): k for k in KNOWN_VLT_CLASSES}


def parse_vpak(data: bytes) -> dict:
    if len(data) < 16 or data[:4] != b"VPAK":
        raise ValueError(f"Not a VPAK container (magic={data[:4]!r})")
    _, num_vaults, str_off, str_size = struct.unpack_from("<4sIII", data, 0)
    vaults: List[dict] = []

    for i in range(num_vaults):
        name_off, bin_size, vlt_size, bin_off, vlt_off = struct.unpack_from(
            "<5I", data, 16 + i * 20
        )
        # Read vault name from VPAK string table
        name_pos = str_off + name_off
        nul = data.find(b"\x00", name_pos)
        vname = (
            data[name_pos:nul].decode("ascii", errors="replace")
            if nul != -1
            else f"vault_{i}"
        )

        # Walk little-endian 4CC chunks inside the VLT section
        chunks = []
        pos = vlt_off
        end = vlt_off + vlt_size
        resolved_classes: List[dict] = []

        while pos + 8 <= end:
            raw_tag = data[pos : pos + 4]
            tag = raw_tag[::-1].decode("ascii", errors="replace")  # NpeD -> DepN
            sz = struct.unpack_from("<I", data, pos + 4)[0]
            if sz < 8 or pos + sz > end:
                break
            cinfo = {"tag": tag, "offset": pos, "size": sz}

            if tag == "ExpN":
                # Export table: count(u32), followed by entries (id:u64, type:u32, size:u32, offset:u32)
                exp_count = struct.unpack_from("<I", data, pos + 8)[0]
                cinfo["export_count"] = exp_count
                epos = pos + 12
                for _ in range(exp_count):
                    if epos + 20 > pos + sz:
                        break
                    eid_lo, eid_hi, etype, esize, eoff = struct.unpack_from(
                        "<IIIII", data, epos
                    )
                    if eid_lo in VLT_CLASS_HASHES:
                        resolved_classes.append(
                            {
                                "class": VLT_CLASS_HASHES[eid_lo],
                                "hash": f"0x{eid_lo:08x}",
                                "export_type": f"0x{etype:08x}",
                                "size": esize,
                                "vlt_offset": eoff,
                            }
                        )
                    epos += 20
            elif tag == "DepN":
                dep_count = struct.unpack_from("<I", data, pos + 8)[0]
                cinfo["dependency_count"] = dep_count

            chunks.append(cinfo)
            pos += sz

        # Also scan DatN for known VLT class hashes (32-bit aligned) to report all present schemas
        dat_chunk = next((c for c in chunks if c["tag"] == "DatN"), None)
        found_in_dat = set()
        if dat_chunk:
            dstart = dat_chunk["offset"] + 8
            dend = dat_chunk["offset"] + dat_chunk["size"]
            for off in range(dstart, dend - 3, 4):
                w = struct.unpack_from("<I", data, off)[0]
                if w in VLT_CLASS_HASHES:
                    found_in_dat.add(VLT_CLASS_HASHES[w])

        vaults.append(
            {
                "index": i,
                "name": vname,
                "bin_offset": bin_off,
                "bin_size": bin_size,
                "vlt_offset": vlt_off,
                "vlt_size": vlt_size,
                "chunks": chunks,
                "resolved_exports": resolved_classes,
                "known_classes_detected": sorted(found_in_dat),
            }
        )

    return {
        "magic": "VPAK",
        "vault_count": num_vaults,
        "total_size": len(data),
        "vaults": vaults,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect PS2 NFSMW VPAK/VLT databases (ATTRIBUTES.BIN / GAMEPLAY.BIN)"
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Path to PS2 NFSMW .iso (read-only) OR an extracted .bin VPAK file",
    )
    parser.add_argument(
        "--virtual-path",
        default=r"GLOBAL\ATTRIBUTES.BIN",
        help="Virtual path inside ZDIR.BIN when source is an .iso (default: GLOBAL\\ATTRIBUTES.BIN)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    if args.source.suffix.lower() == ".iso":
        arc = PS2NFSMWArchive(args.source)
        entry = arc.find(args.virtual_path)
        raw = arc.read_entry(entry)
    else:
        raw = args.source.read_bytes()

    parsed = parse_vpak(raw)
    if args.json:
        print(json.dumps(parsed, indent=2))
        return 0

    print(f"VPAK container: {args.source} ({args.virtual_path if args.source.suffix.lower()=='.iso' else 'raw'})")
    print(f"Total size    : {parsed['total_size']:,} bytes | Vaults: {parsed['vault_count']}")
    for v in parsed["vaults"][:10]:
        chunk_str = ", ".join(f"{c['tag']}({c['size']:,}B)" for c in v["chunks"])
        print(f"\n  Vault [{v['index']}] {v['name']!r}")
        print(f"    BIN: off=0x{v['bin_offset']:x} size={v['bin_size']:,} | VLT: off=0x{v['vlt_offset']:x} size={v['vlt_size']:,}")
        print(f"    Chunks: {chunk_str}")
        if v["known_classes_detected"]:
            print(f"    Detected VLT Classes ({len(v['known_classes_detected'])}): {', '.join(v['known_classes_detected'])}")
        if v["resolved_exports"]:
            print(f"    Resolved Exports ({len(v['resolved_exports'])}): " + ", ".join(e["class"] for e in v["resolved_exports"][:20]))
    if len(parsed["vaults"]) > 10:
        print(f"\n  ... ({len(parsed['vaults']) - 10} additional vaults omitted; use --json for full dump)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
