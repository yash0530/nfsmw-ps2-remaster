#!/usr/bin/env python3
"""Strictly opt-in, per-car VU1 smoothing + promotion validator (v2.3).

Why blind fleet subdivision (``upgrade_car_fleet.py --all-cars``) broke cars (measured on the ISO):
  * Every hero ``*_KIT00_BODY_B`` carries 0x00134017/18/19 damage/morph chunks whose 0x00134019
    records are ``(hash, count, u16 strip, u16 vertex)`` into the *original* strip layout (143,147
    records on the 4 hero cars, 0 out of range). The compiler drops them, and re-stripping would
    invalidate them anyway -> such solids are never eligible.
  * Fleet caps (768 KiB solid, 5 MiB pack) are 4.5x / 1.7x the largest stock car data
    (171,024 B solid in COBALTSS, 3,090,112 B pack in SL500).
  * Stock eSolid AABBs carry ~10 mm authored margin; the compiler rewrites them tight (an identity
    recompile shrinks the BMW tyre 10 mm per face) and the fleet check compared vertex bounds.
  * Wheels/brakes hang off the hub origin (identity pivot, X/Z-symmetric AABB) + 0x0013401A markers;
    shared WHEELS/SPOILER/ROOF/BRAKES packs are header-cached in GLOBALMEMORYFILE.BIN.

Only explicitly named cars/parts are smoothed (L1 crease-aware PN); every rebuilt pack is validated
and only passing cars are written to ``build/``. The ISO is opened read-only ('rb').
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import geometry_compiler as gc  # noqa: E402
import geometry_inspect as gi  # noqa: E402
import subdivide_car_mesh as scm  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

DEFAULT_OUT_DIR = _TOOLS_DIR.parent / "build" / "optin_cars_v23"
DEFAULT_OUT_DIR_V26 = _TOOLS_DIR.parent / "build" / "optin_cars_v26"
DEFAULT_ISO = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
RETAIL_MAX_CAR_PACK = 3_090_112  # CARS\SL500\GEOMETRY.BIN, largest stock car pack (WHEELS excluded)
SAFE_CAR_PACK_CAP = 2_450_000    # conservative cap enforced on all cars whose stock pack < 2.45 MB
RETAIL_MAX_SOLID = 171_024       # v2.3 standard-tier solid cap
LIMIT_MAX_SOLID = 450_000        # v2.6 limit-tier EE SolidPack cap (well below 768 KiB compiler guardrail)
SHARED_PACKS = frozenset({"BRAKES", "ROOF", "SPOILER", "SPOILER_CARRERA", "SPOILER_HATCH", "SPOILER_PORSCHES", "WHEELS"})
HERO_CARS: Tuple[str, ...] = ("BMWM3GTRE46", "CARRERAGT", "GALLARDO", "MUSTANGGT")
BLACKLIST_PRESET: Tuple[str, ...] = (
    "BMWM3GTRE46", "CARRERAGT", "GALLARDO", "MUSTANGGT",
    "CORVETTE", "VIPER", "SLR", "FORDGT", "DB9", "SUPRA", "RX7",
    "IMPREZAWRX", "LANCEREVO8", "GTO", "911TURBO", "911GT2",
    "MURCIELAGO", "CAYMANS", "CLIO", "COBALTSS", "PUNTO",
    "COPGTO", "COPSPORT", "COPMIDSIZE",
)
HERO_PARTS: Tuple[str, ...] = ("KIT00_BODY_B", "KIT00_FRONT_TIRE_B")
HUB_KEYS = ("TIRE", "WHEEL", "RIM", "BRAKE")
HUB_TOL, AABB_TOL = 0.001, 0.0005  # metres
_MASKED = frozenset([0x14, 0x15, 0x19, 0x1A])  # eSolid poly count + tex/mat counts; all else must match
_LIMIT_PRIMARY_SUFFIXES = (
    "KIT00_BODY_B", "KIT00_DAMAGE0_BODY_A", "KIT00_DAMAGE0_BODY_B",
    "BASE_A", "BASE_B",
    "KIT00_FRONT_TIRE_A", "KIT00_FRONT_TIRE_B", "KIT00_REAR_TIRE_A", "KIT00_REAR_TIRE_B",
    "KIT00_HOOD_B", "KIT00_SPOILER_B", "KIT00_UNIVERSAL_SPOILER_BASE_B",
    "KIT00_LEFT_SIDE_MIRROR_B", "KIT00_RIGHT_SIDE_MIRROR_B",
    "KIT00_DAMAGE0_HOOD_A", "KIT00_DAMAGE0_HOOD_B",
    "KIT00_DAMAGE0_FRONT_BUMPER_A", "KIT00_DAMAGE0_FRONT_BUMPER_B",
    "KIT00_DAMAGE0_REAR_BUMPER_A", "KIT00_DAMAGE0_REAR_BUMPER_B",
    "KIT00_DAMAGE0_TRUNK_A", "KIT00_DAMAGE0_TRUNK_B",
)

@dataclass(frozen=True)
class CarBudget:
    """Hard per-car caps; stored bytes == on-disc bytes (car packs sit raw in ZZDATA)."""
    max_pack_bytes: int = RETAIL_MAX_CAR_PACK
    max_pack_growth: float = 1.35
    max_solid_bytes: int = RETAIL_MAX_SOLID
    max_solid_tris: int = 8192
    max_added_tris: int = 6144
    max_added_verts: int = 8192
    tier: str = "standard"
    allow_primary_morph_strip: bool = False
    min_curve_deg: float = 8.0
    min_edge_len: float = 0.015

    def pack_cap(self, orig_len: int) -> int:
        # Allow a 32 KiB floor only for tiny synthetic test packs; real retail cars (>100 KiB) always use max_pack_growth.
        cap = min(self.max_pack_bytes, max(int(orig_len * self.max_pack_growth), orig_len + 32_768))
        if self.tier == "limit" and orig_len < SAFE_CAR_PACK_CAP:
            cap = min(cap, SAFE_CAR_PACK_CAP)
        return cap


LIMIT_BUDGET = CarBudget(
    max_pack_bytes=RETAIL_MAX_CAR_PACK,
    max_pack_growth=1.35,
    max_solid_bytes=LIMIT_MAX_SOLID,
    max_solid_tris=8192,
    max_added_tris=12_288,
    max_added_verts=20_480,
    tier="limit",
    allow_primary_morph_strip=True,
    min_curve_deg=8.0,
    min_edge_len=0.015,
)

def _chunks(data: bytes, p: int, end: int) -> Iterator[Tuple[int, int, int]]:
    """Yield (chunk_id, payload_start, payload_end) for consecutive bChunks in [p, end)."""
    end = min(end, len(data))
    while p + 8 <= end:
        cid, sz = struct.unpack_from("<II", data, p)
        yield cid, p + 8, min(end, p + 8 + sz)
        p += 8 + sz

def _solid_len(data: bytes, off: int) -> int:
    return 8 + struct.unpack_from("<I", data, off + 4)[0]

def _spans(data: bytes, off: int) -> Dict[int, Tuple[int, int]]:
    """SolidPack sub-chunk payload spans with EA 0x11 alignment padding skipped."""
    return {cid: ((gi._skip_11_padding if cid == gc.CHUNK_POS_MARKERS else gi._skip_11_word_padding)(data, s, e), e)
            for cid, s, e in _chunks(data, off + 8, off + _solid_len(data, off))}

def _subs(data: bytes, off: int) -> Dict[int, bytes]:
    return {c: data[s:e] for c, (s, e) in _spans(data, off).items()}

def _damage_ids(sub: Dict[int, Any]) -> List[str]:
    return sorted(f"0x{c:08x}" for c in sub if c in gc.DAMAGE_CHUNK_IDS)

def restore_authored_aabb(orig: bytes, new: bytes, names: Sequence[str]) -> bytes:
    """Copy the stock eSolid AABB (with its authored margin) back over the compiler's tight one."""
    (oi, _), (ni, _), buf = gi.parse_geometry_pack(orig), gi.parse_geometry_pack(new), bytearray(new)
    for op, np_ in zip(oi.parts, ni.parts):
        if op.name in names:
            o, n = (_spans(d, p.chunk_offset)[gc.CHUNK_ESOLID_HDR][0] for d, p in ((orig, op), (new, np_)))
            buf[n + 0x20: n + 0x40] = orig[o + 0x20: o + 0x40]
    return bytes(buf)

