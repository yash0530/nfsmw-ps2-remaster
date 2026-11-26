#!/usr/bin/env python3
"""Unit and live read-only ISO integration tests for tools/export_rockport_world.py & 0x11 pad fix."""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_rockport_world as erw  # noqa: E402
import geometry_inspect as gi  # noqa: E402


def _default_iso() -> Path:
    env = os.environ.get("NFSMW_ISO", "").strip()
    if env:
        return Path(env).expanduser()
    return erw.DEFAULT_SOURCE_ISO


def _make_padded_solid_pack(pad_bytes: int, first_tex_hash: int = 0xAABBCC11) -> bytes:
    """Build a minimal 0x80134000 GeometryPack with pad_bytes (0/4/8/12) of 0x11 in 0x00134011/12/13."""
    pad = b"\x11" * pad_bytes

    # 0x00134011 eSolid header (0xA0 + name)
    hdr_body = bytearray(0xA0)
    struct.pack_into("<BBHIhhbbbb", hdr_body, 0x0C, 0x18, 0, 0x0001, 0x12345678, 12, 0, 0, 2, 1, 0)
    struct.pack_into("<3f", hdr_body, 0x20, -1.5, -2.5, 0.25)
    struct.pack_into("<3f", hdr_body, 0x30, 3.5, 4.5, 6.25)
    struct.pack_into("<2f", hdr_body, 0x98, 42.0, 1.0)
    hdr_payload = pad + bytes(hdr_body) + b"TEST_WORLD_SOLID_01\x00"
    c_hdr = struct.pack("<II", gi.CHUNK_ESOLID_HDR, len(hdr_payload)) + hdr_payload

    # 0x00134012 Texture table (2 entries; first hash intentionally ends with low byte 0x11!)
    tex_payload = pad + struct.pack("<IIII", first_tex_hash, 0, 0xDEADBEEF, 0)
    c_tex = struct.pack("<II", gi.CHUNK_TEX_TABLE, len(tex_payload)) + tex_payload

    # 0x00134013 Material table
    mat_payload = pad + struct.pack("<II", 0xCAFEBAB1, 0)
    c_mat = struct.pack("<II", gi.CHUNK_MAT_TABLE, len(mat_payload)) + mat_payload

    solid_body = c_hdr + c_tex + c_mat
    c_solid = struct.pack("<II", gi.CHUNK_SOLID_PACK, len(solid_body)) + solid_body
    return struct.pack("<II", gi.CHUNK_GEOM_PACK, len(c_solid)) + c_solid


