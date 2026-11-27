#!/usr/bin/env python3
"""
build_modded_iso.py — Reproducible, strictly non-destructive Modded ISO Builder v0.2
for Need for Speed: Most Wanted - Black Edition (PS2, SLUS-21351).

SAFETY GUARANTEES:
1. Refuses to run if output path resolves to the same file as source ISO.
2. Opens source ISO strictly read-only ('rb') and NEVER writes to it.
3. Verifies every target ELF word against `expected_orig` before patching.
4. Supports APFS instant copy-on-write cloning (`cp -c`) on macOS with safe
   fallback to streaming copy, preserving 100% original Sony ISO9660 + UDF 1.02
   disc structure.
5. Patches virtual files inside `ZZDATA0..3.BIN` + updates `ZDIR.BIN`:
   - Updates ALL duplicated `ZDIR.BIN` rows sharing the same `name_hash`
     (e.g., `GLOBAL\\DYNTEX.BIN` x3, `GLOBAL\\GAMEPLAY.BIN` x2,
     `LANGUAGES\\LANGUAGETEXTURES.BIN` x4).
   - Recomputes and writes the verified EA `bCalculateCrc32` `ZDIR.BIN` checksum
     (or `0xFFFFFFFF` for 0-byte payloads).
   - Supports `ZZDATA3.BIN` append mode when a patched virtual file exceeds its
     in-place sector span: appends sector-aligned data to `ZZDATA3.BIN` (the
     last file on disc), updates `ZDIR.BIN` (`zz_index=3`, `sector_in_zz`,
     `sector_cumulative`, `byte_size`, `checksum`), patches `NFS/ZZDATA3.BIN`'s
     ISO9660 directory record length (both LE & BE u32), and updates the
     ISO9660 Primary Volume Descriptor Volume Space Size (LE & BE u32) whenever
     the appended data extends past the original disc volume boundary.
6. Supports declarative build profiles (`--profile patches/profiles/rockport2026_v0.2.json`)
   declaring ELF patch IDs, optional VLT recipes, and ZZDATA virtual-file overrides.
7. Computes the new modded ELF PCSX2 CRC and emits a companion `.pnach` file
   for runtime-only RAM flags (e.g., Black Edition Widescreen 16:9 flag at `0x00AAFE54`).
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
from typing import Dict, List, Optional, Tuple

from iso_inspect import (
    SECTOR_SIZE,
    compute_pcsx2_elf_crc,
    parse_elf_header,
    parse_iso9660,
    sha256_file,
)
from zdir_tool import bstringhash, compute_zdir_checksum, normalize_virtual_path

MAX_ISO9660_FILE_SIZE = 0xFFFFFFFF  # 4 GiB - 1 byte (32-bit ISO9660 extent length)


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
            dst.chmod(dst.stat().st_mode | 0o200)
            return "apfs-clonefile"

    # Cross-platform fallback: buffered streaming copy
    with src.open("rb") as f_in, dst.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=16 * 1024 * 1024)
    return "stream-copy"


def load_build_profile(profile_path: Path) -> dict:
    """Load and validate a Rockport 2026 declarative build profile JSON."""
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise ValueError(f"Profile must be a JSON object: {profile_path}")
    if "schema_version" not in profile:
        raise ValueError(f"Profile missing 'schema_version': {profile_path}")
    return profile


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

    known_ids = {p["id"] for p in spec["patches"]}
    if selected_ids is not None:
        unknown = [pid for pid in selected_ids if pid not in known_ids]
        if unknown:
            raise ValueError(f"Unknown ELF patch ID(s) requested: {unknown}")

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


def _parse_override_hash(vpath: str) -> Tuple[str, int]:
    norm = normalize_virtual_path(vpath)
    if norm.startswith("0X"):
        return norm, int(norm, 16)
    return norm, bstringhash(norm)


def patch_iso9660_file_size(iso_fp, entry_meta: dict, new_size: int) -> None:
    """Update an ISO9660 directory record's both-endian Data Length (LE @ +10, BE @ +14)."""
    if new_size < 0 or new_size > MAX_ISO9660_FILE_SIZE:
        raise ValueError(f"ISO9660 file size out of 32-bit range: {new_size}")
    rec_off = entry_meta["dir_record_sector"] * SECTOR_SIZE + entry_meta["dir_record_offset"]
    iso_fp.seek(rec_off)
    rec_hdr = iso_fp.read(34)
    if len(rec_hdr) < 34:
        raise ValueError(f"Truncated ISO9660 directory record at offset {rec_off}")
    old_le = struct.unpack_from("<I", rec_hdr, 10)[0]
    old_be = struct.unpack_from(">I", rec_hdr, 14)[0]
    if old_le != old_be:
        raise ValueError(
            f"Corrupt ISO9660 directory record for {entry_meta['path']}: LE size {old_le} != BE size {old_be}"
        )
    iso_fp.seek(rec_off + 10)
    iso_fp.write(struct.pack("<I", new_size))
    iso_fp.write(struct.pack(">I", new_size))


