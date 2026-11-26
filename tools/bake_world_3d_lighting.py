#!/usr/bin/env python3
"""bake_world_3d_lighting.py — In-place 3D World Lighting & Geometric AO Baker for PS2 NFSMW.

Re-bakes physically-grounded 3D Hemisphere Sun/Sky Lighting and Geometric Ambient Occlusion
(curb/ground contact shadows, concave corner/alcove occlusion, and overpass/bridge undersides)
into PS2 `TRACKS\\STREAML2RA.BUN` world VIF `UNPACK V4-8` (`addr=0x02D`) vertex colors.

SAFETY GUARANTEES:
1. Source ISO is opened strictly read-only ('rb') via `tools/zdir_tool.py` and never mutated.
2. Exact byte-length identity (`len(patched) == len(orig)`): edits RGB bytes in-place inside
   existing `addr=0x02D` VIF payloads (zero LBA/chunk/DMA shifts, zero DVD5 size growth).
3. Alpha channel (byte +3 of every RGBA vertex) is never modified (bit-exact preservation).
4. Skips sky, shadow overlays, reflections, foliage, water, flares, and panoramas; protects
   emissive windows/lamps via smoothstep luma mask.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import geometry_inspect as gi  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

CHUNK_TRACK_SECTIONS = 0x00034110
SECTION_RECORD_BYTES = 92

# Verified Rockport daytime sun direction in PS2 Z-up coordinates (GLOBALB.LZC MIDDAY [5300,-3350,3850])
_RAW_SUN = (5300.0, -3350.0, 3850.0)
_SUN_MAG = math.sqrt(sum(c * c for c in _RAW_SUN))
ROCKPORT_SUN_DIR_PS2: Tuple[float, float, float] = tuple(c / _SUN_MAG for c in _RAW_SUN)  # type: ignore[assignment]

DISTRICT_LETTERS: Dict[str, Optional[frozenset[str]]] = {
    "rosewood": frozenset("AFGHIJ"),
    "downtown": frozenset("BCDEMT"),
    "camden": frozenset("LOPQRS"),
    "highway99": frozenset("ABDGIJPQ"),
    "shared": frozenset("UVWXYZ"),
    "all": None,
}
SKIP_PREFIXES = ("SKY", "SUN", "CLOUD", "SHD_", "SHADOW", "RFL_", "PAN_", "PANORAMA", "WATER", "ANM_", "XT_")
SKIP_SUBSTRS = ("_1Z_", "_2Z_", "FLARE", "GLOW", "NEON", "OCEAN", "RIVER", "WATER")


@dataclasses.dataclass(frozen=True)
class SectionRecord:
    name: str
    number: int
    file_offset: int
    size: int
    centre_x: float
    centre_y: float
    radius: float


@dataclasses.dataclass(frozen=True)
class BakeConfig:
    sun_dir: Tuple[float, float, float] = ROCKPORT_SUN_DIR_PS2
    sun_boost: float = 0.24
    shadow_darken: float = 0.24
    underside_darken: float = 0.34
    ground_ao_strength: float = 0.24
    ground_ao_height: float = 1.8
    curb_seam_darken: float = 0.16
    overhead_ao_strength: float = 0.30
    concave_ao_strength: float = 0.22
    convex_edge_lift: float = 0.05
    emissive_start: float = 204.0
    emissive_full: float = 232.0
    min_factor: float = 0.44
    max_factor: float = 1.28
    rgb_max_non_emissive: float = 242.0
    strength: float = 1.0


@dataclasses.dataclass
class StripSlice:
    positions: np.ndarray  # (nv, 3) float32
    triangles: np.ndarray  # (nt, 3) int32
    rgba: np.ndarray       # (nv, 4) uint8
    abs_vcol_offset: int


@dataclasses.dataclass
class ExtractedSolid:
    name: str
    is_local_origin: bool
    aabb_min: Tuple[float, float, float]
    aabb_max: Tuple[float, float, float]
    strips: List[StripSlice]


def should_skip_solid(name: str) -> bool:
    up = name.upper()
    return any(up.startswith(p) for p in SKIP_PREFIXES) or any(s in up for s in SKIP_SUBSTRS)


def _skip_11_bytes(data: Any, pos: int, end: int) -> int:
    while pos < end and data[pos] == 0x11:
        pos += 1
    return pos


def parse_l2ra_sections(l2ra_bytes: bytes) -> List[SectionRecord]:
    """Parse 92-byte TrackStreamingSection entries from L2RA.BUN chunk 0x00034110."""
    pos, n = 0, len(l2ra_bytes)
    while pos + 8 <= n:
        cid, csz = struct.unpack_from("<II", l2ra_bytes, pos)
        if cid == CHUNK_TRACK_SECTIONS:
            base, end = pos + 8, min(n, pos + 8 + csz)
            out: List[SectionRecord] = []
            for off in range(base, end - SECTION_RECORD_BYTES + 1, SECTION_RECORD_BYTES):
                sname = l2ra_bytes[off : off + 8].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
                snum = struct.unpack_from("<h", l2ra_bytes, off + 8)[0]
                foff, fsz = struct.unpack_from("<II", l2ra_bytes, off + 20)
                cx, cy, rad = struct.unpack_from("<3f", l2ra_bytes, off + 40)
                if fsz > 0:
                    out.append(SectionRecord(sname, int(snum), int(foff), int(fsz), cx, cy, rad))
            return out
        pos += 8 + csz
    raise ValueError("Missing 0x00034110 TrackStreamingSection table in L2RA.BUN")


def filter_sections(
    sections: Sequence[SectionRecord], district: str = "all", section_names: Optional[Iterable[str]] = None
) -> List[SectionRecord]:
    key = district.lower().strip()
    if key not in DISTRICT_LETTERS:
        raise ValueError(f"Unknown district {district!r}; expected one of {sorted(DISTRICT_LETTERS)}")
    allowed = DISTRICT_LETTERS[key]
    explicit = {s.strip().upper() for s in section_names if s.strip()} if section_names else None
    if explicit is not None:
        return [sec for sec in sections if sec.name.upper() in explicit]
    return [sec for sec in sections if allowed is None or (sec.name and sec.name[0].upper() in allowed)]


def _decode_blocked_xyz(vals: Sequence[float], nv: int) -> Tuple[List[float], List[float], List[float]]:
    xs: List[float] = []
    ys: List[float] = []
    zs: List[float] = []
    idx, rem, n_vals = 0, nv, len(vals)
    while rem > 0:
        b = 4 if rem >= 4 else rem
        if idx + 3 * b > n_vals:
            break
        xs.extend(vals[idx : idx + b])
        ys.extend(vals[idx + b : idx + 2 * b])
        zs.extend(vals[idx + 2 * b : idx + 3 * b])
        idx += 3 * b
        rem -= b
    return xs, ys, zs


def _unpack_world_vif_strip(buf: memoryview, s_start: int, s_end: int, nv: int) -> Optional[StripSlice]:
    """Decode one PS2 world VIF strip into positions, triangle indices, RGBA, and abs offset."""
    if nv <= 0 or s_start >= s_end:
        return None
    p = s_start
    v1: Optional[Tuple[int, int, int, int]] = None
    scale: Optional[float] = None
    vec2: Optional[Tuple[float, float, float]] = None
    pos_ints: List[int] = []
    pos_f32: List[float] = []
    vcol_abs_off: Optional[int] = None
    vcol_len = 0

    while p + 4 <= s_end:
        tag = struct.unpack_from("<I", buf, p)[0]
        p += 4
        cmd, num, imm = (tag >> 24) & 0xFF, (tag >> 16) & 0xFF, tag & 0xFFFF
        if cmd in (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x13, 0x14, 0x15, 0x17):
            continue
        if cmd == 0x20:
            p += 4
        elif cmd in (0x30, 0x31):
            p += 16
        elif cmd in (0x50, 0x51):
            p += imm * 16
        elif (cmd & 0x60) == 0x60:
            vn, vl, usn, addr = (cmd >> 2) & 3, cmd & 3, (imm >> 14) & 1, imm & 0x3FF
            cnt = 256 if num == 0 else num
            eb = (32, 16, 8, 16)[vl]
            blen = (((16 if vl == 3 else (vn + 1) * eb) * cnt + 31) // 32) * 4
            if p + blen > s_end:
                break
            if addr == 1 and blen >= 16:
                v1 = struct.unpack_from("<4I", buf, p)
                scale = struct.unpack_from("<f", buf, p + 4)[0]
            elif addr == 2 and blen >= 12:
                vec2 = struct.unpack_from("<3f", buf, p)
            elif 5 <= addr < 0x1D and vl == 1:
                pos_ints.extend(struct.unpack_from(f"<{blen // 2}" + ("H" if usn else "h"), buf, p))
            elif 5 <= addr < 0x1D and vl == 0:
                pos_f32.extend(struct.unpack_from(f"<{blen // 4}f", buf, p))
            elif addr == 0x2D and vn == 3 and vl == 2 and usn == 1:
                vcol_abs_off, vcol_len = p, blen
            p += blen
        else:
            break

    if vcol_abs_off is None or vcol_len < nv * 4:
        return None
    if pos_f32:
        xs, ys, zs = _decode_blocked_xyz(pos_f32, nv)
        if len(xs) < nv:
            return None
        positions = np.column_stack((xs[:nv], ys[:nv], zs[:nv])).astype(np.float32)
    elif pos_ints and vec2 is not None and scale is not None:
        xs_i, ys_i, zs_i = _decode_blocked_xyz(pos_ints, nv)
        if len(xs_i) < nv:
            return None
        bases = np.array([float(int(vec2[a]) - 1) for a in range(3)], dtype=np.float32)
        positions = bases + np.column_stack((xs_i[:nv], ys_i[:nv], zs_i[:nv])).astype(np.float32) * float(scale)
    else:
        return None

    adcs = gi.decode_vu_adc_bits(v1, nv) if v1 is not None else [1, 1] + [0] * max(0, nv - 2)
    tris: List[Tuple[int, int, int]] = []
    parity = 0
    for i in range(2, nv):
        if adcs[i]:
            parity = 0
            continue
        tris.append((i - 2, i - 1, i) if parity == 0 else (i - 1, i - 2, i))
        parity ^= 1
    tri_arr = np.asarray(tris, dtype=np.int32) if tris else np.empty((0, 3), dtype=np.int32)
    rgba = np.frombuffer(buf[vcol_abs_off : vcol_abs_off + nv * 4], dtype=np.uint8).reshape(nv, 4).copy()
    return StripSlice(positions, tri_arr, rgba, vcol_abs_off)


def extract_section_solids(stream_mv: memoryview, sec_offset: int, sec_size: int) -> Tuple[List[ExtractedSolid], int]:
    """Extract all bakeable solids inside one STREAML2RA section; returns (solids, skipped)."""
    solids: List[ExtractedSolid] = []
    skipped = 0
    sec_end = min(len(stream_mv), sec_offset + sec_size)
    p = sec_offset
    while p + 8 <= sec_end:
        cid, csz = struct.unpack_from("<II", stream_mv, p)
        pay_end = min(sec_end, p + 8 + csz)
        if cid == gi.CHUNK_GEOM_PACK:
            sp = p + 8
            while sp + 8 <= pay_end:
                scid, scsz = struct.unpack_from("<II", stream_mv, sp)
                send = min(pay_end, sp + 8 + scsz)
                if scid == gi.CHUNK_SOLID_PACK:
                    q, name = sp + 8, ""
                    aabb_min = aabb_max = (0.0, 0.0, 0.0)
                    strip_entries: List[Tuple[int, int, int]] = []
                    vif_start = vif_end = 0
                    while q + 8 <= send:
                        ccid, ccsz = struct.unpack_from("<II", stream_mv, q)
                        q_pay, q_end = q + 8, min(send, q + 8 + ccsz)
                        if ccid == gi.CHUNK_ESOLID_HDR:
                            ho = gi._skip_11_word_padding(stream_mv, q_pay, q_end)
                            if ho + 0xA0 <= q_end:
                                aabb_min = struct.unpack_from("<3f", stream_mv, ho + 0x20)
                                aabb_max = struct.unpack_from("<3f", stream_mv, ho + 0x30)
                                name = bytes(stream_mv[ho + 0xA0 : q_end]).split(b"\x00", 1)[0].decode("ascii", errors="replace")
                        elif ccid == gi.CHUNK_MESH_CONT:
                            mp = q_pay
                            while mp + 8 <= q_end:
                                mcid, mcsz = struct.unpack_from("<II", stream_mv, mp)
                                mp_pay, mp_end = mp + 8, min(q_end, mp + 8 + mcsz)
                                if mcid == gi.CHUNK_STRIP_TABLE:
                                    so = _skip_11_bytes(stream_mv, mp_pay, mp_end)
                                    while so + 16 <= mp_end:
                                        _fl, nv, _np, _md, qoff, _nz, _nt, qsz, _pg, _tx, _lm = struct.unpack_from("<HBBfHbbBBbb", stream_mv, so)
                                        strip_entries.append((nv, qoff, qsz))
                                        so += 16
                                elif mcid == gi.CHUNK_VIF_DATA:
                                    vif_start, vif_end = _skip_11_bytes(stream_mv, mp_pay, mp_end), mp_end
                                mp = mp_pay + mcsz
                        q = q_pay + ccsz
                    if not strip_entries or vif_start >= vif_end or should_skip_solid(name):
                        skipped += 1
                    else:
                        decoded = [
                            sl for nv, qoff, qsz in strip_entries
                            if (sl := _unpack_world_vif_strip(stream_mv, vif_start + qoff * 16, min(vif_end, vif_start + (qoff + qsz) * 16), nv)) is not None
                        ]
                        if decoded:
                            cx = 0.5 * (aabb_min[0] + aabb_max[0])
                            cy = 0.5 * (aabb_min[1] + aabb_max[1])
                            is_local = (not name.upper().startswith("TRN_")) and abs(cx) < 25.0 and abs(cy) < 25.0
                            solids.append(ExtractedSolid(name, is_local, aabb_min, aabb_max, decoded))
                        else:
                            skipped += 1
                sp += 8 + scsz
        p += 8 + csz
    return solids, skipped


def _voxel_keys(pos: np.ndarray, cell: float, dim3: bool = False) -> Tuple[np.ndarray, int]:
    ix = np.floor(pos[:, 0] / cell).astype(np.int64)
    iy = np.floor(pos[:, 1] / cell).astype(np.int64)
    if dim3:
        iz = np.floor(pos[:, 2] / cell).astype(np.int64)
        keys = ((ix & 0x1FFFFF) << 42) | ((iy & 0x1FFFFF) << 21) | (iz & 0x1FFFFF)
    else:
        keys = ((ix & 0xFFFFFFFF) << 32) | (iy & 0xFFFFFFFF)
    uniq, inv = np.unique(keys, return_inverse=True)
    return inv, len(uniq)


def compute_3d_vertex_colors(
    positions: np.ndarray,
    triangles: np.ndarray,
    rgba_in: np.ndarray,
    *,
    config: BakeConfig = BakeConfig(),
    is_local_solid: bool = False,
    aabb_min_z: Optional[float] = None,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Compute physically-grounded 3D Sun/Sky Hemisphere + Geometric AO vertex colors."""
    n = len(positions)
    if n == 0:
        return rgba_in.copy(), {"ao_sum": 0.0, "ao_min": 1.0, "emissive_count": 0, "sunlit_delta_sum": 0.0, "sunlit_count": 0, "shadow_delta_sum": 0.0, "shadow_count": 0, "underside_delta_sum": 0.0, "underside_count": 0}

    pos = np.nan_to_num(positions.astype(np.float32, copy=False), nan=0.0, posinf=0.0, neginf=0.0)
    normals = np.zeros((n, 3), dtype=np.float32)
    if len(triangles) > 0:
        fn = np.cross(pos[triangles[:, 1]] - pos[triangles[:, 0]], pos[triangles[:, 2]] - pos[triangles[:, 0]])
        for k in range(3):
            np.add.at(normals, triangles[:, k], fn)
    n_len = np.linalg.norm(normals, axis=1, keepdims=True)
    valid = n_len[:, 0] > 1e-6
    normals[valid] /= n_len[valid]
    normals[~valid] = (0.0, 0.0, 1.0)
    nz = np.clip(normals[:, 2], -1.0, 1.0)

    # 1. Directional Rockport Sun + Sky Hemisphere separation (warm sunlit vs cool-slate shadow)
    sun = np.asarray(config.sun_dir, dtype=np.float32)
    sun = sun / max(float(np.linalg.norm(sun)), 1e-6)
    ndotl = np.clip(normals @ sun, -1.0, 1.0)
    pos_sun, neg_sun = np.maximum(ndotl, 0.0), np.maximum(-ndotl, 0.0)
    sun_w = pos_sun * (1.0 - 0.65 * np.maximum(nz, 0.0))

    dir_rgb = np.ones((n, 3), dtype=np.float32)
    for ch, s_mul, sh_mul in ((0, 1.15, 1.15), (1, 0.98, 0.98), (2, 0.72, 0.72)):
        dir_rgb[:, ch] = (1.0 + config.sun_boost * s_mul * sun_w) * (1.0 - config.shadow_darken * sh_mul * neg_sun)
    dir_rgb *= (1.0 - config.underside_darken * np.power(np.maximum(-nz, 0.0), 0.85))[:, None]

    # 2. Spatial Column Ground-Contact AO + Overhead Bridge/Overpass Shadow AO
    inv_xy, n_xy = _voxel_keys(pos, 2.5, dim3=False)
    z = pos[:, 2]
    if is_local_solid:
        z_lo = float(z.min()) if aabb_min_z is None or not math.isfinite(aabb_min_z) else float(aabb_min_z)
        z_ground = np.full(n, 0.0 if (z_lo < -0.25 and float(z.max()) > 1.5) else z_lo, dtype=np.float32)
    else:
        col_zmin = np.full(n_xy, np.inf, dtype=np.float32)
        up_m = nz > 0.45
        if np.any(up_m):
            np.minimum.at(col_zmin, inv_xy[up_m], z[up_m])
        if np.any(~np.isfinite(col_zmin)):
            inv_c, n_c = _voxel_keys(pos, 5.5, dim3=False)
            c_zmin = np.full(n_c, np.inf, dtype=np.float32)
            if np.any(up_m):
                np.minimum.at(c_zmin, inv_c[up_m], z[up_m])
            np.minimum.at(c_zmin, inv_c, z)
            z_ground = np.where(np.isfinite(col_zmin[inv_xy]), col_zmin[inv_xy], c_zmin[inv_c])
        else:
            z_ground = col_zmin[inv_xy]

    h_above = np.maximum(0.0, z - z_ground)
    wall_m = np.clip((0.75 - nz) / 0.45, 0.0, 1.0)
    ao_ground = 1.0 - config.ground_ao_strength * wall_m * np.exp(-h_above / max(0.25, config.ground_ao_height * 0.40))

    col_deck = np.full(n_xy, -np.inf, dtype=np.float32)
    col_over = np.full(n_xy, -np.inf, dtype=np.float32)
    if np.any(np.abs(nz) > 0.55):
        np.maximum.at(col_deck, inv_xy[np.abs(nz) > 0.55], z[np.abs(nz) > 0.55])
    if np.any(nz < -0.35):
        np.maximum.at(col_over, inv_xy[nz < -0.35], z[nz < -0.35])
    dz_deck, dz_over = col_deck[inv_xy] - z, col_over[inv_xy] - z
    under_br = (nz > 0.35) & (dz_deck >= 2.2) & (dz_deck <= 18.0)
    under_ev = (~under_br) & (nz > -0.25) & (dz_over >= 1.5) & (dz_over <= 14.0)
    occ_w = np.where(under_br, np.clip(1.0 - (dz_deck - 2.2) / 18.0, 0.40, 1.0), np.where(under_ev, 0.80 * np.clip(1.0 - (dz_over - 1.5) / 14.0, 0.35, 1.0), 0.0))
    ao_overhead = 1.0 - config.overhead_ao_strength * occ_w

    # 3. 3D Concave Corner, Recessed Alcove & Curb-Meets-Wall Seam AO
    inv_3d, n_3d = _voxel_keys(pos, 1.8, dim3=True)
    cell_cnt = np.bincount(inv_3d, minlength=n_3d).astype(np.float32)
    cell_wall = np.zeros(n_3d, dtype=bool)
    np.logical_or.at(cell_wall, inv_3d, np.abs(nz) < 0.45)
    ao_seam = np.where((nz > 0.60) & cell_wall[inv_3d], 1.0 - config.curb_seam_darken, 1.0).astype(np.float32)

    nbr_sum, nbr_cnt = np.zeros((n, 3), dtype=np.float32), np.zeros(n, dtype=np.float32)
    if len(triangles) > 0:
        t0, t1, t2 = triangles[:, 0], triangles[:, 1], triangles[:, 2]
        for a_i, b_i in ((t0, t1), (t1, t0), (t1, t2), (t2, t1), (t2, t0), (t0, t2)):
            np.add.at(nbr_sum, a_i, pos[b_i])
            np.add.at(nbr_cnt, a_i, 1.0)
    topo_conc = np.zeros(n, dtype=np.float32)
    has_nbr = nbr_cnt > 0.0
    if np.any(has_nbr):
        d_t = (nbr_sum[has_nbr] / nbr_cnt[has_nbr, None]) - pos[has_nbr]
        dl = np.linalg.norm(d_t, axis=1)
        topo_conc[has_nbr] = (np.sum(d_t * normals[has_nbr], axis=1) / (dl + 1e-5)) * np.clip(dl / 0.15, 0.0, 1.0)

    denom = np.maximum(cell_cnt, 1.0)
    c_pos = np.column_stack([np.bincount(inv_3d, weights=pos[:, ax], minlength=n_3d) / denom for ax in range(3)])
    c_nrm = np.column_stack([np.bincount(inv_3d, weights=normals[:, ax], minlength=n_3d) / denom for ax in range(3)])
    c_div = np.clip(1.0 - np.linalg.norm(c_nrm, axis=1), 0.0, 1.0)
    d_v = c_pos[inv_3d] - pos
    dv_l = np.linalg.norm(d_v, axis=1)
    vox_conc = np.where(
        (cell_cnt[inv_3d] >= 2.0) & (c_div[inv_3d] > 0.08),
        (np.sum(d_v * normals, axis=1) / (dv_l + 1e-5)) * np.clip(dv_l / 0.20, 0.0, 1.0),
        0.0,
    )
    conc = np.where(topo_conc > 0.0, np.maximum(topo_conc, vox_conc), np.maximum(topo_conc, np.maximum(vox_conc, 0.0)))
    ao_concave = np.where(conc >= 0.0, 1.0 - config.concave_ao_strength * np.clip(conc, 0.0, 1.0), 1.0 + config.convex_edge_lift * np.clip(-conc, 0.0, 1.0)).astype(np.float32)

    # 4. Combine with physical direct-sunlight AO bounce, emissive protection, safe clamps, exact alpha
    raw_ao = (ao_ground * ao_overhead * ao_seam * ao_concave).astype(np.float32)
    eff_ao = (1.0 - (1.0 - raw_ao) * (1.0 - 0.30 * sun_w)).astype(np.float32)
    factor_rgb = 1.0 + float(config.strength) * (np.clip(dir_rgb * eff_ao[:, None], config.min_factor, config.max_factor) - 1.0)

    rgb_in = rgba_in[:, :3].astype(np.float32)
    luma_in = 0.299 * rgb_in[:, 0] + 0.587 * rgb_in[:, 1] + 0.114 * rgb_in[:, 2]
    em_t = np.clip((np.maximum(rgb_in.max(axis=1), luma_in + 8.0) - config.emissive_start) / max(1.0, config.emissive_full - config.emissive_start), 0.0, 1.0)
    em_p = em_t * em_t * (3.0 - 2.0 * em_t)

    out_rgb = np.minimum(rgb_in * (factor_rgb * (1.0 - em_p[:, None]) + em_p[:, None]), np.maximum(rgb_in, config.rgb_max_non_emissive))
    out_rgba = np.empty_like(rgba_in)
    out_rgba[:, :3] = np.clip(np.rint(out_rgb), 0.0, 255.0).astype(np.uint8)
    out_rgba[:, 3] = rgba_in[:, 3]

    delta = (0.299 * out_rgba[:, 0] + 0.587 * out_rgba[:, 1] + 0.114 * out_rgba[:, 2]) - luma_in
    sun_m, shd_m, und_m = (ndotl > 0.35) & (np.abs(nz) < 0.55), (ndotl < -0.35) & (np.abs(nz) < 0.55), nz < -0.45
    return out_rgba, {
        "ao_sum": float(eff_ao.sum()), "ao_min": float(eff_ao.min()), "emissive_count": int(np.sum(em_p > 0.5)),
        "sunlit_delta_sum": float(delta[sun_m].sum()), "sunlit_count": int(np.sum(sun_m)),
        "shadow_delta_sum": float(delta[shd_m].sum()), "shadow_count": int(np.sum(shd_m)),
        "underside_delta_sum": float(delta[und_m].sum()), "underside_count": int(np.sum(und_m)),
    }


