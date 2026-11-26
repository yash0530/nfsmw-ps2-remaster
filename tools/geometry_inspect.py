#!/usr/bin/env python3
"""geometry_inspect.py — Read-only PS2 NFSMW 0x80134000 Geometry & VIF Mesh Inspector.

Inspects Need for Speed: Most Wanted - Black Edition (PS2, SLUS-21351)
GEOMETRY.BIN packs (CARS\\WHEELS\\GEOMETRY.BIN, CARS\\SPOILER\\GEOMETRY.BIN,
CARS\\<MODEL>\\GEOMETRY.BIN, FRONTEND\\PLATFORMS\\*.BIN) directly from an untouched
ISO (strictly read-only 'rb' via tools/zdir_tool.py) or standalone .BIN files.

Chunk Tree (32-bit Little-Endian):
  0x80134000  GeometryPack (solid list)
    0x80134001  GeometryInfo
      0x00134002  Header (version, solid_count, filename, group_name)
      0x00134003  Solid key hash table
      0x00134004  Solid streaming offset/size descriptors
    0x80134010  SolidPack (one per mesh part / LOD)
      0x00134011  eSolid header (name, hash, polys, AABBMin/Max, pivot, volume)
      0x00134012  Texture table (uint32 tex_hash, uint32 flags)[]
      0x00134013  LightMaterial table (uint32 mat_hash, uint32 flags)[]
      0x0013401A  Position markers / normal smoother table
      0x80134100  PS2 VU1/VIF Mesh Container
        0x00134A00  eSolidPlatInfo (version, strip_flags, num_strips, strip_data_bytes)
        0x00134A01  eStripEntry[num_strips] (16 B each: flags, nv, np, qoff, qsz, tx, lm)
        0x00134A02  PS2 VIF DMA packets (4-blocked int16 pos, int16 UV, int8 norm, R-LFSR ADC)
      0x00134017/18/19  Damage / morph / connectivity data

Supports optional `--export-obj <dir>` (place repo previews only in gitignored `build/`!)
to dump decoded .obj meshes for visual sanity inspection.

Pure Python 3 standard library (zero third-party dependencies).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from zdir_tool import PS2NFSMWArchive  # noqa: E402

CHUNK_PADDING = 0x00000000
CHUNK_GEOM_PACK = 0x80134000
CHUNK_GEOM_INFO = 0x80134001
CHUNK_GEOM_HEADER = 0x00134002
CHUNK_GEOM_KEYS = 0x00134003
CHUNK_GEOM_OFFSETS = 0x00134004
CHUNK_SOLID_PACK = 0x80134010
CHUNK_ESOLID_HDR = 0x00134011
CHUNK_TEX_TABLE = 0x00134012
CHUNK_MAT_TABLE = 0x00134013
CHUNK_POS_MARKERS = 0x0013401A
CHUNK_MESH_CONT = 0x80134100
CHUNK_PLAT_INFO = 0x00134A00
CHUNK_STRIP_TABLE = 0x00134A01
CHUNK_VIF_DATA = 0x00134A02

LOD_SUFFIX_RE = re.compile(r"_([A-E])$")


def _skip_11_padding(data: bytes, pos: int, end: int) -> int:
    """Skip EA bChunk 0x11 alignment padding bytes up to 16-byte alignment."""
    pad = (16 - (pos & 15)) & 15
    if pad and pos + pad <= end and data[pos : pos + pad] == b"\x11" * pad:
        return pos + pad
    while pos < end and data[pos] == 0x11:
        pos += 1
    return pos


def _vu_rnext(r: int) -> int:
    """Advance PS2 VU1 23-bit LFSR R register (RNEXT instruction)."""
    new_bit = ((r >> 4) ^ (r >> 22)) & 1
    return ((r << 1) | new_bit) & 0x7FFFFF


def decode_vu_adc_bits(v1: Tuple[int, int, int, int], num_verts: int) -> List[int]:
    """Decode per-vertex GS ADC (no-draw restart) bits from VU1 RINIT/RNEXT seeds.

    Matches retail SLUS_213.51 `.vutext` (`0x1118..0x1648`) jump-table microprogram:
      - h1 = (num_verts - 1) // 4
      - h1 in 0..2  (nv=1..12) : v1[0] covers all vertices
      - h1 == 3      (nv=13..16): v1[3] covers all vertices
      - h1 in 4..6   (nv=17..28): v1[0] covers first `7 + 4*(h1 - 4)`, then v1[3]
      - h1 >= 7      (nv=29+)   : v1[2] covers first `19`, then v1[3]
    """
    if num_verts <= 0:
        return []
    h1 = (num_verts - 1) // 4

    def gen(r0: int, count: int) -> List[int]:
        out: List[int] = []
        r = r0 & 0x7FFFFF
        for _ in range(count):
            r = _vu_rnext(r)
            out.append((r >> 15) & 1)
        return out

    if h1 <= 2:
        return gen(v1[0], num_verts)
    if h1 == 3:
        return gen(v1[3], num_verts)
    if h1 <= 6:
        sp = min(num_verts, 7 + 4 * (h1 - 4))
        return gen(v1[0], sp) + gen(v1[3], num_verts - sp)
    sp = min(num_verts, 19)
    return gen(v1[2], sp) + gen(v1[3], num_verts - sp)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class StripEntry:
    index: int
    flags: int
    num_verts: int
    num_polys: int
    material_density: float
    data_qword_offset: int
    nz: int
    nt: int
    data_qword_size: int
    poly_group: int
    texture_index: int
    light_material_index: int


@dataclasses.dataclass
class DecodedMesh:
    vertices: List[Tuple[float, float, float]]
    normals: List[Tuple[float, float, float]]
    uvs: List[Tuple[float, float]]
    triangles: List[Tuple[int, int, int, int, int]]  # (i0, i1, i2, tex_idx, mat_idx) 0-based


@dataclasses.dataclass
class SolidPartInfo:
    index: int
    name: str
    name_hash: str
    lod: str
    version: int
    flags: int
    header_polys: int
    num_bones: int
    num_textures: int
    num_light_materials: int
    num_markers: int
    aabb_min: List[float]
    aabb_max: List[float]
    volume: float
    density: float
    texture_hashes: List[str]
    light_material_hashes: List[str]
    plat_version: int
    strip_flags: int
    strip_count: int
    strip_data_bytes: int
    strip_vertex_total: int
    strip_poly_total: int
    chunk_offset: int

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class GeometryPackInfo:
    path: str
    byte_size: int
    pack_version: int
    filename: str
    group_name: str
    header_solid_count: int
    solid_count: int
    total_strips: int
    total_vertices: int
    total_polys: int
    lod_counts: Dict[str, int]
    unique_texture_hashes: int
    unique_material_hashes: int
    aabb_min: Optional[List[float]]
    aabb_max: Optional[List[float]]
    parts: List[SolidPartInfo]

    def to_dict(self, include_parts: bool = False) -> Dict[str, Any]:
        out = dataclasses.asdict(self)
        if not include_parts:
            out.pop("parts", None)
        return out


# ---------------------------------------------------------------------------
# VIF Strip Packet Decoder
# ---------------------------------------------------------------------------


def _unpack_vif_strip(
    buf: bytes, strip: StripEntry
) -> Tuple[List[Tuple[float, float, float]], List[Tuple[float, float, float]], List[Tuple[float, float]], List[int]]:
    """Decode one PS2 VIF DMA strip buffer into vertices, normals, UVs, and ADC bits."""
    p = 0
    v1: Optional[Tuple[int, int, int, int]] = None
    scale: Optional[float] = None
    vec2: Optional[Tuple[float, float, float]] = None
    pos_ints: List[int] = []
    uv_ints: List[int] = []
    norm_bytes = bytearray()

    while p + 4 <= len(buf):
        tag = struct.unpack_from("<I", buf, p)[0]
        p += 4
        cmd = (tag >> 24) & 0xFF
        num = (tag >> 16) & 0xFF
        imm = tag & 0xFFFF
        if cmd == 0x00 or cmd in (0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x13, 0x14, 0x15, 0x17):
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
            usn = (imm >> 14) & 1
            addr = imm & 0x3FF
            cnt = 256 if num == 0 else num
            elem_bits = (32, 16, 8, 16)[vl]
            total_bits = (16 if vl == 3 else (vn + 1) * elem_bits) * cnt
            blen = ((total_bits + 31) // 32) * 4
            if p + blen > len(buf):
                break
            pay = buf[p : p + blen]
            p += blen

            if addr == 1 and blen >= 16:
                v1 = struct.unpack_from("<4I", pay, 0)
                scale = struct.unpack_from("<f", pay, 4)[0]
            elif addr == 2 and blen >= 24:
                vec2 = struct.unpack_from("<3f", pay, 0)
            elif 5 <= addr < 0x1D and vl == 1:
                fmt = f"<{len(pay) // 2}" + ("H" if usn else "h")
                pos_ints.extend(struct.unpack(fmt, pay))
            elif 0x1D <= addr < 0x4D and vl == 1:
                fmt = f"<{len(pay) // 2}" + ("H" if usn else "h")
                uv_ints.extend(struct.unpack(fmt, pay))
            elif addr >= 0x4D and vl == 2:
                norm_bytes.extend(pay)
        else:
            break

    nv = strip.num_verts
    if nv == 0 or not pos_ints or vec2 is None or scale is None:
        return [], [], [], [1] * nv

    # Unpack 4-vertex blocked positions: [X0..b-1, Y0..b-1, Z0..b-1]
    xs: List[int] = []
    ys: List[int] = []
    zs: List[int] = []
    idx = 0
    rem = nv
    while rem > 0 and idx + 3 * min(4, rem) <= len(pos_ints):
        b = min(4, rem)
        xs.extend(pos_ints[idx : idx + b])
        ys.extend(pos_ints[idx + b : idx + 2 * b])
        zs.extend(pos_ints[idx + 2 * b : idx + 3 * b])
        idx += 3 * b
        rem -= b

    # Exact PS2 VU1 hardware position base (SLUS_213.51 .vutext 0x0f48..0x1028):
    #   LQ.xyzw vf8, 2(vi12) -> FTOI0 vf8 -> ITOF0 vf8 -> SUBw.xyz vf8, vf8, vf0
    #   base[ax] = float(int(vec2[ax]) - 1)
    bases = [float(int(vec2[ax]) - 1) for ax in range(3)]

    verts = [
        (bases[0] + x * scale, bases[1] + y * scale, bases[2] + z * scale)
        for x, y, z in zip(xs, ys, zs)
    ]

    # Unpack 4-vertex blocked signed int8 normals (padded to 4 bytes per axis per block)
    norms: List[Tuple[float, float, float]] = []
    nidx = 0
    rem = len(verts)
    while rem > 0 and nidx + 12 <= len(norm_bytes):
        b = min(4, rem)
        nb = struct.unpack_from("<12b", norm_bytes, nidx)
        for j in range(b):
            nx, ny, nz = nb[j] / 127.0, nb[4 + j] / 127.0, nb[8 + j] / 127.0
            mag = (nx * nx + ny * ny + nz * nz) ** 0.5
            if mag > 1e-6:
                norms.append((nx / mag, ny / mag, nz / mag))
            else:
                norms.append((0.0, 0.0, 1.0))
        nidx += 12
        rem -= b
    while len(norms) < len(verts):
        norms.append((0.0, 0.0, 1.0))

    # Unpack UVs (odd vertices have U/V swapped on PS2 VU1)
    uvs: List[Tuple[float, float]] = []
    for i in range(len(verts)):
        if 2 * i + 1 < len(uv_ints):
            a_uv = uv_ints[2 * i] / 4096.0
            b_uv = uv_ints[2 * i + 1] / 4096.0
            if i & 1:
                uvs.append((b_uv, 1.0 - a_uv))
            else:
                uvs.append((a_uv, 1.0 - b_uv))
        else:
            uvs.append((0.0, 0.0))

    adcs = decode_vu_adc_bits(v1, len(verts)) if v1 is not None else [1, 1] + [0] * max(0, len(verts) - 2)
    return verts, norms, uvs, adcs


# ---------------------------------------------------------------------------
# 0x80134000 Chunk Tree Parser
# ---------------------------------------------------------------------------


def _detect_lod(part_name: str) -> str:
    m = LOD_SUFFIX_RE.search(part_name)
    return m.group(1) if m else "other"


def _parse_solid_chunk(
    data: bytes, chunk_off: int, chunk_size: int, part_index: int, decode_mesh: bool = False
) -> Tuple[SolidPartInfo, Optional[DecodedMesh]]:
    p = chunk_off + 8
    end = p + chunk_size

    name = f"PART_{part_index}"
    name_hash = 0
    ver = 0
    flags = 0
    hdr_polys = 0
    num_bones = 0
    num_tex = 0
    num_mat = 0
    num_markers = 0
    aabb_min = [0.0, 0.0, 0.0]
    aabb_max = [0.0, 0.0, 0.0]
    volume = 0.0
    density = 0.0
    tex_hashes: List[str] = []
    mat_hashes: List[str] = []

    plat_ver = 0
    strip_flags = 0
    strips: List[StripEntry] = []
    vif_start = 0
    vif_end = 0
    strip_data_bytes = 0

    while p + 8 <= end:
        cid, csz = struct.unpack_from("<II", data, p)
        pay_off = p + 8
        pay_end = min(end, pay_off + csz)
        if cid == CHUNK_ESOLID_HDR and pay_off + 0xA0 <= pay_end:
            ver, _esw, flags, name_hash, hdr_polys, _hv, num_bones, num_tex, num_mat, num_markers = (
                struct.unpack_from("<BBHIhhbbbb", data, pay_off + 0x0C)
            )
            aabb_min = [round(x, 6) for x in struct.unpack_from("<3f", data, pay_off + 0x20)]
            aabb_max = [round(x, 6) for x in struct.unpack_from("<3f", data, pay_off + 0x30)]
            if pay_off + 0xA0 <= pay_end:
                volume, density = struct.unpack_from("<2f", data, pay_off + 0x98)
            raw_name = data[pay_off + 0xA0 : pay_end].split(b"\x00", 1)[0]
            name = raw_name.decode("ascii", errors="replace") or name
        elif cid == CHUNK_TEX_TABLE:
            for q in range(pay_off, pay_end - 7, 8):
                th = struct.unpack_from("<I", data, q)[0]
                tex_hashes.append(f"0x{th:08x}")
        elif cid == CHUNK_MAT_TABLE:
            for q in range(pay_off, pay_end - 7, 8):
                mh = struct.unpack_from("<I", data, q)[0]
                mat_hashes.append(f"0x{mh:08x}")
        elif cid == CHUNK_MESH_CONT:
            mp = pay_off
            while mp + 8 <= pay_end:
                mcid, mcsz = struct.unpack_from("<II", data, mp)
                mp_off = mp + 8
                mp_end = min(pay_end, mp_off + mcsz)
                if mcid == CHUNK_PLAT_INFO:
                    po = _skip_11_padding(data, mp_off, mp_end)
                    if po + 12 <= mp_end:
                        plat_ver, strip_flags, n_strips, strip_data_bytes = struct.unpack_from("<HHII", data, po)
                elif mcid == CHUNK_STRIP_TABLE:
                    so = _skip_11_padding(data, mp_off, mp_end)
                    idx = 0
                    while so + 16 <= mp_end:
                        fl, nv, np, md, qoff, nz, nt, qsz, pg, tx, lm = struct.unpack_from(
                            "<HBBfHbbBBbb", data, so
                        )
                        strips.append(
                            StripEntry(
                                index=idx,
                                flags=fl,
                                num_verts=nv,
                                num_polys=np,
                                material_density=md,
                                data_qword_offset=qoff,
                                nz=nz,
                                nt=nt,
                                data_qword_size=qsz,
                                poly_group=pg,
                                texture_index=tx,
                                light_material_index=lm,
                            )
                        )
                        idx += 1
                        so += 16
                elif mcid == CHUNK_VIF_DATA:
                    vif_start = _skip_11_padding(data, mp_off, mp_end)
                    vif_end = mp_end
                mp = mp_off + mcsz
        p = pay_off + csz

    mesh: Optional[DecodedMesh] = None
    if decode_mesh and strips and vif_start < vif_end:
        all_v: List[Tuple[float, float, float]] = []
        all_vn: List[Tuple[float, float, float]] = []
        all_vt: List[Tuple[float, float]] = []
        all_tris: List[Tuple[int, int, int, int, int]] = []
        for s in strips:
            s_off = vif_start + s.data_qword_offset * 16
            s_end = min(vif_end, s_off + s.data_qword_size * 16)
            if s_off >= vif_end:
                continue
            vs, vns, vts, adcs = _unpack_vif_strip(data[s_off:s_end], s)
            base_v = len(all_v)
            all_v.extend(vs)
            all_vn.extend(vns)
            all_vt.extend(vts)
            parity = 0
            for i in range(2, len(vs)):
                if adcs[i]:
                    parity = 0
                    continue
                if parity == 0:
                    all_tris.append((base_v + i - 2, base_v + i - 1, base_v + i, s.texture_index, s.light_material_index))
                else:
                    all_tris.append((base_v + i - 1, base_v + i - 2, base_v + i, s.texture_index, s.light_material_index))
                parity ^= 1
        mesh = DecodedMesh(vertices=all_v, normals=all_vn, uvs=all_vt, triangles=all_tris)

    part_info = SolidPartInfo(
        index=part_index,
        name=name,
        name_hash=f"0x{name_hash:08x}",
        lod=_detect_lod(name),
        version=ver,
        flags=flags,
        header_polys=hdr_polys,
        num_bones=num_bones,
        num_textures=num_tex,
        num_light_materials=num_mat,
        num_markers=num_markers,
        aabb_min=aabb_min,
        aabb_max=aabb_max,
        volume=round(volume, 5),
        density=round(density, 5),
        texture_hashes=tex_hashes,
        light_material_hashes=mat_hashes,
        plat_version=plat_ver,
        strip_flags=strip_flags,
        strip_count=len(strips),
        strip_data_bytes=strip_data_bytes,
        strip_vertex_total=sum(s.num_verts for s in strips),
        strip_poly_total=sum(s.num_polys for s in strips),
        chunk_offset=chunk_off,
    )
    return part_info, mesh


def parse_geometry_pack(
    data: bytes,
    path: str = "<buffer>",
    decode_parts: Optional[Sequence[int]] = None,
) -> Tuple[GeometryPackInfo, Dict[int, DecodedMesh]]:
    """Parse an EA 0x80134000 GeometryPack container."""
    if len(data) < 16:
        raise ValueError(f"Geometry pack too small: {len(data)} bytes")
    root_id, root_sz = struct.unpack_from("<II", data, 0)
    if root_id != CHUNK_GEOM_PACK:
        raise ValueError(f"Expected 0x80134000 GeometryPack, got 0x{root_id:08x}")

    pack_end = min(len(data), 8 + root_sz)
    pos = 8
    pack_ver = 0
    hdr_solid_count = 0
    filename = ""
    group_name = ""
    parts: List[SolidPartInfo] = []
    meshes: Dict[int, DecodedMesh] = {}
    decode_set = set(decode_parts) if decode_parts is not None else None
    part_idx = 0

    while pos + 8 <= pack_end:
        cid, csz = struct.unpack_from("<II", data, pos)
        if pos + 8 + csz > len(data):
            break
        if cid == CHUNK_GEOM_INFO:
            p2 = pos + 8
            end2 = p2 + csz
            while p2 + 8 <= end2:
                scid, scsz = struct.unpack_from("<II", data, p2)
                if scid == CHUNK_GEOM_HEADER and p2 + 8 + 0x50 <= p2 + 8 + scsz:
                    pay = p2 + 8
                    pack_ver, hdr_solid_count = struct.unpack_from("<II", data, pay + 8)
                    filename = data[pay + 0x10 : pay + 0x48].split(b"\x00", 1)[0].decode("ascii", errors="replace")
                    group_name = data[pay + 0x48 : pay + 0x68].split(b"\x00", 1)[0].decode("ascii", errors="replace")
                p2 += 8 + scsz
        elif cid == CHUNK_SOLID_PACK:
            should_decode = decode_set is not None and part_idx in decode_set
            part_info, mesh = _parse_solid_chunk(data, pos, csz, part_idx, decode_mesh=should_decode)
            parts.append(part_info)
            if mesh is not None:
                meshes[part_idx] = mesh
            part_idx += 1
        pos += 8 + csz

    lod_counts: Dict[str, int] = {}
    tex_set = set()
    mat_set = set()
    overall_min: Optional[List[float]] = None
    overall_max: Optional[List[float]] = None
    for pinfo in parts:
        lod_counts[pinfo.lod] = lod_counts.get(pinfo.lod, 0) + 1
        tex_set.update(pinfo.texture_hashes)
        mat_set.update(pinfo.light_material_hashes)
        if overall_min is None:
            overall_min = list(pinfo.aabb_min)
            overall_max = list(pinfo.aabb_max)
        else:
            for ax in range(3):
                overall_min[ax] = min(overall_min[ax], pinfo.aabb_min[ax])
                overall_max[ax] = max(overall_max[ax], pinfo.aabb_max[ax])

    pack_info = GeometryPackInfo(
        path=path,
        byte_size=len(data),
        pack_version=pack_ver,
        filename=filename,
        group_name=group_name,
        header_solid_count=hdr_solid_count,
        solid_count=len(parts),
        total_strips=sum(p.strip_count for p in parts),
        total_vertices=sum(p.strip_vertex_total for p in parts),
        total_polys=sum(p.strip_poly_total for p in parts),
        lod_counts=lod_counts,
        unique_texture_hashes=len(tex_set),
        unique_material_hashes=len(mat_set),
        aabb_min=[round(x, 4) for x in overall_min] if overall_min else None,
        aabb_max=[round(x, 4) for x in overall_max] if overall_max else None,
        parts=parts,
    )
    return pack_info, meshes


def decode_pack_part_mesh(data: bytes, part_index: int) -> Tuple[SolidPartInfo, DecodedMesh]:
    """Parse and decode the full VIF mesh for a single part index inside a GeometryPack."""
    pack_info, meshes = parse_geometry_pack(data, decode_parts=[part_index])
    if part_index not in meshes:
        raise IndexError(f"Part index {part_index} not found or has no VIF mesh (parts={pack_info.solid_count})")
    return pack_info.parts[part_index], meshes[part_index]


def write_obj_mesh(out_path: Path, part_name: str, mesh: DecodedMesh) -> Path:
    """Write a DecodedMesh cleanly to Wavefront .obj format."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        f"# NFSMW PS2 Geometry Preview: {part_name}",
        f"# Vertices: {len(mesh.vertices)}, Triangles: {len(mesh.triangles)}",
        f"o {part_name}",
    ]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    for u, v in mesh.uvs:
        lines.append(f"vt {u:.6f} {v:.6f}")
    for nx, ny, nz in mesh.normals:
        lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")

    cur_tex = -999
    for i0, i1, i2, tx, _lm in mesh.triangles:
        if tx != cur_tex:
            cur_tex = tx
            lines.append(f"g {part_name}_tex{tx}")
        a, b, c = i0 + 1, i1 + 1, i2 + 1
        lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def select_export_parts(
    pack_info: GeometryPackInfo,
    lod_mode: str = "auto",
    part_filter: str = "",
    max_parts: int = 4,
) -> List[int]:
    """Select part indices for OBJ export matching LOD / substring criteria."""
    parts = pack_info.parts
    if part_filter:
        needle = part_filter.lower()
        chosen = [p.index for p in parts if needle in p.name.lower()]
    elif lod_mode == "all":
        chosen = [p.index for p in parts]
    elif lod_mode in ("A", "B", "C", "D", "E"):
        chosen = [p.index for p in parts if p.lod == lod_mode]
    else:
        # "auto": pick LOD A parts first, plus highest-poly LOD B body/part if no non-decal LOD A exists
        lod_a = [p for p in parts if p.lod == "A"]
        non_decal_a = [p for p in lod_a if "DECAL" not in p.name.upper()]
        if non_decal_a:
            chosen = [p.index for p in non_decal_a]
        else:
            lod_b = sorted([p for p in parts if p.lod == "B"], key=lambda x: x.strip_poly_total, reverse=True)
            picked = lod_b[:1] + lod_a[: max(0, max_parts - 1)]
            if not picked and parts:
                picked = [max(parts, key=lambda x: x.strip_poly_total)]
            chosen = [p.index for p in picked]
    return chosen[:max_parts] if max_parts > 0 else chosen