def patch_iso9660_volume_space_size(iso_fp, new_vol_sectors: int) -> List[int]:
    """Update ISO9660 Primary (and any Supplementary) Volume Descriptor Volume Space Size (LE @ +80, BE @ +84)."""
    if new_vol_sectors <= 0 or new_vol_sectors > 0xFFFFFFFF:
        raise ValueError(f"Invalid ISO9660 volume sector count: {new_vol_sectors}")

    updated_sectors: List[int] = []
    for vd_sector in range(16, 32):
        iso_fp.seek(vd_sector * SECTOR_SIZE)
        sec = iso_fp.read(SECTOR_SIZE)
        if len(sec) < SECTOR_SIZE or sec[1:6] != b"CD001":
            break
        vd_type = sec[0]
        if vd_type == 255:  # Volume Descriptor Set Terminator
            break
        if vd_type in (1, 2):  # Primary (1) or Supplementary (2) Volume Descriptor
            old_le = struct.unpack_from("<I", sec, 80)[0]
            old_be = struct.unpack_from(">I", sec, 84)[0]
            if old_le != old_be:
                raise ValueError(
                    f"Corrupt ISO9660 VD at sector {vd_sector}: LE vol_sectors {old_le} != BE {old_be}"
                )
            iso_fp.seek(vd_sector * SECTOR_SIZE + 80)
            iso_fp.write(struct.pack("<I", new_vol_sectors))
            iso_fp.write(struct.pack(">I", new_vol_sectors))
            updated_sectors.append(vd_sector)

    if 16 not in updated_sectors:
        raise ValueError("Missing ISO9660 Primary Volume Descriptor at sector 16")
    return updated_sectors


def _verify_zzdata3_is_last_file(by_path: Dict[str, dict]) -> None:
    """Verify `NFS/ZZDATA3.BIN` is the final file extent on the ISO before growing it."""
    zz3 = by_path["NFS/ZZDATA3.BIN"]
    zz3_lba = zz3["lba"]
    for path, meta in by_path.items():
        if path == "NFS/ZZDATA3.BIN":
            continue
        if meta["lba"] > zz3_lba:
            raise ValueError(
                f"Cannot append to NFS/ZZDATA3.BIN (LBA {zz3_lba}): "
                f"{path} lies after it at LBA {meta['lba']}."
            )
        other_end = meta["lba"] + (meta["size"] + SECTOR_SIZE - 1) // SECTOR_SIZE
        if other_end > zz3_lba:
            raise ValueError(
                f"Cannot append to NFS/ZZDATA3.BIN (LBA {zz3_lba}): "
                f"{path} overlaps its start (ends at LBA {other_end})."
            )