def _bake_solid_batch(
    solids: Sequence[ExtractedSolid], out_buf: Optional[bytearray], *, config: BakeConfig, is_local_solid: bool, aabb_min_z: Optional[float]
) -> Dict[str, Any]:
    strips = [sl for sol in solids for sl in sol.strips]
    if not strips:
        return {}
    nv = sum(len(sl.positions) for sl in strips)
    pos_all = np.empty((nv, 3), dtype=np.float32)
    rgba_all = np.empty((nv, 4), dtype=np.uint8)
    tri_parts: List[np.ndarray] = []
    cur = 0
    for sl in strips:
        m = len(sl.positions)
        pos_all[cur : cur + m], rgba_all[cur : cur + m] = sl.positions, sl.rgba
        if len(sl.triangles) > 0:
            tri_parts.append(sl.triangles + cur)
        cur += m
    tri_all = np.concatenate(tri_parts, axis=0) if tri_parts else np.empty((0, 3), dtype=np.int32)
    out_rgba, st = compute_3d_vertex_colors(pos_all, tri_all, rgba_all, config=config, is_local_solid=is_local_solid, aabb_min_z=aabb_min_z)

    if out_buf is not None:
        cur = 0
        for sl in strips:
            m = len(sl.positions)
            out_buf[sl.abs_vcol_offset : sl.abs_vcol_offset + m * 4] = out_rgba[cur : cur + m].tobytes()
            cur += m

    lb = 0.299 * rgba_all[:, 0] + 0.587 * rgba_all[:, 1] + 0.114 * rgba_all[:, 2]
    la = 0.299 * out_rgba[:, 0] + 0.587 * out_rgba[:, 1] + 0.114 * out_rgba[:, 2]
    d = la - lb
    st.update({
        "strips": len(strips), "verts": nv, "tris": len(tri_all),
        "luma_before_sum": float(lb.sum()), "luma_after_sum": float(la.sum()),
        "delta_min": float(d.min()), "delta_max": float(d.max()), "delta_sq_sum": float(np.sum(d * d)),
        "alpha_diffs": int(np.sum(out_rgba[:, 3] != rgba_all[:, 3])),
    })
    return st