# ---------------------------------------------------------------------------
# High-Level ISO & File Inspection
# ---------------------------------------------------------------------------


def inspect_iso(
    iso_path: Path,
    name_filter: str = "",
    lod_filter: str = "",
    export_dir: Optional[Path] = None,
    lod_export_mode: str = "auto",
    part_filter: str = "",
    max_export_parts: int = 3,
    cars_only: bool = False,
) -> Dict[str, Any]:
    """Inspect all 0x80134000 Geometry packs in an untouched PS2 ISO."""
    arc = PS2NFSMWArchive(iso_path)
    needle = name_filter.lower().strip()

    packs: List[GeometryPackInfo] = []
    exported_files: List[Dict[str, Any]] = []

    for e in arc.entries:
        if e.magic_hex != "00401380":
            continue
        label = e.resolved_path or f"<hash:0x{e.name_hash:08x}>"
        if cars_only and not label.upper().startswith("CARS\\"):
            continue
        if needle and needle not in label.lower():
            continue

        data = arc.read_entry(e)
        pack_info, _ = parse_geometry_pack(data, path=label)

        if export_dir is not None and (not exported_files or needle or part_filter):
            selected = select_export_parts(
                pack_info, lod_mode=lod_export_mode, part_filter=part_filter, max_parts=max_export_parts
            )
            if selected:
                _, meshes = parse_geometry_pack(data, path=label, decode_parts=selected)
                pack_slug = re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_").lower()
                for pidx in selected:
                    if pidx not in meshes:
                        continue
                    part = pack_info.parts[pidx]
                    mesh = meshes[pidx]
                    obj_name = f"{pack_slug}__{part.name}.obj"
                    out_file = write_obj_mesh(export_dir / obj_name, part.name, mesh)
                    exported_files.append(
                        {
                            "pack": label,
                            "part": part.name,
                            "lod": part.lod,
                            "vertices": len(mesh.vertices),
                            "triangles": len(mesh.triangles),
                            "path": str(out_file),
                        }
                    )

        if lod_filter:
            pack_info.parts = [p for p in pack_info.parts if p.lod.upper() == lod_filter.upper()]
        packs.append(pack_info)

    lod_totals: Dict[str, int] = {}
    for pk in packs:
        for k, v in pk.lod_counts.items():
            lod_totals[k] = lod_totals.get(k, 0) + v

    return {
        "source": str(iso_path),
        "summary": {
            "pack_count": len(packs),
            "car_pack_count": sum(1 for pk in packs if pk.path.upper().startswith("CARS\\")),
            "total_solids": sum(pk.solid_count for pk in packs),
            "total_strips": sum(pk.total_strips for pk in packs),
            "total_vertices": sum(pk.total_vertices for pk in packs),
            "total_polys": sum(pk.total_polys for pk in packs),
            "lod_counts": lod_totals,
        },
        "packs": packs,
        "exported_objs": exported_files,
    }