def apply_zz_overrides(
    iso_fp,
    by_path: Dict[str, dict],
    overrides: Dict[str, Path],
    vol_sectors: Optional[int] = None,
    allow_append: bool = True,
    force_append: bool = False,
) -> Tuple[List[dict], dict]:
    """Patch virtual files inside `ZZDATA0..3.BIN` and update `ZDIR.BIN` + ISO9660 metadata.

    Handles:
    - Multi-duplicate `ZDIR.BIN` rows (ALL records matching `name_hash` are updated).
    - EA `bCalculateCrc32` checksum recomputation (`0xFFFFFFFF` for empty payloads).
    - In-place writes when ALL duplicate slots fit without colliding with adjacent
      or shared-sector entries.
    - `ZZDATA3.BIN` append mode when a payload exceeds its in-place slot capacity
      (or `force_append=True`), updating `NFS/ZZDATA3.BIN`'s ISO9660 directory
      record length (LE & BE) and extending the ISO + PVD Volume Space Size (LE & BE)
      if the appended extent grows past `vol_sectors`.

    Returns:
        `(applied_list, iso_layout_update)` where `iso_layout_update` summarizes
        any `ZZDATA3.BIN` / PVD growth performed.
    """
    if vol_sectors is None:
        iso_fp.seek(16 * SECTOR_SIZE + 80)
        vol_sectors = struct.unpack("<I", iso_fp.read(4))[0]

    zz3_meta = by_path["NFS/ZZDATA3.BIN"]
    orig_zz3_size = zz3_meta["size"]
    layout_info = {
        "zzdata3_orig_size": orig_zz3_size,
        "zzdata3_new_size": orig_zz3_size,
        "zzdata3_appended_sectors": 0,
        "vol_sectors_orig": vol_sectors,
        "vol_sectors_new": vol_sectors,
        "pvd_updated": False,
    }

    if not overrides:
        return [], layout_info

    zdir_meta = by_path["NFS/ZDIR.BIN"]
    zz_lbas = [by_path[f"NFS/ZZDATA{i}.BIN"]["lba"] for i in range(4)]
    zz_sizes = [by_path[f"NFS/ZZDATA{i}.BIN"]["size"] for i in range(4)]
    zz_sectors = [(sz + SECTOR_SIZE - 1) // SECTOR_SIZE for sz in zz_sizes]

    # Cumulative sector base for ZZDATA3.BIN (= sum of sector counts of ZZDATA0..2)
    zz3_cum_base = sum(zz_sectors[:3])
    cur_zz3_sectors = zz_sectors[3]

    iso_fp.seek(zdir_meta["lba"] * SECTOR_SIZE)
    zdir_buf = bytearray(iso_fp.read(zdir_meta["size"]))
    count = len(zdir_buf) // 24

    # Parse every ZDIR record and group ALL duplicates by hash!
    records: List[list] = []
    by_hash_all: Dict[int, List[list]] = {}
    nonzero_starts_by_zz: Dict[int, List[int]] = {i: [] for i in range(4)}
    nonzero_slot_counts: Dict[Tuple[int, int], int] = {}

    for i in range(count):
        h1, zidx, sec_rel, sec_cum, fsize, chk = struct.unpack_from("<6I", zdir_buf, i * 24)
        rec = [i, h1, zidx, sec_rel, sec_cum, fsize, chk]
        records.append(rec)
        by_hash_all.setdefault(h1, []).append(rec)
        if fsize > 0:
            nonzero_starts_by_zz[zidx].append(sec_rel)
            slot_key = (zidx, sec_rel)
            nonzero_slot_counts[slot_key] = nonzero_slot_counts.get(slot_key, 0) + 1

    for z in nonzero_starts_by_zz:
        nonzero_starts_by_zz[z] = sorted(set(nonzero_starts_by_zz[z]))

    def slot_capacity_sectors(rec: list) -> int:
        _, _, zidx, sec_rel, _, old_size, _ = rec
        # A 0-byte record often shares its sector offset with a real file -> 0 in-place capacity.
        if old_size == 0:
            return 0
        # If multiple non-zero records ever share the exact same sector, don't overwrite in-place.
        if nonzero_slot_counts.get((zidx, sec_rel), 0) > 1:
            return 0
        starts = nonzero_starts_by_zz[zidx]
        pos_in_zz = starts.index(sec_rel)
        next_start = starts[pos_in_zz + 1] if pos_in_zz + 1 < len(starts) else zz_sectors[zidx]
        return max(0, next_start - sec_rel)

    applied: List[dict] = []
    appended_any = False

    for vpath, local_file in overrides.items():
        norm, h = _parse_override_hash(vpath)
        if h not in by_hash_all:
            raise KeyError(f"ZZ override target not found in ZDIR.BIN: {vpath!r} (0x{h:08X})")

        matching_recs = by_hash_all[h]
        payload = local_file.read_bytes()
        new_size = len(payload)
        needed_sectors = (new_size + SECTOR_SIZE - 1) // SECTOR_SIZE
        new_checksum = compute_zdir_checksum(payload)
        padded = payload + b"\x00" * (needed_sectors * SECTOR_SIZE - new_size)

        # Determine if ALL matching duplicate slots can safely be overwritten in-place
        capacities = [slot_capacity_sectors(r) for r in matching_recs]
        can_write_all_in_place = (
            not force_append
            and (
                new_size == 0
                or all(needed_sectors <= cap for cap in capacities)
            )
        )

        updated_rows_log = []

        if can_write_all_in_place:
            for rec in matching_recs:
                idx, h1, zidx, sec_rel, sec_cum, old_size, old_chk = rec
                if needed_sectors > 0:
                    abs_lba = zz_lbas[zidx] + sec_rel
                    iso_fp.seek(abs_lba * SECTOR_SIZE)
                    iso_fp.write(padded)
                # Update ZDIR row (byte_size & recomputed checksum)
                rec[5] = new_size
                rec[6] = new_checksum
                struct.pack_into("<6I", zdir_buf, idx * 24, h1, zidx, sec_rel, sec_cum, new_size, new_checksum)
                updated_rows_log.append(
                    {
                        "zdir_index": idx,
                        "mode": "in-place",
                        "zzdata": f"ZZDATA{zidx}.BIN",
                        "sector_in_zz": sec_rel,
                        "sector_cumulative": sec_cum,
                        "old_size": old_size,
                        "new_size": new_size,
                        "old_checksum": f"0x{old_chk:08X}",
                        "new_checksum": f"0x{new_checksum:08X}",
                    }
                )
        else:
            if not allow_append:
                min_cap = min(capacities) if capacities else 0
                raise ValueError(
                    f"Override {vpath} requires {needed_sectors} sectors ({new_size:,} B), "
                    f"exceeding in-place slot capacity of {min_cap} sectors "
                    f"({min_cap * SECTOR_SIZE:,} B), and ZZDATA3 append mode is disabled."
                )

            if not appended_any:
                _verify_zzdata3_is_last_file(by_path)
                appended_any = True

            # Append payload ONCE at the current end of ZZDATA3.BIN
            new_sec_in_zz3 = cur_zz3_sectors
            new_sec_cum = zz3_cum_base + new_sec_in_zz3
            new_zz3_sectors = cur_zz3_sectors + needed_sectors
            new_zz3_size = new_zz3_sectors * SECTOR_SIZE
            if new_zz3_size > MAX_ISO9660_FILE_SIZE:
                raise ValueError(
                    f"Appending {vpath} would grow NFS/ZZDATA3.BIN to {new_zz3_size:,} bytes, "
                    f"exceeding the 32-bit ISO9660 file size limit (4 GiB - 1)."
                )

            if needed_sectors > 0:
                abs_lba = zz_lbas[3] + new_sec_in_zz3
                iso_fp.seek(abs_lba * SECTOR_SIZE)
                iso_fp.write(padded)
                cur_zz3_sectors = new_zz3_sectors

            # Repoint ALL matching ZDIR rows (including duplicates) to the appended ZZDATA3 slot!
            for rec in matching_recs:
                idx, h1, old_zidx, old_sec_rel, old_sec_cum, old_size, old_chk = rec
                rec[2] = 3
                rec[3] = new_sec_in_zz3
                rec[4] = new_sec_cum
                rec[5] = new_size
                rec[6] = new_checksum
                struct.pack_into(
                    "<6I",
                    zdir_buf,
                    idx * 24,
                    h1,
                    3,
                    new_sec_in_zz3,
                    new_sec_cum,
                    new_size,
                    new_checksum,
                )
                updated_rows_log.append(
                    {
                        "zdir_index": idx,
                        "mode": "append-zzdata3",
                        "old_zzdata": f"ZZDATA{old_zidx}.BIN",
                        "old_sector_in_zz": old_sec_rel,
                        "zzdata": "ZZDATA3.BIN",
                        "sector_in_zz": new_sec_in_zz3,
                        "sector_cumulative": new_sec_cum,
                        "old_size": old_size,
                        "new_size": new_size,
                        "old_checksum": f"0x{old_chk:08X}",
                        "new_checksum": f"0x{new_checksum:08X}",
                    }
                )

        applied.append(
            {
                "virtual_path": norm,
                "hash": f"0x{h:08X}",
                "duplicate_rows_updated": len(matching_recs),
                "mode": updated_rows_log[0]["mode"],
                "zzdata": updated_rows_log[0]["zzdata"],
                "sector_in_zz": updated_rows_log[0]["sector_in_zz"],
                "old_size": updated_rows_log[0]["old_size"],
                "new_size": new_size,
                "new_checksum": f"0x{new_checksum:08X}",
                "rows": updated_rows_log,
            }
        )

    # Flush updated ZDIR.BIN back into the ISO copy
    iso_fp.seek(zdir_meta["lba"] * SECTOR_SIZE)
    iso_fp.write(zdir_buf)

    # If ZZDATA3.BIN grew, update ISO9660 directory record length (LE & BE) and PVD if needed
    appended_sectors = cur_zz3_sectors - zz_sectors[3]
    if appended_sectors > 0:
        new_zz3_size = cur_zz3_sectors * SECTOR_SIZE
        patch_iso9660_file_size(iso_fp, zz3_meta, new_zz3_size)
        layout_info["zzdata3_new_size"] = new_zz3_size
        layout_info["zzdata3_appended_sectors"] = appended_sectors

        new_zz3_end_lba = zz_lbas[3] + cur_zz3_sectors
        if new_zz3_end_lba > vol_sectors:
            # Extend ISO image to full sector boundary and update PVD Volume Space Size (LE & BE)
            iso_fp.seek(0, os.SEEK_END)
            cur_file_size = iso_fp.tell()
            target_file_size = new_zz3_end_lba * SECTOR_SIZE
            if cur_file_size < target_file_size:
                iso_fp.seek(target_file_size - 1)
                iso_fp.write(b"\x00")
            patch_iso9660_volume_space_size(iso_fp, new_zz3_end_lba)
            layout_info["vol_sectors_new"] = new_zz3_end_lba
            layout_info["pvd_updated"] = True

    return applied, layout_info


def stage_vlt_recipes(
    src_iso: Path,
    vlt_recipes: List[dict],
    repo_root: Path,
    staging_dir: Path,
) -> Dict[str, Path]:
    """Run declarative VLT recipe patches into `staging_dir` (under `build/`) and return ZZ overrides.

    Groups recipes by target `virtual_path` and invokes `tools/vlt_ps2.py patch`.
    When patching `GLOBAL\\ATTRIBUTES.BIN`, also emits and stages a synced
    `GLOBAL\\GLOBALMEMORYFILE.BIN` (via `--memfile-out`) because the PS2 engine
    checks `GLOBALMEMORYFILE.BIN` before `ZDIR.BIN`.
    """
    if not vlt_recipes:
        return {}

    staging_dir.mkdir(parents=True, exist_ok=True)
    vlt_tool = repo_root / "tools" / "vlt_ps2.py"

    recipes_by_vpath: Dict[str, List[Path]] = {}
    for item in vlt_recipes:
        vpath = normalize_virtual_path(item["virtual_path"])
        recipe_rel = Path(item["recipe"])
        recipe_path = recipe_rel if recipe_rel.is_absolute() else (repo_root / recipe_rel)
        is_optional = bool(item.get("optional", False))

        if not recipe_path.is_file():
            if is_optional:
                print(f"      [VLT] Skipping optional missing recipe: {recipe_rel}")
                continue
            raise FileNotFoundError(f"Required VLT recipe not found: {recipe_path}")
        recipes_by_vpath.setdefault(vpath, []).append(recipe_path)

    staged_overrides: Dict[str, Path] = {}
    for vpath, recipe_paths in recipes_by_vpath.items():
        safe_name = vpath.replace("\\", "_")
        out_bin = staging_dir / f"vlt_{safe_name}"
        cmd = [
            sys.executable,
            str(vlt_tool),
            "patch",
            "--iso",
            str(src_iso),
            "--virtual-path",
            vpath,
            "--out",
            str(out_bin),
        ]
        for rp in recipe_paths:
            cmd.extend(["--recipe", str(rp)])

        memfile_out: Optional[Path] = None
        if vpath == r"GLOBAL\ATTRIBUTES.BIN":
            memfile_out = staging_dir / "vlt_GLOBAL_GLOBALMEMORYFILE.BIN"
            cmd.extend(["--memfile-out", str(memfile_out)])

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"VLT patch failed for {vpath} with recipes {[str(p) for p in recipe_paths]}:\n"
                f"{res.stderr or res.stdout}"
            )
        staged_overrides[vpath] = out_bin
        if memfile_out is not None and memfile_out.is_file():
            staged_overrides[r"GLOBAL\GLOBALMEMORYFILE.BIN"] = memfile_out

    return staged_overrides


