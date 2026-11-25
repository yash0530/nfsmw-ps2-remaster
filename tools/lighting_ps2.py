#!/usr/bin/env python3
"""
lighting_ps2.py — Pure-Python PS2 NFSMW lighting inspector & verified patcher
for `GLOBAL\\GLOBALB.LZC` (`SunInfos`, `LightMaterials`, `SunTrack`),
`GLOBAL\\GLOBALMEMORYFILE.BIN`, and `GLOBAL\\ATTRIBUTES.BIN` (`visualrgbtweaker`
/ `visuallook`).

Reads directly from an untouched PS2 ISO (strictly read-only `'rb'`) or from
extracted `GLOBALB.LZC` / `GLOBALB.BUN` / `GLOBALMEMORYFILE.BIN` files.
Re-compresses patched `GLOBALB.BUN` streams with EA JDLZ v2 (verified via
`tools/jdlz.py`) and updates the embedded `GLOBAL\\GLOBALB.LZC` entry inside
`GLOBAL\\GLOBALMEMORYFILE.BIN` cleanly (in-place when fitting within the slot,
or rebuilding shifted entry offsets cleanly if enlarged).

Clean-room format reference: SLUS_213.51 retail EE disassembly (`LoaderSun`,
`SetCurrentSunInfo`, `StuffSun`, `LoaderLights`, `UpdatePlatInfo`,
`AddMemoryFile`, `RenderMWVisualLook`) and CC0 `dbalatoni13/nfsmw`.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from jdlz import (
    FAR_MAX_LEN,
    FAR_MAX_OFFSET,
    FAR_MIN_OFFSET,
    JDLZ_HEADER_SIZE,
    JDLZ_MAGIC,
    JDLZ_VERSION,
    NEAR_MAX_LEN,
    NEAR_MAX_OFFSET,
    _match_length,
    is_jdlz,
    jdlz_compress,
    jdlz_decompress,
    lz_decompress,
    parse_lz_header,
)
from zdir_tool import PS2NFSMWArchive, bstringhash

# ---------------------------------------------------------------------------
# EA Black Box bChunk & Struct Constants (PS2 SLUS-21351, Little-Endian)
# ---------------------------------------------------------------------------

BCHUNK_LIGHT_MATERIALS = 0x00135200
BCHUNK_SUN_TRACK = 0x00034201
BCHUNK_SUN_INFOS = 0x00034202
BCHUNK_WEATHERMAN = 0x00034250

SUN_INFO_SIZE = 272  # 0x110 bytes per SunChunkInfo
SUN_INFO_HEADER_SIZE = 56  # 0x38 bytes before SunLayers[6]
SUN_LAYER_COUNT = 6
SUN_LAYER_SIZE = 36  # 0x24 bytes per SunLayer
SUN_INFO_VERSION = 2

LIGHT_MATERIAL_PAYLOAD_SIZE = 168  # 0xA8 bytes per eLightMaterial
LIGHT_MATERIAL_VERSION = 3

SUN_TRACK_RECORD_SIZE = 288  # 0x120 bytes per track entry in 0x00034201

MEMORY_FILE_MAGIC = 0x53219999
GLOBALB_LZC_VPATH = r"GLOBAL\GLOBALB.LZC"
GLOBALB_LZC_HASH = bstringhash(GLOBALB_LZC_VPATH)

SUN_TEXTURE_NAMES: Dict[int, str] = {
    0: "SUNTEX_CENTER",
    1: "SUNTEX_HALO",
    2: "SUNTEX_MAJORRAYS",
    3: "SUNTEX_MINORRAYS",
    4: "SUNTEX_RING",
}

SUN_ALPHA_NAMES: Dict[int, str] = {
    0: "SUNALPHA_BLEND",
    1: "SUNALPHA_ADD",
}

# Field map inside eLightMaterial (relative to payload start, +0x30 = feData)
LIGHT_MATERIAL_FLOAT_FIELDS: Tuple[Tuple[str, int], ...] = (
    ("DiffuseMinScale", 0x30),
    ("DiffuseMinR", 0x34),
    ("DiffuseMinG", 0x38),
    ("DiffuseMinB", 0x3C),
    ("DiffuseMaxScale", 0x40),
    ("DiffuseMaxR", 0x44),
    ("DiffuseMaxG", 0x48),
    ("DiffuseMaxB", 0x4C),
    ("DiffuseMinA", 0x50),
    ("DiffuseMaxA", 0x54),
    ("SpecularPower", 0x58),
    ("SpecularMinScale", 0x5C),
    ("SpecularMinR", 0x60),
    ("SpecularMinG", 0x64),
    ("SpecularMinB", 0x68),
    ("SpecularMaxScale", 0x6C),
    ("SpecularMaxR", 0x70),
    ("SpecularMaxG", 0x74),
    ("SpecularMaxB", 0x78),
    ("EnvmapPower", 0x7C),
    ("EnvmapMinScale", 0x80),
    ("EnvmapMinR", 0x84),
    ("EnvmapMinG", 0x88),
    ("EnvmapMinB", 0x8C),
    ("EnvmapMaxScale", 0x90),
    ("EnvmapMaxR", 0x94),
    ("EnvmapMaxG", 0x98),
    ("EnvmapMaxB", 0x9C),
    ("MetallicScale", 0xA0),
    ("SpecularHotSpot", 0xA4),
)

LIGHT_MATERIAL_FLOAT_OFFSETS: Dict[str, int] = dict(LIGHT_MATERIAL_FLOAT_FIELDS)

LIGHT_MATERIAL_VEC3_OFFSETS: Dict[str, int] = {
    "DiffuseMinRGB": 0x34,
    "DiffuseMaxRGB": 0x44,
    "SpecularMinRGB": 0x60,
    "SpecularMaxRGB": 0x70,
    "EnvmapMinRGB": 0x84,
    "EnvmapMaxRGB": 0x94,
}

# Field map inside SunChunkInfo (relative to 272-byte SunChunkInfo start)
SUN_HEADER_FIELDS: Dict[str, Tuple[int, str]] = {
    "Position": (0x20, "vec3"),
    "PositionX": (0x20, "float"),
    "PositionY": (0x24, "float"),
    "PositionZ": (0x28, "float"),
    "CarShadowPosition": (0x2C, "vec3"),
    "CarShadowPositionX": (0x2C, "float"),
    "CarShadowPositionY": (0x30, "float"),
    "CarShadowPositionZ": (0x34, "float"),
}

SUN_LAYER_FIELDS: Dict[str, Tuple[int, str]] = {
    "Texture": (0x00, "int32"),
    "AlphaType": (0x04, "int32"),
    "IntensityScale": (0x08, "float"),
    "Size": (0x0C, "float"),
    "OffsetX": (0x10, "float"),
    "OffsetY": (0x14, "float"),
    "Colour": (0x18, "rgba8"),
    "Angle": (0x1C, "int32"),
    "SweepAngleAmount": (0x20, "float"),
}


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BChunkRecord:
    index: int
    chunk_id: int
    offset: int
    payload_offset: int
    size: int


@dataclasses.dataclass
class SunLayerInfo:
    layer_index: int
    file_offset: int
    texture: int
    alpha_type: int
    intensity_scale: float
    size: float
    offset_x: float
    offset_y: float
    colour: List[int]
    angle: int
    sweep_angle_amount: float

    def is_active(self) -> bool:
        return any(
            (
                self.texture != 0,
                self.alpha_type != 0,
                self.intensity_scale != 0.0,
                self.size != 0.0,
                self.offset_x != 0.0,
                self.offset_y != 0.0,
                any(self.colour),
                self.angle != 0,
                self.sweep_angle_amount != 0.0,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_index": self.layer_index,
            "offset": f"0x{self.file_offset:08x}",
            "Texture": self.texture,
            "TextureName": SUN_TEXTURE_NAMES.get(self.texture, f"UNKNOWN({self.texture})"),
            "AlphaType": self.alpha_type,
            "AlphaTypeName": SUN_ALPHA_NAMES.get(self.alpha_type, f"UNKNOWN({self.alpha_type})"),
            "IntensityScale": round(self.intensity_scale, 6),
            "Size": round(self.size, 6),
            "OffsetX": round(self.offset_x, 6),
            "OffsetY": round(self.offset_y, 6),
            "Colour": list(self.colour),
            "Angle": self.angle,
            "SweepAngleAmount": round(self.sweep_angle_amount, 6),
        }


@dataclasses.dataclass
class SunInfoProfile:
    index: int
    file_offset: int
    version: int
    name_hash: int
    name: str
    position: List[float]
    car_shadow_position: List[float]
    layers: List[SunLayerInfo]

    def to_dict(self, include_inactive_layers: bool = False) -> Dict[str, Any]:
        shown = self.layers if include_inactive_layers else [l for l in self.layers if l.is_active()]
        return {
            "index": self.index,
            "offset": f"0x{self.file_offset:08x}",
            "version": self.version,
            "name_hash": f"0x{self.name_hash:08x}",
            "name": self.name,
            "offsets": {
                "Position": f"0x{self.file_offset + 0x20:08x}",
                "CarShadowPosition": f"0x{self.file_offset + 0x2C:08x}",
                "SunLayers": f"0x{self.file_offset + SUN_INFO_HEADER_SIZE:08x}",
            },
            "Position": [round(x, 6) for x in self.position],
            "CarShadowPosition": [round(x, 6) for x in self.car_shadow_position],
            "SunLayers": [l.to_dict() for l in shown],
        }


@dataclasses.dataclass
class LightMaterialInfo:
    index: int
    chunk_offset: int
    payload_offset: int
    name_hash: int
    version: int
    name: str
    fields: Dict[str, float]

    def field_offset(self, field_name: str) -> int:
        if field_name in LIGHT_MATERIAL_FLOAT_OFFSETS:
            return self.payload_offset + LIGHT_MATERIAL_FLOAT_OFFSETS[field_name]
        if field_name in LIGHT_MATERIAL_VEC3_OFFSETS:
            return self.payload_offset + LIGHT_MATERIAL_VEC3_OFFSETS[field_name]
        raise KeyError(f"Unknown LightMaterial field: {field_name!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "chunk_offset": f"0x{self.chunk_offset:08x}",
            "payload_offset": f"0x{self.payload_offset:08x}",
            "name_hash": f"0x{self.name_hash:08x}",
            "version": self.version,
            "name": self.name,
            "field_offsets": {
                name: f"0x{self.payload_offset + off:08x}"
                for name, off in LIGHT_MATERIAL_FLOAT_FIELDS
            },
            "fields": {name: round(val, 6) for name, val in self.fields.items()},
        }


@dataclasses.dataclass
class SunTrackEntry:
    index: int
    file_offset: int
    title: str
    location: str
    tag: str
    region: str
    sun_hash: int
    sun_name: Optional[str]
    track_id: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "offset": f"0x{self.file_offset:08x}",
            "tag": self.tag,
            "title": self.title,
            "location": self.location,
            "region": self.region,
            "sun_hash": f"0x{self.sun_hash:08x}",
            "sun_name": self.sun_name,
            "track_id": self.track_id,
        }


@dataclasses.dataclass
class MemoryFileEntryInfo:
    index: int
    name_hash: int
    offset: int
    file_size: int
    memory_size: int
    data_ptr: int
    slot_capacity: int


# ---------------------------------------------------------------------------
# bChunk & Lighting Binary Parsers
# ---------------------------------------------------------------------------


def iter_top_level_bchunks(data: bytes) -> List[BChunkRecord]:
    """Walk top-level little-endian `{uint32 id, uint32 size}` bChunk records."""
    chunks: List[BChunkRecord] = []
    pos = 0
    n = len(data)
    idx = 0
    while pos + 8 <= n:
        cid, csz = struct.unpack_from("<II", data, pos)
        if pos + 8 + csz > n:
            raise ValueError(
                f"Truncated bChunk #{idx} (id=0x{cid:08x}) at 0x{pos:08x}: "
                f"size={csz} exceeds buffer length {n}"
            )
        chunks.append(
            BChunkRecord(
                index=idx,
                chunk_id=cid,
                offset=pos,
                payload_offset=pos + 8,
                size=csz,
            )
        )
        pos += 8 + csz
        idx += 1
    if pos != n:
        raise ValueError(f"Trailing {n - pos} bytes after bChunk stream at 0x{pos:08x}")
    return chunks


def parse_sun_infos(bun_data: bytes, chunks: Optional[Sequence[BChunkRecord]] = None) -> List[SunInfoProfile]:
    """Parse the `0x00034202` `SunInfos` chunk inside uncompressed `GLOBALB.BUN`."""
    if chunks is None:
        chunks = iter_top_level_bchunks(bun_data)

    sun_chunks = [c for c in chunks if c.chunk_id == BCHUNK_SUN_INFOS]
    if not sun_chunks:
        raise KeyError("Missing SunInfos chunk (0x00034202) in GLOBALB stream")
    if len(sun_chunks) > 1:
        raise ValueError(f"Expected 1 SunInfos chunk, found {len(sun_chunks)}")

    sc = sun_chunks[0]
    # Matches retail SLUS_213.51 LoaderSun (0x001FAC58): ((chunk + 0x17) & -16)
    aligned_start = (sc.offset + 0x17) & ~0x0F
    aligned_size = sc.size - (aligned_start - sc.payload_offset)
    if aligned_size < SUN_INFO_SIZE:
        raise ValueError(f"SunInfos chunk too small ({aligned_size} bytes)")

    count = aligned_size // SUN_INFO_SIZE
    profiles: List[SunInfoProfile] = []
    for i in range(count):
        base = aligned_start + i * SUN_INFO_SIZE
        version, name_hash = struct.unpack_from("<II", bun_data, base)
        if version != SUN_INFO_VERSION:
            raise ValueError(
                f"Unexpected SunInfo[{i}] version {version} at 0x{base:08x} (expected {SUN_INFO_VERSION})"
            )
        raw_name = bun_data[base + 8 : base + 32]
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        pos = list(struct.unpack_from("<3f", bun_data, base + 0x20))
        car_pos = list(struct.unpack_from("<3f", bun_data, base + 0x2C))

        layers: List[SunLayerInfo] = []
        for li in range(SUN_LAYER_COUNT):
            lbase = base + SUN_INFO_HEADER_SIZE + li * SUN_LAYER_SIZE
            tex, atype, iscale, sz, ox, oy = struct.unpack_from("<II4f", bun_data, lbase)
            rgba = list(struct.unpack_from("<4B", bun_data, lbase + 0x18))
            ang, sweep = struct.unpack_from("<if", bun_data, lbase + 0x1C)
            layers.append(
                SunLayerInfo(
                    layer_index=li,
                    file_offset=lbase,
                    texture=tex,
                    alpha_type=atype,
                    intensity_scale=iscale,
                    size=sz,
                    offset_x=ox,
                    offset_y=oy,
                    colour=rgba,
                    angle=ang,
                    sweep_angle_amount=sweep,
                )
            )
        profiles.append(
            SunInfoProfile(
                index=i,
                file_offset=base,
                version=version,
                name_hash=name_hash,
                name=name,
                position=pos,
                car_shadow_position=car_pos,
                layers=layers,
            )
        )
    return profiles


def parse_light_materials(
    bun_data: bytes, chunks: Optional[Sequence[BChunkRecord]] = None
) -> List[LightMaterialInfo]:
    """Parse all `0x00135200` `eLightMaterial` chunks inside uncompressed `GLOBALB.BUN`."""
    if chunks is None:
        chunks = iter_top_level_bchunks(bun_data)

    mats: List[LightMaterialInfo] = []
    for idx, ch in enumerate(c for c in chunks if c.chunk_id == BCHUNK_LIGHT_MATERIALS):
        if ch.size != LIGHT_MATERIAL_PAYLOAD_SIZE:
            raise ValueError(
                f"LightMaterial[{idx}] at 0x{ch.offset:08x} has size {ch.size} "
                f"(expected {LIGHT_MATERIAL_PAYLOAD_SIZE})"
            )
        base = ch.payload_offset
        name_hash, version = struct.unpack_from("<II", bun_data, base + 0x0C)
        if version != LIGHT_MATERIAL_VERSION:
            raise ValueError(
                f"LightMaterial[{idx}] at 0x{base:08x} has version {version} "
                f"(expected {LIGHT_MATERIAL_VERSION})"
            )
        raw_name = bun_data[base + 0x14 : base + 0x30]
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        vals = struct.unpack_from("<30f", bun_data, base + 0x30)
        field_map = {fname: vals[i] for i, (fname, _) in enumerate(LIGHT_MATERIAL_FLOAT_FIELDS)}
        mats.append(
            LightMaterialInfo(
                index=idx,
                chunk_offset=ch.offset,
                payload_offset=base,
                name_hash=name_hash,
                version=version,
                name=name,
                fields=field_map,
            )
        )
    return mats


def parse_sun_track(
    bun_data: bytes,
    sun_profiles: Sequence[SunInfoProfile],
    chunks: Optional[Sequence[BChunkRecord]] = None,
) -> List[SunTrackEntry]:
    """Parse the `0x00034201` track-to-SunInfo assignment table inside `GLOBALB.BUN`."""
    if chunks is None:
        chunks = iter_top_level_bchunks(bun_data)
    hits = [c for c in chunks if c.chunk_id == BCHUNK_SUN_TRACK]
    if not hits:
        return []
    ch = hits[0]
    sun_by_hash = {p.name_hash: p.name for p in sun_profiles}
    count = ch.size // SUN_TRACK_RECORD_SIZE
    out: List[SunTrackEntry] = []
    for i in range(count):
        base = ch.payload_offset + i * SUN_TRACK_RECORD_SIZE
        title = bun_data[base : base + 32].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        loc = bun_data[base + 32 : base + 64].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        tag = bun_data[base + 64 : base + 72].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        reg = bun_data[base + 72 : base + 96].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        sun_hash, track_id = struct.unpack_from("<II", bun_data, base + 0x90)
        out.append(
            SunTrackEntry(
                index=i,
                file_offset=base,
                title=title,
                location=loc,
                tag=tag,
                region=reg,
                sun_hash=sun_hash,
                sun_name=sun_by_hash.get(sun_hash),
                track_id=track_id,
            )
        )
    return out


class GlobalBDatabase:
    """In-memory index of decoded `SunInfos`, `LightMaterials`, and `SunTrack` from `GLOBALB.BUN`."""

    def __init__(self, bun_data: bytes):
        self.bun_data = bun_data
        self.chunks = iter_top_level_bchunks(bun_data)
        self.sun_infos = parse_sun_infos(bun_data, self.chunks)
        self.light_materials = parse_light_materials(bun_data, self.chunks)
        self.sun_track = parse_sun_track(bun_data, self.sun_infos, self.chunks)
        self.sun_by_name = {s.name.upper(): s for s in self.sun_infos}
        self.sun_by_hash = {s.name_hash: s for s in self.sun_infos}
        self.mat_by_name = {m.name.upper(): m for m in self.light_materials}
        self.mat_by_hash = {m.name_hash: m for m in self.light_materials}

    def find_sun(self, ref: str) -> SunInfoProfile:
        key = ref.strip()
        up = key.upper()
        if up in self.sun_by_name:
            return self.sun_by_name[up]
        if key.lower().startswith("0x"):
            h = int(key, 16)
            if h in self.sun_by_hash:
                return self.sun_by_hash[h]
        raise KeyError(f"SunInfo profile not found: {ref!r}")

    def find_material(self, ref: str) -> LightMaterialInfo:
        key = ref.strip()
        up = key.upper()
        if up in self.mat_by_name:
            return self.mat_by_name[up]
        if key.lower().startswith("0x"):
            h = int(key, 16)
            if h in self.mat_by_hash:
                return self.mat_by_hash[h]
        raise KeyError(f"LightMaterial not found: {ref!r}")


# ---------------------------------------------------------------------------
# EA MemoryFile (`GLOBAL\GLOBALMEMORYFILE.BIN`) Inspection & Updating
# ---------------------------------------------------------------------------


def parse_memory_file(memfile_data: bytes) -> List[MemoryFileEntryInfo]:
    """Parse EA MemoryFile (`0x53219999`) directory entries and slot capacities."""
    if len(memfile_data) < 16:
        raise ValueError(f"MemoryFile too small ({len(memfile_data)} bytes)")
    _n_next, _n_prev, magic, count = struct.unpack_from("<4I", memfile_data, 0)
    if magic != MEMORY_FILE_MAGIC:
        raise ValueError(
            f"Invalid MemoryFile magic 0x{magic:08x} (expected 0x{MEMORY_FILE_MAGIC:08x})"
        )
    dir_end = 16 + count * 20
    if dir_end > len(memfile_data):
        raise ValueError(f"Truncated MemoryFile directory ({count} entries > {len(memfile_data)} bytes)")

    raw_rows: List[Tuple[int, int, int, int, int, int]] = []
    for i in range(count):
        h, off, fsz, msz, ptr = struct.unpack_from("<5I", memfile_data, 16 + i * 20)
        raw_rows.append((i, h, off, fsz, msz, ptr))

    by_off = sorted(raw_rows, key=lambda r: r[2])
    slot_cap_by_idx: Dict[int, int] = {}
    for pos_i, row in enumerate(by_off):
        idx, _h, off, _fsz, _msz, _ptr = row
        next_off = by_off[pos_i + 1][2] if pos_i + 1 < len(by_off) else len(memfile_data)
        slot_cap_by_idx[idx] = max(0, next_off - off)

    return [
        MemoryFileEntryInfo(
            index=idx,
            name_hash=h,
            offset=off,
            file_size=fsz,
            memory_size=msz,
            data_ptr=ptr,
            slot_capacity=slot_cap_by_idx[idx],
        )
        for idx, h, off, fsz, msz, ptr in raw_rows
    ]


def inspect_globalb_in_memfile(memfile_data: bytes) -> Dict[str, Any]:
    """Inspect how `GLOBAL\\GLOBALB.LZC` is embedded inside `GLOBAL\\GLOBALMEMORYFILE.BIN`."""
    entries = parse_memory_file(memfile_data)
    hits = [e for e in entries if e.name_hash == GLOBALB_LZC_HASH]
    if not hits:
        raise KeyError(
            f"Entry {GLOBALB_LZC_VPATH!r} (0x{GLOBALB_LZC_HASH:08x}) not found in MemoryFile"
        )
    ent = hits[0]
    payload = memfile_data[ent.offset : ent.offset + ent.memory_size]
    hdr = parse_lz_header(payload)
    return {
        "entry_index": ent.index,
        "hash": f"0x{ent.name_hash:08x}",
        "offset": f"0x{ent.offset:08x}",
        "file_size": ent.file_size,
        "memory_size": ent.memory_size,
        "slot_capacity": ent.slot_capacity,
        "memfile_total_bytes": len(memfile_data),
        "storage_format": hdr.magic.decode("ascii", errors="replace"),
        "lz_version": hdr.version,
        "lz_uncompressed_size": hdr.uncompressed_size,
        "lz_compressed_size": hdr.compressed_size,
    }


def patch_globalmemoryfile_globalb(
    memfile_data: bytes,
    new_globalb_lzc: bytes,
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Update `GLOBAL\\GLOBALB.LZC` inside `GLOBAL\\GLOBALMEMORYFILE.BIN`.

    - Verifies that `new_globalb_lzc` is a valid JDLZ/RAWW stream matching the
      MemoryFile's compressed storage format.
    - Updates in-place (preserving exact MemoryFile size) whenever `len(new_globalb_lzc)`
      fits inside the entry's 16-byte-aligned slot capacity.
    - Cleanly rebuilds shifted MemoryFile offsets (16-byte aligned) if
      `len(new_globalb_lzc)` exceeds the original slot capacity. `AddMemoryFile`
      in `SLUS_213.51` (`0x0031A7C0`) recomputes every runtime `Data` pointer as
      `mf_base + Entry.Offset` at load time.
    """
    if not is_jdlz(new_globalb_lzc) and new_globalb_lzc[:4] != b"RAWW":
        raise ValueError("GLOBALMEMORYFILE.BIN requires JDLZ/RAWW-wrapped GLOBALB.LZC payload")

    entries = parse_memory_file(memfile_data)
    target = next((e for e in entries if e.name_hash == GLOBALB_LZC_HASH), None)
    if target is None:
        raise KeyError(
            f"Entry {GLOBALB_LZC_VPATH!r} (0x{GLOBALB_LZC_HASH:08x}) not found in MemoryFile"
        )

    new_len = len(new_globalb_lzc)
    if new_len <= target.slot_capacity:
        out = bytearray(memfile_data)
        out[target.offset : target.offset + new_len] = new_globalb_lzc
        pad_len = target.slot_capacity - new_len
        if pad_len > 0:
            out[target.offset + new_len : target.offset + target.slot_capacity] = b"\x00" * pad_len
        entry_off = 16 + target.index * 20
        struct.pack_into("<II", out, entry_off + 8, new_len, new_len)
        return bytes(out), {
            "virtual_path": GLOBALB_LZC_VPATH,
            "mode": "in-place",
            "entry_index": target.index,
            "embedded_offset": f"0x{target.offset:08x}",
            "orig_file_size": target.file_size,
            "new_file_size": new_len,
            "slot_capacity": target.slot_capacity,
            "memfile_orig_size": len(memfile_data),
            "memfile_new_size": len(out),
        }

    # Rebuild shifted MemoryFile cleanly while preserving entry order & 16-byte alignment.
    ordered = sorted(entries, key=lambda e: e.offset)
    hdr_end = 16 + len(entries) * 20
    first_off = ordered[0].offset if ordered else hdr_end
    out = bytearray(memfile_data[:first_off])

    new_offsets: Dict[int, Tuple[int, int, int]] = {}
    for pos_i, ent in enumerate(ordered):
        while len(out) % 16 != 0:
            out.append(0)
        cur_off = len(out)
        if ent.index == target.index:
            payload = new_globalb_lzc
            new_fsz = new_len
            new_msz = new_len
        else:
            span = ent.memory_size
            if ent.offset + span > len(memfile_data):
                raise ValueError(
                    f"Corrupt MemoryFile entry [{ent.index}] span 0x{ent.offset:x}+{span}"
                )
            payload = memfile_data[ent.offset : ent.offset + span]
            new_fsz = ent.file_size
            new_msz = ent.memory_size
        out.extend(payload)
        # Keep non-final entries 16-byte aligned like EA's retail packer
        if pos_i + 1 < len(ordered):
            while len(out) % 16 != 0:
                out.append(0)
        new_offsets[ent.index] = (cur_off, new_fsz, new_msz)

    for ent in entries:
        cur_off, new_fsz, new_msz = new_offsets[ent.index]
        e_hdr = 16 + ent.index * 20
        struct.pack_into("<III", out, e_hdr + 4, cur_off, new_fsz, new_msz)

    return bytes(out), {
        "virtual_path": GLOBALB_LZC_VPATH,
        "mode": "repacked-shifted",
        "entry_index": target.index,
        "embedded_offset": f"0x{new_offsets[target.index][0]:08x}",
        "orig_file_size": target.file_size,
        "new_file_size": new_len,
        "slot_capacity": target.slot_capacity,
        "memfile_orig_size": len(memfile_data),
        "memfile_new_size": len(out),
    }


