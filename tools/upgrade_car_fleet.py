#!/usr/bin/env python3
"""upgrade_car_fleet.py — Batch PS2 Car Fleet Geometry/Audio Upgrader & 58K-Poly HQ RT Exporter.

Wave 4 pipeline for Need for Speed: Most Wanted - Black Edition (PS2, SLUS-21351):
  1. Batch PS2 High-Poly Car Fleet Upgrade (`--out-dir build/geom_wave4`):
       - Opens the source PS2 ISO strictly read-only ('rb', supporting NFSMW_ISO).
       - Locates iconic CARS\\<CAR>\\GEOMETRY.BIN packs in ZDIR.BIN:
         BMWM3GTRE46, CARRERAGT, GALLARDO, CAMARO, CORVETTE, VIPER, MURCIELAGO, RX7.
       - Exports each car's primary `*_BODY_B` solid to rich Wavefront .obj, applies
         1-level crease-aware PN-triangle subdivision (default --crease-deg 38.0)
         within PS2 EE/VU1 budgets (768 KiB solid cap, 32,767 poly cap, 5 MiB pack cap),
         repacks via `geometry_compiler.replace_solid_in_pack`, and immediately validates
         every repacked .bin via `geometry_inspect.parse_geometry_pack()`.
       - Batch-tunes hero Gnsu v20 engine synths (`bmw_m3_gtr`, `carrera_gt`,
         `gallardo`, `camaro_ss`) into `<out-dir>/audio/` and emits
         `<out-dir>/fleet_manifest.json` mapping `virtual_path -> upgraded_file`.
  2. Ultra-Smooth 58K-Poly Metal 4 RT Hero Export (`--export-hq-rt`):
       - Exports `BMWM3GTRE46_KIT00_BODY_B` (2-level PN subdivision -> ~58K tris),
         interior/glass/light/brake parts, 4x `ENKEI_STYLE01_17_25_B` wheels
         (1-level PN subdivision for round rims/tires), and `SPOILER_STYLE01_B`
         into Y-up right-handed meters (`tires at Y=0`) matching `export_pbr_scene.py`'s
         exact `scene.json` schema for `native_metal_rt/`.
  3. Offline `--synthetic` mode for CI tests without requiring an ISO.

Pure Python 3 standard library. NEVER writes to or modifies any source ISO.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audio_inspect  # noqa: E402
import audio_patcher  # noqa: E402
import export_pbr_scene as eps  # noqa: E402
import geometry_compiler as gc  # noqa: E402
import geometry_inspect as gi  # noqa: E402
import subdivide_car_mesh as scm  # noqa: E402
from jdlz import jdlz_decompress  # noqa: E402
from lighting_ps2 import parse_light_materials, iter_top_level_bchunks  # noqa: E402
from zdir_tool import PS2NFSMWArchive, bstringhash  # noqa: E402

ICONIC_FLEET_CARS: Tuple[str, ...] = (
    "BMWM3GTRE46",
    "CARRERAGT",
    "GALLARDO",
    "CAMARO",
    "CORVETTE",
    "VIPER",
    "MURCIELAGO",
    "RX7",
)

HERO_GNSU_PRESETS: Tuple[str, ...] = (
    "bmw_m3_gtr",
    "carrera_gt",
    "gallardo",
    "camaro_ss",
)

# Shared part packs whose 0x80134001 / 0x00134004 streaming offset tables (or full file
# for BRAKES) are pre-cached inside GLOBAL\GLOBALMEMORYFILE.BIN (entries [3] & [12..17]).
# Replacing these packs in ZZDATA without also syncing their cached headers + full_sz in
# GLOBAL\GLOBALMEMORYFILE.BIN causes bFile::ReadAsync (0x0031B6A4) to serve stale stock
# 0x00134004 offsets, making aftermarket wheels/roof scoops/spoilers vanish in-game.
# Excluded by default in `--all-cars`; only opt-in via `--include-parts` when paired with
# GLOBALMEMORYFILE.BIN header synchronization.
GLOBALMEMORYFILE_CACHED_PART_PACKS: Tuple[str, ...] = (
    "BRAKES",
    "ROOF",
    "SPOILER",
    "SPOILER_CARRERA",
    "SPOILER_HATCH",
    "SPOILER_PORSCHES",
    "WHEELS",
)
SHARED_PART_PACKS: Tuple[str, ...] = GLOBALMEMORYFILE_CACHED_PART_PACKS
SHARED_PART_PACK_SET = frozenset(SHARED_PART_PACKS)

# Iconic wheel brand ordering when greedily upgrading aftermarket rims under 5 MiB cap
WHEEL_BRAND_PRIORITY: Tuple[str, ...] = (
    "BBS",
    "ENKEI",
    "OZ",
    "VOLK",
    "5ZIGEN",
    "RACINGHART",
    "KONIG",
    "ADR",
    "LOWENHART",
    "ROJA",
)
WHEEL_DIAMETER_PRIORITY: Dict[str, int] = {"18": 0, "19": 1, "20": 2, "17": 3}

DEFAULT_OUT_DIR = Path("build/geom_wave4")
DEFAULT_HQ_OUT_DIR = Path("build/flywheel/hq_m3_gtr")
DEFAULT_CREASE_DEG = 38.0
DEFAULT_TARGET_MAX_POLYS = gc.DEFAULT_MAX_POLYS
DEFAULT_MAX_SOLID_BYTES = gc.DEFAULT_MAX_SOLID_BYTES
DEFAULT_MAX_PACK_BYTES = gc.DEFAULT_MAX_PACK_BYTES
DVD5_CAPACITY_BYTES = 4_700_000_000


def _sanitize_display_path(path: Optional[Path]) -> Optional[str]:
    """Avoid embedding personal home directory usernames in manifest metadata."""
    if path is None:
        return None
    try:
        rel = path.expanduser().resolve().relative_to(Path.home().resolve())
        return f"~/{rel.as_posix()}"
    except ValueError:
        return str(path)


def resolve_iso_path(cli_iso: Optional[Path] = None) -> Optional[Path]:
    """Resolve untouched PS2 ISO path from --iso, NFSMW_ISO env var, or default Home path."""
    if cli_iso is not None:
        p = cli_iso.expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"ISO not found: {p}")
        return p
    if "NFSMW_ISO" in os.environ:
        env_val = os.environ["NFSMW_ISO"].strip()
        if not env_val:
            return None
        p = Path(env_val).expanduser()
        return p if p.is_file() else None
    default_iso = eps.DEFAULT_SOURCE_ISO
    return default_iso if default_iso.is_file() else None


def _rel_or_str(path: Path) -> str:
    """Format path relative to REPO_ROOT when inside the repo, otherwise string."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def _extract_solid_group_meta(
    pack_data: bytes, part: gi.SolidPartInfo
) -> Dict[Tuple[int, int], gc.GroupMetadata]:
    """Read `(tx, lm) -> GroupMetadata(strip_flags, poly_group)` from a SolidPack chunk."""
    group_meta: Dict[Tuple[int, int], gc.GroupMetadata] = {}
    if part.chunk_offset + 8 > len(pack_data):
        return group_meta
    _, csz = struct.unpack_from("<II", pack_data, part.chunk_offset)
    p = part.chunk_offset + 8
    end = min(len(pack_data), p + csz)
    while p + 8 <= end:
        scid, scsz = struct.unpack_from("<II", pack_data, p)
        if scid == gc.CHUNK_MESH_CONT:
            mp = p + 8
            mend = min(end, mp + scsz)
            while mp + 8 <= mend:
                mcid, mcsz = struct.unpack_from("<II", pack_data, mp)
                if mcid == gc.CHUNK_STRIP_TABLE:
                    so = gi._skip_11_padding(pack_data, mp + 8, min(mend, mp + 8 + mcsz))
                    st_end = min(mend, mp + 8 + mcsz)
                    while so + 16 <= st_end:
                        fl, _nv, _np, _md, _qo, _nz, _nt, _qs, pg, tx, lm = struct.unpack_from(
                            "<HBBfHbbBBbb", pack_data, so
                        )
                        group_meta.setdefault(
                            (tx, lm), gc.GroupMetadata(strip_flags=fl, poly_group=pg)
                        )
                        so += 16
                mp += 8 + mcsz
        p += 8 + scsz
    return group_meta


def _select_primary_body_b(
    pack_info: gi.GeometryPackInfo, car_name: str
) -> Optional[gi.SolidPartInfo]:
    """Select the primary `<CAR>_KIT00_BODY_B` or `*_BODY_B` solid in a car GEOMETRY.BIN."""
    exact = f"{car_name.upper()}_KIT00_BODY_B"
    for part in pack_info.parts:
        if part.name.upper() == exact:
            return part
    kit00 = [p for p in pack_info.parts if p.name.upper().endswith("_KIT00_BODY_B")]
    if kit00:
        return max(kit00, key=lambda p: p.strip_poly_total)
    any_body_b = [p for p in pack_info.parts if p.name.upper().endswith("_BODY_B")]
    if any_body_b:
        return max(any_body_b, key=lambda p: p.strip_poly_total)
    return None


