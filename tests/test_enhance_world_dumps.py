#!/usr/bin/env python3
"""Unit tests for tools/enhance_world_dumps.py.

Covers:
  - PCSX2 v2.6.3 3-part & 2-part stem parsing and exact lowercase stem preservation
  - Multi-CLUT quarantine (same TexHash with >= 2 ClutHash values)
  - Ultra-strict UI / icon / font / alpha-cutout / cubemap / minimap / taillight / TPK filtering
  - PS2 GS uniform 0x80 (128) alpha preservation (NEVER promoted to 255!)
  - Material classification (asphalt, masonry_building, foliage_terrain, sky)
  - Detail-preserving 4x enhancement (aspect-preserving PoT sizing, Sobel AO/relief, sharpness gain)
  - End-to-end native `rockport_dds` BC7_UNORM encoding, DX10 DDS validation, and preview sheet
"""

from __future__ import annotations

import json
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

import build_4k_texture_pack as pack_mod  # noqa: E402
import enhance_world_dumps as ewd  # noqa: E402
import install_macos_texture_tools as tools_mod  # noqa: E402


def _make_brick_texture(w: int = 128, h: int = 128, alpha: int = 128) -> np.ndarray:
    """Synthetic opaque masonry brick wall texture with PS2 0x80 alpha."""
    rng = np.random.default_rng(42)
    yy, xx = np.mgrid[0:h, 0:w]
    mortar_h = (yy % 16) < 2
    row_idx = yy // 16
    mortar_v = ((xx + (row_idx % 2) * 16) % 32) < 2
    mortar = mortar_h | mortar_v

    noise = rng.normal(0.0, 9.0, size=(h, w))
    r = np.where(mortar, 122.0, 146.0) + noise
    g = np.where(mortar, 118.0, 92.0) + noise * 0.8
    b = np.where(mortar, 112.0, 74.0) + noise * 0.7
    a = np.full((h, w), alpha, dtype=np.uint8)
    rgb = np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)
    return np.dstack([rgb, a])


