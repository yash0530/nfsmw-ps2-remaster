#!/usr/bin/env python3
"""geometry_compiler.py — Clean-room PS2 NFSMW 0x80134000 Geometry Pack Encoder & Repacker.

Inverse of tools/geometry_inspect.py for Need for Speed: Most Wanted - Black Edition
(PS2, SLUS-21351). Encodes triangles, normals, UVs, and material/texture bindings into
PS2 VU1/VIF DMA packets (4-blocked int16 positions, int16 UVs with odd-vertex swap,
4-blocked int8 normals, and exact GF(2)-solved VU1 RINIT/RNEXT 23-bit LFSR ADC seeds),
and repacks named solids inside existing GEOMETRY.BIN files while keeping all other
solids byte-intact and updating streaming offsets/sizes.

Pure Python 3 standard library (zero third-party dependencies).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import geometry_inspect as gi  # noqa: E402
from zdir_tool import PS2NFSMWArchive, bstringhash  # noqa: E402

# ---------------------------------------------------------------------------
# PS2 VU1 / EE Hardware Constants & Budget Guardrails
# ---------------------------------------------------------------------------

DEFAULT_SCALE = 0.0002
MAX_STRIP_VERTS = 30        # Verified retail SLUS-21351 max nv per VU1 strip batch
MAX_QOFF = 0xFFFF           # uint16 qword offset limit in eStripEntry (~1.04 MB VIF)
MAX_QSZ = 0xFF              # uint8 qword size limit per strip (4,080 B)
DEFAULT_MAX_POLYS = 32767   # Fits signed & unsigned 16-bit eSolid.num_polys cleanly
DEFAULT_MAX_SOLID_BYTES = 768 * 1024    # EE RAM guardrail per SolidPack (retail max ~373 KiB)
DEFAULT_MAX_PACK_BYTES = 5 * 1024 * 1024  # EE pack guardrail (retail WHEELS is 4.53 MB)

CHUNK_PADDING = gi.CHUNK_PADDING
CHUNK_GEOM_PACK = gi.CHUNK_GEOM_PACK
CHUNK_GEOM_INFO = gi.CHUNK_GEOM_INFO
CHUNK_GEOM_HEADER = gi.CHUNK_GEOM_HEADER
CHUNK_GEOM_KEYS = gi.CHUNK_GEOM_KEYS
CHUNK_GEOM_OFFSETS = gi.CHUNK_GEOM_OFFSETS
CHUNK_SOLID_PACK = gi.CHUNK_SOLID_PACK
CHUNK_ESOLID_HDR = gi.CHUNK_ESOLID_HDR
CHUNK_TEX_TABLE = gi.CHUNK_TEX_TABLE
CHUNK_MAT_TABLE = gi.CHUNK_MAT_TABLE
CHUNK_POS_MARKERS = gi.CHUNK_POS_MARKERS
CHUNK_MESH_CONT = gi.CHUNK_MESH_CONT
CHUNK_PLAT_INFO = gi.CHUNK_PLAT_INFO
CHUNK_STRIP_TABLE = gi.CHUNK_STRIP_TABLE
CHUNK_VIF_DATA = gi.CHUNK_VIF_DATA

DAMAGE_CHUNK_IDS = frozenset({0x00134017, 0x00134018, 0x00134019})
USEMTL_MAT_RE = re.compile(
    r"^mat_tx(?P<tx>-?\d+)_lm(?P<lm>-?\d+)(?:_fl(?P<fl>[0-9a-fA-F]+))?(?:_pg(?P<pg>\d+))?$"
)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class GroupMetadata:
    strip_flags: int = 0x4180
    poly_group: int = 0


@dataclasses.dataclass
class CompiledMesh:
    vertices: List[Tuple[float, float, float]]
    normals: List[Tuple[float, float, float]]
    uvs: List[Tuple[float, float]]
    triangles: List[Tuple[int, int, int, int, int]]  # (i0, i1, i2, tx, lm)
    group_meta: Dict[Tuple[int, int], GroupMetadata] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class CompileStats:
    part_name: str
    input_vertices: int
    input_triangles: int
    strip_count: int
    strip_vertices: int
    strip_polys: int
    vif_bytes: int
    solid_bytes: int
    pack_bytes: int
    aabb_min: List[float]
    aabb_max: List[float]


# ---------------------------------------------------------------------------
# Exact GF(2) VU1 RINIT / RNEXT 23-bit LFSR Seed Solver
# ---------------------------------------------------------------------------


def vu_rnext(r: int) -> int:
    """Advance PS2 VU1 23-bit LFSR R register by one RNEXT step."""
    new_bit = ((r >> 4) ^ (r >> 22)) & 1
    return ((r << 1) | new_bit) & 0x7FFFFF


def eval_vu_lfsr(r0: int, count: int) -> List[int]:
    """Generate `count` ADC bits starting from 23-bit seed `r0`."""
    out: List[int] = []
    r = r0 & 0x7FFFFF
    for _ in range(count):
        r = vu_rnext(r)
        out.append((r >> 15) & 1)
    return out


def _build_lfsr_basis_rows(steps: int) -> List[int]:
    """Build `steps x 23` GF(2) observation matrix rows for bit 15 of T^{t+1}(r0)."""
    rows = [0] * steps
    for bit_idx in range(23):
        r = 1 << bit_idx
        for t in range(steps):
            r = vu_rnext(r)
            if (r >> 15) & 1:
                rows[t] |= 1 << bit_idx
    return rows


_LFSR_BASIS_CACHE: Dict[int, List[int]] = {}


def _get_lfsr_basis_rows(steps: int) -> List[int]:
    cached = _LFSR_BASIS_CACHE.get(steps)
    if cached is None:
        cached = _build_lfsr_basis_rows(steps)
        _LFSR_BASIS_CACHE[steps] = cached
    return cached


def solve_lfsr_seed(bits: Sequence[int]) -> int:
    """Solve for a 23-bit RINIT seed `r0` whose RNEXT bit-15 sequence equals `bits`."""
    m = len(bits)
    if m == 0:
        return 0
    basis = _get_lfsr_basis_rows(m)
    # Augmented GF(2) matrix: each entry is [mask_23bit, rhs_bit]
    mat = [[basis[t], int(bits[t]) & 1] for t in range(m)]

    pivot_cols: List[Tuple[int, int]] = []
    rank = 0
    for col in range(23):
        piv = -1
        for r in range(rank, m):
            if (mat[r][0] >> col) & 1:
                piv = r
                break
        if piv < 0:
            continue
        mat[rank], mat[piv] = mat[piv], mat[rank]
        piv_mask, piv_rhs = mat[rank]
        for r in range(m):
            if r != rank and ((mat[r][0] >> col) & 1):
                mat[r][0] ^= piv_mask
                mat[r][1] ^= piv_rhs
        pivot_cols.append((rank, col))
        rank += 1

    # Check consistency of any remaining rows (critical when m > 23)
    for r in range(rank, m):
        if mat[r][0] == 0 and mat[r][1] != 0:
            raise ValueError(
                f"ADC bitvector of length {m} is inconsistent with 23-bit VU1 LFSR subspace"
            )

    seed = 0
    for row_idx, col in pivot_cols:
        if mat[row_idx][1]:
            seed |= 1 << col

    if eval_vu_lfsr(seed, m) != [int(b) & 1 for b in bits]:
        raise AssertionError("Internal error: solved LFSR seed failed forward verification")
    return seed


def solve_vu_adc_seeds(adcs: Sequence[int]) -> Tuple[int, int, int]:
    """Solve exact `(v1[0], v1[2], v1[3])` 23-bit LFSR seeds for `decode_vu_adc_bits`.

    Matches the retail SLUS_213.51 `.vutext` jump-table split on `h1 = (nv - 1) // 4`:
      - h1 in 0..2 (nv=1..12)  : v1[0] covers all vertices
      - h1 == 3    (nv=13..16) : v1[3] covers all vertices
      - h1 in 4..6 (nv=17..28) : v1[0] covers first `7 + 4*(h1 - 4)`, then v1[3]
      - h1 >= 7    (nv=29+)    : v1[2] covers first `19`, then v1[3]
    """
    nv = len(adcs)
    if nv == 0:
        return 0, 0, 0
    target = [int(b) & 1 for b in adcs]
    h1 = (nv - 1) // 4

    v1_0 = 0
    v1_2 = 0
    v1_3 = 0
    if h1 <= 2:
        v1_0 = solve_lfsr_seed(target)
    elif h1 == 3:
        v1_3 = solve_lfsr_seed(target)
    elif h1 <= 6:
        sp = min(nv, 7 + 4 * (h1 - 4))
        v1_0 = solve_lfsr_seed(target[:sp])
        v1_3 = solve_lfsr_seed(target[sp:])
    else:
        sp = min(nv, 19)
        v1_2 = solve_lfsr_seed(target[:sp])
        v1_3 = solve_lfsr_seed(target[sp:])

    decoded = gi.decode_vu_adc_bits((v1_0, 0, v1_2, v1_3), nv)
    if decoded != target:
        raise AssertionError(
            f"VU1 LFSR round-trip mismatch for nv={nv}: expected={target}, got={decoded}"
        )
    return v1_0, v1_2, v1_3


# ---------------------------------------------------------------------------
# Wavefront OBJ I/O (Preserving (tx, lm, flags, pg) Material Groups)
# ---------------------------------------------------------------------------


def export_rich_obj(
    out_path: Path,
    part_name: str,
    mesh: gi.DecodedMesh,
    group_meta: Optional[Dict[Tuple[int, int], GroupMetadata]] = None,
) -> Path:
    """Export a DecodedMesh to Wavefront .obj preserving (tx, lm, flags, pg) via usemtl."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        f"# NFSMW PS2 Rich Geometry Export: {part_name}",
        f"# Vertices: {len(mesh.vertices)}, Triangles: {len(mesh.triangles)}",
        f"o {part_name}",
    ]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    for u, v in mesh.uvs:
        lines.append(f"vt {u:.6f} {v:.6f}")
    for nx, ny, nz in mesh.normals:
        lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")

    cur_key: Optional[Tuple[int, int]] = None
    for i0, i1, i2, tx, lm in mesh.triangles:
        key = (tx, lm)
        if key != cur_key:
            cur_key = key
            meta = group_meta.get(key) if group_meta else None
            if meta is not None:
                mtl = f"mat_tx{tx}_lm{lm}_fl{meta.strip_flags:04x}_pg{meta.poly_group}"
            else:
                mtl = f"mat_tx{tx}_lm{lm}"
            lines.append(f"g {part_name}_tx{tx}_lm{lm}")
            lines.append(f"usemtl {mtl}")
        a, b, c = i0 + 1, i1 + 1, i2 + 1
        lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def _parse_material_name(
    mtl_name: str,
    group_name: str,
    material_map: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Tuple[int, int, Optional[GroupMetadata]]:
    if material_map and mtl_name in material_map:
        tx, lm = material_map[mtl_name]
        return tx, lm, None
    m = USEMTL_MAT_RE.match(mtl_name)
    if m:
        tx = int(m.group("tx"))
        lm = int(m.group("lm"))
        meta = None
        if m.group("fl") is not None:
            pg = int(m.group("pg")) if m.group("pg") is not None else 0
            meta = GroupMetadata(strip_flags=int(m.group("fl"), 16), poly_group=pg)
        return tx, lm, meta
    m_tx = re.search(r"tx(\d+)", mtl_name) or re.search(r"tex(\d+)", mtl_name) or re.search(r"tex(\d+)", group_name)
    m_lm = re.search(r"lm(\d+)", mtl_name) or re.search(r"lm(\d+)", group_name)
    tx = int(m_tx.group(1)) if m_tx else 0
    lm = int(m_lm.group(1)) if m_lm else tx
    return tx, lm, None


def load_obj_mesh(
    obj_path: Path,
    material_map: Optional[Dict[str, Tuple[int, int]]] = None,
) -> CompiledMesh:
    """Load a Wavefront .obj into a CompiledMesh, splitting corners per unique (v, vt, vn)."""
    raw_v: List[Tuple[float, float, float]] = []
    raw_vt: List[Tuple[float, float]] = []
    raw_vn: List[Tuple[float, float, float]] = []

    corner_map: Dict[Tuple[int, int, int], int] = {}
    out_v: List[Tuple[float, float, float]] = []
    out_vt: List[Tuple[float, float]] = []
    out_vn: List[Tuple[float, float, float]] = []
    out_tris: List[Tuple[int, int, int, int, int]] = []
    group_meta: Dict[Tuple[int, int], GroupMetadata] = {}

    cur_mtl = "mat_tx0_lm0"
    cur_grp = ""
    cur_tx, cur_lm = 0, 0

    def _resolve_idx(raw_str: str, count: int) -> int:
        if not raw_str:
            return -1
        idx = int(raw_str)
        return idx - 1 if idx > 0 else count + idx

    def _get_corner(vi: int, vti: int, vni: int) -> int:
        key = (vi, vti, vni)
        existing = corner_map.get(key)
        if existing is not None:
            return existing
        pos = raw_v[vi]
        uv = raw_vt[vti] if 0 <= vti < len(raw_vt) else (0.0, 0.0)
        nrm = raw_vn[vni] if 0 <= vni < len(raw_vn) else (0.0, 0.0, 1.0)
        idx = len(out_v)
        out_v.append(pos)
        out_vt.append(uv)
        out_vn.append(nrm)
        corner_map[key] = idx
        return idx

    for raw_line in obj_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        op = parts[0]
        if op == "v" and len(parts) >= 4:
            raw_v.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif op == "vt" and len(parts) >= 3:
            raw_vt.append((float(parts[1]), float(parts[2])))
        elif op == "vn" and len(parts) >= 4:
            nx, ny, nz = float(parts[1]), float(parts[2]), float(parts[3])
            mag = (nx * nx + ny * ny + nz * nz) ** 0.5
            raw_vn.append((nx / mag, ny / mag, nz / mag) if mag > 1e-8 else (0.0, 0.0, 1.0))
        elif op == "g":
            cur_grp = parts[1] if len(parts) > 1 else ""
            if cur_mtl == "mat_tx0_lm0" and cur_grp:
                cur_tx, cur_lm, meta = _parse_material_name(cur_mtl, cur_grp, material_map)
                if meta is not None:
                    group_meta.setdefault((cur_tx, cur_lm), meta)
        elif op == "usemtl" and len(parts) >= 2:
            cur_mtl = parts[1]
            cur_tx, cur_lm, meta = _parse_material_name(cur_mtl, cur_grp, material_map)
            if meta is not None:
                group_meta.setdefault((cur_tx, cur_lm), meta)
        elif op == "f" and len(parts) >= 4:
            face_verts: List[int] = []
            for tok in parts[1:]:
                comps = tok.split("/")
                vi = _resolve_idx(comps[0], len(raw_v))
                vti = _resolve_idx(comps[1], len(raw_vt)) if len(comps) > 1 else -1
                vni = _resolve_idx(comps[2], len(raw_vn)) if len(comps) > 2 else -1
                face_verts.append(_get_corner(vi, vti, vni))
            for k in range(1, len(face_verts) - 1):
                a, b, c = face_verts[0], face_verts[k], face_verts[k + 1]
                if a != b and b != c and a != c:
                    out_tris.append((a, b, c, cur_tx, cur_lm))

    if not out_tris:
        raise ValueError(f"OBJ contains zero valid triangles: {obj_path}")

    # Compute smooth fallback normals if OBJ omitted vn
    if not raw_vn:
        accum = [[0.0, 0.0, 0.0] for _ in range(len(out_v))]
        for i0, i1, i2, _, _ in out_tris:
            p0, p1, p2 = out_v[i0], out_v[i1], out_v[i2]
            e1 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
            e2 = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
            fn = (
                e1[1] * e2[2] - e1[2] * e2[1],
                e1[2] * e2[0] - e1[0] * e2[2],
                e1[0] * e2[1] - e1[1] * e2[0],
            )
            for idx in (i0, i1, i2):
                accum[idx][0] += fn[0]
                accum[idx][1] += fn[1]
                accum[idx][2] += fn[2]
        for idx, acc in enumerate(accum):
            mag = (acc[0] * acc[0] + acc[1] * acc[1] + acc[2] * acc[2]) ** 0.5
            out_vn[idx] = (acc[0] / mag, acc[1] / mag, acc[2] / mag) if mag > 1e-12 else (0.0, 0.0, 1.0)

    return CompiledMesh(
        vertices=out_v,
        normals=out_vn,
        uvs=out_vt,
        triangles=out_tris,
        group_meta=group_meta,
    )


# ---------------------------------------------------------------------------
# Triangle Stripper & PS2 VIF Strip Batch Encoder
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class StripBatch:
    indices: List[int]
    adcs: List[int]
    num_polys: int
    tx: int
    lm: int
    strip_flags: int
    poly_group: int


def _decode_strip_tris(indices: Sequence[int], adcs: Sequence[int]) -> List[Tuple[int, int, int]]:
    tris: List[Tuple[int, int, int]] = []
    parity = 0
    for i in range(2, len(indices)):
        if adcs[i]:
            parity = 0
            continue
        if parity == 0:
            tris.append((indices[i - 2], indices[i - 1], indices[i]))
        else:
            tris.append((indices[i - 1], indices[i - 2], indices[i]))
        parity ^= 1
    return tris


def _canonical_tri(t: Tuple[int, int, int]) -> Tuple[int, int, int]:
    a, b, c = t
    if a <= b and a <= c:
        return (a, b, c)
    if b <= a and b <= c:
        return (b, c, a)
    return (c, a, b)


def build_triangle_strips(
    triangles: Sequence[Tuple[int, int, int, int, int]],
    group_meta: Optional[Dict[Tuple[int, int], GroupMetadata]] = None,
    max_strip_verts: int = MAX_STRIP_VERTS,
) -> List[StripBatch]:
    """Group triangles by (tx, lm), greedily strip neighbors, and pack into VU1 batches."""
    if max_strip_verts < 3 or max_strip_verts > 42:
        raise ValueError(f"max_strip_verts must be in [3, 42], got {max_strip_verts}")

    by_group: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = defaultdict(list)
    for i0, i1, i2, tx, lm in triangles:
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        by_group[(tx, lm)].append((i0, i1, i2))

    all_batches: List[StripBatch] = []
    auto_pg = 0

    for (tx, lm), tris in by_group.items():
        meta = group_meta.get((tx, lm)) if group_meta else None
        s_flags = meta.strip_flags if meta is not None else 0x4180
        pg = meta.poly_group if meta is not None else auto_pg
        auto_pg += 1

        # Build directed edge -> candidate triangle indices
        dir_edges: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for ti, (a, b, c) in enumerate(tris):
            dir_edges[(a, b)].append(ti)
            dir_edges[(b, c)].append(ti)
            dir_edges[(c, a)].append(ti)

        used = [False] * len(tris)

        # Order seed candidates by lowest edge-neighbor degree for longer strips
        def _degree(ti: int) -> int:
            a, b, c = tris[ti]
            return len(dir_edges[(b, a)]) + len(dir_edges[(c, b)]) + len(dir_edges[(a, c)])

        order = sorted(range(len(tris)), key=_degree)

        def _extend_from(a: int, b: int, c: int, limit: int) -> List[Tuple[int, int]]:
            """Extend a sub-strip starting with triangle (a, b, c) without mutating `used`.

            Returns list of `(vertex_index, tri_index_or_-1)` for vertices added.
            """
            local_used = set()
            seq = [a, b, c]
            picked_tris: List[int] = []
            parity = 1  # Next emitted tri at i=3 has parity=1
            while len(seq) < limit:
                u, v = seq[-2], seq[-1]
                # For parity == 1: decode emits (v, u, w), so triangle needs directed edge (v -> u).
                # For parity == 0: decode emits (u, v, w), so triangle needs directed edge (u -> v).
                need_edge = (v, u) if parity == 1 else (u, v)
                cand_ti = -1
                cand_w = -1
                for nti in dir_edges.get(need_edge, ()):
                    if used[nti] or nti in local_used:
                        continue
                    t0, t1, t2 = tris[nti]
                    if (t0, t1) == need_edge:
                        w = t2
                    elif (t1, t2) == need_edge:
                        w = t0
                    elif (t2, t0) == need_edge:
                        w = t1
                    else:
                        continue
                    cand_ti = nti
                    cand_w = w
                    break
                if cand_ti < 0:
                    break
                local_used.add(cand_ti)
                picked_tris.append(cand_ti)
                seq.append(cand_w)
                parity ^= 1
            return seq, picked_tris  # type: ignore[return-value]

        sub_strips: List[List[int]] = []
        for seed_ti in order:
            if used[seed_ti]:
                continue
            used[seed_ti] = True
            a, b, c = tris[seed_ti]
            best_seq: Optional[List[int]] = None
            best_picked: List[int] = []
            for rot in ((a, b, c), (b, c, a), (c, a, b)):
                seq, picked = _extend_from(rot[0], rot[1], rot[2], max_strip_verts)
                if best_seq is None or len(seq) > len(best_seq):
                    best_seq = seq
                    best_picked = picked
            assert best_seq is not None
            for nti in best_picked:
                used[nti] = True
            sub_strips.append(best_seq)

        # Pack sub_strips into VU1 batches of <= max_strip_verts using ADC=[1,1,0...] restarts
        cur_indices: List[int] = []
        cur_adcs: List[int] = []
        for sub in sub_strips:
            if cur_indices and len(cur_indices) + len(sub) > max_strip_verts:
                all_batches.append(
                    StripBatch(
                        indices=cur_indices,
                        adcs=cur_adcs,
                        num_polys=sum(1 for x in cur_adcs[2:] if x == 0),
                        tx=tx,
                        lm=lm,
                        strip_flags=s_flags,
                        poly_group=pg,
                    )
                )
                cur_indices = []
                cur_adcs = []
            cur_indices.extend(sub)
            cur_adcs.extend([1, 1] + [0] * (len(sub) - 2))

        if cur_indices:
            all_batches.append(
                StripBatch(
                    indices=cur_indices,
                    adcs=cur_adcs,
                    num_polys=sum(1 for x in cur_adcs[2:] if x == 0),
                    tx=tx,
                    lm=lm,
                    strip_flags=s_flags,
                    poly_group=pg,
                )
            )

    # Exact verification: ensure decoded triangles match input multiset + winding
    expected_counter: Dict[Tuple[int, int, int, int, int], int] = defaultdict(int)
    for i0, i1, i2, tx, lm in triangles:
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        a, b, c = _canonical_tri((i0, i1, i2))
        expected_counter[(a, b, c, tx, lm)] += 1

    got_counter: Dict[Tuple[int, int, int, int, int], int] = defaultdict(int)
    for batch in all_batches:
        for t in _decode_strip_tris(batch.indices, batch.adcs):
            a, b, c = _canonical_tri(t)
            got_counter[(a, b, c, batch.tx, batch.lm)] += 1

    if expected_counter != got_counter:
        raise AssertionError("Triangle stripper failed exact winding/triangle verification")

    return all_batches


def _choose_integer_bases(
    verts: Sequence[Tuple[float, float, float]], scale: float
) -> List[float]:
    """Choose integer coordinate bases so `(v - base) / scale` fits cleanly in int16."""
    max_span = 32760 * scale
    bases: List[float] = []
    for ax in range(3):
        vals = [v[ax] for v in verts]
        vmin, vmax = min(vals), max(vals)
        if vmin >= -max_span and vmax <= max_span:
            bases.append(0.0)
        else:
            mid = round(0.5 * (vmin + vmax))
            if (vmin - mid) < -max_span or (vmax - mid) > max_span:
                raise ValueError(
                    f"Strip axis {ax} span [{vmin:.4f}, {vmax:.4f}] exceeds int16 range at scale={scale}"
                )
            bases.append(float(mid))
    return bases


def pack_vif_strip(
    verts: Sequence[Tuple[float, float, float]],
    normals: Sequence[Tuple[float, float, float]],
    uvs: Sequence[Tuple[float, float]],
    adcs: Sequence[int],
    scale: float = DEFAULT_SCALE,
) -> bytes:
    """Encode one PS2 VU1/VIF strip DMA packet buffer (16-byte qword aligned)."""
    nv = len(verts)
    if nv < 3 or nv > 42:
        raise ValueError(f"VIF strip vertex count must be in [3, 42], got {nv}")
    if len(normals) != nv or len(uvs) != nv or len(adcs) != nv:
        raise ValueError("verts, normals, uvs, and adcs must have equal length")

    bases = _choose_integer_bases(verts, scale)
    xs_q = [max(-32768, min(32767, round((v[0] - bases[0]) / scale))) for v in verts]
    ys_q = [max(-32768, min(32767, round((v[1] - bases[1]) / scale))) for v in verts]
    zs_q = [max(-32768, min(32767, round((v[2] - bases[2]) / scale))) for v in verts]

    vec2 = (
        bases[0] + min(xs_q) * scale - 0.01,
        bases[1] + min(ys_q) * scale - 0.01,
        bases[2] + min(zs_q) * scale - 0.01,
    )
    vec3 = (
        bases[0] + max(xs_q) * scale + 0.01,
        bases[1] + max(ys_q) * scale + 0.01,
        bases[2] + max(zs_q) * scale + 0.01,
    )

    v1_0, v1_2, v1_3 = solve_vu_adc_seeds(adcs)
    v1_1 = struct.unpack("<I", struct.pack("<f", scale))[0]

    buf = bytearray()

    # 1. addr 0x00: strip header [nv, (nv - 1) // 4, 0x17, 0x04]
    buf += struct.pack("<I", 0x6E018000)
    buf += struct.pack("<4B", nv, (nv - 1) // 4, 0x17, 0x04)

    # 2. addr 0x01: v1[0..3] (LFSR seeds + scale)
    buf += struct.pack("<I", 0x6C01C001)
    buf += struct.pack("<4I", v1_0, v1_1, v1_2, v1_3)

    # 3. addr 0x02..0x03: vec2, vec3 (strip AABB +/- 0.01)
    buf += struct.pack("<I", 0x6802C002)
    buf += struct.pack("<3f3f", *vec2, *vec3)

    # 4. addr 0x04: culling normal cone (0 = disabled so no triangles are over-culled)
    buf += struct.pack("<I", 0x6C01C004)
    buf += struct.pack("<4f", 0.0, 0.0, 0.0, 0.0)

    # 5. addr 0x05..: 4-vertex blocked int16 positions [X0..b-1, Y0..b-1, Z0..b-1]
    full_blocks = nv // 4
    rem = nv % 4
    if full_blocks > 0:
        buf += struct.pack("<I", (0x6D << 24) | ((3 * full_blocks) << 16) | 0x8005)
        for bi in range(full_blocks):
            s = bi * 4
            buf += struct.pack("<4h4h4h", *xs_q[s : s + 4], *ys_q[s : s + 4], *zs_q[s : s + 4])
    if rem > 0:
        cmd_rem = (0x61, 0x65, 0x69)[rem - 1]
        addr_rem = 0x05 + 3 * full_blocks
        buf += struct.pack("<I", (cmd_rem << 24) | (3 << 16) | 0x8000 | addr_rem)
        s = full_blocks * 4
        buf += struct.pack(
            f"<{rem}h{rem}h{rem}h",
            *xs_q[s : s + rem],
            *ys_q[s : s + rem],
            *zs_q[s : s + rem],
        )
        while len(buf) % 4 != 0:
            buf.append(0x00)

    # 6. addr 0x1D..: int16 UVs (4096.0 scale, v = 1 - y, odd vertices U/V swapped)
    uv_ints: List[int] = []
    for i, (u, v) in enumerate(uvs):
        uq = max(-32768, min(32767, round(u * 4096.0)))
        vq = max(-32768, min(32767, round((1.0 - v) * 4096.0)))
        if i & 1:
            uv_ints.extend((vq, uq))
        else:
            uv_ints.extend((uq, vq))

    if full_blocks > 0:
        buf += struct.pack("<I", (0x6D << 24) | ((2 * full_blocks) << 16) | 0x801D)
        buf += struct.pack(f"<{8 * full_blocks}h", *uv_ints[: 8 * full_blocks])
    if rem > 0:
        rem_q = 1 if rem <= 2 else 2
        addr_rem = 0x1D + 2 * full_blocks
        buf += struct.pack("<I", (0x6D << 24) | (rem_q << 16) | 0x8000 | addr_rem)
        tail_uv = uv_ints[8 * full_blocks :]
        while len(tail_uv) < rem_q * 4:
            tail_uv.append(0)
        buf += struct.pack(f"<{rem_q * 4}h", *tail_uv)

    # 7. addr 0x4D..: 4-vertex blocked int8 normals (padded to 4 bytes per axis)
    total_blocks = (nv + 3) // 4
    buf += struct.pack("<I", (0x6E << 24) | ((3 * total_blocks) << 16) | 0x804D)
    for bi in range(total_blocks):
        s = bi * 4
        b = min(4, nv - s)
        nxs = [max(-127, min(127, round(normals[s + j][0] * 127.0))) for j in range(b)] + [0] * (4 - b)
        nys = [max(-127, min(127, round(normals[s + j][1] * 127.0))) for j in range(b)] + [0] * (4 - b)
        nzs = [max(-127, min(127, round(normals[s + j][2] * 127.0))) for j in range(b)] + [0] * (4 - b)
        buf += struct.pack("<12b", *nxs, *nys, *nzs)

    # 8. Execute VU1 microprogram (MSCAL 0x0) + NOP pad to 16-byte qword boundary
    buf += struct.pack("<I", 0x14000000)
    while len(buf) % 16 != 0:
        buf += struct.pack("<I", 0x00000000)

    qsz = len(buf) // 16
    if qsz > MAX_QSZ:
        raise ValueError(f"VIF strip packet exceeds uint8 qword limit: {qsz} > {MAX_QSZ}")
    return bytes(buf)


# ---------------------------------------------------------------------------
# EA bChunk Helpers & SolidPack Encoder
# ---------------------------------------------------------------------------


def _make_bchunk(cid: int, payload: bytes) -> bytes:
    return struct.pack("<II", cid, len(payload)) + payload


def _make_aligned_subchunk(cid: int, raw_payload: bytes, chunk_start_off: int) -> bytes:
    """Emit a bChunk whose payload begins on a 16-byte boundary via 0x11 padding."""
    pay_off = chunk_start_off + 8
    pad = (16 - (pay_off & 15)) & 15
    body = (b"\x11" * pad) + raw_payload
    return struct.pack("<II", cid, len(body)) + body


def _pad_chunk_to_alignment(current_pos: int, align: int = 64) -> bytes:
    """Emit a 0x00000000 padding chunk so the next chunk starts at `align` alignment."""
    rem = (align - (current_pos % align)) % align
    if rem == 0:
        return b""
    if rem < 8:
        rem += align
    return struct.pack("<II", CHUNK_PADDING, rem - 8) + (b"\x00" * (rem - 8))


def _compute_aabb(verts: Sequence[Tuple[float, float, float]]) -> Tuple[List[float], List[float]]:
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


def build_mesh_container(
    mesh: CompiledMesh,
    *,
    mesh_chunk_rel_off: int,
    scale: float = DEFAULT_SCALE,
    plat_version: int = 32,
    plat_strip_flags: int = 0x4180,
    max_strip_verts: int = MAX_STRIP_VERTS,
) -> Tuple[bytes, int, int, int, int]:
    """Compile a CompiledMesh into a `0x80134100` PS2 VU1/VIF Mesh Container.

    `mesh_chunk_rel_off` must share the same mod-16 alignment as the file offset
    (guaranteed when the enclosing `0x80134010` starts at a 16/64-byte aligned offset).

    Returns `(chunk_bytes, num_strips, total_verts, total_polys, vif_bytes_len)`.
    """
    batches = build_triangle_strips(
        mesh.triangles, group_meta=mesh.group_meta, max_strip_verts=max_strip_verts
    )

    strip_entries = bytearray()
    vif_buf = bytearray()
    qoff = 0
    total_verts = 0
    total_polys = 0

    for batch in batches:
        nv = len(batch.indices)
        np = batch.num_polys
        b_verts = [mesh.vertices[i] for i in batch.indices]
        b_norms = [mesh.normals[i] for i in batch.indices]
        b_uvs = [mesh.uvs[i] for i in batch.indices]
        pkt = pack_vif_strip(b_verts, b_norms, b_uvs, batch.adcs, scale=scale)
        qsz = len(pkt) // 16
        if qoff > MAX_QOFF:
            raise ValueError(
                f"VIF strip qword offset {qoff} exceeds uint16 limit {MAX_QOFF} "
                f"({len(vif_buf):,} B)"
            )
        strip_entries += struct.pack(
            "<HBBfHbbBBbb",
            batch.strip_flags,
            nv,
            np,
            0.0,   # material_density / cull cone disabled
            qoff,
            0,     # nz
            0,     # nt
            qsz,
            batch.poly_group & 0xFF,
            batch.tx,
            batch.lm,
        )
        vif_buf += pkt
        qoff += qsz
        total_verts += nv
        total_polys += np

    vif_bytes_len = len(vif_buf)
    sub_cursor = mesh_chunk_rel_off + 8

    # 0x00134A00 eSolidPlatInfo (20 bytes payload after alignment: 12B fields + 8B tail)
    plat_raw = struct.pack("<HHII8x", plat_version, plat_strip_flags, len(batches), vif_bytes_len)
    c_plat = _make_aligned_subchunk(CHUNK_PLAT_INFO, plat_raw, sub_cursor)
    sub_cursor += len(c_plat)

    c_strip = _make_aligned_subchunk(CHUNK_STRIP_TABLE, bytes(strip_entries), sub_cursor)
    sub_cursor += len(c_strip)

    c_vif = _make_aligned_subchunk(CHUNK_VIF_DATA, bytes(vif_buf), sub_cursor)
    c_mesh = _make_bchunk(CHUNK_MESH_CONT, c_plat + c_strip + c_vif)
    return c_mesh, len(batches), total_verts, total_polys, vif_bytes_len


def _build_esolid_chunk(
    orig_esolid_pay: bytes,
    num_polys: int,
    num_tex: int,
    num_mat: int,
    aabb_min: Sequence[float],
    aabb_max: Sequence[float],
) -> bytes:
    """Return an updated `0x00134011` eSolid chunk preserving header metadata & name."""
    if num_polys > DEFAULT_MAX_POLYS:
        raise ValueError(
            f"Compiled solid poly count {num_polys} exceeds 16-bit guardrail {DEFAULT_MAX_POLYS}"
        )
    buf = bytearray(orig_esolid_pay)
    struct.pack_into("<h", buf, 0x14, num_polys)
    struct.pack_into("<bb", buf, 0x19, num_tex, num_mat)
    struct.pack_into("<3f", buf, 0x20, *aabb_min)
    struct.pack_into("<3f", buf, 0x30, *aabb_max)
    return _make_bchunk(CHUNK_ESOLID_HDR, bytes(buf))


def _realign_markers_chunk(raw_pay: bytes, rel_off: int) -> bytes:
    """Preserve `0x0013401A` marker payload while re-aligning its 0x11 prefix to mod-16."""
    i = 0
    while i < len(raw_pay) and raw_pay[i] == 0x11:
        i += 1
    return _make_aligned_subchunk(CHUNK_POS_MARKERS, raw_pay[i:], rel_off)


def build_solid_pack_from_template(
    orig_solid_bytes: bytes,
    mesh: CompiledMesh,
    *,
    scale: float = DEFAULT_SCALE,
    max_strip_verts: int = MAX_STRIP_VERTS,
    max_solid_bytes: int = DEFAULT_MAX_SOLID_BYTES,
) -> Tuple[bytes, CompileStats]:
    """Rebuild an existing `0x80134010` SolidPack with a new CompiledMesh."""
    cid, csz = struct.unpack_from("<II", orig_solid_bytes, 0)
    if cid != CHUNK_SOLID_PACK:
        raise ValueError(f"Expected 0x80134010 SolidPack, got 0x{cid:08x}")

    end = min(len(orig_solid_bytes), 8 + csz)
    p = 8
    esolid_pay: Optional[bytes] = None
    tex_chunk: Optional[bytes] = None
    mat_chunk: Optional[bytes] = None
    markers_pay: Optional[bytes] = None
    extra_chunks: List[bytes] = []

    part_name = "SOLID"
    plat_ver = 32
    plat_strip_flags = 0x4180
    orig_group_meta: Dict[Tuple[int, int], GroupMetadata] = {}

    while p + 8 <= end:
        scid, scsz = struct.unpack_from("<II", orig_solid_bytes, p)
        pay = orig_solid_bytes[p + 8 : p + 8 + scsz]
        if scid == CHUNK_ESOLID_HDR:
            esolid_pay = pay
            part_name = pay[0xA0:].split(b"\x00", 1)[0].decode("ascii", errors="replace") or part_name
        elif scid == CHUNK_TEX_TABLE:
            tex_chunk = orig_solid_bytes[p : p + 8 + scsz]
        elif scid == CHUNK_MAT_TABLE:
            mat_chunk = orig_solid_bytes[p : p + 8 + scsz]
        elif scid == CHUNK_POS_MARKERS:
            markers_pay = pay
        elif scid == CHUNK_MESH_CONT:
            mp = p + 8
            mend = p + 8 + scsz
            while mp + 8 <= mend:
                mcid, mcsz = struct.unpack_from("<II", orig_solid_bytes, mp)
                mp_off = mp + 8
                mp_end = min(mend, mp_off + mcsz)
                if mcid == CHUNK_PLAT_INFO:
                    po = gi._skip_11_padding(orig_solid_bytes, mp_off, mp_end)
                    if po + 4 <= mp_end:
                        plat_ver, plat_strip_flags = struct.unpack_from("<HH", orig_solid_bytes, po)
                elif mcid == CHUNK_STRIP_TABLE:
                    so = gi._skip_11_padding(orig_solid_bytes, mp_off, mp_end)
                    while so + 16 <= mp_end:
                        fl, _nv, _np, _md, _qo, _nz, _nt, _qs, pg, tx, lm = struct.unpack_from(
                            "<HBBfHbbBBbb", orig_solid_bytes, so
                        )
                        orig_group_meta.setdefault(
                            (tx, lm), GroupMetadata(strip_flags=fl, poly_group=pg)
                        )
                        so += 16
                mp = mp_off + mcsz
        elif scid not in DAMAGE_CHUNK_IDS and scid != CHUNK_PADDING:
            extra_chunks.append(orig_solid_bytes[p : p + 8 + scsz])
        p += 8 + scsz

    if esolid_pay is None or tex_chunk is None or mat_chunk is None:
        raise ValueError(f"SolidPack '{part_name}' missing required header/texture/material chunks")

    # Merge original strip_flags & poly_group into mesh.group_meta where not overridden
    merged_meta = dict(orig_group_meta)
    merged_meta.update(mesh.group_meta)
    compile_mesh = CompiledMesh(
        vertices=mesh.vertices,
        normals=mesh.normals,
        uvs=mesh.uvs,
        triangles=mesh.triangles,
        group_meta=merged_meta,
    )

    num_tex = (len(tex_chunk) - 8) // 8
    num_mat = (len(mat_chunk) - 8) // 8
    for _i0, _i1, _i2, tx, lm in compile_mesh.triangles:
        if tx < 0 or tx >= num_tex:
            raise ValueError(
                f"Triangle texture_index {tx} out of range for solid '{part_name}' (num_tex={num_tex})"
            )
        if lm < 0 or lm >= num_mat:
            raise ValueError(
                f"Triangle light_material_index {lm} out of range for solid '{part_name}' (num_mat={num_mat})"
            )

    aabb_min, aabb_max = _compute_aabb(compile_mesh.vertices)

    # Assemble SolidPack payload assuming 16/64-byte aligned SolidPack start
    inner = bytearray()
    inner += _build_esolid_chunk(esolid_pay, len(compile_mesh.triangles), num_tex, num_mat, aabb_min, aabb_max)
    inner += tex_chunk
    inner += mat_chunk
    if markers_pay is not None:
        inner += _realign_markers_chunk(markers_pay, 8 + len(inner))
    for ex in extra_chunks:
        inner += ex

    c_mesh, n_strips, n_verts, n_polys, vif_bytes_len = build_mesh_container(
        compile_mesh,
        mesh_chunk_rel_off=8 + len(inner),
        scale=scale,
        plat_version=plat_ver,
        plat_strip_flags=plat_strip_flags,
        max_strip_verts=max_strip_verts,
    )
    inner += c_mesh
    solid_bytes = _make_bchunk(CHUNK_SOLID_PACK, bytes(inner))

    if len(solid_bytes) > max_solid_bytes:
        raise ValueError(
            f"Compiled SolidPack '{part_name}' is {len(solid_bytes):,} B, exceeding EE budget "
            f"guardrail of {max_solid_bytes:,} B"
        )

    stats = CompileStats(
        part_name=part_name,
        input_vertices=len(compile_mesh.vertices),
        input_triangles=len(compile_mesh.triangles),
        strip_count=n_strips,
        strip_vertices=n_verts,
        strip_polys=n_polys,
        vif_bytes=vif_bytes_len,
        solid_bytes=len(solid_bytes),
        pack_bytes=0,
        aabb_min=[round(x, 6) for x in aabb_min],
        aabb_max=[round(x, 6) for x in aabb_max],
    )
    return solid_bytes, stats


def build_synthetic_geometry_pack(
    filename: str,
    group_name: str,
    solids: Sequence[Tuple[str, CompiledMesh, List[int], List[int]]],
    *,
    scale: float = DEFAULT_SCALE,
    max_strip_verts: int = MAX_STRIP_VERTS,
) -> bytes:
    """Build a fresh `0x80134000` GeometryPack from `(part_name, mesh, tex_hashes, mat_hashes)`."""
    hdr02 = bytearray(0x90)
    struct.pack_into("<II", hdr02, 8, 0x1D, len(solids))
    fn_b = filename.encode("ascii", errors="replace")[:0x37]
    gn_b = group_name.encode("ascii", errors="replace")[:0x1F]
    hdr02[0x10 : 0x10 + len(fn_b)] = fn_b
    hdr02[0x48 : 0x48 + len(gn_b)] = gn_b

    # Build SolidPack chunks first
    compiled_solids: List[Tuple[int, bytes]] = []
    for part_name, mesh, tex_hashes, mat_hashes in solids:
        name_hash = bstringhash(part_name)
        name_bytes = part_name.encode("ascii", errors="replace") + b"\x00"
        while (0xA0 + len(name_bytes)) % 4 != 0:
            name_bytes += b"\x00"
        esolid = bytearray(0xA0 + len(name_bytes))
        struct.pack_into(
            "<BBHIhhbbbb",
            esolid,
            0x0C,
            0x16,
            0,
            0x0040,
            name_hash,
            len(mesh.triangles),
            0,
            0,
            len(tex_hashes),
            len(mat_hashes),
            0,
        )
        aabb_min, aabb_max = _compute_aabb(mesh.vertices)
        struct.pack_into("<3f", esolid, 0x20, *aabb_min)
        struct.pack_into("<3f", esolid, 0x30, *aabb_max)
        # Identity pivot matrix
        struct.pack_into("<4f", esolid, 0x40, 1.0, 0.0, 0.0, 0.0)
        struct.pack_into("<4f", esolid, 0x50, 0.0, 1.0, 0.0, 0.0)
        struct.pack_into("<4f", esolid, 0x60, 0.0, 0.0, 1.0, 0.0)
        struct.pack_into("<4f", esolid, 0x70, 0.0, 0.0, 0.0, 1.0)
        struct.pack_into("<2f", esolid, 0x98, 1.0, 1.0)
        esolid[0xA0:] = name_bytes

        c_esolid = _make_bchunk(CHUNK_ESOLID_HDR, bytes(esolid))
        c_tex = _make_bchunk(
            CHUNK_TEX_TABLE,
            b"".join(struct.pack("<II", h & 0xFFFFFFFF, 0) for h in tex_hashes),
        )
        c_mat = _make_bchunk(
            CHUNK_MAT_TABLE,
            b"".join(struct.pack("<II", h & 0xFFFFFFFF, 0) for h in mat_hashes),
        )
        template = _make_bchunk(CHUNK_SOLID_PACK, c_esolid + c_tex + c_mat)
        solid_bytes, _ = build_solid_pack_from_template(
            template, mesh, scale=scale, max_strip_verts=max_strip_verts
        )
        compiled_solids.append((name_hash, solid_bytes))

    sorted_hashes = sorted(h for h, _ in compiled_solids)
    keys03 = b"".join(struct.pack("<II", h, 0) for h in sorted_hashes)
    offs04_placeholder = b"\x00" * (24 * len(compiled_solids))

    c_02 = _make_bchunk(CHUNK_GEOM_HEADER, bytes(hdr02))
    c_03 = _make_bchunk(CHUNK_GEOM_KEYS, keys03)
    c_04 = _make_bchunk(CHUNK_GEOM_OFFSETS, offs04_placeholder)
    geom_info = _make_bchunk(CHUNK_GEOM_INFO, c_02 + c_03 + c_04)

    body = bytearray()
    body += _make_bchunk(CHUNK_PADDING, b"")  # aligns 0x80134001 start to 0x10
    info_start = 8 + len(body)
    body += geom_info

    solid_locs: Dict[int, Tuple[int, int]] = {}
    for nh, sbytes in compiled_solids:
        body += _pad_chunk_to_alignment(8 + len(body), 64)
        off = 8 + len(body)
        body += sbytes
        solid_locs[nh] = (off, len(sbytes))

    # Patch 0x00134004 offsets and 0x00134002 total pack size
    offs04 = b"".join(
        struct.pack("<6I", h, solid_locs[h][0], solid_locs[h][1], solid_locs[h][1], 0, 0)
        for h in sorted_hashes
    )
    tail_pad = _pad_chunk_to_alignment(8 + len(body), 128)
    total_len = 8 + len(body) + len(tail_pad)
    struct.pack_into("<I", hdr02, 0x68, total_len)

    c_02 = _make_bchunk(CHUNK_GEOM_HEADER, bytes(hdr02))
    c_04 = _make_bchunk(CHUNK_GEOM_OFFSETS, offs04)
    new_info = _make_bchunk(CHUNK_GEOM_INFO, c_02 + c_03 + c_04)
    if len(new_info) != len(geom_info):
        raise AssertionError("GeometryInfo size changed unexpectedly")
    body[info_start - 8 : info_start - 8 + len(new_info)] = new_info

    return _make_bchunk(CHUNK_GEOM_PACK, bytes(body)) + tail_pad


# ---------------------------------------------------------------------------
# GEOMETRY.BIN Repacker (`replace_solid_in_pack`)
# ---------------------------------------------------------------------------


def replace_solid_in_pack(
    pack_data: bytes,
    target_solid_name: str,
    mesh: CompiledMesh,
    *,
    scale: float = DEFAULT_SCALE,
    max_strip_verts: int = MAX_STRIP_VERTS,
    max_solid_bytes: int = DEFAULT_MAX_SOLID_BYTES,
    max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES,
) -> Tuple[bytes, CompileStats]:
    """Replace one named `0x80134010` solid in a GeometryPack, keeping all others byte-intact."""
    if len(pack_data) < 16:
        raise ValueError("Geometry pack too small")
    root_id, root_sz = struct.unpack_from("<II", pack_data, 0)
    if root_id != CHUNK_GEOM_PACK:
        raise ValueError(f"Expected 0x80134000 GeometryPack, got 0x{root_id:08x}")

    pack_end = min(len(pack_data), 8 + root_sz)
    pos = 8
    pre_info_chunks: List[Tuple[int, bytes]] = []
    info_subchunks: List[Tuple[int, bytes]] = []
    solids: List[Tuple[int, str, bytes]] = []
    trailing_after_pack = pack_data[pack_end:]

    found_info = False
    while pos + 8 <= pack_end:
        cid, csz = struct.unpack_from("<II", pack_data, pos)
        if pos + 8 + csz > pack_end:
            break
        raw_chunk = pack_data[pos : pos + 8 + csz]
        if cid == CHUNK_GEOM_INFO:
            found_info = True
            p2 = pos + 8
            end2 = p2 + csz
            while p2 + 8 <= end2:
                scid, scsz = struct.unpack_from("<II", pack_data, p2)
                info_subchunks.append((scid, pack_data[p2 + 8 : p2 + 8 + scsz]))
                p2 += 8 + scsz
        elif cid == CHUNK_SOLID_PACK:
            p2 = pos + 8
            end2 = p2 + csz
            nh = 0
            nm = ""
            while p2 + 8 <= end2:
                scid, scsz = struct.unpack_from("<II", pack_data, p2)
                if scid == CHUNK_ESOLID_HDR and scsz >= 0xA0:
                    pay = pack_data[p2 + 8 : p2 + 8 + scsz]
                    nh = struct.unpack_from("<I", pay, 0x10)[0]
                    nm = pay[0xA0:].split(b"\x00", 1)[0].decode("ascii", errors="replace")
                    break
                p2 += 8 + scsz
            solids.append((nh, nm, raw_chunk))
        elif cid != CHUNK_PADDING and not found_info:
            pre_info_chunks.append((cid, raw_chunk))
        pos += 8 + csz

    if not found_info or not solids:
        raise ValueError("Invalid GeometryPack: missing 0x80134001 or 0x80134010 solids")

    match_indices = [i for i, (_, nm, _) in enumerate(solids) if nm == target_solid_name]
    if not match_indices:
        raise KeyError(f"Solid '{target_solid_name}' not found in GeometryPack")
    if len(match_indices) > 1:
        raise ValueError(f"Multiple solids named '{target_solid_name}' in GeometryPack")
    target_idx = match_indices[0]

    new_solid_bytes, stats = build_solid_pack_from_template(
        solids[target_idx][2],
        mesh,
        scale=scale,
        max_strip_verts=max_strip_verts,
        max_solid_bytes=max_solid_bytes,
    )
    solids[target_idx] = (solids[target_idx][0], solids[target_idx][1], new_solid_bytes)

    # Detect whether 0x00134002+0x68 stores total file size (all 91 CARS\* packs do)
    hdr02_tracks_file_size = False
    for scid, spay in info_subchunks:
        if scid == CHUNK_GEOM_HEADER and len(spay) >= 0x70:
            if struct.unpack_from("<I", spay, 0x68)[0] == len(pack_data):
                hdr02_tracks_file_size = True

    # Assemble 0x80134000 body with exact 64-byte alignment before every 0x80134010
    prefix_bytes = bytearray()
    for _, rc in pre_info_chunks:
        prefix_bytes += rc
    # Preserve initial 0x00000000 alignment chunk if GeometryInfo started at 0x10
    if len(prefix_bytes) == 0:
        prefix_bytes += _make_bchunk(CHUNK_PADDING, b"")

    orig_info_chunk = _make_bchunk(
        CHUNK_GEOM_INFO,
        b"".join(_make_bchunk(scid, spay) for scid, spay in info_subchunks),
    )

    body_after_info = bytearray()
    cursor = 8 + len(prefix_bytes) + len(orig_info_chunk)
    solid_locs: Dict[int, Tuple[int, int]] = {}
    for nh, _nm, sbytes in solids:
        pad_chunk = _pad_chunk_to_alignment(cursor, 64)
        body_after_info += pad_chunk
        cursor += len(pad_chunk)
        solid_locs[nh] = (cursor, len(sbytes))
        body_after_info += sbytes
        cursor += len(sbytes)

    # Determine trailing bytes (preserve non-padding tails if any, else 128-B pad)
    is_pure_padding_tail = True
    tp = 0
    while tp + 8 <= len(trailing_after_pack):
        tcid, tcsz = struct.unpack_from("<II", trailing_after_pack, tp)
        if tcid != CHUNK_PADDING:
            is_pure_padding_tail = False
            break
        tp += 8 + tcsz
    if tp != len(trailing_after_pack):
        is_pure_padding_tail = False

    if trailing_after_pack and not is_pure_padding_tail:
        tail_bytes = trailing_after_pack
    elif hdr02_tracks_file_size:
        tail_bytes = _pad_chunk_to_alignment(cursor, 128)
    else:
        tail_bytes = b""

    total_pack_bytes = cursor + len(tail_bytes)
    if total_pack_bytes > max_pack_bytes:
        raise ValueError(
            f"Repacked GEOMETRY.BIN is {total_pack_bytes:,} B, exceeding EE pack guardrail "
            f"of {max_pack_bytes:,} B"
        )

    # Update 0x00134002 (+0x68 total file size) and 0x00134004 (solid offset/size table)
    updated_info_subs: List[bytes] = []
    for scid, spay in info_subchunks:
        if scid == CHUNK_GEOM_HEADER and hdr02_tracks_file_size and len(spay) >= 0x70:
            mutable = bytearray(spay)
            struct.pack_into("<I", mutable, 0x68, total_pack_bytes)
            updated_info_subs.append(_make_bchunk(scid, bytes(mutable)))
        elif scid == CHUNK_GEOM_OFFSETS:
            mutable = bytearray(spay)
            for off in range(0, len(mutable) - 23, 24):
                sh = struct.unpack_from("<I", mutable, off)[0]
                if sh in solid_locs:
                    new_off, new_tot = solid_locs[sh]
                    struct.pack_into("<3I", mutable, off + 4, new_off, new_tot, new_tot)
            updated_info_subs.append(_make_bchunk(scid, bytes(mutable)))
        else:
            updated_info_subs.append(_make_bchunk(scid, spay))

    new_info_chunk = _make_bchunk(CHUNK_GEOM_INFO, b"".join(updated_info_subs))
    if len(new_info_chunk) != len(orig_info_chunk):
        raise AssertionError("GeometryInfo length changed unexpectedly during offset update")

    root_payload = bytes(prefix_bytes) + new_info_chunk + bytes(body_after_info)
    out_bytes = _make_bchunk(CHUNK_GEOM_PACK, root_payload) + tail_bytes
    stats.pack_bytes = len(out_bytes)
    return out_bytes, stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_source_pack(args: argparse.Namespace) -> bytes:
    if args.iso is not None:
        arc = PS2NFSMWArchive(args.iso)
        entry = arc.find(args.virtual_path)
        return arc.read_entry(entry)
    if args.in_file is not None:
        return args.in_file.read_bytes()
    raise ValueError("Specify either --iso or --in")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clean-room PS2 NFSMW 0x80134000 GEOMETRY.BIN compiler & repacker."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export-obj", help="Export a named solid to rich Wavefront .obj (preserves tx & lm)")
    p_exp.add_argument("--iso", type=Path, default=None, help="Untouched PS2 ISO (read-only 'rb')")
    p_exp.add_argument("--virtual-path", type=str, default=r"CARS\BMWM3GTRE46\GEOMETRY.BIN")
    p_exp.add_argument("--in", dest="in_file", type=Path, default=None, help="Standalone GEOMETRY.BIN")
    p_exp.add_argument("--solid", type=str, required=True, help="Exact solid name (e.g. BMWM3GTRE46_KIT00_BODY_B)")
    p_exp.add_argument("--out", type=Path, required=True, help="Output .obj path (keep inside build/)")

    p_rep = sub.add_parser("replace-solid", help="Compile an .obj mesh and replace a named solid inside GEOMETRY.BIN")
    p_rep.add_argument("--iso", type=Path, default=None, help="Untouched PS2 ISO (read-only 'rb')")
    p_rep.add_argument("--virtual-path", type=str, default=r"CARS\BMWM3GTRE46\GEOMETRY.BIN")
    p_rep.add_argument("--in", dest="in_file", type=Path, default=None, help="Standalone input GEOMETRY.BIN")
    p_rep.add_argument("--solid", type=str, required=True, help="Target solid name to replace")
    p_rep.add_argument("--obj", type=Path, required=True, help="Replacement Wavefront .obj mesh")
    p_rep.add_argument("--material-map", type=Path, default=None, help="Optional JSON mapping usemtl -> [tx, lm]")
    p_rep.add_argument("--out", type=Path, required=True, help="Output repacked GEOMETRY.BIN")
    p_rep.add_argument("--scale", type=float, default=DEFAULT_SCALE, help="Position quantization scale (default: 0.0002)")
    p_rep.add_argument("--max-strip-verts", type=int, default=MAX_STRIP_VERTS, help="Max vertices per VU1 strip (default: 30)")
    p_rep.add_argument("--max-solid-bytes", type=int, default=DEFAULT_MAX_SOLID_BYTES)
    p_rep.add_argument("--max-pack-bytes", type=int, default=DEFAULT_MAX_PACK_BYTES)
    p_rep.add_argument("--json", action="store_true", help="Emit JSON summary")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.cmd == "export-obj":
        pack_data = _load_source_pack(args)
        pack_info, _ = gi.parse_geometry_pack(pack_data)
        matches = [p for p in pack_info.parts if p.name == args.solid]
        if not matches:
            raise KeyError(f"Solid '{args.solid}' not found")
        part_idx = matches[0].index
        part, mesh = gi.decode_pack_part_mesh(pack_data, part_idx)

        # Read original group metadata (strip_flags, poly_group) per (tx, lm)
        group_meta: Dict[Tuple[int, int], GroupMetadata] = {}
        p = part.chunk_offset + 8
        _, csz = struct.unpack_from("<II", pack_data, part.chunk_offset)
        end = p + csz
        while p + 8 <= end:
            scid, scsz = struct.unpack_from("<II", pack_data, p)
            if scid == CHUNK_MESH_CONT:
                mp = p + 8
                mend = mp + scsz
                while mp + 8 <= mend:
                    mcid, mcsz = struct.unpack_from("<II", pack_data, mp)
                    if mcid == CHUNK_STRIP_TABLE:
                        so = gi._skip_11_padding(pack_data, mp + 8, mp + 8 + mcsz)
                        while so + 16 <= mp + 8 + mcsz:
                            fl, _nv, _np, _md, _qo, _nz, _nt, _qs, pg, tx, lm = struct.unpack_from(
                                "<HBBfHbbBBbb", pack_data, so
                            )
                            group_meta.setdefault((tx, lm), GroupMetadata(strip_flags=fl, poly_group=pg))
                            so += 16
                    mp += 8 + mcsz
            p += 8 + scsz

        export_rich_obj(args.out, part.name, mesh, group_meta=group_meta)
        print(f"Exported {part.name}: {len(mesh.vertices)} verts, {len(mesh.triangles)} tris -> {args.out}")
        return 0

    if args.cmd == "replace-solid":
        pack_data = _load_source_pack(args)
        mat_map: Optional[Dict[str, Tuple[int, int]]] = None
        if args.material_map is not None:
            raw_map = json.loads(args.material_map.read_text(encoding="utf-8"))
            mat_map = {str(k): (int(v[0]), int(v[1])) for k, v in raw_map.items()}
        mesh = load_obj_mesh(args.obj, material_map=mat_map)
        out_bytes, stats = replace_solid_in_pack(
            pack_data,
            args.solid,
            mesh,
            scale=args.scale,
            max_strip_verts=args.max_strip_verts,
            max_solid_bytes=args.max_solid_bytes,
            max_pack_bytes=args.max_pack_bytes,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(out_bytes)
        if args.json:
            print(json.dumps(dataclasses.asdict(stats), indent=2))
        else:
            print(
                f"Replaced '{stats.part_name}': {stats.input_triangles:,} tris -> "
                f"{stats.strip_count:,} strips ({stats.strip_vertices:,} strip verts, "
                f"{stats.vif_bytes:,} B VIF), solid={stats.solid_bytes:,} B, "
                f"pack={stats.pack_bytes:,} B -> {args.out}"
            )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
