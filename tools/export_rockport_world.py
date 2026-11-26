#!/usr/bin/env python3
"""export_rockport_world.py — Authentic PS2 NFSMW Rockport City (.rkpt v1) Exporter.

Extracts full-detail Rockport City districts directly from an untouched PS2
Need for Speed: Most Wanted - Black Edition (SLUS-21351) ISO (opened strictly
read-only 'rb') via TRACKS\\L2RA.BUN (0x00034110 section table) and
TRACKS\\STREAML2RA.BUN (world 0x80134000 solids, 0x80034100 SceneryGroups,
X*/V*/U* shared prop libraries, and Y*/V*/local/global 0xB3300000 TPKs).

Outputs the locked little-endian `.rkpt` v1 binary format consumed directly by
`native_metal_rt/` alongside a JSON manifest and Pillow-validated top-down
`<district>_layout_check.png`.

Coordinate Convention (proper right-handed rotation, det = +1):
  PS2 Z-up world meters (x, y, z) -> Metal Y-up world meters:
    P(x, y, z) = (x, z, -y)
  Local vertices are converted once: v_metal = P(v_ps2).
  Each SceneryInstance (w_ps2 = v_ps2 * R_ps2 + t_ps2, R_ps2 = int16[9] / 8192)
  becomes column-major MTLPackedFloat4x3:
    A_metal = P @ R_ps2.T @ P.T
    t_metal = (px - ox, pz - oz, -(py - oy))
  eSolid.m44 (pad + 0x40) is NEVER applied to world solids.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import math
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import geometry_inspect as gi  # noqa: E402
import tpk_extract as te  # noqa: E402
import tpk_inspect as tpki  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

# ---------------------------------------------------------------------------
# Locked .rkpt v1 Binary Contract Constants & Struct Formats
# ---------------------------------------------------------------------------

RKPT_MAGIC = b"RKPT"
RKPT_VERSION = 1

HEADER_FMT = "<4s9I3f3f3ff8Q3fI"      # 160 bytes
SECTION_FMT = "<8s3I3ff3f3fI"         # 64 bytes
MESH_FMT = "<6I3f3f"                  # 48 bytes
INSTANCE_FMT = "<4I12f"               # 64 bytes
MATERIAL_FMT = "<3I4fI"               # 32 bytes
TEXTURE_FMT = "<4IQ2I"                # 32 bytes
VERTEX_FMT = "<3f3f2fHBB"             # 36 bytes

HEADER_SIZE = struct.calcsize(HEADER_FMT)
SECTION_SIZE = struct.calcsize(SECTION_FMT)
MESH_SIZE = struct.calcsize(MESH_FMT)
INSTANCE_SIZE = struct.calcsize(INSTANCE_FMT)
MATERIAL_SIZE = struct.calcsize(MATERIAL_FMT)
TEXTURE_SIZE = struct.calcsize(TEXTURE_FMT)
VERTEX_SIZE = struct.calcsize(VERTEX_FMT)

assert HEADER_SIZE == 160
assert SECTION_SIZE == 64
assert MESH_SIZE == 48
assert INSTANCE_SIZE == 64
assert MATERIAL_SIZE == 32
assert TEXTURE_SIZE == 32
assert VERTEX_SIZE == 36

# Mesh flags
MESH_FLAG_HAS_ALPHA_CUTOUT = 1 << 0
MESH_FLAG_DRIVABLE_ROAD = 1 << 1
MESH_FLAG_SOLID_WALL = 1 << 2
MESH_FLAG_TERRAIN_CHOP = 1 << 3
MESH_FLAG_SKY_OR_PANORAMA = 1 << 4

# Material surface classes
SURFACE_CONCRETE_WALL = 0
SURFACE_ROAD_ASPHALT = 1
SURFACE_SIDEWALK = 2
SURFACE_BUILDING_GLASS = 3
SURFACE_FOLIAGE_CUTOUT = 4
SURFACE_METAL_BRIDGE = 5
SURFACE_EMISSIVE_SIGN_LAMP = 6
SURFACE_WATER = 7
SURFACE_TERRAIN_GRASS_DIRT = 8

SURFACE_CLASS_NAMES: Dict[int, str] = {
    SURFACE_CONCRETE_WALL: "CONCRETE_WALL",
    SURFACE_ROAD_ASPHALT: "ROAD_ASPHALT",
    SURFACE_SIDEWALK: "SIDEWALK",
    SURFACE_BUILDING_GLASS: "BUILDING_GLASS",
    SURFACE_FOLIAGE_CUTOUT: "FOLIAGE_CUTOUT",
    SURFACE_METAL_BRIDGE: "METAL_BRIDGE",
    SURFACE_EMISSIVE_SIGN_LAMP: "EMISSIVE_SIGN_LAMP",
    SURFACE_WATER: "WATER",
    SURFACE_TERRAIN_GRASS_DIRT: "TERRAIN_GRASS_DIRT",
}

# Material flags
MAT_FLAG_ALPHA_CUTOUT = 1 << 0
MAT_FLAG_DOUBLE_SIDED = 1 << 1
MAT_FLAG_DRIVABLE_ROAD = 1 << 2
MAT_FLAG_EMISSIVE = 1 << 3
MAT_FLAG_WET_REFLECTIVE = 1 << 4

# Texture flags
TEX_FLAG_HAS_CUTOUT_ALPHA = 1 << 0
TEX_FLAG_PS2_0X80_NORMALIZED = 1 << 1
TEX_FLAG_ENHANCED_2X = 1 << 2

TEX_INDEX_NONE = 0xFFFFFFFF

# EA World Chunk IDs
CHUNK_STREAM_SECTIONS = 0x00034110
CHUNK_SCENERY_GROUP = 0x80034100
CHUNK_SCENERY_INFO = 0x00034102
CHUNK_SCENERY_INSTANCE = 0x00034103

SECTION_ENTRY_STRIDE = 0x5C   # 92 bytes
SCENERY_INFO_STRIDE = 0x48    # 72 bytes
SCENERY_INST_STRIDE = 0x40    # 64 bytes

# Verified Pilot Districts (paired (K, K+40) < 80, excluding 80..99 panoramas)
PILOT_DISTRICTS: Dict[str, Dict[str, Any]] = {
    "rosewood": {
        "display_name": "Central Rosewood (Safe House / Rosewood Blvd / Police Station)",
        "sections": (
            "A26", "A44", "A45", "A47", "A48", "A49", "A50", "A51",
            "A52", "A56", "A59", "A66", "A67", "A71", "A76",
            "G59", "I17", "I19", "I24", "I57", "I59", "I63", "I64", "I65", "I66",
        ),
        # Focal landmark in PS2 Z-up meters (Rosewood Blvd between Safe House I19 & Police Station A76)
        "spawn_focus_ps2": (1380.0, 2340.0),
    },
    "downtown": {
        "display_name": "Downtown Rockport (East Park Safe House / Downtown Core)",
        "sections": (
            "B50", "B53", "B54", "B55", "B56", "B57",
            "D5", "D26", "D41", "D42", "D43", "D44", "D45", "D46",
            "D51", "D52", "D54", "D66", "D67", "D68", "D72", "D73",
            "L45", "O41", "O42", "O51", "O54", "P62",
        ),
        # Focal landmark in PS2 Z-up meters (Downtown Rockport East Park core)
        "spawn_focus_ps2": (2120.0, 120.0),
    },
}

DEFAULT_SOURCE_ISO = (
    Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
)

# Proper right-handed rotation matrix P(x, y, z) = (x, z, -y), det(P) = +1
P_PS2_TO_METAL = np.array(
    [
        [1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, -1.0, 0.0],
    ],
    dtype=np.float64,
)

# Excluded world solid prefixes (shadow overlays, distant panoramas, reflection proxies, dev temp gates)
EXCLUDED_SOLID_PREFIXES = ("SHD_", "PAN_", "RFL_", "SKY_", "SKYDOME", "OCEAN", "__TEMP")

# Excluded Free-Roam-invisible race-event barrier solid substrings / texture prefixes
EXCLUDED_EVENT_SOLID_SUBSTRINGS = (
    "TRACKBARRIER",
    "STARTGATE",
    "FINISHGATE",
    "RACEBARRIER",
)
EXCLUDED_EVENT_TEXTURE_SUBSTRINGS = (
    "SFX_TRACKBARRIER",
)

# Pillow resampling compatibility across Pillow 9/10/11
_RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
_RESAMPLE_BILINEAR = getattr(getattr(Image, "Resampling", Image), "BILINEAR")

# Window / storefront glass matcher (catches DOORWIN, WIN1/2, _WIN, _WINA/B, WINDOW, GLASS, STOREFRONT)
BUILDING_GLASS_RE = re.compile(
    r"(WINDOW|DOORWIN|GLASS|STOREFRONT|SKYLIGHT|SHOWROOM|BLD\d+WIN|_WIN(?:$|[A-Z0-9_])|WIN\d+(?:QUA)?$)"
)

# Foliage / tree texture matcher (avoids matching STREETLIGHT via substring 'TREE')
FOLIAGE_TEX_RE = re.compile(
    r"(^ORG_|TREELINE|FALLTREE|TREE|CYPRESS|POPLAR|BUSH|HEDGE|LEAF|LEAVES|FOLIAGE|BRANCH|FIR_|PINE|SHRUB|VINE|IVY|PLANT|FLORA)"
)

# Hero close-up textures eligible for 4x upscaling up to 512px max edge
HERO_4X_TEX_RE = re.compile(
    r"(^(?:SGN_|ADS_|ORG_)|BRICK|AWNING|DOORWIN|LITALY|STORE|SHOP|FIREHOUSE|FIREHALL|POLICE|SAFEHOUSE|HERITAGE|CYPRESS|FALLTREE|POPLAR|HEDGE|SIGN|DONOTENTER|SPEEDLIMIT)"
)


# ---------------------------------------------------------------------------
# Coordinate Conversion Helpers
# ---------------------------------------------------------------------------


def ps2_point_to_metal(
    x: float, y: float, z: float, origin_ps2: Tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> Tuple[float, float, float]:
    """Convert PS2 Z-up world/local meters to Metal Y-up right-handed meters (det = +1).

    P(x, y, z) = (x - ox, z - oz, -(y - oy)).
    """
    ox, oy, oz = origin_ps2
    return (float(x - ox), float(z - oz), float(-(y - oy)))


def ps2_instance_to_metal_4x3(
    rot9_i16: Sequence[int],
    pos_ps2: Tuple[float, float, float],
    origin_ps2: Tuple[float, float, float],
    is_identity: bool = False,
) -> Tuple[float, ...]:
    """Convert PS2 SceneryInstance (R_ps2 rows = int16[9]/8192, t_ps2) to MTLPackedFloat4x3.

    Returns 12 float32-compatible values in column-major order:
      (col0.x, col0.y, col0.z, col1.x, col1.y, col1.z, col2.x, col2.y, col2.z, tx, ty, tz)
    satisfying:
      A_metal @ P(v_ps2) + t_metal == P((v_ps2 @ R_ps2 + pos_ps2) - origin_ps2).
    """
    if is_identity or all(v == 0 for v in rot9_i16):
        a_metal = np.eye(3, dtype=np.float64)
    else:
        r_ps2 = np.asarray(rot9_i16, dtype=np.float64).reshape(3, 3) / 8192.0
        a_metal = P_PS2_TO_METAL @ r_ps2.T @ P_PS2_TO_METAL.T

    tx, ty, tz = ps2_point_to_metal(pos_ps2[0], pos_ps2[1], pos_ps2[2], origin_ps2)
    return (
        float(a_metal[0, 0]), float(a_metal[1, 0]), float(a_metal[2, 0]),
        float(a_metal[0, 1]), float(a_metal[1, 1]), float(a_metal[2, 1]),
        float(a_metal[0, 2]), float(a_metal[1, 2]), float(a_metal[2, 2]),
        tx, ty, tz,
    )


def apply_metal_4x3(transform12: Sequence[float], pts: np.ndarray) -> np.ndarray:
    """Apply column-major 4x3 Metal transform to (N, 3) array of Metal Y-up points."""
    col0 = np.array(transform12[0:3], dtype=np.float64)
    col1 = np.array(transform12[3:6], dtype=np.float64)
    col2 = np.array(transform12[6:9], dtype=np.float64)
    trans = np.array(transform12[9:12], dtype=np.float64)
    # Row-vector form: pts @ A.T + trans where rows of A.T are col0, col1, col2
    mat_t = np.stack([col0, col1, col2], axis=0)
    return pts @ mat_t + trans


# ---------------------------------------------------------------------------
# L2RA.BUN & STREAML2RA.BUN Binary Parsers
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class StreamSectionEntry:
    name: str
    section_number: int
    file_offset: int
    size: int
    master_stream_chunk: int
    center_x: float
    center_y: float
    radius: float
    checksum: int


@dataclasses.dataclass
class SceneryInfoEntry:
    debug_name: str
    solid_keys: Tuple[int, int, int]
    radius: float
    hierarchy_hash: int


@dataclasses.dataclass
class SceneryInstanceEntry:
    bbox_min_ps2: Tuple[float, float, float]
    bbox_max_ps2: Tuple[float, float, float]
    exclude_flags: int
    flags: int
    flags_u32: int
    preculler_idx: int
    light_ctx: int
    pos_ps2: Tuple[float, float, float]
    rot9_i16: Tuple[int, ...]
    scenery_info_idx: int


def parse_l2ra_section_table(l2ra_bytes: bytes) -> Dict[str, StreamSectionEntry]:
    """Parse the 0x00034110 streaming section table (stride 0x5C = 92 B) from TRACKS\\L2RA.BUN."""
    sections: Dict[str, StreamSectionEntry] = {}
    pos = 0
    end = len(l2ra_bytes)
    while pos + 8 <= end:
        cid, csz = struct.unpack_from("<II", l2ra_bytes, pos)
        pay_off = pos + 8
        pay_end = min(end, pay_off + csz)
        if cid == CHUNK_STREAM_SECTIONS:
            start = gi._skip_11_word_padding(l2ra_bytes, pay_off, pay_end)
            for off in range(start, pay_end - SECTION_ENTRY_STRIDE + 1, SECTION_ENTRY_STRIDE):
                raw_name = l2ra_bytes[off : off + 8].split(b"\x00", 1)[0]
                name = raw_name.decode("ascii", errors="replace").strip()
                if not name:
                    continue
                (
                    sec_num,
                    _z0,
                    _o1,
                    file_off,
                    size,
                    _csz,
                    _psz,
                    msc,
                    cx,
                    cy,
                    rad,
                    chk,
                ) = struct.unpack_from("<8I3fI", l2ra_bytes, off + 8)
                sections[name] = StreamSectionEntry(
                    name=name,
                    section_number=sec_num,
                    file_offset=file_off,
                    size=size,
                    master_stream_chunk=msc,
                    center_x=cx,
                    center_y=cy,
                    radius=rad,
                    checksum=chk,
                )
        pos = pay_off + csz
    return sections


def parse_scenery_groups(section_buf: bytes) -> Tuple[List[SceneryInfoEntry], List[SceneryInstanceEntry]]:
    """Parse all 0x80034100 SceneryGroup chunks inside a STREAML2RA section slice."""
    all_infos: List[SceneryInfoEntry] = []
    all_insts: List[SceneryInstanceEntry] = []
    pos = 0
    end = len(section_buf)
    while pos + 8 <= end:
        cid, csz = struct.unpack_from("<II", section_buf, pos)
        pay_off = pos + 8
        pay_end = min(end, pay_off + csz)
        if cid == CHUNK_SCENERY_GROUP:
            base_info_idx = len(all_infos)
            sp = pay_off
            while sp + 8 <= pay_end:
                scid, scsz = struct.unpack_from("<II", section_buf, sp)
                spo = gi._skip_11_word_padding(section_buf, sp + 8, min(pay_end, sp + 8 + scsz))
                spe = min(pay_end, sp + 8 + scsz)
                if scid == CHUNK_SCENERY_INFO:
                    for q in range(spo, spe - SCENERY_INFO_STRIDE + 1, SCENERY_INFO_STRIDE):
                        dname = section_buf[q : q + 24].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
                        skeys = struct.unpack_from("<3I", section_buf, q + 0x1C)
                        rad = struct.unpack_from("<f", section_buf, q + 0x38)[0]
                        hhash = struct.unpack_from("<I", section_buf, q + 0x3C)[0]
                        all_infos.append(SceneryInfoEntry(dname, skeys, rad, hhash))
                elif scid == CHUNK_SCENERY_INSTANCE:
                    for q in range(spo, spe - SCENERY_INST_STRIDE + 1, SCENERY_INST_STRIDE):
                        bmin = struct.unpack_from("<3f", section_buf, q + 0x00)
                        bmax = struct.unpack_from("<3f", section_buf, q + 0x0C)
                        flags_u32 = struct.unpack_from("<I", section_buf, q + 0x18)[0]
                        excl = section_buf[q + 0x18]
                        fl = section_buf[q + 0x19]
                        prec, lctx = struct.unpack_from("<hh", section_buf, q + 0x1C)
                        ppos = struct.unpack_from("<3f", section_buf, q + 0x20)
                        rot9 = struct.unpack_from("<9h", section_buf, q + 0x2C)
                        sidx = struct.unpack_from("<h", section_buf, q + 0x3E)[0]
                        if sidx >= 0:
                            all_insts.append(
                                SceneryInstanceEntry(
                                    bbox_min_ps2=bmin,
                                    bbox_max_ps2=bmax,
                                    exclude_flags=excl,
                                    flags=fl,
                                    flags_u32=flags_u32,
                                    preculler_idx=prec,
                                    light_ctx=lctx,
                                    pos_ps2=ppos,
                                    rot9_i16=rot9,
                                    scenery_info_idx=base_info_idx + sidx,
                                )
                            )
                sp += 8 + scsz
        pos = pay_off + csz
    return all_infos, all_insts


def _unpack_vif_strip_ao(buf: bytes, num_verts: int) -> List[int]:
    """Extract per-vertex baked AO (0..255) from world VIF 0x2D..0x4C unsigned uint8 RGBA channel."""
    p = 0
    vc_bytes = bytearray()
    while p + 4 <= len(buf):
        tag = struct.unpack_from("<I", buf, p)[0]
        p += 4
        cmd = (tag >> 24) & 0xFF
        num = (tag >> 16) & 0xFF
        imm = tag & 0xFFFF
        if cmd in (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x13, 0x14, 0x15, 0x17):
            continue
        if cmd == 0x20:
            p += 4
            continue
        if cmd in (0x30, 0x31):
            p += 16
            continue
        if cmd in (0x50, 0x51):
            p += imm * 16
            continue
        if (cmd & 0x60) == 0x60:
            vn = (cmd >> 2) & 3
            vl = cmd & 3
            addr = imm & 0x3FF
            cnt = 256 if num == 0 else num
            elem_bits = (32, 16, 8, 16)[vl]
            total_bits = (16 if vl == 3 else (vn + 1) * elem_bits) * cnt
            blen = ((total_bits + 31) // 32) * 4
            if p + blen > len(buf):
                break
            if 0x2D <= addr < 0x4D and vl == 2 and vn == 3:
                vc_bytes.extend(buf[p : p + blen])
            p += blen
        else:
            break

    if len(vc_bytes) < 4:
        return [255] * num_verts

    ao_out: List[int] = []
    n_avail = len(vc_bytes) // 4
    for i in range(num_verts):
        if i < n_avail:
            r = vc_bytes[i * 4]
            g = vc_bytes[i * 4 + 1]
            b = vc_bytes[i * 4 + 2]
            # PS2 GS vertex lighting modulates by vc / 128 (~180..200 = full sunlit brightness)
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            ao = int(min(255, max(24, round(luma * (255.0 / 192.0)))))
            ao_out.append(ao)
        else:
            ao_out.append(255)
    return ao_out


@dataclasses.dataclass
class DecodedWorldSolid:
    name: str
    name_hash: int
    texture_hashes: List[int]
    positions_metal: np.ndarray      # (V, 3) float32
    normals_metal: np.ndarray        # (V, 3) float32
    uvs_metal: np.ndarray            # (V, 2) float32 (row 0 = top)
    vertex_ao: np.ndarray            # (V,) uint8
    vertex_tex_slot: np.ndarray      # (V,) int32 (index into texture_hashes, -1 if untextured)
    indices: np.ndarray              # (I,) uint32
    aabb_min_metal: Tuple[float, float, float]
    aabb_max_metal: Tuple[float, float, float]


def decode_world_solid_chunk(data: bytes, chunk_off: int, chunk_size: int) -> Optional[DecodedWorldSolid]:
    """Decode one world 0x80134010 SolidPack directly into Metal Y-up space (never applying m44)."""
    p = chunk_off + 8
    end = min(len(data), p + chunk_size)

    name = ""
    name_hash = 0
    tex_hashes: List[int] = []
    strips: List[gi.StripEntry] = []
    vif_start = 0
    vif_end = 0

    while p + 8 <= end:
        cid, csz = struct.unpack_from("<II", data, p)
        pay_off = p + 8
        pay_end = min(end, pay_off + csz)
        if cid == gi.CHUNK_ESOLID_HDR:
            hdr_off = gi._skip_11_word_padding(data, pay_off, pay_end)
            if hdr_off + 0xA0 <= pay_end:
                name_hash = struct.unpack_from("<I", data, hdr_off + 0x10)[0]
                raw_name = data[hdr_off + 0xA0 : pay_end].split(b"\x00", 1)[0]
                name = raw_name.decode("ascii", errors="replace").strip()
        elif cid == gi.CHUNK_TEX_TABLE:
            to = gi._skip_11_word_padding(data, pay_off, pay_end)
            for q in range(to, pay_end - 7, 8):
                tex_hashes.append(struct.unpack_from("<I", data, q)[0])
        elif cid == gi.CHUNK_MESH_CONT:
            mp = pay_off
            while mp + 8 <= pay_end:
                mcid, mcsz = struct.unpack_from("<II", data, mp)
                mp_off = mp + 8
                mp_end = min(pay_end, mp_off + mcsz)
                if mcid == gi.CHUNK_STRIP_TABLE:
                    so = gi._skip_11_padding(data, mp_off, mp_end)
                    idx = 0
                    while so + 16 <= mp_end:
                        fl, nv, np_, md, qoff, nz, nt, qsz, pg, tx, lm = struct.unpack_from(
                            "<HBBfHbbBBbb", data, so
                        )
                        strips.append(
                            gi.StripEntry(idx, fl, nv, np_, md, qoff, nz, nt, qsz, pg, tx, lm)
                        )
                        idx += 1
                        so += 16
                elif mcid == gi.CHUNK_VIF_DATA:
                    vif_start = gi._skip_11_padding(data, mp_off, mp_end)
                    vif_end = mp_end
                mp = mp_off + mcsz
        p = pay_off + csz

    if not strips or vif_start >= vif_end or name_hash == 0:
        return None

    all_v_ps2: List[Tuple[float, float, float]] = []
    all_vn_ps2: List[Tuple[float, float, float]] = []
    all_uv_metal: List[Tuple[float, float]] = []
    all_ao: List[int] = []
    all_tex_slot: List[int] = []
    all_indices: List[int] = []

    for s in strips:
        s_off = vif_start + s.data_qword_offset * 16
        s_end = min(vif_end, s_off + s.data_qword_size * 16)
        if s_off >= vif_end:
            continue
        sbuf = data[s_off:s_end]
        vs, vns, vts_obj, adcs = gi._unpack_vif_strip(sbuf, s)
        if not vs:
            continue
        aos = _unpack_vif_strip_ao(sbuf, len(vs))
        base_v = len(all_v_ps2)
        tex_slot = s.texture_index if 0 <= s.texture_index < len(tex_hashes) else -1

        all_v_ps2.extend(vs)
        all_vn_ps2.extend(vns)
        # geometry_inspect returns OpenGL bottom-up (u, 1.0 - v_ps2); invert back to Metal row-0-top (u, v_ps2)
        all_uv_metal.extend((u, 1.0 - v_obj) for (u, v_obj) in vts_obj)
        all_ao.extend(aos)
        all_tex_slot.extend([tex_slot] * len(vs))

        parity = 0
        for i in range(2, len(vs)):
            if adcs[i]:
                parity = 0
                continue
            if parity == 0:
                i0, i1, i2 = base_v + i - 2, base_v + i - 1, base_v + i
            else:
                i0, i1, i2 = base_v + i - 1, base_v + i - 2, base_v + i
            parity ^= 1
            if i0 != i1 and i1 != i2 and i0 != i2:
                all_indices.extend((i0, i1, i2))

    if not all_indices or not all_v_ps2:
        return None

    v_ps2 = np.asarray(all_v_ps2, dtype=np.float64)
    # P(x, y, z) = (x, z, -y)
    pos_metal = np.empty_like(v_ps2)
    pos_metal[:, 0] = v_ps2[:, 0]
    pos_metal[:, 1] = v_ps2[:, 2]
    pos_metal[:, 2] = -v_ps2[:, 1]

    idx_arr = np.asarray(all_indices, dtype=np.uint32)
    tris = idx_arr.reshape(-1, 3)

    # Filter geometrically degenerate triangles (zero area)
    p0 = pos_metal[tris[:, 0]]
    p1 = pos_metal[tris[:, 1]]
    p2 = pos_metal[tris[:, 2]]
    fn = np.cross(p1 - p0, p2 - p0)
    fn_mag = np.linalg.norm(fn, axis=1)
    valid_tri = fn_mag > 1e-10
    if not np.any(valid_tri):
        return None
    tris = tris[valid_tri]
    fn = fn[valid_tri]
    idx_arr = tris.reshape(-1).astype(np.uint32)

    # Compute area-weighted smooth vertex normals in Metal Y-up space
    accum = np.zeros_like(pos_metal)
    np.add.at(accum, tris[:, 0], fn)
    np.add.at(accum, tris[:, 1], fn)
    np.add.at(accum, tris[:, 2], fn)
    nmag = np.linalg.norm(accum, axis=1, keepdims=True)

    # Fallback to rotated PS2 hardware normal or (0, 1, 0) for any isolated/zero-normal vertex
    vn_ps2 = np.asarray(all_vn_ps2, dtype=np.float64)
    vn_metal = np.empty_like(vn_ps2)
    vn_metal[:, 0] = vn_ps2[:, 0]
    vn_metal[:, 1] = vn_ps2[:, 2]
    vn_metal[:, 2] = -vn_ps2[:, 1]
    vn_mag = np.linalg.norm(vn_metal, axis=1, keepdims=True)
    vn_fallback = np.where(vn_mag > 1e-6, vn_metal / np.maximum(vn_mag, 1e-6), np.array([0.0, 1.0, 0.0]))

    norms_metal = np.where(nmag > 1e-8, accum / np.maximum(nmag, 1e-8), vn_fallback)

    bmin = pos_metal.min(axis=0)
    bmax = pos_metal.max(axis=0)
    return DecodedWorldSolid(
        name=name or f"SOLID_0x{name_hash:08x}",
        name_hash=name_hash,
        texture_hashes=tex_hashes,
        positions_metal=pos_metal.astype(np.float32),
        normals_metal=norms_metal.astype(np.float32),
        uvs_metal=np.asarray(all_uv_metal, dtype=np.float32),
        vertex_ao=np.asarray(all_ao, dtype=np.uint8),
        vertex_tex_slot=np.asarray(all_tex_slot, dtype=np.int32),
        indices=idx_arr,
        aabb_min_metal=(float(bmin[0]), float(bmin[1]), float(bmin[2])),
        aabb_max_metal=(float(bmax[0]), float(bmax[1]), float(bmax[2])),
    )


def index_solids_in_section(
    section_buf: bytes,
    out_map: Dict[int, Tuple[bytes, int, int, str]],
    overwrite: bool = False,
) -> int:
    """Scan a section buffer for all 0x80134010 SolidPacks and index (buf, off, sz, name) by name_hash."""
    count = 0
    pos = 0
    end = len(section_buf)
    while pos + 8 <= end:
        cid, csz = struct.unpack_from("<II", section_buf, pos)
        pay_off = pos + 8
        pay_end = min(end, pay_off + csz)
        if cid == gi.CHUNK_GEOM_PACK:
            sp = pay_off
            while sp + 8 <= pay_end:
                scid, scsz = struct.unpack_from("<II", section_buf, sp)
                if scid == gi.CHUNK_SOLID_PACK and sp + 8 + scsz <= pay_end:
                    # Peek 0x00134011 header for name_hash & name
                    ep = sp + 8
                    eend = ep + scsz
                    while ep + 8 <= eend:
                        ecid, ecsz = struct.unpack_from("<II", section_buf, ep)
                        if ecid == gi.CHUNK_ESOLID_HDR:
                            hdr_off = gi._skip_11_word_padding(section_buf, ep + 8, min(eend, ep + 8 + ecsz))
                            if hdr_off + 0xA0 <= min(eend, ep + 8 + ecsz):
                                nh = struct.unpack_from("<I", section_buf, hdr_off + 0x10)[0]
                                nm = (
                                    section_buf[hdr_off + 0xA0 : min(eend, ep + 8 + ecsz)]
                                    .split(b"\x00", 1)[0]
                                    .decode("ascii", errors="replace")
                                    .strip()
                                )
                                if nh and (overwrite or nh not in out_map):
                                    out_map[nh] = (section_buf, sp, scsz, nm)
                                    count += 1
                            break
                        ep += 8 + ecsz
                sp += 8 + scsz
        pos = pay_off + csz
    return count


# ---------------------------------------------------------------------------
# Texture Index & Selective PS2 TPK Decoder
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DecodedRkptTexture:
    name: str
    tpk_name_hash: int
    width: int
    height: int
    rgba_bytes: bytes
    flags: int
    source: str


def normalize_ps2_alpha_rgba(rgba: np.ndarray, psm: int) -> Tuple[np.ndarray, bool]:
    """Apply exact contract PS2 [0..0x80] alpha normalization: min(255, round(a * 255 / 128))."""
    out = rgba.copy()
    if psm in (te.PS2_PSMCT16, te.PS2_PSMCT16S, te.PS2_PSMCT24):
        return out, False
    a = out[:, :, 3].astype(np.float32)
    out[:, :, 3] = np.minimum(255.0, np.rint(a * (255.0 / 128.0))).astype(np.uint8)
    return out, True


def _is_foliage_texture_name(tex_name: str) -> bool:
    tn = tex_name.upper().replace("STREET", "")
    if "TRUNK" in tn or "ROCK" in tn:
        return False
    return bool(FOLIAGE_TEX_RE.search(tn))


def enhance_world_texture_2x(
    rgba: np.ndarray,
    has_cutout_alpha: bool,
    max_source_dim: int = 256,
    tex_name: str = "",
    is_foliage: Optional[bool] = None,
    max_output_dim: int = 512,
) -> Tuple[np.ndarray, bool]:
    """Detail-preserving 2x/4x world texture upscaler (capped at 512px max edge).

    - Small textures (<= 64px) and hero close-up signs/foliage/brick/storefronts/awnings
      (<= 128px) upscale 4x (and <= 32px SGN_* street signs up to 256px) capped at 512px.
    - Remaining <= 256px textures upscale 2x capped at 512px.
    - Foliage textures (cypress, fall trees, hedges) receive leaf-crevice micro-contrast
      expansion + crisp cutout alpha edge recovery so conifers/canopies never read as clay cones.
    """
    h, w = rgba.shape[0], rgba.shape[1]
    max_dim = max(w, h)
    if max_dim > max_source_dim or w <= 0 or h <= 0:
        return rgba, False

    tn = tex_name.upper()
    foliage_mode = _is_foliage_texture_name(tn) if is_foliage is None else is_foliage
    is_sign = tn.startswith(("SGN_", "ADS_")) or "SIGN" in tn or "DONOTENTER" in tn

    if is_sign and max_dim <= 32:
        scale = min(8, max_output_dim // max_dim)
    elif max_dim <= 32 or (max_dim <= 128 and (foliage_mode or bool(HERO_4X_TEX_RE.search(tn)))):
        scale = min(4, max_output_dim // max_dim)
    else:
        scale = min(2, max_output_dim // max_dim)

    if scale < 2:
        return rgba, False

    new_w = w * scale
    new_h = h * scale
    new_size = (new_w, new_h)

    rgb_img = Image.fromarray(rgba[:, :, :3])
    rgb_up = rgb_img.resize(new_size, resample=_RESAMPLE_LANCZOS)

    if foliage_mode:
        # Boost pine-needle / leaf-cluster micro-contrast around opaque pixels + strong crisp unsharp
        rgb_sharp = rgb_up.filter(ImageFilter.UnsharpMask(radius=1.4, percent=155, threshold=2))
        rgb_f = np.asarray(rgb_sharp, dtype=np.float32)
        alpha_src_f = rgba[:, :, 3]
        if int(alpha_src_f.max()) > 128 and np.any(alpha_src_f >= 128):
            opaque_mean = rgba[:, :, :3][alpha_src_f >= 128].mean(axis=0).astype(np.float32)
        else:
            opaque_mean = rgb_f.mean(axis=(0, 1))
        # Expand contrast around leaf mean so dark shadowed needle crevices pop against lit tips
        rgb_f = (rgb_f - opaque_mean) * 1.32 + opaque_mean
        rgb_arr = np.clip(np.rint(rgb_f), 0.0, 255.0).astype(np.uint8)
    elif is_sign:
        # Clean edge-preserving sharpening for crisp lettering without halo ringing
        rgb_sharp = rgb_up.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=4))
        rgb_arr = np.asarray(rgb_sharp, dtype=np.uint8)
    else:
        # Architectural brick/masonry/asphalt detail sharpening
        percent = 105 if scale >= 4 else 85
        rgb_sharp = rgb_up.filter(ImageFilter.UnsharpMask(radius=1.0, percent=percent, threshold=3))
        rgb_arr = np.asarray(rgb_sharp, dtype=np.uint8)

    alpha_src = rgba[:, :, 3]
    if int(alpha_src.min()) >= 254:
        alpha_arr = np.full((new_h, new_w), 255, dtype=np.uint8)
    else:
        a_img = Image.fromarray(alpha_src)
        a_up = np.asarray(a_img.resize(new_size, resample=_RESAMPLE_BILINEAR), dtype=np.float32)
        if has_cutout_alpha:
            # Stronger silhouette edge sharpening on foliage conifers/canopies vs fences
            steep = 2.2 if foliage_mode else 1.45
            a_up = np.clip((a_up - 128.0) * steep + 128.0, 0.0, 255.0)
        alpha_arr = np.rint(a_up).astype(np.uint8)

    out = np.empty((new_h, new_w, 4), dtype=np.uint8)
    out[:, :, :3] = rgb_arr
    out[:, :, 3] = alpha_arr
    return out, True


def decode_tpk_textures_matching(
    data: bytes,
    source: str,
    wanted_hashes: Set[int],
    resolved: Dict[int, DecodedRkptTexture],
    enhance_textures: bool = True,
) -> int:
    """Decode only TPK textures whose name_hash is in wanted_hashes and not yet in resolved."""
    if not wanted_hashes:
        return 0
    roots: List[Tuple[int, int]] = []
    te._find_tpk_roots(data, 0, len(data), roots)
    if not roots:
        return 0

    added = 0
    for root_pos, root_sz in roots:
        root_start = root_pos + 8
        root_end = root_start + root_sz

        tex_infos_raw: List[bytes] = []
        plat_infos_raw: List[bytes] = []
        stream_entries: List[Tuple[int, int, int, int]] = []
        sp_start: Optional[int] = None
        sp_size = 0

        for cid, pos, csz in te._iter_linear_chunks(data, root_start, root_end):
            c_start, c_end = pos + 8, pos + 8 + csz
            if cid == tpki.CHUNK_TPK_INFO:
                for scid, spos, scsz in te._iter_linear_chunks(data, c_start, c_end):
                    s_start = spos + 8
                    if scid == tpki.CHUNK_TPK_STREAM_TABLE:
                        for i in range(scsz // tpki.STREAM_ENTRY_SIZE):
                            hsh, off, csz_e, ucsz_e = struct.unpack_from(
                                "<IIII", data, s_start + i * tpki.STREAM_ENTRY_SIZE
                            )
                            stream_entries.append((hsh, off, csz_e, ucsz_e))
                    elif scid == tpki.CHUNK_TPK_TEX_INFO:
                        for i in range(scsz // tpki.TEXTURE_INFO_SIZE):
                            off_i = s_start + i * tpki.TEXTURE_INFO_SIZE
                            tex_infos_raw.append(data[off_i : off_i + tpki.TEXTURE_INFO_SIZE])
                    elif scid == tpki.CHUNK_TPK_TEX_PLAT:
                        for i in range(scsz // tpki.TEXTURE_PLAT_SIZE):
                            off_i = s_start + i * tpki.TEXTURE_PLAT_SIZE
                            plat_infos_raw.append(data[off_i : off_i + tpki.TEXTURE_PLAT_SIZE])
            elif cid == tpki.CHUNK_TPK_DATA:
                for dcid, dpos, dcsz in te._iter_linear_chunks(data, c_start, c_end):
                    if dcid == tpki.CHUNK_TPK_DATA_PAYLOAD:
                        sp_start = dpos + 8
                        sp_size = dcsz

        if stream_entries:
            for hsh, off, csz_e, _ucsz in stream_entries:
                if hsh not in wanted_hashes or hsh in resolved:
                    continue
                abs_off = root_pos + off if (root_pos + off + csz_e <= len(data) and off < root_pos) else off
                if abs_off + csz_e > len(data):
                    abs_off = root_pos + off
                if abs_off + csz_e > len(data):
                    continue
                try:
                    dec, _algo = tpki.decompress_ea_block(data[abs_off : abs_off + csz_e])
                except Exception:
                    continue
                tail_needed = tpki.TEXTURE_INFO_SIZE + tpki.TEXTURE_PLAT_SIZE
                if len(dec) < tail_needed:
                    continue
                meta = te._decode_texture_pair(dec[-tail_needed : -tpki.TEXTURE_PLAT_SIZE], dec[-tpki.TEXTURE_PLAT_SIZE :])
                if meta["name_hash"] == 0:
                    meta["name_hash"] = hsh
                img_sz = meta["image_size"]
                pal_sz = meta["palette_size"]
                try:
                    img = te.decode_ps2_texture(
                        dec[:img_sz],
                        dec[img_sz : img_sz + pal_sz] if pal_sz > 0 else b"",
                        width=meta["width"],
                        height=meta["height"],
                        psm=meta["psm"],
                        clut_psm=meta["clut_psm"],
                        num_palette_entries=meta["num_palette_entries"],
                        is_swizzled=meta["is_swizzled"],
                        scale_alpha=False,
                    )
                except Exception:
                    continue
                rgba, norm_0x80 = normalize_ps2_alpha_rgba(np.asarray(img.convert("RGBA"), dtype=np.uint8), meta["psm"])
                has_cutout = bool(np.mean(rgba[:, :, 3] < 128) >= 0.005)
                enhanced = False
                if enhance_textures:
                    rgba, enhanced = enhance_world_texture_2x(
                        rgba,
                        has_cutout_alpha=has_cutout,
                        tex_name=meta["name"],
                    )
                flags = (
                    (TEX_FLAG_HAS_CUTOUT_ALPHA if has_cutout else 0)
                    | (TEX_FLAG_PS2_0X80_NORMALIZED if norm_0x80 else 0)
                    | (TEX_FLAG_ENHANCED_2X if enhanced else 0)
                )
                resolved[hsh] = DecodedRkptTexture(
                    name=meta["name"],
                    tpk_name_hash=hsh,
                    width=int(rgba.shape[1]),
                    height=int(rgba.shape[0]),
                    rgba_bytes=rgba.tobytes(),
                    flags=flags,
                    source=source,
                )
                added += 1
            continue

        if sp_start is None or not tex_infos_raw:
            continue

        parsed_metas: List[Dict[str, Any]] = []
        max_end = 0
        any_wanted = False
        for i, ti_b in enumerate(tex_infos_raw):
            pi_b = plat_infos_raw[i] if i < len(plat_infos_raw) else b"\x00" * tpki.TEXTURE_PLAT_SIZE
            meta = te._decode_texture_pair(ti_b, pi_b)
            parsed_metas.append(meta)
            max_end = max(
                max_end,
                meta["image_offset"] + meta["image_size"],
                (meta["palette_offset"] + meta["palette_size"]) if meta["palette_size"] > 0 else 0,
            )
            if meta["name_hash"] in wanted_hashes and meta["name_hash"] not in resolved:
                any_wanted = True

        if not any_wanted:
            continue

        rel_sp = sp_start - root_pos
        pad_128 = ((rel_sp + 127) & ~127) - rel_sp
        if (
            pad_128 > 0
            and sp_size >= max_end + pad_128
            and data[sp_start : sp_start + pad_128] == b"\x11" * pad_128
        ):
            payload_base = sp_start + pad_128
        else:
            payload_base = sp_start
        payload_end = sp_start + sp_size

        for meta in parsed_metas:
            hsh = meta["name_hash"]
            if hsh not in wanted_hashes or hsh in resolved:
                continue
            ioff = payload_base + meta["image_offset"]
            poff = payload_base + meta["palette_offset"]
            if ioff + meta["image_size"] > payload_end or meta["width"] <= 0 or meta["height"] <= 0:
                continue
            img_bytes = data[ioff : ioff + meta["image_size"]]
            pal_bytes = (
                data[poff : poff + meta["palette_size"]]
                if meta["palette_size"] > 0 and poff + meta["palette_size"] <= payload_end
                else b""
            )
            try:
                img = te.decode_ps2_texture(
                    img_bytes,
                    pal_bytes,
                    width=meta["width"],
                    height=meta["height"],
                    psm=meta["psm"],
                    clut_psm=meta["clut_psm"],
                    num_palette_entries=meta["num_palette_entries"],
                    is_swizzled=meta["is_swizzled"],
                    scale_alpha=False,
                )
            except Exception:
                continue
            rgba, norm_0x80 = normalize_ps2_alpha_rgba(np.asarray(img.convert("RGBA"), dtype=np.uint8), meta["psm"])
            has_cutout = bool(np.mean(rgba[:, :, 3] < 128) >= 0.005)
            enhanced = False
            if enhance_textures:
                rgba, enhanced = enhance_world_texture_2x(
                    rgba,
                    has_cutout_alpha=has_cutout,
                    tex_name=meta["name"],
                )
            flags = (
                (TEX_FLAG_HAS_CUTOUT_ALPHA if has_cutout else 0)
                | (TEX_FLAG_PS2_0X80_NORMALIZED if norm_0x80 else 0)
                | (TEX_FLAG_ENHANCED_2X if enhanced else 0)
            )
            resolved[hsh] = DecodedRkptTexture(
                name=meta["name"],
                tpk_name_hash=hsh,
                width=int(rgba.shape[1]),
                height=int(rgba.shape[0]),
                rgba_bytes=rgba.tobytes(),
                flags=flags,
                source=source,
            )
            added += 1

    return added


# ---------------------------------------------------------------------------
# Semantic Surface Classification & Material Hints
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RkptMaterialSpec:
    diffuse_tex_index: int
    surface_class: int
    flags: int
    roughness_hint: float
    metallic_hint: float
    emissive_hint: float
    bump_strength: float
    tpk_name_hash: int
    debug_name: str


def classify_world_material(
    tex_name: str,
    solid_name: str,
    tex_has_cutout_alpha: bool,
    diffuse_tex_index: int,
    tpk_name_hash: int,
) -> RkptMaterialSpec:
    """Classify a Rockport world texture/solid pair into the locked .rkpt surfaceClass & PBR hints."""
    tn = tex_name.upper()
    sn = solid_name.upper()

    is_open_cutout = any(
        k in tn
        for k in (
            "FENCE", "GRATE", "FIREESCAPE", "CHAINLINK", "WIRE", "LATTICE",
            "SCAFFOLD", "NET", "GUARDRAIL", "RAIL", "RADIOTOWER", "TOWER",
            "IRON", "BARS", "GATE", "TREELINE", "TREE", "BUSH", "HEDGE",
            "LEAF", "LEAVES", "BRANCH", "VINE", "SHRUB", "GRASS_A", "FOLI",
            "DECAL", "CLOTH",
        )
    )

    # 1. Drivable Road Asphalt (EA uses PAVEMENT* for almost all drivable streets!)
    if any(k in tn for k in ("PAVEMENT", "ASPHALT", "ROAD", "HWY", "HIGHWAY", "INTERSECT", "CROSSWALK", "DRIVEWAY", "PARKINGLOT")):
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_ROAD_ASPHALT,
            flags=MAT_FLAG_DRIVABLE_ROAD | MAT_FLAG_WET_REFLECTIVE,
            roughness_hint=0.38,
            metallic_hint=0.0,
            emissive_hint=0.0,
            bump_strength=0.45,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # 2. Water
    if any(k in tn for k in ("WATER", "RIVER", "POND", "FOUNTAIN", "LAKE", "POOL", "CANAL", "OCEAN")) and "WATERTOWER" not in tn:
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_WATER,
            flags=MAT_FLAG_WET_REFLECTIVE,
            roughness_hint=0.05,
            metallic_hint=0.0,
            emissive_hint=0.0,
            bump_strength=0.15,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # 3. Foliage Cutout (trees, cypress, hedges, bushes, vines; avoids matching STREETLIGHT)
    if _is_foliage_texture_name(tn):
        fl = MAT_FLAG_DOUBLE_SIDED | (MAT_FLAG_ALPHA_CUTOUT if tex_has_cutout_alpha else 0)
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_FOLIAGE_CUTOUT,
            flags=fl,
            roughness_hint=0.82,
            metallic_hint=0.0,
            emissive_hint=0.0,
            bump_strength=1.35,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # 4. Sidewalks, curbs & plazas
    if any(k in tn for k in ("SIDEWALK", "CURB", "PLAZA", "WALKWAY", "COBBLE", "OLDCONCRETE", "PAINTEDCONCRETE")):
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_SIDEWALK,
            flags=0,
            roughness_hint=0.72,
            metallic_hint=0.0,
            emissive_hint=0.0,
            bump_strength=0.35,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # 5. Building Glass / Storefronts (catches WINDOW, DOORWIN, WIN1/2, _WIN, _WINA/B, GLASS; excludes SGN_ signs)
    if not tn.startswith(("SGN_", "ADS_")) and BUILDING_GLASS_RE.search(tn):
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_BUILDING_GLASS,
            flags=MAT_FLAG_WET_REFLECTIVE,
            roughness_hint=0.08,
            metallic_hint=0.12,
            emissive_hint=0.16,
            bump_strength=0.05,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # 6. Emissive Signs, Lamps, Streetlights, Billboards, Neon
    if (
        tn.startswith(("SGN_", "ADS_"))
        or any(
            k in tn
            for k in (
                "NEON", "SIGN", "BILLBOARD", "TRAFFICLIGHT", "STOPLIGHT",
                "STREETLIGHT", "LAMP", "MARQUEE", "GLOW", "LIGHT_",
            )
        )
    ) and "BACKSIGN" not in tn and "BACK_" not in tn and "LIGHTBACK" not in tn:
        is_bright = any(k in tn for k in ("NEON", "LIGHT_", "LAMP", "GLOW", "TRAFFICLIGHT", "STOPLIGHT"))
        fl = MAT_FLAG_EMISSIVE | (MAT_FLAG_ALPHA_CUTOUT | MAT_FLAG_DOUBLE_SIDED if tex_has_cutout_alpha and is_open_cutout else 0)
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_EMISSIVE_SIGN_LAMP,
            flags=fl,
            roughness_hint=0.25,
            metallic_hint=0.05,
            emissive_hint=2.4 if is_bright else 0.75,
            bump_strength=0.10,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # 7. Metal Bridges, Steel, Fences, Poles, Towers, Fire Escapes
    if any(
        k in tn
        for k in (
            "BRIDGE", "STEEL", "IRON", "METAL", "GIRDER", "TRUSS", "FENCE",
            "GUARDRAIL", "POLE", "SCAFFOLD", "FIREESCAPE", "RADIOTOWER",
            "WATERTOWER", "CHAINLINK", "GRATE", "GATE", "RAIL", "PIPE",
            "HYDRANT", "PARKINGMETER", "SPIKEBELT", "BACKSIGN", "BACK_",
            "LIGHTBACK", "GARAGEDOOR",
        )
    ):
        fl = (MAT_FLAG_ALPHA_CUTOUT | MAT_FLAG_DOUBLE_SIDED) if (tex_has_cutout_alpha and is_open_cutout) else 0
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_METAL_BRIDGE,
            flags=fl,
            roughness_hint=0.35,
            metallic_hint=0.82,
            emissive_hint=0.0,
            bump_strength=0.25,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # 8. Terrain Grass / Dirt / Gravel / Hills / Fields
    if any(k in tn for k in ("GRASS", "DIRT", "GRAVEL", "MUD", "SOIL", "SAND", "ROCK", "CLIFF", "HILL", "FIELD", "MULCH", "TURF", "LAWN", "GRUNG", "SEWERGRND", "TRUNK")) or tn.startswith("TER_"):
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_TERRAIN_GRASS_DIRT,
            flags=0,
            roughness_hint=0.88,
            metallic_hint=0.0,
            emissive_hint=0.0,
            bump_strength=0.55,
            tpk_name_hash=tpk_name_hash,
            debug_name=tex_name or solid_name,
        )

    # Fallback: check solid name if texture was unresolved
    if not tn and ("ROAD" in sn or "HWY" in sn):
        return RkptMaterialSpec(
            diffuse_tex_index=diffuse_tex_index,
            surface_class=SURFACE_ROAD_ASPHALT,
            flags=MAT_FLAG_DRIVABLE_ROAD | MAT_FLAG_WET_REFLECTIVE,
            roughness_hint=0.38,
            metallic_hint=0.0,
            emissive_hint=0.0,
            bump_strength=0.45,
            tpk_name_hash=tpk_name_hash,
            debug_name=solid_name,
        )

    # 0. Default: Concrete / Masonry / Brick / Building Walls
    fl = (MAT_FLAG_ALPHA_CUTOUT | MAT_FLAG_DOUBLE_SIDED) if (tex_has_cutout_alpha and is_open_cutout) else 0
    return RkptMaterialSpec(
        diffuse_tex_index=diffuse_tex_index,
        surface_class=SURFACE_CONCRETE_WALL,
        flags=fl,
        roughness_hint=0.75,
        metallic_hint=0.0,
        emissive_hint=0.0,
        bump_strength=0.40,
        tpk_name_hash=tpk_name_hash,
        debug_name=tex_name or solid_name,
    )


# ---------------------------------------------------------------------------
# Spawn Finder & Top-Down Layout Verification Map
# ---------------------------------------------------------------------------


def is_excluded_world_solid(solid_name: str, debug_name: str = "") -> Optional[str]:
    """Return exclusion reason if solid is a Free-Roam-invisible event barrier, shadow card, or proxy."""
    up_s = solid_name.upper()
    up_d = debug_name.upper()
    if up_s.startswith(EXCLUDED_SOLID_PREFIXES):
        return "shd_pan_rfl_temp"
    for sub in EXCLUDED_EVENT_SOLID_SUBSTRINGS:
        if sub in up_s or sub in up_d:
            return "event_barrier"
    return None


def find_verified_road_spawn(
    meshes: Sequence[DecodedWorldSolid],
    mesh_vert_mats: Sequence[np.ndarray],
    materials: Sequence[RkptMaterialSpec],
    instances: Sequence[Tuple[int, int, int, Tuple[float, ...]]],
    focus_metal_xz: Tuple[float, float],
    min_clearance_m: float = 6.0,
) -> Tuple[Tuple[float, float, float], float, Dict[str, Any]]:
    """Find a verified unobstructed ROAD_ASPHALT spawnPos (+Y normal, >= 6m clearance) & street yaw.

    Checks bidirectional road continuity (long forward sightline + clean rear chase-camera corridor
    with zero median poles or barrier props behind the rear bumper) and rewards scenic street-edge
    framing by real Rockport storefronts and trees.
    """
    cell_sz = 2.0
    road_sum_y: Dict[Tuple[int, int], float] = collections.defaultdict(float)
    road_cnt: Dict[Tuple[int, int], int] = collections.defaultdict(int)
    obstacle_pts: List[Tuple[float, float, float, float]] = []  # (x, z, ymin, ymax)
    scenic_cells: Set[Tuple[int, int]] = set()

    mat_class = np.array([m.surface_class for m in materials], dtype=np.int32)

    for mesh_idx, _sec_idx, _ifl, xform in instances:
        m = meshes[mesh_idx]
        w_pts = apply_metal_4x3(xform, m.positions_metal.astype(np.float64))
        tris = m.indices.reshape(-1, 3)
        t_mats = mesh_vert_mats[mesh_idx][tris[:, 0]]
        t_cls = mat_class[t_mats]

        # Collect flat upward-facing ROAD_ASPHALT triangles
        road_mask = t_cls == SURFACE_ROAD_ASPHALT
        if np.any(road_mask):
            r_tris = tris[road_mask]
            p0 = w_pts[r_tris[:, 0]]
            p1 = w_pts[r_tris[:, 1]]
            p2 = w_pts[r_tris[:, 2]]
            fn = np.cross(p1 - p0, p2 - p0)
            fmag = np.linalg.norm(fn, axis=1)
            up_mask = (fmag > 1e-4) & ((fn[:, 1] / np.maximum(fmag, 1e-6)) >= 0.92)
            if np.any(up_mask):
                p0u, p1u, p2u = p0[up_mask], p1[up_mask], p2[up_mask]
                sample_pts = np.concatenate(
                    [
                        (p0u + p1u + p2u) / 3.0,
                        p0u,
                        p1u,
                        p2u,
                        0.5 * (p0u + p1u),
                        0.5 * (p1u + p2u),
                        0.5 * (p2u + p0u),
                    ],
                    axis=0,
                )
                gx = np.floor(sample_pts[:, 0] / cell_sz).astype(np.int32)
                gz = np.floor(sample_pts[:, 2] / cell_sz).astype(np.int32)
                ys = sample_pts[:, 1]
                for cx, cz, yv in zip(gx.tolist(), gz.tolist(), ys.tolist()):
                    key = (cx, cz)
                    road_sum_y[key] += yv
                    road_cnt[key] += 1

        # Collect scenic storefronts / glass / trees on a coarse 4m grid for framing score
        scenic_mask = (t_cls == SURFACE_CONCRETE_WALL) | (t_cls == SURFACE_BUILDING_GLASS) | (t_cls == SURFACE_FOLIAGE_CUTOUT)
        if np.any(scenic_mask):
            s_tris = tris[scenic_mask]
            sc = (w_pts[s_tris[:, 0]] + w_pts[s_tris[:, 1]] + w_pts[s_tris[:, 2]]) / 3.0
            sx_g = np.floor(sc[:, 0] / 4.0).astype(np.int32)
            sz_g = np.floor(sc[:, 2] / 4.0).astype(np.int32)
            for gx4, gz4 in zip(sx_g.tolist(), sz_g.tolist()):
                scenic_cells.add((gx4, gz4))

        # Collect non-road obstacle points (buildings, walls, props, trees, bridges, poles, signs)
        obs_mask = (
            (t_cls != SURFACE_ROAD_ASPHALT)
            & (t_cls != SURFACE_SIDEWALK)
            & (t_cls != SURFACE_TERRAIN_GRASS_DIRT)
            & (t_cls != SURFACE_WATER)
        )
        if np.any(obs_mask):
            o_tris = tris[obs_mask]
            op0 = w_pts[o_tris[:, 0]]
            op1 = w_pts[o_tris[:, 1]]
            op2 = w_pts[o_tris[:, 2]]
            oc = (op0 + op1 + op2) / 3.0
            oy_min = np.minimum(np.minimum(op0[:, 1], op1[:, 1]), op2[:, 1])
            oy_max = np.maximum(np.maximum(op0[:, 1], op1[:, 1]), op2[:, 1])
            ov = np.concatenate([oc, op0, op1, op2], axis=0)
            ov_ymin = np.concatenate([oy_min, op0[:, 1], op1[:, 1], op2[:, 1]], axis=0)
            ov_ymax = np.concatenate([oy_max, op0[:, 1], op1[:, 1], op2[:, 1]], axis=0)
            for x, z, y0, y1 in zip(ov[:, 0].tolist(), ov[:, 2].tolist(), ov_ymin.tolist(), ov_ymax.tolist()):
                obstacle_pts.append((x, z, y0, y1))

    if not road_cnt:
        return (0.0, 0.0, 0.0), 0.0, {"verified": False, "reason": "no_road_asphalt_found"}

    road_y: Dict[Tuple[int, int], float] = {k: road_sum_y[k] / road_cnt[k] for k in road_cnt}

    # Map obstacles into 2m grid cells storing vertical intervals near ground/car height
    obs_cells: Dict[Tuple[int, int], List[Tuple[float, float]]] = collections.defaultdict(list)
    for ox, oz, y0, y1 in obstacle_pts:
        key = (int(math.floor(ox / cell_sz)), int(math.floor(oz / cell_sz)))
        if any((key[0] + dx, key[1] + dz) in road_y for dx in range(-4, 5, 2) for dz in range(-4, 5, 2)):
            cur = obs_cells[key]
            if len(cur) < 8:
                cur.append((y0, y1))
            else:
                cur[0] = (min(cur[0][0], y0), max(cur[0][1], y1))

    def _is_cell_blocked(cx: int, cz: int, ref_y: float) -> bool:
        intervals = obs_cells.get((cx, cz))
        if not intervals:
            return False
        for y0, y1 in intervals:
            if y1 >= ref_y + 0.30 and y0 <= ref_y + 4.5:
                return True
        return False

    def _ray_road_run(wx: float, wz: float, ry: float, dx: float, dz: float, max_m: int, half_w: float) -> float:
        run_m = 0.0
        cur_y = ry
        for step_m in range(2, max_m + 2, 2):
            tx = int(math.floor((wx + dx * step_m) / cell_sz))
            tz = int(math.floor((wz + dz * step_m) / cell_sz))
            ny = road_y.get((tx, tz))
            if ny is None:
                for ox_t, oz_t in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny = road_y.get((tx + ox_t, tz + oz_t))
                    if ny is not None:
                        break
            if ny is None or abs(ny - cur_y) > 1.8:
                break
            corridor_blocked = False
            for lat in (-half_w, -half_w * 0.5, 0.0, half_w * 0.5, half_w):
                lx = int(math.floor((wx + dx * step_m + dz * lat) / cell_sz))
                lz = int(math.floor((wz + dz * step_m - dx * lat) / cell_sz))
                if _is_cell_blocked(lx, lz, ny):
                    corridor_blocked = True
                    break
            if corridor_blocked:
                break
            cur_y = ny
            run_m = float(step_m)
        return run_m

    rad_cells = int(math.ceil(min_clearance_m / cell_sz))
    fx, fz = focus_metal_xz

    road_keys = list(road_y.keys())
    road_keys.sort(key=lambda k: ((k[0] + 0.5) * cell_sz - fx) ** 2 + ((k[1] + 0.5) * cell_sz - fz) ** 2)

    best_score = -1e18
    best_spawn: Optional[Tuple[float, float, float]] = None
    best_yaw = 0.0
    best_meta: Dict[str, Any] = {}

    angles = [math.radians(deg) for deg in range(0, 360, 10)]
    for cx, cz in road_keys[:2000]:
        ry = road_y[(cx, cz)]
        # Verify circular obstacle-free clearance >= min_clearance_m AND solid wide road footing
        blocked = False
        road_neighbors = 0
        min_obs_d = 99.0
        for dx in range(-rad_cells - 1, rad_cells + 2):
            for dz in range(-rad_cells - 1, rad_cells + 2):
                d_m = math.hypot(dx * cell_sz, dz * cell_sz)
                if _is_cell_blocked(cx + dx, cz + dz, ry):
                    if d_m < min_obs_d:
                        min_obs_d = d_m
                    if d_m <= min_clearance_m:
                        blocked = True
                        break
                if d_m <= 4.5:
                    ny = road_y.get((cx + dx, cz + dz))
                    if ny is not None and abs(ny - ry) <= 1.2:
                        road_neighbors += 1
            if blocked:
                break
        if blocked or road_neighbors < 10:
            continue

        wx = (cx + 0.5) * cell_sz
        wz = (cz + 0.5) * cell_sz
        best_ang = 0.0
        best_pair_score = -1e18
        best_fwd = 0.0
        best_rear = 0.0
        for ang in angles:
            dx = math.sin(ang)
            dz = math.cos(ang)
            fwd_run = _ray_road_run(wx, wz, ry, dx, dz, max_m=120, half_w=3.5)
            if fwd_run < 36.0:
                continue
            # Require wide-open rear corridor behind the car (>= 18m clear, +-4.5m lateral)
            rear_run = _ray_road_run(wx, wz, ry, -dx, -dz, max_m=40, half_w=4.5)
            if rear_run < 18.0:
                continue
            # Count scenic street-edge storefronts/trees along left & right flanks (12..26m)
            scenic_hits = 0
            for step_m in (10.0, 24.0, 40.0):
                for side in (-1.0, 1.0):
                    for lat in (14.0, 22.0):
                        sx4 = int(math.floor((wx + dx * step_m + dz * lat * side) / 4.0))
                        sz4 = int(math.floor((wz + dz * step_m - dx * lat * side) / 4.0))
                        if (sx4, sz4) in scenic_cells:
                            scenic_hits += 1
                            break
            pair_score = fwd_run * 2.5 + min(rear_run, 36.0) * 2.2 + scenic_hits * 18.0
            if pair_score > best_pair_score:
                best_pair_score = pair_score
                best_ang = ang
                best_fwd = fwd_run
                best_rear = rear_run

        if best_fwd < 36.0 or best_rear < 18.0:
            continue

        dist_focus = math.hypot(wx - fx, wz - fz)
        score = best_pair_score + road_neighbors * 2.5 + min(min_obs_d, 14.0) * 4.0 - dist_focus * 0.20
        if score > best_score:
            best_score = score
            best_spawn = (wx, ry, wz)
            best_yaw = best_ang
            best_meta = {
                "verified": True,
                "clearance_m": min_clearance_m,
                "min_obstacle_dist_m": round(min(min_obs_d, 99.0), 2),
                "forward_road_run_m": best_fwd,
                "rear_road_run_m": best_rear,
                "road_neighbor_cells": road_neighbors,
                "dist_to_focus_m": round(dist_focus, 2),
            }

    if best_spawn is None:
        cx, cz = road_keys[0]
        ry = road_y[(cx, cz)]
        best_spawn = ((cx + 0.5) * cell_sz, ry, (cz + 0.5) * cell_sz)
        best_meta = {"verified": False, "reason": "fallback_closest_road"}

    # Refine exact surface elevation Y by intersecting vertical line (sx, sz) with actual ROAD_ASPHALT triangles
    sx, sy, sz = best_spawn
    exact_ys: List[float] = []
    for mesh_idx, _sec_idx, _ifl, xform in instances:
        m = meshes[mesh_idx]
        # Quick AABB reject
        bmin_w = apply_metal_4x3(xform, np.array([m.aabb_min_metal, m.aabb_max_metal], dtype=np.float64))
        lo = bmin_w.min(axis=0) - 5.0
        hi = bmin_w.max(axis=0) + 5.0
        if sx < lo[0] or sx > hi[0] or sz < lo[2] or sz > hi[2]:
            continue
        w_pts = apply_metal_4x3(xform, m.positions_metal.astype(np.float64))
        tris = m.indices.reshape(-1, 3)
        t_cls = mat_class[mesh_vert_mats[mesh_idx][tris[:, 0]]]
        r_tris = tris[t_cls == SURFACE_ROAD_ASPHALT]
        if len(r_tris) == 0:
            continue
        p0 = w_pts[r_tris[:, 0]]
        p1 = w_pts[r_tris[:, 1]]
        p2 = w_pts[r_tris[:, 2]]
        # 2D barycentric test in XZ plane
        v0x, v0z = p2[:, 0] - p0[:, 0], p2[:, 2] - p0[:, 2]
        v1x, v1z = p1[:, 0] - p0[:, 0], p1[:, 2] - p0[:, 2]
        v2x, v2z = sx - p0[:, 0], sz - p0[:, 2]
        dot00 = v0x * v0x + v0z * v0z
        dot01 = v0x * v1x + v0z * v1z
        dot02 = v0x * v2x + v0z * v2z
        dot11 = v1x * v1x + v1z * v1z
        dot12 = v1x * v2x + v1z * v2z
        denom = dot00 * dot11 - dot01 * dot01
        valid = np.abs(denom) > 1e-8
        u = np.where(valid, (dot11 * dot02 - dot01 * dot12) / np.where(valid, denom, 1.0), -1.0)
        v = np.where(valid, (dot00 * dot12 - dot01 * dot02) / np.where(valid, denom, 1.0), -1.0)
        inside = valid & (u >= -1e-4) & (v >= -1e-4) & (u + v <= 1.0001)
        if np.any(inside):
            ys_hit = p0[inside, 1] + u[inside] * (p2[inside, 1] - p0[inside, 1]) + v[inside] * (p1[inside, 1] - p0[inside, 1])
            for yh in ys_hit.tolist():
                if abs(yh - sy) < 3.0:
                    exact_ys.append(yh)

    if exact_ys:
        # Pick highest road surface within 3m of grid estimate (avoids underground tunnels)
        sy = max(exact_ys)
        best_meta["exact_surface_y_snap"] = True
    else:
        best_meta["exact_surface_y_snap"] = False

    return (float(sx), float(sy), float(sz)), float(best_yaw), best_meta


def render_layout_check_png(
    out_png: Path,
    district_name: str,
    meshes: Sequence[DecodedWorldSolid],
    mesh_vert_mats: Sequence[np.ndarray],
    materials: Sequence[RkptMaterialSpec],
    instances: Sequence[Tuple[int, int, int, Tuple[float, ...]]],
    world_min: Tuple[float, float, float],
    world_max: Tuple[float, float, float],
    spawn_pos: Tuple[float, float, float],
    spawn_yaw_rad: float,
    img_size: int = 1200,
) -> Path:
    """Render a top-down orthographic verification PNG and Pillow-validate before returning."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (img_size, img_size), (18, 22, 28))
    draw = ImageDraw.Draw(img)

    pad = 64
    span_x = max(1.0, world_max[0] - world_min[0])
    span_z = max(1.0, world_max[2] - world_min[2])
    scale = (img_size - 2 * pad) / max(span_x, span_z)
    off_x = pad + (img_size - 2 * pad - span_x * scale) * 0.5
    off_z = pad + (img_size - 2 * pad - span_z * scale) * 0.5

    def to_px(x: float, z: float) -> Tuple[float, float]:
        # +X right, +Z down (matching standard top-down XZ view: north = -Z up, east = +X right)
        return (off_x + (x - world_min[0]) * scale, off_z + (z - world_min[2]) * scale)

    # Surface class color palette matching contract specification
    palette: Dict[int, Tuple[int, int, int]] = {
        SURFACE_TERRAIN_GRASS_DIRT: (56, 84, 52),
        SURFACE_WATER: (36, 118, 168),
        SURFACE_ROAD_ASPHALT: (62, 64, 70),
        SURFACE_SIDEWALK: (146, 148, 152),
        SURFACE_CONCRETE_WALL: (182, 160, 128),
        SURFACE_BUILDING_GLASS: (102, 148, 196),
        SURFACE_METAL_BRIDGE: (126, 138, 154),
        SURFACE_EMISSIVE_SIGN_LAMP: (238, 192, 64),
        SURFACE_FOLIAGE_CUTOUT: (46, 138, 62),
    }

    # Layer draw order: terrain -> water -> road -> sidewalk -> walls/glass -> bridges -> signs -> foliage
    layer_order = (
        SURFACE_TERRAIN_GRASS_DIRT,
        SURFACE_WATER,
        SURFACE_ROAD_ASPHALT,
        SURFACE_SIDEWALK,
        SURFACE_CONCRETE_WALL,
        SURFACE_BUILDING_GLASS,
        SURFACE_METAL_BRIDGE,
        SURFACE_EMISSIVE_SIGN_LAMP,
        SURFACE_FOLIAGE_CUTOUT,
    )
    mat_class = np.array([m.surface_class for m in materials], dtype=np.int32)
    layer_polys: Dict[int, List[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]]] = {
        c: [] for c in layer_order
    }

    for mesh_idx, _sec_idx, _ifl, xform in instances:
        m = meshes[mesh_idx]
        w_pts = apply_metal_4x3(xform, m.positions_metal.astype(np.float64))
        tris = m.indices.reshape(-1, 3)
        t_cls = mat_class[mesh_vert_mats[mesh_idx][tris[:, 0]]]
        px = off_x + (w_pts[:, 0] - world_min[0]) * scale
        pz = off_z + (w_pts[:, 2] - world_min[2]) * scale
        for cls_id in layer_order:
            mask = t_cls == cls_id
            if not np.any(mask):
                continue
            sub = tris[mask]
            p0x, p0z = px[sub[:, 0]], pz[sub[:, 0]]
            p1x, p1z = px[sub[:, 1]], pz[sub[:, 1]]
            p2x, p2z = px[sub[:, 2]], pz[sub[:, 2]]
            # Skip sub-pixel slivers in layout preview
            area2 = np.abs((p1x - p0x) * (p2z - p0z) - (p2x - p0x) * (p1z - p0z))
            keep = area2 >= 0.35
            if not np.any(keep):
                continue
            bucket = layer_polys[cls_id]
            for x0, z0, x1, z1, x2, z2 in zip(
                p0x[keep].tolist(), p0z[keep].tolist(),
                p1x[keep].tolist(), p1z[keep].tolist(),
                p2x[keep].tolist(), p2z[keep].tolist(),
            ):
                bucket.append(((x0, z0), (x1, z1), (x2, z2)))

    for cls_id in layer_order:
        col = palette[cls_id]
        for tri in layer_polys[cls_id]:
            draw.polygon(tri, fill=col)

    # Subtle 100m coordinate grid
    gx0 = int(math.floor(world_min[0] / 100.0)) * 100
    gx1 = int(math.ceil(world_max[0] / 100.0)) * 100
    for gx in range(gx0, gx1 + 1, 100):
        px_g, _ = to_px(float(gx), world_min[2])
        if pad <= px_g <= img_size - pad:
            draw.line([(px_g, pad), (px_g, img_size - pad)], fill=(44, 52, 64), width=1)
    gz0 = int(math.floor(world_min[2] / 100.0)) * 100
    gz1 = int(math.ceil(world_max[2] / 100.0)) * 100
    for gz in range(gz0, gz1 + 1, 100):
        _, pz_g = to_px(world_min[0], float(gz))
        if pad <= pz_g <= img_size - pad:
            draw.line([(pad, pz_g), (img_size - pad, pz_g)], fill=(44, 52, 64), width=1)

    # Bright red spawn marker + street-aligned directional arrow
    spx, spz = to_px(spawn_pos[0], spawn_pos[2])
    r_outer = max(10.0, 6.0 * scale)
    draw.ellipse(
        [(spx - r_outer, spz - r_outer), (spx + r_outer, spz + r_outer)],
        outline=(255, 48, 48),
        width=3,
    )
    draw.ellipse([(spx - 5, spz - 5), (spx + 5, spz + 5)], fill=(255, 32, 32))
    arrow_len = max(36.0, 32.0 * scale)
    ax = spx + math.sin(spawn_yaw_rad) * arrow_len
    az = spz + math.cos(spawn_yaw_rad) * arrow_len
    draw.line([(spx, spz), (ax, az)], fill=(255, 32, 32), width=4)
    # Arrowhead wings
    head_ang_l = spawn_yaw_rad + math.radians(150)
    head_ang_r = spawn_yaw_rad - math.radians(150)
    draw.polygon(
        [
            (ax, az),
            (ax + math.sin(head_ang_l) * 14.0, az + math.cos(head_ang_l) * 14.0),
            (ax + math.sin(head_ang_r) * 14.0, az + math.cos(head_ang_r) * 14.0),
        ],
        fill=(255, 32, 32),
    )

    # Header & legend overlay
    draw.rectangle([(12, 10), (img_size - 12, 46)], fill=(12, 16, 22))
    draw.text(
        (22, 20),
        f"NFSMW Rockport (.rkpt v1) Layout: {district_name.upper()} | "
        f"Spawn=({spawn_pos[0]:.1f}, {spawn_pos[1]:.1f}, {spawn_pos[2]:.1f})m yaw={math.degrees(spawn_yaw_rad):.1f} deg | "
        f"Span={span_x:.0f}x{span_z:.0f}m",
        fill=(235, 240, 248),
    )

    # Legend bar at bottom
    draw.rectangle([(12, img_size - 44), (img_size - 12, img_size - 10)], fill=(12, 16, 22))
    legend_items = [
        ("Road", palette[SURFACE_ROAD_ASPHALT]),
        ("Sidewalk", palette[SURFACE_SIDEWALK]),
        ("Wall", palette[SURFACE_CONCRETE_WALL]),
        ("Glass", palette[SURFACE_BUILDING_GLASS]),
        ("Foliage", palette[SURFACE_FOLIAGE_CUTOUT]),
        ("Bridge", palette[SURFACE_METAL_BRIDGE]),
        ("Grass/Dirt", palette[SURFACE_TERRAIN_GRASS_DIRT]),
        ("Spawn", (255, 32, 32)),
    ]
    lx = 24
    for label, col in legend_items:
        draw.rectangle([(lx, img_size - 33), (lx + 14, img_size - 19)], fill=col)
        draw.text((lx + 20, img_size - 33), label, fill=(220, 228, 236))
        lx += 132

    # Scale bar (100m)
    bar_px = 100.0 * scale
    bx0, by0 = img_size - pad - bar_px, img_size - 64
    draw.line([(bx0, by0), (bx0 + bar_px, by0)], fill=(240, 244, 248), width=3)
    draw.text((bx0, by0 - 16), "100 m", fill=(240, 244, 248))

    img.save(out_png, format="PNG")

    # Validate decodability immediately (Pillow verify/load rule)
    with Image.open(out_png) as chk:
        chk.load()

    return out_png