def discover_limit_parts(pack_info: gi.GeometryPackInfo, car: str) -> List[str]:
    """Select curated primary exterior/wheel solids present in ``pack_info`` for ``--tier limit``."""
    car_u = car.upper()
    by_name = {p.name.upper(): p for p in pack_info.parts}
    out: List[str] = []
    for suf in _LIMIT_PRIMARY_SUFFIXES:
        full = f"{car_u}_{suf}"
        if full in by_name:
            out.append(suf)
    return out


def eligibility(data: bytes, part: Optional[gi.SolidPartInfo], budget: CarBudget) -> Optional[str]:
    """Pre-compile gate; returns a rejection reason or None."""
    if part is None:
        return "part_not_found"
    u = part.name.upper()
    if budget.tier == "limit":
        if not u.endswith(("_A", "_B")) or "DECAL" in u:
            return "not_lod_a_or_b"
        if any(k in u for k in ("DAMAGE1", "DAMAGE2", "SCRATCH")):
            return "secondary_damage_state_skipped"
    elif not u.endswith("_B"):
        return "not_lod_b"  # LOD A = frontend/showroom decals, C-E = distance LODs
    if _damage_ids(_spans(data, part.chunk_offset)) and not budget.allow_primary_morph_strip:
        return "damage_morph_tables_index_original_strips"
    if scm.adaptive_part_subdivision_params(part.name)[1] == 0:
        return "part_class_not_smoothed"
    if budget.tier == "limit":
        if part.strip_poly_total <= 0 or part.strip_poly_total >= budget.max_solid_tris:
            return f"tri_budget({part.strip_poly_total}>={budget.max_solid_tris})"
    elif part.strip_poly_total <= 0 or part.strip_poly_total * 4 > budget.max_solid_tris:
        return f"tri_budget({part.strip_poly_total}*4>{budget.max_solid_tris})"
    return None

