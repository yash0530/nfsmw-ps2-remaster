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
DEFAULT_ISO = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
RETAIL_MAX_CAR_PACK = 3_090_112  # CARS\SL500\GEOMETRY.BIN, largest stock car pack (WHEELS excluded)
RETAIL_MAX_SOLID = 171_024       # largest stock SolidPack in any car pack (COBALTSS)
SHARED_PACKS = frozenset({"BRAKES", "ROOF", "SPOILER", "SPOILER_CARRERA", "SPOILER_HATCH", "SPOILER_PORSCHES", "WHEELS"})
HERO_CARS: Tuple[str, ...] = ("BMWM3GTRE46", "CARRERAGT", "GALLARDO", "MUSTANGGT")
HERO_PARTS: Tuple[str, ...] = ("KIT00_BODY_B", "KIT00_FRONT_TIRE_B")
HUB_KEYS = ("TIRE", "WHEEL", "RIM", "BRAKE")
HUB_TOL, AABB_TOL = 0.001, 0.0005  # metres
_MASKED = frozenset([0x14, 0x15, 0x19, 0x1A])  # eSolid poly count + tex/mat counts; all else must match

@dataclass(frozen=True)
class CarBudget:
    """Hard per-car caps; stored bytes == on-disc bytes (car packs sit raw in ZZDATA)."""
    max_pack_bytes: int = RETAIL_MAX_CAR_PACK
    max_pack_growth: float = 1.35
    max_solid_bytes: int = RETAIL_MAX_SOLID
    max_solid_tris: int = 8192
    max_added_tris: int = 6144
    max_added_verts: int = 8192

    def pack_cap(self, orig_len: int) -> int:
        # Allow a 32 KiB floor only for tiny synthetic test packs; real retail cars (>100 KiB) always use max_pack_growth.
        return min(self.max_pack_bytes, max(int(orig_len * self.max_pack_growth), orig_len + 32_768))

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

def eligibility(data: bytes, part: Optional[gi.SolidPartInfo], budget: CarBudget) -> Optional[str]:
    """Pre-compile gate; returns a rejection reason or None."""
    if part is None:
        return "part_not_found"
    if not part.name.upper().endswith("_B"):
        return "not_lod_b"  # LOD A = frontend/showroom decals, C-E = distance LODs
    if _damage_ids(_spans(data, part.chunk_offset)):
        return "damage_morph_tables_index_original_strips"
    if scm.adaptive_part_subdivision_params(part.name)[1] == 0:
        return "part_class_not_smoothed"
    if part.strip_poly_total <= 0 or part.strip_poly_total * 4 > budget.max_solid_tris:
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
    if _damage_ids(os_) != _damage_ids(ns):
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

def smooth_car_pack(pack: bytes, car: str, parts: Sequence[str] = HERO_PARTS,
                    budget: CarBudget = CarBudget()) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """Smooth eligible named parts of one car pack; returns (bytes only if validated, report)."""
    car = car.upper()
    rep: Dict[str, Any] = {"car": car, "orig_pack_bytes": len(pack), "budget": asdict(budget), "parts": [], "ok": False}
    if car in SHARED_PACKS:
        return None, {**rep, "failures": ["shared_pack_cached_in_globalmemoryfile"]}
    by_name = {p.name.upper(): p for p in gi.parse_geometry_pack(pack)[0].parts}
    targets: List[gi.SolidPartInfo] = []
    for suffix in parts:
        name = suffix.upper() if suffix.upper().startswith(car + "_") else f"{car}_{suffix.upper()}"
        reason = eligibility(pack, by_name.get(name), budget)
        rep["parts"].append({"solid": name, "status": "ineligible" if reason else "smoothed", "reason": reason})
        targets += [] if reason else [by_name[name]]
    if not targets:
        return None, {**rep, "failures": ["no_eligible_parts"]}
    decoded = gi.parse_geometry_pack(pack, decode_parts=[p.index for p in targets])[1]
    repl: Dict[str, gc.CompiledMesh] = {}
    for p in targets:
        m, (crease, _lv, lock) = decoded[p.index], scm.adaptive_part_subdivision_params(p.name)
        src = gc.CompiledMesh(list(m.vertices), list(m.normals), list(m.uvs), [t for t in m.triangles if len(set(t[:3])) == 3])
        sub, _ = scm.subdivide_compiled_mesh(src, crease_deg=crease, mode="pn", levels=1, lock_material_seams=lock)
        sub.triangles = [t for t in sub.triangles if len(set(t[:3])) == 3]
        repl[p.name] = sub
    try:
        new, stats = gc.replace_solids_in_pack(pack, repl, max_solid_bytes=budget.max_solid_bytes,
                                               max_pack_bytes=budget.pack_cap(len(pack)), auto_coarse_scale=False)
    except ValueError as exc:
        return None, {**rep, "failures": [f"compile_budget: {exc}"]}
    new = restore_authored_aabb(pack, new, list(repl))
    rep.update(validate_pack(pack, new, {n: s.strip_polys for n, s in stats.items()}, budget))
    return (new if rep["ok"] else None), rep

def resolve_iso(cli: Optional[str] = None) -> Optional[Path]:
    raw = cli or os.environ.get("NFSMW_ISO", "").strip()
    p = Path(raw).expanduser() if raw else DEFAULT_ISO
    return p if p.is_file() else None

def run(iso: Path, cars: Sequence[str], out_dir: Path, *, parts: Sequence[str] = HERO_PARTS,
        budget: CarBudget = CarBudget(), write: bool = True) -> Dict[str, Any]:
    """Opt-in run over explicitly named cars; only validated cars are promoted into ``out_dir``."""
    before = iso.stat()
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
    manifest = {"schema": "rockport.optin_cars.v1", "iso_name": iso.name, "overrides": overrides, "cars": reports}
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "optin_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iso", help="Untouched SLUS-21351 ISO (or NFSMW_ISO); opened read-only")
    ap.add_argument("--car", action="append", default=[], help="Car to smooth (repeatable); nothing runs by default")
    ap.add_argument("--hero-preset", action="store_true", help="Curated: " + ", ".join(HERO_CARS))
    ap.add_argument("--part", action="append", default=None, help="Part suffix override, e.g. KIT00_FRONT_TIRE_B")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--validate-only", action="store_true", help="Validate without writing anything")
    args = ap.parse_args(argv)
    cars = list(dict.fromkeys([c.upper() for c in args.car] + (list(HERO_CARS) if args.hero_preset else [])))
    iso = resolve_iso(args.iso) if cars else None
    if iso is None:
        print("error: " + ("ISO not found (pass --iso or set NFSMW_ISO)" if cars else
                           "opt-in only; pass --car <NAME> or --hero-preset"), file=sys.stderr)
        return 2
    m = run(iso, cars, args.out, parts=args.part or HERO_PARTS, write=not args.validate_only)
    for r in m["cars"]:
        parts = ", ".join(f"{p['solid'].split('_', 1)[-1]}={p['reason'] or 'smoothed'}" for p in r.get("parts", []))
        print(f"[optin] {r['car']}: {'PASS' if r['ok'] else 'FAIL'} promoted={r.get('promoted', False)} "
              f"bytes={r.get('pack_bytes', '-')}/{r.get('pack_cap', '-')} +tris={r.get('added_tris', '-')} [{parts}]")
        print("".join(f"    - {f}\n" for f in r.get("failures", [])), end="")
    return 0 if all(r["ok"] for r in m["cars"]) else 1

if __name__ == "__main__":
    raise SystemExit(main())