def _select_primary_body_highest_lod(
    pack_info: gi.GeometryPackInfo, car_name: str
) -> Optional[gi.SolidPartInfo]:
    """Select highest-LOD primary body solid (LOD A if present, else LOD B, else LOD C)."""
    car_u = car_name.upper()
    for lod in ("A", "B", "C"):
        for exact in (f"{car_u}_KIT00_BODY_{lod}", f"{car_u}_KIT00_DAMAGE0_BODY_{lod}"):
            for part in pack_info.parts:
                if part.name.upper() == exact:
                    return part
        kit00 = [
            p
            for p in pack_info.parts
            if f"_KIT00_" in p.name.upper() and p.name.upper().endswith(f"_BODY_{lod}")
        ]
        if kit00:
            return max(kit00, key=lambda p: p.strip_poly_total)
        any_body = [p for p in pack_info.parts if p.name.upper().endswith(f"_BODY_{lod}")]
        if any_body:
            return max(any_body, key=lambda p: p.strip_poly_total)
    return None


def _wheel_priority_key(part: gi.SolidPartInfo) -> Tuple[int, int, int, str]:
    """Priority order for WHEELS\\GEOMETRY.BIN aftermarket rims (STYLE01 across all brands first)."""
    tokens = part.name.upper().split("_")
    brand = tokens[0] if tokens else ""
    brand_rank = (
        WHEEL_BRAND_PRIORITY.index(brand)
        if brand in WHEEL_BRAND_PRIORITY
        else len(WHEEL_BRAND_PRIORITY)
    )
    style_num = 99
    diam_rank = 9
    for tok in tokens[1:]:
        if tok.startswith("STYLE") and tok[5:].isdigit():
            style_num = int(tok[5:])
        elif tok in WHEEL_DIAMETER_PRIORITY:
            diam_rank = WHEEL_DIAMETER_PRIORITY[tok]
    return (style_num, diam_rank, brand_rank, part.name)


def select_target_solids(
    pack_info: gi.GeometryPackInfo,
    car_name: str,
    *,
    include_kits: bool = False,
    prefer_lod_a: bool = False,
) -> List[gi.SolidPartInfo]:
    """Select priority-ordered target solids to PN-subdivide in a GEOMETRY.BIN pack."""
    car_u = car_name.upper()

    if car_u in SHARED_PART_PACK_SET:
        b_parts = [p for p in pack_info.parts if p.name.upper().endswith("_B")]
        if car_u == "WHEELS":
            return sorted(b_parts, key=_wheel_priority_key)
        return sorted(b_parts, key=lambda p: p.index)

    if not include_kits:
        part = (
            _select_primary_body_highest_lod(pack_info, car_u)
            if prefer_lod_a
            else _select_primary_body_b(pack_info, car_u)
        )
        return [part] if part is not None else []

    # Determine highest available body LOD (A -> B -> C)
    best_lod: Optional[str] = None
    for lod in ("A", "B", "C"):
        if any(p.name.upper().endswith(f"_BODY_{lod}") for p in pack_info.parts):
            best_lod = lod
            break
    if best_lod is None:
        return []

    seen: set[int] = set()
    ordered: List[gi.SolidPartInfo] = []

    def _add(parts: Sequence[gi.SolidPartInfo]) -> None:
        for p in parts:
            if p.index not in seen:
                seen.add(p.index)
                ordered.append(p)

    # Priority 1: Primary KIT00 / DAMAGE0 body at highest LOD
    primary = _select_primary_body_highest_lod(pack_info, car_u)
    if primary is not None:
        _add([primary])

    # Priority 2: Widebody kits KIT01..KIT05 & DAMAGE1 bodies at highest LOD
    _add(
        sorted(
            [p for p in pack_info.parts if p.name.upper().endswith(f"_BODY_{best_lod}")],
            key=lambda p: p.name,
        )
    )
    # If highest LOD is A (e.g. COPMIDSIZE), also include LOD B bodies as fallback LOD
    if best_lod == "A":
        _add(
            sorted(
                [p for p in pack_info.parts if p.name.upper().endswith("_BODY_B")],
                key=lambda p: p.name,
            )
        )

    # Priority 3: Stock wheels/tires (*_FRONT_TIRE_*, *_REAR_TIRE_*) at highest available LOD
    tires = [
        p
        for p in pack_info.parts
        if p.name.upper().endswith((f"_FRONT_TIRE_{best_lod}", f"_REAR_TIRE_{best_lod}"))
    ]
    if not tires and best_lod == "A":
        tires = [
            p
            for p in pack_info.parts
            if p.name.upper().endswith(("_FRONT_TIRE_B", "_REAR_TIRE_B"))
        ]
    _add(sorted(tires, key=lambda p: p.name))

    # Priority 4: Primary KIT00 hoods & cop bumpers at highest LOD
    _add(
        sorted(
            [
                p
                for p in pack_info.parts
                if "_KIT00_" in p.name.upper()
                and p.name.upper().endswith(
                    (f"_HOOD_{best_lod}", f"_FRONT_BUMPER_{best_lod}", f"_REAR_BUMPER_{best_lod}")
                )
            ],
            key=lambda p: p.name,
        )
    )

    # Priority 5: Aftermarket STYLE* hoods at highest LOD
    _add(
        sorted(
            [
                p
                for p in pack_info.parts
                if "_STYLE" in p.name.upper() and p.name.upper().endswith(f"_HOOD_{best_lod}")
            ],
            key=lambda p: p.name,
        )
    )

    return ordered


def discover_zdir_car_packs(
    arc: PS2NFSMWArchive,
    *,
    include_parts: bool = False,
) -> List[str]:
    """Discover all CARS\\<CAR>\\GEOMETRY.BIN directories present in ZDIR.BIN."""
    found: List[str] = []
    seen: set[str] = set()
    for e in arc.entries:
        vp = (e.resolved_path or "").upper()
        if vp.startswith("CARS\\") and vp.endswith("\\GEOMETRY.BIN"):
            parts = vp.split("\\")
            if len(parts) == 3:
                car = parts[1]
                if not include_parts and car in SHARED_PART_PACK_SET:
                    continue
                if car not in seen:
                    seen.add(car)
                    found.append(car)
    found.sort()
    return found


def _validate_repacked_pack_multi(
    orig_data: bytes,
    orig_info: gi.GeometryPackInfo,
    repacked_data: bytes,
    expected_by_index: Dict[int, Tuple[int, List[float], List[float]]],
    max_solid_bytes: int,
    max_pack_bytes: int,
) -> Tuple[gi.GeometryPackInfo, Dict[int, gi.DecodedMesh], Dict[int, Dict[str, Any]]]:
    """Validate repacked GEOMETRY.BIN via geometry_inspect + strict PS2 VU1 hardware invariants."""
    if len(repacked_data) > max_pack_bytes:
        raise ValueError(
            f"Repacked pack size {len(repacked_data):,} B exceeds cap {max_pack_bytes:,} B"
        )
    target_indices = sorted(expected_by_index.keys())
    upg_info, upg_meshes = gi.parse_geometry_pack(repacked_data, decode_parts=target_indices)
    if upg_info.solid_count != orig_info.solid_count:
        raise ValueError(
            f"Solid count mismatch after repack: {upg_info.solid_count} != {orig_info.solid_count}"
        )

    vu1_reports: Dict[int, Dict[str, Any]] = {}
    for idx in target_indices:
        expected_tris, orig_vmin, orig_vmax = expected_by_index[idx]
        if idx not in upg_meshes:
            raise ValueError(f"Decoded mesh missing for target solid index {idx}")
        upg_part = upg_info.parts[idx]
        upg_mesh = upg_meshes[idx]
        if upg_part.strip_poly_total != expected_tris:
            raise ValueError(
                f"{upg_part.name}: strip_poly_total mismatch: {upg_part.strip_poly_total} != {expected_tris}"
            )
        if upg_part.header_polys != expected_tris:
            raise ValueError(
                f"{upg_part.name}: header_polys mismatch: {upg_part.header_polys} != {expected_tris}"
            )
        if len(upg_mesh.triangles) != expected_tris:
            raise ValueError(
                f"{upg_part.name}: decoded triangle count mismatch: {len(upg_mesh.triangles)} != {expected_tris}"
            )

        t_off = upg_part.chunk_offset
        _, t_sz = struct.unpack_from("<II", repacked_data, t_off)
        if 8 + t_sz > max_solid_bytes:
            raise ValueError(
                f"{upg_part.name}: SolidPack size {8 + t_sz:,} B exceeds cap {max_solid_bytes:,} B"
            )

        vu1 = gc.verify_solid_vu1_invariants(
            repacked_data, upg_part, expected_min=orig_vmin, expected_max=orig_vmax
        )
        if vu1["bad_tag"] != 0 or vu1["bad_b2"] != 0 or vu1["bad_b3"] != 0 or vu1["bad_q"] != 0:
            raise ValueError(f"{upg_part.name}: VU1 hardware invariant failure: {vu1}")
        if vu1["vu1_strip_overshoot"] > 1e-4:
            raise ValueError(
                f"{upg_part.name}: VU1 strip AABB overshoot {vu1['vu1_strip_overshoot']:.6f}"
            )
        max_aabb_tol = 0.0025 if abs(vu1["scale"] - (1.0 / 256.0)) <= 1e-7 else 0.0005
        if vu1["vu1_aabb_err"] > max_aabb_tol:
            raise ValueError(
                f"{upg_part.name}: VU1 AABB error {vu1['vu1_aabb_err']:.6f} > {max_aabb_tol}"
            )
        vu1_reports[idx] = vu1

    target_set = set(target_indices)
    for idx in range(orig_info.solid_count):
        if idx in target_set:
            continue
        o_off = orig_info.parts[idx].chunk_offset
        _, o_sz = struct.unpack_from("<II", orig_data, o_off)
        n_off = upg_info.parts[idx].chunk_offset
        _, n_sz = struct.unpack_from("<II", repacked_data, n_off)
        if orig_data[o_off : o_off + 8 + o_sz] != repacked_data[n_off : n_off + 8 + n_sz]:
            raise ValueError(
                f"Untouched sibling solid index {idx} ({orig_info.parts[idx].name}) changed"
            )

    return upg_info, upg_meshes, vu1_reports