def hub_metrics(verts: Sequence[Tuple[float, float, float]]) -> Dict[str, float]:
    """Wheel-plane (X/Z) hub centre, radii, max radial reach and tread (Y) extents."""
    xs, ys, zs = ([v[i] for v in verts] for i in range(3))
    return {"cx": (min(xs) + max(xs)) / 2, "cz": (min(zs) + max(zs)) / 2, "rx": (max(xs) - min(xs)) / 2,
            "rz": (max(zs) - min(zs)) / 2, "ymin": min(ys), "ymax": max(ys),
            "rmax": max(math.hypot(v[0], v[2]) for v in verts)}

def hub_errors(orig_verts: Sequence[Tuple[float, float, float]], new_verts: Sequence[Tuple[float, float, float]]) -> List[str]:
    o, n = hub_metrics(orig_verts), hub_metrics(new_verts)
    errs = [f"wheel_hub_{k}_drift({o[k]:.4f}->{n[k]:.4f})" for k in ("cx", "cz", "rx", "rz", "ymin", "ymax")
            if abs(o[k] - n[k]) > HUB_TOL]
    return errs + ([f"wheel_hub_radial_bulge({o['rmax']:.4f}->{n['rmax']:.4f})"] if n["rmax"] > o["rmax"] + HUB_TOL else [])

def pack_table_errors(orig: bytes, new: bytes) -> List[str]:
    """0x00134004 entries must hit the SolidPack with matching hash/size; 0x00134002+0x68 tracks length."""
    info, _ = gi.parse_geometry_pack(new)
    by_off = {p.chunk_offset: int(p.name_hash, 16) for p in info.parts}
    tables = []
    for d in (orig, new):
        gid = next(((s, e) for c, s, e in _chunks(d, 8, _solid_len(d, 0)) if c == gc.CHUNK_GEOM_INFO), (0, 0))
        tables.append({c: d[s:e] for c, s, e in _chunks(d, *gid)})
    errs, table = [], tables[1].get(gc.CHUNK_GEOM_OFFSETS, b"")
    if len(table) // 24 != info.solid_count:
        errs.append(f"offset_table_count({len(table) // 24}!={info.solid_count})")
    for e in range(0, len(table) - 23, 24):
        h, off, size = struct.unpack_from("<3I", table, e)
        if by_off.get(off) != h or _solid_len(new, off) != size:
            errs.append(f"offset_entry_{e // 24}_mismatch(0x{h:08x}@{off})")
    hl = [struct.unpack_from("<I", t.get(gc.CHUNK_GEOM_HEADER, bytes(0x70)), 0x68)[0] for t in tables]
    return errs + ([f"header_file_size({hl[1]}!={len(new)})"] if hl[0] == len(orig) and hl[1] != len(new) else [])