# ---------------------------------------------------------------------------
# .rkpt v1 Binary Writer & Self-Verifier
# ---------------------------------------------------------------------------


def _pad16(buf: bytearray) -> int:
    rem = len(buf) & 15
    if rem:
        buf.extend(b"\x00" * (16 - rem))
    return len(buf)


def write_rkpt_v1(
    out_path: Path,
    sections_meta: Sequence[Tuple[str, int, int, int, Tuple[float, float, float], float, Tuple[float, float, float], Tuple[float, float, float]]],
    meshes: Sequence[DecodedWorldSolid],
    mesh_vert_mats: Sequence[np.ndarray],
    mesh_flags: Sequence[int],
    instances: Sequence[Tuple[int, int, int, Tuple[float, ...]]],
    materials: Sequence[RkptMaterialSpec],
    textures: Sequence[DecodedRkptTexture],
    world_bounds_min: Tuple[float, float, float],
    world_bounds_max: Tuple[float, float, float],
    spawn_pos: Tuple[float, float, float],
    spawn_yaw_rad: float,
    ps2_origin: Tuple[float, float, float],
) -> Dict[str, Any]:
    """Serialize assembled district geometry, materials, instances, and RGBA8 textures to `.rkpt` v1."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray(HEADER_SIZE)  # Placeholder header; filled at end

    # 1. Sections (64 B each)
    sections_off = _pad16(buf)
    for name, sec_num, inst_start, inst_cnt, center, radius, a_min, a_max in sections_meta:
        name8 = name.encode("ascii", errors="ignore")[:8].ljust(8, b"\x00")
        buf.extend(
            struct.pack(
                SECTION_FMT,
                name8,
                sec_num,
                inst_start,
                inst_cnt,
                center[0], center[1], center[2],
                radius,
                a_min[0], a_min[1], a_min[2],
                a_max[0], a_max[1], a_max[2],
                0,
            )
        )

    # 2. Meshes (48 B each)
    meshes_off = _pad16(buf)
    cur_v_off = 0
    cur_i_off = 0
    for m, mfl in zip(meshes, mesh_flags):
        v_cnt = int(len(m.positions_metal))
        i_cnt = int(len(m.indices))
        buf.extend(
            struct.pack(
                MESH_FMT,
                m.name_hash & 0xFFFFFFFF,
                cur_v_off,
                v_cnt,
                cur_i_off,
                i_cnt,
                mfl & 0xFFFFFFFF,
                m.aabb_min_metal[0], m.aabb_min_metal[1], m.aabb_min_metal[2],
                m.aabb_max_metal[0], m.aabb_max_metal[1], m.aabb_max_metal[2],
            )
        )
        cur_v_off += v_cnt
        cur_i_off += i_cnt

    total_verts = cur_v_off
    total_indices = cur_i_off

    # 3. Instances (64 B each)
    instances_off = _pad16(buf)
    for mesh_idx, sec_idx, ifl, xform12 in instances:
        buf.extend(
            struct.pack(
                INSTANCE_FMT,
                mesh_idx,
                sec_idx,
                ifl & 0xFFFFFFFF,
                0,
                *xform12,
            )
        )

    # 4. Materials (32 B each)
    materials_off = _pad16(buf)
    for mat in materials:
        buf.extend(
            struct.pack(
                MATERIAL_FMT,
                mat.diffuse_tex_index & 0xFFFFFFFF,
                mat.surface_class & 0xFFFFFFFF,
                mat.flags & 0xFFFFFFFF,
                float(mat.roughness_hint),
                float(mat.metallic_hint),
                float(mat.emissive_hint),
                float(mat.bump_strength),
                mat.tpk_name_hash & 0xFFFFFFFF,
            )
        )

    # 5. Textures table (32 B each) + packed texData buffer (16B-aligned entries)
    textures_off = _pad16(buf)
    tex_data_blob = bytearray()
    for tex in textures:
        rem = len(tex_data_blob) & 15
        if rem:
            tex_data_blob.extend(b"\x00" * (16 - rem))
        rel_off = len(tex_data_blob)
        expected_sz = tex.width * tex.height * 4
        if len(tex.rgba_bytes) != expected_sz:
            raise ValueError(f"Texture {tex.name} RGBA size mismatch: {len(tex.rgba_bytes)} != {expected_sz}")
        tex_data_blob.extend(tex.rgba_bytes)
        buf.extend(
            struct.pack(
                TEXTURE_FMT,
                tex.tpk_name_hash & 0xFFFFFFFF,
                tex.width,
                tex.height,
                0,  # pixelFormat = 0 (RGBA8_UNORM_SRGB)
                rel_off,
                expected_sz,
                tex.flags & 0xFFFFFFFF,
            )
        )

    # 6. Vertices (36 B each, vectorized via NumPy structured dtype for speed)
    vertices_off = _pad16(buf)
    v_dtype = np.dtype(
        [
            ("pos", "<3f4"),
            ("norm", "<3f4"),
            ("uv", "<2f4"),
            ("mat", "<u2"),
            ("ao", "u1"),
            ("fl", "u1"),
        ]
    )
    assert v_dtype.itemsize == VERTEX_SIZE
    for m, vmats in zip(meshes, mesh_vert_mats):
        n = len(m.positions_metal)
        arr = np.empty(n, dtype=v_dtype)
        arr["pos"] = m.positions_metal
        arr["norm"] = m.normals_metal
        arr["uv"] = m.uvs_metal
        arr["mat"] = vmats.astype(np.uint16)
        arr["ao"] = m.vertex_ao
        arr["fl"] = 0
        buf.extend(arr.tobytes())

    # 7. Indices (uint32 each)
    indices_off = _pad16(buf)
    for m in meshes:
        buf.extend(np.ascontiguousarray(m.indices, dtype="<u4").tobytes())

    # 8. Raw RGBA8 TexData
    tex_data_off = _pad16(buf)
    buf.extend(tex_data_blob)

    # Patch 160-byte Header
    header = struct.pack(
        HEADER_FMT,
        RKPT_MAGIC,
        RKPT_VERSION,
        0,
        len(sections_meta),
        len(meshes),
        len(instances),
        len(materials),
        len(textures),
        total_verts,
        total_indices,
        world_bounds_min[0], world_bounds_min[1], world_bounds_min[2],
        world_bounds_max[0], world_bounds_max[1], world_bounds_max[2],
        spawn_pos[0], spawn_pos[1], spawn_pos[2],
        spawn_yaw_rad,
        sections_off,
        meshes_off,
        instances_off,
        materials_off,
        textures_off,
        vertices_off,
        indices_off,
        tex_data_off,
        ps2_origin[0], ps2_origin[1], ps2_origin[2],
        0,
    )
    buf[0:HEADER_SIZE] = header
    out_path.write_bytes(buf)

    return {
        "file_bytes": len(buf),
        "sections_offset": sections_off,
        "meshes_offset": meshes_off,
        "instances_offset": instances_off,
        "materials_offset": materials_off,
        "textures_offset": textures_off,
        "vertices_offset": vertices_off,
        "indices_offset": indices_off,
        "tex_data_offset": tex_data_off,
        "tex_data_bytes": len(tex_data_blob),
    }


def read_rkpt_header(path_or_bytes: Any) -> Dict[str, Any]:
    """Parse and validate a `.rkpt` v1 binary header + section offsets."""
    if isinstance(path_or_bytes, (str, Path)):
        p = Path(path_or_bytes)
        file_size = p.stat().st_size
        with p.open("rb") as f:
            raw = f.read(HEADER_SIZE)
    else:
        raw = bytes(path_or_bytes[:HEADER_SIZE])
        file_size = len(path_or_bytes)

    if len(raw) < HEADER_SIZE:
        raise ValueError(f"RKPT header truncated: {len(raw)} < {HEADER_SIZE}")
    vals = struct.unpack(HEADER_FMT, raw)
    magic = vals[0]
    if magic != RKPT_MAGIC:
        raise ValueError(f"Invalid RKPT magic: {magic!r}")
    out = {
        "magic": magic.decode("ascii"),
        "version": vals[1],
        "flags": vals[2],
        "sectionCount": vals[3],
        "meshCount": vals[4],
        "instanceCount": vals[5],
        "materialCount": vals[6],
        "textureCount": vals[7],
        "totalVertexCount": vals[8],
        "totalIndexCount": vals[9],
        "worldBoundsMin": [vals[10], vals[11], vals[12]],
        "worldBoundsMax": [vals[13], vals[14], vals[15]],
        "spawnPos": [vals[16], vals[17], vals[18]],
        "spawnYawRad": vals[19],
        "sectionsOffset": vals[20],
        "meshesOffset": vals[21],
        "instancesOffset": vals[22],
        "materialsOffset": vals[23],
        "texturesOffset": vals[24],
        "verticesOffset": vals[25],
        "indicesOffset": vals[26],
        "texDataOffset": vals[27],
        "ps2Origin": [vals[28], vals[29], vals[30]],
        "reserved": vals[31],
        "fileSize": file_size,
    }
    for k in (
        "sectionsOffset", "meshesOffset", "instancesOffset", "materialsOffset",
        "texturesOffset", "verticesOffset", "indicesOffset", "texDataOffset",
    ):
        if (out[k] & 15) != 0:
            raise ValueError(f"Offset {k}={out[k]} is not 16-byte aligned")
    return out


# ---------------------------------------------------------------------------
# High-Level District Export Pipeline
# ---------------------------------------------------------------------------


def export_rockport_district(
    iso_path: Path,
    district: str,
    out_dir: Path,
    render_layout: bool = True,
    enhance_textures: bool = True,
) -> Dict[str, Any]:
    """Extract, assemble, verify, and serialize one Rockport City pilot district to `.rkpt` v1."""
    district_key = district.strip().lower()
    if district_key not in PILOT_DISTRICTS:
        raise ValueError(f"Unknown district {district!r}; valid: {sorted(PILOT_DISTRICTS)}")
    if not iso_path.is_file():
        raise FileNotFoundError(f"ISO not found: {iso_path}")

    cfg = PILOT_DISTRICTS[district_key]
    target_sec_names: Tuple[str, ...] = cfg["sections"]

    # Validate no panorama sections (80..99)
    for sname in target_sec_names:
        num = int(sname[1:])
        if num >= 80:
            raise ValueError(f"Panorama/LOD section {sname} must not be included in full-detail export")

    arc = PS2NFSMWArchive(iso_path)
    l2ra_bytes = arc.read_entry(arc.find(r"TRACKS\L2RA.BUN"))
    stream_entry = arc.find(r"TRACKS\STREAML2RA.BUN")
    stream_abs_off = (arc.zz_lbas[stream_entry.zz_index] + stream_entry.sector_in_zz) * 2048

    section_table = parse_l2ra_section_table(l2ra_bytes)
    missing_secs = [s for s in target_sec_names if s not in section_table]
    if missing_secs:
        raise KeyError(f"Missing sections in L2RA.BUN: {missing_secs}")

    # Helper to read a slice of STREAML2RA.BUN strictly read-only without loading all 191 MB if needed
    # (opening ISO once in 'rb' for fast random-access section reads)
    section_bufs: Dict[str, bytes] = {}
    x_names = sorted(k for k in section_table if k.startswith("X"))
    v_names = sorted(k for k in section_table if k.startswith("V"))
    u_names = sorted(k for k in section_table if k.startswith("U"))
    y_names = sorted(k for k in section_table if k.startswith("Y"))
    w_names = sorted(k for k in section_table if k.startswith("W"))

    with iso_path.open("rb") as iso_f:
        def _read_sec(sname: str) -> bytes:
            if sname in section_bufs:
                return section_bufs[sname]
            ent = section_table[sname]
            iso_f.seek(stream_abs_off + ent.file_offset)
            data = iso_f.read(ent.size)
            section_bufs[sname] = data
            return data

        # 1. Index solids in priority order: district local > X* > V* > U*
        solid_index: Dict[int, Tuple[bytes, int, int, str]] = {}
        for sname in target_sec_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)
        for sname in x_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)
        for sname in v_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)
        for sname in u_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)

        # 2. Parse SceneryGroups from all district spatial sections
        sec_infos: Dict[str, List[SceneryInfoEntry]] = {}
        sec_insts: Dict[str, List[SceneryInstanceEntry]] = {}
        for sname in target_sec_names:
            infos, insts = parse_scenery_groups(_read_sec(sname))
            sec_infos[sname] = infos
            sec_insts[sname] = insts

        # Compute district origin in PS2 Z-up space (centered on section centers, oz=0.0)
        xs_c = [section_table[s].center_x for s in target_sec_names]
        ys_c = [section_table[s].center_y for s in target_sec_names]
        ps2_origin = (
            round(float(sum(xs_c) / len(xs_c)), 3),
            round(float(sum(ys_c) / len(ys_c)), 3),
            0.0,
        )

        # 3. Filter instances & collect referenced unique solid hashes
        # Store (sec_idx, solid_hash, inst_entry)
        kept_instances_raw: List[Tuple[int, int, SceneryInstanceEntry]] = []
        filtered_excl = 0
        filtered_shd_pan = 0
        filtered_event_barriers: Dict[str, int] = collections.Counter()
        unresolved_inst = 0

        for sec_idx, sname in enumerate(target_sec_names):
            infos = sec_infos[sname]
            for inst in sec_insts[sname]:
                # Filter race triggers / smashable clutter (ExcludeFlags & 0x01)
                if inst.exclude_flags & 0x01:
                    filtered_excl += 1
                    continue
                if inst.scenery_info_idx < 0 or inst.scenery_info_idx >= len(infos):
                    unresolved_inst += 1
                    continue
                info = infos[inst.scenery_info_idx]
                # Pick highest-detail available LOD solid key (SolidKey[0] -> [1] -> [2])
                chosen_hash = 0
                chosen_name = ""
                for sk in info.solid_keys:
                    if sk and sk in solid_index:
                        chosen_hash = sk
                        chosen_name = solid_index[sk][3]
                        break
                if not chosen_hash:
                    unresolved_inst += 1
                    continue
                up_name = chosen_name.upper()
                excl_reason = is_excluded_world_solid(chosen_name, info.debug_name)
                if excl_reason == "event_barrier":
                    filtered_event_barriers[f"{chosen_name} (0x{chosen_hash:08x}, excl=0x{inst.exclude_flags:02x})"] += 1
                    continue
                if excl_reason is not None or (inst.flags & 0x01 and "SHD" in up_name):
                    filtered_shd_pan += 1
                    continue
                kept_instances_raw.append((sec_idx, chosen_hash, inst))

        # 4. Decode unique referenced solids into Metal Y-up space
        decoded_by_hash: Dict[int, DecodedWorldSolid] = {}
        wanted_tex_hashes: Set[int] = set()
        for _sec_idx, shash, _inst in kept_instances_raw:
            if shash in decoded_by_hash:
                continue
            sbuf, soff, ssz, _nm = solid_index[shash]
            dec = decode_world_solid_chunk(sbuf, soff, ssz)
            if dec is not None:
                decoded_by_hash[shash] = dec
                # Only request strip-actually-referenced texture hashes!
                used_slots = np.unique(dec.vertex_tex_slot)
                for sl in used_slots.tolist():
                    if 0 <= sl < len(dec.texture_hashes):
                        th = dec.texture_hashes[sl]
                        if th != 0:
                            wanted_tex_hashes.add(th)

        # 5. Decode matching textures in priority order: Y* > local A..T > V* > global > W*
        # Note: Y* are master high-res world TPKs; local A..T hold section-specific unique textures
        resolved_textures: Dict[int, DecodedRkptTexture] = {}
        for sname in y_names:
            if len(resolved_textures) >= len(wanted_tex_hashes):
                break
            decode_tpk_textures_matching(
                _read_sec(sname), f"STREAML2RA:{sname}", wanted_tex_hashes, resolved_textures,
                enhance_textures=enhance_textures,
            )
        for sname in target_sec_names:
            if len(resolved_textures) >= len(wanted_tex_hashes):
                break
            decode_tpk_textures_matching(
                _read_sec(sname), f"STREAML2RA:{sname}", wanted_tex_hashes, resolved_textures,
                enhance_textures=enhance_textures,
            )
        for sname in v_names:
            if len(resolved_textures) >= len(wanted_tex_hashes):
                break
            decode_tpk_textures_matching(
                _read_sec(sname), f"STREAML2RA:{sname}", wanted_tex_hashes, resolved_textures,
                enhance_textures=enhance_textures,
            )
        for gvpath in (r"GLOBAL\GLOBALB.BUN", r"GLOBAL\DYNTEX.BIN", r"GLOBAL\INGAMEA.BUN"):
            if len(resolved_textures) >= len(wanted_tex_hashes):
                break
            try:
                gdata = arc.read_entry(arc.find(gvpath))
                decode_tpk_textures_matching(
                    gdata, gvpath, wanted_tex_hashes, resolved_textures,
                    enhance_textures=enhance_textures,
                )
            except KeyError:
                pass
        for sname in w_names:
            if len(resolved_textures) >= len(wanted_tex_hashes):
                break
            decode_tpk_textures_matching(
                _read_sec(sname), f"STREAML2RA:{sname}", wanted_tex_hashes, resolved_textures,
                enhance_textures=enhance_textures,
            )

        # Secondary safeguard: drop any remaining solid whose resolved textures include SFX_TRACKBARRIER*
        event_tex_hashes = {
            h for h, t in resolved_textures.items()
            if any(sub in t.name.upper() for sub in EXCLUDED_EVENT_TEXTURE_SUBSTRINGS)
        }
        if event_tex_hashes:
            bad_solid_hashes: Set[int] = set()
            for shash, dec in list(decoded_by_hash.items()):
                used_ths = {dec.texture_hashes[int(sl)] for sl in np.unique(dec.vertex_tex_slot) if 0 <= int(sl) < len(dec.texture_hashes)}
                if used_ths & event_tex_hashes:
                    bad_solid_hashes.add(shash)
                    decoded_by_hash.pop(shash, None)
            if bad_solid_hashes:
                new_kept = []
                for sec_idx, shash, inst in kept_instances_raw:
                    if shash in bad_solid_hashes:
                        snm = solid_index[shash][3]
                        filtered_event_barriers[f"{snm} (0x{shash:08x}, tex=SFX_TRACKBARRIER, excl=0x{inst.exclude_flags:02x})"] += 1
                    else:
                        new_kept.append((sec_idx, shash, inst))
                kept_instances_raw = new_kept
                for h in event_tex_hashes:
                    resolved_textures.pop(h, None)
                    wanted_tex_hashes.discard(h)

    # 6. Build deterministic texture table, material table, mesh list, and section-grouped instances
    ordered_tex_hashes = sorted(resolved_textures.keys())
    tex_index_by_hash: Dict[int, int] = {h: idx for idx, h in enumerate(ordered_tex_hashes)}
    textures_list: List[DecodedRkptTexture] = [resolved_textures[h] for h in ordered_tex_hashes]

    materials_list: List[RkptMaterialSpec] = []
    mat_index_by_key: Dict[Tuple[int, int], int] = {}

    def _get_or_create_material(tex_hash: int, solid_name: str) -> int:
        tex_obj = resolved_textures.get(tex_hash) if tex_hash else None
        tex_name = tex_obj.name if tex_obj is not None else ""
        tex_idx = tex_index_by_hash[tex_hash] if tex_obj is not None else TEX_INDEX_NONE
        has_cutout = bool(tex_obj is not None and (tex_obj.flags & TEX_FLAG_HAS_CUTOUT_ALPHA))
        spec = classify_world_material(tex_name, solid_name, has_cutout, tex_idx, tex_hash)
        key = (tex_hash, spec.surface_class)
        if key not in mat_index_by_key:
            mat_index_by_key[key] = len(materials_list)
            materials_list.append(spec)
        return mat_index_by_key[key]

    ordered_solid_hashes = sorted(decoded_by_hash.keys())
    mesh_index_by_hash: Dict[int, int] = {}
    meshes_list: List[DecodedWorldSolid] = []
    mesh_vert_mats: List[np.ndarray] = []
    mesh_flags_list: List[int] = []

    for shash in ordered_solid_hashes:
        m = decoded_by_hash[shash]
        vmats = np.empty(len(m.positions_metal), dtype=np.int32)
        slot_to_mat: Dict[int, int] = {}
        for sl in np.unique(m.vertex_tex_slot).tolist():
            th = m.texture_hashes[sl] if 0 <= sl < len(m.texture_hashes) else 0
            slot_to_mat[sl] = _get_or_create_material(th, m.name)
        for sl, midx in slot_to_mat.items():
            vmats[m.vertex_tex_slot == sl] = midx

        # Derive RkptMesh flags
        mfl = 0
        up_name = m.name.upper()
        if up_name.startswith("TRN_") or "_CHOP_" in up_name:
            mfl |= MESH_FLAG_TERRAIN_CHOP
        if up_name.startswith(("PAN_", "SKY", "RFL_")):
            mfl |= MESH_FLAG_SKY_OR_PANORAMA
        used_mats = [materials_list[i] for i in set(slot_to_mat.values())]
        if any(mt.flags & MAT_FLAG_ALPHA_CUTOUT for mt in used_mats):
            mfl |= MESH_FLAG_HAS_ALPHA_CUTOUT
        if any(mt.surface_class == SURFACE_ROAD_ASPHALT for mt in used_mats):
            mfl |= MESH_FLAG_DRIVABLE_ROAD
        if any(mt.surface_class in (SURFACE_CONCRETE_WALL, SURFACE_BUILDING_GLASS, SURFACE_METAL_BRIDGE) for mt in used_mats):
            mfl |= MESH_FLAG_SOLID_WALL

        mesh_index_by_hash[shash] = len(meshes_list)
        meshes_list.append(m)
        mesh_vert_mats.append(vmats)
        mesh_flags_list.append(mfl)

    # Group instances by sectionIndex so each RkptSection has a contiguous [instanceStart, +instanceCount)
    per_sec_insts: List[List[Tuple[int, int, int, Tuple[float, ...]]]] = [[] for _ in target_sec_names]
    world_min = np.array([1e18, 1e18, 1e18], dtype=np.float64)
    world_max = np.array([-1e18, -1e18, -1e18], dtype=np.float64)
    sec_mins = [np.array([1e18, 1e18, 1e18], dtype=np.float64) for _ in target_sec_names]
    sec_maxs = [np.array([-1e18, -1e18, -1e18], dtype=np.float64) for _ in target_sec_names]

    # Precompute 8 AABB corners per mesh for fast world-bounds accumulation
    mesh_corners = []
    for m in meshes_list:
        lo, hi = m.aabb_min_metal, m.aabb_max_metal
        corners = np.array(
            [
                [lo[0], lo[1], lo[2]], [hi[0], lo[1], lo[2]],
                [lo[0], hi[1], lo[2]], [hi[0], hi[1], lo[2]],
                [lo[0], lo[1], hi[2]], [hi[0], lo[1], hi[2]],
                [lo[0], hi[1], hi[2]], [hi[0], hi[1], hi[2]],
            ],
            dtype=np.float64,
        )
        mesh_corners.append(corners)

    for sec_idx, shash, inst in kept_instances_raw:
        if shash not in mesh_index_by_hash:
            continue
        midx = mesh_index_by_hash[shash]
        is_id = bool(inst.flags_u32 & 0x200)
        xform12 = ps2_instance_to_metal_4x3(inst.rot9_i16, inst.pos_ps2, ps2_origin, is_identity=is_id)
        per_sec_insts[sec_idx].append((midx, sec_idx, inst.flags_u32 & 0xFFFF, xform12))

        wc = apply_metal_4x3(xform12, mesh_corners[midx])
        cmin = wc.min(axis=0)
        cmax = wc.max(axis=0)
        sec_mins[sec_idx] = np.minimum(sec_mins[sec_idx], cmin)
        sec_maxs[sec_idx] = np.maximum(sec_maxs[sec_idx], cmax)
        world_min = np.minimum(world_min, cmin)
        world_max = np.maximum(world_max, cmax)

    flat_instances: List[Tuple[int, int, int, Tuple[float, ...]]] = []
    sections_meta = []
    for sec_idx, sname in enumerate(target_sec_names):
        ent = section_table[sname]
        istart = len(flat_instances)
        s_list = per_sec_insts[sec_idx]
        flat_instances.extend(s_list)
        icnt = len(s_list)
        c_metal = ps2_point_to_metal(ent.center_x, ent.center_y, 0.0, ps2_origin)
        if icnt > 0:
            s_min = tuple(float(v) for v in sec_mins[sec_idx])
            s_max = tuple(float(v) for v in sec_maxs[sec_idx])
        else:
            s_min = c_metal
            s_max = c_metal
        sections_meta.append((sname, ent.section_number, istart, icnt, c_metal, ent.radius, s_min, s_max))

    world_bounds_min = tuple(float(v) for v in world_min)
    world_bounds_max = tuple(float(v) for v in world_max)

    # 7. Select verified unobstructed ROAD_ASPHALT spawnPos + street-aligned yaw
    fx_ps2, fy_ps2 = cfg["spawn_focus_ps2"]
    focus_metal = ps2_point_to_metal(fx_ps2, fy_ps2, 0.0, ps2_origin)
    spawn_pos, spawn_yaw_rad, spawn_meta = find_verified_road_spawn(
        meshes=meshes_list,
        mesh_vert_mats=mesh_vert_mats,
        materials=materials_list,
        instances=flat_instances,
        focus_metal_xz=(focus_metal[0], focus_metal[2]),
        min_clearance_m=6.0,
    )

    # 8. Write `.rkpt` v1 binary + self-verify header
    out_dir.mkdir(parents=True, exist_ok=True)
    rkpt_path = out_dir / f"{district_key}.rkpt"
    write_stats = write_rkpt_v1(
        out_path=rkpt_path,
        sections_meta=sections_meta,
        meshes=meshes_list,
        mesh_vert_mats=mesh_vert_mats,
        mesh_flags=mesh_flags_list,
        instances=flat_instances,
        materials=materials_list,
        textures=textures_list,
        world_bounds_min=world_bounds_min,
        world_bounds_max=world_bounds_max,
        spawn_pos=spawn_pos,
        spawn_yaw_rad=spawn_yaw_rad,
        ps2_origin=ps2_origin,
    )
    verified_hdr = read_rkpt_header(rkpt_path)

    # 9. Render Pillow-validated top-down verification PNG
    layout_png_rel: Optional[str] = None
    if render_layout:
        layout_png = out_dir / f"{district_key}_layout_check.png"
        render_layout_check_png(
            out_png=layout_png,
            district_name=district_key,
            meshes=meshes_list,
            mesh_vert_mats=mesh_vert_mats,
            materials=materials_list,
            instances=flat_instances,
            world_min=world_bounds_min,
            world_max=world_bounds_max,
            spawn_pos=spawn_pos,
            spawn_yaw_rad=spawn_yaw_rad,
        )
        layout_png_rel = layout_png.name

    # Compute surface class & triangle breakdown across instanced scene
    class_tri_counts: Dict[str, int] = collections.Counter()
    total_instanced_tris = 0
    mat_class_arr = np.array([m.surface_class for m in materials_list], dtype=np.int32)
    mesh_cls_tris: List[Dict[int, int]] = []
    for m, vmats in zip(meshes_list, mesh_vert_mats):
        tris = m.indices.reshape(-1, 3)
        tcls = mat_class_arr[vmats[tris[:, 0]]]
        uniq, cnts = np.unique(tcls, return_counts=True)
        mesh_cls_tris.append({int(u): int(c) for u, c in zip(uniq.tolist(), cnts.tolist())})
    for midx, _sidx, _ifl, _xf in flat_instances:
        for cid, cnt in mesh_cls_tris[midx].items():
            class_tri_counts[SURFACE_CLASS_NAMES[cid]] += cnt
            total_instanced_tris += cnt

    tex_resolved_pct = (
        round(100.0 * len(resolved_textures) / len(wanted_tex_hashes), 2) if wanted_tex_hashes else 100.0
    )

    enhanced_2x_count = sum(1 for t in textures_list if (t.flags & TEX_FLAG_ENHANCED_2X) != 0)
    manifest = {
        "format": "RKPT",
        "version": RKPT_VERSION,
        "district": district_key,
        "display_name": cfg["display_name"],
        "iso_name": iso_path.name,
        "rkpt_file": rkpt_path.name,
        "layout_png": layout_png_rel,
        "rkpt_bytes": write_stats["file_bytes"],
        "sections": list(target_sec_names),
        "section_count": verified_hdr["sectionCount"],
        "mesh_count": verified_hdr["meshCount"],
        "instance_count": verified_hdr["instanceCount"],
        "filtered_exclude_flags_0x01": filtered_excl,
        "filtered_shd_pan_rfl": filtered_shd_pan,
        "filtered_event_barrier_instances": sum(filtered_event_barriers.values()),
        "filtered_event_barriers": dict(sorted(filtered_event_barriers.items(), key=lambda kv: -kv[1])),
        "unresolved_instances": unresolved_inst,
        "material_count": verified_hdr["materialCount"],
        "texture_wanted_count": len(wanted_tex_hashes),
        "texture_resolved_count": verified_hdr["textureCount"],
        "texture_resolved_pct": tex_resolved_pct,
        "texture_enhanced_2x_count": enhanced_2x_count,
        "enhance_textures_enabled": enhance_textures,
        "unique_vertex_count": verified_hdr["totalVertexCount"],
        "unique_triangle_count": verified_hdr["totalIndexCount"] // 3,
        "instanced_triangle_count": total_instanced_tris,
        "ps2_origin": list(ps2_origin),
        "world_bounds_min": [round(v, 3) for v in world_bounds_min],
        "world_bounds_max": [round(v, 3) for v in world_bounds_max],
        "spawn_pos": [round(v, 3) for v in spawn_pos],
        "spawn_yaw_rad": round(spawn_yaw_rad, 6),
        "spawn_yaw_deg": round(math.degrees(spawn_yaw_rad), 2),
        "spawn_verification": spawn_meta,
        "surface_class_triangles": dict(sorted(class_tri_counts.items(), key=lambda kv: -kv[1])),
        "offsets": {k: write_stats[k] for k in write_stats if k.endswith("_offset") or k == "tex_data_bytes"},
    }
    manifest_path = out_dir / f"{district_key}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _resolve_iso_path(cli_iso: Optional[Path]) -> Path:
    if cli_iso is not None:
        return cli_iso.expanduser().resolve()
    env_iso = os.environ.get("NFSMW_ISO", "").strip()
    if env_iso:
        return Path(env_iso).expanduser().resolve()
    return DEFAULT_SOURCE_ISO


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export authentic PS2 NFSMW Rockport City districts to .rkpt v1 for Native Metal RT."
    )
    parser.add_argument(
        "--district",
        choices=("rosewood", "downtown", "all"),
        default="rosewood",
        help="Pilot district to export (default: rosewood).",
    )
    parser.add_argument(
        "--iso",
        type=Path,
        default=None,
        help="Path to untouched PS2 NFSMW Black Edition ISO (opened strictly read-only 'rb').",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "build" / "rockport_world",
        help="Output directory inside gitignored build/ (default: build/rockport_world).",
    )
    parser.add_argument(
        "--enhance-textures",
        dest="enhance_textures",
        action="store_true",
        default=True,
        help="2x Lanczos + crisp UnsharpMask upscale for <=256px world textures (default: ON).",
    )
    parser.add_argument(
        "--no-enhance-textures",
        dest="enhance_textures",
        action="store_false",
        help="Disable 2x world texture enhancement and emit native PS2 resolutions.",
    )
    parser.add_argument(
        "--no-layout",
        action="store_true",
        help="Skip rendering <district>_layout_check.png.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON manifest to stdout.")
    args = parser.parse_args(argv)

    iso_path = _resolve_iso_path(args.iso)
    targets = ("rosewood", "downtown") if args.district == "all" else (args.district,)
    results = []
    for dist in targets:
        manifest = export_rockport_district(
            iso_path=iso_path,
            district=dist,
            out_dir=args.out_dir,
            render_layout=not args.no_layout,
            enhance_textures=args.enhance_textures,
        )
        results.append(manifest)
        if not args.json:
            print(
                f"[{dist.upper()}] {manifest['rkpt_file']} ({manifest['rkpt_bytes'] / (1024*1024):.2f} MiB) | "
                f"secs={manifest['section_count']} meshes={manifest['mesh_count']} "
                f"insts={manifest['instance_count']} (filtered_barriers={manifest['filtered_event_barrier_instances']}) "
                f"mats={manifest['material_count']} "
                f"tex={manifest['texture_resolved_count']}/{manifest['texture_wanted_count']} "
                f"({manifest['texture_resolved_pct']}%, 2x={manifest['texture_enhanced_2x_count']}) | "
                f"uniq_tris={manifest['unique_triangle_count']:,} inst_tris={manifest['instanced_triangle_count']:,} | "
                f"spawn={manifest['spawn_pos']} yaw={manifest['spawn_yaw_deg']}deg "
                f"({manifest['spawn_verification']})"
            )

    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
