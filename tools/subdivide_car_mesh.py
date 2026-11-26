#!/usr/bin/env python3
"""subdivide_car_mesh.py — Crease-aware PN-Triangle & Loop mesh smoother for PS2 NFSMW cars.

Subdivides extracted PS2 car `_B` meshes (`~3,640` tris -> `~12K-14.5K` smooth tris)
while preserving sharp panel seams, fender lips, and window borders using dihedral
crease detection (default threshold 38 deg), UV island boundaries, material groups
(`usemtl`), and exact part AABB bounds.

Pure Python 3 standard library (zero third-party dependencies).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from geometry_compiler import CompiledMesh, export_rich_obj, load_obj_mesh  # noqa: E402


# ---------------------------------------------------------------------------
# Vector Math Helpers
# ---------------------------------------------------------------------------


Vec3 = Tuple[float, float, float]
Vec2 = Tuple[float, float]
PosKey = Tuple[int, int, int]


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _len(a: Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _norm(a: Vec3, fallback: Vec3 = (0.0, 0.0, 1.0)) -> Vec3:
    l = _len(a)
    if l > 1e-12:
        return (a[0] / l, a[1] / l, a[2] / l)
    return fallback


def _pos_key(p: Vec3, inv_eps: float = 5000.0) -> PosKey:
    """Quantize 3D position onto a 0.2 mm grid (matching PS2 0.0002 m scale)."""
    return (round(p[0] * inv_eps), round(p[1] * inv_eps), round(p[2] * inv_eps))


# ---------------------------------------------------------------------------
# Statistics & Crease Topology Analysis
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SubdivideStats:
    mode: str
    levels: int
    crease_deg: float
    input_vertices: int
    input_triangles: int
    output_vertices: int
    output_triangles: int
    smooth_edges: int
    sharp_edges: int
    boundary_edges: int
    aabb_min: List[float]
    aabb_max: List[float]


def adaptive_part_subdivision_params(part_name: str) -> Tuple[float, int, bool]:
    """Return adaptive (crease_deg, levels, lock_material_seams) for a PS2 car solid part.

    Small curved trim pieces (mirrors, spoilers, hoods) have 20-38 deg facets on PS2
    meshes that stay flat under a global 18 deg crease lock; relaxing their dihedral
    threshold to 42 deg at L2 while locking material seams smooths mirror/arch glints
    without rounding crisp trim boundaries.
    """
    pn = part_name.upper()
    if any(k in pn for k in ("WINDOW", "GLASS", "HEADLIGHT", "BRAKELIGHT", "INTERIOR", "BRAKE")):
        return (18.0, 0, True)
    if any(k in pn for k in ("MIRROR", "SPOILER", "HOOD")):
        return (42.0, 2, True)
    if any(k in pn for k in ("WHEEL", "TIRE", "ENKEI", "BBS")):
        return (40.0, 1, True)
    if any(k in pn for k in ("BODY", "BASE")):
        return (28.0, 1, True)
    return (28.0, 1, True)


def _analyze_topology(
    mesh: CompiledMesh,
    crease_deg: float,
    inv_eps: float = 5000.0,
    lock_material_seams: bool = False,
    lock_uv_seams: bool = False,
) -> Tuple[
    List[PosKey],
    Dict[PosKey, Vec3],
    List[Vec3],
    Dict[Tuple[PosKey, PosKey], List[int]],
    Set[Tuple[PosKey, PosKey]],
    Dict[Tuple[int, int], Vec3],
    int,
    int,
    int,
]:
    """Build spatial vertex clusters, face normals, sharp edges, and crease-split smooth normals."""
    vert_pkeys = [_pos_key(v, inv_eps) for v in mesh.vertices]

    # Representative exact spatial coordinates (averaged per cluster to remove tiny noise)
    pk_sums: Dict[PosKey, List[float]] = {}
    pk_cnts: Dict[PosKey, int] = {}
    for pk, (x, y, z) in zip(vert_pkeys, mesh.vertices):
        if pk not in pk_sums:
            pk_sums[pk] = [x, y, z]
            pk_cnts[pk] = 1
        else:
            s = pk_sums[pk]
            s[0] += x
            s[1] += y
            s[2] += z
            pk_cnts[pk] += 1
    pk_pos: Dict[PosKey, Vec3] = {
        pk: (s[0] / pk_cnts[pk], s[1] / pk_cnts[pk], s[2] / pk_cnts[pk])
        for pk, s in pk_sums.items()
    }

    tri_fnorms: List[Vec3] = []
    tri_areas: List[float] = []
    tri_mats: List[Tuple[int, int]] = []
    edge_tris: Dict[Tuple[PosKey, PosKey], List[int]] = defaultdict(list)
    edge_uvs: Dict[Tuple[PosKey, PosKey], List[Tuple[Tuple[int, int], Tuple[int, int]]]] = defaultdict(list)
    pk_tris: Dict[PosKey, List[int]] = defaultdict(list)

    for ti, (i0, i1, i2, tx, lm) in enumerate(mesh.triangles):
        pk0, pk1, pk2 = vert_pkeys[i0], vert_pkeys[i1], vert_pkeys[i2]
        p0, p1, p2 = pk_pos[pk0], pk_pos[pk1], pk_pos[pk2]
        cr = _cross(_sub(p1, p0), _sub(p2, p0))
        area2 = _len(cr)
        tri_areas.append(area2)
        tri_fnorms.append((cr[0] / area2, cr[1] / area2, cr[2] / area2) if area2 > 1e-12 else (0.0, 0.0, 1.0))
        tri_mats.append((tx, lm))
        uv0 = mesh.uvs[i0] if i0 < len(mesh.uvs) else (0.0, 0.0)
        uv1 = mesh.uvs[i1] if i1 < len(mesh.uvs) else (0.0, 0.0)
        uv2 = mesh.uvs[i2] if i2 < len(mesh.uvs) else (0.0, 0.0)
        for (a, uva), (b, uvb) in (((pk0, uv0), (pk1, uv1)), ((pk1, uv1), (pk2, uv2)), ((pk2, uv2), (pk0, uv0))):
            if a != b:
                if a <= b:
                    ek = (a, b)
                    uv_pair = ((round(uva[0] * 1024), round(uva[1] * 1024)), (round(uvb[0] * 1024), round(uvb[1] * 1024)))
                else:
                    ek = (b, a)
                    uv_pair = ((round(uvb[0] * 1024), round(uvb[1] * 1024)), (round(uva[0] * 1024), round(uva[1] * 1024)))
                edge_tris[ek].append(ti)
                if lock_uv_seams:
                    edge_uvs[ek].append(uv_pair)
        pk_tris[pk0].append(ti)
        pk_tris[pk1].append(ti)
        pk_tris[pk2].append(ti)

    cos_thresh = math.cos(math.radians(crease_deg))
    sharp_edges: Set[Tuple[PosKey, PosKey]] = set()
    smooth_cnt = 0
    sharp_cnt = 0
    boundary_cnt = 0

    # Also track smooth adjacency between triangles sharing a non-sharp edge at each vertex
    smooth_adj: Dict[Tuple[int, PosKey], List[int]] = defaultdict(list)

    for ek, ts in edge_tris.items():
        if len(ts) == 1:
            sharp_edges.add(ek)
            boundary_cnt += 1
        elif len(ts) == 2:
            t0, t1 = ts[0], ts[1]
            d = max(-1.0, min(1.0, _dot(tri_fnorms[t0], tri_fnorms[t1])))
            mat_seam = lock_material_seams and (tri_mats[t0] != tri_mats[t1])
            uv_seam = lock_uv_seams and len(edge_uvs[ek]) == 2 and (edge_uvs[ek][0] != edge_uvs[ek][1])
            if d < cos_thresh or mat_seam or uv_seam:
                sharp_edges.add(ek)
                sharp_cnt += 1
            else:
                smooth_cnt += 1
                smooth_adj[(t0, ek[0])].append(t1)
                smooth_adj[(t1, ek[0])].append(t0)
                smooth_adj[(t0, ek[1])].append(t1)
                smooth_adj[(t1, ek[1])].append(t0)
        else:
            sharp_edges.add(ek)
            sharp_cnt += 1

    # Compute area-weighted smooth fan normal for every (tri_idx, corner_slot 0..2)
    fan_cache: Dict[Tuple[int, PosKey], Vec3] = {}
    corner_normals: Dict[Tuple[int, int], Vec3] = {}
    for ti, (i0, i1, i2, _tx, _lm) in enumerate(mesh.triangles):
        pks = (vert_pkeys[i0], vert_pkeys[i1], vert_pkeys[i2])
        for slot, pk in enumerate(pks):
            if (ti, pk) not in fan_cache:
                # BFS across smooth edges incident to pk
                visited = {ti}
                stack = [ti]
                acc = [0.0, 0.0, 0.0]
                while stack:
                    cur = stack.pop()
                    w = max(tri_areas[cur], 1e-8)
                    fn = tri_fnorms[cur]
                    acc[0] += fn[0] * w
                    acc[1] += fn[1] * w
                    acc[2] += fn[2] * w
                    for nxt in smooth_adj.get((cur, pk), ()):
                        if nxt not in visited:
                            visited.add(nxt)
                            stack.append(nxt)
                n_sm = _norm((acc[0], acc[1], acc[2]), fallback=tri_fnorms[ti])
                for v_ti in visited:
                    fan_cache[(v_ti, pk)] = n_sm
            corner_normals[(ti, slot)] = fan_cache[(ti, pk)]

    return (
        vert_pkeys,
        pk_pos,
        tri_fnorms,
        edge_tris,
        sharp_edges,
        corner_normals,
        smooth_cnt,
        sharp_cnt,
        boundary_cnt,
    )


def _subdivide_once(
    mesh: CompiledMesh,
    *,
    crease_deg: float = 38.0,
    mode: str = "pn",
    preserve_aabb: bool = True,
    lock_material_seams: bool = False,
    lock_uv_seams: bool = False,
) -> Tuple[CompiledMesh, int, int, int]:
    """Perform one level of crease-aware 1-to-4 triangle subdivision."""
    if mode not in ("pn", "loop"):
        raise ValueError(f"Unsupported subdivision mode '{mode}'; expected 'pn' or 'loop'")

    (
        vert_pkeys,
        pk_pos,
        tri_fnorms,
        edge_tris,
        sharp_edges,
        corner_normals,
        smooth_cnt,
        sharp_cnt,
        boundary_cnt,
    ) = _analyze_topology(
        mesh,
        crease_deg=crease_deg,
        lock_material_seams=lock_material_seams,
        lock_uv_seams=lock_uv_seams,
    )

    orig_xs = [v[0] for v in mesh.vertices]
    orig_ys = [v[1] for v in mesh.vertices]
    orig_zs = [v[2] for v in mesh.vertices]
    aabb_min = (min(orig_xs), min(orig_ys), min(orig_zs))
    aabb_max = (max(orig_xs), max(orig_ys), max(orig_zs))

    # For Loop mode, compute updated base vertex positions per spatial vertex pk
    pk_new_pos: Dict[PosKey, Vec3] = dict(pk_pos)
    if mode == "loop":
        pk_neighbors: Dict[PosKey, Set[PosKey]] = defaultdict(set)
        pk_crease_neighbors: Dict[PosKey, List[PosKey]] = defaultdict(list)
        for a, b in edge_tris:
            pk_neighbors[a].add(b)
            pk_neighbors[b].add(a)
            ek = (a, b) if a <= b else (b, a)
            if ek in sharp_edges:
                pk_crease_neighbors[a].append(b)
                pk_crease_neighbors[b].append(a)

        for pk, p in pk_pos.items():
            cn = pk_crease_neighbors.get(pk, [])
            if len(cn) == 2:
                # Hoppe et al. crease vertex rule (3/4, 1/8, 1/8)
                p_l, p_r = pk_pos[cn[0]], pk_pos[cn[1]]
                pk_new_pos[pk] = (
                    0.75 * p[0] + 0.125 * (p_l[0] + p_r[0]),
                    0.75 * p[1] + 0.125 * (p_l[1] + p_r[1]),
                    0.75 * p[2] + 0.125 * (p_l[2] + p_r[2]),
                )
            elif len(cn) >= 1:
                # Corner / dart vertex stays exact
                pk_new_pos[pk] = p
            else:
                nbrs = list(pk_neighbors.get(pk, ()))
                n = len(nbrs)
                if n >= 3:
                    beta = 3.0 / 16.0 if n == 3 else 3.0 / (8.0 * n)
                    sx = sum(pk_pos[nb][0] for nb in nbrs)
                    sy = sum(pk_pos[nb][1] for nb in nbrs)
                    sz = sum(pk_pos[nb][2] for nb in nbrs)
                    w0 = 1.0 - n * beta
                    pk_new_pos[pk] = (
                        w0 * p[0] + beta * sx,
                        w0 * p[1] + beta * sy,
                        w0 * p[2] + beta * sz,
                    )

    # Precompute Loop smooth edge midpoints when mode == "loop"
    loop_edge_mid: Dict[Tuple[PosKey, PosKey], Vec3] = {}
    if mode == "loop":
        for ek, ts in edge_tris.items():
            pa, pb = pk_pos[ek[0]], pk_pos[ek[1]]
            if ek in sharp_edges or len(ts) != 2:
                loop_edge_mid[ek] = _scale(_add(pa, pb), 0.5)
            else:
                opp: List[Vec3] = []
                for t in ts:
                    i0, i1, i2, _, _ = mesh.triangles[t]
                    for vi in (i0, i1, i2):
                        pk = vert_pkeys[vi]
                        if pk != ek[0] and pk != ek[1]:
                            opp.append(pk_pos[pk])
                            break
                if len(opp) == 2:
                    loop_edge_mid[ek] = (
                        0.375 * (pa[0] + pb[0]) + 0.125 * (opp[0][0] + opp[1][0]),
                        0.375 * (pa[1] + pb[1]) + 0.125 * (opp[0][1] + opp[1][1]),
                        0.375 * (pa[2] + pb[2]) + 0.125 * (opp[0][2] + opp[1][2]),
                    )
                else:
                    loop_edge_mid[ek] = _scale(_add(pa, pb), 0.5)

    out_v: List[Vec3] = []
    out_vn: List[Vec3] = []
    out_vt: List[Vec2] = []
    out_tris: List[Tuple[int, int, int, int, int]] = []

    def _clamp_pos(p: Vec3) -> Vec3:
        if not preserve_aabb:
            return p
        return (
            max(aabb_min[0], min(aabb_max[0], p[0])),
            max(aabb_min[1], min(aabb_max[1], p[1])),
            max(aabb_min[2], min(aabb_max[2], p[2])),
        )

    def _pn_edge_midpoint(
        pa: Vec3, pb: Vec3, na: Vec3, nb: Vec3, ek: Tuple[PosKey, PosKey]
    ) -> Vec3:
        mid = _scale(_add(pa, pb), 0.5)
        if ek in sharp_edges:
            return mid
        edge_vec = _sub(pb, pa)
        elen = _len(edge_vec)
        if elen < 1e-9:
            return mid
        w_ab = _dot(edge_vec, na)
        w_ba = -_dot(edge_vec, nb)
        delta = (
            -0.125 * (w_ab * na[0] + w_ba * nb[0]),
            -0.125 * (w_ab * na[1] + w_ba * nb[1]),
            -0.125 * (w_ab * na[2] + w_ba * nb[2]),
        )
        # Prevent sliver-triangle over-bulging (> 25% of edge length)
        dlen = _len(delta)
        max_bulge = 0.25 * elen
        if dlen > max_bulge and dlen > 1e-12:
            delta = _scale(delta, max_bulge / dlen)
        return _clamp_pos(_add(mid, delta))

    # Vertex deduplication cache keyed on (pos_key_or_edge, normal_q, uv_q)
    vert_cache: Dict[Tuple[object, Tuple[int, int, int], Tuple[int, int]], int] = {}

    def _emit_vert(tag: object, pos: Vec3, nrm: Vec3, uv: Vec2) -> int:
        p_c = _clamp_pos(pos)
        n_c = _norm(nrm)
        nq = (round(n_c[0] * 127.0), round(n_c[1] * 127.0), round(n_c[2] * 127.0))
        uvq = (round(uv[0] * 4096.0), round(uv[1] * 4096.0))
        key = (tag, nq, uvq)
        idx = vert_cache.get(key)
        if idx is not None:
            return idx
        idx = len(out_v)
        out_v.append(p_c)
        out_vn.append(n_c)
        out_vt.append(uv)
        vert_cache[key] = idx
        return idx

    for ti, (i0, i1, i2, tx, lm) in enumerate(mesh.triangles):
        pk0, pk1, pk2 = vert_pkeys[i0], vert_pkeys[i1], vert_pkeys[i2]
        p0, p1, p2 = pk_new_pos[pk0], pk_new_pos[pk1], pk_new_pos[pk2]
        n0 = corner_normals[(ti, 0)]
        n1 = corner_normals[(ti, 1)]
        n2 = corner_normals[(ti, 2)]
        uv0, uv1, uv2 = mesh.uvs[i0], mesh.uvs[i1], mesh.uvs[i2]

        ek01 = (pk0, pk1) if pk0 <= pk1 else (pk1, pk0)
        ek12 = (pk1, pk2) if pk1 <= pk2 else (pk2, pk1)
        ek20 = (pk2, pk0) if pk2 <= pk0 else (pk0, pk2)

        if mode == "pn":
            m01 = _pn_edge_midpoint(p0, p1, n0, n1, ek01)
            m12 = _pn_edge_midpoint(p1, p2, n1, n2, ek12)
            m20 = _pn_edge_midpoint(p2, p0, n2, n0, ek20)
        else:
            m01 = loop_edge_mid.get(ek01, _scale(_add(p0, p1), 0.5))
            m12 = loop_edge_mid.get(ek12, _scale(_add(p1, p2), 0.5))
            m20 = loop_edge_mid.get(ek20, _scale(_add(p2, p0), 0.5))

        nm01 = _norm(_add(n0, n1), fallback=tri_fnorms[ti])
        nm12 = _norm(_add(n1, n2), fallback=tri_fnorms[ti])
        nm20 = _norm(_add(n2, n0), fallback=tri_fnorms[ti])

        uvm01 = (0.5 * (uv0[0] + uv1[0]), 0.5 * (uv0[1] + uv1[1]))
        uvm12 = (0.5 * (uv1[0] + uv2[0]), 0.5 * (uv1[1] + uv2[1]))
        uvm20 = (0.5 * (uv2[0] + uv0[0]), 0.5 * (uv2[1] + uv0[1]))

        v0 = _emit_vert(("v", pk0), p0, n0, uv0)
        v1 = _emit_vert(("v", pk1), p1, n1, uv1)
        v2 = _emit_vert(("v", pk2), p2, n2, uv2)
        e01 = _emit_vert(("e", ek01), m01, nm01, uvm01)
        e12 = _emit_vert(("e", ek12), m12, nm12, uvm12)
        e20 = _emit_vert(("e", ek20), m20, nm20, uvm20)

        out_tris.append((v0, e01, e20, tx, lm))
        out_tris.append((e01, v1, e12, tx, lm))
        out_tris.append((e20, e12, v2, tx, lm))
        out_tris.append((e01, e12, e20, tx, lm))

    return (
        CompiledMesh(
            vertices=out_v,
            normals=out_vn,
            uvs=out_vt,
            triangles=out_tris,
            group_meta=dict(mesh.group_meta),
        ),
        smooth_cnt,
        sharp_cnt,
        boundary_cnt,
    )


def subdivide_compiled_mesh(
    mesh: CompiledMesh,
    *,
    crease_deg: float = 38.0,
    mode: str = "pn",
    levels: int = 1,
    preserve_aabb: bool = True,
    lock_material_seams: bool = False,
    lock_uv_seams: bool = False,
) -> Tuple[CompiledMesh, SubdivideStats]:
    """Subdivide a CompiledMesh `levels` times with crease-aware smoothing."""
    if levels < 1 or levels > 3:
        raise ValueError(f"levels must be in [1, 3], got {levels}")

    cur = mesh
    smooth_cnt = sharp_cnt = boundary_cnt = 0
    for _ in range(levels):
        cur, sm, sh, bd = _subdivide_once(
            cur,
            crease_deg=crease_deg,
            mode=mode,
            preserve_aabb=preserve_aabb,
            lock_material_seams=lock_material_seams,
            lock_uv_seams=lock_uv_seams,
        )
        smooth_cnt += sm
        sharp_cnt += sh
        boundary_cnt += bd

    xs = [v[0] for v in cur.vertices]
    ys = [v[1] for v in cur.vertices]
    zs = [v[2] for v in cur.vertices]
    stats = SubdivideStats(
        mode=mode,
        levels=levels,
        crease_deg=crease_deg,
        input_vertices=len(mesh.vertices),
        input_triangles=len(mesh.triangles),
        output_vertices=len(cur.vertices),
        output_triangles=len(cur.triangles),
        smooth_edges=smooth_cnt,
        sharp_edges=sharp_cnt,
        boundary_edges=boundary_cnt,
        aabb_min=[round(min(xs), 6), round(min(ys), 6), round(min(zs), 6)],
        aabb_max=[round(max(xs), 6), round(max(ys), 6), round(max(zs), 6)],
    )
    return cur, stats


def subdivide_obj_file(
    in_obj: Path,
    out_obj: Path,
    *,
    crease_deg: float = 38.0,
    mode: str = "pn",
    levels: int = 1,
    preserve_aabb: bool = True,
    part_name: Optional[str] = None,
    lock_material_seams: bool = False,
    lock_uv_seams: bool = False,
) -> SubdivideStats:
    """Load an OBJ mesh, apply crease-aware subdivision, and write the subdivided OBJ."""
    mesh = load_obj_mesh(in_obj)
    sub_mesh, stats = subdivide_compiled_mesh(
        mesh,
        crease_deg=crease_deg,
        mode=mode,
        levels=levels,
        preserve_aabb=preserve_aabb,
        lock_material_seams=lock_material_seams,
        lock_uv_seams=lock_uv_seams,
    )
    name = part_name or in_obj.stem
    export_rich_obj(out_obj, name, sub_mesh, group_meta=sub_mesh.group_meta)  # type: ignore[arg-type]
    return stats


def smooth_pbr_obj_inplace(
    obj_path: Path,
    *,
    crease_deg: Optional[float] = None,
    levels: Optional[int] = None,
    lock_material_seams: Optional[bool] = None,
) -> SubdivideStats:
    """Apply seam-aware adaptive PN subdivision to a Y-up PBR Wavefront OBJ preserving usemtl names."""
    part_name = obj_path.stem
    auto_crease, auto_levels, auto_lock = adaptive_part_subdivision_params(part_name)
    c_deg = auto_crease if crease_deg is None else crease_deg
    lvls = auto_levels if levels is None else levels
    lock_m = auto_lock if lock_material_seams is None else lock_material_seams

    verts: List[Vec3] = []
    uvs: List[Vec2] = []
    norms: List[Vec3] = []
    mtl_names: List[str] = []
    mtl_to_id: Dict[str, int] = {}
    mtllib_line: Optional[str] = None
    cur_mtl = "car_paint_hero"
    tri_raw: List[Tuple[int, int, int, int]] = []
    vert_map: Dict[Tuple[int, int, int], int] = {}
    out_verts: List[Vec3] = []
    out_uvs: List[Vec2] = []
    out_norms: List[Vec3] = []

    def _get_mtl(name: str) -> int:
        if name not in mtl_to_id:
            mtl_to_id[name] = len(mtl_names)
            mtl_names.append(name)
        return mtl_to_id[name]

    def _get_v(vi: int, vti: int, vni: int) -> int:
        k = (vi, vti, vni)
        idx = vert_map.get(k)
        if idx is not None:
            return idx
        idx = len(out_verts)
        out_verts.append(verts[vi] if 0 <= vi < len(verts) else (0.0, 0.0, 0.0))
        out_uvs.append(uvs[vti] if 0 <= vti < len(uvs) else (0.0, 0.0))
        out_norms.append(norms[vni] if 0 <= vni < len(norms) else (0.0, 1.0, 0.0))
        vert_map[k] = idx
        return idx

    for line in obj_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        head = parts[0]
        if head == "mtllib":
            mtllib_line = s
        elif head == "v" and len(parts) >= 4:
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif head == "vt" and len(parts) >= 3:
            uvs.append((float(parts[1]), float(parts[2])))
        elif head == "vn" and len(parts) >= 4:
            norms.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif head == "usemtl" and len(parts) >= 2:
            cur_mtl = parts[1]
        elif head == "f" and len(parts) >= 4:
            mid = _get_mtl(cur_mtl)
            fvs: List[int] = []
            for tok in parts[1:]:
                comps = tok.split("/")
                vi = int(comps[0]) - 1 if comps[0] else -1
                vti = int(comps[1]) - 1 if len(comps) > 1 and comps[1] else -1
                vni = int(comps[2]) - 1 if len(comps) > 2 and comps[2] else -1
                fvs.append(_get_v(vi, vti, vni))
            for k in range(1, len(fvs) - 1):
                tri_raw.append((fvs[0], fvs[k], fvs[k + 1], mid))

    tris = [(a, b, c, 0, mid) for (a, b, c, mid) in tri_raw]
    compiled = CompiledMesh(vertices=out_verts, normals=out_norms, uvs=out_uvs, triangles=tris)
    if lvls <= 0 or not tris:
        xs = [v[0] for v in out_verts] or [0.0]
        ys = [v[1] for v in out_verts] or [0.0]
        zs = [v[2] for v in out_verts] or [0.0]
        return SubdivideStats("pn", 0, c_deg, len(out_verts), len(tris), len(out_verts), len(tris), 0, 0, 0,
                              [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)])

    sub, stats = subdivide_compiled_mesh(
        compiled,
        crease_deg=c_deg,
        mode="pn",
        levels=lvls,
        preserve_aabb=True,
        lock_material_seams=lock_m,
    )

    lines: List[str] = [
        f"# Seam-aware adaptive PN subdivided PBR mesh ({c_deg:.1f} deg, L{lvls}): {part_name}",
        f"# Vertices: {len(sub.vertices)}, Triangles: {len(sub.triangles)}",
    ]
    if mtllib_line:
        lines.append(mtllib_line)
    lines.append(f"o {part_name}")
    for x, y, z in sub.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    for u, v in sub.uvs:
        lines.append(f"vt {u:.6f} {v:.6f}")
    for nx, ny, nz in sub.normals:
        lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")

    sorted_tris = sorted(sub.triangles, key=lambda t: t[4])
    last_mid = -1
    for a, b, c, _tx, mid in sorted_tris:
        if mid != last_mid:
            last_mid = mid
            mname = mtl_names[mid] if 0 <= mid < len(mtl_names) else "car_paint_hero"
            lines.append(f"g {part_name}_{mname}")
            lines.append(f"usemtl {mname}")
        lines.append(f"f {a+1}/{a+1}/{a+1} {b+1}/{b+1}/{b+1} {c+1}/{c+1}/{c+1}")
    obj_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crease-aware PN-triangle / Loop mesh smoother for extracted PS2 car OBJ meshes."
    )
    parser.add_argument("--in", dest="in_obj", type=Path, required=True, help="Input Wavefront .obj")
    parser.add_argument("--out", dest="out_obj", type=Path, required=True, help="Output smoothed .obj")
    parser.add_argument("--mode", choices=("pn", "loop"), default="pn", help="Subdivision scheme (default: pn)")
    parser.add_argument("--levels", type=int, default=1, help="Subdivision iterations (default: 1)")
    parser.add_argument("--crease-deg", type=float, default=38.0, help="Dihedral crease threshold in degrees (default: 38.0)")
    parser.add_argument("--no-preserve-aabb", action="store_true", help="Allow vertices slightly outside original AABB")
    parser.add_argument("--part-name", type=str, default=None, help="Object name written into output OBJ")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    stats = subdivide_obj_file(
        args.in_obj,
        args.out_obj,
        crease_deg=args.crease_deg,
        mode=args.mode,
        levels=args.levels,
        preserve_aabb=not args.no_preserve_aabb,
        part_name=args.part_name,
    )
    if args.json:
        print(json.dumps(dataclasses.asdict(stats), indent=2))
    else:
        print(
            f"Subdivided ({stats.mode}, L={stats.levels}, crease={stats.crease_deg:.1f} deg): "
            f"{stats.input_triangles:,} -> {stats.output_triangles:,} tris "
            f"({stats.input_vertices:,} -> {stats.output_vertices:,} verts; "
            f"edges: smooth={stats.smooth_edges:,}, sharp={stats.sharp_edges:,}, "
            f"boundary={stats.boundary_edges:,}) -> {args.out_obj}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