def bake_streaml2ra_bytes(
    l2ra_bytes: bytes,
    stream_bytes: bytes,
    *,
    district: str = "all",
    section_names: Optional[Sequence[str]] = None,
    config: BakeConfig = BakeConfig(),
    dry_run: bool = False,
) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """Bake 3D world lighting + geometric AO in-place across selected STREAML2RA.BUN sections."""
    t0 = time.perf_counter()
    all_secs = parse_l2ra_sections(l2ra_bytes)
    selected = filter_sections(all_secs, district=district, section_names=section_names)
    mv = memoryview(stream_bytes)
    out_buf: Optional[bytearray] = None if dry_run else bytearray(stream_bytes)

    sum_keys = ("strips", "verts", "tris", "emissive_count", "alpha_diffs", "luma_before_sum", "luma_after_sum", "delta_sq_sum", "ao_sum", "sunlit_delta_sum", "sunlit_count", "shadow_delta_sum", "shadow_count", "underside_delta_sum", "underside_count")
    acc: Dict[str, Any] = {k: 0 for k in sum_keys}
    acc.update({"sections_baked": 0, "solids_baked": 0, "solids_skipped": 0, "ao_min": 1.0, "delta_min": 0.0, "delta_max": 0.0})
    seen = False

    def _add(st: Dict[str, Any]) -> None:
        nonlocal seen
        if not st:
            return
        for k in sum_keys:
            acc[k] += st[k]
        acc["ao_min"] = min(acc["ao_min"], st["ao_min"])
        acc["delta_min"] = st["delta_min"] if not seen else min(acc["delta_min"], st["delta_min"])
        acc["delta_max"] = st["delta_max"] if not seen else max(acc["delta_max"], st["delta_max"])
        seen = True

    for sec in selected:
        solids, skipped = extract_section_solids(mv, sec.file_offset, sec.size)
        acc["solids_skipped"] += skipped
        if not solids:
            continue
        acc["sections_baked"] += 1
        acc["solids_baked"] += len(solids)
        world_s = [s for s in solids if not s.is_local_origin]
        if world_s:
            _add(_bake_solid_batch(world_s, out_buf, config=config, is_local_solid=False, aabb_min_z=None))
        for sol in (s for s in solids if s.is_local_origin):
            _add(_bake_solid_batch([sol], out_buf, config=config, is_local_solid=True, aabb_min_z=sol.aabb_min[2]))

    out_bytes = bytes(out_buf) if out_buf is not None else None
    if out_bytes is not None and len(out_bytes) != len(stream_bytes):
        raise RuntimeError(f"Byte-length invariant violated: {len(out_bytes)} != {len(stream_bytes)}")
    if acc["alpha_diffs"] != 0:
        raise RuntimeError(f"Alpha preservation invariant violated: {acc['alpha_diffs']} bytes changed")

    nv = max(1, acc["verts"])
    mb, ma = acc["luma_before_sum"] / nv, acc["luma_after_sum"] / nv
    md = ma - mb
    report = {
        "district": district, "dry_run": dry_run, "strength": config.strength,
        "sections_total": len(all_secs), "sections_selected": len(selected), "sections_baked": acc["sections_baked"],
        "solids_baked": acc["solids_baked"], "solids_skipped": acc["solids_skipped"],
        "strips_baked": acc["strips"], "vertices_baked": acc["verts"], "triangles_decoded": acc["tris"],
        "emissive_vertices_protected": acc["emissive_count"], "alpha_bytes_modified": acc["alpha_diffs"],
        "orig_bytes": len(stream_bytes), "patched_bytes": len(out_bytes) if out_bytes is not None else len(stream_bytes),
        "byte_length_identical": True,
        "luma_before_mean": round(mb, 3), "luma_after_mean": round(ma, 3),
        "delta_luma_min": round(acc["delta_min"], 3), "delta_luma_mean": round(md, 3),
        "delta_luma_max": round(acc["delta_max"], 3),
        "delta_luma_std": round(math.sqrt(max(0.0, acc["delta_sq_sum"] / nv - md * md)), 3),
        "ao_mean": round(acc["ao_sum"] / nv, 4), "ao_min": round(acc["ao_min"], 4),
        "sunlit_mean_delta": round(acc["sunlit_delta_sum"] / max(1, acc["sunlit_count"]), 3),
        "shadow_mean_delta": round(acc["shadow_delta_sum"] / max(1, acc["shadow_count"]), 3),
        "underside_mean_delta": round(acc["underside_delta_sum"] / max(1, acc["underside_count"]), 3),
        "elapsed_seconds": round(time.perf_counter() - t0, 3),
    }
    return out_bytes, report