def _make_asphalt_road_texture(w: int = 128, h: int = 128, alpha: int = 128) -> np.ndarray:
    """Synthetic dark gray asphalt road with crisp yellow double center lines."""
    rng = np.random.default_rng(77)
    base = 78.0 + rng.normal(0.0, 11.0, size=(h, w))
    r = base.copy()
    g = base.copy()
    b = base * 1.03

    # Double yellow center stripe
    stripe = ((np.arange(w)[None, :] >= w // 2 - 5) & (np.arange(w)[None, :] <= w // 2 - 3)) | (
        (np.arange(w)[None, :] >= w // 2 + 2) & (np.arange(w)[None, :] <= w // 2 + 4)
    )
    stripe = np.broadcast_to(stripe, (h, w))
    r[stripe] = 218.0
    g[stripe] = 178.0
    b[stripe] = 42.0

    rgb = np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)
    a = np.full((h, w), alpha, dtype=np.uint8)
    return np.dstack([rgb, a])


def _make_sky_texture(w: int = 128, h: int = 64, alpha: int = 128) -> np.ndarray:
    """Synthetic blue sky dome with soft cloud gradient."""
    yy = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    cloud = 0.5 + 0.5 * np.sin(xx * 6.283 * 2.0) * np.cos(yy * 3.1415 * 2.0)
    r = np.clip(70.0 + 115.0 * yy + 45.0 * cloud * (1.0 - yy), 0, 255)
    g = np.clip(115.0 + 95.0 * yy + 35.0 * cloud * (1.0 - yy), 0, 255)
    b = np.clip(185.0 + 45.0 * yy + 20.0 * cloud * (1.0 - yy), 0, 255)
    rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
    a = np.full((h, w), alpha, dtype=np.uint8)
    return np.dstack([rgb, a])


class TestEnhanceWorldDumps(unittest.TestCase):
    def test_parse_pcsx2_stems_3part_and_2part(self) -> None:
        p3 = ewd.parse_pcsx2_dump_stem("106EA7248D315E0B-8107762CC5490EA2-000019D4")
        self.assertTrue(p3.valid)
        self.assertEqual(p3.stem, "106ea7248d315e0b-8107762cc5490ea2-000019d4")
        self.assertEqual(p3.tex_hash, "106ea7248d315e0b")
        self.assertEqual(p3.clut_hash, "8107762cc5490ea2")
        self.assertEqual(p3.flags_hex, "000019d4")
        self.assertEqual(p3.psm, ewd.PSM_T4)

        p2 = ewd.parse_pcsx2_dump_stem("215aea45065087de-00001dc0")
        self.assertTrue(p2.valid)
        self.assertEqual(p2.stem, "215aea45065087de-00001dc0")
        self.assertEqual(p2.tex_hash, "215aea45065087de")
        self.assertIsNone(p2.clut_hash)
        self.assertEqual(p2.flags_hex, "00001dc0")
        self.assertEqual(p2.psm, ewd.PSM_CT32)

        bad = ewd.parse_pcsx2_dump_stem("not_a_hash_name")
        self.assertFalse(bad.valid)

    def test_multi_clut_quarantine(self) -> None:
        paths = [
            Path("aaaa111122223333-cccc111122223333-00001dd4.png"),
            Path("aaaa111122223333-dddd111122223333-00001dd4.png"),
            Path("bbbb111122223333-eeee111122223333-00001dd4.png"),
        ]
        mc = ewd.find_multi_clut_tex_hashes(paths)
        self.assertEqual(mc, {"aaaa111122223333"})

        brick = _make_brick_texture()
        dec_blocked = ewd.evaluate_dump_safety(paths[0], brick, multi_clut_hashes=mc)
        self.assertFalse(dec_blocked.allowed)
        self.assertEqual(dec_blocked.reason, "multi_clut_quarantine")

        dec_ok = ewd.evaluate_dump_safety(paths[2], brick, multi_clut_hashes=mc)
        self.assertTrue(dec_ok.allowed)
        self.assertEqual(dec_ok.material, "masonry_building")

    def test_safety_filters_reject_ui_icons_fonts_cubemaps_taillights_and_tpk(self) -> None:
        valid_path = Path("1122334455667788-aabbccddeeff0011-00001dd4.png")

        # 1. Small icon (<64px)
        small = _make_brick_texture(32, 32, alpha=128)
        self.assertFalse(ewd.evaluate_dump_safety(valid_path, small, multi_clut_hashes=set()).allowed)

        # 2. Extreme UI banner strip (aspect > 4.0)
        strip = _make_brick_texture(512, 64, alpha=128)
        dec_strip = ewd.evaluate_dump_safety(valid_path, strip, multi_clut_hashes=set())
        self.assertFalse(dec_strip.allowed)
        self.assertEqual(dec_strip.reason, "extreme_aspect")

        # 3. Transparent alpha-cutout UI icon / logo
        cutout = _make_brick_texture(128, 128, alpha=128)
        cutout[:40, :40, 3] = 0
        dec_cutout = ewd.evaluate_dump_safety(valid_path, cutout, multi_clut_hashes=set())
        self.assertFalse(dec_cutout.allowed)
        self.assertEqual(dec_cutout.reason, "non_opaque_alpha")

        # 4. Monochrome binary font atlas
        font = np.zeros((128, 128, 4), dtype=np.uint8)
        font[:, :, 3] = 128
        font[10:24, 10:118, :3] = 255
        font[40:54, 10:118, :3] = 255
        dec_font = ewd.evaluate_dump_safety(valid_path, font, multi_clut_hashes=set())
        self.assertFalse(dec_font.allowed)

        # 5. Monochrome minimap spline / car tribal vinyl (flat solid + sharp anti-aliased strokes)
        minimap = np.full((128, 128, 4), (42, 42, 42, 128), dtype=np.uint8)
        for k in range(12, 116):
            minimap[k - 4 : k + 4, k - 4 : k + 4, :3] = 110
            minimap[k - 2 : k + 2, k - 2 : k + 2, :3] = 220
            minimap[127 - k - 4 : 127 - k + 4, k - 4 : k + 4, :3] = 155
            minimap[127 - k - 2 : 127 - k + 2, k - 2 : k + 2, :3] = 205
        minimap[58:70, 12:116, :3] = 180
        minimap[12:116, 58:70, :3] = 195
        dec_map = ewd.evaluate_dump_safety(valid_path, minimap, multi_clut_hashes=set())
        self.assertFalse(dec_map.allowed)
        self.assertEqual(dec_map.reason, "monochrome_vinyl_or_minimap")

        # 6. Crimson car taillight (with realistic ribbed reflector shading)
        yy, xx = np.mgrid[0:64, 0:128]
        tail = np.zeros((64, 128, 4), dtype=np.uint8)
        tail[:, :, 0] = np.clip(145 + 55 * np.sin(yy * 0.45) + 25 * np.cos(xx * 0.15), 90, 240).astype(np.uint8)
        tail[:, :, 1] = np.clip(18 + 10 * np.sin(yy * 0.45), 5, 40).astype(np.uint8)
        tail[:, :, 2] = np.clip(14 + 8 * np.cos(xx * 0.15), 5, 35).astype(np.uint8)
        tail[:, :, 3] = 128
        dec_tail = ewd.evaluate_dump_safety(valid_path, tail, multi_clut_hashes=set())
        self.assertFalse(dec_tail.allowed)
        self.assertEqual(dec_tail.reason, "car_taillight_or_signal")

        # 7. Reflection cubemap (PSMCT32 dark corners, smooth spherical bright core)
        env_path = Path("215aea45065087de-00001dc0.png")
        yy_e, xx_e = np.mgrid[-1.0:1.0:128j, -1.0:1.0:128j]
        rr_e = np.sqrt(xx_e * xx_e + yy_e * yy_e)
        sphere = np.clip((1.0 - rr_e) * 230.0, 0.0, 230.0).astype(np.uint8)
        env = np.zeros((128, 128, 4), dtype=np.uint8)
        env[:, :, 0] = sphere
        env[:, :, 1] = sphere
        env[:, :, 2] = np.clip(sphere.astype(np.int32) + 12, 0, 255).astype(np.uint8)
        env[:, :, 3] = 128
        dec_env = ewd.evaluate_dump_safety(env_path, env, multi_clut_hashes=set())
        self.assertFalse(dec_env.allowed)
        self.assertEqual(dec_env.reason, "reflection_envmap")

        # 8. Circular wheel rim / hubcap (high 90-degree rotational symmetry)
        yy_w, xx_w = np.mgrid[-1.0:1.0:128j, -1.0:1.0:128j]
        rr_w = np.sqrt(xx_w * xx_w + yy_w * yy_w)
        ang_w = np.arctan2(yy_w, xx_w)
        rim = np.clip(115 + 65 * np.cos(rr_w * 14.0) + 35 * np.cos(ang_w * 8.0), 20, 235).astype(np.uint8)
        wheel = np.dstack([rim, rim, rim, np.full((128, 128), 128, dtype=np.uint8)])
        dec_wheel = ewd.evaluate_dump_safety(valid_path, wheel, multi_clut_hashes=set())
        self.assertFalse(dec_wheel.allowed)
        self.assertEqual(dec_wheel.reason, "circular_wheel_or_hubcap")

        # 9. TPK denylist protection (UI/LOADING/LOGO/VINYL blocked, SKY allowed)
        brick = _make_brick_texture()
        dec_tpk_ui = ewd.evaluate_dump_safety(
            valid_path,
            brick,
            multi_clut_hashes=set(),
            tpk_match_info={"score": 0.92, "tpk_name": "LOADING_ICON_01", "tpk_archive": "GLOBAL_INGAMEA"},
        )
        self.assertFalse(dec_tpk_ui.allowed)
        self.assertEqual(dec_tpk_ui.reason, "tpk_protected_ui_car_fx")

        sky = _make_sky_texture()
        dec_tpk_sky = ewd.evaluate_dump_safety(
            valid_path,
            sky,
            multi_clut_hashes=set(),
            tpk_match_info={"score": 0.89, "tpk_name": "SKY_MIDDAY_A_ENVIRONMEN", "tpk_archive": "GLOBAL_DYNTEX"},
        )
        self.assertTrue(dec_tpk_sky.allowed)
        self.assertEqual(dec_tpk_sky.material, "sky")

    def test_ps2_0x80_alpha_preservation_and_4x_enhancement(self) -> None:
        # PS2 uniform 0x80 (128) MUST stay exact 128 — NEVER promoted to 255!
        road = _make_asphalt_road_texture(128, 128, alpha=128)
        self.assertEqual(ewd.classify_world_material(road[:, :, :3]), "asphalt")

        enh_road, stats = ewd.enhance_world_texture_rgba(
            road,
            material="asphalt",
            scale=4,
            max_size=512,
            seed_key="test_road",
        )
        self.assertEqual(enh_road.shape, (512, 512, 4))
        self.assertTrue(np.all(enh_road[:, :, 3] == 128))
        self.assertEqual(stats["alpha_preserved_min"], 128)
        self.assertEqual(stats["alpha_preserved_max"], 128)
        # Sharpness should strictly improve over plain bilinear upscale
        self.assertGreater(stats["sharpness_gain"], 1.15)
        # Yellow road stripe center column must remain clearly yellow (R > B + 60)
        stripe_col = enh_road[:, 256 - 16 : 256 - 12, :3].astype(np.float32)
        self.assertGreater(float(stripe_col[:, :, 0].mean() - stripe_col[:, :, 2].mean()), 60.0)

        # Standard PC opaque 255 MUST stay exact 255!
        brick_255 = _make_brick_texture(128, 64, alpha=255)
        enh_brick, bstats = ewd.enhance_world_texture_rgba(
            brick_255,
            material="masonry_building",
            scale=4,
            max_size=512,
            seed_key="test_brick",
        )
        # Aspect ratio 2:1 preserved (128x64 * 4 = 512x256)
        self.assertEqual(enh_brick.shape, (256, 512, 4))
        self.assertTrue(np.all(enh_brick[:, :, 3] == 255))
        self.assertGreater(bstats["sharpness_gain"], 1.15)

        # Sky enhancement preserves 128 alpha & produces smooth output
        sky = _make_sky_texture(128, 64, alpha=128)
        enh_sky, _ = ewd.enhance_world_texture_rgba(
            sky, material="sky", scale=4, max_size=512, seed_key="test_sky"
        )
        self.assertEqual(enh_sky.shape, (256, 512, 4))
        self.assertTrue(np.all(enh_sky[:, :, 3] == 128))

    def test_v2_rosewood_road_strips_and_specular_alpha_storefronts(self) -> None:
        # 1. Vertical 64x512 Rosewood asphalt road strip with specular alpha (0..95) + yellow lines
        road_strip_path = Path("1bd97bedb7d7483b-7b3a65f48a3b7bd4-00002593.png")
        road_strip = _make_asphalt_road_texture(64, 512, alpha=0)
        # Matte tarmac has alpha=0, wheel tracks have subtle specular sheen alpha=45..95
        road_strip[:, 10:26, 3] = 64
        road_strip[:, 38:54, 3] = 95
        dec_road = ewd.evaluate_dump_safety(road_strip_path, road_strip, multi_clut_hashes=set())
        self.assertTrue(dec_road.allowed, dec_road.reason)
        self.assertEqual(dec_road.material, "asphalt")
        self.assertEqual(dec_road.alpha_mode, "ps2_specular_mask")
        enh_strip, s_stats = ewd.enhance_world_texture_rgba(
            road_strip, material="asphalt", scale=4, max_size=2048, seed_key="strip_test"
        )
        self.assertEqual(enh_strip.shape, (2048, 256, 4))
        self.assertEqual(s_stats["alpha_preserved_min"], 0)
        self.assertEqual(s_stats["alpha_preserved_max"], 95)

        # 2. 128x128 Rosewood storefront with matte stone (alpha=0) & glossy windows (alpha=64)
        store_path = Path("27e0b90621f81945-1f9b589b5c0c8ed1-00001dd4.png")
        store = _make_brick_texture(128, 128, alpha=0)
        store[24:64, 24:56, 3] = 64
        store[24:64, 72:104, 3] = 64
        dec_store = ewd.evaluate_dump_safety(store_path, store, multi_clut_hashes=set())
        self.assertTrue(dec_store.allowed, dec_store.reason)
        self.assertEqual(dec_store.material, "masonry_building")
        self.assertEqual(dec_store.alpha_mode, "ps2_specular_mask")

        # 3. 512x512 PS2 wet cracked asphalt (alpha=94..127, PSMT4) approved,
        #    while 512x512 car livery (alpha=12..128, PSMT8) is strictly REJECTED!
        wet_path = Path("4d1032308a1a8aa5-bbaa37201ef487e6-00002654.png")
        wet_road = _make_asphalt_road_texture(128, 128, alpha=94)
        wet_road[:64, :, 3] = 127
        dec_wet = ewd.evaluate_dump_safety(wet_path, wet_road, multi_clut_hashes=set())
        self.assertTrue(dec_wet.allowed, dec_wet.reason)
        self.assertEqual(dec_wet.alpha_mode, "ps2_wet_alpha")

        livery_path = Path("5566778899aabbcc-1122334455667788-00002653.png")
        livery = np.full((128, 128, 4), (210, 210, 210, 128), dtype=np.uint8)
        livery[20:108, 20:108, :3] = (25, 65, 165)
        livery[20:108, 20:108, 3] = 18
        dec_liv = ewd.evaluate_dump_safety(livery_path, livery, multi_clut_hashes=set())
        self.assertFalse(dec_liv.allowed)

        # 4. Tachometer HUD arc (alpha=0..127, flat black bg) & horizontal UI logo (256x64, alpha=0..128) REJECTED!
        hud_path = Path("1122334455667788-aabbccddeeff0011-00001dd4.png")
        tach = np.zeros((128, 128, 4), dtype=np.uint8)
        tach[16:32, 24:104, :3] = 240
        tach[16:32, 24:104, 3] = 127
        self.assertFalse(ewd.evaluate_dump_safety(hud_path, tach, multi_clut_hashes=set()).allowed)

        logo = np.full((64, 256, 4), (255, 255, 255, 0), dtype=np.uint8)
        logo[16:48, 32:224, :3] = 10
        logo[16:48, 32:224, 3] = 128
        self.assertFalse(ewd.evaluate_dump_safety(hud_path, logo, multi_clut_hashes=set()).allowed)

    def test_end_to_end_pipeline_bc7_dds_and_preview(self) -> None:
        encoder = tools_mod.find_rockport_dds()
        if encoder is None:
            self.skipTest("Native rockport_dds binary not available in test environment")

        with tempfile.TemporaryDirectory(prefix="test_ewd_") as tmp:
            tmp_path = Path(tmp)
            dumps_dir = tmp_path / "dumps"
            out_dir = tmp_path / "out" / "replacements"
            dumps_dir.mkdir(parents=True)

            # 2 valid world textures (asphalt + brick, uppercase input stem to test lowercasing)
            Image.fromarray(_make_asphalt_road_texture(64, 64, alpha=128)).save(
                dumps_dir / "AA11BB22CC33DD44-1122334455667788-00001994.png"
            )
            Image.fromarray(_make_brick_texture(128, 64, alpha=128)).save(
                dumps_dir / "bb22cc33dd44ee55-000019d4.png"
            )
            # 2 multi-CLUT collisions (must both be quarantined!)
            Image.fromarray(_make_brick_texture(64, 64, alpha=128)).save(
                dumps_dir / "deadbeef12345678-1111111111111111-00001994.png"
            )
            Image.fromarray(_make_brick_texture(64, 64, alpha=128)).save(
                dumps_dir / "deadbeef12345678-2222222222222222-00001994.png"
            )
            # 1 transparent UI icon (must be skipped!)
            ui_icon = _make_brick_texture(64, 64, alpha=128)
            ui_icon[:, :, 3] = 0
            ui_icon[16:48, 16:48, 3] = 255
            Image.fromarray(ui_icon).save(
                dumps_dir / "feedface87654321-3333333333333333-00001994.png"
            )

            manifest = ewd.enhance_dumps_directory(
                dumps_dir=dumps_dir,
                out_replacements_dir=out_dir,
                scale=4,
                max_size=512,
                tpk_dir=None,
                encoder_bin=encoder,
                workers=2,
                dds_threads=1,
            )

            counts = manifest["counts"]
            self.assertEqual(counts["scanned"], 5)
            self.assertEqual(counts["enhanced_bc7_dds"], 2)
            self.assertEqual(counts["skipped_total"], 3)
            self.assertEqual(counts["skipped_by_reason"].get("multi_clut_quarantine"), 2)
            self.assertEqual(counts["skipped_by_reason"].get("non_opaque_alpha"), 1)

            # Verify exact lowercase stems & valid DXGI_FORMAT_BC7_UNORM (98) DDS output
            dds_a = out_dir / "aa11bb22cc33dd44-1122334455667788-00001994.dds"
            dds_b = out_dir / "bb22cc33dd44ee55-000019d4.dds"
            self.assertTrue(dds_a.is_file())
            self.assertTrue(dds_b.is_file())

            val_a = pack_mod.validate_bc7_dds(dds_a, require_srgb=False)
            val_b = pack_mod.validate_bc7_dds(dds_b, require_srgb=False)
            self.assertTrue(val_a.valid, val_a.error)
            self.assertEqual((val_a.width, val_a.height), (256, 256))
            self.assertEqual(val_a.dxgi_format, pack_mod.DXGI_FORMAT_BC7_UNORM)
            self.assertGreater(val_a.mip_count, 1)

            self.assertTrue(val_b.valid, val_b.error)
            self.assertEqual((val_b.width, val_b.height), (512, 256))

            # Verify preview sheet and manifest exist and are valid
            preview_png = out_dir.parent / "preview_before_after.png"
            self.assertTrue(preview_png.is_file())
            with Image.open(preview_png) as check_im:
                check_im.load()
                self.assertGreater(check_im.width, 300)

            manifest_file = out_dir.parent / "manifest.json"
            self.assertTrue(manifest_file.is_file())
            parsed_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(parsed_manifest["counts"]["enhanced_bc7_dds"], 2)


if __name__ == "__main__":
    unittest.main()