# ---------------------------------------------------------------------------
# Compact EA JDLZ v2 Re-compression (100% SLUS_213.51 JLZDecompress Compatible)
# ---------------------------------------------------------------------------


def compress_jdlz_compact(data: bytes, max_chain: int = 256, hash_bits: int = 15) -> bytes:
    """
    Lazy-matching EA Black Box JDLZ v2 compressor built on `tools/jdlz.py` bitstream
    constants. Compresses the 871,900-byte retail `GLOBALB.BUN` under the 518,707-byte
    `GLOBALMEMORYFILE.BIN` slot so patched `GLOBALB.LZC` fits cleanly in-place, and
    round-trip verifies decompression via `jdlz.jdlz_decompress`.
    """
    n = len(data)
    if n == 0:
        return jdlz_compress(data)

    out = bytearray(JDLZ_HEADER_SIZE + 2)
    flags1_pos = JDLZ_HEADER_SIZE
    flags2_pos = JDLZ_HEADER_SIZE + 1
    flags1 = 0
    flags2 = 0
    bits1 = 0
    bits2 = 0

    hash_mask = (1 << hash_bits) - 1
    head: Dict[int, int] = {}
    win_size = FAR_MAX_OFFSET + 1
    prev = [-1] * win_size

    def insert_pos(pos: int) -> None:
        if pos + 2 < n:
            h = ((data[pos] << 10) ^ (data[pos + 1] << 5) ^ data[pos + 2]) & hash_mask
            prev[pos % win_size] = head.get(h, -1)
            head[h] = pos

    def find_best(pos: int) -> Tuple[int, int, bool]:
        if pos + 3 > n:
            return 0, 0, False
        best_len = 0
        best_off = 0
        best_is_near = False
        max_near = min(NEAR_MAX_LEN, n - pos)
        near_limit = min(NEAR_MAX_OFFSET, pos)
        b0, b1, b2 = data[pos], data[pos + 1], data[pos + 2]

        if near_limit >= 1 and data[pos - 1] == b0 and b1 == b0 and b2 == b0:
            r = 3
            while r < max_near and data[pos + r] == b0:
                r += 1
            best_len, best_off, best_is_near = r, 1, True

        if best_len < max_near and best_len < 256:
            for off in range(2, near_limit + 1):
                p = pos - off
                if data[p] == b0 and data[p + 1] == b1 and data[p + 2] == b2:
                    mlen = 3 + _match_length(data, p + 3, pos + 3, max_near - 3)
                    if mlen > best_len:
                        best_len, best_off, best_is_near = mlen, off, True
                        if best_len == max_near:
                            break

        h = ((b0 << 10) ^ (b1 << 5) ^ b2) & hash_mask
        if best_len < FAR_MAX_LEN:
            max_far = min(FAR_MAX_LEN, n - pos)
            min_pos = max(0, pos - FAR_MAX_OFFSET)
            cand = head.get(h, -1)
            steps = 0
            while cand >= min_pos and steps < max_chain:
                off = pos - cand
                if (
                    off >= FAR_MIN_OFFSET
                    and data[cand] == b0
                    and data[cand + 1] == b1
                    and data[cand + 2] == b2
                ):
                    mlen = 3 + _match_length(data, cand + 3, pos + 3, max_far - 3)
                    if mlen > best_len:
                        best_len, best_off, best_is_near = mlen, off, False
                        if best_len == max_far:
                            break
                nxt = prev[cand % win_size]
                if nxt >= cand:
                    break
                cand = nxt
                steps += 1

        return best_len, best_off, best_is_near

    i = 0
    while i < n:
        best_len, best_off, best_is_near = find_best(i)
        inserted_i = False

        if 3 <= best_len < FAR_MAX_LEN and i + 1 < n:
            insert_pos(i)
            inserted_i = True
            next_len, next_off, next_near = find_best(i + 1)
            if next_len >= best_len + 2:
                out.append(data[i])
                bits1 += 1
                i += 1
                if bits1 == 8:
                    out[flags1_pos] = flags1
                    flags1_pos = len(out)
                    out.append(0)
                    flags1 = 0
                    bits1 = 0
                best_len, best_off, best_is_near = next_len, next_off, next_near
                inserted_i = False

        if best_len >= 3:
            flags1 |= 1 << bits1
            bits1 += 1
            if best_is_near:
                flags2 |= 1 << bits2
                run_enc = best_len - 3
                off_enc = best_off - 1
                out.append(((run_enc >> 8) << 4) | off_enc)
                out.append(run_enc & 0xFF)
            else:
                run_enc = best_len - 3
                off_enc = best_off - 17
                out.append(((off_enc >> 8) << 5) | run_enc)
                out.append(off_enc & 0xFF)
            bits2 += 1

            end_i = i + best_len
            start_ins = i + (1 if inserted_i else 0)
            if best_len <= 64:
                for pos in range(start_ins, end_i):
                    insert_pos(pos)
            else:
                if not inserted_i:
                    insert_pos(i)
                insert_pos(end_i - 2)
                insert_pos(end_i - 1)
            i = end_i
        else:
            out.append(data[i])
            bits1 += 1
            if not inserted_i:
                insert_pos(i)
            i += 1

        # Exact SLUS_213.51 JLZDecompress order: control FIRST, runtype SECOND
        if bits1 == 8:
            out[flags1_pos] = flags1
            flags1_pos = len(out)
            out.append(0)
            flags1 = 0
            bits1 = 0
        if bits2 == 8:
            out[flags2_pos] = flags2
            flags2_pos = len(out)
            out.append(0)
            flags2 = 0
            bits2 = 0

    out[flags1_pos] = flags1
    out[flags2_pos] = flags2
    struct.pack_into("<4sBBHII", out, 0, JDLZ_MAGIC, JDLZ_VERSION, JDLZ_HEADER_SIZE, 0, n, len(out))
    result = bytes(out)
    if jdlz_decompress(result) != data:
        raise RuntimeError("Internal error: JDLZ round-trip verification failed")
    return result