class TestRkptContractOffline(unittest.TestCase):
    def test_locked_struct_sizes(self) -> None:
        self.assertEqual(erw.HEADER_SIZE, 160)
        self.assertEqual(erw.SECTION_SIZE, 64)
        self.assertEqual(erw.MESH_SIZE, 48)
        self.assertEqual(erw.INSTANCE_SIZE, 64)
        self.assertEqual(erw.MATERIAL_SIZE, 32)
        self.assertEqual(erw.TEXTURE_SIZE, 32)
        self.assertEqual(erw.VERTEX_SIZE, 36)

    def test_det_plus_one_coordinate_and_instance_transform_roundtrip(self) -> None:
        det_p = float(np.linalg.det(erw.P_PS2_TO_METAL))
        self.assertAlmostEqual(det_p, 1.0, places=9)

        rng = np.random.default_rng(2026)
        origin_ps2 = (1386.4, 2310.7, 0.0)
        for _ in range(25):
            # Build random proper 3D rotation via Euler angles, quantized like PS2 int16 / 8192
            a, b, c = rng.uniform(-math.pi, math.pi, size=3)
            rz = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
            ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
            rx = np.array([[1, 0, 0], [0, math.cos(c), -math.sin(c)], [0, math.sin(c), math.cos(c)]])
            r_ps2 = rz @ ry @ rx
            rot9_i16 = [int(round(v * 8192.0)) for v in r_ps2.reshape(-1)]
            r_ps2_q = np.asarray(rot9_i16, dtype=np.float64).reshape(3, 3) / 8192.0
            pos_ps2 = tuple(float(v) for v in rng.uniform(-500.0, 2500.0, size=3))

            pts_ps2 = rng.uniform(-25.0, 25.0, size=(32, 3))
            # Ground truth in PS2 Z-up, then projected to Metal Y-up
            w_ps2 = pts_ps2 @ r_ps2_q + np.asarray(pos_ps2)
            expected_metal = np.stack(
                [w_ps2[:, 0] - origin_ps2[0], w_ps2[:, 2] - origin_ps2[2], -(w_ps2[:, 1] - origin_ps2[1])],
                axis=1,
            )

            # Local Metal vertices transformed by MTLPackedFloat4x3
            pts_metal = np.stack([pts_ps2[:, 0], pts_ps2[:, 2], -pts_ps2[:, 1]], axis=1)
            xform12 = erw.ps2_instance_to_metal_4x3(rot9_i16, pos_ps2, origin_ps2, is_identity=False)
            got_metal = erw.apply_metal_4x3(xform12, pts_metal)

            max_err = float(np.max(np.abs(got_metal - expected_metal)))
            self.assertLess(max_err, 1e-4)

            # Verify CCW triangle winding / outward normal preservation (det = +1)
            tri_ps2 = np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 3.0, 0.0]])
            tri_w_ps2 = tri_ps2 @ r_ps2_q + np.asarray(pos_ps2)
            fn_ps2 = np.cross(tri_w_ps2[1] - tri_w_ps2[0], tri_w_ps2[2] - tri_w_ps2[0])
            fn_ps2_m = np.array([fn_ps2[0], fn_ps2[2], -fn_ps2[1]])
            tri_m = np.stack([tri_ps2[:, 0], tri_ps2[:, 2], -tri_ps2[:, 1]], axis=1)
            tri_wm = erw.apply_metal_4x3(xform12, tri_m)
            fn_wm = np.cross(tri_wm[1] - tri_wm[0], tri_wm[2] - tri_wm[0])
            cos_sim = float(np.dot(fn_wm, fn_ps2_m) / (np.linalg.norm(fn_wm) * np.linalg.norm(fn_ps2_m)))
            self.assertGreater(cos_sim, 0.9999)

    def test_geometry_inspect_0x11_alignment_padding_all_offsets(self) -> None:
        for pad in (0, 4, 8, 12):
            pack = _make_padded_solid_pack(pad, first_tex_hash=0xAABBCC11)
            info, _ = gi.parse_geometry_pack(pack, path=f"synthetic_pad_{pad}")
            self.assertEqual(info.solid_count, 1)
            part = info.parts[0]
            self.assertEqual(part.name, "TEST_WORLD_SOLID_01")
            self.assertEqual(part.name_hash, "0x12345678")
            self.assertEqual(part.aabb_min, [-1.5, -2.5, 0.25])
            self.assertEqual(part.aabb_max, [3.5, 4.5, 6.25])
            self.assertEqual(part.texture_hashes, ["0xaabbcc11", "0xdeadbeef"])
            self.assertEqual(part.light_material_hashes, ["0xcafebab1"])

    def test_ps2_alpha_normalization_and_material_classifier(self) -> None:
        raw = np.array([[[10, 20, 30, 0], [10, 20, 30, 64], [10, 20, 30, 128], [10, 20, 30, 200]]], dtype=np.uint8)
        norm, scaled = erw.normalize_ps2_alpha_rgba(raw, psm=0x14)
        self.assertTrue(scaled)
        self.assertEqual(norm[0, :, 3].tolist(), [0, 128, 255, 255])

        m_road = erw.classify_world_material("TRNS_ALL_LA_PAVEMENTA_Y", "TRN_CT_ROAD", False, 0, 1)
        self.assertEqual(m_road.surface_class, erw.SURFACE_ROAD_ASPHALT)
        self.assertTrue(m_road.flags & erw.MAT_FLAG_DRIVABLE_ROAD)

        m_glass = erw.classify_world_material("ARC_WHOUSE_WINDOW_WH", "XB_BLDG", True, 1, 2)
        self.assertEqual(m_glass.surface_class, erw.SURFACE_BUILDING_GLASS)
        self.assertFalse(m_glass.flags & erw.MAT_FLAG_ALPHA_CUTOUT)

        m_foliage = erw.classify_world_material("ORG_CT_TREELINE_FIR_256", "XT_TREE", True, 2, 3)
        self.assertEqual(m_foliage.surface_class, erw.SURFACE_FOLIAGE_CUTOUT)
        self.assertTrue(m_foliage.flags & erw.MAT_FLAG_ALPHA_CUTOUT)
        self.assertTrue(m_foliage.flags & erw.MAT_FLAG_DOUBLE_SIDED)
        self.assertGreaterEqual(m_foliage.bump_strength, 1.25)

        # Verify Little Italy / Downtown shortened WIN / DOORWIN storefront glass classification
        for win_tex in ("ARC_CITYEASTBLD9DOORWIN", "ARC_CITYEASTBLD7WIN1", "ARC_LB_FIREHALL_WIN", "ARC_OLDBRICKB_WINA_LL"):
            m_win = erw.classify_world_material(win_tex, "XB_CITYEAST", True, 3, 4)
            self.assertEqual(m_win.surface_class, erw.SURFACE_BUILDING_GLASS, win_tex)
            self.assertFalse(m_win.flags & erw.MAT_FLAG_ALPHA_CUTOUT, win_tex)

        # Verify SGN_WARNWINDING stays EMISSIVE_SIGN_LAMP and OBJ_ETHNICSTREETLIGHT is NOT FOLIAGE_CUTOUT
        self.assertEqual(
            erw.classify_world_material("SGN_WARNWINDING", "XS_SIGN", False, 4, 5).surface_class,
            erw.SURFACE_EMISSIVE_SIGN_LAMP,
        )
        self.assertEqual(
            erw.classify_world_material("OBJ_ETHNICSTREETLIGHT", "XO_STREETLIGHTG", False, 5, 6).surface_class,
            erw.SURFACE_EMISSIVE_SIGN_LAMP,
        )

        # Verify race-event chevron barriers & temp gates are excluded while real signs/fences stay
        self.assertEqual(erw.is_excluded_world_solid("XO_TRACKBARRIERTRAFFIC_1B_00"), "event_barrier")
        self.assertEqual(erw.is_excluded_world_solid("XO_TRACKBARRIERPLAYER_1B_00"), "event_barrier")
        self.assertEqual(erw.is_excluded_world_solid("__TEMPGATE"), "shd_pan_rfl_temp")
        self.assertIsNone(erw.is_excluded_world_solid("XS_STOPSIGNB_1B_00"))
        self.assertIsNone(erw.is_excluded_world_solid("XW_FENCECEMETERY_1B_LK_00"))

        # Verify 4x hero foliage/cypress & sign enhancement (capped at 512px) and skip >256px
        cypress = np.zeros((128, 64, 4), dtype=np.uint8)
        cypress[:, :, 0] = 60
        cypress[:, :, 1] = 85
        cypress[:, :, 2] = 45
        cypress[::2, ::2, 1] = 105
        cypress[:, :32, 3] = 255
        cypress[:, 32:, 3] = 0
        up_cyp, did_cyp = erw.enhance_world_texture_2x(cypress, has_cutout_alpha=True, tex_name="ORG_TREE_CYPRESS")
        self.assertTrue(did_cyp)
        self.assertEqual(up_cyp.shape, (512, 256, 4))
        self.assertGreater(float(up_cyp[:, :120, 1].std()), float(cypress[:, :30, 1].std()))
        self.assertEqual(int(up_cyp[:, :116, 3].min()), 255)
        self.assertEqual(int(up_cyp[:, 140:, 3].max()), 0)

        sgn = np.full((32, 32, 4), 200, dtype=np.uint8)
        up_sgn, did_sgn = erw.enhance_world_texture_2x(sgn, has_cutout_alpha=False, tex_name="SGN_DONOTENTER")
        self.assertTrue(did_sgn)
        self.assertEqual(up_sgn.shape, (256, 256, 4))

        large = np.zeros((512, 256, 4), dtype=np.uint8)
        same, did_large = erw.enhance_world_texture_2x(large, has_cutout_alpha=False)
        self.assertFalse(did_large)
        self.assertEqual(same.shape, (512, 256, 4))

    def test_synthetic_rkpt_v1_write_and_read_roundtrip(self) -> None:
        solid = erw.DecodedWorldSolid(
            name="TRN_SYNTH_ROAD",
            name_hash=0x01020304,
            texture_hashes=[0x11223344],
            positions_metal=np.array([[0, 0, 0], [10, 0, 0], [0, 0, -10]], dtype=np.float32),
            normals_metal=np.array([[0, 1, 0], [0, 1, 0], [0, 1, 0]], dtype=np.float32),
            uvs_metal=np.array([[0, 0], [1, 0], [0, 1]], dtype=np.float32),
            vertex_ao=np.array([255, 240, 220], dtype=np.uint8),
            vertex_tex_slot=np.array([0, 0, 0], dtype=np.int32),
            indices=np.array([0, 1, 2], dtype=np.uint32),
            aabb_min_metal=(0.0, 0.0, -10.0),
            aabb_max_metal=(10.0, 0.0, 0.0),
        )
        mat = erw.classify_world_material("TRNS_ALL_LA_PAVEMENTA_Y", "TRN_SYNTH_ROAD", False, 0, 0x11223344)
        tex = erw.DecodedRkptTexture("TRNS_ALL_LA_PAVEMENTA_Y", 0x11223344, 4, 4, bytes([128] * 64), erw.TEX_FLAG_PS2_0X80_NORMALIZED, "synth")
        xform = erw.ps2_instance_to_metal_4x3([8192, 0, 0, 0, 8192, 0, 0, 0, 8192], (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))

        with tempfile.TemporaryDirectory() as td:
            out_rkpt = Path(td) / "synth.rkpt"
            erw.write_rkpt_v1(
                out_path=out_rkpt,
                sections_meta=[("A47", 47, 0, 1, (0.0, 0.0, 0.0), 100.0, (0.0, 0.0, -10.0), (10.0, 0.0, 0.0))],
                meshes=[solid],
                mesh_vert_mats=[np.array([0, 0, 0], dtype=np.int32)],
                mesh_flags=[erw.MESH_FLAG_DRIVABLE_ROAD | erw.MESH_FLAG_TERRAIN_CHOP],
                instances=[(0, 0, 0, xform)],
                materials=[mat],
                textures=[tex],
                world_bounds_min=(0.0, 0.0, -10.0),
                world_bounds_max=(10.0, 0.0, 0.0),
                spawn_pos=(3.0, 0.0, -3.0),
                spawn_yaw_rad=0.25,
                ps2_origin=(1000.0, 2000.0, 0.0),
            )
            hdr = erw.read_rkpt_header(out_rkpt)
            self.assertEqual(hdr["magic"], "RKPT")
            self.assertEqual(hdr["version"], 1)
            self.assertEqual(hdr["sectionCount"], 1)
            self.assertEqual(hdr["meshCount"], 1)
            self.assertEqual(hdr["instanceCount"], 1)
            self.assertEqual(hdr["materialCount"], 1)
            self.assertEqual(hdr["textureCount"], 1)
            self.assertEqual(hdr["totalVertexCount"], 3)
            self.assertEqual(hdr["totalIndexCount"], 3)
            self.assertAlmostEqual(hdr["spawnYawRad"], 0.25, places=5)


