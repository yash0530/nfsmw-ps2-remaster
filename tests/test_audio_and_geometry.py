#!/usr/bin/env python3
"""
tests/test_audio_and_geometry.py — Offline & Read-Only ISO Integration Tests for:
  1. tools/audio_inspect.py (EA ABKC + BNKl v5 + PT, Gnsu v20, SCHl / BIG+IDX / MUS+MPF / MPC)
  2. tools/geometry_inspect.py (EA 0x80134000 GeometryPack, PS2 VIF strips, VU1 R-LFSR ADC, OBJ export)
  3. patches/vlt/full_roster_handling.json (schema, non-overlap guarantees, VpakDatabase apply)

Runs 100% offline in CI via synthetic binary fixtures, plus optional strictly read-only live ISO
tests when the SLUS-21351 ISO is present locally (respecting NFSMW_ISO when explicitly set).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import audio_inspect  # noqa: E402
import audit_repo_clean  # noqa: E402
import geometry_inspect  # noqa: E402
import vlt_ps2  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402


def _find_local_iso() -> Optional[Path]:
    if "NFSMW_ISO" in os.environ:
        env_iso = os.environ["NFSMW_ISO"].strip()
        if not env_iso:
            return None
        p = Path(env_iso)
        return p if p.is_file() else None
    default_iso = (
        Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    )
    return default_iso if default_iso.is_file() else None


def _encode_pt_header(
    *,
    channels: int = 1,
    sample_rate: int = 32000,
    num_samples: int = 64000,
    loop_start: Optional[int] = None,
    loop_length: Optional[int] = None,
    data_offset: int = 0x20,
    codec_id: Optional[int] = 0x0A,
    use_gstr: bool = False,
) -> bytes:
    out = bytearray()
    if use_gstr:
        out += b"GSTR" + struct.pack("<I", 1)
    else:
        out += b"PT\x05\x00"

    def add_tlv(tag: int, val: int) -> None:
        if val == 0:
            raw = b"\x00"
        else:
            raw = val.to_bytes((val.bit_length() + 7) // 8, "big")
        out.append(tag)
        out.append(len(raw))
        out.extend(raw)

    if channels != 1:
        add_tlv(0x82, channels)
    add_tlv(0x84, sample_rate)
    add_tlv(0x85, num_samples)
    if loop_start is not None:
        add_tlv(0x86, loop_start)
    if loop_length is not None:
        add_tlv(0x87, loop_length)
    add_tlv(0x88, data_offset)
    if codec_id is not None:
        add_tlv(0xA0, codec_id)
    out.append(0xFF)
    while len(out) % 4 != 0:
        out.append(0x00)
    return bytes(out)


def _build_synthetic_abkc() -> bytes:
    """Build a valid ABKC bank with 3 BNKl slots (slot 0 empty, slots 1 & 2 active) + tail cue tags."""
    bnkl_off = 0x80
    hdr = bytearray(bnkl_off)
    hdr[0:4] = b"ABKC"
    hdr[4:8] = bytes.fromhex("01010100")
    struct.pack_into("<HH", hdr, 8, 2, 1)  # 2 modules, 1 bank

    pt1 = _encode_pt_header(
        channels=1, sample_rate=24000, num_samples=24000, loop_start=1200, loop_length=18000, codec_id=0x0A
    )
    pt2 = _encode_pt_header(
        channels=2, sample_rate=32000, num_samples=16000, codec_id=0x0A
    )

    slot_count = 3
    slot_tbl_off = bnkl_off + 0x14
    pt1_off = slot_tbl_off + slot_count * 4
    pt2_off = pt1_off + len(pt1)
    payload_off = pt2_off + len(pt2)
    payload = b"\x00" * 64
    t3_off = payload_off + len(payload)
    tail_tags = b"CAR_SYNTH_TEST\x00HORN_LOOP\x00"

    bnkl_buf = bytearray()
    bnkl_hdr_sz = t3_off - bnkl_off - len(payload)
    bnkl_buf += struct.pack("<4sHHIII", b"BNKl", 5, slot_count, bnkl_hdr_sz, len(payload), 0)
    # Self-relative offsets: rel = pt_off - (slot_tbl_off + idx * 4)
    rel0 = 0
    rel1 = pt1_off - (slot_tbl_off + 4)
    rel2 = pt2_off - (slot_tbl_off + 8)
    bnkl_buf += struct.pack("<3I", rel0, rel1, rel2)
    bnkl_buf += pt1 + pt2 + payload

    total_size = bnkl_off + len(bnkl_buf)
    struct.pack_into("<5I", hdr, 0x14, total_size, bnkl_off, 0x5C, bnkl_off, len(bnkl_buf))
    struct.pack_into("<3I", hdr, 0x30, t3_off, t3_off, t3_off)
    return bytes(hdr) + bytes(bnkl_buf) + tail_tags


def _build_synthetic_gnsu(*, decel: bool = False) -> bytes:
    """Build a valid Gnsu v20 granular engine synth binary."""
    n_rpm = 4
    n_grain = 3
    total_samples = 64000
    sample_rate = 32000
    hdr = struct.pack("<4s4sff4I", b"Gnsu", b"20\x00\x00", 1250.0, 7200.0, n_rpm, n_grain, total_samples, sample_rate)
    rpm_vals = [50000, 38000, 26000, 14000, 2000] if decel else [2000, 14000, 26000, 38000, 50000]
    rpm_tbl = struct.pack(f"<{n_rpm + 1}I", *rpm_vals)
    grain_tbl = struct.pack(f"<{n_grain}I", 0, 21000, 42000)
    granules = b"\x55" * 48
    return hdr + rpm_tbl + grain_tbl + granules


def _make_bchunk(cid: int, payload: bytes) -> bytes:
    return struct.pack("<II", cid, len(payload)) + payload


def _make_aligned_subchunk(cid: int, raw_payload: bytes, chunk_start_off: int) -> bytes:
    """Pack a PS2 0x00134A0x subchunk with exact EA bChunk 0x11 alignment padding."""
    pay_off = chunk_start_off + 8
    pad = (16 - (pay_off & 15)) & 15
    body = (b"\x11" * pad) + raw_payload
    return struct.pack("<II", cid, len(body)) + body


def _build_synthetic_geometry_pack() -> bytes:
    """Build an authentic 0x80134000 GeometryPack with 1 quad LOD B solid and 1 VIF strip."""
    # Vertices in meters: unit quad in XY plane at Z=0
    verts_expected = [
        (1.0, 2.0, 0.0),
        (1.2, 2.0, 0.0),
        (1.0, 2.4, 0.0),
        (1.2, 2.4, 0.0),
    ]
    scale = 0.0002
    base_x, base_y, base_z = 1.0, 2.0, 0.0
    xs_q = [round((v[0] - base_x) / scale) for v in verts_expected]  # [0, 1000, 0, 1000]
    ys_q = [round((v[1] - base_y) / scale) for v in verts_expected]  # [0, 0, 2000, 2000]
    zs_q = [0, 0, 0, 0]

    # Bit 14..11 = 1,1,0,0 -> 0x6000 produces ADC=[1,1,0,0] across 4 RNEXT calls
    seed = 0x6000
    aabb_min = (1.0, 2.0, 0.0)
    aabb_max = (1.2, 2.4, 0.0)
    vec2 = tuple(c - 0.01 for c in aabb_min)
    vec3 = tuple(c + 0.01 for c in aabb_max)

    # UVs: test odd-vertex swap correction cleanly (v0=(0.25, 0.75), v1=(0.5, 0.25) swapped in raw)
    # raw int16 * (1/4096):
    # even i=0: (a=1024, b=1024) -> (0.25, 1 - 0.25 = 0.75)
    # odd  i=1: (a=3072, b=2048) -> odd swaps -> (b=0.5, 1 - a=0.25)
    uv_pairs = [1024, 1024, 3072, 2048, 0, 4096, 4096, 4096]
    # Normals: 4 blocks pointing +Z -> (0, 0, 127)
    norm_block = [0, 0, 0, 0, 0, 0, 0, 0, 127, 127, 127, 127]

    vif = bytearray()
    vif += struct.pack("<I", 0x6C010001) + struct.pack("<IfII", seed, scale, seed, seed)
    vif += struct.pack("<I", 0x68020002) + struct.pack("<3f3f", *vec2, *vec3)
    vif += struct.pack("<I", 0x610C0005) + struct.pack("<12h", *(xs_q + ys_q + zs_q))
    vif += struct.pack("<I", 0x6108001D) + struct.pack("<8h", *uv_pairs)
    vif += struct.pack("<I", 0x620C004D) + struct.pack("<12b", *norm_block)
    while len(vif) % 16 != 0:
        vif.append(0x00)
    vif_bytes = bytes(vif)

    # Construct 0x00134002 GeometryHeader (at offset 8 of 0x80134000, inside 0x80134001)
    hdr02 = bytearray(0x70)
    struct.pack_into("<II", hdr02, 8, 1, 1)
    hdr02[0x10 : 0x10 + len(b"TEST_CAR\\GEOMETRY.BIN")] = b"TEST_CAR\\GEOMETRY.BIN"
    hdr02[0x48 : 0x48 + len(b"TEST_GROUP")] = b"TEST_GROUP"
    geom_info = _make_bchunk(geometry_inspect.CHUNK_GEOM_INFO, _make_bchunk(geometry_inspect.CHUNK_GEOM_HEADER, bytes(hdr02)))

    # 0x00134011 eSolid header
    part_name = b"TEST_CAR_BODY_B\x00"
    esolid = bytearray(0xA0 + len(part_name))
    struct.pack_into("<BBHIhhbbbb", esolid, 0x0C, 3, 0, 0x11, 0xCAFEBABE, 2, 0, 0, 1, 1, 0)
    struct.pack_into("<3f", esolid, 0x20, *aabb_min)
    struct.pack_into("<3f", esolid, 0x30, *aabb_max)
    struct.pack_into("<2f", esolid, 0x98, 1.25, 2.5)
    esolid[0xA0:] = part_name
    c_esolid = _make_bchunk(geometry_inspect.CHUNK_ESOLID_HDR, bytes(esolid))
    c_tex = _make_bchunk(geometry_inspect.CHUNK_TEX_TABLE, struct.pack("<II", 0x12345678, 0))
    c_mat = _make_bchunk(geometry_inspect.CHUNK_MAT_TABLE, struct.pack("<II", 0x87654321, 0))

    # Mesh container starts at solid_body_offset = 8 + len(geom_info) + 8 + len(c_esolid + c_tex + c_mat)
    solid_hdr_base = 8 + len(geom_info)
    mesh_chunk_off = solid_hdr_base + 8 + len(c_esolid) + len(c_tex) + len(c_mat)
    sub_cursor = mesh_chunk_off + 8  # start of first child inside 0x80134100

    plat_raw = struct.pack("<HHII", 1, 0x04, 1, len(vif_bytes))
    c_plat = _make_aligned_subchunk(geometry_inspect.CHUNK_PLAT_INFO, plat_raw, sub_cursor)
    sub_cursor += len(c_plat)

    strip_raw = struct.pack("<HBBfHbbBBbb", 0, 4, 2, 1.0, 0, 0, 0, len(vif_bytes) // 16, 0, 0, 0)
    c_strip = _make_aligned_subchunk(geometry_inspect.CHUNK_STRIP_TABLE, strip_raw, sub_cursor)
    sub_cursor += len(c_strip)

    c_vif = _make_aligned_subchunk(geometry_inspect.CHUNK_VIF_DATA, vif_bytes, sub_cursor)
    c_mesh = _make_bchunk(geometry_inspect.CHUNK_MESH_CONT, c_plat + c_strip + c_vif)

    solid_pack = _make_bchunk(geometry_inspect.CHUNK_SOLID_PACK, c_esolid + c_tex + c_mat + c_mesh)
    return _make_bchunk(geometry_inspect.CHUNK_GEOM_PACK, geom_info + solid_pack)


class TestAudioInspectOffline(unittest.TestCase):
    def test_parse_pt_and_gstr_headers(self) -> None:
        pt = _encode_pt_header(
            channels=2,
            sample_rate=32000,
            num_samples=64000,
            loop_start=1000,
            loop_length=30000,
            data_offset=0x40,
            codec_id=0x0A,
            use_gstr=False,
        )
        m_pt = audio_inspect.parse_pt_header(pt, 0)
        self.assertEqual(m_pt.pt_kind, "PT")
        self.assertEqual(m_pt.pt_version, 5)
        self.assertEqual(m_pt.channels, 2)
        self.assertEqual(m_pt.sample_rate, 32000)
        self.assertEqual(m_pt.num_samples, 64000)
        self.assertEqual(m_pt.loop_start, 1000)
        self.assertEqual(m_pt.loop_length, 30000)
        self.assertEqual(m_pt.codec_id, 0x0A)
        self.assertEqual(m_pt.duration_sec, 2.0)

        gstr = _encode_pt_header(
            channels=2,
            sample_rate=32000,
            num_samples=16000,
            codec_id=None,
            use_gstr=True,
        )
        m_gs = audio_inspect.parse_pt_header(gstr, 0)
        self.assertEqual(m_gs.pt_kind, "GSTR")
        self.assertEqual(m_gs.channels, 2)
        self.assertEqual(m_gs.codec_name, "EA-XA v2 (GSTR default)")
        self.assertEqual(m_gs.duration_sec, 0.5)

    def test_parse_synthetic_abkc_bank(self) -> None:
        raw = _build_synthetic_abkc()
        info = audio_inspect.parse_abkc(raw, path="SOUND\\TEST\\SYNTH.ABK", name_hash=0x1234ABCD)
        self.assertEqual(info.name_hash, "0x1234abcd")
        self.assertEqual(info.version_hex, "01010100")
        self.assertEqual(info.bnkl_version, 5)
        self.assertEqual(info.slot_count, 3)
        self.assertEqual(info.active_samples, 2)
        self.assertEqual(info.sample_rates, [24000, 32000])
        self.assertEqual(info.channels, [1, 2])
        self.assertAlmostEqual(info.total_duration_sec, 1.5, places=3)
        self.assertIn("CAR_SYNTH_TEST", info.tags)
        self.assertIn("HORN_LOOP", info.tags)
        self.assertTrue(info.samples[0].looped)
        self.assertFalse(info.samples[1].looped)

    def test_parse_synthetic_gnsu_accel_and_decel(self) -> None:
        acc = audio_inspect.parse_gnsu(_build_synthetic_gnsu(decel=False), path="GIN_ACC.GIN")
        self.assertEqual(acc.version, "20")
        self.assertEqual(acc.direction, "accel")
        self.assertEqual(acc.num_rpm_steps, 4)
        self.assertEqual(acc.num_grains, 3)
        self.assertEqual(acc.sample_rate, 32000)
        self.assertEqual(acc.rpm_table_offset, 0x20)
        self.assertEqual(acc.grain_table_offset, 0x20 + 5 * 4)
        self.assertEqual(acc.granule_data_bytes, 48)
        self.assertEqual(acc.duration_sec, 2.0)

        dec = audio_inspect.parse_gnsu(_build_synthetic_gnsu(decel=True), path="GIN_DEC.GIN")
        self.assertEqual(dec.direction, "decel")

    def test_parse_schl_idx_big_mpf_mus_and_mpc_offline(self) -> None:
        pt_chunk_hdr = _encode_pt_header(channels=2, sample_rate=32000, num_samples=48000, codec_id=0x0A)
        schl0 = b"SCHl" + struct.pack("<I", 8 + len(pt_chunk_hdr) + 16) + pt_chunk_hdr + (b"\x00" * 16)

        gstr_hdr = _encode_pt_header(channels=2, sample_rate=32000, num_samples=96000, codec_id=None, use_gstr=True)
        schl1 = b"SCHl" + struct.pack("<I", 8 + len(gstr_hdr) + 16) + gstr_hdr + (b"\x00" * 16)

        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)

            # 1) BIG + IDX
            big_path = td / "TEST.BIG"
            idx_path = td / "TEST.IDX"
            off0 = 0
            off1 = 256
            big_buf = bytearray(off1 + len(schl1))
            big_buf[off0 : off0 + len(schl0)] = schl0
            big_buf[off1 : off1 + len(schl1)] = schl1
            big_path.write_bytes(bytes(big_buf))

            idx_buf = bytearray(0x64 + 2 * 16)
            struct.pack_into("<II", idx_buf, 0, 1, 2)
            struct.pack_into("<I", idx_buf, 0x64, off0)
            struct.pack_into("<I", idx_buf, 0x64 + 16, off1)
            idx_path.write_bytes(bytes(idx_buf))

            rep_big = audio_inspect.inspect_standalone_file(big_path)
            self.assertEqual(rep_big["summary"]["schl_container_count"], 1)
            self.assertEqual(rep_big["summary"]["schl_total_streams"], 2)
            c_big = rep_big["schl_containers"][0]
            self.assertEqual(c_big.container_type, "BIG+IDX")
            self.assertEqual(c_big.sample_rates, [32000])
            self.assertAlmostEqual(c_big.total_duration_sec, 4.5, places=3)

            # 2) MUS + MPF (PFDx)
            mus_path = td / "MW_TEST.MUS"
            mpf_path = td / "MW_TEST.MPF"
            mus_buf = bytearray(384 + len(schl1))
            mus_buf[128 : 128 + len(schl0)] = schl0
            mus_buf[384 : 384 + len(schl1)] = schl1
            mus_path.write_bytes(bytes(mus_buf))

            mpf_buf = bytearray(0x60)
            mpf_buf[0:4] = b"xDFP"
            # offs[8]=0x40, offs[9]=0x50 (2 * 8-byte records)
            struct.pack_into("<10I", mpf_buf, 0x14, *([0x40] * 9 + [0x50]))
            struct.pack_into("<II", mpf_buf, 0x40, 1, 1500)  # blk 1 * 128 = 128
            struct.pack_into("<II", mpf_buf, 0x48, 3, 3000)  # blk 3 * 128 = 384
            mpf_path.write_bytes(bytes(mpf_buf))

            rep_mus = audio_inspect.inspect_standalone_file(mus_path)
            c_mus = rep_mus["schl_containers"][0]
            self.assertEqual(c_mus.container_type, "MUS+MPF")
            self.assertEqual(c_mus.stream_count, 2)
            self.assertAlmostEqual(c_mus.total_duration_sec, 4.5, places=3)

            # 3) MPC FMV container
            mpc_path = td / "TEST.MPC"
            mpc_path.write_bytes(b"MPCh" + (b"\x00" * 60) + schl1)
            rep_mpc = audio_inspect.inspect_standalone_file(mpc_path)
            c_mpc = rep_mpc["schl_containers"][0]
            self.assertEqual(c_mpc.container_type, "MPC")
            self.assertEqual(c_mpc.channels, [2])

            # 4) ABKC JSON CLI output
            abk_path = td / "TEST.ABK"
            abk_path.write_bytes(_build_synthetic_abkc())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = audio_inspect.main([str(abk_path), "--json"])
            self.assertEqual(rc, 0)
            parsed = json.loads(stdout.getvalue())
            self.assertEqual(parsed["summary"]["abkc_bank_count"], 1)
            self.assertEqual(parsed["summary"]["abkc_active_samples"], 2)


class TestGeometryInspectOffline(unittest.TestCase):
    def test_decode_vu_adc_bits_all_h1_regimes(self) -> None:
        v1 = (0x6000, 0x3951B717, 0x123456, 0x05A5A5)
        for nv in (4, 8, 12, 14, 16, 20, 28, 32, 48):
            bits = geometry_inspect.decode_vu_adc_bits(v1, nv)
            self.assertEqual(len(bits), nv)
            self.assertTrue(all(b in (0, 1) for b in bits))
        # Seed 0x6000 guarantees [1, 1, 0, 0] for the 4-vertex regime (h1=0)
        self.assertEqual(geometry_inspect.decode_vu_adc_bits(v1, 4), [1, 1, 0, 0])

        # Verify h1 >= 7 uses v1[2] for first 19 steps and matches independent LFSR simulation
        expected_19 = geometry_inspect.decode_vu_adc_bits((0x123456, 0, 0, 0), 8)
        self.assertEqual(geometry_inspect.decode_vu_adc_bits(v1, 32)[:8], expected_19)

    def test_parse_synthetic_geometry_pack_and_export_obj(self) -> None:
        pack_bytes = _build_synthetic_geometry_pack()
        info, meshes = geometry_inspect.parse_geometry_pack(
            pack_bytes, path="CARS\\TEST\\GEOMETRY.BIN", decode_parts=[0]
        )
        self.assertEqual(info.filename, "TEST_CAR\\GEOMETRY.BIN")
        self.assertEqual(info.group_name, "TEST_GROUP")
        self.assertEqual(info.solid_count, 1)
        self.assertEqual(info.total_strips, 1)
        self.assertEqual(info.total_vertices, 4)
        self.assertEqual(info.total_polys, 2)
        self.assertEqual(info.lod_counts, {"B": 1})

        part = info.parts[0]
        self.assertEqual(part.name, "TEST_CAR_BODY_B")
        self.assertEqual(part.name_hash, "0xcafebabe")
        self.assertEqual(part.lod, "B")

        mesh = meshes[0]
        self.assertEqual(len(mesh.vertices), 4)
        self.assertEqual(len(mesh.triangles), 2)
        expected_v = [(1.0, 2.0, 0.0), (1.2, 2.0, 0.0), (1.0, 2.4, 0.0), (1.2, 2.4, 0.0)]
        for actual, exp in zip(mesh.vertices, expected_v):
            for ax in range(3):
                self.assertAlmostEqual(actual[ax], exp[ax], places=4)

        # Verify odd-vertex UV swap correction
        self.assertAlmostEqual(mesh.uvs[0][0], 0.25, places=4)
        self.assertAlmostEqual(mesh.uvs[0][1], 0.75, places=4)
        self.assertAlmostEqual(mesh.uvs[1][0], 0.50, places=4)
        self.assertAlmostEqual(mesh.uvs[1][1], 0.25, places=4)

        # Verify normals (+Z)
        for nx, ny, nz in mesh.normals:
            self.assertAlmostEqual(nx, 0.0, places=4)
            self.assertAlmostEqual(ny, 0.0, places=4)
            self.assertAlmostEqual(nz, 1.0, places=4)

        selected = geometry_inspect.select_export_parts(info, lod_mode="auto", max_parts=2)
        self.assertEqual(selected, [0])

        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            geo_file = td / "GEOMETRY.BIN"
            geo_file.write_bytes(pack_bytes)
            obj_dir = td / "obj_out"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = geometry_inspect.main(
                    [str(geo_file), "--export-obj", str(obj_dir), "--json"]
                )
            self.assertEqual(rc, 0)
            rep = json.loads(stdout.getvalue())
            self.assertEqual(rep["summary"]["total_solids"], 1)
            self.assertEqual(len(rep["exported_objs"]), 1)
            obj_path = Path(rep["exported_objs"][0]["path"])
            self.assertTrue(obj_path.is_file())
            obj_txt = obj_path.read_text(encoding="utf-8")
            self.assertEqual(obj_txt.count("\nv "), 4)
            self.assertEqual(obj_txt.count("\nf "), 2)


class TestFullRosterHandlingRecipe(unittest.TestCase):
    def test_full_roster_recipe_schema_and_zero_overlaps(self) -> None:
        roster_path = REPO_ROOT / "patches" / "vlt" / "full_roster_handling.json"
        self.assertTrue(roster_path.is_file(), f"Missing {roster_path}")
        recipe = json.loads(roster_path.read_text(encoding="utf-8"))

        self.assertEqual(recipe["schema_version"], 1)
        self.assertEqual(recipe["target"]["game"], "SLUS-21351")
        self.assertEqual(recipe["target"]["virtual_path"], "GLOBAL\\ATTRIBUTES.BIN")

        patches = recipe["patches"]
        self.assertGreaterEqual(len(patches), 200)

        cars_covered = {
            p["node"]
            for p in patches
            if p["class"] in ("tires", "brakes", "chassis")
        }
        self.assertGreaterEqual(len(cars_covered), 38)
        for required_car in (
            "bmwm3gtre46",
            "bmwm3gtr",
            "camaro",
            "corvette",
            "corvettec6r",
            "carreragt",
            "911turbo",
            "911gt2",
            "murcielago",
            "gallardo",
            "fordgt",
            "viper",
            "mustanggt",
            "supra",
            "rx7",
            "lancerevo8",
            "imprezawrx",
            "slr",
            "elise",
            "gti",
            "cobaltss",
        ):
            self.assertIn(required_car, cars_covered, f"Missing expected car {required_car}")

        # Check internal uniqueness by both offset and (class, node, field, member)
        seen_offs: Dict[int, str] = {}
        seen_keys: Dict[Tuple[str, str, str, Optional[str]], int] = {}
        for p in patches:
            off = int(p["expected_offset"], 16)
            key = (p["class"], p["node"], p["field"], p.get("member"))
            self.assertNotIn(off, seen_offs, f"Duplicate offset {p['expected_offset']} inside recipe")
            self.assertNotIn(key, seen_keys, f"Duplicate field key {key} inside recipe")
            self.assertTrue(p.get("note"), f"Missing descriptive note on {key}")
            seen_offs[off] = p["node"]
            seen_keys[key] = off

        # Check zero overlaps against every sibling VLT recipe under patches/vlt/
        for sibling in sorted((REPO_ROOT / "patches" / "vlt").glob("*.json")):
            if sibling == roster_path:
                continue
            sib_data = json.loads(sibling.read_text(encoding="utf-8"))
            for sp in sib_data.get("patches", []):
                if "expected_offset" in sp and sp["expected_offset"]:
                    soff = int(sp["expected_offset"], 16)
                    self.assertNotIn(
                        soff,
                        seen_offs,
                        f"Offset collision 0x{soff:08x} between {roster_path.name} and {sibling.name}",
                    )
                skey = (sp["class"], sp["node"], sp["field"], sp.get("member"))
                self.assertNotIn(
                    skey,
                    seen_keys,
                    f"Field collision {skey} between {roster_path.name} and {sibling.name}",
                )

    def test_repo_clean_audit_on_owned_deliverables(self) -> None:
        owned_rels = [
            Path("tools/audio_inspect.py"),
            Path("tools/geometry_inspect.py"),
            Path("patches/vlt/full_roster_handling.json"),
            Path("docs/research/PS2_AUDIO_AND_GEOMETRY.md"),
            Path("tests/test_audio_and_geometry.py"),
        ]
        issues = audit_repo_clean.audit_files(REPO_ROOT, owned_rels)
        self.assertEqual(
            issues,
            [],
            "Cleanliness audit issues:\n" + "\n".join(i.format_human() for i in issues),
        )


class TestLiveISOReadOnlyIntegration(unittest.TestCase):
    ISO_PATH = _find_local_iso()

    @unittest.skipUnless(ISO_PATH is not None, "Local SLUS-21351 ISO not present; skipping live ISO audio test")
    def test_live_iso_audio_inspection(self) -> None:
        assert self.ISO_PATH is not None
        report = audio_inspect.inspect_iso(self.ISO_PATH, kind="all", max_streams=5)
        s = report["summary"]
        self.assertEqual(s["abkc_bank_count"], 301)
        self.assertEqual(s["abkc_active_samples"], 2350)
        self.assertEqual(s["gnsu_synth_count"], 160)
        self.assertEqual(s["gnsu_accel_count"], 85)
        self.assertEqual(s["gnsu_decel_count"], 75)
        self.assertEqual(s["schl_container_count"], 41)
        self.assertEqual(s["schl_total_streams"], 1980)

    @unittest.skipUnless(ISO_PATH is not None, "Local SLUS-21351 ISO not present; skipping live ISO geometry test")
    def test_live_iso_geometry_inspection_and_obj_export(self) -> None:
        assert self.ISO_PATH is not None
        report = geometry_inspect.inspect_iso(self.ISO_PATH)
        s = report["summary"]
        self.assertEqual(s["pack_count"], 97)
        self.assertEqual(s["car_pack_count"], 91)
        self.assertEqual(s["total_solids"], 10751)
        self.assertEqual(s["total_strips"], 190902)
        self.assertEqual(s["total_polys"], 2066815)

        with tempfile.TemporaryDirectory() as tmp:
            m3 = geometry_inspect.inspect_iso(
                self.ISO_PATH,
                name_filter="BMWM3GTRE46",
                export_dir=Path(tmp),
                lod_export_mode="auto",
                max_export_parts=1,
            )
            self.assertEqual(m3["summary"]["pack_count"], 1)
            self.assertEqual(len(m3["exported_objs"]), 1)
            obj_info = m3["exported_objs"][0]
            self.assertEqual(obj_info["part"], "BMWM3GTRE46_KIT00_BODY_B")
            self.assertEqual(obj_info["vertices"], 5176)
            self.assertEqual(obj_info["triangles"], 3640)
            self.assertTrue(Path(obj_info["path"]).is_file())

    @unittest.skipUnless(ISO_PATH is not None, "Local SLUS-21351 ISO not present; skipping live VLT patch test")
    def test_live_iso_vlt_patch_standalone_and_stacked(self) -> None:
        assert self.ISO_PATH is not None
        arc = PS2NFSMWArchive(self.ISO_PATH)
        raw_attr = arc.read_entry(arc.find("GLOBAL\\ATTRIBUTES.BIN"))

        vlt_dir = REPO_ROOT / "patches" / "vlt"
        roster_recipe = json.loads((vlt_dir / "full_roster_handling.json").read_text(encoding="utf-8"))
        modern_recipe = json.loads((vlt_dir / "modern_handling_tuning.json").read_text(encoding="utf-8"))

        # 1. Standalone application
        patched_solo, log_solo = vlt_ps2.apply_vlt_recipes(raw_attr, [roster_recipe])
        self.assertEqual(len(log_solo), len(roster_recipe["patches"]))
        db_solo = vlt_ps2.VpakDatabase(patched_solo)
        camaro_tires = db_solo.find_collection("tires", "camaro", vault_name="db")
        self.assertAlmostEqual(camaro_tires.fields["GRIP_SCALE"].value["Front"], 1.07, places=4)
        self.assertAlmostEqual(camaro_tires.fields["GRIP_SCALE"].value["Rear"], 1.09, places=4)

        # 2. Stacked application with all existing VLT recipes
        all_recipes = [
            modern_recipe,
            roster_recipe,
            json.loads((vlt_dir / "modern_camera.json").read_text(encoding="utf-8")),
            json.loads((vlt_dir / "modern_visual_look.json").read_text(encoding="utf-8")),
            json.loads((vlt_dir / "pursuit_and_traffic.json").read_text(encoding="utf-8")),
        ]
        patched_all, log_all = vlt_ps2.apply_vlt_recipes(raw_attr, all_recipes)
        expected_total = sum(len(r["patches"]) for r in all_recipes)
        self.assertEqual(len(log_all), expected_total)

        db_all = vlt_ps2.VpakDatabase(patched_all)
        m3_tires = db_all.find_collection("tires", "bmwm3gtre46", vault_name="db")
        m3_chassis = db_all.find_collection("chassis", "bmwm3gtre46", vault_name="db")
        self.assertAlmostEqual(m3_tires.fields["GRIP_SCALE"].value["Front"], 1.22, places=4)
        self.assertAlmostEqual(m3_chassis.fields["SWAYBAR_STIFFNESS"].value["Front"], 240.0, places=4)


if __name__ == "__main__":
    unittest.main()