def solid_errors(orig: bytes, op: gi.SolidPartInfo, omesh: gi.DecodedMesh, new: bytes, np_: gi.SolidPartInfo,
                 nmesh: Optional[gi.DecodedMesh], exp_tris: int, budget: CarBudget) -> List[str]:
    """Post-compile integrity checks for one rebuilt solid."""
    errs: List[str] = []
    os_, ns = _subs(orig, op.chunk_offset), _subs(new, np_.chunk_offset)
    oh, nh = os_.get(gc.CHUNK_ESOLID_HDR, b""), ns.get(gc.CHUNK_ESOLID_HDR, b"")
    if len(oh) != len(nh) or any(a != b for i, (a, b) in enumerate(zip(oh, nh)) if i not in _MASKED):
        errs.append("esolid_header_changed(name/flags/bones/markers/aabb/pivot)")
    for cid, label in ((gc.CHUNK_TEX_TABLE, "texture_table"), (gc.CHUNK_MAT_TABLE, "material_table"),
                       (gc.CHUNK_POS_MARKERS, "part_attachment_markers")):
        if os_.get(cid) != ns.get(cid):
            errs.append(f"{label}_changed")
    if budget.allow_primary_morph_strip:
        if _damage_ids(ns):
            errs.append(f"stale_damage_chunks_retained({_damage_ids(ns)})")
        if len(nh) >= 0x88 and struct.unpack_from("<I", nh, 0x84)[0] != 0:
            errs.append("esolid_morph_ptr_nonzero")
    elif _damage_ids(os_) != _damage_ids(ns):
        errs.append(f"damage_chunks_changed({_damage_ids(os_)}->{_damage_ids(ns)})")
    if _solid_len(new, np_.chunk_offset) > budget.max_solid_bytes:
        errs.append(f"solid_bytes({_solid_len(new, np_.chunk_offset)}>{budget.max_solid_bytes})")
    if not (np_.strip_poly_total == np_.header_polys == exp_tris) or nmesh is None or len(nmesh.triangles) != exp_tris:
        return errs + [f"triangle_count_mismatch(expected {exp_tris})"]
    vmin, vmax = gc._compute_aabb(omesh.vertices)  # stock *vertex* bounds (header has authored margin)
    vu1 = gc.verify_solid_vu1_invariants(new, np_, expected_min=vmin, expected_max=vmax)
    ref_scale = gc.verify_solid_vu1_invariants(orig, op)["scale"]
    if vu1["bad_tag"] or vu1["bad_b2"] or vu1["bad_b3"] or vu1["bad_q"] or abs(vu1["scale"] - ref_scale) > 1e-9:
        errs.append(f"vu1_invariants(tag={vu1['bad_tag']},b2={vu1['bad_b2']},b3={vu1['bad_b3']},q={vu1['bad_q']},"
                    f"scale={vu1['scale']:.6g}/{ref_scale:.6g})")
    if vu1["vu1_aabb_err"] > AABB_TOL or vu1["vu1_strip_overshoot"] > 1e-4:
        errs.append(f"vu1_aabb_drift({vu1['vu1_aabb_err']:.6f})")
    if any(not (lo - 1e-4 <= v[a] <= hi + 1e-4) for v in nmesh.vertices for a, lo, hi in zip(range(3), np_.aabb_min, np_.aabb_max)):
        errs.append("vertices_outside_esolid_aabb")
    return errs + (hub_errors(omesh.vertices, nmesh.vertices) if any(k in op.name.upper() for k in HUB_KEYS) else [])