# ---------------------------------------------------------------------------
# Verified Declarative Lighting Recipe Patcher
# ---------------------------------------------------------------------------


def _values_match(kind: str, actual: Any, expected: Any, tol: float = 1e-4) -> bool:
    if kind == "float":
        return math.isclose(float(actual), float(expected), rel_tol=tol, abs_tol=tol)
    if kind == "vec3":
        if not isinstance(expected, (list, tuple)) or len(expected) != 3:
            return False
        return all(math.isclose(float(a), float(e), rel_tol=tol, abs_tol=tol) for a, e in zip(actual, expected))
    if kind == "rgba8":
        if not isinstance(expected, (list, tuple)) or len(expected) != 4:
            return False
        return [int(a) for a in actual] == [int(e) for e in expected]
    if kind == "int32":
        return int(actual) == int(expected)
    return actual == expected


def _encode_value(kind: str, val: Any) -> bytes:
    if kind == "float":
        return struct.pack("<f", float(val))
    if kind == "vec3":
        if not isinstance(val, (list, tuple)) or len(val) != 3:
            raise ValueError(f"Expected 3-float vector, got {val!r}")
        return struct.pack("<3f", *[float(x) for x in val])
    if kind == "rgba8":
        if not isinstance(val, (list, tuple)) or len(val) != 4:
            raise ValueError(f"Expected 4-byte RGBA list, got {val!r}")
        ints = [int(x) for x in val]
        if any(c < 0 or c > 255 for c in ints):
            raise ValueError(f"RGBA channel out of 0..255 range: {ints!r}")
        return struct.pack("<4B", *ints)
    if kind == "int32":
        return struct.pack("<i", int(val))
    raise ValueError(f"Unsupported field kind: {kind!r}")


