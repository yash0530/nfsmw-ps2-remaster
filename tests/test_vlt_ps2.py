#!/usr/bin/env python3
"""
tests/test_vlt_ps2.py — Unit & integration tests for PS2 NFSMW VPAK/VLT parser & patcher.

Runs 100% offline in CI using clean-room synthetic VPAK/MemoryFile binary fixtures,
plus optional read-only live verification when a local SLUS-21351 ISO is available.
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import vlt_ps2  # noqa: E402
from zdir_tool import PS2NFSMWArchive, bstringhash  # noqa: E402


# ---------------------------------------------------------------------------
# Clean-Room Synthetic PS2 VPAK Fixture Builder
# ---------------------------------------------------------------------------


def _align(buf: bytearray, align: int) -> None:
    rem = len(buf) % align
    if rem:
        buf.extend(b"\x00" * (align - rem))


def build_synthetic_vpak() -> Tuple[bytes, Dict[str, int]]:
    """
    Construct a spec-compliant PS2 32-bit VPAK container ('db') containing:
      - DatabaseLoadData (Float, Int32, UInt32, Bool, Text, Vector4, Matrix,
        RefSpec, StringKey, AxlePair, TrafficPatternRecord)
      - ClassLoadData for camerainfo, tires, trafficpattern, visuallook
      - CollectionLoadData with parent inheritance, layout scalars/arrays,
        16B-aligned Vector4 arrays (Flag_AlignedAt16), inline floats, inline_ptr
        strings, and out-of-line TrafficPatternRecord struct arrays.
    Returns (vpak_bytes, expected_file_offsets_dict).
    """
    bin_buf = bytearray()
    vlt_ptrs: List[Tuple[int, int, int]] = []  # (vlt_off, ptype, dst_bin_off)
    bin_ptrs: List[Tuple[int, int, int]] = []  # (bin_off, ptype, dst_bin_off)

    # 1. StrE chunk in .bin
    strings = [
        "default",
        "close",
        "downtown",
        "bmwm3gtre46",
        "traftaxi",
        "inline_tag_value",
        "sk_copmidsize",
        "VecAligned",
        "ExtraTag",
    ]
    stre_payload = bytearray()
    str_offsets: Dict[str, int] = {}
    for s in strings:
        str_offsets[s] = 8 + len(stre_payload)
        stre_payload.extend(s.encode("ascii") + b"\x00")
    _align(stre_payload, 16)
    bin_buf.extend(b"ErtS")
    bin_buf.extend(struct.pack("<I", 8 + len(stre_payload)))
    bin_buf.extend(stre_payload)

    # 2. Type names table in .bin (for DatabaseLoadData)
    types_spec = [
        ("EA::Reflection::Float", 4),
        ("EA::Reflection::Int32", 4),
        ("EA::Reflection::UInt32", 4),
        ("EA::Reflection::Bool", 4),
        ("EA::Reflection::Text", 4),
        ("Attrib::Types::Vector4", 16),
        ("Attrib::Types::Matrix", 64),
        ("Attrib::RefSpec", 12),
        ("Attrib::StringKey", 16),
        ("AxlePair", 8),
        ("TrafficPatternRecord", 24),
    ]
    type_names_bin_off = len(bin_buf)
    for tname, _ in types_spec:
        bin_buf.extend(tname.encode("ascii") + b"\x00")
    _align(bin_buf, 16)

    # 3. Class definitions in .bin
    def write_defs(defs: List[Tuple[str, str, int, int, int, int, int]]) -> int:
        _align(bin_buf, 16)
        off = len(bin_buf)
        for fname, tname, foff, fsz, fmax, fflags, align_log2 in defs:
            bin_buf.extend(
                struct.pack(
                    "<IIHHHBB",
                    vlt_ps2.vlt32_hash(fname),
                    vlt_ps2.vlt32_hash(tname),
                    foff,
                    fsz,
                    fmax,
                    fflags,
                    align_log2,
                )
            )
        return off

    # camerainfo layout: FOV Float[2] @0x00, SELECTABLE Bool[2] @0x10, CollectionName Text @0x20 => 36B
    cam_defs_off = write_defs(
        [
            ("FOV", "EA::Reflection::Float", 0x00, 4, 2, 0x07, 2),
            ("SELECTABLE", "EA::Reflection::Bool", 0x10, 4, 2, 0x07, 2),
            ("CollectionName", "EA::Reflection::Text", 0x20, 4, 1, 0x06, 2),
        ]
    )

    # tires layout: GRIP_SCALE AxlePair @0x00, STEERING Float @0x08; optional YAW_SPEED, VecAligned
    tires_defs_off = write_defs(
        [
            ("GRIP_SCALE", "AxlePair", 0x00, 8, 1, 0x06, 2),
            ("STEERING", "EA::Reflection::Float", 0x08, 4, 1, 0x06, 2),
            ("YAW_SPEED", "EA::Reflection::Float", 0x00, 4, 1, 0x04, 2),
            ("VecAligned", "Attrib::Types::Vector4", 0x00, 16, 2, 0x05, 4),
        ]
    )

    # trafficpattern layout: CollectionName Text @0x00; optional Vehicles, SpawnTime, ExtraTag
    tp_defs_off = write_defs(
        [
            ("CollectionName", "EA::Reflection::Text", 0x00, 4, 1, 0x06, 2),
            ("Vehicles", "TrafficPatternRecord", 0x00, 24, 4, 0x05, 2),
            ("SpawnTime", "EA::Reflection::Float", 0x00, 4, 1, 0x04, 2),
            ("ExtraTag", "EA::Reflection::Text", 0x00, 4, 1, 0x04, 2),
        ]
    )

    # visuallook layout: ColourBloomCurve Matrix @0x00, ColourBloomTint Vector4 @0x40, ColourBloomIntensity Float @0x50
    vl_defs_off = write_defs(
        [
            ("ColourBloomCurve", "Attrib::Types::Matrix", 0x00, 64, 1, 0x06, 4),
            ("ColourBloomTint", "Attrib::Types::Vector4", 0x40, 16, 1, 0x06, 4),
            ("ColourBloomIntensity", "EA::Reflection::Float", 0x50, 4, 1, 0x06, 2),
        ]
    )

    # 4. Collection layouts & out-of-line payloads in .bin
    rel_offsets: Dict[str, int] = {}

    def write_cam_layout(fov0: float, fov1: float, sel0: bool, sel1: bool, cname: str) -> int:
        _align(bin_buf, 16)
        off = len(bin_buf)
        bin_buf.extend(struct.pack("<HHHHff", 2, 2, 4, 0, fov0, fov1))
        bin_buf.extend(struct.pack("<HHHHII", 2, 2, 4, 0, 1 if sel0 else 0, 1 if sel1 else 0))
        ptr_off = len(bin_buf)
        bin_buf.extend(struct.pack("<I", 0xEFFECADD))
        bin_ptrs.append((ptr_off, vlt_ps2.PTR_DEP_RELATIVE, str_offsets[cname]))
        return off

    cam_def_lay = write_cam_layout(78.0, 78.0, False, False, "default")
    cam_close_lay = write_cam_layout(78.0, 82.0, True, True, "close")
    rel_offsets["cam_close_fov0_bin"] = cam_close_lay + 8
    rel_offsets["cam_close_sel1_bin"] = cam_close_lay + 0x10 + 12

    _align(bin_buf, 16)
    tires_def_lay = len(bin_buf)
    bin_buf.extend(struct.pack("<fff", 1.0, 1.0, 1.0))

    _align(bin_buf, 16)
    tires_m3_lay = len(bin_buf)
    bin_buf.extend(struct.pack("<fff", 1.175, 1.225, 0.9))
    rel_offsets["tires_m3_grip_rear_bin"] = tires_m3_lay + 4
    rel_offsets["tires_m3_steering_bin"] = tires_m3_lay + 8

    # Optional 16-byte aligned Vector4[2] array (Flag_AlignedAt16 = 0x8000)
    _align(bin_buf, 16)
    vec_arr_bin = len(bin_buf)
    bin_buf.extend(struct.pack("<HHHH", 2, 2, 16, vlt_ps2.ARRAY_FLAG_ALIGNED_16))
    bin_buf.extend(b"\x00" * 8)
    bin_buf.extend(struct.pack("<4f", 1.0, 2.0, 3.0, 4.0))
    bin_buf.extend(struct.pack("<4f", 5.0, 6.0, 7.0, 8.0))
    rel_offsets["tires_m3_vecaligned1_bin"] = vec_arr_bin + 16 + 16

    _align(bin_buf, 16)
    tp_def_lay = len(bin_buf)
    bin_buf.extend(struct.pack("<I", 0xEFFECADD))
    bin_ptrs.append((tp_def_lay, vlt_ps2.PTR_DEP_RELATIVE, str_offsets["default"]))

    _align(bin_buf, 16)
    tp_dt_lay = len(bin_buf)
    bin_buf.extend(struct.pack("<I", 0xEFFECADD))
    bin_ptrs.append((tp_dt_lay, vlt_ps2.PTR_DEP_RELATIVE, str_offsets["downtown"]))

    _align(bin_buf, 16)
    tp_dt_veh_arr = len(bin_buf)
    bin_buf.extend(struct.pack("<HHHH", 2, 2, 24, 0))
    bin_buf.extend(
        struct.pack(
            "<IIIfII",
            vlt_ps2.vlt32_hash("pvehicle"),
            vlt_ps2.vlt32_hash("traftaxi"),
            0,
            3.0,
            0,
            25,
        )
    )
    bin_buf.extend(
        struct.pack(
            "<IIIfII",
            vlt_ps2.vlt32_hash("pvehicle"),
            vlt_ps2.vlt32_hash("default"),
            0,
            6.0,
            1,
            75,
        )
    )
    rel_offsets["tp_dt_veh0_rate_bin"] = tp_dt_veh_arr + 8 + 12

    _align(bin_buf, 16)
    vl_def_lay = len(bin_buf)
    bin_buf.extend(struct.pack("<16f", *[float(i) for i in range(16)]))
    bin_buf.extend(struct.pack("<4f", 0.87, 0.68, 0.04, 1.0))
    bin_buf.extend(struct.pack("<f", 0.32))
    rel_offsets["vl_def_tint_bin"] = vl_def_lay + 0x40
    rel_offsets["vl_def_curve_1_2_bin"] = vl_def_lay + (1 * 4 + 2) * 4

    _align(bin_buf, 16)

    # ------------------------------------------------------------------
    # Build .vlt chunks: DepN + StrN + DatN + ExpN + PtrN
    # ------------------------------------------------------------------
    vlt_buf = bytearray()

    # DepN
    dep_body = bytearray()
    dep_body.extend(struct.pack("<I", 2))
    dep_body.extend(struct.pack("<II", vlt_ps2.vlt32_hash("db.vlt"), vlt_ps2.vlt32_hash("db.bin")))
    dep_body.extend(struct.pack("<II", 0, 7))
    dep_body.extend(b"db.vlt\x00db.bin\x00")
    _align(dep_body, 16)
    vlt_buf.extend(b"NpeD")
    vlt_buf.extend(struct.pack("<I", 8 + len(dep_body)))
    vlt_buf.extend(dep_body)

    # StrN
    vlt_buf.extend(b"NrtS")
    vlt_buf.extend(struct.pack("<III", 16, 0, 0))

    # DatN
    dat_chunk_start = len(vlt_buf)
    vlt_buf.extend(b"NtaD")
    vlt_buf.extend(struct.pack("<I", 0))

    exports: List[Tuple[int, int, int, int]] = []

    def add_export(eid: int, etype: int, payload: bytes) -> int:
        _align(vlt_buf, 8)
        eoff = len(vlt_buf)
        vlt_buf.extend(payload)
        exports.append((eid, etype, len(payload), eoff))
        return eoff

    db_payload = struct.pack(
        f"<4I{len(types_spec)}I",
        4,
        64,
        len(types_spec),
        0xEFFECADD,
        *[sz for _, sz in types_spec],
    )
    db_off = add_export(vlt_ps2.vlt32_hash("db"), vlt_ps2.EXPORT_TYPE_DATABASE, db_payload)
    vlt_ptrs.append((db_off + 12, vlt_ps2.PTR_DEP_RELATIVE, type_names_bin_off))

    def add_class(cname: str, col_res: int, num_defs: int, defs_bin_off: int, lay_sz: int, lay_cnt: int) -> None:
        ch = vlt_ps2.vlt32_hash(cname)
        cp = struct.pack("<7I", ch, col_res, num_defs, 0xEFFECADD, lay_sz, 0, lay_cnt)
        coff = add_export(ch, vlt_ps2.EXPORT_TYPE_CLASS, cp)
        vlt_ptrs.append((coff + 12, vlt_ps2.PTR_DEP_RELATIVE, defs_bin_off))

    add_class("camerainfo", 4, 3, cam_defs_off, 36, 3)
    add_class("tires", 4, 4, tires_defs_off, 12, 2)
    add_class("trafficpattern", 4, 4, tp_defs_off, 4, 1)
    add_class("visuallook", 2, 3, vl_defs_off, 84, 3)

    def add_collection(
        cname: str,
        node_name: str,
        parent_name: str,
        lay_bin_off: int,
        type_names: List[str],
        entries: List[Tuple[str, int, int, int, int]],
    ) -> int:
        ch = vlt_ps2.vlt32_hash(cname)
        kh = vlt_ps2.vlt32_hash(node_name)
        ph = vlt_ps2.vlt32_hash(parent_name) if parent_name else 0
        payload = bytearray()
        payload.extend(
            struct.pack(
                "<8I",
                kh,
                ch,
                ph,
                len(entries),
                0,
                len(entries),
                len(type_names),
                0xEFFECADD if lay_bin_off else 0,
            )
        )
        for tn in type_names:
            payload.extend(struct.pack("<I", vlt_ps2.vlt32_hash(tn)))
        entry_start_rel = len(payload)
        for fname, raw_u32, tidx, nflags, _ptr_dst in entries:
            payload.extend(struct.pack("<IIHBB", vlt_ps2.vlt32_hash(fname), raw_u32, tidx, nflags, 0))
        eoff = add_export(vlt_ps2.vlt32_hash(f"{cname}/{node_name}"), vlt_ps2.EXPORT_TYPE_COLLECTION, bytes(payload))
        if lay_bin_off:
            vlt_ptrs.append((eoff + 28, vlt_ps2.PTR_DEP_RELATIVE, lay_bin_off))
        for idx_e, (_fname, _raw_u32, _tidx, _nflags, ptr_dst) in enumerate(entries):
            if ptr_dst >= 0:
                vlt_ptrs.append((eoff + entry_start_rel + idx_e * 12 + 4, vlt_ps2.PTR_DEP_RELATIVE, ptr_dst))
        return eoff

    add_collection("camerainfo", "default", "", cam_def_lay, ["EA::Reflection::Float"], [])
    add_collection("camerainfo", "close", "default", cam_close_lay, ["EA::Reflection::Float"], [])

    add_collection(
        "tires",
        "default",
        "",
        tires_def_lay,
        ["EA::Reflection::Float"],
        [("YAW_SPEED", struct.unpack("<I", struct.pack("<f", 1.0))[0], 0, vlt_ps2.NODE_FLAG_IS_BY_VALUE, -1)],
    )
    add_collection(
        "tires",
        "bmwm3gtre46",
        "default",
        tires_m3_lay,
        ["Attrib::Types::Vector4"],
        [("VecAligned", 0xEFFECADD, 0, vlt_ps2.NODE_FLAG_IS_ARRAY, vec_arr_bin)],
    )

    tp_def_vlt_off = add_collection(
        "trafficpattern",
        "default",
        "",
        tp_def_lay,
        ["EA::Reflection::Float", "EA::Reflection::Text"],
        [
            ("SpawnTime", struct.unpack("<I", struct.pack("<f", 4.0))[0], 0, vlt_ps2.NODE_FLAG_IS_BY_VALUE, -1),
            ("ExtraTag", 0xEFFECADD, 1, vlt_ps2.NODE_FLAG_IS_BY_VALUE, str_offsets["inline_tag_value"]),
        ],
    )
    rel_offsets["tp_def_spawntime_vlt"] = tp_def_vlt_off + 32 + 8 + 4

    add_collection(
        "trafficpattern",
        "downtown",
        "default",
        tp_dt_lay,
        ["TrafficPatternRecord"],
        [("Vehicles", 0xEFFECADD, 0, vlt_ps2.NODE_FLAG_IS_ARRAY, tp_dt_veh_arr)],
    )

    add_collection("visuallook", "default", "", vl_def_lay, ["EA::Reflection::Float"], [])

    _align(vlt_buf, 16)
    struct.pack_into("<I", vlt_buf, dat_chunk_start + 4, len(vlt_buf) - dat_chunk_start)

    # ExpN
    exp_body = bytearray(struct.pack("<I", len(exports)))
    for eid, etype, esz, eoff in exports:
        exp_body.extend(struct.pack("<5I", eid, etype, 0, esz, eoff))
    _align(exp_body, 16)
    vlt_buf.extend(b"NpxE")
    vlt_buf.extend(struct.pack("<I", 8 + len(exp_body)))
    vlt_buf.extend(exp_body)

    # PtrN
    ptr_body = bytearray()
    ptr_body.extend(struct.pack("<IHHI", 0, vlt_ps2.PTR_SET_FIXUP_TARGET, 1, 0))
    for off, pt, dst in bin_ptrs:
        ptr_body.extend(struct.pack("<IHHI", off, pt, 1, dst))
    ptr_body.extend(struct.pack("<IHHI", 0, vlt_ps2.PTR_SET_FIXUP_TARGET, 0, 0))
    for off, pt, dst in vlt_ptrs:
        ptr_body.extend(struct.pack("<IHHI", off, pt, 1, dst))
    ptr_body.extend(struct.pack("<IHHI", 0, vlt_ps2.PTR_END, 0, 0))
    _align(ptr_body, 16)
    vlt_buf.extend(b"NrtP")
    vlt_buf.extend(struct.pack("<I", 8 + len(ptr_body)))
    vlt_buf.extend(ptr_body)

    # Assemble VPAK container
    str_tbl_off = 0x40
    str_tbl = b"db\x00" + b"\x00" * 13
    bin_off = 0x80
    vlt_off = bin_off + len(bin_buf)
    if vlt_off % 0x80:
        vlt_off += 0x80 - (vlt_off % 0x80)

    out = bytearray(vlt_off + len(vlt_buf))
    struct.pack_into("<4sIII", out, 0, b"VPAK", 1, str_tbl_off, len(str_tbl))
    struct.pack_into("<5I", out, 16, 0, len(bin_buf), len(vlt_buf), bin_off, vlt_off)
    out[str_tbl_off : str_tbl_off + len(str_tbl)] = str_tbl
    out[bin_off : bin_off + len(bin_buf)] = bin_buf
    out[vlt_off : vlt_off + len(vlt_buf)] = vlt_buf

    abs_offsets = {
        "cam_close_fov0": bin_off + rel_offsets["cam_close_fov0_bin"],
        "cam_close_sel1": bin_off + rel_offsets["cam_close_sel1_bin"],
        "tires_m3_grip_rear": bin_off + rel_offsets["tires_m3_grip_rear_bin"],
        "tires_m3_steering": bin_off + rel_offsets["tires_m3_steering_bin"],
        "tires_m3_vecaligned1": bin_off + rel_offsets["tires_m3_vecaligned1_bin"],
        "tp_dt_veh0_rate": bin_off + rel_offsets["tp_dt_veh0_rate_bin"],
        "tp_def_spawntime": vlt_off + rel_offsets["tp_def_spawntime_vlt"],
        "vl_def_tint": bin_off + rel_offsets["vl_def_tint_bin"],
        "vl_def_curve_1_2": bin_off + rel_offsets["vl_def_curve_1_2_bin"],
    }
    return bytes(out), abs_offsets


def build_synthetic_memfile(entries: List[Tuple[str, bytes]]) -> bytes:
    """Build a synthetic EA MemoryFile (magic 0x53219999) embedding virtual files."""
    header_sz = 16 + len(entries) * 20
    if header_sz % 16:
        header_sz += 16 - (header_sz % 16)
    buf = bytearray(header_sz)
    struct.pack_into("<4I", buf, 0, 0, 0, vlt_ps2.MEMORY_FILE_MAGIC, len(entries))
    for i, (vpath, payload) in enumerate(entries):
        _align(buf, 16)
        off = len(buf)
        buf.extend(payload)
        h = bstringhash(vpath.replace("/", "\\").upper())
        struct.pack_into("<5I", buf, 16 + i * 20, h, off, len(payload), len(payload), 0)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Offline Unit Tests (Always Run in CI)
# ---------------------------------------------------------------------------


class TestVltHashingAndParsing(unittest.TestCase):
    def test_vlt32_known_hashes(self) -> None:
        self.assertEqual(vlt_ps2.vlt32_hash("default"), 0xEEC2271A)
        self.assertEqual(vlt_ps2.vlt32_hash("camerainfo"), 0x93C171E4)
        self.assertEqual(vlt_ps2.vlt32_hash("visuallook"), 0x339F7D3D)
        self.assertEqual(vlt_ps2.vlt32_hash("pvehicle"), 0x4A97EC8F)
        self.assertEqual(vlt_ps2.vlt32_hash("pursuitlevels"), 0x551E22B3)
        self.assertEqual(vlt_ps2.vlt32_hash("trafficpattern"), 0x20D08342)
        self.assertEqual(vlt_ps2.vlt32_hash("heat_01"), 0xE1A5CE21)
        self.assertEqual(vlt_ps2.vlt32_hash("pursuitbreaker"), 0x90D06C71)

    def test_parse_vpak_backwards_compatible(self) -> None:
        raw, _ = build_synthetic_vpak()
        summary = vlt_ps2.parse_vpak(raw)
        self.assertEqual(summary["magic"], "VPAK")
        self.assertEqual(summary["vault_count"], 1)
        v0 = summary["vaults"][0]
        self.assertEqual(v0["name"], "db")
        tags = [c["tag"] for c in v0["chunks"]]
        self.assertEqual(tags, ["DepN", "StrN", "DatN", "ExpN", "PtrN"])
        exp_classes = {e["class"] for e in v0["resolved_exports"]}
        self.assertEqual(exp_classes, {"camerainfo", "tires", "trafficpattern", "visuallook"})

    def test_parse_vpak_rejects_bad_magic(self) -> None:
        with self.assertRaises(ValueError):
            vlt_ps2.parse_vpak(b"NOPE" + b"\x00" * 32)

    def test_database_full_decode_and_exact_offsets(self) -> None:
        raw, offs = build_synthetic_vpak()
        db = vlt_ps2.VpakDatabase(raw)

        cls_rows = {r["class"]: r for r in db.list_classes()}
        self.assertEqual(set(cls_rows.keys()), {"camerainfo", "tires", "trafficpattern", "visuallook"})
        self.assertEqual(cls_rows["camerainfo"]["collections"], 2)
        self.assertEqual(cls_rows["tires"]["collections"], 2)

        cam = db.find_collection("camerainfo", "close")
        self.assertEqual(cam.parent_name, "default")
        self.assertEqual(cam.fields["FOV"].value, [78.0, 82.0])
        self.assertEqual(cam.fields["FOV"].element_offsets[0], offs["cam_close_fov0"])
        self.assertEqual(cam.fields["SELECTABLE"].value, [True, True])
        self.assertEqual(cam.fields["SELECTABLE"].element_offsets[1], offs["cam_close_sel1"])
        self.assertEqual(cam.fields["CollectionName"].value, "close")

        m3 = db.find_collection("tires", "bmwm3gtre46")
        self.assertAlmostEqual(m3.fields["GRIP_SCALE"].value["Front"], 1.175, places=5)
        self.assertAlmostEqual(m3.fields["GRIP_SCALE"].value["Rear"], 1.225, places=5)
        self.assertEqual(m3.fields["STEERING"].file_offset, offs["tires_m3_steering"])
        self.assertEqual(m3.fields["VecAligned"].array_pad, 8)
        self.assertEqual(m3.fields["VecAligned"].element_offsets[1], offs["tires_m3_vecaligned1"])
        self.assertEqual(m3.fields["VecAligned"].value[1], [5.0, 6.0, 7.0, 8.0])
        self.assertNotIn("YAW_SPEED", m3.fields)
        eff = db.get_effective_fields(m3, include_inherited=True)
        self.assertIn("YAW_SPEED", eff)
        self.assertEqual(eff["YAW_SPEED"].value, 1.0)
        self.assertEqual(eff["YAW_SPEED"].inherited_from, "default")

        tp_def = db.find_collection("trafficpattern", "default")
        self.assertEqual(tp_def.fields["SpawnTime"].storage, "inline")
        self.assertEqual(tp_def.fields["SpawnTime"].file_offset, offs["tp_def_spawntime"])
        self.assertEqual(tp_def.fields["SpawnTime"].value, 4.0)
        self.assertEqual(tp_def.fields["ExtraTag"].storage, "inline_ptr")
        self.assertEqual(tp_def.fields["ExtraTag"].value, "inline_tag_value")

        tp_dt = db.find_collection("trafficpattern", "downtown")
        self.assertEqual(len(tp_dt.fields["Vehicles"].value), 2)
        self.assertEqual(tp_dt.fields["Vehicles"].value[0]["Vehicle"]["collection"], "traftaxi")
        self.assertEqual(tp_dt.fields["Vehicles"].value[0]["Rate"], 3.0)
        self.assertEqual(tp_dt.fields["Vehicles"].value[0]["Percent"], 25)

        vl = db.find_collection("visuallook", "default")
        self.assertEqual(vl.fields["ColourBloomTint"].file_offset, offs["vl_def_tint"])
        self.assertEqual(vl.fields["ColourBloomTint"].value, [0.87, 0.68, 0.04, 1.0])
        self.assertEqual(vl.fields["ColourBloomCurve"].value[1][2], 6.0)


class TestVltRecipePatching(unittest.TestCase):
    def test_apply_verified_recipe_all_field_types(self) -> None:
        raw, offs = build_synthetic_vpak()
        recipe = {
            "schema_version": 1,
            "id": "synthetic_all_types",
            "target": {"vault": "db"},
            "patches": [
                {
                    "class": "camerainfo",
                    "node": "close",
                    "field": "FOV",
                    "index": 0,
                    "expected_offset": hex(offs["cam_close_fov0"]),
                    "expected_orig": 78.0,
                    "new_value": 86.5,
                },
                {
                    "class": "camerainfo",
                    "node": "close",
                    "field": "SELECTABLE",
                    "index": 1,
                    "expected_offset": hex(offs["cam_close_sel1"]),
                    "expected_orig": True,
                    "new_value": False,
                },
                {
                    "class": "tires",
                    "node": "bmwm3gtre46",
                    "field": "GRIP_SCALE",
                    "member": "Rear",
                    "expected_offset": hex(offs["tires_m3_grip_rear"]),
                    "expected_orig": 1.225,
                    "new_value": 1.35,
                },
                {
                    "class": "tires",
                    "node": "bmwm3gtre46",
                    "field": "VecAligned",
                    "index": 1,
                    "member": "z",
                    "expected_offset": hex(offs["tires_m3_vecaligned1"] + 8),
                    "expected_orig": 7.0,
                    "new_value": 9.25,
                },
                {
                    "class": "trafficpattern",
                    "node": "default",
                    "field": "SpawnTime",
                    "expected_offset": hex(offs["tp_def_spawntime"]),
                    "expected_orig": 4.0,
                    "new_value": 2.5,
                },
                {
                    "class": "trafficpattern",
                    "node": "downtown",
                    "field": "Vehicles",
                    "index": 0,
                    "member": "Rate",
                    "expected_offset": hex(offs["tp_dt_veh0_rate"]),
                    "expected_orig": 3.0,
                    "new_value": 1.75,
                },
                {
                    "class": "visuallook",
                    "node": "default",
                    "field": "ColourBloomTint",
                    "expected_offset": hex(offs["vl_def_tint"]),
                    "expected_orig": [0.87, 0.68, 0.04, 1.0],
                    "new_value": [0.96, 0.94, 0.9, 1.0],
                },
                {
                    "class": "visuallook",
                    "node": "default",
                    "field": "ColourBloomCurve",
                    "member": [1, 2],
                    "expected_offset": hex(offs["vl_def_curve_1_2"]),
                    "expected_orig": 6.0,
                    "new_value": 12.5,
                },
            ],
        }

        patched, audit = vlt_ps2.apply_vlt_recipes(raw, [recipe])
        self.assertEqual(len(patched), len(raw))
        self.assertEqual(len(audit), 8)

        db2 = vlt_ps2.VpakDatabase(patched)
        self.assertAlmostEqual(db2.find_collection("camerainfo", "close").fields["FOV"].value[0], 86.5, places=5)
        self.assertEqual(db2.find_collection("camerainfo", "close").fields["SELECTABLE"].value[1], False)
        self.assertAlmostEqual(db2.find_collection("tires", "bmwm3gtre46").fields["GRIP_SCALE"].value["Rear"], 1.35, places=5)
        self.assertAlmostEqual(db2.find_collection("tires", "bmwm3gtre46").fields["VecAligned"].value[1][2], 9.25, places=5)
        self.assertAlmostEqual(db2.find_collection("trafficpattern", "default").fields["SpawnTime"].value, 2.5, places=5)
        self.assertAlmostEqual(db2.find_collection("trafficpattern", "downtown").fields["Vehicles"].value[0]["Rate"], 1.75, places=5)
        self.assertEqual(db2.find_collection("visuallook", "default").fields["ColourBloomTint"].value, [0.96, 0.94, 0.9, 1.0])
        self.assertAlmostEqual(db2.find_collection("visuallook", "default").fields["ColourBloomCurve"].value[1][2], 12.5, places=5)

    def test_rejects_missing_expected_orig(self) -> None:
        raw, _ = build_synthetic_vpak()
        bad_recipe = {
            "id": "no_orig",
            "patches": [{"class": "camerainfo", "node": "close", "field": "FOV", "index": 0, "new_value": 90.0}],
        }
        with self.assertRaisesRegex(ValueError, "expected_orig"):
            vlt_ps2.apply_vlt_recipes(raw, [bad_recipe])

    def test_rejects_wrong_expected_orig_atomically(self) -> None:
        raw, _ = build_synthetic_vpak()
        bad_recipe = {
            "id": "bad_orig",
            "patches": [
                {"class": "camerainfo", "node": "close", "field": "FOV", "index": 0, "expected_orig": 78.0, "new_value": 85.0},
                {"class": "tires", "node": "bmwm3gtre46", "field": "STEERING", "expected_orig": 99.0, "new_value": 1.1},
            ],
        }
        with self.assertRaisesRegex(ValueError, "Safety verification failed"):
            vlt_ps2.apply_vlt_recipes(raw, [bad_recipe])

    def test_rejects_wrong_expected_offset(self) -> None:
        raw, _ = build_synthetic_vpak()
        bad_recipe = {
            "id": "bad_off",
            "patches": [
                {
                    "class": "camerainfo",
                    "node": "close",
                    "field": "FOV",
                    "index": 0,
                    "expected_offset": "0x00000004",
                    "expected_orig": 78.0,
                    "new_value": 85.0,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "Offset verification failed"):
            vlt_ps2.apply_vlt_recipes(raw, [bad_recipe])

    def test_rejects_overlapping_writes(self) -> None:
        raw, _ = build_synthetic_vpak()
        r1 = {"id": "r1", "patches": [{"class": "camerainfo", "node": "close", "field": "FOV", "index": 0, "expected_orig": 78.0, "new_value": 85.0}]}
        r2 = {"id": "r2", "patches": [{"class": "camerainfo", "node": "close", "field": "FOV", "index": 0, "expected_orig": 78.0, "new_value": 88.0}]}
        with self.assertRaisesRegex(ValueError, "overlapping"):
            vlt_ps2.apply_vlt_recipes(raw, [r1, r2])

    def test_refuses_implicit_parent_mutation(self) -> None:
        raw, _ = build_synthetic_vpak()
        bad_recipe = {
            "id": "inherited_write",
            "patches": [
                {"class": "tires", "node": "bmwm3gtre46", "field": "YAW_SPEED", "expected_orig": 1.0, "new_value": 1.5}
            ],
        }
        with self.assertRaises(KeyError):
            vlt_ps2.apply_vlt_recipes(raw, [bad_recipe])

    def test_memory_file_sync_helper(self) -> None:
        raw, _ = build_synthetic_vpak()
        mem = build_synthetic_memfile([(r"GLOBAL\ATTRIBUTES.BIN", raw)])
        recipe = {
            "id": "fov_patch",
            "patches": [
                {"class": "camerainfo", "node": "close", "field": "FOV", "index": 0, "expected_orig": 78.0, "new_value": 91.0}
            ],
        }
        patched, _ = vlt_ps2.apply_vlt_recipes(raw, [recipe])
        updated_mem, meta = vlt_ps2.patch_memory_file_entry(mem, r"GLOBAL\ATTRIBUTES.BIN", patched)
        self.assertEqual(len(updated_mem), len(mem))
        emb_off = int(meta["embedded_offset"], 16)
        self.assertEqual(updated_mem[emb_off : emb_off + len(patched)], patched)
        with self.assertRaises(ValueError):
            vlt_ps2.patch_memory_file_entry(mem, r"GLOBAL\ATTRIBUTES.BIN", patched + b"\x00")


class TestVltCliAndRepoRecipes(unittest.TestCase):
    def test_cli_subcommands_and_legacy_invocation(self) -> None:
        raw, _ = build_synthetic_vpak()
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            bin_path = tdir / "attributes.bin"
            out_path = tdir / "patched.bin"
            rec_path = tdir / "recipe.json"
            bin_path.write_bytes(raw)
            rec_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "cli_test",
                        "patches": [
                            {
                                "class": "camerainfo",
                                "node": "close",
                                "field": "FOV",
                                "index": 0,
                                "expected_orig": 78.0,
                                "new_value": 85.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            # 1. Backwards-compatible legacy inspect invocation (no subcommand)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = vlt_ps2.main([str(bin_path), "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["magic"], "VPAK")

            # 2. classes --json
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = vlt_ps2.main(["classes", str(bin_path), "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(json.loads(buf.getvalue())["classes"]), 4)

            # 3. dump-node --json
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = vlt_ps2.main(["dump-node", str(bin_path), "camerainfo", "close", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["node"], "close")

            # 4. patch --in ... --recipe ... --out ...
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = vlt_ps2.main(["patch", "--in", str(bin_path), "--recipe", str(rec_path), "--out", str(out_path), "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["patch_count"], 1)
            self.assertTrue(out_path.is_file())
            self.assertAlmostEqual(
                vlt_ps2.VpakDatabase(out_path.read_bytes()).find_collection("camerainfo", "close").fields["FOV"].value[0],
                85.0,
                places=5,
            )

    def test_repo_starter_recipes_schema_valid(self) -> None:
        vlt_dir = REPO_ROOT / "patches" / "vlt"
        expected_files = [
            "modern_camera.json",
            "forza_hero_camera.json",
            "modern_handling_tuning.json",
            "full_roster_handling.json",
            "pursuit_and_traffic.json",
            "modern_visual_look.json",
            "gt7_visual_look.json",
            "forza_ultra_look.json",
        ]
        for fname in expected_files:
            fpath = vlt_dir / fname
            self.assertTrue(fpath.is_file(), f"Missing recipe {fpath}")
            spec = json.loads(fpath.read_text(encoding="utf-8"))
            self.assertEqual(spec["schema_version"], 1)
            self.assertEqual(spec["target"]["game"], "SLUS-21351")
            self.assertGreater(len(spec["patches"]), 0)
            for p in spec["patches"]:
                for req in ("class", "node", "field", "expected_orig", "new_value", "expected_offset"):
                    self.assertIn(req, p, f"{fname} patch missing {req}")


# ---------------------------------------------------------------------------
# Optional Live Read-Only ISO Verification (Skipped Automatically in CI)
# ---------------------------------------------------------------------------


def _find_local_iso() -> Optional[Path]:
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso:
        p = Path(env_iso).expanduser()
        return p if p.is_file() else None
    default_p = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    return default_p if default_p.is_file() else None


LOCAL_ISO = _find_local_iso()


@unittest.skipUnless(LOCAL_ISO is not None, "Local SLUS-21351 ISO not present; skipping live read-only ISO tests")
class TestLiveReadOnlyISO(unittest.TestCase):
    def test_live_attributes_fe_and_gameplay_decode(self) -> None:
        assert LOCAL_ISO is not None
        arc = PS2NFSMWArchive(LOCAL_ISO)

        attr_raw = arc.read_entry(arc.find(r"GLOBAL\ATTRIBUTES.BIN"))
        attr_db = vlt_ps2.VpakDatabase(attr_raw)
        cls_rows = {r["class"]: r for r in attr_db.list_classes()}
        self.assertEqual(len(cls_rows), 56)
        self.assertEqual(sum(r["collections"] for r in cls_rows.values()), 2301)
        self.assertEqual(
            attr_db.find_collection("visuallook", "default").fields["ColourBloomTint"].value,
            [0.87, 0.68, 0.04, 1.0],
        )

        mf_raw = arc.read_entry(arc.find(r"GLOBAL\GLOBALMEMORYFILE.BIN"))
        synced_mf, mf_meta = vlt_ps2.patch_memory_file_entry(mf_raw, r"GLOBAL\ATTRIBUTES.BIN", attr_raw)
        self.assertEqual(synced_mf, mf_raw)
        self.assertEqual(mf_meta["embedded_offset"], "0x00000380")

        fe_raw = arc.read_entry(arc.find(r"GLOBAL\FE_ATTRIB.BIN"))
        fe_db = vlt_ps2.VpakDatabase(fe_raw, schema_source=attr_db)
        self.assertEqual(len(fe_db.list_nodes("frontend")), 411)

        gp_raw = arc.read_entry(arc.find(r"GLOBAL\GAMEPLAY.BIN"))
        gp_db = vlt_ps2.VpakDatabase(gp_raw, schema_source=attr_db)
        self.assertEqual(len(gp_db.vaults), 272)
        self.assertEqual(len(gp_db.list_nodes("gameplay")), 6287)

    def test_live_all_repo_recipes_verify_and_roundtrip(self) -> None:
        assert LOCAL_ISO is not None
        arc = PS2NFSMWArchive(LOCAL_ISO)
        attr_raw = arc.read_entry(arc.find(r"GLOBAL\ATTRIBUTES.BIN"))
        recipes = [
            json.loads((REPO_ROOT / "patches" / "vlt" / name).read_text(encoding="utf-8"))
            for name in (
                "modern_camera.json",
                "modern_handling_tuning.json",
                "pursuit_and_traffic.json",
                "modern_visual_look.json",
            )
        ]
        patched, audit = vlt_ps2.apply_vlt_recipes(attr_raw, recipes)
        self.assertEqual(len(patched), len(attr_raw))
        self.assertEqual(len(audit), 75)

        db2 = vlt_ps2.VpakDatabase(patched)
        self.assertAlmostEqual(db2.find_collection("camerainfo", "close").fields["FOV"].value[0], 84.0, places=4)
        self.assertAlmostEqual(db2.find_collection("camerainfo", "close").fields["FOV"].value[1], 92.0, places=4)
        self.assertAlmostEqual(db2.find_collection("tires", "bmwm3gtre46").fields["STEERING"].value, 1.05, places=4)
        self.assertAlmostEqual(db2.find_collection("trafficpattern", "default").fields["SpawnTime"].value, 2.8, places=4)
        self.assertEqual(db2.find_collection("pursuitlevels", "heat_05").fields["cops"].value[0]["Count"], 12)
        self.assertEqual(
            db2.find_collection("visuallook", "default").fields["ColourBloomTint"].value,
            [0.96, 0.94, 0.9, 1.0],
        )

    def test_live_v05_gt7_visual_look_full_stack_zero_overlaps(self) -> None:
        assert LOCAL_ISO is not None
        arc = PS2NFSMWArchive(LOCAL_ISO)
        attr_raw = arc.read_entry(arc.find(r"GLOBAL\ATTRIBUTES.BIN"))
        recipes = [
            json.loads((REPO_ROOT / "patches" / "vlt" / name).read_text(encoding="utf-8"))
            for name in (
                "modern_camera.json",
                "modern_handling_tuning.json",
                "full_roster_handling.json",
                "pursuit_and_traffic.json",
                "gt7_visual_look.json",
            )
        ]
        patched, audit = vlt_ps2.apply_vlt_recipes(attr_raw, recipes)
        self.assertEqual(len(patched), len(attr_raw))
        self.assertEqual(len(audit), 368)

        db2 = vlt_ps2.VpakDatabase(patched)
        self.assertEqual(
            db2.find_collection("visuallook", "default").fields["ColourBloomTint"].value,
            [0.97, 0.98, 1.0, 1.0],
        )
        self.assertAlmostEqual(
            db2.find_collection("visuallook", "default").fields["Desaturation"].value,
            0.02,
            places=4,
        )
        self.assertAlmostEqual(
            db2.find_collection("visuallook", "extreme_uves").fields["BlackBloomIntensity"].value,
            0.12,
            places=4,
        )
        self.assertAlmostEqual(
            db2.find_collection("visuallook", "copcam").fields["ColourBloomIntensity"].value,
            0.22,
            places=4,
        )
        # Ensure dead visualrgbtweaker class (0 EE xrefs on SLUS_213.51) is not patched
        self.assertTrue(all(p["class"] != "visualrgbtweaker" for r in recipes for p in r["patches"]))

    def test_live_forza_ultra_full_stack_zero_overlaps(self) -> None:
        assert LOCAL_ISO is not None
        arc = PS2NFSMWArchive(LOCAL_ISO)
        attr_raw = arc.read_entry(arc.find(r"GLOBAL\ATTRIBUTES.BIN"))
        recipes = [
            json.loads((REPO_ROOT / "patches" / "vlt" / name).read_text(encoding="utf-8"))
            for name in (
                "forza_hero_camera.json",
                "modern_handling_tuning.json",
                "full_roster_handling.json",
                "pursuit_and_traffic.json",
                "forza_ultra_look.json",
            )
        ]
        patched, audit = vlt_ps2.apply_vlt_recipes(attr_raw, recipes)
        self.assertEqual(len(patched), len(attr_raw))
        self.assertEqual(len(audit), 374)

        db2 = vlt_ps2.VpakDatabase(patched)
        self.assertEqual(
            db2.find_collection("visuallook", "default").fields["ColourBloomTint"].value,
            [1.0, 0.89, 0.72, 1.0],
        )
        self.assertAlmostEqual(
            db2.find_collection("camerainfo", "close").fields["HEIGHT"].value[0],
            1.34,
            places=4,
        )
        self.assertAlmostEqual(
            db2.find_collection("camerainfo", "close").fields["FOV"].value[1],
            94.0,
            places=4,
        )
        self.assertEqual(
            db2.find_collection("light_flares_cg", "sun_flare").fields["colour"].value,
            [0.72, 0.66, 0.54, 0.72],
        )
        self.assertAlmostEqual(
            db2.find_collection("visuallook", "default").fields["ColourBloomIntensity"].value,
            0.12,
            places=4,
        )
        self.assertAlmostEqual(
            db2.find_collection("visuallook", "default").fields["BlackBloomIntensity"].value,
            0.10,
            places=4,
        )
        self.assertTrue(all(p["class"] != "visualrgbtweaker" for r in recipes for p in r["patches"]))


if __name__ == "__main__":
    unittest.main()