def validate_pack(orig: bytes, new: bytes, expected: Dict[str, int], budget: CarBudget) -> Dict[str, Any]:
    """Validate a rebuilt car pack against the untouched one; ``ok`` gates promotion."""
    cap = budget.pack_cap(len(orig))
    fails = [f"pack_bytes({len(new)}>{cap})"] if len(new) > cap else []
    oi, _ = gi.parse_geometry_pack(orig)
    idx = [p.index for p in oi.parts if p.name in expected]
    (_, omeshes), (ni, nmeshes) = gi.parse_geometry_pack(orig, decode_parts=idx), gi.parse_geometry_pack(new, decode_parts=idx)
    if [p.name for p in oi.parts] != [p.name for p in ni.parts]:
        return {"ok": False, "failures": fails + ["solid_list_changed"]}
    fails += pack_table_errors(orig, new)
    added_tris = added_verts = 0
    for op, np_ in zip(oi.parts, ni.parts):
        if op.name not in expected:
            if orig[op.chunk_offset: op.chunk_offset + _solid_len(orig, op.chunk_offset)] != \
                    new[np_.chunk_offset: np_.chunk_offset + _solid_len(new, np_.chunk_offset)]:
                fails.append(f"{op.name}: untouched_sibling_changed")
            continue
        added_tris += np_.strip_poly_total - op.strip_poly_total
        added_verts += np_.strip_vertex_total - op.strip_vertex_total
        fails += [f"{op.name}: {e}" for e in solid_errors(orig, op, omeshes[op.index], new, np_,
                                                         nmeshes.get(np_.index), expected[op.name], budget)]
    fails += [f"car_added_{k}({v}>{c})" for k, v, c in (("tris", added_tris, budget.max_added_tris),
                                                        ("verts", added_verts, budget.max_added_verts)) if v > c]
    return {"ok": not fails, "failures": fails, "pack_bytes": len(new), "pack_cap": cap,
            "added_tris": added_tris, "added_strip_verts": added_verts}

def _build_replacements(
    pack: bytes,
    targets: Sequence[gi.SolidPartInfo],
    decoded: Dict[int, gi.DecodedMesh],
    budget: CarBudget,
    tri_budget_scale: float = 1.0,
) -> Dict[str, gc.CompiledMesh]:
    """Subdivide ``targets`` using standard 1-to-4 PN or v2.6 curvature-selective PN."""
    repl: Dict[str, gc.CompiledMesh] = {}
    if budget.tier != "limit":
        for p in targets:
            m, (crease, _lv, lock) = decoded[p.index], scm.adaptive_part_subdivision_params(p.name)
            src = gc.CompiledMesh(list(m.vertices), list(m.normals), list(m.uvs), [t for t in m.triangles if len(set(t[:3])) == 3])
            sub, _ = scm.subdivide_compiled_mesh(src, crease_deg=crease, mode="pn", levels=1, lock_material_seams=lock)
            sub.triangles = [t for t in sub.triangles if len(set(t[:3])) == 3]
            repl[p.name] = sub
        return repl

    # Limit tier: compute safe total added-triangle budget from pack byte headroom + reclaimed morph bytes
    reclaimed = sum(
        sp[1] - sp[0]
        for p in targets
        for cid, sp in _spans(pack, p.chunk_offset).items()
        if cid in gc.DAMAGE_CHUNK_IDS
    )
    eff_headroom = max(0, budget.pack_cap(len(pack)) - len(pack) + reclaimed - 6_144)
    total_tri_cap = min(budget.max_added_tris, max(256, int(eff_headroom / 36.0 * tri_budget_scale)))
    # Share added-tri budget proportionally across targets (tires + main body prioritized)
    weights = [
        (2.0 if "TIRE" in p.name.upper() else 1.5 if "BODY" in p.name.upper() else 1.0) * max(p.strip_poly_total, 1)
        for p in targets
    ]
    wsum = sum(weights) or 1.0
    rem_tris = total_tri_cap
    for p, w in zip(targets, weights):
        m, (crease, _lv, lock) = decoded[p.index], scm.adaptive_part_subdivision_params(p.name)
        src = gc.CompiledMesh(list(m.vertices), list(m.normals), list(m.uvs), [t for t in m.triangles if len(set(t[:3])) == 3])
        in_tris = len(src.triangles)
        share = max(64, int(total_tri_cap * (w / wsum)))
        max_out = min(budget.max_solid_tris, in_tris + min(share, rem_tris))
        if max_out <= in_tris + 4:
            continue
        is_tire = any(k in p.name.upper() for k in HUB_KEYS)
        min_c = 4.0 if is_tire else budget.min_curve_deg
        min_e = 0.008 if is_tire else budget.min_edge_len
        sub, _ = scm.subdivide_compiled_mesh_selective(
            src, crease_deg=crease, min_curve_deg=min_c, min_edge_len=min_e,
            max_output_tris=max_out, preserve_aabb=True, lock_material_seams=lock,
        )
        sub.triangles = [t for t in sub.triangles if len(set(t[:3])) == 3]
        if len(sub.triangles) > in_tris:
            repl[p.name] = sub
            rem_tris = max(0, rem_tris - (len(sub.triangles) - in_tris))
    return repl


