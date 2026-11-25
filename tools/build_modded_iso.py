#!/usr/bin/env python3
"""
build_modded_iso.py — Reproducible, strictly non-destructive Modded ISO Builder
for Need for Speed: Most Wanted - Black Edition (PS2, SLUS-21351).

SAFETY GUARANTEES:
1. Refuses to run if output path resolves to the same file as source ISO.
2. Opens source ISO strictly read-only ('rb') and NEVER writes to it.
3. Verifies every target ELF word against `expected_orig` before patching.
4. Supports APFS instant copy-on-write cloning (`cp -c`) on macOS with safe
   fallback to streaming copy, preserving 100% original Sony ISO9660 + UDF 1.02
   disc structure.
5. Also supports patching virtual files inside ZZDATA0..3.BIN + updating ZDIR.BIN.
6. Computes the new modded ELF PCSX2 CRC and emits a companion `.pnach` file
   for runtime-only RAM flags (e.g. Widescreen 16:9 flag at 0x00AAFDD4).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from iso_inspect import (
    SECTOR_SIZE,
    compute_pcsx2_elf_crc,
    parse_elf_header,
    parse_iso9660,
    sha256_file,
)
from zdir_tool import bstringhash


def clone_iso_non_destructively(src: Path, dst: Path) -> str:
    """Create an independent copy of `src` at `dst` without touching `src`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    # On macOS APFS, `cp -c` creates an instant copy-on-write clone (separate inode;
    # subsequent writes to dst allocate new blocks and never mutate src).
    if platform.system() == "Darwin":
        res = subprocess.run(
            ["cp", "-c", str(src), str(dst)],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return "apfs-clonefile"

    # Cross-platform fallback: buffered streaming copy
    with src.open("rb") as f_in, dst.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=16 * 1024 * 1024)
    return "stream-copy"


def apply_elf_patches(
    elf_bytes: bytearray,
    spec: dict,
    selected_ids: Optional[List[str]],
    include_optional: bool,
) -> List[dict]:
    target = spec["target"]
    pt_vaddr = int(target["pt_load_vaddr"], 16)
    pt_off = int(target["pt_load_file_offset"], 16)
    applied: List[dict] = []

    for patch in spec["patches"]:
        pid = patch["id"]
        if selected_ids is not None:
            if pid not in selected_ids:
                continue
        else:
            if not patch.get("enabled_by_default", False) and not include_optional:
                continue

        write_log = []
        for w in patch["writes"]:
            vaddr = int(w["vaddr"], 16)
            expected = int(w["expected_orig"], 16)
            patched = int(w["patched"], 16)
            file_off = vaddr - pt_vaddr + pt_off
            if file_off < 0 or file_off + 4 > len(elf_bytes):
                raise ValueError(f"Patch {pid}: vaddr 0x{vaddr:08X} out of ELF range")

            actual = struct.unpack_from("<I", elf_bytes, file_off)[0]
            if actual != expected:
                raise ValueError(
                    f"Safety check failed for patch '{pid}' at vaddr 0x{vaddr:08X}: "
                    f"expected 0x{expected:08X}, found 0x{actual:08X}. Aborting cleanly."
                )
            struct.pack_into("<I", elf_bytes, file_off, patched)
            write_log.append(
                {
                    "vaddr": f"0x{vaddr:08X}",
                    "file_offset": f"0x{file_off:08X}",
                    "orig": f"0x{actual:08X}",
                    "new": f"0x{patched:08X}",
                }
            )
        applied.append({"id": pid, "name": patch["name"], "writes": write_log})
    return applied