def _validate_repacked_pack(
    orig_data: bytes,
    orig_info: gi.GeometryPackInfo,
    repacked_data: bytes,
    target_idx: int,
    expected_tris: int,
    max_solid_bytes: int,
    max_pack_bytes: int,
) -> Tuple[gi.GeometryPackInfo, gi.DecodedMesh]:
    """Validate single-solid repacked GEOMETRY.BIN (backwards-compatible wrapper)."""
    orig_part = orig_info.parts[target_idx]
    upg_info, upg_meshes, _ = _validate_repacked_pack_multi(
        orig_data,
        orig_info,
        repacked_data,
        {target_idx: (expected_tris, list(orig_part.aabb_min), list(orig_part.aabb_max))},
        max_solid_bytes=max_solid_bytes,
        max_pack_bytes=max_pack_bytes,
    )
    return upg_info, upg_meshes[target_idx]


def upgrade_single_car_pack(
    pack_data: bytes,
    car_name: str,
    virtual_path: str,
    out_dir: Path,
    *,
    crease_deg: float = DEFAULT_CREASE_DEG,
    max_polys: int = DEFAULT_TARGET_MAX_POLYS,
    max_solid_bytes: int = DEFAULT_MAX_SOLID_BYTES,
    max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES,
    include_kits: bool = False,
    prefer_lod_a: bool = False,
    auto_coarse_scale: bool = True,
    write_obj: bool = False,
) -> Dict[str, Any]:
    """Subdivide, repack, and VU1-verify one car/part GEOMETRY.BIN within PS2 EE/VU1 budgets."""
    orig_info, _ = gi.parse_geometry_pack(pack_data, path=virtual_path)
    candidates = select_target_solids(
        orig_info,
        car_name,
        include_kits=include_kits,
        prefer_lod_a=prefer_lod_a,
    )
    if not candidates:
        return {
            "car": car_name,
            "virtual_path": virtual_path,
            "status": "skipped_no_body_b",
            "solid_count": orig_info.solid_count,
        }

    # Filter candidates that can legally be 4x subdivided within 16-bit poly cap
    eligible = [
        p for p in candidates if p.strip_poly_total > 0 and p.strip_poly_total * 4 <= max_polys
    ]
    if not eligible:
        p0 = candidates[0]
        return {
            "car": car_name,
            "virtual_path": virtual_path,
            "solid_name": p0.name,
            "status": "skipped_poly_budget",
            "orig_triangles": p0.strip_poly_total,
            "projected_triangles": p0.strip_poly_total * 4,
            "max_polys": max_polys,
        }

    # Decode eligible solids in one pass
    _, decoded_map = gi.parse_geometry_pack(pack_data, decode_parts=[p.index for p in eligible])

    # Reserve alignment overhead safety margin (64B per solid + 128B tail + header safety)
    align_margin = 4096
    projected_pack_bytes = len(pack_data)
    replacements: Dict[str, gc.CompiledMesh] = {}
    solid_plan: List[Dict[str, Any]] = []
    skipped_budget_solids: List[str] = []
    expected_by_index: Dict[int, Tuple[int, List[float], List[float]]] = {}

    obj_dir = out_dir / "obj" if write_obj else None
    if obj_dir is not None:
        obj_dir.mkdir(parents=True, exist_ok=True)

    for part in eligible:
        orig_mesh = decoded_map.get(part.index)
        if orig_mesh is None or not orig_mesh.triangles:
            continue
        group_meta = _extract_solid_group_meta(pack_data, part)
        valid_orig_tris = [
            t for t in orig_mesh.triangles if t[0] != t[1] and t[1] != t[2] and t[0] != t[2]
        ]
        src_compiled = gc.CompiledMesh(
            vertices=list(orig_mesh.vertices),
            normals=list(orig_mesh.normals),
            uvs=list(orig_mesh.uvs),
            triangles=valid_orig_tris,
            group_meta=group_meta,
        )
        orig_tris = len(src_compiled.triangles)
        if orig_tris <= 0 or orig_tris * 4 > max_polys:
            skipped_budget_solids.append(part.name)
            continue

        orig_vmin = [min(v[ax] for v in orig_mesh.vertices) for ax in range(3)]
        orig_vmax = [max(v[ax] for v in orig_mesh.vertices) for ax in range(3)]

        sub_mesh, sub_stats = scm.subdivide_compiled_mesh(
            src_compiled,
            crease_deg=crease_deg,
            mode="pn",
            levels=1,
            preserve_aabb=True,
        )
        valid_sub_tris = [
            t for t in sub_mesh.triangles if t[0] != t[1] and t[1] != t[2] and t[0] != t[2]
        ]
        if len(valid_sub_tris) != len(sub_mesh.triangles):
            sub_mesh = gc.CompiledMesh(
                vertices=sub_mesh.vertices,
                normals=sub_mesh.normals,
                uvs=sub_mesh.uvs,
                triangles=valid_sub_tris,
                group_meta=sub_mesh.group_meta,
            )
        actual_sub_tris = len(sub_mesh.triangles)
        if actual_sub_tris <= 0 or actual_sub_tris > max_polys:
            skipped_budget_solids.append(part.name)
            continue

        _, orig_solid_sz = struct.unpack_from("<II", pack_data, part.chunk_offset)
        raw_orig_solid = pack_data[part.chunk_offset : part.chunk_offset + 8 + orig_solid_sz]
        try:
            compiled_solid_bytes, c_stats = gc.build_solid_pack_from_template(
                raw_orig_solid,
                sub_mesh,
                max_solid_bytes=max_solid_bytes,
                auto_coarse_scale=auto_coarse_scale,
            )
        except ValueError:
            skipped_budget_solids.append(part.name)
            continue

        delta_bytes = len(compiled_solid_bytes) - len(raw_orig_solid)
        if projected_pack_bytes + delta_bytes + align_margin > max_pack_bytes:
            skipped_budget_solids.append(part.name)
            # For homogeneous large packs (like WHEELS), stop early once headroom < 24 KiB
            if car_name.upper() == "WHEELS" and (max_pack_bytes - projected_pack_bytes - align_margin) < 24576:
                break
            continue

        projected_pack_bytes += delta_bytes
        replacements[part.name] = sub_mesh
        expected_by_index[part.index] = (c_stats.strip_polys, orig_vmin, orig_vmax)

        rec: Dict[str, Any] = {
            "solid_name": part.name,
            "solid_index": part.index,
            "orig_triangles": orig_tris,
            "upgraded_triangles": c_stats.strip_polys,
            "upgraded_vertices": sub_stats.output_vertices,
            "smooth_edges": sub_stats.smooth_edges,
            "sharp_edges": sub_stats.sharp_edges,
            "strip_count": c_stats.strip_count,
            "vif_bytes": c_stats.vif_bytes,
            "orig_solid_bytes": len(raw_orig_solid),
            "solid_bytes": c_stats.solid_bytes,
        }
        if obj_dir is not None:
            orig_obj_path = obj_dir / f"{car_name}_{part.name}_orig.obj"
            sub_obj_path = obj_dir / f"{car_name}_{part.name}_l1.obj"
            gc.export_rich_obj(orig_obj_path, part.name, orig_mesh, group_meta=group_meta)
            gc.export_rich_obj(sub_obj_path, part.name, _compiled_to_decoded(sub_mesh), group_meta=group_meta)
            rec["orig_obj"] = _rel_or_str(orig_obj_path)
            rec["sub_obj"] = _rel_or_str(sub_obj_path)
        solid_plan.append(rec)

    if not replacements:
        p0 = eligible[0]
        return {
            "car": car_name,
            "virtual_path": virtual_path,
            "solid_name": p0.name,
            "status": "skipped_pack_budget",
            "orig_triangles": p0.strip_poly_total,
            "orig_pack_bytes": len(pack_data),
            "max_pack_bytes": max_pack_bytes,
            "skipped_solids": skipped_budget_solids,
        }

    repacked_bytes, _stats_map = gc.replace_solids_in_pack(
        pack_data,
        replacements,
        max_solid_bytes=max_solid_bytes,
        max_pack_bytes=max_pack_bytes,
        auto_coarse_scale=auto_coarse_scale,
    )
    out_bin_path = out_dir / f"{car_name}_GEOMETRY_UPGRADED.bin"
    out_bin_path.parent.mkdir(parents=True, exist_ok=True)
    out_bin_path.write_bytes(repacked_bytes)

    # Immediately validate written repacked .bin via geometry_inspect + strict VU1 verifier
    upg_info, _upg_meshes, vu1_map = _validate_repacked_pack_multi(
        pack_data,
        orig_info,
        out_bin_path.read_bytes(),
        expected_by_index,
        max_solid_bytes=max_solid_bytes,
        max_pack_bytes=max_pack_bytes,
    )

    for rec in solid_plan:
        rec["vu1"] = vu1_map[rec["solid_index"]]

    primary_rec = solid_plan[0]
    total_orig_tris = sum(r["orig_triangles"] for r in solid_plan)
    total_upg_tris = sum(r["upgraded_triangles"] for r in solid_plan)
    total_strips = sum(r["vu1"]["strip_count"] for r in solid_plan)
    vu1_summary = {
        "total_strips": total_strips,
        "bad_tag": sum(r["vu1"]["bad_tag"] for r in solid_plan),
        "bad_b2": sum(r["vu1"]["bad_b2"] for r in solid_plan),
        "bad_b3": sum(r["vu1"]["bad_b3"] for r in solid_plan),
        "bad_q": sum(r["vu1"]["bad_q"] for r in solid_plan),
        "max_vu1_aabb_err": round(max(r["vu1"]["vu1_aabb_err"] for r in solid_plan), 6),
        "max_vu1_strip_overshoot": round(max(r["vu1"]["vu1_strip_overshoot"] for r in solid_plan), 6),
    }

    report: Dict[str, Any] = {
        "car": car_name,
        "virtual_path": virtual_path,
        "solid_name": primary_rec["solid_name"],
        "solid_index": primary_rec["solid_index"],
        "status": "upgraded",
        "parse_errors": 0,
        "crease_deg": crease_deg,
        "solids_upgraded": len(solid_plan),
        "solids_skipped_budget": len(skipped_budget_solids),
        "orig_triangles": total_orig_tris,
        "upgraded_triangles": total_upg_tris,
        "upgraded_vertices": sum(r["upgraded_vertices"] for r in solid_plan),
        "smooth_edges": sum(r["smooth_edges"] for r in solid_plan),
        "sharp_edges": sum(r["sharp_edges"] for r in solid_plan),
        "strip_count": sum(r["strip_count"] for r in solid_plan),
        "vif_bytes": sum(r["vif_bytes"] for r in solid_plan),
        "solid_bytes": max(r["solid_bytes"] for r in solid_plan),
        "orig_pack_bytes": len(pack_data),
        "upgraded_pack_bytes": len(repacked_bytes),
        "pack_delta_bytes": len(repacked_bytes) - len(pack_data),
        "solid_count": upg_info.solid_count,
        "upgraded_file": _rel_or_str(out_bin_path),
        "vu1_verification": vu1_summary,
        "solids": solid_plan,
    }
    if "orig_obj" in primary_rec:
        report["orig_obj"] = primary_rec["orig_obj"]
        report["sub_obj"] = primary_rec["sub_obj"]
    return report