def smooth_car_pack(pack: bytes, car: str, parts: Optional[Sequence[str]] = None,
                    budget: Optional[CarBudget] = None) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """Smooth eligible named parts of one car pack; returns (bytes only if validated, report)."""
    car = car.upper()
    budget = budget or CarBudget()
    rep: Dict[str, Any] = {"car": car, "tier": budget.tier, "orig_pack_bytes": len(pack),
                           "budget": asdict(budget), "parts": [], "ok": False}
    if car in SHARED_PACKS:
        return None, {**rep, "failures": ["shared_pack_cached_in_globalmemoryfile"]}
    pack_info = gi.parse_geometry_pack(pack)[0]
    by_name = {p.name.upper(): p for p in pack_info.parts}
    req_parts = list(parts) if parts is not None else (
        discover_limit_parts(pack_info, car) if budget.tier == "limit" else list(HERO_PARTS)
    )
    targets: List[gi.SolidPartInfo] = []
    for suffix in req_parts:
        name = suffix.upper() if suffix.upper().startswith(car + "_") else f"{car}_{suffix.upper()}"
        part_obj = by_name.get(name)
        reason = eligibility(pack, part_obj, budget)
        entry: Dict[str, Any] = {"solid": name, "status": "ineligible" if reason else "smoothed", "reason": reason}
        if part_obj is not None:
            entry["orig_tris"] = part_obj.strip_poly_total
            entry["orig_bytes"] = _solid_len(pack, part_obj.chunk_offset)
        rep["parts"].append(entry)
        targets += [] if reason else [by_name[name]]
    if not targets:
        return None, {**rep, "failures": ["no_eligible_parts"]}
    decoded = gi.parse_geometry_pack(pack, decode_parts=[p.index for p in targets])[1]
    scales = (1.0, 0.72, 0.48) if budget.tier == "limit" else (1.0,)
    last_fail = ""
    new: Optional[bytes] = None
    stats: Dict[str, gc.CompileStats] = {}
    for sc in scales:
        repl = _build_replacements(pack, targets, decoded, budget, tri_budget_scale=sc)
        if not repl:
            last_fail = "no_curved_triangles_within_budget"
            continue
        try:
            cand, st = gc.replace_solids_in_pack(pack, repl, max_solid_bytes=budget.max_solid_bytes,
                                                 max_pack_bytes=budget.pack_cap(len(pack)), auto_coarse_scale=False)
        except ValueError as exc:
            last_fail = f"compile_budget: {exc}"
            continue
        cand = restore_authored_aabb(pack, cand, list(repl))
        vrep = validate_pack(pack, cand, {n: s.strip_polys for n, s in st.items()}, budget)
        if vrep["ok"] or budget.tier != "limit":
            new, stats = cand, st
            rep.update(vrep)
            break
        last_fail = "; ".join(vrep["failures"])
        # Only retry if the failure was a byte/tri/vert budget overrun
        if not all(f.startswith(("pack_bytes", "car_added_", "compile_budget")) or "solid_bytes" in f for f in vrep["failures"]):
            new, stats = cand, st
            rep.update(vrep)
            break
    if new is None:
        return None, {**rep, "failures": [last_fail or "compile_budget_exceeded"]}
    for pe in rep["parts"]:
        st_p = stats.get(pe["solid"])
        if st_p is not None:
            pe["new_tris"] = st_p.strip_polys
            pe["added_tris"] = st_p.strip_polys - pe.get("orig_tris", 0)
            pe["new_bytes"] = st_p.solid_bytes
        elif pe["status"] == "smoothed":
            pe["status"], pe["reason"] = "skipped_flat", "no_curved_tris_above_threshold"
    return (new if rep["ok"] else None), rep