class TestRockportLiveISOReadOnly(unittest.TestCase):
    def test_live_rosewood_and_downtown_exports(self) -> None:
        iso = _default_iso()
        if not iso.is_file():
            self.skipTest("Untouched PS2 NFSMW ISO not present; skipping live Rockport export verification")

        out_dir = REPO_ROOT / "build" / "rockport_world"
        expected_secs = {"rosewood": 25, "downtown": 28}
        for dist, exp_sec_cnt in expected_secs.items():
            rkpt_path = out_dir / f"{dist}.rkpt"
            manifest_path = out_dir / f"{dist}_manifest.json"
            layout_png = out_dir / f"{dist}_layout_check.png"
            if not rkpt_path.is_file() or not manifest_path.is_file():
                erw.export_rockport_district(iso, dist, out_dir, render_layout=True)

            hdr = erw.read_rkpt_header(rkpt_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(hdr["magic"], "RKPT")
            self.assertEqual(hdr["version"], 1)
            self.assertEqual(hdr["sectionCount"], exp_sec_cnt)
            self.assertGreater(hdr["meshCount"], 400)
            self.assertGreater(hdr["instanceCount"], 2000)
            self.assertLess(hdr["fileSize"], 110 * 1024 * 1024)
            self.assertEqual(manifest["unresolved_instances"], 0)
            self.assertGreaterEqual(manifest["texture_resolved_pct"], 97.5)
            self.assertGreater(manifest["texture_enhanced_2x_count"], 100)
            self.assertGreater(manifest["filtered_event_barrier_instances"], 100)
            self.assertTrue(manifest["spawn_verification"]["verified"])
            self.assertGreaterEqual(manifest["spawn_verification"]["forward_road_run_m"], 36.0)
            self.assertGreaterEqual(manifest["spawn_verification"]["rear_road_run_m"], 18.0)
            self.assertIn("ROAD_ASPHALT", manifest["surface_class_triangles"])
            self.assertGreater(manifest["surface_class_triangles"]["ROAD_ASPHALT"], 4000)

            # Verify binary RkptMaterial foliage bumpStrength >= 1.25
            raw_rkpt = rkpt_path.read_bytes()
            mat_off = hdr["materialsOffset"]
            foliage_mats = 0
            for i in range(hdr["materialCount"]):
                _dti, scls, _mfl, _rgh, _met, _emi, bump, _hsh = struct.unpack_from(
                    erw.MATERIAL_FMT, raw_rkpt, mat_off + i * erw.MATERIAL_SIZE
                )
                if scls == erw.SURFACE_FOLIAGE_CUTOUT:
                    foliage_mats += 1
                    self.assertGreaterEqual(bump, 1.25)
            self.assertGreater(foliage_mats, 5)

            # Verify ZERO Free-Roam-invisible race-event chevron barrier hashes survive in binary RkptMesh table
            forbidden_hashes = {0x2483F271, 0x54A4FC3F, 0x33363E74}
            m_off = hdr["meshesOffset"]
            mesh_hashes = {
                struct.unpack_from("<I", raw_rkpt, m_off + i * erw.MESH_SIZE)[0]
                for i in range(hdr["meshCount"])
            }
            self.assertEqual(mesh_hashes & forbidden_hashes, set())

            # Verify ENHANCED_2X flag is set on upscaled RkptTexture records
            t_off = hdr["texturesOffset"]
            enhanced_flag_cnt = sum(
                1
                for i in range(hdr["textureCount"])
                if (struct.unpack_from(erw.TEXTURE_FMT, raw_rkpt, t_off + i * erw.TEXTURE_SIZE)[6] & erw.TEX_FLAG_ENHANCED_2X)
            )
            self.assertEqual(enhanced_flag_cnt, manifest["texture_enhanced_2x_count"])

            # Verify no personal paths leaked in manifest
            raw_manifest = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", raw_manifest)
            self.assertNotIn("C:\\Users\\", raw_manifest)

            # Verify layout PNG is non-empty and completely Pillow-decodable
            self.assertTrue(layout_png.is_file())
            self.assertGreater(layout_png.stat().st_size, 10_000)
            with Image.open(layout_png) as im:
                im.load()
                self.assertEqual(im.size, (1200, 1200))


if __name__ == "__main__":
    unittest.main()
