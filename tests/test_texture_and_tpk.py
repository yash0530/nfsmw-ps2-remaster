#!/usr/bin/env python3
"""Unit tests for PBR-to-PS2 diffuse baker, 4K dual-theme HUD kit, PS2 TPK inspector,
and texture_pipeline check-env preflight.

Runs completely offline in CI using synthetic fixtures; includes an optional live
read-only ISO verification test that automatically skips when the local ISO is absent.
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
from typing import List, Tuple

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import bake_pbr_to_diffuse as baker
import generate_hud_pack as hudgen
import texture_pipeline as tpipe
import tpk_inspect as tpki


# ---------------------------------------------------------------------------
# Synthetic PS2 TPK & JDLZ Binary Fixture Builders (100% clean-room test data)
# ---------------------------------------------------------------------------


def _ea_chunk(chunk_id: int, payload: bytes) -> bytes:
    return struct.pack("<II", chunk_id, len(payload)) + payload


def _make_tex_info_and_plat(
    name: str,
    name_hash: int,
    width: int,
    height: int,
    mip_count: int = 1,
    ps2_psm: int = 0x14,
    num_pal: int = 16,
    img_size: int = 2048,
    pal_size: int = 64,
) -> Tuple[bytes, bytes]:
    info = bytearray(tpki.TEXTURE_INFO_SIZE)
    raw_name = name.encode("ascii")[:23]
    info[12 : 12 + len(raw_name)] = raw_name
    struct.pack_into("<8I", info, 36, name_hash, 0x001A93CF, 0, 0x40, 0, img_size, pal_size, img_size)
    img_cmp = 0x04 if ps2_psm == 0x14 else (0x08 if ps2_psm == 0x13 else 0x20)
    sw = int(round(np.log2(max(1, width))))
    sh = int(round(np.log2(max(1, height))))
    struct.pack_into("<HHBBBBHB", info, 68, width, height, sw, sh, img_cmp, 0, num_pal, mip_count)

    plat = bytearray(tpki.TEXTURE_PLAT_SIZE)
    plat[4] = ps2_psm
    struct.pack_into("<HH", plat, 16, width, height)
    return bytes(info), bytes(plat)


def _encode_jdlz_literals_only(raw: bytes) -> bytes:
    """Valid JDLZ v2 stream encoding raw bytes as literal runs (for deterministic unit tests)."""
    body = bytearray()
    pos = 0
    n = len(raw)
    first_chunk = True
    while pos < n:
        # flags1 = 0x00 -> all 8 slots are raw literal bytes
        body.append(0x00)
        if first_chunk:
            # Decompressor initializes flags2 on first iteration
            body.append(0x00)
            first_chunk = False
        chunk = raw[pos : pos + 8]
        body.extend(chunk)
        pos += len(chunk)
    header = struct.pack("<4sBBHII", b"JDLZ", 2, 16, 0, len(raw), len(body))
    return header + bytes(body)


def _build_synthetic_uncompressed_tpk(
    pack_name: str,
    filename: str,
    specs: List[Tuple[str, int, int, int, int, int]],
) -> bytes:
    """Build a valid PS2 uncompressed 0xB3300000 TPK container."""
    hdr = bytearray(tpki.TPK_HEADER_SIZE)
    struct.pack_into("<I", hdr, 0, 5)
    pn = pack_name.encode("ascii")[:27]
    fn = filename.encode("ascii")[:63]
    hdr[4 : 4 + len(pn)] = pn
    hdr[32 : 32 + len(fn)] = fn
    struct.pack_into("<I", hdr, 96, 0x12345678)

    hash_tbl = bytearray()
    info_tbl = bytearray()
    plat_tbl = bytearray()
    for name, thash, w, h, mips, psm in specs:
        hash_tbl.extend(struct.pack("<II", thash, 0))
        num_pal = 16 if psm == 0x14 else (256 if psm == 0x13 else 0)
        ti, tp = _make_tex_info_and_plat(name, thash, w, h, mips, psm, num_pal=num_pal)
        info_tbl.extend(ti)
        plat_tbl.extend(tp)

    info_container = _ea_chunk(
        tpki.CHUNK_TPK_INFO,
        _ea_chunk(tpki.CHUNK_TPK_HEADER, bytes(hdr))
        + _ea_chunk(tpki.CHUNK_TPK_HASH_TABLE, bytes(hash_tbl))
        + _ea_chunk(tpki.CHUNK_TPK_TEX_INFO, bytes(info_tbl))
        + _ea_chunk(tpki.CHUNK_TPK_TEX_PLAT, bytes(plat_tbl)),
    )
    data_container = _ea_chunk(
        tpki.CHUNK_TPK_DATA,
        _ea_chunk(tpki.CHUNK_TPK_DATA_INFO, b"\x00" * 24)
        + _ea_chunk(tpki.CHUNK_TPK_DATA_PAYLOAD, b"\x00" * 256),
    )
    return _ea_chunk(tpki.CHUNK_TPK_ROOT, info_container + data_container)


def _build_synthetic_streamed_tpk(
    pack_name: str,
    specs: List[Tuple[str, int, int, int, int, int]],
) -> bytes:
    """Build a valid PS2 streamed 0xB3300000 TPK with 0x33310003 JDLZ compressed blocks."""
    hdr = bytearray(tpki.TPK_HEADER_SIZE)
    struct.pack_into("<I", hdr, 0, 5)
    pn = pack_name.encode("ascii")[:27]
    hdr[4 : 4 + len(pn)] = pn
    struct.pack_into("<I", hdr, 96, 0xCAFEBABE)

    hash_tbl = bytearray()
    blocks: List[Tuple[int, bytes, int]] = []
    for name, thash, w, h, mips, psm in specs:
        hash_tbl.extend(struct.pack("<II", thash, 0))
        ti, tp = _make_tex_info_and_plat(name, thash, w, h, mips, psm)
        raw_uncompressed = (b"\x55" * 128) + ti + tp
        jdlz_blk = _encode_jdlz_literals_only(raw_uncompressed)
        blocks.append((thash, jdlz_blk, len(raw_uncompressed)))

    # Calculate exact offsets from start of 0xB3300000 chunk
    hdr_chunk = _ea_chunk(tpki.CHUNK_TPK_HEADER, bytes(hdr))
    hash_chunk = _ea_chunk(tpki.CHUNK_TPK_HASH_TABLE, bytes(hash_tbl))
    stream_tbl_size = len(blocks) * tpki.STREAM_ENTRY_SIZE
    info_payload_size = len(hdr_chunk) + len(hash_chunk) + 8 + stream_tbl_size
    data_info_chunk = _ea_chunk(tpki.CHUNK_TPK_DATA_INFO, b"\x00" * 24)

    # Offsets relative to 0xB3300000 start:
    # 8 (root hdr) + 8 (info hdr) + info_payload_size + 8 (data hdr) + len(data_info_chunk) + 8 (payload hdr)
    payload_base_off = 8 + 8 + info_payload_size + 8 + len(data_info_chunk) + 8

    stream_tbl = bytearray()
    data_payload = bytearray()
    cur_off = payload_base_off
    for thash, blk, usize in blocks:
        stream_tbl.extend(struct.pack("<6I", thash, cur_off, len(blk), usize, 0x100, 0))
        data_payload.extend(blk)
        cur_off += len(blk)

    info_container = _ea_chunk(
        tpki.CHUNK_TPK_INFO,
        hdr_chunk + hash_chunk + _ea_chunk(tpki.CHUNK_TPK_STREAM_TABLE, bytes(stream_tbl)),
    )
    data_container = _ea_chunk(
        tpki.CHUNK_TPK_DATA,
        data_info_chunk + _ea_chunk(tpki.CHUNK_TPK_DATA_PAYLOAD, bytes(data_payload)),
    )
    return _ea_chunk(tpki.CHUNK_TPK_ROOT, info_container + data_container)


# ---------------------------------------------------------------------------
# 1. PBR-to-PS2-Diffuse Baker Tests
# ---------------------------------------------------------------------------


class TestBakePBRToDiffuse(unittest.TestCase):
    def test_srgb_linear_roundtrip(self) -> None:
        vals = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        rt = baker.linear_to_srgb(baker.srgb_to_linear(vals))
        self.assertLess(float(np.max(np.abs(rt - vals))), 1e-4)

    def test_periodic_fbm_is_seamless(self) -> None:
        fbm = baker.periodic_fbm(128, 128, base_freq=4, octaves=4, seed=42, domain_warp=0.25)
        self.assertEqual(fbm.shape, (128, 128))
        rgb = np.repeat(np.clip(fbm * 255.0, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)
        rep = baker.check_seamless_edges(rgb)
        self.assertTrue(rep.is_seamless, f"Periodic fBm should be seamless: {rep}")

    def test_directional_sun_normal_shading_and_dx_flip(self) -> None:
        h, w = 64, 64
        diffuse = np.full((h, w, 3), 140, dtype=np.uint8)
        # Left half faces +X (nx = +0.6), right half faces -X (nx = -0.6)
        normal = np.full((h, w, 3), [128, 128, 255], dtype=np.uint8)
        normal[:, : w // 2, 0] = 205
        normal[:, w // 2 :, 0] = 50

        # Sun from +X (azimuth=0, elevation=35)
        cfg = baker.BakeConfig(sun_elevation_deg=35.0, sun_azimuth_deg=0.0, cavity_strength=0.0, specular_strength=0.0)
        baked, _ = baker.bake_pbr_arrays(diffuse, normal_rgb=normal, config=cfg)
        left_mean = float(np.mean(baked[:, : w // 2]))
        right_mean = float(np.mean(baked[:, w // 2 :]))
        self.assertGreater(left_mean, right_mean + 15.0)

        # Now test Y-axis OpenGL vs DirectX flip with sun at +Y (azimuth=90)
        normal_y = np.full((h, w, 3), [128, 128, 255], dtype=np.uint8)
        normal_y[: h // 2, :, 1] = 205
        normal_y[h // 2 :, :, 1] = 50
        cfg_gl = baker.BakeConfig(sun_elevation_deg=35.0, sun_azimuth_deg=90.0, normal_convention="opengl", cavity_strength=0.0, specular_strength=0.0)
        cfg_dx = baker.BakeConfig(sun_elevation_deg=35.0, sun_azimuth_deg=90.0, normal_convention="directx", cavity_strength=0.0, specular_strength=0.0)
        b_gl, _ = baker.bake_pbr_arrays(diffuse, normal_rgb=normal_y, config=cfg_gl)
        b_dx, _ = baker.bake_pbr_arrays(diffuse, normal_rgb=normal_y, config=cfg_dx)
        self.assertGreater(float(np.mean(b_gl[: h // 2, :])), float(np.mean(b_gl[h // 2 :, :])))
        self.assertLess(float(np.mean(b_dx[: h // 2, :])), float(np.mean(b_dx[h // 2 :, :])))

    def test_ao_and_wet_puddle_baking(self) -> None:
        h, w = 128, 128
        rng = np.random.default_rng(7)
        # Subtle seamless noisy asphalt base
        grain = baker.periodic_fbm(h, w, base_freq=8, octaves=3, seed=7, domain_warp=0.0)
        base_v = np.clip(95.0 + (grain - 0.5) * 35.0, 40, 160).astype(np.uint8)
        diffuse = np.stack([base_v, base_v, base_v], axis=2)
        ao = np.full((h, w), 255, dtype=np.uint8)
        ao[:, 60:68] = 60  # dark crack strip
        rough = np.full((h, w), 210, dtype=np.uint8)

        dry_cfg = baker.BakeConfig(wetness=0.0, ao_strength=0.85)
        wet_cfg = baker.BakeConfig(wetness=0.70, ao_strength=0.85, puddle_scale=4, puddle_seed=2026)

        dry_out, dry_stats = baker.bake_pbr_arrays(diffuse, roughness_gray=rough, ao_gray=ao, config=dry_cfg)
        wet_out, wet_stats = baker.bake_pbr_arrays(diffuse, roughness_gray=rough, ao_gray=ao, config=wet_cfg)

        # AO crack strip must be measurably darker than surrounding asphalt
        self.assertLess(float(np.mean(dry_out[:, 61:67])), float(np.mean(dry_out[:, 10:40])) - 20.0)

        # Wet output must generate puddles, dampen asphalt, and remain seamless
        self.assertGreater(wet_stats["puddle_coverage"], 0.10)
        self.assertGreater(wet_stats["damp_coverage"], 0.40)
        self.assertLess(wet_stats["output_mean_luma"], dry_stats["output_mean_luma"])
        self.assertTrue(baker.check_seamless_edges(wet_out).is_seamless)
        _ = rng  # keep linter happy

    def test_non_seamless_gradient_detected(self) -> None:
        grad = np.tile(np.linspace(10, 245, 128, dtype=np.uint8)[None, :], (128, 1))
        rgb = np.stack([grad, grad, grad], axis=2)
        rep = baker.check_seamless_edges(rgb)
        self.assertFalse(rep.is_seamless)
        self.assertGreater(rep.lr_seam_ratio, 10.0)

    def test_discover_polyhaven_arm_and_cli_pot_resize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            mat_dir = tdir / "road_mat"
            mat_dir.mkdir()
            # Non-PoT 150x150 input maps using Poly Haven naming (including packed ARM!)
            Image.fromarray(np.full((150, 150, 3), 120, dtype=np.uint8)).save(mat_dir / "road_diff_1k.png")
            Image.fromarray(np.full((150, 150, 3), [128, 128, 255], dtype=np.uint8)).save(mat_dir / "road_nor_gl_1k.png")
            Image.fromarray(np.full((150, 150, 3), [220, 180, 0], dtype=np.uint8)).save(mat_dir / "road_arm_1k.png")
            Image.fromarray(np.full((150, 150), 128, dtype=np.uint8)).save(mat_dir / "road_disp_1k.png")
            Image.fromarray(np.full((150, 150), 200, dtype=np.uint8)).save(mat_dir / "road_opacity_1k.png")

            maps = baker.discover_pbr_maps(mat_dir)
            self.assertEqual(maps.diffuse.name, "road_diff_1k.png")
            self.assertEqual(maps.normal.name, "road_nor_gl_1k.png")
            self.assertEqual(maps.arm.name, "road_arm_1k.png")
            self.assertEqual(maps.displacement.name, "road_disp_1k.png")
            self.assertEqual(maps.opacity.name, "road_opacity_1k.png")

            out_png = tdir / "baked_road.png"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = baker.main(["--dir", str(mat_dir), "--out", str(out_png), "--wetness", "0.5", "--json"])
            self.assertEqual(rc, 0)
            rep = json.loads(buf.getvalue())
            self.assertEqual(rep["output_size"], [128, 128])
            self.assertEqual(rep["mode"], "RGBA")
            self.assertTrue(rep["seamless_check"]["is_seamless"])

            with Image.open(out_png) as img:
                self.assertEqual(img.size, (128, 128))
                self.assertEqual(img.mode, "RGBA")
                alpha = np.asarray(img)[:, :, 3]
                self.assertTrue(np.all(alpha == 200))


# ---------------------------------------------------------------------------
# 2. Dual-Theme 4K HUD Kit Generator Tests
# ---------------------------------------------------------------------------


class TestGenerateHUDPack(unittest.TestCase):
    EXPECTED_ASSETS = (
        "hud_tach_dial.png",
        "hud_tach_needle.png",
        "hud_nos_arc.png",
        "hud_speedbreaker_arc.png",
        "hud_speed_plate.png",
        "hud_minimap_frame.png",
        "hud_player_arrow.png",
        "hud_heat_1.png",
        "hud_heat_2.png",
        "hud_heat_3.png",
        "hud_heat_4.png",
        "hud_heat_5.png",
    )

    def test_generate_both_themes_and_verify_cutout_and_palettes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hud_pack"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = hudgen.main(
                    ["--theme", "all", "--out", str(root), "--size", "512", "--supersample", "1", "--json"]
                )
            self.assertEqual(rc, 0)
            reports = json.loads(buf.getvalue())
            self.assertEqual(len(reports), 2)

            dials = {}
            for theme_name in ("motorsport2026", "rockport_neon"):
                tdir = root / theme_name
                self.assertTrue((tdir / "hud_contact_sheet.png").is_file())
                for fname in self.EXPECTED_ASSETS:
                    fp = tdir / fname
                    self.assertTrue(fp.is_file(), f"Missing {fname} for theme {theme_name}")
                    with Image.open(fp) as img:
                        self.assertEqual(img.mode, "RGBA")
                        self.assertTrue(baker.is_power_of_two(img.width))
                        self.assertTrue(baker.is_power_of_two(img.height))
                        arr = np.asarray(img)
                        # Must contain both transparent background and visible artwork
                        self.assertEqual(int(arr[:, :, 3].min()), 0)
                        self.assertGreater(int(arr[:, :, 3].max()), 200)
                        if fname == "hud_tach_dial.png":
                            dials[theme_name] = arr
                        if fname == "hud_minimap_frame.png":
                            # Center of minimap frame MUST be transparent cutout for live roads
                            cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
                            self.assertEqual(int(arr[cy, cx, 3]), 0)

            # Themes must produce visually distinct outputs
            diff = float(np.mean(np.abs(dials["motorsport2026"].astype(np.float32) - dials["rockport_neon"].astype(np.float32))))
            self.assertGreater(diff, 2.0)


# ---------------------------------------------------------------------------
# 3. PS2 EA 0xB3300000 TPK Inspector Tests
# ---------------------------------------------------------------------------


class TestTPKInspector(unittest.TestCase):
    def test_parse_synthetic_uncompressed_tpk(self) -> None:
        raw = _build_synthetic_uncompressed_tpk(
            pack_name="INGAME",
            filename=r"Global\HudTexturesSingleRace.tpk",
            specs=[
                ("METER_BACKING", 0x20865C0A, 128, 128, 1, 0x14),
                ("RADAR_DIRECTIONARROW", 0x24196AD4, 32, 32, 1, 0x13),
                ("SKY_SUN_GLOW", 0xAABBCCDD, 256, 256, 4, 0x00),
            ],
        )
        containers = tpki.parse_tpk_bytes(raw, source="synthetic_hud.bin")
        self.assertEqual(len(containers), 1)
        c = containers[0]
        self.assertEqual(c.pack_name, "INGAME")
        self.assertEqual(c.version, 5)
        self.assertEqual(c.layout, "uncompressed")
        self.assertEqual(len(c.textures), 3)
        self.assertEqual(c.decode_errors, [])

        self.assertEqual(c.textures[0].name, "METER_BACKING")
        self.assertEqual(c.textures[0].name_hash, 0x20865C0A)
        self.assertEqual((c.textures[0].width, c.textures[0].height), (128, 128))
        self.assertEqual(c.textures[0].ps2_psm_name, "PSMT4")

        self.assertEqual(c.textures[1].ps2_psm_name, "PSMT8")
        self.assertEqual(c.textures[2].ps2_psm_name, "PSMCT32")
        self.assertEqual(c.textures[2].mip_count, 4)

    def test_parse_synthetic_streamed_jdlz_tpk(self) -> None:
        raw = _build_synthetic_streamed_tpk(
            pack_name="CARTEXTURES",
            specs=[
                ("INJEN_WHITE_RECT", 0x001F39C0, 128, 32, 1, 0x14),
                ("VOLK_WHEEL_01", 0x028F5AD9, 128, 128, 2, 0x13),
            ],
        )
        containers = tpki.parse_tpk_bytes(raw, source="synthetic_cars.bin", decompress_streamed=True)
        self.assertEqual(len(containers), 1)
        c = containers[0]
        self.assertEqual(c.layout, "streamed")
        self.assertEqual(c.decode_errors, [])
        self.assertEqual(len(c.textures), 2)
        self.assertEqual(c.textures[0].name, "INJEN_WHITE_RECT")
        self.assertEqual(c.textures[0].storage, "JDLZ")
        self.assertEqual((c.textures[0].width, c.textures[0].height), (128, 32))
        self.assertEqual(c.textures[1].name, "VOLK_WHEEL_01")
        self.assertEqual(c.textures[1].ps2_psm_name, "PSMT8")
        self.assertEqual(c.textures[1].mip_count, 2)

        # Fast metadata-only scan (--no-decompress)
        fast = tpki.parse_tpk_bytes(raw, source="synthetic_cars.bin", decompress_streamed=False)
        self.assertEqual(len(fast[0].textures), 2)
        self.assertEqual(fast[0].textures[0].storage, "streamed_skipped")

    def test_parse_multi_tpk_bundle_and_cli_json(self) -> None:
        tpk1 = _build_synthetic_uncompressed_tpk("GLOBAL_A", "a.tpk", [("TEX_A", 0x11111111, 64, 64, 1, 0x14)])
        tpk2 = _build_synthetic_uncompressed_tpk("GLOBAL_B", "b.tpk", [("TEX_B", 0x22222222, 128, 64, 1, 0x13)])
        with tempfile.TemporaryDirectory() as tmp:
            bun_path = Path(tmp) / "bundle.bun"
            out_json = Path(tmp) / "catalog.json"
            bun_path.write_bytes(tpk1 + tpk2)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tpki.main(["--file", str(bun_path), "--json", "--out", str(out_json)])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["container_count"], 2)
            self.assertEqual(payload["total_textures"], 2)
            self.assertEqual(payload["format_totals"], {"PSMT4": 1, "PSMT8": 1})
            self.assertTrue(out_json.is_file())

    def test_live_iso_tpk_catalog_read_only(self) -> None:
        env_iso = os.environ.get("NFSMW_ISO_PATH")
        iso_path = (
            Path(env_iso)
            if env_iso
            else Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
        )
        if not iso_path.is_file():
            self.skipTest("Local untouched PS2 NFSMW ISO not present (CI offline mode).")

        containers = tpki.inspect_iso_tpks(iso_path, targets=tpki.DEFAULT_ISO_TARGETS, decompress_streamed=True)
        by_src = {c.source: c for c in containers}
        self.assertEqual(len(by_src[r"GLOBAL\HUDTEXRACE.BIN"].textures), 23)
        self.assertEqual(len(by_src[r"CARS\TEXTURES.BIN"].textures), 465)
        self.assertEqual(len(by_src[r"CARS\WHEELS\TEXTURES.BIN"].textures), 82)
        self.assertEqual(len(by_src[r"GLOBAL\DYNTEX.BIN"].textures), 151)
        for c in containers:
            self.assertEqual(c.decode_errors, [], f"Decode errors in {c.source}: {c.decode_errors}")


# ---------------------------------------------------------------------------
# 4. texture_pipeline.py check-env Preflight Tests
# ---------------------------------------------------------------------------


class TestTexturePipelineCheckEnv(unittest.TestCase):
    def test_check_env_cli_and_schema(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tpipe.main(["check-env", "--json"])
        self.assertEqual(rc, 0)
        rep = json.loads(buf.getvalue())
        self.assertIn("system", rep)
        self.assertIn("packages", rep)
        self.assertIn("acceleration", rep)
        self.assertIn("dds_encoders", rep)
        self.assertIn("readiness", rep)

        # Core imaging deps must be detected as installed in test env
        self.assertTrue(rep["packages"]["pillow"]["installed"])
        self.assertTrue(rep["packages"]["numpy"]["installed"])
        self.assertTrue(rep["readiness"]["pbr_and_hud_ready"])

        # All 3 macOS arm64 BC7 encoders must be inspected with install hints
        for enc in ("dds_tool", "kram", "cuttlefish"):
            self.assertIn(enc, rep["dds_encoders"])
            self.assertTrue(rep["dds_encoders"][enc]["install_hint"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