def apply_zz_overrides(
    iso_fp,
    by_path: Dict[str, dict],
    overrides: Dict[str, Path],
) -> List[dict]:
    """Patch virtual files inside ZZDATA0..3.BIN in-place (when <= allocated span) and update ZDIR.BIN."""
    if not overrides:
        return []

    zdir_meta = by_path["NFS/ZDIR.BIN"]
    zz_lbas = [by_path[f"NFS/ZZDATA{i}.BIN"]["lba"] for i in range(4)]
    zz_sectors = [by_path[f"NFS/ZZDATA{i}.BIN"]["size"] // SECTOR_SIZE for i in range(4)]

    iso_fp.seek(zdir_meta["lba"] * SECTOR_SIZE)
    zdir_buf = bytearray(iso_fp.read(zdir_meta["size"]))
    count = len(zdir_buf) // 24

    # Map every ZDIR entry and compute maximum safe sector span before next file in same ZZDATA
    records = []
    for i in range(count):
        h1, zidx, sec_rel, sec_cum, fsize, h2 = struct.unpack_from("<6I", zdir_buf, i * 24)
        records.append([i, h1, zidx, sec_rel, sec_cum, fsize, h2])

    by_hash = {r[1]: r for r in records}
    by_zz: Dict[int, List[int]] = {i: [] for i in range(4)}
    for r in records:
        by_zz[r[2]].append(r[3])
    for z in by_zz:
        by_zz[z] = sorted(set(by_zz[z]))

    applied = []
    for vpath, local_file in overrides.items():
        norm = vpath.replace("/", "\\").upper()
        h = int(norm, 16) if norm.startswith("0X") else bstringhash(norm)
        if h not in by_hash:
            raise KeyError(f"ZZ override target not found in ZDIR.BIN: {vpath!r} (0x{h:08X})")
        rec = by_hash[h]
        idx, h1, zidx, sec_rel, sec_cum, old_size, h2 = rec

        payload = local_file.read_bytes()
        needed_sectors = (len(payload) + SECTOR_SIZE - 1) // SECTOR_SIZE

        starts = by_zz[zidx]
        pos_in_zz = starts.index(sec_rel)
        next_start = starts[pos_in_zz + 1] if pos_in_zz + 1 < len(starts) else zz_sectors[zidx]
        max_sectors = next_start - sec_rel
        if needed_sectors > max_sectors:
            raise ValueError(
                f"Override {vpath} requires {needed_sectors} sectors ({len(payload):,} B), "
                f"exceeding in-place slot capacity of {max_sectors} sectors "
                f"({max_sectors * SECTOR_SIZE:,} B). Append-to-ZZDATA3 support coming in Phase 2."
            )

        abs_lba = zz_lbas[zidx] + sec_rel
        padded = payload + b"\x00" * (needed_sectors * SECTOR_SIZE - len(payload))
        iso_fp.seek(abs_lba * SECTOR_SIZE)
        iso_fp.write(padded)

        # Update byte_size in ZDIR.BIN
        struct.pack_into("<6I", zdir_buf, idx * 24, h1, zidx, sec_rel, sec_cum, len(payload), h2)
        applied.append(
            {
                "virtual_path": norm,
                "hash": f"0x{h1:08X}",
                "zzdata": f"ZZDATA{zidx}.BIN",
                "sector_in_zz": sec_rel,
                "old_size": old_size,
                "new_size": len(payload),
            }
        )

    iso_fp.seek(zdir_meta["lba"] * SECTOR_SIZE)
    iso_fp.write(zdir_buf)
    return applied


def emit_companion_pnach(new_crc: str, out_path: Path, applied_ids: List[str]) -> None:
    """Emit a PCSX2 v2 .pnach matching the modded ISO's new ELF CRC for runtime-only flags."""
    lines = [
        "gametitle=Need for Speed - Most Wanted [Black Edition - Rockport 2026 Mod] [SLUS-21351]",
        f"comment=Companion runtime patch for Modded ISO (ELF CRC: {new_crc}). Static ELF patches already baked into ISO: {', '.join(applied_ids)}.",
        "",
        "[Widescreen 16:9 (Runtime Flag)]",
        "gsaspectratio=16:9",
        "author=PeterDelta / NFSMW: Rockport 2026",
        "description=Enables runtime 16:9 widescreen flag at 0x00AAFDD4 (static 0.75f projection ratio is already baked into the modded ELF).",
        "patch=1,EE,00AAFDD4,word,00000001",
        "",
    ]
    if "remove_brume_fog" not in applied_ids:
        lines.extend(
            [
                "[Remove Distance Brume (Optional)]",
                "author=PeterDelta",
                "description=Optional runtime toggle to disable distance haze/brume.",
                "patch=0,EE,005500F0,word,00000000",
                "",
            ]
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build a brand-new modded PS2 NFSMW Black Edition ISO without modifying the original."
    )
    parser.add_argument("--source", type=Path, required=True, help="Path to clean original SLUS-21351 ISO (read-only)")
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "build" / "NFSMW_BlackEdition_Rockport2026_v0.1.iso",
        help="Output path for the NEW modded ISO",
    )
    parser.add_argument(
        "--patches",
        type=Path,
        default=repo_root / "patches" / "elf_patches.json",
        help="Path to declarative ELF patch spec JSON",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        help="Optional explicit list of patch IDs to bake into the ELF",
    )
    parser.add_argument(
        "--include-optional",
        action="store_true",
        help="Also bake optional patches (e.g., remove_brume_fog)",
    )
    parser.add_argument(
        "--zz-override",
        action="append",
        default=[],
        help="Override a virtual file in ZZDATA: VIRTUAL_PATH=/path/to/modded_file (repeatable)",
    )
    parser.add_argument(
        "--verify-source-sha256",
        action="store_true",
        help="Compute source ISO SHA-256 before and after build to cryptographically prove zero mutation",
    )
    args = parser.parse_args()

    src = args.source.expanduser().resolve()
    dst = args.output.expanduser().resolve()

    if not src.is_file():
        print(f"ERROR: Source ISO not found: {src}", file=sys.stderr)
        return 1
    if src == dst:
        print("REFUSED: Output ISO path must NOT match source ISO path!", file=sys.stderr)
        return 2

    spec = json.loads(args.patches.read_text(encoding="utf-8"))
    expected_elf = spec["target"]["elf_name"].upper()
    expected_crc = spec["target"]["elf_pcsx2_crc"].upper()

    src_sha_before = None
    if args.verify_source_sha256:
        print(f"[1/6] Hashing original source ISO (read-only): {src}")
        src_sha_before = sha256_file(src)
        print(f"      Original SHA-256: {src_sha_before}")

    # Validate source ISO read-only before copying
    with src.open("rb") as f:
        _, vol_sectors, entries = parse_iso9660(f)
        by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
        if expected_elf not in by_path:
            print(f"ERROR: Expected boot ELF {expected_elf} not found in source ISO.", file=sys.stderr)
            return 1
        elf_meta = by_path[expected_elf]
        f.seek(elf_meta["lba"] * SECTOR_SIZE)
        orig_elf = f.read(elf_meta["size"])
        orig_crc = f"{compute_pcsx2_elf_crc(orig_elf):08X}"
        if orig_crc != expected_crc:
            print(
                f"ERROR: Source ELF CRC mismatch (expected {expected_crc}, got {orig_crc}). "
                f"Ensure you are using clean NTSC-U Black Edition SLUS-21351.",
                file=sys.stderr,
            )
            return 1

    print(f"[2/6] Verified clean source ISO: {expected_elf} (CRC {orig_crc}, {vol_sectors:,} sectors)")
    print(f"[3/6] Cloning non-destructively -> {dst}")
    copy_mode = clone_iso_non_destructively(src, dst)
    print(f"      Created independent ISO copy via {copy_mode}")

    # Patch ELF inside the NEW ISO copy only
    elf_buf = bytearray(orig_elf)
    applied_elf = apply_elf_patches(elf_buf, spec, args.only, args.include_optional)
    new_crc = f"{compute_pcsx2_elf_crc(bytes(elf_buf)):08X}"

    overrides: Dict[str, Path] = {}
    for item in args.zz_override:
        if "=" not in item:
            raise ValueError(f"Invalid --zz-override format (expected VPATH=LOCAL_FILE): {item}")
        vp, lp = item.split("=", 1)
        overrides[vp] = Path(lp).expanduser().resolve()

    print(f"[4/6] Writing {len(applied_elf)} verified ELF patch groups into new ISO (new CRC: {new_crc})")
    with dst.open("r+b") as f_dst:
        f_dst.seek(elf_meta["lba"] * SECTOR_SIZE)
        f_dst.write(elf_buf)
        applied_zz = apply_zz_overrides(f_dst, by_path, overrides)

    # Re-verify new ISO structure and patched ELF
    with dst.open("rb") as f_chk:
        _, chk_sectors, chk_entries = parse_iso9660(f_chk)
        chk_by_path = {e["path"].upper(): e for e in chk_entries if not e["is_dir"]}
        f_chk.seek(chk_by_path[expected_elf]["lba"] * SECTOR_SIZE)
        chk_elf = f_chk.read(chk_by_path[expected_elf]["size"])
        chk_info = parse_elf_header(chk_elf)
        assert chk_info["pcsx2_crc"] == new_crc, "Post-write ELF CRC verification failed!"
        assert chk_sectors == vol_sectors

    companion_pnach = dst.parent / f"SLUS-21351_{new_crc}.pnach"
    emit_companion_pnach(new_crc, companion_pnach, [p["id"] for p in applied_elf])
    print(f"[5/6] Generated companion runtime .pnach: {companion_pnach}")

    manifest = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_iso": str(src),
        "source_elf_crc": orig_crc,
        "source_sha256_before": src_sha_before,
        "output_iso": str(dst),
        "output_elf_crc": new_crc,
        "copy_mode": copy_mode,
        "companion_pnach": str(companion_pnach),
        "applied_elf_patches": applied_elf,
        "applied_zz_overrides": applied_zz,
    }

    if args.verify_source_sha256:
        src_sha_after = sha256_file(src)
        manifest["source_sha256_after"] = src_sha_after
        if src_sha_after != src_sha_before:
            print("CRITICAL ERROR: Source ISO hash changed!", file=sys.stderr)
            return 3
        print(f"[6/6] Verified original ISO SHA-256 UNCHANGED: {src_sha_after}")
    else:
        print("[6/6] Build complete (original source ISO was opened read-only and never modified).")

    manifest_path = dst.with_suffix(".build-manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"      Build manifest written: {manifest_path}")
    print(f"\nREADY TO PLAY IN PCSX2: {dst}")
    print(f"Modded ELF CRC        : {new_crc} (stock was {orig_crc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
