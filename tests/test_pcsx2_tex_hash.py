"""Cross-platform offline + live verification tests for v2.2 PCSX2 texture hasher & BC7 pack builder."""

from __future__ import annotations

import os, struct, subprocess, sys, unittest, warnings
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
import build_pcsx2_texture_pack as bpack, pcsx2_tex_hash as pth, tpk_extract as te  # noqa: E402


def _find_live_iso() -> Optional[Path]:
    env = os.environ.get("NFSMW_ISO")
    p = Path(env).expanduser() if env is not None else (Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso")
    return p if p.is_file() else None


def _find_rosewood_dumps() -> Optional[Path]:
    rel = Path("build") / "flywheel" / "sandbox_textures" / "SLUS-21351" / "dumps"
    if (REPO_ROOT / rel).is_dir():
        return REPO_ROOT / rel
    env = os.environ.get("NFSMW_ROSEWOOD_DUMPS")
    if env and Path(env).expanduser().is_dir():
        return Path(env).expanduser()
    try:
        common = subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "--git-common-dir"], text=True, stderr=subprocess.DEVNULL).strip()
        cand = (REPO_ROOT / common).resolve().parent / rel
        if cand.is_dir():
            return cand
    except Exception:
        pass
    return None


class TestXXH3AndGSVramOffline(unittest.TestCase):
    """Offline tests (Ubuntu Linux + macOS CI safe with NFSMW_ISO=/nonexistent)."""

    def test_xxh3_64bits_official_sanity_vectors_and_no_overflow_warnings(self) -> None:
        g, buf = 2654435761, bytearray(2240)
        for i in range(len(buf)):
            buf[i] = (g >> 56) & 0xFF
            g = (g * 11400714785074694797) & 0xFFFFFFFFFFFFFFFF
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            for length, expected in [
                (0, 0x2D06800538D394C2), (1, 0xC44BDFF4074EECDB), (6, 0x27B56A84CD2D7325),
                (12, 0xA713DAF0DFBB77E7), (24, 0xA3FE70BF9D3510EB), (48, 0x397DA259ECBA1F11),
                (80, 0xBCDEFBBB2C47C90A), (195, 0xCD94217EE362EC3A), (403, 0xCDEB804D65C6DEA4),
                (512, 0x617E49599013CB6B), (2048, 0xDD59E2C3A5F038E0), (2240, 0x6E73A90539CF2948),
            ]:
                self.assertEqual(pth.xxh3_64bits(bytes(buf[:length])), expected, f"len={length}")
        self.assertEqual(pth.xxh3_64bits(b"abc"), 0x78AF5F94892F3950)

    def test_gs_vram_swizzle_bijections_and_block_tables(self) -> None:
        for tbl in (pth.BLOCK_TABLE_32, pth.BLOCK_TABLE_8, pth.BLOCK_TABLE_4):
            self.assertEqual(sorted(b for row in tbl for b in row), list(range(32)))
        for psm, bw, bh in ((pth.PSM_T4, 8, 16), (pth.PSM_T8, 16, 8), (pth.PSM_CT32, 16, 8)):
            self.assertEqual(len({pth.gs_block_address(bx, by, psm, tbw_pages=2) for by in range(bh) for bx in range(bw)}), bw * bh)
        self.assertEqual(sorted(int(v) for v in pth._LUT_B32.ravel()), list(range(64)))
        self.assertEqual(sorted(int(v) for v in pth._LUT_B8.ravel()), list(range(256)))
        self.assertEqual(sorted(int(v) for v in (pth._LUT_B4 * 2 + (pth._LUT_N4 >> 2)).ravel()), list(range(512)))
        self.assertEqual(len(pth.gs_swizzle_level_blocks(np.zeros((16, 32), dtype=np.uint8), pth.PSM_T4)), 256)
        self.assertEqual(len(pth.gs_swizzle_level_blocks(np.zeros((8, 16), dtype=np.uint8), pth.PSM_T4)), 128)
        self.assertEqual(len(pth.gs_swizzle_level_blocks(np.zeros((16, 16), dtype=np.uint8), pth.PSM_T8)), 256)
        self.assertEqual(len(pth.gs_swizzle_level_blocks(np.zeros((8, 16), dtype=np.uint8), pth.PSM_T8)), 128)
        self.assertEqual(len(pth.gs_swizzle_level_blocks(np.zeros((8, 8, 4), dtype=np.uint8), pth.PSM_CT32)), 256)
        self.assertEqual(len(pth.gs_swizzle_level_blocks(np.zeros((4, 4, 4), dtype=np.uint8), pth.PSM_CT32)), 64)
        self.assertEqual(sorted(pth.gs_swizzle_level_blocks(np.arange(256, dtype=np.uint8).reshape(16, 16), pth.PSM_T8)), list(range(256)))

    def test_pcsx2_flags_and_stem_formatting_no_leading_zeros(self) -> None:
        fl = pth.pcsx2_flags(pth.PSM_T4, 128, 128)
        self.assertEqual((fl, pth.pcsx2_flags(pth.PSM_T8, 256, 64), pth.pcsx2_flags(pth.PSM_CT32, 64, 32)), (0x1DD4, 0x1A13, 0x1580))
        self.assertEqual(pth.format_pcsx2_stem(0x09683FAE9720A5C8, 0x00C477D6508224AF, fl), "9683fae9720a5c8-c477d6508224af-00001dd4")
        self.assertEqual(pth.format_pcsx2_stem(0x00ABCDEF, None, 0x1580), "abcdef-00001580")
        pal_raw = bytes([10, 20, 30, 0x80] * 16)
        self.assertEqual(pth.hash_clut_ps2(pal_raw, entries=16), pth.xxh3_64bits(pal_raw))
        self.assertNotEqual(pth.hash_clut_ps2(pal_raw, entries=16), pth.xxh3_64bits(te.unswizzle_clut_csm1(pal_raw, entries=16, scale_alpha=True).tobytes()))

    def test_ps2_alpha_clamping_and_cutout_edge_dilation_fringe(self) -> None:
        opaque = np.full((16, 16, 4), (100, 120, 140, 128), dtype=np.uint8)
        enh_op, mat, amode = bpack.enhance_faithful_rgba(opaque, name="ARC_BRICK_01")
        self.assertEqual((mat, amode), ("world_surface", "opaque_128"))
        self.assertTrue((enh_op[:, :, 3] == 128).all())

        spec = np.zeros((16, 16, 4), dtype=np.uint8)
        spec[:, :, :3], spec[:, :8, 3], spec[:, 8:, 3] = 90, 40, 112
        enh_sp, _, amode_sp = bpack.enhance_faithful_rgba(spec, name="RO_ASPHALT_01")
        self.assertEqual(amode_sp, "mask_0_128")
        self.assertTrue(35 <= int(enh_sp[:, :, 3].min()) <= int(enh_sp[:, :, 3].max()) <= 128)

        leaf = np.zeros((16, 16, 4), dtype=np.uint8)
        leaf[:, :8], leaf[:, 8:] = (24, 88, 28, 128), (255, 255, 255, 0)
        dil = bpack.dilate_alpha_edge_rgb(leaf[:, :, :3], leaf[:, :, 3], threshold=16, iters=6)
        self.assertTrue(int(dil[8, 9, 0]) <= 30 and int(dil[8, 9, 1]) >= 82 and int(dil[8, 9, 2]) <= 34)
        enh_cut, _, amode_cut = bpack.enhance_faithful_rgba(leaf, name="ORG_CYPRESS_01", scale=4, small_scale=4)
        self.assertEqual(amode_cut, "cutout_0_128")
        self.assertLessEqual(int(enh_cut[:, :, 3].max()), 128)
        self.assertTrue(int(enh_cut[32, 33, 0]) < 48 and int(enh_cut[32, 33, 1]) > 70)

    def test_hard_denylist_global_allowlist_stage2_guard_and_dds_header(self) -> None:
        for name, arc in [
            ("HUD_TACH", r"GLOBAL\GLOBALB.LZC"), ("FE_BUTTON", r"GLOBAL\GLOBALA.BUN"),
            ("FONT_MAIN", r"GLOBAL\GLOBALB.LZC"), ("MINIMAP_ARROW", r"TRACKS\STREAML2RA.BUN"),
            ("LOADING_SPINNER", r"GLOBAL\INGAMEA.BUN"), ("LOAD_LED_GLOW", r"GLOBAL\GLOBALB.LZC"),
            ("INGAME_ICON_PAUSE_MAIN", r"GLOBAL\INGAMEA.BUN"), ("FX_GLOW01_ADDITIVE", r"GLOBAL\INGAMEB.LZC"),
            ("TIP_BLACKLIST", r"GLOBAL\DYNTEX.BIN"), ("CAREER_XPOS", r"GLOBAL\DYNTEX.BIN"),
            ("DPAD_UP", r"GLOBAL\GLOBALB.LZC"), ("FLARE_SUN", r"GLOBAL\INGAMEB.LZC"),
            ("SHADOW_CAR", r"GLOBAL\GLOBALB.LZC"), ("ENV_REFLECT", r"GLOBAL\GLOBALB.LZC"),
            ("ANY_TEX", r"GLOBAL\HUDTEXRACE.BIN"), ("ANY_TEX", r"GLOBAL\HUDTEXDRAG.BIN"),
            ("ANY_TEX", r"FRONTEND\FRONTB.LZC"), ("MW_LOGO", r"GLOBAL\GLOBALA.BUN"),
        ]:
            self.assertTrue(bpack.is_denylisted_texture(name, arc)[0], f"Expected denylisted: {name} ({arc})")
        for name, arc in [
            ("RO_ASPHALT_01", r"TRACKS\STREAML2RA.BUN"), ("ARC_FRATFENCE2", r"TRACKS\STREAML2RA.BUN"),
            ("ORG_TREE_A", r"TRACKS\STREAML2RA.BUN"), ("SGN_FWY_ROSEWOOD_EA", r"TRACKS\STREAML2RA.BUN"),
            ("ADS_AUTOZONE_BB_A_EA", r"TRACKS\STREAML2RA.BUN"), ("SHD_CT_ROAD_COLONIAL_01", r"TRACKS\STREAML2RA.BUN"),
            ("SKY_MIDDAY_A_ENVIRONMEN", r"GLOBAL\DYNTEX.BIN"), ("CARBONFIBRE", r"GLOBAL\GLOBALB.LZC"),
            ("SKID_ASPHALT", r"GLOBAL\INGAMEA.BUN"), ("BMWM3GTRE46_MISC", r"CARS\BMWM3GTRE46\TEXTURES.BIN"),
        ]:
            self.assertFalse(bpack.is_denylisted_texture(name, arc)[0], f"Unexpectedly blocked: {name} ({arc})")
        # Stage-2 blocked_stems guard blocks protected FRONTEND/TRACKMAP/ENVMAP stems
        self.assertEqual(bpack.filter_stage2_stem("abc-def-00001dd4", set(), {"abc-def-00001dd4"}), (False, "stage2_blocked_stem"))
        self.assertEqual(bpack.filter_stage2_stem("abc-def-00001dd4", {"abc-def-00001dd4"}, set()), (False, "stage2_already_covered"))
        self.assertEqual(bpack.filter_stage2_stem("abc-def-00001dd4", set(), set()), (True, ""))
        self.assertTrue(bpack.section_matches_district("H41", ["ARC_CAMPUS"], "rosewood") and not bpack.section_matches_district("C41", ["ARC_CITYCENTER"], "rosewood"))
        self.assertTrue(bpack.section_matches_district("C41", ["ARC_CITYCENTER"], "downtown") and bpack.section_matches_district("Q41", ["ARC_DOCK_CK"], "camden") and bpack.section_matches_district("I41", ["SGN_FWY_HWY99_SOUTH_EA"], "highway99"))
        hdr = bpack.build_bc7_dds_header(512, 256, 10)
        self.assertEqual((len(hdr), hdr[:4], hdr[84:88], struct.unpack_from("<I", hdr, 128)[0]), (148, b"DDS ", b"DX10", 98))


class TestLiveRosewoodHashMatch(unittest.TestCase):
    """Live ISO + Rosewood dump verification (cleanly skipped when ISO or dumps are absent)."""

    def test_exact_offline_hash_match_on_real_rosewood_dumps(self) -> None:
        iso, dumps = _find_live_iso(), _find_rosewood_dumps()
        if iso is None or dumps is None:
            self.skipTest("Live PS2 ISO or Rosewood dumps unavailable (offline CI mode).")
        report = pth.verify_against_dumps(iso, dumps, include_all_cars=True)
        self.assertGreaterEqual(int(report["clut_covered"]), 500)
        self.assertEqual(int(report["clut_mismatched"]), 0, f"Hash mismatches: {report['mismatched_samples']}")
        self.assertEqual(int(report["clut_matched"]), int(report["clut_covered"]))


if __name__ == "__main__":
    unittest.main()