def stage_lighting_recipes(
    src_iso: Path,
    lighting_recipes: List,
    repo_root: Path,
    staging_dir: Path,
    existing_memfile: Optional[Path] = None,
) -> Dict[str, Path]:
    """Run declarative lighting recipes via `tools/lighting_ps2.py patch` into `staging_dir`.

    When `existing_memfile` is provided (for example from `stage_vlt_recipes()`), passes it via
    `--in-memfile` so `GLOBAL\\GLOBALMEMORYFILE.BIN` preserves Entry [1] (`GLOBAL\\ATTRIBUTES.BIN`)
    while updating Entry [2] (`GLOBAL\\GLOBALB.LZC`).
    """
    if not lighting_recipes:
        return {}

    staging_dir.mkdir(parents=True, exist_ok=True)
    lighting_tool = repo_root / "tools" / "lighting_ps2.py"

    recipe_paths: List[Path] = []
    for item in lighting_recipes:
        if isinstance(item, str):
            recipe_rel = Path(item)
            is_optional = False
        elif isinstance(item, dict):
            recipe_rel = Path(item["recipe"])
            is_optional = bool(item.get("optional", False))
        else:
            raise ValueError(f"Unsupported lighting_recipes item: {item!r}")

        recipe_path = recipe_rel if recipe_rel.is_absolute() else (repo_root / recipe_rel)
        if not recipe_path.is_file():
            if is_optional:
                print(f"      [Lighting] Skipping optional missing recipe: {recipe_rel}")
                continue
            raise FileNotFoundError(f"Required lighting recipe not found: {recipe_path}")
        recipe_paths.append(recipe_path)

    if not recipe_paths:
        return {}

    out_lzc = staging_dir / "lighting_GLOBALB.LZC"
    out_bun = staging_dir / "lighting_GLOBALB.BUN"
    out_memfile = staging_dir / "lighting_GLOBALMEMORYFILE.BIN"

    cmd = [
        sys.executable,
        str(lighting_tool),
        "patch",
        "--iso",
        str(src_iso),
        "--out-globalb",
        str(out_lzc),
        "--out-bun",
        str(out_bun),
        "--memfile-out",
        str(out_memfile),
    ]
    if existing_memfile is not None and existing_memfile.is_file():
        cmd.extend(["--in-memfile", str(existing_memfile)])
    for rp in recipe_paths:
        cmd.extend(["--recipe", str(rp)])

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"Lighting patch failed for recipes {[str(p) for p in recipe_paths]}:\n"
            f"{res.stderr or res.stdout}"
        )

    staged_overrides: Dict[str, Path] = {
        r"GLOBAL\GLOBALB.LZC": out_lzc,
    }
    if out_bun.is_file():
        staged_overrides[r"GLOBAL\GLOBALB.BUN"] = out_bun
    if out_memfile.is_file():
        staged_overrides[r"GLOBAL\GLOBALMEMORYFILE.BIN"] = out_memfile
    return staged_overrides