def resolve_iso(cli: Optional[str] = None) -> Optional[Path]:
    raw = cli or os.environ.get("NFSMW_ISO", "").strip()
    p = Path(raw).expanduser() if raw else DEFAULT_ISO
    return p if p.is_file() else None

def run(iso: Path, cars: Sequence[str], out_dir: Path, *, parts: Optional[Sequence[str]] = None,
        budget: Optional[CarBudget] = None, write: bool = True) -> Dict[str, Any]:
    """Opt-in run over explicitly named cars; only validated cars are promoted into ``out_dir``."""
    before = iso.stat()
    budget = budget or CarBudget()
    arc = PS2NFSMWArchive(iso)
    entries = {(e.resolved_path or "").upper(): e for e in arc.entries}
    reports, overrides = [], {}
    for car in (c.upper() for c in cars):
        vp = f"CARS\\{car}\\GEOMETRY.BIN"
        if vp not in entries:
            reports.append({"car": car, "ok": False, "failures": ["missing_in_zdir"]})
            continue
        new, rep = smooth_car_pack(arc.read_entry(entries[vp]), car, parts, budget)
        rep["promoted"] = bool(new is not None and write)
        if rep["promoted"]:
            dest = out_dir / "CARS" / car / "GEOMETRY.BIN"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(new)
            overrides[vp] = dest.relative_to(out_dir).as_posix()
        reports.append(rep)
    if (before.st_size, before.st_mtime_ns) != (iso.stat().st_size, iso.stat().st_mtime_ns):
        raise RuntimeError("source ISO changed during run; aborting")
    manifest = {"schema": "rockport.optin_cars.v1", "tier": budget.tier, "iso_name": iso.name,
                "overrides": overrides, "cars": reports}
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "optin_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iso", help="Untouched SLUS-21351 ISO (or NFSMW_ISO); opened read-only")
    ap.add_argument("--car", action="append", default=[], help="Car to smooth (repeatable); nothing runs by default")
    ap.add_argument("--hero-preset", action="store_true", help="Curated 4 hero cars: " + ", ".join(HERO_CARS))
    ap.add_argument("--blacklist-preset", action="store_true", help=f"Curated {len(BLACKLIST_PRESET)} Blacklist/hero/cop cars")
    ap.add_argument("--tier", choices=("standard", "limit"), default="standard",
                    help="standard (v2.3 uniform) or limit (v2.6 curvature-selective 3D body+wheel smoothing)")
    ap.add_argument("--part", action="append", default=None, help="Part suffix override, e.g. KIT00_FRONT_TIRE_B")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--validate-only", action="store_true", help="Validate without writing anything")
    args = ap.parse_args(argv)
    cars = list(dict.fromkeys(
        [c.upper() for c in args.car]
        + (list(HERO_CARS) if args.hero_preset else [])
        + (list(BLACKLIST_PRESET) if args.blacklist_preset else [])
    ))
    iso = resolve_iso(args.iso) if cars else None
    if iso is None:
        print("error: " + ("ISO not found (pass --iso or set NFSMW_ISO)" if cars else
                           "opt-in only; pass --car <NAME>, --hero-preset, or --blacklist-preset"), file=sys.stderr)
        return 2
    budget = LIMIT_BUDGET if args.tier == "limit" else CarBudget()
    out_dir = args.out or (DEFAULT_OUT_DIR_V26 if args.tier == "limit" else DEFAULT_OUT_DIR)
    m = run(iso, cars, out_dir, parts=args.part, budget=budget, write=not args.validate_only)
    for r in m["cars"]:
        parts = ", ".join(
            f"{p['solid'].split('_', 1)[-1]}={p['orig_tris']}->{p['new_tris']}"
            if p.get("new_tris") is not None else f"{p['solid'].split('_', 1)[-1]}={p['reason'] or p['status']}"
            for p in r.get("parts", [])
        )
        print(f"[optin:{args.tier}] {r['car']}: {'PASS' if r['ok'] else 'FAIL'} promoted={r.get('promoted', False)} "
              f"bytes={r.get('pack_bytes', '-')}/{r.get('pack_cap', '-')} +tris={r.get('added_tris', '-')} [{parts}]")
        print("".join(f"    - {f}\n" for f in r.get("failures", [])), end="")
    return 0 if all(r["ok"] for r in m["cars"]) else 1

if __name__ == "__main__":
    raise SystemExit(main())