def _tune_hero_gnsu_from_iso(
    arc: PS2NFSMWArchive,
    out_audio_dir: Path,
    presets: Sequence[str] = HERO_GNSU_PRESETS,
) -> List[Dict[str, Any]]:
    """Batch-tune hero Gnsu v20 engine synths from read-only ISO and verify each output."""
    out_audio_dir.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    entry_map = {
        (e.resolved_path or "").upper(): e for e in arc.entries if e.resolved_path
    }
    for preset_name in presets:
        cfg = audio_patcher.HERO_CAR_GNSU_PRESETS[preset_name]
        vpath = cfg["virtual_path"]
        entry = entry_map.get(vpath.upper())
        if entry is None:
            results.append(
                {
                    "preset": preset_name,
                    "car_name": cfg["car_name"],
                    "virtual_path": vpath,
                    "status": "missing_in_zdir",
                }
            )
            continue
        raw_gin = arc.read_entry(entry)
        res = audio_patcher.patch_gnsu_bytes(
            raw_gin, preset=preset_name, path_label=vpath
        )
        assert res.patched_bytes is not None
        out_gin = out_audio_dir / f"{preset_name}_patched.GIN"
        out_gin.write_bytes(res.patched_bytes)
        inspected = audio_inspect.inspect_standalone_file(out_gin)
        synth = inspected["gnsu_synths"][0].to_dict()
        results.append(
            {
                "preset": preset_name,
                "car_name": cfg["car_name"],
                "virtual_path": vpath,
                "status": "tuned",
                "parse_errors": 0,
                "upgraded_file": _rel_or_str(out_gin),
                "diff": res.diff,
                "verified_synth": synth,
            }
        )
    return results