def _decode_raw(kind: str, buf: bytes, offset: int) -> Any:
    if kind == "float":
        return round(struct.unpack_from("<f", buf, offset)[0], 6)
    if kind == "vec3":
        return [round(x, 6) for x in struct.unpack_from("<3f", buf, offset)]
    if kind == "rgba8":
        return list(struct.unpack_from("<4B", buf, offset))
    if kind == "int32":
        return struct.unpack_from("<i", buf, offset)[0]
    raise ValueError(f"Unsupported field kind: {kind!r}")


def _resolve_lighting_patch_target(
    db: GlobalBDatabase, patch: dict
) -> Tuple[int, str, Any, str]:
    """
    Resolve a single SunInfos or LightMaterials patch record into
    `(bun_offset, kind, actual_value, human_label)`.
    """
    section = str(patch.get("section", "")).strip()
    field = str(patch.get("field", "")).strip()
    if not field:
        raise ValueError(f"Patch missing 'field': {patch!r}")

    if section == "SunInfos":
        prof_ref = patch.get("profile") or patch.get("node")
        if not prof_ref:
            raise ValueError(f"SunInfos patch missing 'profile': {patch!r}")
        sun = db.find_sun(str(prof_ref))
        if field == "SunLayers":
            if "index" not in patch or patch["index"] is None:
                raise ValueError(f"SunLayers patch missing 'index' (0..5): {patch!r}")
            li = int(patch["index"])
            if not (0 <= li < SUN_LAYER_COUNT):
                raise IndexError(f"SunLayers index {li} out of range 0..{SUN_LAYER_COUNT - 1}")
            member = str(patch.get("member", "")).strip()
            if member not in SUN_LAYER_FIELDS:
                raise KeyError(f"Unknown SunLayer member {member!r}")
            rel_off, kind = SUN_LAYER_FIELDS[member]
            file_off = sun.layers[li].file_offset + rel_off
            label = f"SunInfos/{sun.name}.SunLayers[{li}].{member}"
        elif field in SUN_HEADER_FIELDS:
            rel_off, kind = SUN_HEADER_FIELDS[field]
            file_off = sun.file_offset + rel_off
            label = f"SunInfos/{sun.name}.{field}"
        else:
            raise KeyError(f"Unknown SunInfos field: {field!r}")
        return file_off, kind, _decode_raw(kind, db.bun_data, file_off), label

    if section == "LightMaterials":
        mat_ref = patch.get("material") or patch.get("node")
        if not mat_ref:
            raise ValueError(f"LightMaterials patch missing 'material': {patch!r}")
        mat = db.find_material(str(mat_ref))
        if field in LIGHT_MATERIAL_FLOAT_OFFSETS:
            kind = "float"
        elif field in LIGHT_MATERIAL_VEC3_OFFSETS:
            kind = "vec3"
        else:
            raise KeyError(f"Unknown LightMaterials field: {field!r}")
        file_off = mat.field_offset(field)
        label = f"LightMaterials/{mat.name}.{field}"
        return file_off, kind, _decode_raw(kind, db.bun_data, file_off), label

    raise ValueError(
        f"Unsupported lighting patch section {section!r} (expected 'SunInfos' or 'LightMaterials')"
    )


