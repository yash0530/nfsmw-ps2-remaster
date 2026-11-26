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
import subprocess
import sys
import tempfile
import time
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
RKPT_VERSION_V2 = 2

# Header flags (base header offset 8)
RKPT_HDR_FLAG_HAS_V2_EXT = 1 << 0
RKPT_HDR_FLAG_BC7_TEXTURES = 1 << 1
RKPT_HDR_FLAG_HAS_PANORAMA = 1 << 2
RKPT_HDR_FLAG_HAS_PVS = 1 << 3

HEADER_FMT = "<4s9I3f3f3ff8Q3fI"      # 160 bytes
V2_EXT_HEADER_FMT = "<8I8Q"           # 96 bytes (follows 160B header when version>=2 & HAS_V2_EXT)
SECTION_FMT = "<8s3I3ff3f3fI"         # 64 bytes
MESH_FMT = "<6I3f3f"                  # 48 bytes
INSTANCE_FMT = "<4I12f"               # 64 bytes
MATERIAL_FMT = "<3I4fI"               # 32 bytes
TEXTURE_FMT = "<4IQ2I"                # 32 bytes
VERTEX_FMT = "<3f3f2fHBB"             # 36 bytes
ROAD_NODE_FMT = "<3fHHBB7H"           # 32 bytes
ROAD_SEGMENT_FMT = "<HHBBHff"         # 16 bytes
WORLD_LIGHT_FMT = "<4f4f"             # 32 bytes
LANDMARK_FMT = "<32s3ff4I"            # 64 bytes
PVS_ZONE_FMT = "<4f4I"                # 32 bytes (minX, minZ, maxX, maxZ, firstVert, vertCount, firstSecEntry, secEntryCount)
PVS_VERT_FMT = "<2f"                  # 8 bytes  (Metal XZ vertex)

HEADER_SIZE = struct.calcsize(HEADER_FMT)
V2_EXT_HEADER_SIZE = struct.calcsize(V2_EXT_HEADER_FMT)
SECTION_SIZE = struct.calcsize(SECTION_FMT)
MESH_SIZE = struct.calcsize(MESH_FMT)
INSTANCE_SIZE = struct.calcsize(INSTANCE_FMT)
MATERIAL_SIZE = struct.calcsize(MATERIAL_FMT)
TEXTURE_SIZE = struct.calcsize(TEXTURE_FMT)
VERTEX_SIZE = struct.calcsize(VERTEX_FMT)
ROAD_NODE_SIZE = struct.calcsize(ROAD_NODE_FMT)
ROAD_SEGMENT_SIZE = struct.calcsize(ROAD_SEGMENT_FMT)
WORLD_LIGHT_SIZE = struct.calcsize(WORLD_LIGHT_FMT)
LANDMARK_SIZE = struct.calcsize(LANDMARK_FMT)
PVS_ZONE_SIZE = struct.calcsize(PVS_ZONE_FMT)
PVS_VERT_SIZE = struct.calcsize(PVS_VERT_FMT)

assert HEADER_SIZE == 160
assert V2_EXT_HEADER_SIZE == 96
assert SECTION_SIZE == 64
assert MESH_SIZE == 48
assert INSTANCE_SIZE == 64
assert MATERIAL_SIZE == 32
assert TEXTURE_SIZE == 32
assert VERTEX_SIZE == 36
assert ROAD_NODE_SIZE == 32
assert ROAD_SEGMENT_SIZE == 16
assert WORLD_LIGHT_SIZE == 32
assert LANDMARK_SIZE == 64
assert PVS_ZONE_SIZE == 32
assert PVS_VERT_SIZE == 8

# Section flags (stored in RkptSection.reserved)
SEC_FLAG_PANORAMA = 1 << 0

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

# Texture pixel formats & flags
PIXEL_FORMAT_RGBA8_SRGB = 0
PIXEL_FORMAT_BC7_SRGB = 1

TEX_FLAG_HAS_CUTOUT_ALPHA = 1 << 0
TEX_FLAG_PS2_0X80_NORMALIZED = 1 << 1
TEX_FLAG_ENHANCED_2X = 1 << 2
TEX_FLAG_NARROW_AXIS_UPSCALED = 1 << 3
TEX_FLAG_PROCEDURAL_BOTANICAL = 1 << 4
TEX_FLAG_MIPCOUNT_SHIFT = 8  # bits 8..15 pack mipCount (0 or 1 = single mip; >1 = full chain)

TEX_INDEX_NONE = 0xFFFFFFFF

# EA World Chunk IDs
CHUNK_STREAM_SECTIONS = 0x00034110
CHUNK_PVS_CONTAINER = 0x80034150
CHUNK_PVS_POLYGONS = 0x00034152
CHUNK_PVS_VISLISTS = 0x00034153
CHUNK_SCENERY_GROUP = 0x80034100
CHUNK_SCENERY_INFO = 0x00034102
CHUNK_SCENERY_INSTANCE = 0x00034103
CHUNK_ROAD_NETWORK_CARP = 0x0003B800

# Botanical cypress upgrade constants
CYPRESS_FROND_TEX_HASH = 0x4CCF947E   # ORG_TREE_CYPRESS
CYPRESS_TRUNK_TEX_HASH = 0x0DB48892   # ORG_TREE_CYPRESSTRUNK
CYPRESS_SINGLE_SOLIDS = frozenset({"XO_CT_CYPRESS_1B_00", "XO_CT_CYPRESS_1C_00"})
CYPRESS_CLUMP_SOLIDS = frozenset({"XO_CT_CYPRESSCLUMP_1B_00", "XO_CT_CYPRESSCLUMP_1C_00"})

# All-district v1.4.0 botanical foliage upgrade constants (6 sculpted archetypes, 8,610+ instances)
BOTANICAL_TEX_MAPLE_CRIMSON = 0xDBF97A3D   # ORG_CT_FALLTREE2
BOTANICAL_TEX_FALL_AMBER = 0xC874AC8F      # ORG_CT_FALLTREETOP1
BOTANICAL_TEX_POPLAR_GOLD = 0x49E9B8DE     # ORG_CT_POPLARFLORA
BOTANICAL_TEX_CEDAR_BOUGH = 0x149BB535     # ORG_CEDAR_JG
BOTANICAL_TEX_ROADSIDE_BUSH = 0xD65417BF   # ORG_CT_BUSH1
BOTANICAL_TEX_FOREST_BUSH = 0x8B8FE06F     # ORG_BUSH_H_JG
BOTANICAL_TEX_STADIUM_HEDGE = 0x63344935   # ORG_CT_HEDGELP
BOTANICAL_TEX_BEACHGRASS = 0xA755A58C      # ORG_SCRUBBRUSH_D

BOTANICAL_ARCHETYPE_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("XT_JPNMAPLE_1B", "autumn_maple"),
    ("XT_CT_FALLTREES_1B", "autumn_maple"),
    ("XT_CT_FALLTREEFILLERB", "autumn_maple"),
    ("XT_CT_POPLARD", "golden_poplar"),
    ("XT_CEDARM_1B", "pnw_cedar"),
    ("XT_CT_BUSH_1B", "roadside_bush"),
    ("XT_BUSH_FOREST", "roadside_bush"),
    ("XW_HEDGESTADIUM", "stadium_hedge"),
    ("XT_BEACHGRASS", "coastal_beachgrass"),
)

# Curated Rockport landmarks (name, focus_ps2_xy, region_id: 0=Rosewood, 1=Downtown, 2=Camden, 3=Highway)
ROCKPORT_LANDMARK_SPECS: Tuple[Tuple[str, Tuple[float, float], int], ...] = (
    ("Rosewood Blvd", (1380.0, 2340.0), 0),
    ("Downtown East Park", (2120.0, 120.0), 1),
    ("Camden Beach", (3840.0, 845.0), 2),
    ("Highway 99", (2246.0, 1400.0), 3),
    ("Rosewood College", (1969.0, 3566.0), 0),
    ("Rockport Waterfront", (1070.0, -700.0), 1),
)

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
class RkptRoadNode:
    pos_metal: Tuple[float, float, float]
    node_index: int
    flags: int
    degree: int
    adj_segments: Tuple[int, int, int, int, int, int, int]


@dataclasses.dataclass
class RkptRoadSegment:
    node0: int
    node1: int
    lane_flags: int
    dir_flags: int
    road_id: int
    length_m: float
    half_width_m: float


@dataclasses.dataclass
class RTWorldLight:
    pos_metal: Tuple[float, float, float]
    radius: float
    color_rgb: Tuple[float, float, float]  # pre-multiplied by intensity (linear radiance)
    cone_min_cos: float                    # >0 downward streetlamp cone, <=-0.9 omni


@dataclasses.dataclass
class RkptLandmark:
    name: str
    spawn_pos: Tuple[float, float, float]
    spawn_yaw_rad: float
    region_id: int
    flags: int = 0


@dataclasses.dataclass
class RkptPvsZone:
    min_x: float
    min_z: float
    max_x: float
    max_z: float
    first_vert: int
    vert_count: int
    first_sec_entry: int
    sec_entry_count: int


def parse_l2ra_pvs_zones(
    l2ra_bytes: bytes,
    sections_meta: Sequence[Tuple[Any, ...]],
    spatial_section_count: int,
    ps2_origin: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    panorama_anchor_radius_m: float = 450.0,
    max_vista_anchor_dist_m: float = 4800.0,
) -> Tuple[List[RkptPvsZone], List[Tuple[float, float]], List[int], Dict[str, Any]]:
    """Decode EA's PVS container (0x80034150: 0x00034152 polygons + 0x00034153 vis-lists) from L2RA.BUN.

    Converts all PVS polygon XZ vertices into Metal Y-up world coordinates matching `.rkpt` sections
    and resolves EA's visible section IDs cleanly into spatial section indices `0..<spatial_section_count`:
      - Direct spatial section match (`it`)
      - Paired spatial section match (`it + 40`, strictly guarded by `it < 2100 and (it % 100) < 40`)
      - Distant EA skyline panorama targets (`80..99`) anchored to the nearest in-island non-empty
        spatial skyline section within `panorama_anchor_radius_m` (up to `max_vista_anchor_dist_m`)
        so EA's real 2-4.6 km bay/highway sightlines stream crisp full-detail skyline geometry.
    """
    subs: Dict[int, bytes] = {}
    pos = 0
    end = len(l2ra_bytes)
    while pos + 8 <= end:
        cid, csz = struct.unpack_from("<II", l2ra_bytes, pos)
        pay_off = pos + 8
        pay_end = min(end, pay_off + csz)
        if cid == CHUNK_PVS_CONTAINER:
            sp = pay_off
            while sp + 8 <= pay_end:
                scid, scsz = struct.unpack_from("<II", l2ra_bytes, sp)
                subs[scid] = l2ra_bytes[sp + 8 : min(pay_end, sp + 8 + scsz)]
                sp += 8 + scsz
            break
        pos = pay_off + csz

    b52 = subs.get(CHUNK_PVS_POLYGONS)
    b53 = subs.get(CHUNK_PVS_VISLISTS)
    if not b52 or not b53 or spatial_section_count <= 0:
        return [], [], [], {
            "pvs_polygon_count": 0,
            "pvs_vislist_count": 0,
            "pvs_zone_count": 0,
            "pvs_vert_count": 0,
            "pvs_entry_count": 0,
        }

    # 1. Parse 0x00034152 PVS zone polygons (bTNode(8B) + u16 zid + u8 nverts + u8 flags + 6*f32 + nverts*2*f32)
    raw_spatial_polys: List[Tuple[int, Tuple[float, float], List[Tuple[float, float]]]] = []
    p = 0
    total_polys_all = 0
    while p + 36 <= len(b52):
        _p0, _p1, zid, nverts, zflags = struct.unpack_from("<IIHBB", b52, p)
        _bmin_x, _bmin_y, _bmax_x, _bmax_y, cx_ps2, cy_ps2 = struct.unpack_from("<6f", b52, p + 12)
        v_end = p + 36 + nverts * 8
        if v_end > len(b52):
            break
        total_polys_all += 1
        if zflags == 0 and nverts >= 3:
            verts_ps2 = [struct.unpack_from("<2f", b52, p + 36 + i * 8) for i in range(nverts)]
            raw_spatial_polys.append((zid, (cx_ps2, cy_ps2), verts_ps2))
        p = v_end

    # 2. Parse 0x00034153 PVS section visibility lists
    vis_lists: Dict[int, Tuple[int, ...]] = {}
    p = 0
    while p + 18 <= len(b53):
        u0, u1, _u2 = struct.unpack_from("<III", b53, p)
        if (u0, u1) != (11, 11):
            nxt = b53.find(struct.pack("<II", 11, 11), p + 4)
            if nxt < 0:
                break
            p = nxt
            continue
        zid = struct.unpack_from("<H", b53, p + 12)[0]
        _cnt0, cap0 = b53[p + 14], b53[p + 15]
        cnt1 = struct.unpack_from("<H", b53, p + 16)[0]
        if p + 18 + cnt1 * 2 > len(b53):
            break
        vis_lists[zid] = struct.unpack_from(f"<{cnt1}H", b53, p + 18)
        p = (p + 18 + cap0 * 2 + 3) & ~3

    # Build section_number -> rkpt section index lookup tables
    sp_idx_by_num: Dict[int, int] = {}
    pan_idx_by_num: Dict[int, int] = {}
    for s_idx, row in enumerate(sections_meta):
        s_num = int(row[1])
        if s_idx < spatial_section_count:
            sp_idx_by_num[s_num] = s_idx
        else:
            pan_idx_by_num[s_num] = s_idx

    # Pre-anchor each panorama target (80..99) to its closest non-empty in-island spatial skyline section
    pan_to_skyline_sp: Dict[int, int] = {}
    for pan_snum, pan_sidx in pan_idx_by_num.items():
        pc = sections_meta[pan_sidx][4]
        best_key: Optional[Tuple[float, int]] = None
        best_sp = -1
        for sp_idx in range(spatial_section_count):
            icnt = int(sections_meta[sp_idx][3])
            if icnt <= 0:
                continue
            sc = sections_meta[sp_idx][4]
            d = math.hypot(sc[0] - pc[0], sc[2] - pc[2])
            if d <= panorama_anchor_radius_m:
                key = (d, -icnt)
                if best_key is None or key < best_key:
                    best_key = key
                    best_sp = sp_idx
        if best_sp >= 0:
            pan_to_skyline_sp[pan_snum] = best_sp

    pvs_zones: List[RkptPvsZone] = []
    pvs_verts: List[Tuple[float, float]] = []
    pvs_entries: List[int] = []
    zone_max_vistas: List[float] = []
    matched_vis = 0

    for zid, (cx_ps2, cy_ps2), verts_ps2 in raw_spatial_polys:
        raw_items = vis_lists.get(zid)
        if raw_items is None:
            continue
        matched_vis += 1
        cm = ps2_point_to_metal(cx_ps2, cy_ps2, 0.0, ps2_origin)
        vis_sp: Set[int] = set()
        for it in raw_items:
            if it in sp_idx_by_num:
                si = sp_idx_by_num[it]
                if int(sections_meta[si][3]) > 0:
                    vis_sp.add(si)
            elif it in pan_to_skyline_sp:
                ai = pan_to_skyline_sp[it]
                ac = sections_meta[ai][4]
                if math.hypot(ac[0] - cm[0], ac[2] - cm[2]) <= max_vista_anchor_dist_m:
                    vis_sp.add(ai)
            if it < 2100 and (it % 100) < 40 and (it + 40) in sp_idx_by_num:
                si40 = sp_idx_by_num[it + 40]
                if int(sections_meta[si40][3]) > 0:
                    vis_sp.add(si40)

        sorted_sp = sorted(vis_sp)
        mverts = [
            (ps2_point_to_metal(vx, vy, 0.0, ps2_origin)[0], ps2_point_to_metal(vx, vy, 0.0, ps2_origin)[2])
            for vx, vy in verts_ps2
        ]
        xs = [v[0] for v in mverts]
        zs = [v[1] for v in mverts]
        first_v = len(pvs_verts)
        pvs_verts.extend(mverts)
        first_e = len(pvs_entries)
        pvs_entries.extend(sorted_sp)
        pvs_zones.append(
            RkptPvsZone(
                min_x=float(min(xs)),
                min_z=float(min(zs)),
                max_x=float(max(xs)),
                max_z=float(max(zs)),
                first_vert=first_v,
                vert_count=len(mverts),
                first_sec_entry=first_e,
                sec_entry_count=len(sorted_sp),
            )
        )
        if sorted_sp:
            md = max(
                math.hypot(sections_meta[si][4][0] - cm[0], sections_meta[si][4][2] - cm[2])
                for si in sorted_sp
            )
            zone_max_vistas.append(md)

    stats: Dict[str, Any] = {
        "pvs_polygon_count": len(raw_spatial_polys),
        "pvs_total_polygons_raw": total_polys_all,
        "pvs_vislist_count": len(vis_lists),
        "pvs_matched_vis_count": matched_vis,
        "pvs_zone_count": len(pvs_zones),
        "pvs_vert_count": len(pvs_verts),
        "pvs_entry_count": len(pvs_entries),
        "pvs_panorama_anchors": len(pan_to_skyline_sp),
        "pvs_vista_median_m": round(float(np.median(zone_max_vistas)), 1) if zone_max_vistas else 0.0,
        "pvs_vista_p90_m": round(float(np.percentile(zone_max_vistas, 90)), 1) if zone_max_vistas else 0.0,
        "pvs_vista_max_m": round(float(max(zone_max_vistas)), 1) if zone_max_vistas else 0.0,
    }
    return pvs_zones, pvs_verts, pvs_entries, stats