def inspect_standalone_file(
    bin_path: Path,
    export_dir: Optional[Path] = None,
    lod_export_mode: str = "auto",
    part_filter: str = "",
    max_export_parts: int = 4,
) -> Dict[str, Any]:
    """Inspect a standalone extracted GEOMETRY.BIN file."""
    data = bin_path.read_bytes()
    pack_info, _ = parse_geometry_pack(data, path=str(bin_path))
    exported_files: List[Dict[str, Any]] = []
    if export_dir is not None:
        selected = select_export_parts(
            pack_info, lod_mode=lod_export_mode, part_filter=part_filter, max_parts=max_export_parts
        )
        _, meshes = parse_geometry_pack(data, path=str(bin_path), decode_parts=selected)
        for pidx in selected:
            if pidx not in meshes:
                continue
            part = pack_info.parts[pidx]
            mesh = meshes[pidx]
            out_file = write_obj_mesh(export_dir / f"{part.name}.obj", part.name, mesh)
            exported_files.append(
                {
                    "pack": str(bin_path),
                    "part": part.name,
                    "lod": part.lod,
                    "vertices": len(mesh.vertices),
                    "triangles": len(mesh.triangles),
                    "path": str(out_file),
                }
            )
    return {
        "source": str(bin_path),
        "summary": {
            "pack_count": 1,
            "car_pack_count": 1,
            "total_solids": pack_info.solid_count,
            "total_strips": pack_info.total_strips,
            "total_vertices": pack_info.total_vertices,
            "total_polys": pack_info.total_polys,
            "lod_counts": pack_info.lod_counts,
        },
        "packs": [pack_info],
        "exported_objs": exported_files,
    }