def apply_lighting_recipes(
    bun_data: bytes,
    recipes: Sequence[dict],
) -> Tuple[bytes, List[dict]]:
    """
    Validate and apply one or more declarative lighting recipes to uncompressed `GLOBALB.BUN`.

    SAFETY GUARANTEE:
    Every patch entry MUST specify BOTH `expected_offset` AND `expected_orig`.
    All patches across all recipes are verified against the untouched input buffer
    before a single byte is modified. Raises ValueError / KeyError / IndexError and
    mutates nothing on any mismatch or overlapping write.
    """
    db = GlobalBDatabase(bun_data)
    planned: List[Tuple[int, bytes, dict]] = []
    seen_ranges: List[Tuple[int, int, str]] = []

    for r_idx, recipe in enumerate(recipes):
        rid = str(recipe.get("id", f"lighting_recipe_{r_idx}"))
        patches = recipe.get("patches")
        if not isinstance(patches, list) or not patches:
            raise ValueError(f"Recipe {rid!r} contains no 'patches' list")

        for p_idx, patch in enumerate(patches):
            if "expected_offset" not in patch or patch["expected_offset"] is None:
                raise ValueError(
                    f"Recipe {rid!r} patch #{p_idx} is missing mandatory 'expected_offset'. "
                    f"Aborting cleanly."
                )
            if "expected_orig" not in patch:
                raise ValueError(
                    f"Recipe {rid!r} patch #{p_idx} is missing mandatory 'expected_orig'. "
                    f"Aborting cleanly."
                )
            if "new_value" not in patch:
                raise ValueError(
                    f"Recipe {rid!r} patch #{p_idx} is missing mandatory 'new_value'."
                )

            file_off, kind, actual_val, label = _resolve_lighting_patch_target(db, patch)
            exp_off = (
                int(patch["expected_offset"], 16)
                if isinstance(patch["expected_offset"], str)
                else int(patch["expected_offset"])
            )
            if file_off != exp_off:
                raise ValueError(
                    f"Offset verification failed for {rid}:{label}: "
                    f"expected 0x{exp_off:08x}, resolved 0x{file_off:08x}. Aborting cleanly."
                )

            exp_orig = patch["expected_orig"]
            if not _values_match(kind, actual_val, exp_orig):
                raise ValueError(
                    f"Safety verification failed for {rid}:{label} @ 0x{file_off:08x}: "
                    f"expected_orig={exp_orig!r}, actual={actual_val!r}. Aborting cleanly."
                )

            encoded = _encode_value(kind, patch["new_value"])
            w_end = file_off + len(encoded)
            if file_off < 0 or w_end > len(bun_data):
                raise ValueError(f"Write out of bounds for {rid}:{label} @ 0x{file_off:08x}")

            for s_start, s_end, s_lbl in seen_ranges:
                if file_off < s_end and s_start < w_end:
                    raise ValueError(
                        f"Conflicting overlapping patch write at 0x{file_off:08x}: "
                        f"{rid}:{label} overlaps {s_lbl}"
                    )
            seen_ranges.append((file_off, w_end, f"{rid}:{label}"))

            orig_bytes = bun_data[file_off:w_end]
            planned.append(
                (
                    file_off,
                    encoded,
                    {
                        "recipe_id": rid,
                        "target": label,
                        "type": kind,
                        "bun_offset": f"0x{file_off:08x}",
                        "size": len(encoded),
                        "orig_value": actual_val,
                        "new_value": patch["new_value"],
                        "orig_hex": orig_bytes.hex(),
                        "new_hex": encoded.hex(),
                    },
                )
            )

    out = bytearray(bun_data)
    audit: List[dict] = []
    for file_off, encoded, log_entry in planned:
        out[file_off : file_off + len(encoded)] = encoded
        audit.append(log_entry)
    return bytes(out), audit


