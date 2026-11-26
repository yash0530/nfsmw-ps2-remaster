"""Cross-platform offline + live unit tests for v2.5 sculpted 3D texture relief & baked AO."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_pcsx2_texture_pack as bpack  # noqa: E402
import pcsx2_tex_hash as pth  # noqa: E402
import texture_relief_3d as tr3d  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402


def _find_live_iso() -> Optional[Path]:
    env = os.environ.get("NFSMW_ISO")
    p = Path(env).expanduser() if env is not None else (Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso")
    return p if p.is_file() else None


def _make_brick_wall(h: int = 64, w: int = 64) -> np.ndarray:
    """Synthetic brick facade with darker mortar joints & a recessed dark window."""
    img = np.full((h, w, 4), (118, 86, 68, 128), dtype=np.uint8)
    for y in range(0, h, 8):
        img[y : y + 2, :, :3] = (54, 46, 40)
    for row_idx, y in enumerate(range(0, h, 8)):
        off = 8 if (row_idx % 2) else 0
        for x in range(off, w, 16):
            img[y : y + 8, x : x + 2, :3] = (54, 46, 40)
    img[20:44, 20:44, :3] = (34, 40, 48)
    return img


class TestTextureRelief3DOffline(unittest.TestCase):
    """Offline tests (100% Ubuntu Linux + macOS CI safe with NFSMW_ISO=/nonexistent)."""

    def test_material_classification_all_classes(self) -> None:
        cases = [
            ("ARC_BRICK01", r"TRACKS\STREAML2RA.BUN", "architecture"),
            ("BLD_DOWNTOWN_01", r"TRACKS\STREAML2RA.BUN", "architecture"),
            ("WAL_GREYCEMENT_001", r"TRACKS\STREAML2RA.BUN", "architecture"),
            ("CON_PILLAR_A", r"TRACKS\STREAML2RA.BUN", "architecture"),
            ("BRK_OLDWALL", r"TRACKS\STREAML2RA.BUN", "architecture"),
            ("WIN_OFFICE_01", r"TRACKS\STREAML2RA.BUN", "architecture"),
            ("ARC_ROADHOUSE_WALL", r"TRACKS\STREAML2RA.BUN", "architecture"),
            ("RO_ASPHALT_01", r"TRACKS\STREAML2RA.BUN", "road"),
            ("TRNS_ALL_LB_PAVEMENTA_N", r"TRACKS\STREAML2RA.BUN", "road"),
            ("SDW_CURB_01", r"TRACKS\STREAML2RA.BUN", "road"),
            ("CRB_STONE_A", r"TRACKS\STREAML2RA.BUN", "road"),
            ("ASPH_HIGHWAY", r"TRACKS\STREAML2RA.BUN", "road"),
            ("PAVE_SIDEWALK", r"TRACKS\STREAML2RA.BUN", "road"),
            ("BMWM3GTRE46_MISC", r"CARS\BMWM3GTRE46\TEXTURES.BIN", "vehicle"),
            ("WHEEL_BBS_01", r"WHEELS\WHEELS.BIN", "vehicle"),
            ("CARBONFIBRE", r"GLOBAL\GLOBALB.LZC", "vehicle"),
            ("ROTOR1", r"GLOBAL\GLOBALB.LZC", "vehicle"),
            ("TREAD", r"GLOBAL\GLOBALB.LZC", "vehicle"),
            ("GRILL_01", r"GLOBAL\GLOBALB.LZC", "vehicle"),
            ("ORG_CT_FALLTREE2", r"TRACKS\STREAML2RA.BUN", "foliage"),
            ("TREE_OAK_A", r"TRACKS\STREAML2RA.BUN", "foliage"),
            ("BUSH_RED_01", r"TRACKS\STREAML2RA.BUN", "foliage"),
            ("LEAF3", r"GLOBAL\INGAMEA.BUN", "foliage"),
            ("SGN_FWY_DOWNTOWNROCKPOR", r"TRACKS\STREAML2RA.BUN", "signage"),
            ("ADS_FITNESSB_BB_EA", r"TRACKS\STREAML2RA.BUN", "signage"),
            ("LICENSEPLATE", r"GLOBAL\GLOBALB.LZC", "signage"),
            ("BMWM3GTRE46_BADGING", r"CARS\BMWM3GTRE46\TEXTURES.BIN", "signage"),
            ("SKY_SUNSET_A_GRADIENT_A", r"GLOBAL\DYNTEX.BIN", "sky"),
            ("SHD_CP_BE_ROAD_A_03", r"TRACKS\STREAML2RA.BUN", "shadow_map"),
        ]
        for name, archive, expected in cases:
            self.assertEqual(tr3d.classify_relief_material(name, archive), expected, f"{name} ({archive})")

    def test_baked_ao_darkens_mortar_and_recess_crevices(self) -> None:
        wall = _make_brick_wall(64, 64)
        rgb_f = wall[:, :, :3].astype(np.float32) / 255.0
        ao, _lambert, _spec = tr3d.compute_relief_maps(rgb_f, tr3d.MATERIAL_PROFILES["architecture"], name="ARC_BRICK01")
        ao_flat_brick = float(ao[4:7, 4:14].mean())
        ao_mortar = float(ao[8:10, 4:14].mean())
        ao_window_recess = float(ao[22:26, 22:42].mean())
        self.assertGreater(ao_flat_brick, 0.90)
        self.assertLess(ao_mortar, ao_flat_brick - 0.14)
        self.assertLess(ao_window_recess, ao_flat_brick - 0.15)

        sculpted, mat, amode = tr3d.sculpt_texture_relief_rgba(wall, name="ARC_BRICK01", scale=4, small_scale=4)
        faithful, _, _ = bpack.enhance_faithful_rgba(wall, name="ARC_BRICK01", scale=4, small_scale=4)
        self.assertEqual((mat, amode), ("architecture", "opaque_128"))
        self.assertTrue((sculpted[:, :, 3] == 128).all())
        # Mortar groove at scaled y=32..39 should be darker in sculpted 3D than faithful
        self.assertLess(float(sculpted[32:40, 20:56, :3].mean()), float(faithful[32:40, 20:56, :3].mean()) - 4.0)

    def test_directional_sun_bevel_lit_vs_shadowed_edges(self) -> None:
        # Raised bright stone block centered on darker mortar bed
        block = np.full((64, 64, 4), (72, 72, 72, 128), dtype=np.uint8)
        block[16:48, 16:48, :3] = (148, 144, 138)
        out, mat, _ = tr3d.sculpt_texture_relief_rgba(block, name="ARC_STONEBLOCK_01", scale=4, small_scale=4)
        self.assertEqual(mat, "architecture")
        # Upper-left sun illuminates top (y~64..70) & left (x~64..70) bevels brighter than bottom (y~186..192) & right (x~186..192)
        top_bevel = float(out[62:70, 96:160, :3].mean())
        bot_bevel = float(out[186:194, 96:160, :3].mean())
        left_bevel = float(out[96:160, 62:70, :3].mean())
        right_bevel = float(out[96:160, 186:194, :3].mean())
        self.assertGreater(top_bevel, bot_bevel + 12.0)
        self.assertGreater(left_bevel, right_bevel + 10.0)
        # Warm golden key light makes lit bevel slightly warmer (higher R/B ratio) than shadowed edge
        top_rb = float(out[62:70, 96:160, 0].mean()) / max(1.0, float(out[62:70, 96:160, 2].mean()))
        bot_rb = float(out[186:194, 96:160, 0].mean()) / max(1.0, float(out[186:194, 96:160, 2].mean()))
        self.assertGreater(top_rb, bot_rb)

    def test_road_asphalt_aggregate_and_paint_protection(self) -> None:
        road = np.full((64, 64, 4), (62, 64, 66, 84), dtype=np.uint8)
        # Subtle dark crack + bright yellow painted road stripe
        road[:, 14:16, :3] = (34, 35, 36)
        road[:, 42:52, :3] = (196, 166, 56)
        out, mat, amode = tr3d.sculpt_texture_relief_rgba(road, name="RO_ASPHALT_STRIPE", scale=4, small_scale=4)
        self.assertEqual((mat, amode), ("road", "mask_0_128"))
        self.assertTrue(int(out[:, :, 3].max()) <= 128 and int(out[:, :, 3].min()) >= 80)
        # Asphalt gets tactile micro-relief variance while painted stripe interior stays smooth
        luma = 0.299 * out[:, :, 0] + 0.587 * out[:, :, 1] + 0.114 * out[:, :, 2]
        asph_std = float(luma[32:224, 80:144].std())
        stripe_std = float(luma[32:224, 180:196].std())
        self.assertGreater(asph_std, 2.2)
        self.assertLess(stripe_std, asph_std * 0.85)
        # Painted stripe catches subtle raised bevel on sun-facing left edge vs right edge
        self.assertGreater(float(luma[32:224, 168:173].mean()), float(luma[32:224, 203:208].mean()))

    def test_vehicle_surfaces_honeycomb_ao_and_determinism(self) -> None:
        grille = np.full((64, 64, 4), (112, 114, 118, 128), dtype=np.uint8)
        for y in range(4, 60, 8):
            for x in range(4, 60, 8):
                grille[y : y + 4, x : x + 4, :3] = (24, 24, 26)
        out1, mat1, _ = tr3d.sculpt_texture_relief_rgba(grille, name="GRILL_01", archive=r"GLOBAL\GLOBALB.LZC", scale=4, small_scale=4)
        out2, _, _ = tr3d.sculpt_texture_relief_rgba(grille, name="GRILL_01", archive=r"GLOBAL\GLOBALB.LZC", scale=4, small_scale=4)
        self.assertEqual(mat1, "vehicle")
        np.testing.assert_array_equal(out1, out2)
        self.assertTrue((out1[:, :, 3] == 128).all())
        # Honeycomb recesses stay deeply occluded relative to metallic mesh bars
        self.assertLess(float(out1[18:28, 18:28, :3].mean()), float(out1[4:12, 4:12, :3].mean()) * 0.45)

    def test_foliage_ps2_alpha_strict_and_fringe_free(self) -> None:
        leaf = np.zeros((32, 32, 4), dtype=np.uint8)
        leaf[:, :16] = (34, 96, 36, 128)
        leaf[8:24, 4:12, :3] = (64, 132, 52)
        leaf[:, 16:] = (255, 255, 255, 0)
        out, mat, amode = tr3d.sculpt_texture_relief_rgba(leaf, name="ORG_MAPLE_01", scale=4, small_scale=4)
        self.assertEqual((mat, amode), ("foliage", "cutout_0_128"))
        self.assertLessEqual(int(out[:, :, 3].max()), 128)
        self.assertEqual(int(out[:, :, 3].min()), 0)
        # Adjacent transparent pixels right outside the alpha cutout edge must be green-dilated, never white fringe
        self.assertLess(int(out[64, 66, 0]), 65)
        self.assertGreater(int(out[64, 66, 1]), 72)

    def test_signage_legibility_and_shadow_map_passthrough(self) -> None:
        sign = np.full((32, 64, 4), (24, 92, 64, 128), dtype=np.uint8)
        sign[10:22, 12:52, :3] = (228, 232, 224)
        s_out, s_mat, _ = tr3d.sculpt_texture_relief_rgba(sign, name="SGN_FWY_EXIT", scale=4, small_scale=4)
        f_out, _, _ = bpack.enhance_faithful_rgba(sign, name="SGN_FWY_EXIT", scale=4, small_scale=4)
        self.assertEqual(s_mat, "signage")
        # Signage must stay super close to clean Lanczos with zero directional bevel skew
        self.assertLess(float(np.abs(s_out[:, :, :3].astype(np.float32) - f_out[:, :, :3].astype(np.float32)).mean()), 2.5)
        top_edge = float(s_out[39:42, 64:192, :3].mean())
        bot_edge = float(s_out[86:89, 64:192, :3].mean())
        self.assertLess(abs(top_edge - bot_edge), 4.0)

        # Flat constant-RGB shadow map (SHD_*) must remain spotless with zero synthetic noise
        shd = np.full((32, 32, 4), (26, 26, 26, 0), dtype=np.uint8)
        shd[8:24, 8:24, 3] = 78
        shd_out, shd_mat, shd_am = tr3d.sculpt_texture_relief_rgba(shd, name="SHD_CP_BE_ROAD_A_03", scale=4, small_scale=4)
        self.assertEqual((shd_mat, shd_am), ("shadow_map", "mask_0_128"))
        self.assertTrue((shd_out[:, :, :3] == 26).all())
        self.assertLessEqual(int(shd_out[:, :, 3].max()), 78)

    def test_mean_luminance_preservation_and_pack_builder_cli_dispatch(self) -> None:
        wall = _make_brick_wall(64, 64)
        s_out, _, _ = bpack.enhance_texture_rgba(wall, name="ARC_BRICK01", scale=4, small_scale=4, relief_mode="sculpted")
        f_out, _, _ = bpack.enhance_texture_rgba(wall, name="ARC_BRICK01", scale=4, small_scale=4, relief_mode="faithful")
        f_ref, _, _ = bpack.enhance_faithful_rgba(wall, name="ARC_BRICK01", scale=4, small_scale=4)
        s_ref, _, _ = tr3d.sculpt_texture_relief_rgba(wall, name="ARC_BRICK01", scale=4, small_scale=4)
        np.testing.assert_array_equal(f_out, f_ref)
        np.testing.assert_array_equal(s_out, s_ref)
        ratio = float(s_out[:, :, :3].mean()) / max(1.0, float(f_out[:, :, :3].mean()))
        self.assertTrue(0.90 <= ratio <= 1.08, f"Mean luminance drifted unexpectedly: {ratio:.3f}")
        with self.assertRaises(ValueError):
            bpack.enhance_texture_rgba(wall, name="ARC_BRICK01", relief_mode="invalid_mode")
        # CLI parses --relief-mode sculpted and --relief-mode faithful cleanly even when ISO is absent
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bpack.main(["--iso", "/nonexistent/nfsmw.iso", "--relief-mode", "sculpted"]), 1)
            self.assertEqual(bpack.main(["--iso", "/nonexistent/nfsmw.iso", "--relief-mode", "faithful"]), 1)


class TestLiveReliefOnRetailISO(unittest.TestCase):
    """Live retail ISO smoke check (skipped cleanly in offline CI when ISO is absent)."""

    def test_real_rockport_brick_and_road_relief_invariants(self) -> None:
        iso = _find_live_iso()
        if iso is None:
            self.skipTest("Live PS2 ISO unavailable (offline CI mode).")
        arc = PS2NFSMWArchive(iso)
        targets = {}
        for sname, _, sbytes in pth.iter_streaml2ra_sections(
            arc.read_entry(arc.find(r"TRACKS\L2RA.BUN")),
            arc.read_entry(arc.find(r"TRACKS\STREAML2RA.BUN")),
        ):
            for tex in pth.iter_tpk_raw_textures(sbytes, r"TRACKS\STREAML2RA.BUN", section=sname):
                nm = tex.name.upper()
                if nm in ("ARC_OLDBRICKC_WINA_LL", "TRNS_ALL_LZ_PAVEMENTA_Y") and nm not in targets:
                    targets[nm] = pth.decode_raw_tpk_rgba(tex, scale_alpha=False)
            if len(targets) == 2:
                break
        self.assertEqual(len(targets), 2)
        for nm, rgba in targets.items():
            s_out, mat, _ = tr3d.sculpt_texture_relief_rgba(rgba, name=nm, archive=r"TRACKS\STREAML2RA.BUN")
            f_out, _, _ = bpack.enhance_faithful_rgba(rgba, name=nm, archive=r"TRACKS\STREAML2RA.BUN")
            self.assertIn(mat, ("architecture", "road"))
            self.assertLessEqual(int(s_out[:, :, 3].max()), 128)
            self.assertGreater(float(s_out[:, :, :3].astype(np.float32).std()), float(f_out[:, :, :3].astype(np.float32).std()))


if __name__ == "__main__":
    unittest.main()