def _print_summary(report: Dict[str, Any], show_parts: bool = False, limit: int = 30) -> None:
    s = report["summary"]
    print(f"PS2 NFSMW 0x80134000 Geometry Inspection: {report['source']}")
    print(
        f"  Packs: {s['pack_count']} (cars/parts={s['car_pack_count']}) | "
        f"Solids: {s['total_solids']:,} | Strips: {s['total_strips']:,} | "
        f"Verts: {s['total_vertices']:,} | Polys: {s['total_polys']:,}"
    )
    print(f"  LOD breakdown: {s['lod_counts']}")

    packs: List[GeometryPackInfo] = report["packs"]
    print(f"\nGeometry Packs (showing {min(len(packs), limit)}/{len(packs)}):")
    for pk in packs[:limit]:
        lods = ",".join(f"{k}:{v}" for k, v in sorted(pk.lod_counts.items()))
        bbox = ""
        if pk.aabb_min and pk.aabb_max:
            bbox = (
                f"  bbox=[{pk.aabb_min[0]:.2f}..{pk.aabb_max[0]:.2f}, "
                f"{pk.aabb_min[1]:.2f}..{pk.aabb_max[1]:.2f}, {pk.aabb_min[2]:.2f}..{pk.aabb_max[2]:.2f}]"
            )
        print(
            f"  {pk.path:42s}  {pk.byte_size:10,d} B  solids={pk.solid_count:3d}  "
            f"strips={pk.total_strips:5,d}  verts={pk.total_vertices:6,d}  polys={pk.total_polys:6,d}  [{lods}]{bbox}"
        )
        if show_parts:
            for part in pk.parts[:limit]:
                print(
                    f"    [{part.index:3d}] {part.name:44s} LOD={part.lod:5s}  "
                    f"strips={part.strip_count:3d}  v={part.strip_vertex_total:5d}  "
                    f"p={part.strip_poly_total:5d}  tex={len(part.texture_hashes):2d}  "
                    f"mat={len(part.light_material_hashes):2d}"
                )

    if report["exported_objs"]:
        print(f"\nExported Preview OBJ Meshes ({len(report['exported_objs'])}):")
        for ex in report["exported_objs"]:
            print(
                f"  {ex['path']}  ({ex['part']} LOD={ex['lod']}: "
                f"{ex['vertices']:,} verts, {ex['triangles']:,} tris)"
            )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only PS2 NFSMW 0x80134000 geometry pack & VIF mesh inspector."
    )
    parser.add_argument("source", nargs="?", type=Path, default=None, help="Path to PS2 .iso or extracted GEOMETRY.BIN")
    parser.add_argument("--iso", type=Path, default=None, help="Path to PS2 .iso (strictly read-only 'rb')")
    parser.add_argument("--in", dest="in_file", type=Path, default=None, help="Path to standalone GEOMETRY.BIN")
    parser.add_argument("--filter", type=str, default="", help="Substring filter on virtual path (e.g. WHEELS, BMWM3)")
    parser.add_argument("--cars-only", action="store_true", help="Inspect only CARS\\* geometry packs")
    parser.add_argument("--parts", action="store_true", help="Show per-solid part breakdown")
    parser.add_argument("--lod", type=str, default="", help="Filter parts display by LOD (A/B/C/D/E)")
    parser.add_argument(
        "--export-obj",
        type=Path,
        default=None,
        help="Optional output directory (use gitignored build/obj_preview/) to export decoded .obj meshes",
    )
    parser.add_argument(
        "--export-lod",
        choices=("auto", "A", "B", "C", "D", "E", "all"),
        default="auto",
        help="LOD selection for --export-obj (default: auto)",
    )
    parser.add_argument("--part", type=str, default="", help="Part name substring filter for --export-obj")
    parser.add_argument("--max-export-parts", type=int, default=3, help="Max parts to export per pack (default: 3)")
    parser.add_argument("--limit", type=int, default=30, help="Max packs/parts shown in text output")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    source = args.iso or args.in_file or args.source
    if source is None:
        parser.error("Specify a source .iso or GEOMETRY.BIN (positional, --iso, or --in).")

    if source.suffix.lower() == ".iso":
        report = inspect_iso(
            source,
            name_filter=args.filter,
            lod_filter=args.lod,
            export_dir=args.export_obj,
            lod_export_mode=args.export_lod,
            part_filter=args.part,
            max_export_parts=args.max_export_parts,
            cars_only=args.cars_only,
        )
    else:
        report = inspect_standalone_file(
            source,
            export_dir=args.export_obj,
            lod_export_mode=args.export_lod,
            part_filter=args.part,
            max_export_parts=args.max_export_parts,
        )

    if args.json:
        payload = {
            "source": report["source"],
            "summary": report["summary"],
            "packs": [pk.to_dict(include_parts=args.parts) for pk in report["packs"]],
            "exported_objs": report["exported_objs"],
        }
        print(json.dumps(payload, indent=2))
    else:
        _print_summary(report, show_parts=args.parts, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