# ---------------------------------------------------------------------------
# Source Loading Helpers (Strictly Read-Only ISO Access)
# ---------------------------------------------------------------------------


def _load_sources(
    iso_path: Optional[Path],
    in_globalb: Optional[Path],
    in_memfile: Optional[Path],
    in_attributes: Optional[Path],
) -> Tuple[bytes, bytes, Optional[bytes], Optional[bytes], str]:
    """Load `GLOBALB.BUN` (decompressed), raw `GLOBALB.LZC` (if compressed), optional MemoryFile & ATTRIBUTES.BIN."""
    raw_globalb: bytes
    mf_raw: Optional[bytes] = None
    attr_raw: Optional[bytes] = None
    src_label: str

    if iso_path is not None:
        arc = PS2NFSMWArchive(iso_path)
        raw_globalb = arc.read_entry(arc.find(GLOBALB_LZC_VPATH))
        try:
            mf_raw = arc.read_entry(arc.find(r"GLOBAL\GLOBALMEMORYFILE.BIN"))
        except KeyError:
            mf_raw = None
        try:
            attr_raw = arc.read_entry(arc.find(r"GLOBAL\ATTRIBUTES.BIN"))
        except KeyError:
            attr_raw = None
        src_label = f"{iso_path} ({GLOBALB_LZC_VPATH})"
    elif in_globalb is not None:
        raw_globalb = in_globalb.read_bytes()
        src_label = str(in_globalb)
    else:
        raise ValueError("Provide --iso <src.iso> or --in-globalb <GLOBALB.LZC_or_BUN>")

    if in_memfile is not None:
        mf_raw = in_memfile.read_bytes()
    if in_attributes is not None:
        attr_raw = in_attributes.read_bytes()

    if is_jdlz(raw_globalb) or raw_globalb[:4] == b"RAWW":
        bun_data = lz_decompress(raw_globalb)
    else:
        bun_data = raw_globalb

    return bun_data, raw_globalb, mf_raw, attr_raw, src_label