def main(argv: Optional[Sequence[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Bake 3D Sun/Sky Lighting & Geometric AO in-place into PS2 STREAML2RA.BUN.")
    ap.add_argument("--iso", type=Path, default=None, help="Untouched retail SLUS-21351 ISO (read-only 'rb')")
    ap.add_argument("--l2ra", type=Path, default=None, help="Standalone TRACKS\\L2RA.BUN")
    ap.add_argument("--stream", type=Path, default=None, help="Standalone TRACKS\\STREAML2RA.BUN")
    ap.add_argument("--district", choices=sorted(DISTRICT_LETTERS), default="all")
    ap.add_argument("--sections", type=str, default="", help="Optional comma-separated section names (e.g. A41,D41)")
    ap.add_argument("--output", type=Path, default=repo_root / "build" / "world3d_v27" / "STREAML2RA.BUN")
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.iso is not None:
        arc = PS2NFSMWArchive(args.iso.expanduser().resolve())
        l2ra_bytes = arc.read_entry(arc.find(r"TRACKS\L2RA.BUN"))
        stream_bytes = arc.read_entry(arc.find(r"TRACKS\STREAML2RA.BUN"))
    elif args.l2ra is not None and args.stream is not None:
        l2ra_bytes = args.l2ra.expanduser().resolve().read_bytes()
        stream_bytes = args.stream.expanduser().resolve().read_bytes()
    else:
        ap.error("Provide either --iso <ISO_PATH> or both --l2ra <L2RA.BUN> and --stream <STREAML2RA.BUN>")

    sec_filter = [s for s in args.sections.split(",") if s.strip()] if args.sections else None
    patched, rep = bake_streaml2ra_bytes(
        l2ra_bytes, stream_bytes, district=args.district, section_names=sec_filter, config=BakeConfig(strength=float(args.strength)), dry_run=args.dry_run
    )
    if patched is not None:
        out_path = args.output.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(patched)
        rep["output_path"] = str(out_path)
        out_path.with_name("bake_report.json").write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(
            f"v2.7 3D World Lighting Bake ({rep['district']}, dry_run={rep['dry_run']}):\n"
            f"  Sections : {rep['sections_baked']}/{rep['sections_selected']} selected ({rep['sections_total']} total)\n"
            f"  Geometry : {rep['solids_baked']:,} solids ({rep['solids_skipped']:,} skipped) | "
            f"{rep['strips_baked']:,} strips | {rep['vertices_baked']:,} verts | {rep['triangles_decoded']:,} tris\n"
            f"  Shading  : luma {rep['luma_before_mean']:.2f} -> {rep['luma_after_mean']:.2f} "
            f"(delta min={rep['delta_luma_min']:+.1f}, mean={rep['delta_luma_mean']:+.2f}, max={rep['delta_luma_max']:+.1f}, std={rep['delta_luma_std']:.2f})\n"
            f"  3D Pop   : sunlit={rep['sunlit_mean_delta']:+.2f} | shadow={rep['shadow_mean_delta']:+.2f} | "
            f"underside={rep['underside_mean_delta']:+.2f} | AO mean={rep['ao_mean']:.3f} (min={rep['ao_min']:.3f})\n"
            f"  Safety   : emissive_protected={rep['emissive_vertices_protected']:,} | "
            f"alpha_diffs={rep['alpha_bytes_modified']} | bytes={rep['patched_bytes']:,} (identical={rep['byte_length_identical']}) | time={rep['elapsed_seconds']:.2f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