def _build_fleet_manifest(
    *,
    mode: str,
    source_iso: Optional[Path],
    crease_deg: float,
    cars: List[Dict[str, Any]],
    audio: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble fleet_manifest.json mapping virtual_path -> upgraded_file for ISO injection."""
    overrides: Dict[str, str] = {}
    zz_overrides: List[Dict[str, Any]] = []
    for c in cars:
        if c.get("status") == "upgraded":
            vp = c["virtual_path"]
            uf = c["upgraded_file"]
            overrides[vp] = uf
            zz_overrides.append(
                {
                    "virtual_path": vp,
                    "file": uf,
                    "kind": "geometry_pack",
                    "car": c["car"],
                    "orig_triangles": c["orig_triangles"],
                    "upgraded_triangles": c["upgraded_triangles"],
                }
            )
    for a in audio:
        if a.get("status") == "tuned":
            vp = a["virtual_path"]
            uf = a["upgraded_file"]
            overrides[vp] = uf
            zz_overrides.append(
                {
                    "virtual_path": vp,
                    "file": uf,
                    "kind": "gnsu_synth",
                    "preset": a["preset"],
                }
            )

    upgraded_cars = [c for c in cars if c.get("status") == "upgraded"]
    tuned_audio = [a for a in audio if a.get("status") == "tuned"]
    total_solids_upgraded = sum(c.get("solids_upgraded", 1) for c in upgraded_cars)
    total_orig_pack_bytes = sum(c.get("orig_pack_bytes", 0) for c in upgraded_cars)
    total_upg_pack_bytes = sum(c.get("upgraded_pack_bytes", 0) for c in upgraded_cars)
    total_pack_delta_bytes = total_upg_pack_bytes - total_orig_pack_bytes
    zzdata3_append_bytes = sum(
        ((c.get("upgraded_pack_bytes", 0) + 2047) // 2048) * 2048
        for c in upgraded_cars
        if c.get("upgraded_pack_bytes", 0) > c.get("orig_pack_bytes", 0)
    )
    vu1_list = [c["vu1_verification"] for c in upgraded_cars if "vu1_verification" in c]
    vu1_summary = {
        "total_strips": sum(v["total_strips"] for v in vu1_list),
        "bad_tag": sum(v["bad_tag"] for v in vu1_list),
        "bad_b2": sum(v["bad_b2"] for v in vu1_list),
        "bad_b3": sum(v["bad_b3"] for v in vu1_list),
        "bad_q": sum(v["bad_q"] for v in vu1_list),
        "max_vu1_aabb_err": round(max((v["max_vu1_aabb_err"] for v in vu1_list), default=0.0), 6),
        "max_vu1_strip_overshoot": round(
            max((v["max_vu1_strip_overshoot"] for v in vu1_list), default=0.0), 6
        ),
        "parse_errors": sum(c.get("parse_errors", 0) for c in upgraded_cars),
    }
    iso_size_bytes = source_iso.stat().st_size if (source_iso and source_iso.is_file()) else None
    dvd5_headroom_bytes = (
        (DVD5_CAPACITY_BYTES - iso_size_bytes) if iso_size_bytes is not None else None
    )

    return {
        "schema_version": 1,
        "mode": mode,
        "source_iso": _sanitize_display_path(source_iso),
        "crease_deg": crease_deg,
        "budgets": {
            "max_solid_bytes": DEFAULT_MAX_SOLID_BYTES,
            "max_polys": DEFAULT_TARGET_MAX_POLYS,
            "max_pack_bytes": DEFAULT_MAX_PACK_BYTES,
        },
        "summary": {
            "cars_requested": len(cars),
            "cars_upgraded": len(upgraded_cars),
            "cars_missing": sum(1 for c in cars if c.get("status") == "missing_in_zdir"),
            "total_solids_upgraded": total_solids_upgraded,
            "total_orig_body_triangles": sum(c["orig_triangles"] for c in upgraded_cars),
            "total_upgraded_body_triangles": sum(c["upgraded_triangles"] for c in upgraded_cars),
            "total_orig_pack_bytes": total_orig_pack_bytes,
            "total_upgraded_pack_bytes": total_upg_pack_bytes,
            "total_pack_delta_bytes": total_pack_delta_bytes,
            "zzdata3_append_bytes": zzdata3_append_bytes,
            "zzdata3_append_mib": round(zzdata3_append_bytes / (1024 * 1024), 2),
            "dvd5_headroom_bytes": dvd5_headroom_bytes,
            "dvd5_fits_cleanly": (
                (zzdata3_append_bytes < dvd5_headroom_bytes)
                if dvd5_headroom_bytes is not None
                else True
            ),
            "audio_synths_tuned": len(tuned_audio),
            "total_overrides": len(overrides),
            "vu1_verification": vu1_summary,
        },
        "overrides": overrides,
        "zz_overrides": zz_overrides,
        "cars": cars,
        "audio": audio,
    }


def run_fleet_upgrade_from_iso(
    iso_path: Path,
    out_dir: Path,
    *,
    cars: Sequence[str] = ICONIC_FLEET_CARS,
    all_cars: bool = False,
    include_kits: bool = False,
    prefer_lod_a: bool = False,
    include_parts: bool = False,
    crease_deg: float = DEFAULT_CREASE_DEG,
    tune_audio: bool = True,
) -> Dict[str, Any]:
    """Batch-upgrade PS2 car GEOMETRY.BIN packs & Gnsu synths from untouched ISO ('rb')."""
    out_dir.mkdir(parents=True, exist_ok=True)
    arc = PS2NFSMWArchive(iso_path)
    zdir_geom_entries = {
        (e.resolved_path or "").upper(): e
        for e in arc.entries
        if (e.resolved_path or "").upper().startswith("CARS\\")
        and (e.resolved_path or "").upper().endswith("\\GEOMETRY.BIN")
    }

    target_cars: List[str] = (
        discover_zdir_car_packs(arc, include_parts=include_parts)
        if all_cars
        else [c.strip().upper() for c in cars]
    )
    if not all_cars and include_parts:
        for p_name in SHARED_PART_PACKS:
            if p_name not in target_cars:
                target_cars.append(p_name)

    car_reports: List[Dict[str, Any]] = []
    for car in target_cars:
        vpath = f"CARS\\{car}\\GEOMETRY.BIN"
        entry = zdir_geom_entries.get(vpath.upper())
        if entry is None:
            car_reports.append(
                {
                    "car": car,
                    "virtual_path": vpath,
                    "status": "missing_in_zdir",
                }
            )
            continue
        pack_data = arc.read_entry(entry)
        car_reports.append(
            upgrade_single_car_pack(
                pack_data,
                car,
                vpath,
                out_dir,
                crease_deg=crease_deg,
                include_kits=include_kits,
                prefer_lod_a=prefer_lod_a or all_cars,
            )
        )

    audio_reports: List[Dict[str, Any]] = []
    if tune_audio:
        audio_reports = _tune_hero_gnsu_from_iso(arc, out_dir / "audio")

    manifest = _build_fleet_manifest(
        mode="live_iso",
        source_iso=iso_path,
        crease_deg=crease_deg,
        cars=car_reports,
        audio=audio_reports,
    )
    manifest_path = out_dir / "fleet_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _make_synthetic_car_meshes() -> Tuple[gc.CompiledMesh, gc.CompiledMesh]:
    """Create a curved spherical-cap car body + rigid spoiler/trim mesh in PS2 Z-up meters."""
    # Curved spherical dome car shell (Z-up, +X fwd, +Y left, +Z up)
    nx, ny = 8, 6
    verts: List[Tuple[float, float, float]] = []
    norms: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    tris: List[Tuple[int, int, int, int, int]] = []
    radius = 3.2
    for iy in range(ny + 1):
        for ix in range(nx + 1):
            u = ix / nx
            v = iy / ny
            x = (u - 0.5) * 4.2
            y = (v - 0.5) * 1.84
            z = 0.25 + math.sqrt(max(0.05, radius * radius - 0.35 * x * x - 0.8 * y * y)) * 0.28
            verts.append((x, y, z))
            nx_v = 0.35 * x
            ny_v = 0.8 * y
            nz_v = 1.0
            mag = math.sqrt(nx_v * nx_v + ny_v * ny_v + nz_v * nz_v)
            norms.append((nx_v / mag, ny_v / mag, nz_v / mag))
            uvs.append((u, v))

    stride = nx + 1
    for iy in range(ny):
        for ix in range(nx):
            i0 = iy * stride + ix
            i1 = i0 + 1
            i2 = i0 + stride
            i3 = i2 + 1
            tx = 0 if ix < nx // 2 else 1
            lm = 0 if ix < nx // 2 else 1
            tris.append((i0, i1, i3, tx, lm))
            tris.append((i0, i3, i2, tx, lm))

    body = gc.CompiledMesh(
        vertices=verts,
        normals=norms,
        uvs=uvs,
        triangles=tris,
        group_meta={
            (0, 0): gc.GroupMetadata(strip_flags=0x4180, poly_group=0),
            (1, 1): gc.GroupMetadata(strip_flags=0x4080, poly_group=1),
        },
    )

    # Sharp 90-deg cube trim part (untouched sibling solid)
    c_verts = [
        (-0.5, -0.5, 0.2), (0.5, -0.5, 0.2), (0.5, 0.5, 0.2), (-0.5, 0.5, 0.2),
        (-0.5, -0.5, 0.6), (0.5, -0.5, 0.6), (0.5, 0.5, 0.6), (-0.5, 0.5, 0.6),
    ]
    c_norms = [(0.0, 0.0, -1.0)] * 4 + [(0.0, 0.0, 1.0)] * 4
    c_uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)] * 2
    c_tris = [
        (0, 2, 1, 0, 0), (0, 3, 2, 0, 0),
        (4, 5, 6, 0, 0), (4, 6, 7, 0, 0),
        (0, 1, 5, 0, 0), (0, 5, 4, 0, 0),
        (2, 3, 7, 0, 0), (2, 7, 6, 0, 0),
    ]
    trim = gc.CompiledMesh(vertices=c_verts, normals=c_norms, uvs=c_uvs, triangles=c_tris)
    return body, trim


def _make_synthetic_gnsu_v20(min_rpm: float = 1000.0, max_rpm: float = 7200.0, sample_rate: int = 24000) -> bytes:
    """Create a minimal valid Gnsu v20 buffer for offline synthetic synth-tuning tests."""
    n_rpm = 4
    n_grain = 2
    total_samples = 2800
    hdr = struct.pack("<4s4sff4I", b"Gnsu", b"20\x00\x00", min_rpm, max_rpm, n_rpm, n_grain, total_samples, sample_rate)
    # 0x20: rpm_table (n_rpm + 1 uint32) + grain table (n_grain * 4B) + granule bytes
    rpm_table = struct.pack(f"<{n_rpm + 1}I", 0, 600, 1300, 2100, total_samples)
    grains = b"\x00" * (n_grain * 4)
    granules = b"\x00" * 64
    return hdr + rpm_table + grains + granules


def run_synthetic_fleet_upgrade(
    out_dir: Path,
    *,
    crease_deg: float = DEFAULT_CREASE_DEG,
    tune_audio: bool = True,
    include_kits: bool = False,
    prefer_lod_a: bool = False,
) -> Dict[str, Any]:
    """Run the full fleet upgrade + validation pipeline offline using synthetic car packs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    body_mesh, trim_mesh = _make_synthetic_car_meshes()
    car_name = "SYNTHCAR"
    vpath = f"CARS\\{car_name}\\GEOMETRY.BIN"
    solids_spec = [
        (f"{car_name}_KIT00_BODY_B", body_mesh, [0x11111111, 0x22222222], [0xD6D6080A, 0x54949AFD]),
        (f"{car_name}_KIT00_TRIM_C", trim_mesh, [0x33333333], [0x12C9453C]),
    ]
    if include_kits:
        solids_spec.insert(
            1,
            (f"{car_name}_KIT01_BODY_B", body_mesh, [0x11111111, 0x22222222], [0xD6D6080A, 0x54949AFD]),
        )
        solids_spec.insert(
            2,
            (f"{car_name}_KIT00_FRONT_TIRE_B", trim_mesh, [0x33333333], [0x12C9453C]),
        )
    pack_bytes = gc.build_synthetic_geometry_pack(
        vpath,
        "SYNTH_GRP",
        solids_spec,
    )
    car_reports = [
        upgrade_single_car_pack(
            pack_bytes,
            car_name,
            vpath,
            out_dir,
            crease_deg=crease_deg,
            include_kits=include_kits,
            prefer_lod_a=prefer_lod_a,
        )
    ]

    audio_reports: List[Dict[str, Any]] = []
    if tune_audio:
        audio_dir = out_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        synth_gin = _make_synthetic_gnsu_v20()
        res = audio_patcher.patch_gnsu_bytes(
            synth_gin, preset="bmw_m3_gtr", path_label=r"SOUND\ENGINE\SYNTH_CAR.GIN"
        )
        assert res.patched_bytes is not None
        out_gin = audio_dir / "synth_bmw_m3_gtr_patched.GIN"
        out_gin.write_bytes(res.patched_bytes)
        inspected = audio_inspect.inspect_standalone_file(out_gin)
        audio_reports.append(
            {
                "preset": "bmw_m3_gtr",
                "car_name": "Synthetic M3 GTR",
                "virtual_path": r"SOUND\ENGINE\SYNTH_CAR.GIN",
                "status": "tuned",
                "parse_errors": 0,
                "upgraded_file": _rel_or_str(out_gin),
                "diff": res.diff,
                "verified_synth": inspected["gnsu_synths"][0].to_dict(),
            }
        )

    manifest = _build_fleet_manifest(
        mode="synthetic",
        source_iso=None,
        crease_deg=crease_deg,
        cars=car_reports,
        audio=audio_reports,
    )
    manifest_path = out_dir / "fleet_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Ultra-Smooth 58K-Poly Metal 4 RT Hero Export
# ---------------------------------------------------------------------------


def _decoded_to_compiled(mesh: gi.DecodedMesh) -> gc.CompiledMesh:
    return gc.CompiledMesh(
        vertices=list(mesh.vertices),
        normals=list(mesh.normals),
        uvs=list(mesh.uvs),
        triangles=list(mesh.triangles),
    )


def _compiled_to_decoded(mesh: gc.CompiledMesh) -> gi.DecodedMesh:
    return gi.DecodedMesh(
        vertices=list(mesh.vertices),
        normals=list(mesh.normals),
        uvs=list(mesh.uvs),
        triangles=list(mesh.triangles),
    )


def export_hq_rt_iso_scene(
    iso_path: Path,
    hq_out: Path,
    *,
    crease_deg: float = DEFAULT_CREASE_DEG,
    spoiler_mode: str = "auto",
) -> Dict[str, Any]:
    """Export 58K-poly BMW M3 GTR Metal 4 RT showcase scene (L2 body, L1 wheels, Y-up tires at Y=0)."""
    if not iso_path.is_file():
        raise FileNotFoundError(f"ISO not found: {iso_path}")

    arc = PS2NFSMWArchive(iso_path)

    def _read_vpath(vpath: str) -> bytes:
        target = vpath.upper()
        ent = next((e for e in arc.entries if (e.resolved_path or "").upper() == target), None)
        if ent is None:
            raise KeyError(f"Archive entry not found: {vpath}")
        return arc.read_entry(ent)

    mat_lookup: Dict[str, str] = dict(eps.BUILTIN_LIGHT_MATERIAL_NAMES)
    try:
        gb_raw = jdlz_decompress(_read_vpath(r"GLOBAL\GLOBALB.LZC"))
        chunks = iter_top_level_bchunks(gb_raw)
        for lm in parse_light_materials(gb_raw, chunks):
            mat_lookup[f"0x{bstringhash(lm.name):08x}"] = lm.name
            mat_lookup[f"0x{lm.name_hash:08x}"] = lm.name
    except Exception:
        pass

    car_pack_bytes = _read_vpath(r"CARS\BMWM3GTRE46\GEOMETRY.BIN")
    wheel_pack_bytes = _read_vpath(r"CARS\WHEELS\GEOMETRY.BIN")
    spoiler_pack_bytes = _read_vpath(r"CARS\SPOILER\GEOMETRY.BIN")

    car_info, _ = gi.parse_geometry_pack(car_pack_bytes, path=r"CARS\BMWM3GTRE46\GEOMETRY.BIN")
    wheel_info, _ = gi.parse_geometry_pack(wheel_pack_bytes, path=r"CARS\WHEELS\GEOMETRY.BIN")
    spoiler_info, _ = gi.parse_geometry_pack(spoiler_pack_bytes, path=r"CARS\SPOILER\GEOMETRY.BIN")

    wanted_car_parts = [
        "BMWM3GTRE46_KIT00_BODY_B",
        "BMWM3GTRE46_KIT00_INTERIOR_B",
        "BMWM3GTRE46_KIT00_FRONT_WINDOW_B",
        "BMWM3GTRE46_KIT00_REAR_WINDOW_B",
        "BMWM3GTRE46_KIT00_FRONT_LEFT_WINDOW_B",
        "BMWM3GTRE46_KIT00_FRONT_RIGHT_WINDOW_B",
        "BMWM3GTRE46_KIT00_REAR_LEFT_WINDOW_B",
        "BMWM3GTRE46_KIT00_REAR_RIGHT_WINDOW_B",
        "BMWM3GTRE46_KIT00_LEFT_HEADLIGHT_B",
        "BMWM3GTRE46_KIT00_RIGHT_HEADLIGHT_B",
        "BMWM3GTRE46_KIT00_LEFT_HEADLIGHT_GLASS_B",
        "BMWM3GTRE46_KIT00_RIGHT_HEADLIGHT_GLASS_B",
        "BMWM3GTRE46_KIT00_LEFT_BRAKELIGHT_B",
        "BMWM3GTRE46_KIT00_RIGHT_BRAKELIGHT_B",
        "BMWM3GTRE46_KIT00_FRONT_BRAKE_B",
        "BMWM3GTRE46_KIT00_REAR_BRAKE_B",
    ]
    car_idx_by_name = {p.name: p.index for p in car_info.parts}
    decode_car_indices = [car_idx_by_name[n] for n in wanted_car_parts if n in car_idx_by_name]
    _, car_meshes = gi.parse_geometry_pack(car_pack_bytes, decode_parts=decode_car_indices)

    body_idx = car_idx_by_name["BMWM3GTRE46_KIT00_BODY_B"]
    body_part = car_info.parts[body_idx]
    orig_body_mesh = car_meshes[body_idx]

    # 2 levels of crease-aware PN-triangle subdivision on BMW M3 GTR body (~58,240 tris)
    sub_body_compiled, body_sub_stats = scm.subdivide_compiled_mesh(
        _decoded_to_compiled(orig_body_mesh),
        crease_deg=crease_deg,
        mode="pn",
        levels=2,
        preserve_aabb=True,
    )
    hq_body_mesh = _compiled_to_decoded(sub_body_compiled)

    wheel_part = next(p for p in wheel_info.parts if p.name == "ENKEI_STYLE01_17_25_B")
    _, wheel_meshes = gi.parse_geometry_pack(wheel_pack_bytes, decode_parts=[wheel_part.index])
    orig_wheel_mesh = wheel_meshes[wheel_part.index]

    # 1 level of crease-aware PN-triangle subdivision on ENKEI wheels for silky round rims & sidewalls
    sub_wheel_compiled, wheel_sub_stats = scm.subdivide_compiled_mesh(
        _decoded_to_compiled(orig_wheel_mesh),
        crease_deg=crease_deg,
        mode="pn",
        levels=1,
        preserve_aabb=True,
    )
    hq_wheel_mesh = _compiled_to_decoded(sub_wheel_compiled)

    spoiler_part = next(
        (p for p in spoiler_info.parts if p.name == "SPOILER_STYLE01_B"),
        spoiler_info.parts[0],
    )
    _, spoiler_meshes = gi.parse_geometry_pack(spoiler_pack_bytes, decode_parts=[spoiler_part.index])
    spoiler_mesh = spoiler_meshes[spoiler_part.index]

    anchors = eps._compute_wheel_arch_anchors(orig_body_mesh, body_part)
    wheel_r = max(abs(wheel_part.aabb_min[2]), abs(wheel_part.aabb_max[2]))
    arch_clearance = 0.044
    wheel_scale = 1.0
    wheel_z = anchors["arch_top_z"] - wheel_r * wheel_scale - arch_clearance
    wheel_min_z = min(v[2] for v in hq_wheel_mesh.vertices) * wheel_scale
    ground_z_ps2 = wheel_z + wheel_min_z

    outer_face_y = anchors["half_width"] * 0.975
    wheel_y_offset = outer_face_y + wheel_part.aabb_min[1] * wheel_scale

    meshes_dir = hq_out / "meshes"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    eps.write_mtl_library(meshes_dir / "materials.mtl")

    scene_parts: List[Dict[str, Any]] = []
    total_verts = 0
    total_tris = 0

    def _emit_part(
        obj_name: str,
        mesh: gi.DecodedMesh,
        pinfo: Optional[gi.SolidPartInfo],
        role: str,
        transparent: bool = False,
        visible: bool = True,
    ) -> None:
        nonlocal total_verts, total_tris
        obj_rel = f"meshes/{obj_name}.obj"
        _, mat_counts = eps.write_pbr_obj_mesh(
            meshes_dir / f"{obj_name}.obj", obj_name, mesh, pinfo, mat_lookup
        )
        total_verts += len(mesh.vertices)
        total_tris += len(mesh.triangles)
        scene_parts.append(
            {
                "name": obj_name,
                "obj": obj_rel,
                "role": role,
                "transparent": transparent,
                "visible": visible,
                "vertex_count": len(mesh.vertices),
                "triangle_count": len(mesh.triangles),
                "materials": mat_counts,
            }
        )

    for pname in wanted_car_parts:
        if pname not in car_idx_by_name or "BRAKE_B" in pname:
            continue
        pidx = car_idx_by_name[pname]
        pinfo = car_info.parts[pidx]
        src_mesh = hq_body_mesh if pname == "BMWM3GTRE46_KIT00_BODY_B" else car_meshes[pidx]
        ymesh = eps.transform_ps2_mesh_to_yup(src_mesh, ground_z_ps2=ground_z_ps2)
        is_glass = "WINDOW" in pname or "HEADLIGHT_GLASS" in pname
        role = (
            "glass"
            if is_glass
            else ("lights" if ("HEADLIGHT" in pname or "BRAKELIGHT" in pname) else "body")
        )
        _emit_part(pname, ymesh, pinfo, role=role, transparent=is_glass, visible=True)

    wheel_specs = [
        ("WHEEL_FL", (anchors["front_x"], +wheel_y_offset, wheel_z), True, "BMWM3GTRE46_KIT00_FRONT_BRAKE_B"),
        ("WHEEL_FR", (anchors["front_x"], -wheel_y_offset, wheel_z), False, "BMWM3GTRE46_KIT00_FRONT_BRAKE_B"),
        ("WHEEL_RL", (anchors["rear_x"], +wheel_y_offset, wheel_z), True, "BMWM3GTRE46_KIT00_REAR_BRAKE_B"),
        ("WHEEL_RR", (anchors["rear_x"], -wheel_y_offset, wheel_z), False, "BMWM3GTRE46_KIT00_REAR_BRAKE_B"),
    ]
    for wname, pos_ps2, rot180, brake_part_name in wheel_specs:
        wmesh_yup = eps.transform_ps2_mesh_to_yup(
            hq_wheel_mesh,
            offset_ps2=pos_ps2,
            rotate_z_180_ps2=rot180,
            scale=wheel_scale,
            ground_z_ps2=ground_z_ps2,
        )
        _emit_part(
            f"ENKEI_STYLE01_17_25_B_{wname}",
            wmesh_yup,
            wheel_part,
            role="wheel",
            visible=True,
        )
        if brake_part_name in car_idx_by_name:
            bidx = car_idx_by_name[brake_part_name]
            binfo = car_info.parts[bidx]
            inboard = -0.045 if pos_ps2[1] > 0 else +0.045
            bpos = (pos_ps2[0], pos_ps2[1] + inboard, pos_ps2[2])
            bmesh_yup = eps.transform_ps2_mesh_to_yup(
                car_meshes[bidx],
                offset_ps2=bpos,
                rotate_z_180_ps2=rot180,
                scale=1.0,
                ground_z_ps2=ground_z_ps2,
            )
            _emit_part(f"{brake_part_name}_{wname}", bmesh_yup, binfo, role="brake", visible=True)

    has_builtin_wing = eps._body_has_builtin_wing(orig_body_mesh, body_part)
    spoiler_visible = (spoiler_mode == "force") or (
        spoiler_mode == "auto" and not has_builtin_wing
    )
    trunk_x = body_part.aabb_min[0] + 0.24
    trunk_z = 0.875
    spoiler_yup = eps.transform_ps2_mesh_to_yup(
        spoiler_mesh,
        offset_ps2=(trunk_x, 0.0, trunk_z),
        ground_z_ps2=ground_z_ps2,
    )
    _emit_part("SPOILER_STYLE01_B", spoiler_yup, spoiler_part, role="spoiler", visible=spoiler_visible)

    manifest = eps._build_scene_manifest(
        mode="hq_rt_bmwm3gtre46",
        car_name="BMW M3 GTR E46 — HQ 58K-Poly RT Showcase (SLUS-21351)",
        parts=scene_parts,
        total_verts=total_verts,
        total_tris=total_tris,
        anchors=anchors,
        wheel_z_ps2=wheel_z,
        ground_z_ps2=ground_z_ps2,
        spoiler_visible=spoiler_visible,
        has_builtin_wing=has_builtin_wing,
    )
    manifest["summary"]["hq_subdivision"] = {
        "crease_deg": crease_deg,
        "body_levels": 2,
        "body_orig_triangles": body_sub_stats.input_triangles,
        "body_hq_triangles": body_sub_stats.output_triangles,
        "body_hq_vertices": body_sub_stats.output_vertices,
        "wheel_levels": 1,
        "wheel_orig_triangles": wheel_sub_stats.input_triangles,
        "wheel_hq_triangles_each": wheel_sub_stats.output_triangles,
    }

    hq_out.mkdir(parents=True, exist_ok=True)
    (hq_out / "scene.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def export_hq_rt_synthetic_scene(
    hq_out: Path,
    *,
    crease_deg: float = DEFAULT_CREASE_DEG,
) -> Dict[str, Any]:
    """Generate an offline subdivided Y-up synthetic car scene matching export_pbr_scene.py."""
    meshes_dir = hq_out / "meshes"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    eps.write_mtl_library(meshes_dir / "materials.mtl")

    body_ps2, _ = _make_synthetic_car_meshes()
    sub_body_compiled, body_stats = scm.subdivide_compiled_mesh(
        body_ps2, crease_deg=crease_deg, mode="pn", levels=2, preserve_aabb=True
    )
    body_yup = eps.transform_ps2_mesh_to_yup(_compiled_to_decoded(sub_body_compiled), ground_z_ps2=0.0)

    wheel_radius = 0.34
    base_wheel = eps._make_cylinder_wheel(0.0, wheel_radius, 0.0, wheel_radius, 0.25, segs=24)
    sub_wheel_compiled, wheel_stats = scm.subdivide_compiled_mesh(
        _decoded_to_compiled(base_wheel), crease_deg=crease_deg, mode="pn", levels=1, preserve_aabb=True
    )

    body_info = gi.SolidPartInfo(
        index=0, name="SYNTH_HQ_BODY_B", name_hash="0x00000001", lod="B", version=1, flags=0,
        header_polys=0, num_bones=0, num_textures=0, num_light_materials=2, num_markers=0,
        aabb_min=[-2.15, -0.93, 0.14], aabb_max=[2.26, 0.93, 1.16], volume=1.0, density=1.0,
        texture_hashes=[], light_material_hashes=["0xd6d6080a", "0x54949afd"],
        plat_version=0, strip_flags=0, strip_count=0, strip_data_bytes=0,
        strip_vertex_total=len(body_yup.vertices), strip_poly_total=len(body_yup.triangles), chunk_offset=0,
    )
    wh_info = dataclasses.replace(
        body_info, name="ENKEI_STYLE01_17_25_B", light_material_hashes=["0x78743ca1", "0x22719fa9"]
    )
    glass_info = dataclasses.replace(
        body_info, name="SYNTH_HQ_WINDOW_B", light_material_hashes=["0x471a1dca"]
    )
    sp_info = dataclasses.replace(
        body_info, name="SPOILER_STYLE01_B", light_material_hashes=["0xb57ab98e", "0x2e65e067"]
    )

    scene_parts: List[Dict[str, Any]] = []
    total_verts = 0
    total_tris = 0

    def _emit(name: str, m: gi.DecodedMesh, info: gi.SolidPartInfo, role: str, transparent: bool = False) -> None:
        nonlocal total_verts, total_tris
        _, mc = eps.write_pbr_obj_mesh(
            meshes_dir / f"{name}.obj", name, m, info, eps.BUILTIN_LIGHT_MATERIAL_NAMES
        )
        total_verts += len(m.vertices)
        total_tris += len(m.triangles)
        scene_parts.append(
            {
                "name": name,
                "obj": f"meshes/{name}.obj",
                "role": role,
                "transparent": transparent,
                "visible": True,
                "vertex_count": len(m.vertices),
                "triangle_count": len(m.triangles),
                "materials": mc,
            }
        )

    _emit("BMWM3GTRE46_KIT00_BODY_B", body_yup, body_info, "body")
    glass = eps._make_box_mesh(0.0, 0.93, -0.16, 1.44, 0.36, 1.72, mat_idx=0)
    _emit("BMWM3GTRE46_KIT00_FRONT_WINDOW_B", glass, glass_info, "glass", transparent=True)
    spoiler = eps._make_box_mesh(0.0, 1.06, -1.96, 1.54, 0.05, 0.28, mat_idx=0)
    _emit("SPOILER_STYLE01_B", spoiler, sp_info, "spoiler")

    for wname, (wx, wz) in [
        ("WHEEL_FL", (-0.82, 1.38)),
        ("WHEEL_FR", (+0.82, 1.38)),
        ("WHEEL_RL", (-0.82, -1.34)),
        ("WHEEL_RR", (+0.82, -1.34)),
    ]:
        wm = gi.DecodedMesh(
            vertices=[(vx + wx, vy, vz + wz) for vx, vy, vz in sub_wheel_compiled.vertices],
            normals=list(sub_wheel_compiled.normals),
            uvs=list(sub_wheel_compiled.uvs),
            triangles=list(sub_wheel_compiled.triangles),
        )
        _emit(f"ENKEI_STYLE01_17_25_B_{wname}", wm, wh_info, "wheel")

    manifest = eps._build_scene_manifest(
        mode="synthetic_hq_rt",
        car_name="Rockport 2026 Synthetic HQ RT Car",
        parts=scene_parts,
        total_verts=total_verts,
        total_tris=total_tris,
        anchors={"front_x": 1.38, "rear_x": -1.34, "arch_top_z": 0.60, "half_width": 0.93, "length": 4.41},
        wheel_z_ps2=wheel_radius,
        ground_z_ps2=0.0,
        spoiler_visible=True,
        has_builtin_wing=False,
    )
    manifest["summary"]["hq_subdivision"] = {
        "crease_deg": crease_deg,
        "body_levels": 2,
        "body_orig_triangles": body_stats.input_triangles,
        "body_hq_triangles": body_stats.output_triangles,
        "body_hq_vertices": body_stats.output_vertices,
        "wheel_levels": 1,
        "wheel_orig_triangles": wheel_stats.input_triangles,
        "wheel_hq_triangles_each": wheel_stats.output_triangles,
    }
    hq_out.mkdir(parents=True, exist_ok=True)
    (hq_out / "scene.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch PS2 high-poly car fleet upgrader & 58K-poly Metal 4 RT hero exporter."
    )
    parser.add_argument(
        "--iso",
        type=Path,
        default=None,
        help="Path to untouched PS2 ISO (read-only 'rb'; defaults to NFSMW_ISO or Home PS2Games).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Output directory for upgraded PS2 car GEOMETRY.BINs, audio, and fleet_manifest.json.",
    )
    parser.add_argument(
        "--cars",
        nargs="+",
        default=list(ICONIC_FLEET_CARS),
        help="Car directory names under CARS\\ to upgrade.",
    )
    parser.add_argument(
        "--all-cars",
        action="store_true",
        help="Auto-discover and upgrade every vehicle CARS\\*\\GEOMETRY.BIN in ZDIR.BIN.",
    )
    parser.add_argument(
        "--include-kits",
        action="store_true",
        help="Upgrade all body kits (KIT00..KIT05 / DAMAGE0..1), stock tires, hoods, and cop bumpers within PS2 caps.",
    )
    parser.add_argument(
        "--prefer-lod-a",
        action="store_true",
        help="Prefer highest available LOD solid per vehicle (LOD A -> LOD B -> LOD C).",
    )
    parser.add_argument(
        "--include-parts",
        action="store_true",
        help=(
            "Experimental: also upgrade GLOBALMEMORYFILE.BIN-cached shared part packs "
            "(BRAKES, ROOF, SPOILER*, WHEELS). Requires syncing their cached headers in "
            "GLOBAL\\GLOBALMEMORYFILE.BIN or aftermarket wheels/roofs/spoilers will vanish."
        ),
    )
    parser.add_argument(
        "--crease-deg",
        type=float,
        default=DEFAULT_CREASE_DEG,
        help="Dihedral crease angle threshold in degrees (default: 38.0).",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Skip batch Gnsu hero synth tuning.",
    )
    parser.add_argument(
        "--skip-fleet",
        action="store_true",
        help="Skip batch PS2 fleet upgrade (use with --export-hq-rt).",
    )
    parser.add_argument(
        "--export-hq-rt",
        action="store_true",
        help="Export 58K-poly BMW M3 GTR Y-up Metal 4 RT scene (L2 body + L1 wheels).",
    )
    parser.add_argument(
        "--hq-out",
        type=Path,
        default=DEFAULT_HQ_OUT_DIR,
        help="Output directory for HQ Metal 4 RT scene.json and meshes (default: build/flywheel/hq_m3_gtr).",
    )
    parser.add_argument(
        "--spoiler",
        choices=("auto", "force", "off"),
        default="auto",
        help="Spoiler visibility mode for HQ RT export (default: auto).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run offline synthetic subdivision & scene export without requiring an ISO.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    out_dir = args.out_dir if args.out_dir.is_absolute() else (REPO_ROOT / args.out_dir)
    hq_out = args.hq_out if args.hq_out.is_absolute() else (REPO_ROOT / args.hq_out)

    fleet_manifest: Optional[Dict[str, Any]] = None
    hq_manifest: Optional[Dict[str, Any]] = None

    if args.synthetic:
        if not args.skip_fleet:
            fleet_manifest = run_synthetic_fleet_upgrade(
                out_dir,
                crease_deg=args.crease_deg,
                tune_audio=not args.no_audio,
                include_kits=args.include_kits,
                prefer_lod_a=args.prefer_lod_a,
            )
        if args.export_hq_rt:
            hq_manifest = export_hq_rt_synthetic_scene(
                hq_out, crease_deg=args.crease_deg
            )
    else:
        iso_path = resolve_iso_path(args.iso)
        if iso_path is None:
            raise FileNotFoundError(
                "PS2 ISO not found (set NFSMW_ISO, pass --iso, or use --synthetic)"
            )
        if not args.skip_fleet:
            fleet_manifest = run_fleet_upgrade_from_iso(
                iso_path,
                out_dir,
                cars=args.cars,
                all_cars=args.all_cars,
                include_kits=args.include_kits,
                prefer_lod_a=args.prefer_lod_a,
                include_parts=args.include_parts,
                crease_deg=args.crease_deg,
                tune_audio=not args.no_audio,
            )
        if args.export_hq_rt:
            hq_manifest = export_hq_rt_iso_scene(
                iso_path,
                hq_out,
                crease_deg=args.crease_deg,
                spoiler_mode=args.spoiler,
            )

    if args.json:
        print(
            json.dumps(
                {"fleet_manifest": fleet_manifest, "hq_rt_scene": hq_manifest},
                indent=2,
            )
        )
    else:
        if fleet_manifest is not None:
            s = fleet_manifest["summary"]
            v = s["vu1_verification"]
            print(
                f"PS2 Car Fleet Upgrade ({fleet_manifest['mode']}): "
                f"{s['cars_upgraded']}/{s['cars_requested']} packs upgraded "
                f"({s['total_solids_upgraded']} solids), "
                f"{s['total_orig_body_triangles']:,} -> {s['total_upgraded_body_triangles']:,} tris, "
                f"VU1[b2={v['bad_b2']} b3={v['bad_b3']} q={v['bad_q']} aabb_err={v['max_vu1_aabb_err']:.6f}], "
                f"ZZDATA3 append={s['zzdata3_append_mib']:.2f} MiB, "
                f"{s['audio_synths_tuned']} Gnsu synths tuned -> {out_dir / 'fleet_manifest.json'}"
            )
            for c in fleet_manifest["cars"]:
                if c.get("status") == "upgraded":
                    cv = c["vu1_verification"]
                    print(
                        f"  [OK] {c['car']:16s} solids={c['solids_upgraded']:3d} "
                        f"{c['orig_triangles']:6,d} -> {c['upgraded_triangles']:7,d} tris | "
                        f"max_solid={c['solid_bytes']:7,d} B | pack={c['upgraded_pack_bytes']:9,d} B | "
                        f"aabb_err={cv['max_vu1_aabb_err']:.6f}"
                    )
                else:
                    print(f"  [SKIP] {c['car']:16s} status={c.get('status')}")
        if hq_manifest is not None:
            hs = hq_manifest["summary"]
            hq_sub = hs["hq_subdivision"]
            print(
                f"HQ Metal 4 RT Export ({hq_manifest['mode']}): "
                f"body {hq_sub['body_orig_triangles']:,} -> {hq_sub['body_hq_triangles']:,} tris (L2), "
                f"wheel {hq_sub['wheel_orig_triangles']:,} -> {hq_sub['wheel_hq_triangles_each']:,} tris each (L1), "
                f"total {hs['part_count']} parts, {hs['total_triangles']:,} tris -> {hq_out / 'scene.json'}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