def parse_l2ra_road_network(
    l2ra_bytes: bytes,
    ps2_origin: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> Tuple[List[RkptRoadNode], List[RkptRoadSegment]]:
    """Decode EA's WRoadNetwork (0x0003B800 CARP/RNgp: RNnd nodes + RNsg segments) into Metal Y-up space.

    Coordinate verification (Rosewood median |dY|=0.08m, Downtown median |dY|=0.02m):
      EA UMath Y-up (ux, uy, uz) -> PS2 Z-up (uz, -ux, uy) -> Metal Y-up (uz - ox, uy - oz, ux + oy).
    """
    carp_raw: Optional[bytes] = None
    pos = 0
    end = len(l2ra_bytes)
    while pos + 8 <= end:
        cid, csz = struct.unpack_from("<II", l2ra_bytes, pos)
        pay_off = pos + 8
        pay_end = min(end, pay_off + csz)
        if cid == CHUNK_ROAD_NETWORK_CARP:
            start = gi._skip_11_word_padding(l2ra_bytes, pay_off, pay_end)
            carp_raw = l2ra_bytes[start:pay_end]
            break
        pos = pay_off + csz

    if not carp_raw or len(carp_raw) < 0xC0 or carp_raw[0:4] != b"PRAC":
        return [], []

    # Parse 16-byte CARP directory entries to obtain exact counts for RNpf/RNnd/RNrd/RNsg
    dir_counts: Dict[bytes, Tuple[int, int]] = {}
    for off in range(0, min(0x100, len(carp_raw) - 15), 16):
        tag_rev = carp_raw[off : off + 4][::-1]
        u1, cnt, _rel_off = struct.unpack_from("<III", carp_raw, off + 4)
        if tag_rev in (b"RNhd", b"RNnd", b"RNpf", b"RNrd", b"RNsg"):
            dir_counts[tag_rev] = (u1 >> 8, cnt)

    if b"RNnd" not in dir_counts or b"RNsg" not in dir_counts:
        return [], []

    n_pf = dir_counts.get(b"RNpf", (0, 0))[1]
    n_nodes = dir_counts[b"RNnd"][1]
    n_roads = dir_counts.get(b"RNrd", (0, 0))[1]
    n_segs = dir_counts[b"RNsg"][1]
    if n_nodes == 0 or n_segs == 0:
        return [], []

    # Locate self-verifying RNhd header: u16[5] = (n_pf, n_nodes, n_segs, 0, n_roads)
    rnhd_sig = struct.pack("<HHHHH", n_pf, n_nodes, n_segs, 0, n_roads)
    rnhd_off = carp_raw.find(rnhd_sig)
    if rnhd_off < 0:
        return [], []

    rnnd_off = rnhd_off + 16
    rnpf_off = rnnd_off + n_nodes * 32
    rnrd_off = rnpf_off + n_pf * 64
    rnsg_off = rnrd_off + n_roads * 8
    if rnsg_off + n_segs * 22 > len(carp_raw):
        return [], []

    ox, oy, oz = ps2_origin
    nodes: List[RkptRoadNode] = []
    for i in range(n_nodes):
        o = rnnd_off + i * 32
        ux, uy, uz = struct.unpack_from("<3f", carp_raw, o)
        nidx, nflags = struct.unpack_from("<HH", carp_raw, o + 12)
        deg, _pad = struct.unpack_from("<BB", carp_raw, o + 16)
        raw_adj = struct.unpack_from("<7H", carp_raw, o + 18)
        deg_clamped = min(7, int(deg))
        adj = tuple(
            int(raw_adj[j]) if (j < deg_clamped and raw_adj[j] < n_segs) else 0xFFFF
            for j in range(7)
        )
        # EA UMath (ux, uy, uz) -> PS2 (uz, -ux, uy) -> Metal (uz - ox, uy - oz, ux + oy)
        mx, my, mz = ps2_point_to_metal(uz, -ux, uy, (ox, oy, oz))
        nodes.append(
            RkptRoadNode(
                pos_metal=(mx, my, mz),
                node_index=int(nidx),
                flags=int(nflags),
                degree=deg_clamped,
                adj_segments=adj,  # type: ignore[arg-type]
            )
        )

    segments: List[RkptRoadSegment] = []
    for k in range(n_segs):
        o = rnsg_off + k * 22
        n0, n1, _u4, road_id, _sidx, lane_b, dir_b = struct.unpack_from("<HHHHHBB", carp_raw, o)
        if n0 >= n_nodes or n1 >= n_nodes:
            continue
        p0 = nodes[n0].pos_metal
        p1 = nodes[n1].pos_metal
        seg_len = math.sqrt((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2 + (p1[2] - p0[2]) ** 2)
        # Infer nominal half-width from lane byte (highways ~7.5-9.0m, boulevards ~6.0m, city streets ~4.8m)
        if lane_b >= 16:
            half_w = 7.5
        elif lane_b >= 9:
            half_w = 5.5
        else:
            half_w = 4.5
        segments.append(
            RkptRoadSegment(
                node0=int(n0),
                node1=int(n1),
                lane_flags=int(lane_b),
                dir_flags=int(dir_b),
                road_id=int(road_id),
                length_m=float(seg_len),
                half_width_m=half_w,
            )
        )

    return nodes, segments


def generate_botanical_cypress_frond_rgba(size: int = 512) -> np.ndarray:
    """Generate a clean-room procedural 512x512 billowing columnar Italian cypress foliage tuft atlas.

    Uses dense upward-swept organic conifer scale-leaf clusters (NOT flat horizontal spruce spokes),
    yielding ~44-52% feathery outer alpha cutout around an organic flame-shaped evergreen plume
    with deep interior shadow crevices and warm sunlit autumn-olive needle tips.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    u = xx / float(size - 1)          # 0..1 left->right
    v = yy / float(size - 1)          # 0..1 top->bottom (tapered tip at v=0, fuller base at v=1)
    dx = u - 0.5                      # -0.5..+0.5

    # Multi-octave organic foliage cluster noise (anisotropic: stretched slightly vertically like cypress tufts)
    n1 = (
        np.sin(u * 29.0 + np.cos(v * 19.0) * 1.5)
        * np.cos(v * 23.0 - np.sin(u * 25.0) * 1.3)
    )
    n2 = (
        np.sin(u * 67.0 - v * 43.0 + n1 * 2.2)
        * np.cos(u * 53.0 + v * 59.0 - n1 * 1.6)
    )
    n3 = np.sin(u * 149.0 + v * 127.0 + n2 * 1.9) * np.cos(u * 113.0 - v * 163.0)
    n4 = np.sin(u * 293.0 - v * 251.0 + n3 * 1.4)
    tuft_noise = 0.5 + 0.28 * n1 + 0.16 * n2 + 0.08 * n3 + 0.04 * n4

    # Upward-swept flame-shaped cypress tuft silhouette (narrow pointed tip at v=0, plump lobe v=0.4..0.85)
    # No regular horizontal wave bands — purely organic billowing cypress tufts sweeping upward at ~70 deg!
    up_v = v + np.abs(dx) * 0.42  # upward sweep toward crown tip
    env_w = np.clip(
        0.06 + 0.40 * np.power(np.sin(math.pi * np.clip(v * 0.88 + 0.06, 0.0, 1.0)), 0.72),
        0.04,
        0.47,
    )
    radial_norm = np.abs(dx) / np.maximum(env_w, 1e-4)

    # Billowing cluster lobes along upward-swept diagonal branches (zero horizontal stripes!)
    plume_lobes = (
        0.5
        + 0.30 * np.sin((up_v * 11.0 + dx * 6.5) * math.pi + n1 * 2.4)
        + 0.22 * np.cos((up_v * 19.0 - dx * 13.0) * math.pi + n2 * 1.9)
    )
    density = (
        (1.04 - radial_norm)
        + 0.26 * (plume_lobes - 0.5)
        + 0.28 * (tuft_noise - 0.5)
        + 0.12 * n3
    )
    v_fade = np.clip((v - 0.015) / 0.055, 0.0, 1.0) * np.clip((0.985 - v) / 0.055, 0.0, 1.0)
    field = density * v_fade

    # Crisp feathery conifer-scale alpha cutout (~44-52% open air around billowing edge, dense heart)
    alpha = np.where(field > 0.36, np.clip(195.0 + (field - 0.36) * 260.0, 0.0, 255.0), 0.0)

    # Rich late-autumn Pacific-NW / Mediterranean cypress palette:
    # deep shadowed pine interior -> rich mid forest-green -> warm sunlit bronze-olive outer cluster tips
    outer_lit = np.clip(0.55 * radial_norm + 0.35 * (1.0 - v) + 0.35 * (tuft_noise - 0.45), 0.0, 1.0)
    clump_ao = np.clip(0.52 + 0.48 * plume_lobes + 0.24 * n2 + 0.12 * n4, 0.28, 1.32)
    r = (17.0 + 38.0 * outer_lit + 10.0 * n3) * clump_ao
    g = (32.0 + 54.0 * outer_lit + 12.0 * n1 + 6.0 * n4) * clump_ao
    b = (16.0 + 21.0 * outer_lit + 6.0 * n2) * clump_ao

    rgba = np.empty((size, size, 4), dtype=np.uint8)
    rgba[:, :, 0] = np.clip(np.rint(r), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 1] = np.clip(np.rint(g), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 2] = np.clip(np.rint(b), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 3] = np.clip(np.rint(alpha), 0.0, 255.0).astype(np.uint8)
    # Dilate adjacent dark evergreen RGB into transparent border pixels so bilinear filtering
    # & BC7 4x4 block compression never bleed bright outer-edge tints across cutout fringes.
    return dilate_alpha_edge_rgb(rgba, iterations=8, alpha_thresh=32)


def _append_single_botanical_cypress(
    cx: float,
    cz: float,
    height_m: float,
    crown_radius_m: float,
    seed: int,
    tiers: int,
    cards_per_tier: int,
    frond_slot: int,
    trunk_slot: int,
    pos_list: List[Tuple[float, float, float]],
    norm_list: List[Tuple[float, float, float]],
    uv_list: List[Tuple[float, float]],
    ao_list: List[int],
    slot_list: List[int],
    idx_list: List[int],
) -> None:
    """Append one dense columnar Italian cypress (short visible trunk + organic core + steep 65-78 deg plumes).

    Eliminates horizontal stacked-pancake/cell-tower silhouettes: an organic multi-lobe evergreen inner
    core blocks see-through daylight gaps while heavily overlapping upward-swept vertical plume cards
    (65-78 deg pitch) give rich, billowing, feathery silhouettes from street level.
    """
    rng = np.random.default_rng(seed)
    lean_x = float(rng.uniform(-0.32, 0.32))
    lean_z = float(rng.uniform(-0.32, 0.32))

    def _crown_r(t: float) -> float:
        # Columnar flame silhouette: gentle skirt at t=0, plump lower-mid t=0.22..0.55, slender pointed spire at t=1
        base_env = math.sin(math.pi * (0.09 + 0.88 * (t ** 0.72))) ** 0.65
        taper = 1.0 - 0.38 * (t ** 1.35)
        return crown_radius_m * max(0.06, base_env * taper)

    # 1. Short visible woody trunk at base (Y = 0 .. 0.24*height, disappears cleanly inside dense foliage skirt)
    trunk_h = height_m * 0.24
    trunk_segs = 6
    trunk_rings = 2
    ring_base = len(pos_list)
    for r_i in range(trunk_rings + 1):
        t = r_i / float(trunk_rings)
        y = t * trunk_h
        rad = (0.34 * (1.0 - t) + 0.22 * t) * (crown_radius_m / 2.5)
        ao = int(125 + 45 * (1.0 - t))
        for s_i in range(trunk_segs + 1):
            ang = (s_i / float(trunk_segs)) * math.tau
            ca, sa = math.cos(ang), math.sin(ang)
            pos_list.append((cx + ca * rad, y, cz + sa * rad))
            norm_list.append((ca, 0.05, sa))
            uv_list.append((s_i / float(trunk_segs) * 2.0, t * 2.5))
            ao_list.append(ao)
            slot_list.append(trunk_slot)
    stride = trunk_segs + 1
    for r_i in range(trunk_rings):
        for s_i in range(trunk_segs):
            a = ring_base + r_i * stride + s_i
            b = a + 1
            c = a + stride
            d = c + 1
            idx_list.extend((a, b, c, b, d, c))

    # 2. Sculpted organic billowing inner evergreen core (Y = 0.11..0.95 * height)
    # Prevents horizontal gaps/daylight slicing through the trunk while keeping organic lumpy contours
    core_y0 = height_m * 0.11
    core_y1 = height_m * 0.95
    core_rings = 8
    core_segs = 7
    core_base = len(pos_list)
    phase_a = float(rng.uniform(0.0, math.tau))
    phase_b = float(rng.uniform(0.0, math.tau))
    for r_i in range(core_rings + 1):
        t = r_i / float(core_rings)
        y = core_y0 + t * (core_y1 - core_y0)
        ox = cx + lean_x * (t ** 1.4)
        oz = cz + lean_z * (t ** 1.4)
        r_base = _crown_r(t) * 0.68
        if r_i == core_rings:
            r_base = 0.14
        elif r_i == 0:
            r_base *= 0.62
        ao = int(96 + 88 * (t ** 0.85))
        for s_i in range(core_segs + 1):
            ang = (s_i / float(core_segs)) * math.tau
            # Organic vertical foliage flutes & bumps (no flat uniform cone!)
            lobe = 1.0 + 0.16 * math.sin(ang * 3.0 + t * 4.5 + phase_a) + 0.10 * math.cos(ang * 2.0 - t * 6.0 + phase_b)
            rad = r_base * lobe
            ca, sa = math.cos(ang), math.sin(ang)
            pos_list.append((ox + ca * rad, y, oz + sa * rad))
            norm_list.append((ca * 0.88, 0.47, sa * 0.88))
            # Map core onto the dense center band (u=0.22..0.78, v=0.20..0.85) of the foliage atlas so it stays opaque!
            u_core = 0.25 + 0.50 * ((s_i % 2) if s_i < core_segs else 0.0)
            v_core = 0.24 + 0.58 * (1.0 - t)
            uv_list.append((u_core, v_core))
            ao_list.append(ao)
            slot_list.append(frond_slot)
    c_stride = core_segs + 1
    for r_i in range(core_rings):
        for s_i in range(core_segs):
            a = core_base + r_i * c_stride + s_i
            b = a + 1
            c = a + c_stride
            d = c + 1
            idx_list.extend((a, b, c, b, d, c))

    # 3. Heavily-overlapping steep upward-swept (66-78 deg pitch) columnar foliage plume cards
    # Cards stand almost vertically around the core (slight outward flare at base, sweeping up & inward at top)
    # so street-level views ALWAYS see broad feathery conifer clusters — NEVER thin edge-on horizontal rings!
    golden_angle = 2.399963229728653
    y_bot = height_m * 0.095
    y_top = height_m * 0.86
    plume_h_base = (height_m / float(max(tiers, 1))) * 2.55  # >60% vertical overlap between adjacent tiers!

    for ti in range(tiers):
        frac = ti / float(max(1, tiers - 1))
        y_start = y_bot + frac * (y_top - y_bot)
        ox = cx + lean_x * (frac ** 1.4)
        oz = cz + lean_z * (frac ** 1.4)
        tier_r = _crown_r(frac)
        plume_h = plume_h_base * (1.12 - 0.34 * frac)
        y_mid = min(height_m * 0.97, y_start + plume_h * 0.52)
        y_end = min(height_m, y_start + plume_h)
        r_mid = _crown_r(min(1.0, frac + 0.07))
        r_end = _crown_r(min(1.0, frac + 0.14))
        base_az = ti * golden_angle + float(rng.uniform(-0.22, 0.22))

        for ci in range(cards_per_tier):
            az = base_az + (ci / float(cards_per_tier)) * math.tau + float(rng.uniform(-0.20, 0.20))
            dx, dz = math.cos(az), math.sin(az)
            rx, rz = -dz, dx
            r_scale = float(rng.uniform(0.92, 1.14))
            half_w = max(0.55, tier_r * (0.62 + 0.14 * (1.0 - frac)) * float(rng.uniform(0.88, 1.12)))

            # Steep upward-swept plume: bottom flares out slightly beyond core, mid swells out, top sweeps inward/up
            r0 = tier_r * 0.72 * r_scale
            r1 = max(r_mid * 1.06, tier_r * 0.96) * r_scale
            r2 = max(0.12, r_end * 0.78 * r_scale)

            # Outward + gently upward foliage normal (gives natural sunlit side vs cool shaded side!)
            nx, ny, nz = dx * 0.90, 0.43, dz * 0.90
            v0 = len(pos_list)
            # Bottom row (tucked into lower mantle, darker AO)
            pos_list.append((ox + dx * r0 - rx * (half_w * 0.85), y_start, oz + dz * r0 - rz * (half_w * 0.85)))
            pos_list.append((ox + dx * r0 + rx * (half_w * 0.85), y_start, oz + dz * r0 + rz * (half_w * 0.85)))
            # Mid billowing belly (widest, cupped slightly around trunk for 3D volume)
            pos_list.append((ox + dx * (r1 * 0.92) - rx * half_w, y_mid, oz + dz * (r1 * 0.92) - rz * half_w))
            pos_list.append((ox + dx * (r1 * 0.92) + rx * half_w, y_mid, oz + dz * (r1 * 0.92) + rz * half_w))
            # Upper upward-swept tapered plume tip (bright sunlit tip AO)
            pos_list.append((ox + dx * r2 - rx * (half_w * 0.45), y_end, oz + dz * r2 - rz * (half_w * 0.45)))
            pos_list.append((ox + dx * r2 + rx * (half_w * 0.45), y_end, oz + dz * r2 + rz * (half_w * 0.45)))

            uvs_card = ((0.05, 0.96), (0.95, 0.96), (0.02, 0.48), (0.98, 0.48), (0.14, 0.03), (0.86, 0.03))
            ao_lo = int(115 + 35 * frac)
            ao_mid = int(178 + 40 * frac)
            ao_hi = int(225 + 25 * frac)
            for k, ao_c in enumerate((ao_lo, ao_lo, ao_mid, ao_mid, ao_hi, ao_hi)):
                norm_list.append((nx, ny, nz))
                uv_list.append(uvs_card[k])
                ao_list.append(ao_c)
                slot_list.append(frond_slot)
            idx_list.extend((v0, v0 + 1, v0 + 2, v0 + 1, v0 + 3, v0 + 2))
            idx_list.extend((v0 + 2, v0 + 3, v0 + 4, v0 + 3, v0 + 5, v0 + 4))

    # 4. 3 crossed slender vertical spire crown cards at top apex (Y = 0.78..1.00 * height)
    spire_y0 = height_m * 0.78
    spire_y1 = height_m
    spire_w = crown_radius_m * 0.46
    ox_top = cx + lean_x
    oz_top = cz + lean_z
    for si in range(3):
        ang = si * (math.pi / 3.0) + float(rng.uniform(-0.15, 0.15))
        rx, rz = math.cos(ang), math.sin(ang)
        nx, nz = -rz, rx
        v0 = len(pos_list)
        pos_list.append((ox_top - rx * spire_w, spire_y0, oz_top - rz * spire_w))
        pos_list.append((ox_top + rx * spire_w, spire_y0, oz_top + rz * spire_w))
        pos_list.append((ox_top - rx * (spire_w * 0.18), spire_y1, oz_top - rz * (spire_w * 0.18)))
        pos_list.append((ox_top + rx * (spire_w * 0.18), spire_y1, oz_top + rz * (spire_w * 0.18)))
        for uv_c, ao_c in zip(((0.06, 0.94), (0.94, 0.94), (0.22, 0.02), (0.78, 0.02)), (185, 185, 250, 250)):
            norm_list.append((nx * 0.75, 0.66, nz * 0.75))
            uv_list.append(uv_c)
            ao_list.append(ao_c)
            slot_list.append(frond_slot)
        idx_list.extend((v0, v0 + 1, v0 + 2, v0 + 1, v0 + 3, v0 + 2))


def build_botanical_cypress_solid(orig: DecodedWorldSolid) -> DecodedWorldSolid:
    """Upgrade blocky 76-tri XO_CT_CYPRESS_1B/1C/CLUMP cone into a dense columnar botanical cypress."""
    up = orig.name.upper()
    tex_hashes = list(orig.texture_hashes)
    if CYPRESS_FROND_TEX_HASH in tex_hashes:
        frond_slot = tex_hashes.index(CYPRESS_FROND_TEX_HASH)
    else:
        frond_slot = len(tex_hashes)
        tex_hashes.append(CYPRESS_FROND_TEX_HASH)
    if CYPRESS_TRUNK_TEX_HASH in tex_hashes:
        trunk_slot = tex_hashes.index(CYPRESS_TRUNK_TEX_HASH)
    else:
        trunk_slot = len(tex_hashes)
        tex_hashes.append(CYPRESS_TRUNK_TEX_HASH)

    pos_list: List[Tuple[float, float, float]] = []
    norm_list: List[Tuple[float, float, float]] = []
    uv_list: List[Tuple[float, float]] = []
    ao_list: List[int] = []
    slot_list: List[int] = []
    idx_list: List[int] = []

    if up in CYPRESS_CLUMP_SOLIDS:
        # 5 varied columnar cypresses anchored at the exact 5 original XO_CT_CYPRESSCLUMP trunk centers
        clump_trees = (
            (0.30, 0.05, 21.8, 2.35, 107),
            (6.65, 0.25, 25.4, 2.65, 211),
            (12.90, -0.22, 23.6, 2.45, 317),
            (19.65, 0.18, 26.0, 2.75, 421),
            (26.45, -0.15, 21.2, 2.25, 523),
        )
        for cx, cz, h_m, r_m, sd in clump_trees:
            _append_single_botanical_cypress(
                cx, cz, h_m, r_m, sd,
                tiers=10, cards_per_tier=4,
                frond_slot=frond_slot, trunk_slot=trunk_slot,
                pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
                ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
            )
    else:
        h_m = max(12.0, float(orig.aabb_max_metal[1] - min(0.0, orig.aabb_min_metal[1])))
        span_x = float(orig.aabb_max_metal[0] - orig.aabb_min_metal[0])
        span_z = float(orig.aabb_max_metal[2] - orig.aabb_min_metal[2])
        r_m = max(2.1, min(2.75, 0.44 * max(span_x, span_z)))
        # ~400 tris: 24 trunk + 112 organic evergreen core + 13*5*4 steep plumes + 6 spire = 402 tris!
        _append_single_botanical_cypress(
            0.0, 0.0, h_m, r_m, seed=2026,
            tiers=13, cards_per_tier=5,
            frond_slot=frond_slot, trunk_slot=trunk_slot,
            pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
            ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
        )

    pos_arr = np.asarray(pos_list, dtype=np.float32)
    bmin = pos_arr.min(axis=0)
    bmax = pos_arr.max(axis=0)
    return DecodedWorldSolid(
        name=orig.name,
        name_hash=orig.name_hash,
        texture_hashes=tex_hashes,
        positions_metal=pos_arr,
        normals_metal=np.asarray(norm_list, dtype=np.float32),
        uvs_metal=np.asarray(uv_list, dtype=np.float32),
        vertex_ao=np.asarray(ao_list, dtype=np.uint8),
        vertex_tex_slot=np.asarray(slot_list, dtype=np.int32),
        indices=np.asarray(idx_list, dtype=np.uint32),
        aabb_min_metal=(float(bmin[0]), float(bmin[1]), float(bmin[2])),
        aabb_max_metal=(float(bmax[0]), float(bmax[1]), float(bmax[2])),
    )


# ---------------------------------------------------------------------------
# All-District v1.4.0 Sculpted Botanical Foliage Upgrades (6 Archetypes, 8,610+ insts)
# ---------------------------------------------------------------------------


def match_botanical_foliage_archetype(solid_name: str) -> Optional[str]:
    """Return botanical archetype key if solid_name matches one of the 6 v1.4.0 foliage upgrades."""
    up = solid_name.upper()
    for prefix, archetype in BOTANICAL_ARCHETYPE_PREFIXES:
        if up.startswith(prefix):
            return archetype
    return None


def _render_leaf_spray_cluster_rgba(
    size: int,
    seed: int,
    n_leaves: int,
    dark_rgb: Tuple[float, float, float],
    mid_rgb: Tuple[float, float, float],
    tip_rgb: Tuple[float, float, float],
    leaf_shape: str = "maple",
    vertical_stretch: float = 1.0,
) -> np.ndarray:
    """Vectorized crisp 512x512 organic multi-leaf foliage spray atlas with opaque heart & feathery border.

    Produces rich individual overlapping leaf silhouettes (maple 5-lobe, poplar ovate, shrub broad-leaf)
    with deep shadowed interior crevices, sunlit outer leaf tips, ~44-52% feathery open-air cutout,
    and full 10-iteration edge RGB dilation so bilinear/BC7 mips have zero white/gray fringes.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    u = xx / float(size - 1)
    v = yy / float(size - 1)
    cx_grid = (u - 0.5) * 2.0
    cy_grid = (v - 0.5) * 2.0 / max(0.5, vertical_stretch)

    rng = np.random.default_rng(seed)
    rgb_buf = np.zeros((size, size, 3), dtype=np.float32)
    alpha_buf = np.zeros((size, size), dtype=np.float32)
    depth_buf = np.full((size, size), -1.0, dtype=np.float32)

    # Subtle continuous leaf-cluster micro-noise for leaf vein / mottling variation
    micro = (
        0.5
        + 0.25 * np.sin(u * 83.0 + np.cos(v * 71.0) * 1.4) * np.cos(v * 79.0)
        + 0.15 * np.sin(u * 197.0 - v * 173.0)
    )

    # Guarantee an opaque organic leaf heart inside u,v in [0.28..0.72] so inner canopy cores never show holes
    heart_r = np.hypot(cx_grid, cy_grid)
    heart_noise = 0.09 * np.sin(np.arctan2(cy_grid, cx_grid) * 5.0 + 0.7) + 0.06 * (micro - 0.5)
    heart_mask = (heart_r + heart_noise) < 0.50
    for ch in range(3):
        rgb_buf[:, :, ch] = np.where(heart_mask, dark_rgb[ch] * (0.85 + 0.25 * micro), 0.0)
    alpha_buf = np.where(heart_mask, 255.0, 0.0)
    depth_buf = np.where(heart_mask, 0.05, -1.0)

    # Paint individual leaves back-to-front (interior shadowed -> outer sunlit)
    golden = 2.399963229728653
    for i in range(n_leaves):
        frac = i / float(max(1, n_leaves - 1))
        # Radial distribution: dense cluster core + irregular billowing outer lobes
        rad = math.sqrt(frac) * 0.76 * float(rng.uniform(0.84, 1.06))
        ang = i * golden + float(rng.uniform(-0.28, 0.28))
        # Organic 3-lobe cluster contour
        lobe_scale = 0.90 + 0.14 * math.sin(ang * 3.0 + seed * 0.13) + 0.08 * math.cos(ang * 5.0)
        lx = math.cos(ang) * rad * lobe_scale
        ly = math.sin(ang) * rad * lobe_scale
        rot = ang + float(rng.uniform(-0.65, 0.65))
        cr, sr = math.cos(rot), math.sin(rot)
        l_size = float(rng.uniform(0.11, 0.18)) * (1.12 - 0.28 * frac)

        # Bounding box in pixel space for fast vectorized stamping
        px_c = (lx * 0.5 + 0.5) * (size - 1)
        py_c = (ly * 0.5 * vertical_stretch + 0.5) * (size - 1)
        pad_px = int(math.ceil(l_size * size * 0.85)) + 2
        x0 = max(0, int(px_c) - pad_px)
        x1 = min(size, int(px_c) + pad_px + 1)
        y0 = max(0, int(py_c) - pad_px)
        y1 = min(size, int(py_c) + pad_px + 1)
        if x0 >= x1 or y0 >= y1:
            continue

        dx = cx_grid[y0:y1, x0:x1] - lx
        dy = cy_grid[y0:y1, x0:x1] - ly
        rx = (dx * cr + dy * sr) / l_size
        ry = (-dx * sr + dy * cr) / l_size
        r_loc = np.hypot(rx, ry)
        th_loc = np.arctan2(ry, rx)

        if leaf_shape == "maple":
            # Crisp 5-pointed palmate maple leaf silhouette
            boundary = 0.72 + 0.30 * np.cos(th_loc * 5.0) + 0.12 * np.cos(th_loc * 10.0)
            inside = r_loc < boundary
            vein = np.exp(-np.abs(ry) * 9.0) * 0.14 + np.exp(-np.abs(rx) * 7.0) * 0.08
        elif leaf_shape == "poplar":
            # Pointed deltoid/heart aspen-poplar leaf
            taper = np.clip(1.0 - 0.42 * np.maximum(0.0, rx), 0.35, 1.2)
            inside = (rx * rx) * 0.88 + (ry / taper) ** 2 < 0.78
            vein = np.exp(-np.abs(ry) * 12.0) * 0.12
        else:
            # Serrated broad shrub/bush leaf
            boundary = 0.86 + 0.08 * np.sin(th_loc * 9.0)
            inside = ((rx * 0.85) ** 2 + (ry * 1.15) ** 2) < (boundary ** 2)
            vein = np.exp(-np.abs(ry) * 11.0) * 0.10

        z_val = 0.15 + 0.85 * frac + float(rng.uniform(-0.04, 0.04))
        write_mask = inside & (z_val >= depth_buf[y0:y1, x0:x1])
        if not np.any(write_mask):
            continue

        # Leaf shading: outer & upper leaves brighter sunlit, inner darker shadowed, subtle vein highlight
        sun_t = float(np.clip(0.20 + 0.65 * frac + 0.18 * (-ly) + rng.uniform(-0.14, 0.14), 0.0, 1.0))
        if sun_t < 0.5:
            s = sun_t * 2.0
            base_col = tuple(dark_rgb[c] * (1.0 - s) + mid_rgb[c] * s for c in range(3))
        else:
            s = (sun_t - 0.5) * 2.0
            base_col = tuple(mid_rgb[c] * (1.0 - s) + tip_rgb[c] * s for c in range(3))

        edge_lit = np.clip(0.86 + 0.24 * r_loc - vein, 0.68, 1.18)
        m_sub = micro[y0:y1, x0:x1]
        shade = edge_lit * (0.90 + 0.20 * m_sub)
        for ch in range(3):
            plane = rgb_buf[y0:y1, x0:x1, ch]
            plane[write_mask] = base_col[ch] * shade[write_mask]
        alpha_buf[y0:y1, x0:x1][write_mask] = 255.0
        depth_buf[y0:y1, x0:x1][write_mask] = z_val

    rgba = np.empty((size, size, 4), dtype=np.uint8)
    rgba[:, :, :3] = np.clip(np.rint(rgb_buf), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 3] = np.clip(np.rint(alpha_buf), 0.0, 255.0).astype(np.uint8)
    return dilate_alpha_edge_rgb(rgba, iterations=10, alpha_thresh=32)


def generate_botanical_cedar_bough_rgba(size: int = 512) -> np.ndarray:
    """Generate a crisp 512x512 Pacific NW Western Red Cedar drooping pinnate frond atlas."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    u = xx / float(size - 1)
    v = yy / float(size - 1)
    dx = u - 0.5

    # Central woody stem + pinnate outward-drooping flat cedar scale-needle sprays
    n1 = np.sin(u * 41.0 + np.cos(v * 33.0) * 1.5) * np.cos(v * 37.0 - np.sin(u * 29.0))
    n2 = np.sin(u * 107.0 - v * 89.0 + n1 * 2.1) * np.cos(u * 93.0 + v * 113.0)
    n3 = np.sin(u * 239.0 + v * 211.0 + n2 * 1.4)

    # Drooping chevron coordinate (needles fan outward and sweep gracefully downward)
    droop_v = v - np.abs(dx) * 0.52
    env_w = np.clip(
        0.08 + 0.41 * np.power(np.sin(math.pi * np.clip(v * 0.90 + 0.04, 0.0, 1.0)), 0.68),
        0.05,
        0.48,
    )
    rad_norm = np.abs(dx) / np.maximum(env_w, 1e-4)

    # Fine pinnate sub-fronds along drooping diagonal ribs
    ribs = 0.5 + 0.32 * np.sin((droop_v * 14.0) * math.pi + n1 * 1.6) + 0.18 * np.cos((droop_v * 29.0 + np.abs(dx) * 12.0) * math.pi)
    needles = 0.5 + 0.28 * np.sin((dx * 58.0 + droop_v * 42.0) * math.pi + n2 * 1.8) + 0.16 * n3
    density = (1.06 - rad_norm) + 0.28 * (ribs - 0.5) + 0.22 * (needles - 0.5) + 0.16 * n1
    v_fade = np.clip((v - 0.02) / 0.06, 0.0, 1.0) * np.clip((0.98 - v) / 0.06, 0.0, 1.0)
    field = density * v_fade

    # Solid heart for inner core mapping + feathery conifer tips
    heart = (np.hypot(dx * 2.0, (v - 0.5) * 2.0) < 0.44)
    alpha = np.where((field > 0.34) | heart, 255.0, 0.0)

    # Woody center branchlet hint near dx ~ 0
    stem_mask = (np.abs(dx) < 0.024 * (1.2 - 0.7 * v)) & (v > 0.12) & (v < 0.92)
    outer_lit = np.clip(0.50 * rad_norm + 0.30 * (1.0 - v) + 0.30 * (needles - 0.4), 0.0, 1.0)
    ao = np.clip(0.54 + 0.46 * ribs + 0.18 * n2, 0.30, 1.28)
    r = np.where(stem_mask, 58.0, (14.0 + 38.0 * outer_lit + 8.0 * n3) * ao)
    g = np.where(stem_mask, 42.0, (28.0 + 54.0 * outer_lit + 10.0 * n1) * ao)
    b = np.where(stem_mask, 28.0, (16.0 + 22.0 * outer_lit + 6.0 * n2) * ao)

    rgba = np.empty((size, size, 4), dtype=np.uint8)
    rgba[:, :, 0] = np.clip(np.rint(r), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 1] = np.clip(np.rint(g), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 2] = np.clip(np.rint(b), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 3] = np.clip(np.rint(alpha), 0.0, 255.0).astype(np.uint8)
    return dilate_alpha_edge_rgb(rgba, iterations=10, alpha_thresh=32)


def generate_botanical_hedge_rgba(size: int = 512) -> np.ndarray:
    """Generate a horizontally-tileable 512x512 dense boxwood hedge texture with organic top leaf fringes.

    100% periodic along u (seams invisible at u=0,1,2) so both upgraded XW_HEDGESTADIUM and tiled
    park hedges stay lush and solid across v=0.14..1.00 while v=0.00..0.14 carries crisp cutout leaf tips.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    u = xx / float(size)  # strictly periodic [0, 1)
    v = yy / float(size - 1)
    tau = math.tau

    # Periodic multi-scale boxwood foliage noise
    n1 = np.sin(u * tau * 6.0 + np.cos(v * 19.0) * 1.3) * np.cos(v * 23.0 + np.sin(u * tau * 4.0))
    n2 = np.sin(u * tau * 17.0 - v * 47.0 + n1 * 1.8) * np.cos(u * tau * 13.0 + v * 53.0)
    n3 = np.sin(u * tau * 43.0 + v * 119.0 + n2 * 1.4) * np.cos(u * tau * 37.0 - v * 97.0)
    n4 = np.sin(u * tau * 89.0 - v * 211.0 + n3 * 1.1)

    # Organic serrated top leaf-crest silhouette (~11-14% cutout matching EA's ORG_CT_HEDGELP)
    crest = (
        0.105
        + 0.032 * np.sin(u * tau * 5.0 + 0.4)
        + 0.020 * np.cos(u * tau * 14.0 - 0.8)
        + 0.015 * np.sin(u * tau * 31.0 + 1.2)
        + 0.012 * n3
    )
    alpha = np.where(v >= crest, 255.0, 0.0)

    # Dense small-leaf boxwood layering: deeper shadowed base, bright sunlit crown tips
    height_lit = np.clip(1.0 - v * 0.72, 0.25, 1.0)
    leaf_cell = np.clip(0.55 + 0.28 * n2 + 0.22 * n3 + 0.14 * n4, 0.22, 1.32)
    r = (18.0 + 36.0 * height_lit + 14.0 * np.maximum(0.0, n3)) * leaf_cell
    g = (36.0 + 56.0 * height_lit + 18.0 * np.maximum(0.0, n2)) * leaf_cell
    b = (16.0 + 20.0 * height_lit + 8.0 * np.maximum(0.0, n4)) * leaf_cell

    rgba = np.empty((size, size, 4), dtype=np.uint8)
    rgba[:, :, 0] = np.clip(np.rint(r), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 1] = np.clip(np.rint(g), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 2] = np.clip(np.rint(b), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 3] = np.clip(np.rint(alpha), 0.0, 255.0).astype(np.uint8)
    return dilate_alpha_edge_rgb(rgba, iterations=10, alpha_thresh=32)


def generate_botanical_beachgrass_rgba(size: int = 512) -> np.ndarray:
    """Generate a crisp 512x512 windswept coastal dune grass blade atlas (~50-56% open-air cutout)."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    u = xx / float(size - 1)
    v = yy / float(size - 1)  # 0 = tip, 1 = rooted base
    h_frac = 1.0 - v          # 0 at root, 1 at top

    rng = np.random.default_rng(7919)
    rgb_buf = np.zeros((size, size, 3), dtype=np.float32)
    alpha_buf = np.zeros((size, size), dtype=np.float32)
    depth_buf = np.full((size, size), -1.0, dtype=np.float32)

    # 44 gracefully arching coastal dune grass blades leaning with ocean wind
    n_blades = 44
    for i in range(n_blades):
        frac = i / float(max(1, n_blades - 1))
        root_u = float(0.08 + 0.84 * (i + 0.5) / n_blades + rng.uniform(-0.025, 0.025))
        blade_h = float(rng.uniform(0.62, 0.96))
        wind_bend = float(rng.uniform(-0.10, 0.24))  # predominant coastal breeze lean
        half_w_base = float(rng.uniform(0.011, 0.020))
        t_along = h_frac / max(blade_h, 0.1)
        center_u = root_u + wind_bend * (t_along ** 1.75)
        half_w = half_w_base * np.clip(1.0 - 0.88 * (t_along ** 1.2), 0.04, 1.0)
        inside = (t_along >= 0.0) & (t_along <= 1.0) & (np.abs(u - center_u) <= half_w)
        z_val = 0.1 + 0.9 * frac
        write_mask = inside & (z_val >= depth_buf)
        if not np.any(write_mask):
            continue

        # Dry coastal wheat-straw blended with sea-sage green (darker at roots, golden sunlit tips)
        is_dry = (i % 3) != 0
        root_c = (44.0, 50.0, 24.0) if is_dry else (32.0, 52.0, 26.0)
        tip_c = (162.0, 142.0, 74.0) if is_dry else (104.0, 118.0, 56.0)
        t_c = np.clip(t_along, 0.0, 1.0)
        ribbon = 0.88 + 0.22 * (1.0 - np.abs(u - center_u) / np.maximum(half_w, 1e-4))
        for ch in range(3):
            col_v = (root_c[ch] * (1.0 - t_c) + tip_c[ch] * t_c) * ribbon
            rgb_buf[:, :, ch][write_mask] = col_v[write_mask]
        alpha_buf[write_mask] = 255.0
        depth_buf[write_mask] = z_val

    rgba = np.empty((size, size, 4), dtype=np.uint8)
    rgba[:, :, :3] = np.clip(np.rint(rgb_buf), 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 3] = np.clip(np.rint(alpha_buf), 0.0, 255.0).astype(np.uint8)
    return dilate_alpha_edge_rgb(rgba, iterations=10, alpha_thresh=32)


def generate_botanical_foliage_texture_rgba(tex_hash: int, size: int = 512) -> Optional[Tuple[str, str, np.ndarray]]:
    """Generate procedural 512x512 alpha-cutout RGBA texture for any of the 6 v1.4.0 foliage hashes."""
    if tex_hash == BOTANICAL_TEX_MAPLE_CRIMSON:
        return (
            "ORG_CT_FALLTREE2",
            "procedural:botanical_autumn_maple_crimson_512",
            _render_leaf_spray_cluster_rgba(
                size, seed=1401, n_leaves=118,
                dark_rgb=(66.0, 17.0, 12.0), mid_rgb=(156.0, 38.0, 20.0), tip_rgb=(210.0, 96.0, 28.0),
                leaf_shape="maple",
            ),
        )
    if tex_hash == BOTANICAL_TEX_FALL_AMBER:
        return (
            "ORG_CT_FALLTREETOP1",
            "procedural:botanical_autumn_maple_amber_512",
            _render_leaf_spray_cluster_rgba(
                size, seed=1402, n_leaves=122,
                dark_rgb=(68.0, 32.0, 14.0), mid_rgb=(168.0, 86.0, 22.0), tip_rgb=(216.0, 146.0, 36.0),
                leaf_shape="maple",
            ),
        )
    if tex_hash == BOTANICAL_TEX_POPLAR_GOLD:
        return (
            "ORG_CT_POPLARFLORA",
            "procedural:botanical_golden_poplar_512",
            _render_leaf_spray_cluster_rgba(
                size, seed=1403, n_leaves=132,
                dark_rgb=(62.0, 50.0, 18.0), mid_rgb=(166.0, 128.0, 30.0), tip_rgb=(222.0, 184.0, 54.0),
                leaf_shape="poplar", vertical_stretch=1.12,
            ),
        )
    if tex_hash == BOTANICAL_TEX_CEDAR_BOUGH:
        return (
            "ORG_CEDAR_JG",
            "procedural:botanical_pnw_cedar_512",
            generate_botanical_cedar_bough_rgba(size),
        )
    if tex_hash == BOTANICAL_TEX_ROADSIDE_BUSH:
        return (
            "ORG_CT_BUSH1",
            "procedural:botanical_roadside_bush_512",
            _render_leaf_spray_cluster_rgba(
                size, seed=1405, n_leaves=136,
                dark_rgb=(36.0, 46.0, 20.0), mid_rgb=(92.0, 98.0, 42.0), tip_rgb=(140.0, 134.0, 56.0),
                leaf_shape="shrub",
            ),
        )
    if tex_hash == BOTANICAL_TEX_FOREST_BUSH:
        return (
            "ORG_BUSH_H_JG",
            "procedural:botanical_forest_bush_512",
            _render_leaf_spray_cluster_rgba(
                size, seed=1406, n_leaves=136,
                dark_rgb=(18.0, 34.0, 18.0), mid_rgb=(38.0, 68.0, 32.0), tip_rgb=(68.0, 100.0, 44.0),
                leaf_shape="shrub",
            ),
        )
    if tex_hash == BOTANICAL_TEX_STADIUM_HEDGE:
        return (
            "ORG_CT_HEDGELP",
            "procedural:botanical_stadium_hedge_512",
            generate_botanical_hedge_rgba(size),
        )
    if tex_hash == BOTANICAL_TEX_BEACHGRASS:
        return (
            "ORG_SCRUBBRUSH_D",
            "procedural:botanical_coastal_beachgrass_512",
            generate_botanical_beachgrass_rgba(size),
        )
    return None


def _append_tapered_trunk_tube(
    x0: float, y0: float, z0: float, r0: float,
    x1: float, y1: float, z1: float, r1: float,
    segs: int, rings: int, trunk_slot: int,
    pos_list: List[Tuple[float, float, float]],
    norm_list: List[Tuple[float, float, float]],
    uv_list: List[Tuple[float, float]],
    ao_list: List[int],
    slot_list: List[int],
    idx_list: List[int],
) -> None:
    """Append a tapered organic woody trunk/branch segment."""
    dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
    ax_len = max(1e-4, math.sqrt(dx * dx + dy * dy + dz * dz))
    ux, uy, uz = dx / ax_len, dy / ax_len, dz / ax_len
    # Orthonormal basis around branch axis
    ref = (1.0, 0.0, 0.0) if abs(uy) > 0.85 else (0.0, 1.0, 0.0)
    bx = uy * ref[2] - uz * ref[1]
    by = uz * ref[0] - ux * ref[2]
    bz = ux * ref[1] - uy * ref[0]
    bm = max(1e-5, math.sqrt(bx * bx + by * by + bz * bz))
    bx, by, bz = bx / bm, by / bm, bz / bm
    cx = uy * bz - uz * by
    cy = uz * bx - ux * bz
    cz = ux * by - uy * bx

    base = len(pos_list)
    for ri in range(rings + 1):
        t = ri / float(rings)
        px = x0 + dx * t
        py = y0 + dy * t
        pz = z0 + dz * t
        rad = r0 * (1.0 - t) + r1 * t
        ao = int(118 + 42 * (1.0 - t))
        for si in range(segs + 1):
            ang = (si / float(segs)) * math.tau
            ca, sa = math.cos(ang), math.sin(ang)
            nx = bx * ca + cx * sa
            ny = by * ca + cy * sa
            nz = bz * ca + cz * sa
            pos_list.append((px + nx * rad, py + ny * rad, pz + nz * rad))
            norm_list.append((nx, ny, nz))
            uv_list.append((si / float(segs) * 2.0, t * 2.5))
            ao_list.append(ao)
            slot_list.append(trunk_slot)
    stride = segs + 1
    for ri in range(rings):
        for si in range(segs):
            a = base + ri * stride + si
            b = a + 1
            c = a + stride
            d = c + 1
            idx_list.extend((a, b, c, b, d, c))


def _append_organic_foliage_core(
    cx: float, cy: float, cz: float,
    rx: float, ry: float, rz: float,
    rings: int, segs: int, seed: int, foliage_slot: int,
    pos_list: List[Tuple[float, float, float]],
    norm_list: List[Tuple[float, float, float]],
    uv_list: List[Tuple[float, float]],
    ao_list: List[int],
    slot_list: List[int],
    idx_list: List[int],
) -> None:
    """Append a sculpted multi-lobe organic inner foliage core mapped to the opaque atlas heart."""
    rng = np.random.default_rng(seed)
    p1 = float(rng.uniform(0.0, math.tau))
    p2 = float(rng.uniform(0.0, math.tau))
    base = len(pos_list)
    for ri in range(rings + 1):
        v_t = ri / float(rings)
        phi = v_t * math.pi  # 0 = top pole, pi = bottom pole
        sp = math.sin(phi)
        cp = math.cos(phi)
        ao = int(94 + 86 * (0.5 + 0.5 * cp))
        for si in range(segs + 1):
            u_t = si / float(segs)
            th = u_t * math.tau
            ct, st = math.cos(th), math.sin(th)
            lobe = 1.0 + 0.14 * math.sin(th * 3.0 + phi * 2.5 + p1) + 0.09 * math.cos(th * 2.0 - phi * 3.0 + p2)
            px = cx + rx * sp * ct * lobe
            py = cy + ry * cp * (0.94 + 0.06 * lobe)
            pz = cz + rz * sp * st * lobe
            nx, ny, nz = sp * ct * 0.85, max(-0.2, cp * 0.72 + 0.28), sp * st * 0.85
            nm = max(1e-5, math.sqrt(nx * nx + ny * ny + nz * nz))
            pos_list.append((px, py, pz))
            norm_list.append((nx / nm, ny / nm, nz / nm))
            # Map strictly inside opaque heart [0.32..0.68] so zero sky gaps pierce the core
            u_c = 0.34 + 0.32 * (0.5 + 0.5 * math.sin(th * 2.0))
            v_c = 0.34 + 0.32 * v_t
            uv_list.append((u_c, v_c))
            ao_list.append(ao)
            slot_list.append(foliage_slot)
    stride = segs + 1
    for ri in range(rings):
        for si in range(segs):
            a = base + ri * stride + si
            b = a + 1
            c = a + stride
            d = c + 1
            idx_list.extend((a, b, c, b, d, c))


def _append_cupped_leaf_card(
    center: Tuple[float, float, float],
    out_dir: Tuple[float, float, float],
    half_w: float,
    half_h: float,
    cup_depth: float,
    tilt_rad: float,
    ao_lo: int,
    ao_hi: int,
    slot: int,
    pos_list: List[Tuple[float, float, float]],
    norm_list: List[Tuple[float, float, float]],
    uv_list: List[Tuple[float, float]],
    ao_list: List[int],
    slot_list: List[int],
    idx_list: List[int],
    uv_inset: float = 0.02,
) -> None:
    """Append one curved 6-vertex (4-triangle) billowing leaf-cluster card facing `out_dir`."""
    ox, oy, oz = out_dir
    om = max(1e-5, math.sqrt(ox * ox + oy * oy + oz * oz))
    nx, ny, nz = ox / om, oy / om, oz / om
    ref = (1.0, 0.0, 0.0) if abs(ny) > 0.90 else (0.0, 1.0, 0.0)
    rx = ref[1] * nz - ref[2] * ny
    ry = ref[2] * nx - ref[0] * nz
    rz = ref[0] * ny - ref[1] * nx
    rm = max(1e-5, math.sqrt(rx * rx + ry * ry + rz * rz))
    rx, ry, rz = rx / rm, ry / rm, rz / rm
    ux = ny * rz - nz * ry
    uy = nz * rx - nx * rz
    uz = nx * ry - ny * rx

    # Apply organic in-plane roll
    ct, st = math.cos(tilt_rad), math.sin(tilt_rad)
    r2x, r2y, r2z = rx * ct + ux * st, ry * ct + uy * st, rz * ct + uz * st
    u2x, u2y, u2z = -rx * st + ux * ct, -ry * st + uy * ct, -rz * st + uz * ct

    cx, cy, cz = center
    # 3 rows (bottom, cupped middle belly, top) x 2 cols
    rows = (
        (-half_h, 0.85 * half_w, -cup_depth * 0.4, 1.0 - uv_inset, ao_lo),
        (0.0, half_w, cup_depth, 0.5, (ao_lo + ao_hi) // 2),
        (half_h, 0.85 * half_w, -cup_depth * 0.25, uv_inset, ao_hi),
    )
    # Hemisphere-biased sunlit foliage normal
    snx, sny, snz = nx * 0.85, max(0.18, ny * 0.75 + 0.35), nz * 0.85
    snm = max(1e-5, math.sqrt(snx * snx + sny * sny + snz * snz))
    sn = (snx / snm, sny / snm, snz / snm)

    v0 = len(pos_list)
    for dy_v, hw_v, bulge, v_uv, ao_v in rows:
        for side, u_uv in ((-1.0, uv_inset), (1.0, 1.0 - uv_inset)):
            px = cx + r2x * (side * hw_v) + u2x * dy_v + nx * bulge
            py = cy + r2y * (side * hw_v) + u2y * dy_v + ny * bulge
            pz = cz + r2z * (side * hw_v) + u2z * dy_v + nz * bulge
            pos_list.append((px, py, pz))
            norm_list.append(sn)
            uv_list.append((u_uv, v_uv))
            ao_list.append(ao_v)
            slot_list.append(slot)
    idx_list.extend((v0, v0 + 1, v0 + 2, v0 + 1, v0 + 3, v0 + 2))
    idx_list.extend((v0 + 2, v0 + 3, v0 + 4, v0 + 3, v0 + 5, v0 + 4))


def build_botanical_foliage_solid(orig: DecodedWorldSolid, archetype: str) -> DecodedWorldSolid:
    """Upgrade one of EA's 6 blocky tree/bush/hedge/grass archetypes into a sculpted multi-tier botanical BLAS.

    Matches `orig`'s real Metal bounding box & center so nothing blocks roads or clips buildings,
    while eliminating flat horizontal spoke rings and low-poly billboard boxes.
    """
    up = orig.name.upper()
    tex_hashes = list(orig.texture_hashes)

    def _slot_for(th: int, fallback: int = 0) -> int:
        if th in tex_hashes:
            return tex_hashes.index(th)
        return fallback

    xmin, ymin, zmin = orig.aabb_min_metal
    xmax, ymax, zmax = orig.aabb_max_metal
    cx = 0.5 * (xmin + xmax)
    cz = 0.5 * (zmin + zmax)
    hx = max(0.35, 0.5 * (xmax - xmin))
    hz = max(0.35, 0.5 * (zmax - zmin))
    h_tot = max(0.8, ymax - ymin)

    pos_list: List[Tuple[float, float, float]] = []
    norm_list: List[Tuple[float, float, float]] = []
    uv_list: List[Tuple[float, float]] = []
    ao_list: List[int] = []
    slot_list: List[int] = []
    idx_list: List[int] = []

    seed = int(orig.name_hash & 0xFFFF) + 2026
    rng = np.random.default_rng(seed)
    golden = 2.399963229728653

    if archetype == "autumn_maple":
        is_filler = "FILLER" in up
        if BOTANICAL_TEX_MAPLE_CRIMSON in tex_hashes:
            f_slot_a = _slot_for(BOTANICAL_TEX_MAPLE_CRIMSON)
            f_slot_b = f_slot_a
        else:
            f_slot_a = _slot_for(BOTANICAL_TEX_FALL_AMBER)
            f_slot_b = _slot_for(BOTANICAL_TEX_POPLAR_GOLD, f_slot_a)

        if not is_filler:
            # Find trunk slot (first texture slot that isn't foliage)
            t_slot = next(
                (i for i, th in enumerate(tex_hashes) if th not in (BOTANICAL_TEX_MAPLE_CRIMSON, BOTANICAL_TEX_FALL_AMBER, BOTANICAL_TEX_POPLAR_GOLD)),
                0,
            )
            trunk_r0 = min(0.55, max(0.24, 0.045 * min(hx, hz)))
            trunk_top_y = ymin + h_tot * 0.58
            _append_tapered_trunk_tube(
                cx, ymin, cz, trunk_r0, cx + 0.15, trunk_top_y, cz - 0.10, trunk_r0 * 0.42,
                segs=6, rings=3, trunk_slot=t_slot,
                pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
                ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
            )
            # 3 organic forked primary boughs spreading into the crown
            for bi in range(3):
                ba = bi * (math.tau / 3.0) + 0.45
                bx1 = cx + math.cos(ba) * hx * 0.42
                bz1 = cz + math.sin(ba) * hz * 0.42
                by0 = ymin + h_tot * (0.34 + 0.06 * bi)
                by1 = ymin + h_tot * (0.64 + 0.05 * bi)
                _append_tapered_trunk_tube(
                    cx, by0, cz, trunk_r0 * 0.48, bx1, by1, bz1, trunk_r0 * 0.18,
                    segs=5, rings=2, trunk_slot=t_slot,
                    pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
                    ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
                )

        # Multi-lobe billowing deciduous canopy cores + 42 overlapping cupped leaf-cluster cards
        canopy_y0 = ymin + (h_tot * 0.08 if is_filler else h_tot * 0.26)
        canopy_y1 = ymax - h_tot * 0.02
        canopy_h = max(1.0, canopy_y1 - canopy_y0)
        canopy_cy = 0.5 * (canopy_y0 + canopy_y1)
        # Main core + 2 offset sub-lobes for natural asymmetric deciduous silhouette
        lobes = (
            (cx, canopy_cy + canopy_h * 0.05, cz, hx * 0.58, canopy_h * 0.38, hz * 0.58, f_slot_a),
            (cx + hx * 0.22, canopy_cy - canopy_h * 0.06, cz + hz * 0.16, hx * 0.42, canopy_h * 0.30, hz * 0.42, f_slot_b),
            (cx - hx * 0.20, canopy_cy + canopy_h * 0.02, cz - hz * 0.18, hx * 0.40, canopy_h * 0.30, hz * 0.40, f_slot_a),
        )
        for li, (lx, ly, lz, lrx, lry, lrz, lslot) in enumerate(lobes):
            _append_organic_foliage_core(
                lx, ly, lz, lrx, lry, lrz, rings=4, segs=6, seed=seed + li * 37, foliage_slot=lslot,
                pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
                ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
            )

        n_cards = 40 if is_filler else 46
        for ci in range(n_cards):
            u_f = (ci + 0.5) / float(n_cards)
            # Fibonacci sphere distribution biased slightly upward for lush crown dome
            cos_p = 1.0 - 1.72 * u_f
            sin_p = math.sqrt(max(0.0, 1.0 - cos_p * cos_p))
            az = ci * golden + float(rng.uniform(-0.18, 0.18))
            dx_d, dy_d, dz_d = sin_p * math.cos(az), cos_p, sin_p * math.sin(az)
            rad_j = float(rng.uniform(0.68, 0.84))
            px = cx + dx_d * hx * rad_j
            py = canopy_cy + dy_d * (canopy_h * 0.44) * rad_j
            pz = cz + dz_d * hz * rad_j
            cw = max(1.1, 0.36 * math.hypot(hx, hz) * float(rng.uniform(0.82, 1.15)))
            ch = max(1.1, 0.36 * canopy_h * float(rng.uniform(0.80, 1.14)))
            slot_c = f_slot_b if (ci % 3 == 0) else f_slot_a
            ao_lo = int(112 + 48 * (0.5 + 0.5 * dy_d))
            ao_hi = int(205 + 45 * (0.5 + 0.5 * dy_d))
            _append_cupped_leaf_card(
                (px, py, pz), (dx_d, dy_d + 0.25, dz_d), cw, ch, min(cw, ch) * 0.24,
                float(rng.uniform(-0.55, 0.55)), ao_lo, ao_hi, slot_c,
                pos_list, norm_list, uv_list, ao_list, slot_list, idx_list,
            )

    elif archetype == "golden_poplar":
        f_slot = _slot_for(BOTANICAL_TEX_POPLAR_GOLD, 0)
        t_slot = next((i for i, th in enumerate(tex_hashes) if th != BOTANICAL_TEX_POPLAR_GOLD), 0)
        trunk_r0 = min(0.42, max(0.20, 0.055 * min(hx, hz)))
        _append_tapered_trunk_tube(
            cx, ymin, cz, trunk_r0, cx, ymin + h_tot * 0.68, cz, trunk_r0 * 0.28,
            segs=6, rings=3, trunk_slot=t_slot,
            pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
            ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
        )
        # Tall tapered columnar-ovate organic inner poplar core
        core_y0 = ymin + h_tot * 0.16
        core_y1 = ymax - h_tot * 0.04
        core_cy = 0.5 * (core_y0 + core_y1)
        core_ry = 0.5 * (core_y1 - core_y0)
        _append_organic_foliage_core(
            cx, core_cy, cz, hx * 0.56, core_ry, hz * 0.56, rings=6, segs=7, seed=seed + 19, foliage_slot=f_slot,
            pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
            ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
        )
        # 42 steep upward-swept columnar golden poplar plume clusters (zero flat horizontal rings!)
        n_cards = 42
        for ci in range(n_cards):
            t_h = (ci + 0.5) / float(n_cards)
            env = math.sin(math.pi * (0.10 + 0.85 * (t_h ** 0.78))) ** 0.72 * (1.0 - 0.30 * t_h)
            az = ci * golden + float(rng.uniform(-0.20, 0.20))
            dx_d, dz_d = math.cos(az), math.sin(az)
            rad_s = max(0.18, env * float(rng.uniform(0.72, 0.86)))
            px = cx + dx_d * hx * rad_s
            py = core_y0 + t_h * (core_y1 - core_y0)
            pz = cz + dz_d * hz * rad_s
            cw = max(0.85, hx * (0.44 * env + 0.16) * float(rng.uniform(0.88, 1.12)))
            ch = max(1.3, h_tot * 0.22 * (1.05 - 0.30 * t_h))
            _append_cupped_leaf_card(
                (px, py, pz), (dx_d * 0.90, 0.44, dz_d * 0.90), cw, ch, cw * 0.22,
                float(rng.uniform(-0.22, 0.22)), int(120 + 45 * t_h), int(218 + 32 * t_h), f_slot,
                pos_list, norm_list, uv_list, ao_list, slot_list, idx_list,
            )

    elif archetype == "pnw_cedar":
        f_slot = _slot_for(BOTANICAL_TEX_CEDAR_BOUGH, 0)
        t_slot = next((i for i, th in enumerate(tex_hashes) if th != BOTANICAL_TEX_CEDAR_BOUGH), 0)
        trunk_r0 = min(0.68, max(0.28, 0.075 * min(hx, hz)))
        _append_tapered_trunk_tube(
            cx, ymin, cz, trunk_r0, cx, ymin + h_tot * 0.88, cz, trunk_r0 * 0.15,
            segs=6, rings=4, trunk_slot=t_slot,
            pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
            ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
        )
        # Tall conical sculpted Pacific NW cedar core (organic flutes, no smooth cone!)
        core_y0 = ymin + h_tot * 0.08
        core_y1 = ymax - h_tot * 0.03
        core_rings = 8
        core_segs = 7
        c_base = len(pos_list)
        p1, p2 = float(rng.uniform(0, math.tau)), float(rng.uniform(0, math.tau))
        for ri in range(core_rings + 1):
            t = ri / float(core_rings)
            y = core_y0 + t * (core_y1 - core_y0)
            taper = max(0.05, (1.0 - t * 0.92) ** 0.82) * (0.75 if ri == 0 else 1.0)
            ao = int(88 + 92 * t)
            for si in range(core_segs + 1):
                ang = (si / float(core_segs)) * math.tau
                lobe = 1.0 + 0.16 * math.sin(ang * 3.0 + t * 5.0 + p1) + 0.10 * math.cos(ang * 2.0 - t * 4.0 + p2)
                ca, sa = math.cos(ang), math.sin(ang)
                pos_list.append((cx + ca * hx * 0.56 * taper * lobe, y, cz + sa * hz * 0.56 * taper * lobe))
                norm_list.append((ca * 0.86, 0.50, sa * 0.86))
                uv_list.append((0.35 + 0.30 * (si % 2), 0.32 + 0.36 * (1.0 - t)))
                ao_list.append(ao)
                slot_list.append(f_slot)
        c_stride = core_segs + 1
        for ri in range(core_rings):
            for si in range(core_segs):
                a = c_base + ri * c_stride + si
                idx_list.extend((a, a + 1, a + c_stride, a + 1, a + c_stride + 1, a + c_stride))

        # 54 overlapping drooping evergreen cedar bough cards spiraled from skirt to spire
        n_boughs = 54
        for bi in range(n_boughs):
            t_h = (bi + 0.5) / float(n_boughs)
            taper = max(0.14, (1.0 - t_h * 0.88) ** 0.82)
            az = bi * golden + float(rng.uniform(-0.22, 0.22))
            dx_d, dz_d = math.cos(az), math.sin(az)
            r_reach = taper * float(rng.uniform(0.68, 0.86))
            px = cx + dx_d * hx * r_reach
            py = core_y0 + t_h * (core_y1 - core_y0)
            pz = cz + dz_d * hz * r_reach
            bw = max(1.0, hx * 0.46 * taper * float(rng.uniform(0.86, 1.14)))
            bh = max(1.6, h_tot * 0.125 * (1.05 - 0.32 * t_h))
            # Steep outward-and-downward bough droop pitch (~42-62 deg from horizontal, NEVER flat pancake rings!)
            _append_cupped_leaf_card(
                (px, py, pz), (dx_d * 0.84, 0.54, dz_d * 0.84), bw, bh, bw * 0.28,
                float(rng.uniform(-0.24, 0.24)), int(102 + 48 * t_h), int(208 + 38 * t_h), f_slot,
                pos_list, norm_list, uv_list, ao_list, slot_list, idx_list,
            )

    elif archetype == "roadside_bush":
        f_slot = 0
        # Multi-lobe rounded natural shrub core + 30 billowing overlapping leafy cluster cards
        bush_cy = ymin + h_tot * 0.44
        bush_ry = h_tot * 0.38
        sub_lobes = (
            (cx, bush_cy, cz, hx * 0.56, bush_ry * 0.82, hz * 0.56),
            (cx + hx * 0.22, bush_cy - h_tot * 0.06, cz + hz * 0.14, hx * 0.40, bush_ry * 0.68, hz * 0.40),
            (cx - hx * 0.20, bush_cy - h_tot * 0.05, cz - hz * 0.16, hx * 0.40, bush_ry * 0.68, hz * 0.40),
        )
        for li, (lx, ly, lz, lrx, lry, lrz) in enumerate(sub_lobes):
            _append_organic_foliage_core(
                lx, ly, lz, lrx, lry, lrz, rings=4, segs=6, seed=seed + li * 43, foliage_slot=f_slot,
                pos_list=pos_list, norm_list=norm_list, uv_list=uv_list,
                ao_list=ao_list, slot_list=slot_list, idx_list=idx_list,
            )
        n_cards = 30
        for ci in range(n_cards):
            u_f = (ci + 0.5) / float(n_cards)
            # Upper hemisphere + grounded skirt
            cos_p = 1.0 - 1.38 * u_f
            sin_p = math.sqrt(max(0.0, 1.0 - cos_p * cos_p))
            az = ci * golden + float(rng.uniform(-0.22, 0.22))
            dx_d, dy_d, dz_d = sin_p * math.cos(az), cos_p, sin_p * math.sin(az)
            rad_j = float(rng.uniform(0.64, 0.82))
            px = cx + dx_d * hx * rad_j
            py = max(ymin + h_tot * 0.18, bush_cy + dy_d * bush_ry * rad_j)
            pz = cz + dz_d * hz * rad_j
            cw = max(0.35, 0.38 * math.hypot(hx, hz) * float(rng.uniform(0.84, 1.14)))
            ch = max(0.35, 0.36 * h_tot * float(rng.uniform(0.84, 1.14)))
            _append_cupped_leaf_card(
                (px, py, pz), (dx_d, max(0.12, dy_d + 0.30), dz_d), cw, ch, min(cw, ch) * 0.24,
                float(rng.uniform(-0.55, 0.55)), int(108 + 44 * max(0.0, dy_d)), int(212 + 38 * max(0.0, dy_d)), f_slot,
                pos_list, norm_list, uv_list, ao_list, slot_list, idx_list,
            )

    elif archetype == "stadium_hedge":
        hedge_slot = _slot_for(BOTANICAL_TEX_STADIUM_HEDGE, 0)
        curb_slot = next((i for i, th in enumerate(tex_hashes) if th != BOTANICAL_TEX_STADIUM_HEDGE), hedge_slot)
        curb_h = min(0.82, h_tot * 0.32)

        # 1. Clean concrete curb/planter base (exact original footprint [xmin..xmax] x [ymin..ymin+curb_h] x [zmin..zmax])
        def _append_box(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float, sl: int) -> None:
            faces = (
                ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0.0, 0.0, 1.0), (0.0, 1.0), (2.0, 1.0), (2.0, 0.0), (0.0, 0.0)),
                ((x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (0.0, 0.0, -1.0), (0.0, 1.0), (2.0, 1.0), (2.0, 0.0), (0.0, 0.0)),
                ((x0, y1, z1), (x1, y1, z1), (x1, y1, z0), (x0, y1, z0), (0.0, 1.0, 0.0), (0.0, 1.0), (2.0, 1.0), (2.0, 0.0), (0.0, 0.0)),
                ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (-1.0, 0.0, 0.0), (0.0, 1.0), (0.5, 1.0), (0.5, 0.0), (0.0, 0.0)),
                ((x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (1.0, 0.0, 0.0), (0.0, 1.0), (0.5, 1.0), (0.5, 0.0), (0.0, 0.0)),
            )
            for p0, p1, p2, p3, nm, uv0, uv1, uv2, uv3 in faces:
                v0 = len(pos_list)
                for pt, uv_c, ao_c in zip((p0, p1, p2, p3), (uv0, uv1, uv2, uv3), (155, 155, 205, 205)):
                    pos_list.append(pt)
                    norm_list.append(nm)
                    uv_list.append(uv_c)
                    ao_list.append(ao_c)
                    slot_list.append(sl)
                idx_list.extend((v0, v0 + 1, v0 + 2, v0, v0 + 2, v0 + 3))

        _append_box(xmin, xmax, ymin, ymin + curb_h, zmin, zmax, curb_slot)

        # 2. Sculpted organic rounded-crown dense boxwood hedge core (8 longitudinal bays x 7 arch profile stations)
        h_y0 = ymin + curb_h * 0.85
        h_y1 = ymax - 0.14
        hz_inner = hz * 0.82
        n_bays = 8
        prof = (
            (-1.00, 0.00, -0.95, 0.10, 0.98, 128),
            (-1.04, 0.48, -0.96, 0.25, 0.56, 175),
            (-0.76, 0.90, -0.68, 0.73, 0.20, 215),
            (0.00, 1.00, 0.00, 1.00, 0.22, 238),
            (0.76, 0.90, 0.68, 0.73, 0.20, 215),
            (1.04, 0.48, 0.96, 0.25, 0.56, 175),
            (1.00, 0.00, 0.95, 0.10, 0.98, 128),
        )
        h_base = len(pos_list)
        p_cnt = len(prof)
        for bi in range(n_bays + 1):
            tx = bi / float(n_bays)
            x_v = xmin + 0.04 + tx * max(0.2, (xmax - xmin) - 0.08)
            bulge = 1.0 + 0.06 * math.sin(tx * math.tau * 3.0 + 0.5)
            h_var = 0.05 * math.cos(tx * math.tau * 4.0)
            for z_f, y_f, nz_f, ny_f, v_uv, ao_c in prof:
                pos_list.append((x_v, h_y0 + (h_y1 - h_y0) * min(1.0, max(0.0, y_f + h_var * y_f)), cz + z_f * hz_inner * bulge))
                norm_list.append((0.0, ny_f, nz_f))
                uv_list.append((tx * 2.0, max(0.16, v_uv)))  # strictly inside opaque boxwood body!
                ao_list.append(ao_c)
                slot_list.append(hedge_slot)
        for bi in range(n_bays):
            for pi in range(p_cnt - 1):
                a = h_base + bi * p_cnt + pi
                b = a + 1
                c = a + p_cnt
                d = c + 1
                idx_list.extend((a, b, c, b, d, c))

        # 3. 18 overlapping organic boxwood leaf-fringe crown cards along top & flanks (gives feathery leaf silhouette!)
        for fi in range(18):
            tx = (fi + 0.5) / 18.0
            fx = xmin + 0.25 + tx * max(0.2, (xmax - xmin) - 0.50)
            side = -1.0 if (fi % 2 == 0) else 1.0
            fz = cz + side * hz_inner * 0.72
            fy = h_y0 + (h_y1 - h_y0) * 0.78
            v0 = len(pos_list)
            hw_c = max(0.42, (xmax - xmin) / 15.0)
            hh_c = (ymax - h_y0) * 0.34
            # Slight outward fan lean so silhouette breaks up the rectangular hedge edge
            pts_c = (
                (fx - hw_c, fy - hh_c * 0.4, fz - side * 0.08),
                (fx + hw_c, fy - hh_c * 0.4, fz - side * 0.08),
                (fx - hw_c * 0.92, min(ymax, fy + hh_c), fz + side * 0.16),
                (fx + hw_c * 0.92, min(ymax, fy + hh_c), fz + side * 0.16),
            )
            u0_c = (fi * 0.19) % 1.0
            for pt, uv_c, ao_c in zip(
                pts_c,
                ((u0_c, 0.52), (u0_c + 0.35, 0.52), (u0_c, 0.01), (u0_c + 0.35, 0.01)),
                (178, 178, 245, 245),
            ):
                pos_list.append(pt)
                norm_list.append((0.0, 0.65, side * 0.76))
                uv_list.append(uv_c)
                ao_list.append(ao_c)
                slot_list.append(hedge_slot)
            idx_list.extend((v0, v0 + 1, v0 + 2, v0 + 1, v0 + 3, v0 + 2))

    elif archetype == "coastal_beachgrass":
        g_slot = 0
        # 7 windswept coastal dune grass tussocks spanning [xmin..xmax] x [zmin..zmax]
        n_tufts = 7
        for ti in range(n_tufts):
            tx = (ti + 0.5) / float(n_tufts)
            gx = xmin + 0.35 + tx * max(0.2, (xmax - xmin) - 0.70) + float(rng.uniform(-0.18, 0.18))
            gz = cz + float(rng.uniform(-hz * 0.45, hz * 0.45))
            tuft_h = h_tot * float(rng.uniform(0.76, 1.00))
            tuft_w = max(0.45, min(0.95, (xmax - xmin) / 5.5))
            # 4 curved arched blade-fan cards per tussock in crossed wind-swept azimuths
            for ci in range(4):
                ang = ci * (math.pi / 4.0) + float(rng.uniform(-0.22, 0.22))
                rx_v, rz_v = math.cos(ang), math.sin(ang)
                nx_v, nz_v = -rz_v, rx_v
                # Arch gracefully downwind (+X/+Z coastal breeze)
                bend = tuft_h * 0.24
                bx_v, bz_v = nx_v * bend, nz_v * bend
                v0 = len(pos_list)
                rows = (
                    (0.0, 0.0, 0.0, tuft_w * 0.70, 0.99, 128),
                    (tuft_h * 0.54, bx_v * 0.35, bz_v * 0.35, tuft_w * 0.95, 0.48, 198),
                    (tuft_h, bx_v, bz_v, tuft_w * 0.85, 0.02, 246),
                )
                for y_off, ox_b, oz_b, hw_r, v_uv, ao_c in rows:
                    for side, u_uv in ((-1.0, 0.04), (1.0, 0.96)):
                        pos_list.append((
                            gx + rx_v * (side * hw_r) + ox_b,
                            ymin + y_off,
                            gz + rz_v * (side * hw_r) + oz_b,
                        ))
                        norm_list.append((nx_v * 0.60, 0.80, nz_v * 0.60))
                        uv_list.append((u_uv, v_uv))
                        ao_list.append(ao_c)
                        slot_list.append(g_slot)
                idx_list.extend((v0, v0 + 1, v0 + 2, v0 + 1, v0 + 3, v0 + 2))
                idx_list.extend((v0 + 2, v0 + 3, v0 + 4, v0 + 3, v0 + 5, v0 + 4))
    else:
        return orig

    pos_arr = np.asarray(pos_list, dtype=np.float32)
    # Clamp strictly within original solid AABB so zero trees/hedges ever encroach on lanes or clip buildings
    pos_arr[:, 0] = np.clip(pos_arr[:, 0], xmin, xmax)
    pos_arr[:, 1] = np.clip(pos_arr[:, 1], ymin, ymax)
    pos_arr[:, 2] = np.clip(pos_arr[:, 2], zmin, zmax)
    bmin = pos_arr.min(axis=0)
    bmax = pos_arr.max(axis=0)
    return DecodedWorldSolid(
        name=orig.name,
        name_hash=orig.name_hash,
        texture_hashes=tex_hashes,
        positions_metal=pos_arr,
        normals_metal=np.asarray(norm_list, dtype=np.float32),
        uvs_metal=np.asarray(uv_list, dtype=np.float32),
        vertex_ao=np.asarray(ao_list, dtype=np.uint8),
        vertex_tex_slot=np.asarray(slot_list, dtype=np.int32),
        indices=np.asarray(idx_list, dtype=np.uint32),
        aabb_min_metal=(float(bmin[0]), float(bmin[1]), float(bmin[2])),
        aabb_max_metal=(float(bmax[0]), float(bmax[1]), float(bmax[2])),
    )


@dataclasses.dataclass
class DecodedRkptTexture:
    name: str
    tpk_name_hash: int
    width: int
    height: int
    rgba_bytes: bytes
    flags: int
    source: str
    pixel_format: int = PIXEL_FORMAT_RGBA8_SRGB
    mip_count: int = 1
    encoded_bytes: Optional[bytes] = None


def normalize_ps2_alpha_rgba(rgba: np.ndarray, psm: int) -> Tuple[np.ndarray, bool]:
    """Apply exact contract PS2 [0..0x80] alpha normalization: min(255, round(a * 255 / 128))."""
    out = rgba.copy()
    if psm in (te.PS2_PSMCT16, te.PS2_PSMCT16S, te.PS2_PSMCT24):
        return out, False
    a = out[:, :, 3].astype(np.float32)
    out[:, :, 3] = np.minimum(255.0, np.rint(a * (255.0 / 128.0))).astype(np.uint8)
    return out, True


def dilate_alpha_edge_rgb(
    rgba: np.ndarray,
    iterations: int = 8,
    alpha_thresh: int = 32,
) -> np.ndarray:
    """Color-dilate opaque RGB outward into transparent pixels (alpha < alpha_thresh).

    Prevents Lanczos upscaling, GPU bilinear sampling, mipmap downsampling, and BC7 4x4 block
    compression from bleeding white/gray/black junk RGB stored in transparent PS2 texels across
    foliage and fence alpha-cutout silhouettes.
    """
    alpha = rgba[:, :, 3]
    if int(alpha.min()) >= alpha_thresh or int(alpha.max()) < alpha_thresh:
        return rgba

    out = rgba.copy()
    rgb = out[:, :, :3].astype(np.float32)
    valid = alpha >= alpha_thresh

    for _ in range(iterations):
        if bool(valid.all()):
            break
        v_pad = np.pad(valid, ((1, 1), (1, 1)), mode="edge")
        r_pad = np.pad(rgb, ((1, 1), (1, 1), (0, 0)), mode="edge")
        acc = np.zeros_like(rgb)
        cnt = np.zeros(valid.shape, dtype=np.float32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                w_nb = 1.0 if (dy == 0 or dx == 0) else 0.7071
                m = v_pad[1 + dy : 1 + dy + valid.shape[0], 1 + dx : 1 + dx + valid.shape[1]]
                mf = m.astype(np.float32) * w_nb
                acc += r_pad[1 + dy : 1 + dy + valid.shape[0], 1 + dx : 1 + dx + valid.shape[1], :] * mf[:, :, None]
                cnt += mf
        fill_mask = (~valid) & (cnt > 0.0)
        if not bool(fill_mask.any()):
            break
        rgb[fill_mask] = acc[fill_mask] / cnt[fill_mask, None]
        valid[fill_mask] = True

    if not bool(valid.all()):
        # Deep transparent background outside dilation band gets slightly darkened opaque mean RGB
        orig_valid = alpha >= alpha_thresh
        mean_rgb = rgba[:, :, :3][orig_valid].mean(axis=0).astype(np.float32) * 0.85
        rgb[~valid] = mean_rgb

    out[:, :, :3] = np.clip(np.rint(rgb), 0.0, 255.0).astype(np.uint8)
    return out


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
    min_dim = min(w, h)
    if w <= 0 or h <= 0:
        return rgba, False

    tn = tex_name.upper()
    foliage_mode = _is_foliage_texture_name(tn) if is_foliage is None else is_foliage
    is_sign = tn.startswith(("SGN_", "ADS_")) or "SIGN" in tn or "DONOTENTER" in tn

    # Narrow-axis upscale for long road/sidewalk strips (e.g. 64x512 -> 256x512, 32x512 -> 128x512)
    if max_dim > max_source_dim:
        if min_dim > 128 or max_dim > 1024:
            return rgba, False
        narrow_scale = min(4, 256 // max(min_dim, 1))
        if narrow_scale < 2:
            return rgba, False
        new_w = w * narrow_scale if w <= h else w
        new_h = h * narrow_scale if h < w else h
        scale = narrow_scale
    else:
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

    # Dilate opaque RGB into transparent background BEFORE Lanczos + UnsharpMask so cutout edges never halo!
    src_rgba = dilate_alpha_edge_rgb(rgba, iterations=6, alpha_thresh=32) if has_cutout_alpha else rgba

    rgb_img = Image.fromarray(src_rgba[:, :, :3])
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
    if has_cutout_alpha:
        out = dilate_alpha_edge_rgb(out, iterations=6, alpha_thresh=32)
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
                narrow_up = False
                if enhance_textures:
                    orig_max = max(int(rgba.shape[1]), int(rgba.shape[0]))
                    rgba, enhanced = enhance_world_texture_2x(
                        rgba,
                        has_cutout_alpha=has_cutout,
                        tex_name=meta["name"],
                    )
                    narrow_up = enhanced and (orig_max > 256)
                if has_cutout and not enhanced:
                    rgba = dilate_alpha_edge_rgb(rgba, iterations=6, alpha_thresh=32)
                flags = (
                    (TEX_FLAG_HAS_CUTOUT_ALPHA if has_cutout else 0)
                    | (TEX_FLAG_PS2_0X80_NORMALIZED if norm_0x80 else 0)
                    | (TEX_FLAG_ENHANCED_2X if enhanced else 0)
                    | (TEX_FLAG_NARROW_AXIS_UPSCALED if narrow_up else 0)
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
            narrow_up = False
            if enhance_textures:
                orig_max = max(int(rgba.shape[1]), int(rgba.shape[0]))
                rgba, enhanced = enhance_world_texture_2x(
                    rgba,
                    has_cutout_alpha=has_cutout,
                    tex_name=meta["name"],
                )
                narrow_up = enhanced and (orig_max > 256)
            if has_cutout and not enhanced:
                rgba = dilate_alpha_edge_rgb(rgba, iterations=6, alpha_thresh=32)
            flags = (
                (TEX_FLAG_HAS_CUTOUT_ALPHA if has_cutout else 0)
                | (TEX_FLAG_PS2_0X80_NORMALIZED if norm_0x80 else 0)
                | (TEX_FLAG_ENHANCED_2X if enhanced else 0)
                | (TEX_FLAG_NARROW_AXIS_UPSCALED if narrow_up else 0)
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

    # 1. Drivable Road Asphalt (EA uses PAVEMENT* & RO_* such as RO_GENFILL_CRK / RO_DETAILSPACER for streets!)
    if tn.startswith("RO_") or any(
        k in tn for k in ("PAVEMENT", "ASPHALT", "ROAD", "HWY", "HIGHWAY", "INTERSECT", "CROSSWALK", "DRIVEWAY", "PARKINGLOT")
    ):
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

    # 4. Sidewalks, curbs, cobblestones, boardwalks, median tile walkways & plazas
    if tn.startswith("MD_") or any(
        k in tn
        for k in (
            "SIDEWALK", "CURB", "PLAZA", "WALKWAY", "COBBLE", "OLDCONCRETE",
            "PAINTEDCONCRETE", "CONC_WALK", "BOARDWALK", "PAVER", "FLAGSTONE", "GRASSYTILE",
        )
    ):
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


def is_excluded_world_solid(solid_name: str, debug_name: str = "", allow_panorama: bool = False) -> Optional[str]:
    """Return exclusion reason if solid is a Free-Roam-invisible event barrier, shadow card, or proxy.

    For distant panorama ring sections (80..99, only active >250m away), skip redundant close-up
    alpha-cutout `XT_*` bush/tree props (keeping all `PAN_*`, `TRN_*`, `XB_*`, `PANORAMA_*`, and
    `STADIUMLOD` horizon landmarks) to avoid 575+ non-opaque any-hit horizon rays per frame.
    """
    up_s = solid_name.upper()
    up_d = debug_name.upper()
    prefixes = ("SHD_", "RFL_", "__TEMP", "XT_") if allow_panorama else EXCLUDED_SOLID_PREFIXES
    if up_s.startswith(prefixes):
        return "shd_pan_rfl_temp"
    for sub in EXCLUDED_EVENT_SOLID_SUBSTRINGS:
        if sub in up_s or sub in up_d:
            return "event_barrier"
    return None


# ---------------------------------------------------------------------------
# Real Rockport Streetlamp & Emissive Emitter Extraction (RTWorldLight, 32 B)
# ---------------------------------------------------------------------------

_LAMP_SOLID_KEYWORDS = (
    "STREETLIGHT",
    "LIGHTPOST",
    "LIGHTPOLE",
    "IRONLAMP",
    "TRAFFICLIGHT",
    "CARWASHLIGHT",
    "FWY_SUPPORTLIGHTS",
    "FLOODLIGHT",
)

_NON_EMISSIVE_SIGN_KEYWORDS = (
    "STOPSIGN",
    "SPEED",
    "DONOTENTER",
    "ONEWAY",
    "YIELD",
    "MEDIANPOLE",
    "BUSSIGN",
    "SIGNALAHEAD",
    "WARNWINDING",
    "NOLEFT",
    "NORIGHT",
    "NOPARKING",
    "ARROW",
)


def extract_world_lights(
    meshes: Sequence[DecodedWorldSolid],
    mesh_vert_mats: Sequence[np.ndarray],
    materials: Sequence[RkptMaterialSpec],
    instances: Sequence[Tuple[int, int, int, Tuple[float, ...]]],
) -> Tuple[List[RTWorldLight], Dict[str, int]]:
    """Extract real Rockport streetlamp & elevated emissive world lights (32B RTWorldLight)."""
    mat_class = np.array([m.surface_class for m in materials], dtype=np.int32)
    mat_emissive = np.array([m.emissive_hint for m in materials], dtype=np.float32)

    # Precompute local emitter anchor point per mesh so 86,943 instances run in <0.1s!
    # Entry: (kind, local_pt_metal, radius, rgb_radiance, cone_min_cos)
    mesh_lamp_info: Dict[int, Tuple[str, np.ndarray, float, Tuple[float, float, float], float]] = {}
    for midx, m in enumerate(meshes):
        up = m.name.upper()
        pts = m.positions_metal
        if len(pts) == 0:
            continue
        vmats = mesh_vert_mats[midx]
        vcls = mat_class[vmats]
        vem = mat_emissive[vmats]

        if any(k in up for k in _LAMP_SOLID_KEYWORDS):
            # Locate exact luminaire bulb centroid: prefer emissive vertices in upper 35% of pole, else top 20%
            y_min, y_max = float(pts[:, 1].min()), float(pts[:, 1].max())
            y_cut = y_min + (y_max - y_min) * 0.65
            em_top = (vcls == SURFACE_EMISSIVE_SIGN_LAMP) & (pts[:, 1] >= y_cut)
            if np.any(em_top):
                anchor = pts[em_top].mean(axis=0)
            else:
                top_mask = pts[:, 1] >= (y_min + (y_max - y_min) * 0.80)
                anchor = pts[top_mask].mean(axis=0) if np.any(top_mask) else pts.mean(axis=0)
            # Nudge bulb slightly below fixture housing so downward ray/shading clears the metal hood
            anchor = anchor.astype(np.float64)
            anchor[1] = max(y_min + 2.0, float(anchor[1]) - 0.18)

            if "TRAFFICLIGHT" in up:
                mesh_lamp_info[midx] = ("traffic_signal", anchor, 16.0, (10.5, 8.2, 3.3), -0.20)
            elif "IRONLAMP" in up:
                mesh_lamp_info[midx] = ("iron_lamp", anchor, 18.0, (14.5, 10.4, 5.2), -0.10)
            elif "CARWASH" in up or "FLOODLIGHT" in up:
                mesh_lamp_info[midx] = ("flood_lamp", anchor, 24.0, (18.0, 17.0, 15.5), 0.10)
            else:
                # Classic Rockport sodium-vapor highway/avenue streetlamp
                mesh_lamp_info[midx] = ("streetlamp", anchor, 24.0, (19.0, 12.2, 4.7), 0.12)
        else:
            # Check for elevated storefront/neon/gas-station emissive emitters (skip tiny regulatory road signs)
            if any(k in up for k in _NON_EMISSIVE_SIGN_KEYWORDS) or up.startswith(("PAN_", "XT_", "TRN_")):
                continue
            em_mask = (vcls == SURFACE_EMISSIVE_SIGN_LAMP) & (vem >= 0.70)
            if int(np.sum(em_mask)) >= 8:
                em_pts = pts[em_mask]
                anchor = em_pts.mean(axis=0).astype(np.float64)
                if float(anchor[1] - pts[:, 1].min()) >= 1.5 or float(em_pts[:, 1].max() - em_pts[:, 1].min()) >= 1.2:
                    is_gas = "GAS" in up or "NEON" in up
                    col = (9.5, 10.5, 11.5) if is_gas else (10.5, 7.6, 3.8)
                    mesh_lamp_info[midx] = ("emissive_sign", anchor, 14.0, col, -1.0)

    lights: List[RTWorldLight] = []
    counts: Dict[str, int] = collections.Counter()
    seen_lamp_cells: Set[Tuple[int, int]] = set()
    seen_sign_cells: Set[Tuple[int, int]] = set()

    for midx, _sidx, _ifl, xform in instances:
        info = mesh_lamp_info.get(midx)
        if info is None:
            continue
        kind, local_pt, rad, col, cone = info
        wpt = apply_metal_4x3(xform, local_pt.reshape(1, 3))[0]
        wx, wy, wz = float(wpt[0]), float(wpt[1]), float(wpt[2])
        if kind == "emissive_sign":
            cell = (int(math.floor(wx / 10.0)), int(math.floor(wz / 10.0)))
            if cell in seen_sign_cells:
                continue
            seen_sign_cells.add(cell)
        else:
            # Avoid exact duplicate bulbs at the same 2m pole coordinate
            cell = (int(math.floor(wx / 2.0)), int(math.floor(wz / 2.0)))
            if cell in seen_lamp_cells:
                continue
            seen_lamp_cells.add(cell)
        lights.append(RTWorldLight(pos_metal=(wx, wy, wz), radius=rad, color_rgb=col, cone_min_cos=cone))
        counts[kind] += 1

    return lights, dict(counts)


# ---------------------------------------------------------------------------
# Fast Rust Batch BC7 + Mipmap Encoder Integration (tools/dds_encoder_rs)
# ---------------------------------------------------------------------------


def expected_bc7_mip_chain_bytes(width: int, height: int, mipmaps: bool = True) -> Tuple[int, int]:
    """Return (mip_count, total_bc7_bytes) for tightly-packed BC7 4x4 block mips."""
    cw, ch = max(1, int(width)), max(1, int(height))
    mips = 0
    total = 0
    while True:
        mips += 1
        total += ((cw + 3) // 4) * ((ch + 3) // 4) * 16
        if not mipmaps or (cw == 1 and ch == 1):
            break
        cw = max(1, cw // 2)
        ch = max(1, ch // 2)
    return mips, total


def find_or_build_rockport_dds() -> Path:
    """Locate prebuilt rockport_dds release binary, compiling offline via cargo if needed."""
    candidates = [
        REPO_ROOT / "build" / "dds_wave3" / "target" / "release" / "rockport_dds",
        REPO_ROOT / "build" / "dds_ci_target" / "release" / "rockport_dds",
        REPO_ROOT / "tools" / "dds_encoder_rs" / "target" / "release" / "rockport_dds",
    ]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    target_dir = REPO_ROOT / "build" / "dds_wave3" / "target"
    manifest = REPO_ROOT / "tools" / "dds_encoder_rs" / "Cargo.toml"
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(target_dir)
    subprocess.run(
        ["cargo", "build", "--release", "--manifest-path", str(manifest)],
        check=True,
        env=env,
    )
    out_bin = target_dir / "release" / "rockport_dds"
    if not out_bin.is_file():
        raise RuntimeError(f"Failed to build rockport_dds at {out_bin}")
    return out_bin


def encode_textures_bc7_batch(
    textures: Sequence[DecodedRkptTexture],
    mipmaps: bool = True,
    srgb: bool = True,
) -> float:
    """Batch-encode all textures in-place to BC7_RGBA_UNORM_SRGB + full sRGB mip chain via rockport_dds."""
    if not textures:
        return 0.0
    t0 = time.monotonic()
    dds_bin = find_or_build_rockport_dds()
    flags_val = (1 if mipmaps else 0) | (2 if srgb else 0)

    with tempfile.TemporaryDirectory(prefix="rkpt_bc7_") as td:
        in_path = Path(td) / "batch_in.rkb1"
        out_path = Path(td) / "batch_out.rko1"

        with in_path.open("wb") as f:
            f.write(b"RKB1")
            f.write(struct.pack("<I", len(textures)))
            for tex in textures:
                raw_len = len(tex.rgba_bytes)
                if raw_len != tex.width * tex.height * 4:
                    raise ValueError(f"Texture {tex.name} RGBA8 mismatch: {raw_len} != {tex.width}*{tex.height}*4")
                f.write(struct.pack("<4I", tex.width, tex.height, flags_val, raw_len))
                f.write(tex.rgba_bytes)

        cmd = [str(dds_bin), "--batch", str(in_path), "--output", str(out_path), "--format", "bc7"]
        cmd.append("--mipmaps" if mipmaps else "--no-mipmaps")
        if srgb:
            cmd.append("--srgb")
        subprocess.run(cmd, check=True)

        blob = out_path.read_bytes()
        if len(blob) < 8 or blob[0:4] != b"RKO1":
            raise RuntimeError("Invalid RKO1 header returned by rockport_dds --batch")
        out_cnt = struct.unpack_from("<I", blob, 4)[0]
        if out_cnt != len(textures):
            raise RuntimeError(f"RKO1 texture count mismatch: {out_cnt} != {len(textures)}")

        pos = 8
        for tex in textures:
            w, h, mips, enc_len = struct.unpack_from("<4I", blob, pos)
            pos += 16
            exp_mips, exp_bytes = expected_bc7_mip_chain_bytes(tex.width, tex.height, mipmaps=mipmaps)
            if (w, h, mips, enc_len) != (tex.width, tex.height, exp_mips, exp_bytes):
                raise RuntimeError(
                    f"BC7 batch mismatch for {tex.name}: got {(w, h, mips, enc_len)}, expected {(tex.width, tex.height, exp_mips, exp_bytes)}"
                )
            tex.encoded_bytes = blob[pos : pos + enc_len]
            tex.pixel_format = PIXEL_FORMAT_BC7_SRGB
            tex.mip_count = mips
            pos += enc_len

    return time.monotonic() - t0


def find_verified_road_spawn(
    meshes: Sequence[DecodedWorldSolid],
    mesh_vert_mats: Sequence[np.ndarray],
    materials: Sequence[RkptMaterialSpec],
    instances: Sequence[Tuple[int, int, int, Tuple[float, ...]]],
    focus_metal_xz: Tuple[float, float],
    min_clearance_m: float = 6.0,
    max_focus_dist_m: Optional[float] = None,
    max_candidate_dist_m: Optional[float] = None,
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
    fx, fz = focus_metal_xz
    focus_rad_sq = (max_focus_dist_m ** 2) if max_focus_dist_m is not None else None

    filtered_instances: List[Tuple[int, int, int, Tuple[float, ...]]] = []
    for inst_tuple in instances:
        mesh_idx, _sec_idx, _ifl, xform = inst_tuple
        if focus_rad_sq is not None:
            m = meshes[mesh_idx]
            # Fast instance-translation + mesh-radius reject before transforming vertices
            cx_l = 0.5 * (m.aabb_min_metal[0] + m.aabb_max_metal[0])
            cz_l = 0.5 * (m.aabb_min_metal[2] + m.aabb_max_metal[2])
            r_l = 0.5 * math.hypot(
                m.aabb_max_metal[0] - m.aabb_min_metal[0],
                m.aabb_max_metal[2] - m.aabb_min_metal[2],
            )
            wx_approx = xform[9] + xform[0] * cx_l + xform[6] * cz_l
            wz_approx = xform[11] + xform[2] * cx_l + xform[8] * cz_l
            lim = (max_focus_dist_m or 0.0) + r_l + 20.0
            if (wx_approx - fx) ** 2 + (wz_approx - fz) ** 2 > lim * lim:
                continue
        filtered_instances.append(inst_tuple)

    for mesh_idx, _sec_idx, _ifl, xform in filtered_instances:
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
    cand_keys = road_keys[:2000]
    if max_candidate_dist_m is not None:
        max_cd_sq = max_candidate_dist_m ** 2
        tight_keys = [
            k for k in cand_keys
            if ((k[0] + 0.5) * cell_sz - fx) ** 2 + ((k[1] + 0.5) * cell_sz - fz) ** 2 <= max_cd_sq
        ]
        if tight_keys:
            cand_keys = tight_keys
    for cx, cz in cand_keys:
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
    for mesh_idx, _sec_idx, _ifl, xform in filtered_instances:
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
    road_nodes: Sequence[RkptRoadNode] = (),
    road_segments: Sequence[RkptRoadSegment] = (),
    landmarks: Sequence[RkptLandmark] = (),
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

    # On full-city exports (>10K instances), skip tiny sub-pixel prop meshes (< 4m footprint) for 10x speed
    full_map_mode = len(instances) > 10_000
    min_mesh_span = (2.5 / max(scale, 1e-6)) if full_map_mode else 0.0
    mesh_spans = [
        math.hypot(m.aabb_max_metal[0] - m.aabb_min_metal[0], m.aabb_max_metal[2] - m.aabb_min_metal[2])
        for m in meshes
    ] if full_map_mode else []

    for mesh_idx, _sec_idx, _ifl, xform in instances:
        if full_map_mode and mesh_spans[mesh_idx] < min_mesh_span:
            continue
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
            keep = area2 >= (1.2 if full_map_mode else 0.35)
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

    # Overlay decoded WRoadNetwork road graph centerline polylines when available
    if road_nodes and road_segments:
        n_cnt = len(road_nodes)
        for seg in road_segments:
            if seg.node0 < n_cnt and seg.node1 < n_cnt:
                p0 = road_nodes[seg.node0].pos_metal
                p1 = road_nodes[seg.node1].pos_metal
                ax0, az0 = to_px(p0[0], p0[2])
                ax1, az1 = to_px(p1[0], p1[2])
                if pad <= ax0 <= img_size - pad and pad <= az0 <= img_size - pad:
                    lcol = (240, 178, 66) if seg.lane_flags >= 16 else (196, 214, 232)
                    draw.line([(ax0, az0), (ax1, az1)], fill=lcol, width=2 if seg.lane_flags >= 16 else 1)

    # Draw verified Rockport landmark pins
    for lm in landmarks:
        lx_p, lz_p = to_px(lm.spawn_pos[0], lm.spawn_pos[2])
        if pad <= lx_p <= img_size - pad and pad <= lz_p <= img_size - pad:
            draw.ellipse([(lx_p - 6, lz_p - 6), (lx_p + 6, lz_p + 6)], fill=(64, 215, 255), outline=(12, 16, 22), width=2)
            draw.text((lx_p + 9, lz_p - 7), lm.name, fill=(240, 248, 255))

    # Subtle 100m (or 500m for full map) coordinate grid
    grid_step = 500 if max(span_x, span_z) > 2500.0 else 100
    gx0 = int(math.floor(world_min[0] / grid_step)) * grid_step
    gx1 = int(math.ceil(world_max[0] / grid_step)) * grid_step
    for gx in range(gx0, gx1 + 1, grid_step):
        px_g, _ = to_px(float(gx), world_min[2])
        if pad <= px_g <= img_size - pad:
            draw.line([(px_g, pad), (px_g, img_size - pad)], fill=(44, 52, 64), width=1)
    gz0 = int(math.floor(world_min[2] / grid_step)) * grid_step
    gz1 = int(math.ceil(world_max[2] / grid_step)) * grid_step
    for gz in range(gz0, gz1 + 1, grid_step):
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


def write_rkpt_v2(
    out_path: Path,
    sections_meta: Sequence[Tuple[Any, ...]],
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
    *,
    spatial_section_count: Optional[int] = None,
    panorama_section_count: int = 0,
    road_nodes: Sequence[RkptRoadNode] = (),
    road_segments: Sequence[RkptRoadSegment] = (),
    world_lights: Sequence[RTWorldLight] = (),
    landmarks: Sequence[RkptLandmark] = (),
    pvs_zones: Sequence[RkptPvsZone] = (),
    pvs_verts: Sequence[Tuple[float, float]] = (),
    pvs_entries: Sequence[int] = (),
) -> Dict[str, Any]:
    """Serialize assembled Rockport world to locked `.rkpt` v2 (BC7/RGBA8 + RoadGraph + WorldLights + Landmarks + PVS).

    Layout (all section/table offsets 16-byte aligned):
      [0..159]   160B Base RkptHeader (version=2, flags bit0=HAS_V2_EXT, bit1=BC7, bit2=HAS_PANORAMA, bit3=HAS_PVS)
      [160..255]  96B RkptV2ExtHeader (counts + 64-bit offsets for trailing v2 tables including PVS)
      [256..]    Sections (64B), Meshes (48B), Instances (64B), Materials (32B), Textures (32B),
                 Vertices (36B), Indices (4B), TexData (RGBA8 or BC7 full mip chains),
                 RoadNodes (32B), RoadSegments (16B), WorldLights (32B), Landmarks (64B),
                 PvsZones (32B), PvsVerts (8B), PvsEntries (2B).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray(HEADER_SIZE + V2_EXT_HEADER_SIZE)  # 256 bytes placeholder

    # 1. Sections (64 B each; reserved field carries SEC_FLAG_PANORAMA)
    sections_off = _pad16(buf)
    for row in sections_meta:
        name, sec_num, inst_start, inst_cnt, center, radius, a_min, a_max = row[:8]
        sec_flags = int(row[8]) if len(row) > 8 else 0
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
                sec_flags & 0xFFFFFFFF,
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
        buf.extend(struct.pack(INSTANCE_FMT, mesh_idx, sec_idx, ifl & 0xFFFFFFFF, 0, *xform12))

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

    # 5. Textures table (32 B each) + packed texData buffer (16B-aligned)
    textures_off = _pad16(buf)
    tex_data_blob = bytearray()
    any_bc7 = False
    for tex in textures:
        rem = len(tex_data_blob) & 15
        if rem:
            tex_data_blob.extend(b"\x00" * (16 - rem))
        rel_off = len(tex_data_blob)
        if tex.pixel_format == PIXEL_FORMAT_BC7_SRGB and tex.encoded_bytes is not None:
            payload = tex.encoded_bytes
            pfmt = PIXEL_FORMAT_BC7_SRGB
            mips = max(1, int(tex.mip_count))
            any_bc7 = True
        else:
            payload = tex.rgba_bytes
            expected_sz = tex.width * tex.height * 4
            if len(payload) != expected_sz:
                raise ValueError(f"Texture {tex.name} RGBA size mismatch: {len(payload)} != {expected_sz}")
            pfmt = PIXEL_FORMAT_RGBA8_SRGB
            mips = 1
        tex_data_blob.extend(payload)
        packed_flags = (tex.flags & 0xFF) | ((mips & 0xFF) << TEX_FLAG_MIPCOUNT_SHIFT)
        buf.extend(
            struct.pack(
                TEXTURE_FMT,
                tex.tpk_name_hash & 0xFFFFFFFF,
                tex.width,
                tex.height,
                pfmt,
                rel_off,
                len(payload),
                packed_flags & 0xFFFFFFFF,
            )
        )

    # 6. Vertices (36 B each)
    vertices_off = _pad16(buf)
    v_dtype = np.dtype(
        [("pos", "<3f4"), ("norm", "<3f4"), ("uv", "<2f4"), ("mat", "<u2"), ("ao", "u1"), ("fl", "u1")]
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

    # 8. TexData (RGBA8 or BC7+mips)
    tex_data_off = _pad16(buf)
    buf.extend(tex_data_blob)

    # 9. Trailing v2 Tables (each 16-byte aligned; offset=0 when empty)
    road_nodes_off = 0
    if road_nodes:
        road_nodes_off = _pad16(buf)
        for rn in road_nodes:
            buf.extend(
                struct.pack(
                    ROAD_NODE_FMT,
                    rn.pos_metal[0], rn.pos_metal[1], rn.pos_metal[2],
                    rn.node_index & 0xFFFF,
                    rn.flags & 0xFFFF,
                    rn.degree & 0xFF,
                    0,
                    *(s & 0xFFFF for s in rn.adj_segments),
                )
            )

    road_segments_off = 0
    if road_segments:
        road_segments_off = _pad16(buf)
        for rs in road_segments:
            buf.extend(
                struct.pack(
                    ROAD_SEGMENT_FMT,
                    rs.node0 & 0xFFFF,
                    rs.node1 & 0xFFFF,
                    rs.lane_flags & 0xFF,
                    rs.dir_flags & 0xFF,
                    rs.road_id & 0xFFFF,
                    float(rs.length_m),
                    float(rs.half_width_m),
                )
            )

    world_lights_off = 0
    if world_lights:
        world_lights_off = _pad16(buf)
        for wl in world_lights:
            buf.extend(
                struct.pack(
                    WORLD_LIGHT_FMT,
                    wl.pos_metal[0], wl.pos_metal[1], wl.pos_metal[2], float(wl.radius),
                    wl.color_rgb[0], wl.color_rgb[1], wl.color_rgb[2], float(wl.cone_min_cos),
                )
            )

    landmarks_off = 0
    if landmarks:
        landmarks_off = _pad16(buf)
        for lm in landmarks:
            nm32 = lm.name.encode("ascii", errors="ignore")[:31].ljust(32, b"\x00")
            buf.extend(
                struct.pack(
                    LANDMARK_FMT,
                    nm32,
                    lm.spawn_pos[0], lm.spawn_pos[1], lm.spawn_pos[2],
                    float(lm.spawn_yaw_rad),
                    lm.region_id & 0xFFFFFFFF,
                    lm.flags & 0xFFFFFFFF,
                    0,
                    0,
                )
            )

    pvs_zones_off = 0
    pvs_verts_off = 0
    pvs_entries_off = 0
    has_pvs = bool(pvs_zones)
    if has_pvs:
        pvs_zones_off = _pad16(buf)
        for z in pvs_zones:
            buf.extend(
                struct.pack(
                    PVS_ZONE_FMT,
                    float(z.min_x),
                    float(z.min_z),
                    float(z.max_x),
                    float(z.max_z),
                    z.first_vert & 0xFFFFFFFF,
                    z.vert_count & 0xFFFFFFFF,
                    z.first_sec_entry & 0xFFFFFFFF,
                    z.sec_entry_count & 0xFFFFFFFF,
                )
            )
        if pvs_verts:
            pvs_verts_off = _pad16(buf)
            for vx, vz in pvs_verts:
                buf.extend(struct.pack(PVS_VERT_FMT, float(vx), float(vz)))
        if pvs_entries:
            pvs_entries_off = _pad16(buf)
            buf.extend(np.ascontiguousarray(pvs_entries, dtype="<u2").tobytes())

    hdr_flags = RKPT_HDR_FLAG_HAS_V2_EXT
    if any_bc7:
        hdr_flags |= RKPT_HDR_FLAG_BC7_TEXTURES
    if panorama_section_count > 0:
        hdr_flags |= RKPT_HDR_FLAG_HAS_PANORAMA
    if has_pvs:
        hdr_flags |= RKPT_HDR_FLAG_HAS_PVS

    sp_cnt = (len(sections_meta) - panorama_section_count) if spatial_section_count is None else spatial_section_count

    header = struct.pack(
        HEADER_FMT,
        RKPT_MAGIC,
        RKPT_VERSION_V2,
        hdr_flags,
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
    ext_header = struct.pack(
        V2_EXT_HEADER_FMT,
        len(road_nodes),
        len(road_segments),
        len(world_lights),
        len(landmarks),
        sp_cnt,
        panorama_section_count,
        len(pvs_zones) if has_pvs else 0,
        len(pvs_entries) if has_pvs else 0,
        road_nodes_off,
        road_segments_off,
        world_lights_off,
        landmarks_off,
        pvs_zones_off,
        pvs_verts_off,
        pvs_entries_off,
        len(pvs_verts) if has_pvs else 0,
    )
    buf[0:HEADER_SIZE] = header
    buf[HEADER_SIZE : HEADER_SIZE + V2_EXT_HEADER_SIZE] = ext_header
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
        "road_nodes_offset": road_nodes_off,
        "road_segments_offset": road_segments_off,
        "world_lights_offset": world_lights_off,
        "landmarks_offset": landmarks_off,
        "pvs_zones_offset": pvs_zones_off,
        "pvs_verts_offset": pvs_verts_off,
        "pvs_entries_offset": pvs_entries_off,
    }


def read_rkpt_header(path_or_bytes: Any) -> Dict[str, Any]:
    """Parse and validate a `.rkpt` v1 or v2 binary header (+ optional 96B v2 extension header)."""
    if isinstance(path_or_bytes, (str, Path)):
        p = Path(path_or_bytes)
        file_size = p.stat().st_size
        with p.open("rb") as f:
            raw = f.read(HEADER_SIZE + V2_EXT_HEADER_SIZE)
    else:
        raw = bytes(path_or_bytes[: HEADER_SIZE + V2_EXT_HEADER_SIZE])
        file_size = len(path_or_bytes)

    if len(raw) < HEADER_SIZE:
        raise ValueError(f"RKPT header truncated: {len(raw)} < {HEADER_SIZE}")
    vals = struct.unpack(HEADER_FMT, raw[:HEADER_SIZE])
    magic = vals[0]
    if magic != RKPT_MAGIC:
        raise ValueError(f"Invalid RKPT magic: {magic!r}")
    out: Dict[str, Any] = {
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

    if out["version"] >= RKPT_VERSION_V2 and (out["flags"] & RKPT_HDR_FLAG_HAS_V2_EXT):
        if len(raw) < HEADER_SIZE + V2_EXT_HEADER_SIZE:
            raise ValueError(f"RKPT v2 extension header truncated: {len(raw)} < {HEADER_SIZE + V2_EXT_HEADER_SIZE}")
        ev = struct.unpack(V2_EXT_HEADER_FMT, raw[HEADER_SIZE : HEADER_SIZE + V2_EXT_HEADER_SIZE])
        has_pvs = bool(out["flags"] & RKPT_HDR_FLAG_HAS_PVS)
        out.update(
            {
                "roadNodeCount": ev[0],
                "roadSegmentCount": ev[1],
                "worldLightCount": ev[2],
                "landmarkCount": ev[3],
                "spatialSectionCount": ev[4],
                "panoramaSectionCount": ev[5],
                "pvsZoneCount": ev[6] if has_pvs else 0,
                "pvsEntryCount": ev[7] if has_pvs else 0,
                "roadNodesOffset": ev[8],
                "roadSegmentsOffset": ev[9],
                "worldLightsOffset": ev[10],
                "landmarksOffset": ev[11],
                "pvsZonesOffset": ev[12] if has_pvs else 0,
                "pvsVertsOffset": ev[13] if has_pvs else 0,
                "pvsEntriesOffset": ev[14] if has_pvs else 0,
                "pvsVertCount": ev[15] if has_pvs else 0,
                "hasPVS": has_pvs,
            }
        )
        for k in (
            "roadNodesOffset", "roadSegmentsOffset", "worldLightsOffset", "landmarksOffset",
            "pvsZonesOffset", "pvsVertsOffset", "pvsEntriesOffset",
        ):
            if out[k] != 0 and (out[k] & 15) != 0:
                raise ValueError(f"v2 offset {k}={out[k]} is not 16-byte aligned")
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
    rkpt_version: Optional[int] = None,
    compress_bc7: Optional[bool] = None,
    botanical_cypress: bool = True,
    botanical_foliage: bool = True,
) -> Dict[str, Any]:
    """Extract, assemble, verify, and serialize one Rockport district or full island (`rockport_full`).

    Defaults:
      - `rosewood` / `downtown`: `.rkpt` v1 RGBA8 (100% backward-compatible with v0.6 loaders,
        upgraded with narrow-axis sidewalk upscaling + multi-tier botanical cypresses).
      - `rockport_full`: `.rkpt` v2 BC7 (`638` spatial + `80` panorama = `718` sections,
        `WRoadNetwork` road graph, `RTWorldLight` real lamps, and 6 verified Rockport landmarks).
    """
    t_start = time.monotonic()
    district_key = district.strip().lower()
    is_full_map = district_key == "rockport_full"
    if not is_full_map and district_key not in PILOT_DISTRICTS:
        raise ValueError(f"Unknown district {district!r}; valid: {sorted(PILOT_DISTRICTS) + ['rockport_full']}")
    if not iso_path.is_file():
        raise FileNotFoundError(f"ISO not found: {iso_path}")

    version = (RKPT_VERSION_V2 if is_full_map else RKPT_VERSION) if rkpt_version is None else int(rkpt_version)
    use_bc7 = (version >= RKPT_VERSION_V2) if compress_bc7 is None else bool(compress_bc7)
    if use_bc7 and version < RKPT_VERSION_V2:
        version = RKPT_VERSION_V2

    arc = PS2NFSMWArchive(iso_path)
    l2ra_bytes = arc.read_entry(arc.find(r"TRACKS\L2RA.BUN"))
    stream_entry = arc.find(r"TRACKS\STREAML2RA.BUN")
    stream_abs_off = (arc.zz_lbas[stream_entry.zz_index] + stream_entry.sector_in_zz) * 2048

    section_table = parse_l2ra_section_table(l2ra_bytes)
    x_names = sorted(k for k in section_table if k.startswith("X"))
    v_names = sorted(k for k in section_table if k.startswith("V"))
    u_names = sorted(k for k in section_table if k.startswith("U"))
    y_names = sorted(k for k in section_table if k.startswith("Y"))
    w_names = sorted(k for k in section_table if k.startswith("W"))

    if is_full_map:
        spatial_sec_names: Tuple[str, ...] = tuple(
            sorted(k for k in section_table if k[0] in "ABCDEFGHIJKLMNOPQRST" and k[1:].isdigit() and int(k[1:]) < 80)
        )
        panorama_sec_names: Tuple[str, ...] = tuple(
            sorted(k for k in section_table if k[0] in "ABCDEFGHIJKLMNOPQRST" and k[1:].isdigit() and 80 <= int(k[1:]) <= 99)
        )
        target_sec_names: Tuple[str, ...] = spatial_sec_names + panorama_sec_names
        display_name = "Full Rockport City Island (Rosewood + Downtown + Camden Beach + Highways + 80 Panorama)"
        spawn_focus_ps2 = ROCKPORT_LANDMARK_SPECS[0][1]
    else:
        cfg = PILOT_DISTRICTS[district_key]
        spatial_sec_names = cfg["sections"]
        panorama_sec_names = ()
        target_sec_names = spatial_sec_names
        display_name = cfg["display_name"]
        spawn_focus_ps2 = cfg["spawn_focus_ps2"]
        for sname in target_sec_names:
            num = int(sname[1:])
            if num >= 80:
                raise ValueError(f"Panorama/LOD section {sname} must not be included in pilot district export")

    missing_secs = [s for s in target_sec_names if s not in section_table]
    if missing_secs:
        raise KeyError(f"Missing sections in L2RA.BUN: {missing_secs}")

    panorama_sec_set = set(panorama_sec_names)
    section_bufs: Dict[str, bytes] = {}

    with iso_path.open("rb") as iso_f:
        # For full-map export, read STREAML2RA.BUN (191 MB) once in ~0.09s for fast zero-seek slicing
        stream_blob: Optional[bytes] = None
        if is_full_map:
            iso_f.seek(stream_abs_off)
            stream_blob = iso_f.read(stream_entry.byte_size)

        def _read_sec(sname: str) -> bytes:
            if sname in section_bufs:
                return section_bufs[sname]
            ent = section_table[sname]
            if stream_blob is not None:
                data = stream_blob[ent.file_offset : ent.file_offset + ent.size]
            else:
                iso_f.seek(stream_abs_off + ent.file_offset)
                data = iso_f.read(ent.size)
            section_bufs[sname] = data
            return data

        # 1. Index solids in priority order: spatial/pan local > X* > V* > U*
        solid_index: Dict[int, Tuple[bytes, int, int, str]] = {}
        for sname in target_sec_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)
        for sname in x_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)
        for sname in v_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)
        for sname in u_names:
            index_solids_in_section(_read_sec(sname), solid_index, overwrite=False)

        # 2. Parse SceneryGroups from all spatial + panorama sections
        sec_infos: Dict[str, List[SceneryInfoEntry]] = {}
        sec_insts: Dict[str, List[SceneryInstanceEntry]] = {}
        for sname in target_sec_names:
            infos, insts = parse_scenery_groups(_read_sec(sname))
            sec_infos[sname] = infos
            sec_insts[sname] = insts

        # Compute origin in PS2 Z-up space (centered on spatial sections only so float32 precision stays tight)
        xs_c = [section_table[s].center_x for s in spatial_sec_names]
        ys_c = [section_table[s].center_y for s in spatial_sec_names]
        ps2_origin = (
            round(float(sum(xs_c) / len(xs_c)), 3),
            round(float(sum(ys_c) / len(ys_c)), 3),
            0.0,
        )

        # 3. Filter instances & collect referenced unique solid hashes
        kept_instances_raw: List[Tuple[int, int, SceneryInstanceEntry]] = []
        spatial_solid_hashes: Set[int] = set()
        panorama_solid_hashes: Set[int] = set()
        filtered_excl = 0
        filtered_shd_pan = 0
        filtered_event_barriers: Dict[str, int] = collections.Counter()
        unresolved_inst = 0

        for sec_idx, sname in enumerate(target_sec_names):
            is_pan_sec = sname in panorama_sec_set
            infos = sec_infos[sname]
            for inst in sec_insts[sname]:
                if inst.exclude_flags & 0x01:
                    filtered_excl += 1
                    continue
                if inst.scenery_info_idx < 0 or inst.scenery_info_idx >= len(infos):
                    unresolved_inst += 1
                    continue
                info = infos[inst.scenery_info_idx]
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
                excl_reason = is_excluded_world_solid(chosen_name, info.debug_name, allow_panorama=is_pan_sec)
                if excl_reason == "event_barrier":
                    filtered_event_barriers[f"{chosen_name} (0x{chosen_hash:08x}, excl=0x{inst.exclude_flags:02x})"] += 1
                    continue
                if excl_reason is not None or (inst.flags & 0x01 and "SHD" in up_name):
                    filtered_shd_pan += 1
                    continue
                kept_instances_raw.append((sec_idx, chosen_hash, inst))
                if is_pan_sec:
                    panorama_solid_hashes.add(chosen_hash)
                else:
                    spatial_solid_hashes.add(chosen_hash)

        # 4. Decode unique referenced solids into Metal Y-up space (+ botanical cypress & v1.4 all-district foliage)
        decoded_by_hash: Dict[int, DecodedWorldSolid] = {}
        wanted_tex_hashes: Set[int] = set()
        botanical_cypress_meshes_replaced = 0
        botanical_foliage_meshes_replaced = 0
        botanical_archetype_mesh_hashes: Dict[int, str] = {}
        for _sec_idx, shash, _inst in kept_instances_raw:
            if shash in decoded_by_hash:
                continue
            sbuf, soff, ssz, _nm = solid_index[shash]
            dec = decode_world_solid_chunk(sbuf, soff, ssz)
            if dec is not None:
                up_dec = dec.name.upper()
                if botanical_cypress and up_dec in (CYPRESS_SINGLE_SOLIDS | CYPRESS_CLUMP_SOLIDS):
                    dec = build_botanical_cypress_solid(dec)
                    botanical_cypress_meshes_replaced += 1
                elif botanical_foliage:
                    arch = match_botanical_foliage_archetype(up_dec)
                    if arch is not None:
                        dec = build_botanical_foliage_solid(dec, arch)
                        botanical_foliage_meshes_replaced += 1
                        botanical_archetype_mesh_hashes[shash] = arch
                decoded_by_hash[shash] = dec
                used_slots = np.unique(dec.vertex_tex_slot)
                for sl in used_slots.tolist():
                    if 0 <= sl < len(dec.texture_hashes):
                        th = dec.texture_hashes[sl]
                        if th != 0:
                            wanted_tex_hashes.add(th)

        # 5. Decode matching textures in priority order: Y* > local spatial/pan > V* > global > W* > X* > U*
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
        for extra_grp in (w_names, x_names, u_names):
            if len(resolved_textures) >= len(wanted_tex_hashes):
                break
            for sname in extra_grp:
                if len(resolved_textures) >= len(wanted_tex_hashes):
                    break
                decode_tpk_textures_matching(
                    _read_sec(sname), f"STREAML2RA:{sname}", wanted_tex_hashes, resolved_textures,
                    enhance_textures=enhance_textures,
                )

        # Replace ORG_TREE_CYPRESS (0x4CCF947E) with crisp 512x512 procedural feathery frond texture
        botanical_cypress_tex_replaced = False
        if botanical_cypress and CYPRESS_FROND_TEX_HASH in wanted_tex_hashes:
            frond_rgba = generate_botanical_cypress_frond_rgba(512)
            resolved_textures[CYPRESS_FROND_TEX_HASH] = DecodedRkptTexture(
                name="ORG_TREE_CYPRESS",
                tpk_name_hash=CYPRESS_FROND_TEX_HASH,
                width=512,
                height=512,
                rgba_bytes=frond_rgba.tobytes(),
                flags=(
                    TEX_FLAG_HAS_CUTOUT_ALPHA
                    | TEX_FLAG_ENHANCED_2X
                    | TEX_FLAG_PROCEDURAL_BOTANICAL
                ),
                source="procedural:botanical_cypress_512",
            )
            botanical_cypress_tex_replaced = True

        # Replace the 6 v1.4.0 all-district foliage archetype textures in-place (512x512, 100% edge-dilated, 0 extra tex count!)
        botanical_foliage_textures_replaced: List[str] = []
        if botanical_foliage:
            for b_th in (
                BOTANICAL_TEX_MAPLE_CRIMSON,
                BOTANICAL_TEX_FALL_AMBER,
                BOTANICAL_TEX_POPLAR_GOLD,
                BOTANICAL_TEX_CEDAR_BOUGH,
                BOTANICAL_TEX_ROADSIDE_BUSH,
                BOTANICAL_TEX_FOREST_BUSH,
                BOTANICAL_TEX_STADIUM_HEDGE,
                BOTANICAL_TEX_BEACHGRASS,
            ):
                if b_th in wanted_tex_hashes and b_th in resolved_textures:
                    gen = generate_botanical_foliage_texture_rgba(b_th, 512)
                    if gen is not None:
                        t_name, t_src, t_rgba = gen
                        resolved_textures[b_th] = DecodedRkptTexture(
                            name=t_name,
                            tpk_name_hash=b_th,
                            width=512,
                            height=512,
                            rgba_bytes=t_rgba.tobytes(),
                            flags=(
                                TEX_FLAG_HAS_CUTOUT_ALPHA
                                | TEX_FLAG_ENHANCED_2X
                                | TEX_FLAG_PROCEDURAL_BOTANICAL
                            ),
                            source=t_src,
                        )
                        botanical_foliage_textures_replaced.append(t_name)

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

    bc7_encode_sec = 0.0
    if use_bc7:
        bc7_encode_sec = encode_textures_bc7_batch(textures_list, mipmaps=True, srgb=True)

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

        mfl = 0
        up_name = m.name.upper()
        if up_name.startswith("TRN_") or "_CHOP_" in up_name:
            mfl |= MESH_FLAG_TERRAIN_CHOP
        if up_name.startswith(("PAN_", "PANORAMA", "SKY", "RFL_", "STADIUMLOD")) or (
            shash in panorama_solid_hashes and shash not in spatial_solid_hashes
        ):
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
    spatial_min = np.array([1e18, 1e18, 1e18], dtype=np.float64)
    spatial_max = np.array([-1e18, -1e18, -1e18], dtype=np.float64)
    sec_mins = [np.array([1e18, 1e18, 1e18], dtype=np.float64) for _ in target_sec_names]
    sec_maxs = [np.array([-1e18, -1e18, -1e18], dtype=np.float64) for _ in target_sec_names]

    mesh_corners = []
    for m in meshes_list:
        lo, hi = m.aabb_min_metal, m.aabb_max_metal
        mesh_corners.append(
            np.array(
                [
                    [lo[0], lo[1], lo[2]], [hi[0], lo[1], lo[2]],
                    [lo[0], hi[1], lo[2]], [hi[0], hi[1], lo[2]],
                    [lo[0], lo[1], hi[2]], [hi[0], lo[1], hi[2]],
                    [lo[0], hi[1], hi[2]], [hi[0], hi[1], hi[2]],
                ],
                dtype=np.float64,
            )
        )

    n_spatial_secs = len(spatial_sec_names)
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
        if sec_idx < n_spatial_secs:
            spatial_min = np.minimum(spatial_min, cmin)
            spatial_max = np.maximum(spatial_max, cmax)

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
        sec_fl = SEC_FLAG_PANORAMA if sname in panorama_sec_set else 0
        sections_meta.append((sname, ent.section_number, istart, icnt, c_metal, ent.radius, s_min, s_max, sec_fl))

    world_bounds_min = tuple(float(v) for v in world_min)
    world_bounds_max = tuple(float(v) for v in world_max)
    spatial_bounds_min = tuple(float(v) for v in spatial_min) if n_spatial_secs > 0 else world_bounds_min
    spatial_bounds_max = tuple(float(v) for v in spatial_max) if n_spatial_secs > 0 else world_bounds_max

    # 7. Extract EA WRoadNetwork (0x0003B800) + EA PVS (0x80034150) + Real Streetlamps/Lights + Verified Road Spawns / Landmarks
    road_nodes, road_segments = parse_l2ra_road_network(l2ra_bytes, ps2_origin)
    if version >= RKPT_VERSION_V2 and is_full_map:
        pvs_zones, pvs_verts, pvs_entries, pvs_stats = parse_l2ra_pvs_zones(
            l2ra_bytes=l2ra_bytes,
            sections_meta=sections_meta,
            spatial_section_count=len(spatial_sec_names),
            ps2_origin=ps2_origin,
        )
    else:
        pvs_zones, pvs_verts, pvs_entries = [], [], []
        pvs_stats = {
            "pvs_polygon_count": 0,
            "pvs_vislist_count": 0,
            "pvs_matched_vis_count": 0,
            "pvs_zone_count": 0,
            "pvs_vert_count": 0,
            "pvs_entry_count": 0,
            "pvs_vista_median_m": 0.0,
            "pvs_vista_p90_m": 0.0,
            "pvs_vista_max_m": 0.0,
        }
    spatial_only_insts = flat_instances[: sections_meta[n_spatial_secs][2]] if panorama_sec_names else flat_instances
    world_lights, world_light_breakdown = extract_world_lights(
        meshes=meshes_list,
        mesh_vert_mats=mesh_vert_mats,
        materials=materials_list,
        instances=spatial_only_insts,
    )

    landmarks_list: List[RkptLandmark] = []
    if is_full_map:
        spawn_pos = (0.0, 0.0, 0.0)
        spawn_yaw_rad = 0.0
        spawn_meta: Dict[str, Any] = {}
        for lm_idx, (lm_name, (lx_ps2, ly_ps2), reg_id) in enumerate(ROCKPORT_LANDMARK_SPECS):
            fm = ps2_point_to_metal(lx_ps2, ly_ps2, 0.0, ps2_origin)
            tight_cand = 75.0 if lm_name in ("Highway 99", "Rosewood College") else None
            sp, syaw, smeta = find_verified_road_spawn(
                meshes=meshes_list,
                mesh_vert_mats=mesh_vert_mats,
                materials=materials_list,
                instances=spatial_only_insts,
                focus_metal_xz=(fm[0], fm[2]),
                min_clearance_m=6.0,
                max_focus_dist_m=450.0,
                max_candidate_dist_m=tight_cand,
            )
            landmarks_list.append(RkptLandmark(name=lm_name, spawn_pos=sp, spawn_yaw_rad=syaw, region_id=reg_id))
            if lm_idx == 0:
                spawn_pos, spawn_yaw_rad, spawn_meta = sp, syaw, smeta
    else:
        fx_ps2, fy_ps2 = spawn_focus_ps2
        focus_metal = ps2_point_to_metal(fx_ps2, fy_ps2, 0.0, ps2_origin)
        spawn_pos, spawn_yaw_rad, spawn_meta = find_verified_road_spawn(
            meshes=meshes_list,
            mesh_vert_mats=mesh_vert_mats,
            materials=materials_list,
            instances=flat_instances,
            focus_metal_xz=(focus_metal[0], focus_metal[2]),
            min_clearance_m=6.0,
        )
        reg_id = 0 if district_key == "rosewood" else 1
        landmarks_list.append(
            RkptLandmark(
                name="Rosewood Blvd" if district_key == "rosewood" else "Downtown East Park",
                spawn_pos=spawn_pos,
                spawn_yaw_rad=spawn_yaw_rad,
                region_id=reg_id,
            )
        )

    # 8. Write `.rkpt` v1 or v2 binary + self-verify header
    out_dir.mkdir(parents=True, exist_ok=True)
    rkpt_path = out_dir / f"{district_key}.rkpt"
    if version >= RKPT_VERSION_V2:
        write_stats = write_rkpt_v2(
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
            spatial_section_count=len(spatial_sec_names),
            panorama_section_count=len(panorama_sec_names),
            road_nodes=road_nodes,
            road_segments=road_segments,
            world_lights=world_lights,
            landmarks=landmarks_list,
            pvs_zones=pvs_zones,
            pvs_verts=pvs_verts,
            pvs_entries=pvs_entries,
        )
    else:
        sections_v1 = [row[:8] for row in sections_meta]
        write_stats = write_rkpt_v1(
            out_path=rkpt_path,
            sections_meta=sections_v1,  # type: ignore[arg-type]
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
            instances=spatial_only_insts if is_full_map else flat_instances,
            world_min=spatial_bounds_min if is_full_map else world_bounds_min,
            world_max=spatial_bounds_max if is_full_map else world_bounds_max,
            spawn_pos=spawn_pos,
            spawn_yaw_rad=spawn_yaw_rad,
            img_size=1600 if is_full_map else 1200,
            road_nodes=road_nodes if is_full_map else (),
            road_segments=road_segments if is_full_map else (),
            landmarks=landmarks_list if is_full_map else (),
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
    narrow_upscaled_count = sum(1 for t in textures_list if (t.flags & TEX_FLAG_NARROW_AXIS_UPSCALED) != 0)
    botanical_foliage_by_arch: Dict[str, int] = collections.Counter()
    for _sidx, shash, _inst in kept_instances_raw:
        arch_k = botanical_archetype_mesh_hashes.get(shash)
        if arch_k is not None:
            botanical_foliage_by_arch[arch_k] += 1
    botanical_foliage_inst_total = sum(botanical_foliage_by_arch.values())
    elapsed_sec = round(time.monotonic() - t_start, 2)
    manifest: Dict[str, Any] = {
        "format": "RKPT",
        "version": verified_hdr["version"],
        "header_flags": verified_hdr["flags"],
        "district": district_key,
        "display_name": display_name,
        "iso_name": iso_path.name,
        "rkpt_file": rkpt_path.name,
        "layout_png": layout_png_rel,
        "rkpt_bytes": write_stats["file_bytes"],
        "elapsed_seconds": elapsed_sec,
        "bc7_compressed": use_bc7,
        "bc7_encode_seconds": round(bc7_encode_sec, 2),
        "sections": list(target_sec_names) if not is_full_map else f"{len(spatial_sec_names)} spatial + {len(panorama_sec_names)} panorama",
        "section_count": verified_hdr["sectionCount"],
        "spatial_section_count": len(spatial_sec_names),
        "panorama_section_count": len(panorama_sec_names),
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
        "texture_narrow_axis_upscaled_count": narrow_upscaled_count,
        "botanical_cypress_meshes_replaced": botanical_cypress_meshes_replaced,
        "botanical_cypress_texture_replaced": botanical_cypress_tex_replaced,
        "botanical_foliage_meshes_replaced": botanical_foliage_meshes_replaced,
        "botanical_foliage_textures_replaced": botanical_foliage_textures_replaced,
        "botanical_foliage_instance_count": botanical_foliage_inst_total,
        "botanical_foliage_instances_by_archetype": dict(sorted(botanical_foliage_by_arch.items())),
        "pvs": pvs_stats,
        "enhance_textures_enabled": enhance_textures,
        "unique_vertex_count": verified_hdr["totalVertexCount"],
        "unique_triangle_count": verified_hdr["totalIndexCount"] // 3,
        "instanced_triangle_count": total_instanced_tris,
        "road_node_count": len(road_nodes),
        "road_segment_count": len(road_segments),
        "world_light_count": len(world_lights),
        "world_light_breakdown": world_light_breakdown,
        "landmarks": [
            {
                "name": lm.name,
                "spawn_pos": [round(v, 3) for v in lm.spawn_pos],
                "spawn_yaw_rad": round(lm.spawn_yaw_rad, 6),
                "spawn_yaw_deg": round(math.degrees(lm.spawn_yaw_rad), 2),
                "region_id": lm.region_id,
            }
            for lm in landmarks_list
        ],
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
        description="Export authentic PS2 NFSMW Rockport City districts or full island to .rkpt v1/v2 for Native Metal RT."
    )
    parser.add_argument(
        "--district",
        choices=("rosewood", "downtown", "rockport_full", "all"),
        default="rosewood",
        help="District to export: rosewood, downtown, rockport_full (718 secs), or all (default: rosewood).",
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
        "--version",
        type=int,
        choices=(1, 2),
        default=None,
        help="Force .rkpt binary version (default: 2 for rockport_full, 1 for rosewood/downtown).",
    )
    parser.add_argument(
        "--bc7",
        dest="compress_bc7",
        action="store_true",
        default=None,
        help="Compress textures to BC7_RGBA_UNORM_SRGB + full sRGB mipmaps via rockport_dds (implies v2).",
    )
    parser.add_argument(
        "--rgba8",
        dest="compress_bc7",
        action="store_false",
        help="Emit raw RGBA8 textures instead of BC7.",
    )
    parser.add_argument(
        "--enhance-textures",
        dest="enhance_textures",
        action="store_true",
        default=True,
        help="2x/4x/narrow-axis Lanczos + crisp UnsharpMask upscale for world textures (default: ON).",
    )
    parser.add_argument(
        "--no-enhance-textures",
        dest="enhance_textures",
        action="store_false",
        help="Disable world texture enhancement and emit native PS2 resolutions.",
    )
    parser.add_argument(
        "--no-botanical-cypress",
        dest="botanical_cypress",
        action="store_false",
        default=True,
        help="Keep original 76-tri PS2 XO_CT_CYPRESS cone instead of 408-tri botanical replacement.",
    )
    parser.add_argument(
        "--no-botanical-foliage",
        dest="botanical_foliage",
        action="store_false",
        default=True,
        help="Disable v1.4.0 6-archetype all-district botanical foliage BLAS & 512px texture upgrades.",
    )
    parser.add_argument(
        "--no-layout",
        action="store_true",
        help="Skip rendering <district>_layout_check.png.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON manifest to stdout.")
    args = parser.parse_args(argv)

    iso_path = _resolve_iso_path(args.iso)
    targets = ("rosewood", "downtown", "rockport_full") if args.district == "all" else (args.district,)
    results = []
    for dist in targets:
        manifest = export_rockport_district(
            iso_path=iso_path,
            district=dist,
            out_dir=args.out_dir,
            render_layout=not args.no_layout,
            enhance_textures=args.enhance_textures,
            rkpt_version=args.version,
            compress_bc7=args.compress_bc7,
            botanical_cypress=args.botanical_cypress,
            botanical_foliage=args.botanical_foliage,
        )
        results.append(manifest)
        if not args.json:
            fmt_str = "BC7+mips" if manifest["bc7_compressed"] else "RGBA8"
            print(
                f"[{dist.upper()} v{manifest['version']} {fmt_str}] {manifest['rkpt_file']} "
                f"({manifest['rkpt_bytes'] / (1024*1024):.2f} MiB, {manifest['elapsed_seconds']}s) | "
                f"secs={manifest['section_count']} ({manifest['spatial_section_count']}sp+{manifest['panorama_section_count']}pan) "
                f"meshes={manifest['mesh_count']} insts={manifest['instance_count']} "
                f"tex={manifest['texture_resolved_count']}/{manifest['texture_wanted_count']} "
                f"({manifest['texture_resolved_pct']}%, narrow_up={manifest['texture_narrow_axis_upscaled_count']}) | "
                f"roads={manifest['road_node_count']}n/{manifest['road_segment_count']}s "
                f"lights={manifest['world_light_count']} landmarks={len(manifest['landmarks'])} | "
                f"uniq_tris={manifest['unique_triangle_count']:,} inst_tris={manifest['instanced_triangle_count']:,}"
            )

    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