def emit_companion_pnach(new_crc: str, out_path: Path, applied_ids: List[str]) -> None:
    """Emit a PCSX2 v2 .pnach matching the modded ISO's new ELF CRC for runtime/optional flags."""
    baked_str = ", ".join(applied_ids) if applied_ids else "none (stock ELF preserved)"
    is_ultrawide = "ultrawide_21_9_native" in applied_ids
    aspect = "21:9" if is_ultrawide else "16:9"
    lines = [
        "gametitle=Need for Speed - Most Wanted [Black Edition - Rockport 2026 Mod] [SLUS-21351]",
        f"comment=Companion runtime patch for Modded ISO (ELF CRC: {new_crc}). Static ELF patches baked into ISO: {baked_str}.",
        "",
        f"[Widescreen {aspect} (Runtime Confirm)]",
        f"gsaspectratio={aspect}",
        "author=NFSMW: Rockport 2026",
        f"description=Reinforces Black Edition runtime widescreen flag at 0x00AAFE54 (even when loading an existing 4:3 memory card save; static projection & VideoSettings::Default are baked into the ELF).",
        "patch=1,EE,00AAFE54,word,00000001",
        "",
    ]
    if "disable_bloom" not in applied_ids:
        lines.extend(
            [
                "[Disable Bloom (Optional)]",
                "author=Steelmax / PCSX2 community (MIPS-verified on SLUS_213.51)",
                "description=Optional runtime toggle to disable bloom glare at 4K.",
                "patch=0,EE,00550D5C,word,00000001",
                "",
            ]
        )
    if "disable_all_post_processing" not in applied_ids:
        lines.extend(
            [
                "[Disable All Post-Processing (Zero Double-Lining)]",
                "author=Steelmax / PCSX2 community & NFSMW: Rockport 2026 (MIPS-verified on SLUS_213.51)",
                "description=Skips RenderMWVisualLook (0x00550D28=0) to eliminate the shifted native-res RT9 double-border ghosting across cars, buildings, and trees at 4K/5K. Pair with ShadeBoost 43/55/54/50.",
                "patch=0,EE,00550D28,word,00000000",
                "",
            ]
        )
    if "disable_ai_catchup" not in applied_ids:
        lines.extend(
            [
                "[Disable AI Rubber-Banding Catch-Up (Optional)]",
                "author=NFSMW: Rockport 2026",
                "description=Forces GRaceParameters::GetCatchUp() to return 0 (no AI rubber-banding).",
                "patch=0,EE,002B5958,word,03E00008",
                "patch=0,EE,002B595C,word,0000102D",
                "",
            ]
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build a brand-new modded PS2 NFSMW Black Edition ISO without modifying the original (v0.2)."
    )
    parser.add_argument("--source", type=Path, required=True, help="Path to clean original SLUS-21351 ISO (read-only)")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for the NEW modded ISO (defaults to build/<profile_output> or v0.2 ISO name)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="Optional build profile JSON (e.g., patches/profiles/rockport2026_v0.2.json)",
    )
    parser.add_argument(
        "--patches",
        type=Path,
        default=None,
        help="Path to declarative ELF patch spec JSON (defaults to profile spec or patches/elf_patches.json)",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="Optional explicit list of ELF patch IDs to bake into the ELF (pass empty to skip ELF patching)",
    )
    parser.add_argument(
        "--include-optional",
        action="store_true",
        help="Also bake optional ELF patches (e.g., disable_bloom)",
    )
    parser.add_argument(
        "--zz-override",
        action="append",
        default=[],
        help="Override a virtual file in ZZDATA: VIRTUAL_PATH=/path/to/modded_file (repeatable)",
    )
    parser.add_argument(
        "--no-zzdata3-append",
        action="store_true",
        help="Disable automatic ZZDATA3.BIN append mode (fail if any override exceeds its in-place slot)",
    )
    parser.add_argument(
        "--force-zzdata3-append",
        action="store_true",
        help="Always append ZZDATA overrides to ZZDATA3.BIN instead of writing in-place",
    )
    parser.add_argument(
        "--verify-source-sha256",
        action="store_true",
        help="Compute source ISO SHA-256 before and after build to cryptographically prove zero mutation",
    )
    args = parser.parse_args()

    profile: Optional[dict] = None
    profile_path: Optional[Path] = None
    if args.profile is not None:
        profile_path = args.profile.expanduser()
        if not profile_path.is_absolute() and not profile_path.is_file():
            candidate = repo_root / profile_path
            if candidate.is_file():
                profile_path = candidate
        profile = load_build_profile(profile_path.resolve())

    # Resolve output path
    if args.output is not None:
        dst = args.output.expanduser().resolve()
    elif profile and profile.get("output_filename"):
        dst = (repo_root / "build" / profile["output_filename"]).resolve()
    else:
        dst = (repo_root / "build" / "NFSMW_BlackEdition_Rockport2026_v0.2.iso").resolve()

    src = args.source.expanduser().resolve()

    if not src.is_file():
        print(f"ERROR: Source ISO not found: {src}", file=sys.stderr)
        return 1
    if src == dst:
        print("REFUSED: Output ISO path must NOT match source ISO path!", file=sys.stderr)
        return 2

    # Resolve ELF patch spec, selected IDs, optional flag, and append settings
    if args.patches is not None:
        patches_path = args.patches.expanduser().resolve()
    elif profile and profile.get("elf_patches", {}).get("spec"):
        sp = Path(profile["elf_patches"]["spec"])
        patches_path = sp if sp.is_absolute() else (repo_root / sp).resolve()
    else:
        patches_path = (repo_root / "patches" / "elf_patches.json").resolve()

    selected_ids: Optional[List[str]] = args.only
    if selected_ids is None and profile and "include_ids" in profile.get("elf_patches", {}):
        selected_ids = list(profile["elf_patches"]["include_ids"])

    include_optional = bool(args.include_optional)
    if profile and profile.get("elf_patches", {}).get("include_optional"):
        include_optional = True

    allow_append = not args.no_zzdata3_append
    if profile and "allow_zzdata3_append" in profile and not args.no_zzdata3_append:
        allow_append = bool(profile["allow_zzdata3_append"])

    spec = json.loads(patches_path.read_text(encoding="utf-8"))
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

    profile_label = f" | profile={profile.get('profile_id', profile_path.name)}" if profile else ""
    print(f"[2/6] Verified clean source ISO: {expected_elf} (CRC {orig_crc}, {vol_sectors:,} sectors){profile_label}")

    # Stage optional VLT & lighting recipes from profile (read-only from source ISO into build staging dir)
    overrides: Dict[str, Path] = {}
    if profile and profile.get("vlt_recipes"):
        staging_dir = dst.parent / ".vlt_staging"
        staged = stage_vlt_recipes(src, profile["vlt_recipes"], repo_root, staging_dir)
        overrides.update(staged)
    if profile and profile.get("lighting_recipes"):
        staging_dir = dst.parent / ".lighting_staging"
        staged_light = stage_lighting_recipes(
            src,
            profile["lighting_recipes"],
            repo_root,
            staging_dir,
            existing_memfile=overrides.get(r"GLOBAL\GLOBALMEMORYFILE.BIN"),
        )
        overrides.update(staged_light)

    # Load profile zz_overrides
    if profile and profile.get("zz_overrides"):
        raw_ov = profile["zz_overrides"]
        if isinstance(raw_ov, dict):
            items = [{"virtual_path": k, "file": v} for k, v in raw_ov.items()]
        else:
            items = list(raw_ov)
        for item in items:
            vp = item["virtual_path"]
            fp = Path(item["file"])
            resolved_fp = fp if fp.is_absolute() else (repo_root / fp).resolve()
            if not resolved_fp.is_file():
                if item.get("optional", False):
                    print(f"      [ZZ] Skipping optional missing override: {item['file']}")
                    continue
                raise FileNotFoundError(f"Profile ZZ override file not found: {resolved_fp}")
            overrides[vp] = resolved_fp

    # CLI --zz-override takes highest precedence
    for item in args.zz_override:
        if "=" not in item:
            raise ValueError(f"Invalid --zz-override format (expected VPATH=LOCAL_FILE): {item}")
        vp, lp = item.split("=", 1)
        resolved_lp = Path(lp).expanduser().resolve()
        if not resolved_lp.is_file():
            raise FileNotFoundError(f"ZZ override file not found: {resolved_lp}")
        overrides[vp] = resolved_lp

    print(f"[3/6] Cloning non-destructively -> {dst}")
    copy_mode = clone_iso_non_destructively(src, dst)
    print(f"      Created independent ISO copy via {copy_mode}")

    # Patch ELF inside the NEW ISO copy only
    elf_buf = bytearray(orig_elf)
    applied_elf = apply_elf_patches(elf_buf, spec, selected_ids, include_optional)
    new_crc = f"{compute_pcsx2_elf_crc(bytes(elf_buf)):08X}"

    print(
        f"[4/6] Writing {len(applied_elf)} ELF patch group(s) (CRC {orig_crc} -> {new_crc}) "
        f"and {len(overrides)} ZZDATA override(s)"
    )
    with dst.open("r+b") as f_dst:
        if applied_elf:
            f_dst.seek(elf_meta["lba"] * SECTOR_SIZE)
            f_dst.write(elf_buf)
        applied_zz, layout_info = apply_zz_overrides(
            f_dst,
            by_path,
            overrides,
            vol_sectors=vol_sectors,
            allow_append=allow_append,
            force_append=args.force_zzdata3_append,
        )

    # Re-verify new ISO9660 structure, ELF CRC, ZZDATA3 size, and Volume Space Size
    with dst.open("rb") as f_chk:
        _, chk_sectors, chk_entries = parse_iso9660(f_chk)
        chk_by_path = {e["path"].upper(): e for e in chk_entries if not e["is_dir"]}
        f_chk.seek(chk_by_path[expected_elf]["lba"] * SECTOR_SIZE)
        chk_elf = f_chk.read(chk_by_path[expected_elf]["size"])
        chk_info = parse_elf_header(chk_elf)
        assert chk_info["pcsx2_crc"] == new_crc, "Post-write ELF CRC verification failed!"
        assert chk_sectors == layout_info["vol_sectors_new"], (
            f"Post-write ISO9660 volume sector mismatch: {chk_sectors} != {layout_info['vol_sectors_new']}"
        )
        assert chk_by_path["NFS/ZZDATA3.BIN"]["size"] == layout_info["zzdata3_new_size"], (
            "Post-write NFS/ZZDATA3.BIN size mismatch!"
        )

    companion_pnach = dst.parent / f"SLUS-21351_{new_crc}.pnach"
    emit_companion_pnach(new_crc, companion_pnach, [p["id"] for p in applied_elf])
    print(f"[5/6] Generated companion runtime .pnach: {companion_pnach}")

    manifest = {
        "builder_version": "0.2",
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile_id": profile.get("profile_id") if profile else None,
        "source_iso": src.name,
        "source_elf_crc": orig_crc,
        "source_sha256_before": src_sha_before,
        "output_iso": dst.name,
        "output_elf_crc": new_crc,
        "copy_mode": copy_mode,
        "companion_pnach": companion_pnach.name,
        "iso_layout_update": layout_info,
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
    if layout_info["zzdata3_appended_sectors"] > 0:
        print(
            f"      ZZDATA3.BIN grown     : +{layout_info['zzdata3_appended_sectors']:,} sectors "
            f"({layout_info['zzdata3_orig_size']:,} -> {layout_info['zzdata3_new_size']:,} B, "
            f"PVD updated={layout_info['pvd_updated']})"
        )
    print(f"\nREADY TO PLAY IN PCSX2: {dst}")
    print(f"Modded ELF CRC        : {new_crc} (stock was {orig_crc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