def _inspect_vlt_color_grading(attr_raw: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """Inspect `visualrgbtweaker` and `visuallook` inside `GLOBAL\\ATTRIBUTES.BIN` via `vlt_ps2`."""
    if not attr_raw:
        return None
    try:
        from vlt_ps2 import VpakDatabase
    except ImportError:
        return None

    db = VpakDatabase(attr_raw)
    result: Dict[str, Any] = {}
    try:
        rgb = db.find_collection("visualrgbtweaker", "default")
        result["visualrgbtweaker"] = {
            "collection_key": f"0x{rgb.key:08x}",
            "note": (
                "Present in GLOBAL\\ATTRIBUTES.BIN (three 4x4 cubic Bezier splines), "
                "but unreferenced in retail PS2 SLUS_213.51 EE code; PS2 GS uses "
                "visuallook curves in RenderMWVisualLook instead."
            ),
            "channels": {
                fname: {
                    "offset": f"0x{rgb.fields[fname].file_offset:08x}",
                    "matrix": [
                        [round(x, 6) for x in row] for row in rgb.fields[fname].value
                    ],
                }
                for fname in ("red", "green", "blue")
                if fname in rgb.fields
            },
        }
    except KeyError:
        result["visualrgbtweaker"] = None

    looks: Dict[str, Any] = {}
    for col in db.list_nodes("visuallook"):
        flds = col.fields
        looks[col.name] = {
            "key": f"0x{col.key:08x}",
            "ColourBloomTint": (
                [round(x, 4) for x in flds["ColourBloomTint"].value]
                if "ColourBloomTint" in flds
                else None
            ),
            "ColourBloomIntensity": (
                round(flds["ColourBloomIntensity"].value, 4)
                if "ColourBloomIntensity" in flds
                else None
            ),
            "Desaturation": (
                round(flds["Desaturation"].value, 4)
                if "Desaturation" in flds
                else None
            ),
            "BlackBloomIntensity": (
                round(flds["BlackBloomIntensity"].value, 4)
                if "BlackBloomIntensity" in flds
                else None
            ),
        }
    result["visuallook"] = looks
    return result


# ---------------------------------------------------------------------------
# CLI Implementation
# ---------------------------------------------------------------------------


def _cmd_inspect(args: argparse.Namespace) -> int:
    source: Optional[Path] = args.iso or args.source
    iso_path: Optional[Path] = None
    in_globalb: Optional[Path] = args.in_globalb
    if source is not None:
        if source.suffix.lower() == ".iso":
            iso_path = source
        else:
            in_globalb = source

    bun_data, raw_globalb, mf_raw, attr_raw, src_label = _load_sources(
        iso_path, in_globalb, args.in_memfile, args.attributes
    )
    db = GlobalBDatabase(bun_data)
    mem_meta = inspect_globalb_in_memfile(mf_raw) if mf_raw is not None else None
    vlt_info = _inspect_vlt_color_grading(attr_raw)

    sun_rows = db.sun_infos
    if args.sun:
        needle = args.sun.upper()
        sun_rows = [s for s in sun_rows if needle in s.name.upper()]

    mat_rows = db.light_materials
    if args.material:
        pat = args.material.upper()
        if any(ch in pat for ch in "*?[]"):
            mat_rows = [m for m in mat_rows if fnmatch.fnmatchcase(m.name.upper(), pat)]
        else:
            mat_rows = [m for m in mat_rows if pat in m.name.upper()]

    if args.json:
        payload: Dict[str, Any] = {
            "source": src_label,
            "globalb_raw_bytes": len(raw_globalb),
            "globalb_uncompressed_bytes": len(bun_data),
            "globalb_compressed": is_jdlz(raw_globalb),
            "memoryfile_globalb": mem_meta,
            "sun_infos_count": len(db.sun_infos),
            "sun_infos": [s.to_dict(include_inactive_layers=args.all_layers) for s in sun_rows],
            "sun_track": [t.to_dict() for t in db.sun_track],
            "light_materials_total": len(db.light_materials),
            "light_materials_shown": len(mat_rows),
            "light_materials": [m.to_dict() for m in mat_rows],
            "vlt_color_grading": vlt_info,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"PS2 Lighting Container: {src_label}")
    lz_tag = "JDLZ v2" if is_jdlz(raw_globalb) else "uncompressed BUN"
    print(
        f"  GLOBALB payload: {len(raw_globalb):,} B ({lz_tag}) -> "
        f"{len(bun_data):,} B uncompressed | {len(db.chunks)} bChunks"
    )
    if mem_meta is not None:
        print(
            f"  GLOBALMEMORYFILE.BIN [{mem_meta['entry_index']}] GLOBAL\\GLOBALB.LZC: "
            f"off={mem_meta['offset']} format={mem_meta['storage_format']} "
            f"fsz={mem_meta['file_size']:,} msz={mem_meta['memory_size']:,} "
            f"slot={mem_meta['slot_capacity']:,} B"
        )

    print(f"\nSunInfos (0x{BCHUNK_SUN_INFOS:08x}, {len(sun_rows)}/{len(db.sun_infos)} profiles):")
    for s in sun_rows:
        active = [l for l in s.layers if l.is_active()]
        print(
            f"  [{s.index}] {s.name:10s} @ 0x{s.file_offset:06x}  hash=0x{s.name_hash:08x}  "
            f"pos=({s.position[0]:.1f}, {s.position[1]:.1f}, {s.position[2]:.1f})  "
            f"active_layers={len(active)}/{SUN_LAYER_COUNT}"
        )
        for l in (s.layers if args.all_layers else active):
            tname = SUN_TEXTURE_NAMES.get(l.texture, str(l.texture))
            aname = SUN_ALPHA_NAMES.get(l.alpha_type, str(l.alpha_type))
            print(
                f"      Layer[{l.layer_index}] @ 0x{l.file_offset:06x}: "
                f"{tname}/{aname} int={l.intensity_scale:.3f} size={l.size:.1f} "
                f"off=({l.offset_x:.1f},{l.offset_y:.1f}) rgba={l.colour} "
                f"ang={l.angle} sweep={l.sweep_angle_amount:.3f}"
            )

    if db.sun_track:
        unique_hashes = sorted({t.sun_hash for t in db.sun_track})
        names = [db.sun_by_hash[h].name if h in db.sun_by_hash else f"0x{h:08x}" for h in unique_hashes]
        print(f"\nSunTrack (0x{BCHUNK_SUN_TRACK:08x}, {len(db.sun_track)} entries): assigned={names}")

    print(
        f"\nLightMaterials (0x{BCHUNK_LIGHT_MATERIALS:08x}, "
        f"{len(mat_rows)}/{len(db.light_materials)} materials):"
    )
    preview = mat_rows if (args.all_materials or args.material) else mat_rows[:20]
    for m in preview:
        f = m.fields
        print(
            f"  [{m.index:3d}] {m.name:24s} @ 0x{m.payload_offset:06x} (h=0x{m.name_hash:08x}) "
            f"diffMin={f['DiffuseMinScale']:.2f}*({f['DiffuseMinR']:.2f},{f['DiffuseMinG']:.2f},{f['DiffuseMinB']:.2f}) "
            f"diffMax={f['DiffuseMaxScale']:.2f}*({f['DiffuseMaxR']:.2f},{f['DiffuseMaxG']:.2f},{f['DiffuseMaxB']:.2f}) "
            f"specP={f['SpecularPower']:.2f} envP={f['EnvmapPower']:.2f} metal={f['MetallicScale']:.2f}"
        )
    if len(preview) < len(mat_rows):
        print(
            f"  ... ({len(mat_rows) - len(preview)} more materials omitted; "
            f"pass --all-materials, --material <GLOB>, or --json)"
        )

    if vlt_info and vlt_info.get("visualrgbtweaker"):
        rgb = vlt_info["visualrgbtweaker"]
        print("\nVLT visualrgbtweaker/default (GLOBAL\\ATTRIBUTES.BIN):")
        print(f"  Note: {rgb['note']}")
        for ch_name, ch_data in rgb["channels"].items():
            print(f"  {ch_name:6s} @ {ch_data['offset']}: {ch_data['matrix']}")
        if vlt_info.get("visuallook"):
            print("  Active PS2 visuallook nodes:", ", ".join(vlt_info["visuallook"].keys()))

    return 0


def _cmd_patch(args: argparse.Namespace) -> int:
    bun_data, raw_globalb, mf_raw, _attr_raw, src_label = _load_sources(
        args.iso, args.in_globalb, args.in_memfile, None
    )
    recipes = [json.loads(rp.read_text(encoding="utf-8")) for rp in args.recipe]

    patched_bun, audit = apply_lighting_recipes(bun_data, recipes)
    assert len(patched_bun) == len(bun_data), "In-place lighting patch must preserve GLOBALB.BUN byte size!"

    if args.greedy_jdlz:
        compressed_lzc = jdlz_compress(patched_bun, max_chain=args.max_chain)
    else:
        compressed_lzc = compress_jdlz_compact(patched_bun, max_chain=args.max_chain)

    # Verify JDLZ stream integrity before writing anything
    lz_hdr = parse_lz_header(compressed_lzc)
    if lz_hdr.uncompressed_size != len(patched_bun) or jdlz_decompress(compressed_lzc) != patched_bun:
        raise RuntimeError("JDLZ re-compression verification failed!")

    orig_lzc_size = len(raw_globalb) if is_jdlz(raw_globalb) else None
    memfile_audit = None
    memfile_bytes: Optional[bytes] = None

    if args.memfile_out is not None or mf_raw is not None:
        if mf_raw is not None:
            memfile_bytes, memfile_audit = patch_globalmemoryfile_globalb(mf_raw, compressed_lzc)
        elif args.memfile_out is not None:
            raise ValueError(
                "--memfile-out requires --iso <src.iso> or --in-memfile <GLOBALMEMORYFILE.BIN>"
            )

    if not args.dry_run:
        args.out_globalb.parent.mkdir(parents=True, exist_ok=True)
        args.out_globalb.write_bytes(compressed_lzc)

        if args.out_bun is not None:
            args.out_bun.parent.mkdir(parents=True, exist_ok=True)
            args.out_bun.write_bytes(patched_bun)

        if args.memfile_out is not None and memfile_bytes is not None:
            args.memfile_out.parent.mkdir(parents=True, exist_ok=True)
            args.memfile_out.write_bytes(memfile_bytes)

    report = {
        "source": src_label,
        "dry_run": args.dry_run,
        "uncompressed_bytes": len(patched_bun),
        "orig_compressed_bytes": orig_lzc_size,
        "new_compressed_bytes": len(compressed_lzc),
        "output_globalb": str(args.out_globalb) if not args.dry_run else None,
        "output_memfile": str(args.memfile_out) if (not args.dry_run and args.memfile_out) else None,
        "recipes_applied": [r.get("id", str(p)) for r, p in zip(recipes, args.recipe)],
        "patch_count": len(audit),
        "writes": audit,
        "memfile_update": memfile_audit,
    }
    if args.report_json is not None:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    mode = "DRY-RUN VERIFIED" if args.dry_run else "PATCHED"
    print(f"[{mode}] {len(audit)} verified lighting write(s) across {len(recipes)} recipe(s)")
    for w in audit:
        print(
            f"  {w['bun_offset']}  {w['target']:46s}  {w['orig_value']!r} -> {w['new_value']!r}"
        )
    diff_s = (
        f" ({len(compressed_lzc) - orig_lzc_size:+d} B vs stock)"
        if orig_lzc_size is not None
        else ""
    )
    print(
        f"  GLOBALB.LZC: {len(patched_bun):,} B uncompressed -> "
        f"{len(compressed_lzc):,} B JDLZ v2{diff_s}"
    )
    if memfile_audit:
        print(
            f"  GLOBALMEMORYFILE.BIN [{memfile_audit['mode']}]: "
            f"GLOBAL\\GLOBALB.LZC @ {memfile_audit['embedded_offset']} "
            f"({memfile_audit['orig_file_size']:,} -> {memfile_audit['new_file_size']:,} B, "
            f"slot {memfile_audit['slot_capacity']:,} B)"
        )
    if not args.dry_run:
        print(f"Wrote patched GLOBALB.LZC: {args.out_globalb}")
        if args.memfile_out is not None:
            print(f"Wrote synced GLOBALMEMORYFILE.BIN: {args.memfile_out}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PS2 NFSMW SunInfos, LightMaterials & GLOBALB.LZC inspector and verified patcher"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ins = sub.add_parser("inspect", help="Inspect SunInfos, LightMaterials & MemoryFile layout")
    p_ins.add_argument("source", nargs="?", type=Path, default=None, help="Path to PS2 .iso or GLOBALB.LZC/.BUN")
    p_ins.add_argument("--iso", type=Path, default=None, help="Path to clean PS2 .iso (read-only)")
    p_ins.add_argument("--in-globalb", type=Path, default=None, help="Path to extracted GLOBALB.LZC or GLOBALB.BUN")
    p_ins.add_argument("--in-memfile", type=Path, default=None, help="Optional extracted GLOBALMEMORYFILE.BIN")
    p_ins.add_argument("--attributes", type=Path, default=None, help="Optional extracted GLOBAL\\ATTRIBUTES.BIN")
    p_ins.add_argument("--sun", type=str, default=None, help="Filter SunInfos by name substring")
    p_ins.add_argument("--material", type=str, default=None, help="Filter LightMaterials by substring or glob")
    p_ins.add_argument("--all-materials", action="store_true", help="Print all 156 LightMaterials in text mode")
    p_ins.add_argument("--all-layers", action="store_true", help="Include inactive zeroed SunLayers")
    p_ins.add_argument("--json", action="store_true")
    p_ins.set_defaults(func=_cmd_inspect)

    p_pat = sub.add_parser("patch", help="Apply verified lighting JSON recipe(s) to GLOBALB.LZC & GLOBALMEMORYFILE.BIN")
    p_pat.add_argument("--iso", type=Path, default=None, help="Path to clean PS2 .iso (strictly read-only)")
    p_pat.add_argument("--in-globalb", type=Path, default=None, help="Path to extracted GLOBALB.LZC or GLOBALB.BUN")
    p_pat.add_argument("--in-memfile", type=Path, default=None, help="Optional extracted GLOBALMEMORYFILE.BIN")
    p_pat.add_argument("--recipe", type=Path, action="append", required=True, help="Lighting recipe JSON path (repeatable)")
    p_pat.add_argument("--out-globalb", type=Path, required=True, help="Output path for patched GLOBALB.LZC")
    p_pat.add_argument("--memfile-out", type=Path, default=None, help="Optional output path for synced GLOBALMEMORYFILE.BIN")
    p_pat.add_argument("--out-bun", type=Path, default=None, help="Optional output path for uncompressed patched GLOBALB.BUN")
    p_pat.add_argument("--max-chain", type=int, default=256, help="JDLZ hash-chain depth (default: 256)")
    p_pat.add_argument("--greedy-jdlz", action="store_true", help="Use greedy jdlz.jdlz_compress instead of compact lazy JDLZ")
    p_pat.add_argument("--dry-run", action="store_true", help="Verify all patches and recompression without writing outputs")
    p_pat.add_argument("--report-json", type=Path, default=None, help="Write JSON audit report")
    p_pat.add_argument("--json", action="store_true")
    p_pat.set_defaults(func=_cmd_patch)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
