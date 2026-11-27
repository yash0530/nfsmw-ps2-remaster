#!/usr/bin/env python3
"""v2.9 tests: window glass / car contact shadow / foliage coverage mips + PINE-matched fair benchmark.

Offline tests pass on Linux + macOS CI with NFSMW_ISO absent or nonexistent; the live test is skipped then.
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
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import benchmark_pcsx2_live as bench  # noqa: E402
import pcsx2_profile_2026 as prof  # noqa: E402

try:  # imaging stack is optional (e.g. `python3 -s` without user site-packages): those tests skip cleanly
    import numpy as np
    from PIL import Image
    import build_pcsx2_texture_pack as bpack
    import install_macos_texture_tools as imtt
    import pcsx2_tex_hash as pth
    import texture_relief_3d as tr3d
    from zdir_tool import PS2NFSMWArchive
    HAS_IMAGING = True
except ImportError:
    HAS_IMAGING = False
needs_imaging = unittest.skipUnless(HAS_IMAGING, "numpy + Pillow required")

GLOBALB = r"GLOBAL\GLOBALB.LZC"
CARSHADOW_STEM = "84af8772e33c387e-bf065d3fd0fefd12-00001594"  # retail GLOBALB.LZC CARSHADOW (64x32 PSM_T4)


def _find_live_iso() -> Optional[Path]:
    env = os.environ.get("NFSMW_ISO")
    p = Path(env).expanduser() if env is not None else (Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso")
    return p if p.is_file() else None


def _luma(rgb: np.ndarray) -> np.ndarray:
    f = rgb.astype(np.float32)
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


def _car_shadow_src() -> np.ndarray:
    yy, xx = np.mgrid[0:32, 0:64]
    r = np.sqrt(((xx - 31.5) / 28.0) ** 2 + ((yy - 15.5) / 13.0) ** 2)
    rgba = np.zeros((32, 64, 4), dtype=np.uint8)
    rgba[..., 3] = np.round(np.clip((1.0 - r) / 0.45, 0.0, 1.0) * 127.0).astype(np.uint8)
    return rgba


def _foliage_src() -> np.ndarray:
    rng = np.random.default_rng(3)
    rgba = np.zeros((128, 128, 4), dtype=np.uint8)
    rgba[..., :3] = 255  # white transparent background must never fringe into leaves
    yy, xx = np.mgrid[0:128, 0:128]
    for _ in range(150):  # sparse small leaf discs (r 1.2..2.6 px) with 1-px partial-alpha rims
        cy, cx, r = rng.uniform(4, 124), rng.uniform(4, 124), rng.uniform(1.2, 2.6)
        d = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        a = np.clip((r + 0.5 - d) * 128.0, 0, 128).astype(np.uint8)
        leaf = a > rgba[..., 3]
        rgba[leaf] = np.dstack([np.full_like(a, 40), np.full_like(a, 110), np.full_like(a, 35), a])[leaf]
    return rgba


@needs_imaging
class TestWindowGlassAndGrit(unittest.TestCase):
    def _facade(self) -> np.ndarray:
        rgb = np.full((160, 160, 3), 150, dtype=np.uint8)
        rgb[40:120, 40:100] = 40
        return rgb

    def test_window_glass_gradient_and_inner_recess_frame(self) -> None:
        src = self._facade()
        pane = tr3d.detect_window_panes(src.astype(np.float32) / 255.0)
        self.assertTrue(pane[45:115, 45:95].all() and not pane[:38].any() and not pane[:, 102:].any())
        out, frac = tr3d.polish_window_glass(src)
        self.assertAlmostEqual(frac, 80 * 60 / 160.0 ** 2, delta=0.01)
        np.testing.assert_array_equal(out[:30], src[:30])  # facade wall untouched
        lum = _luma(out)
        # Recessed frame: top inner edge (sun drop-shadow) darker than glass just below it, left darker than right
        self.assertLess(float(lum[42, 60:80].mean()), float(lum[56, 60:80].mean()) - 3.0)
        self.assertLess(float(lum[70:90, 42].mean()), float(lum[70:90, 97].mean()) - 3.0)
        # Reflection: upper glass reads sky-blue, lower glass warm street haze, glass brighter than flat paint
        o = out.astype(np.int32)
        self.assertGreater(int(o[60, 50:90, 2].mean() - o[60, 50:90, 0].mean()), 4)
        self.assertGreater(int(o[110, 50:90, 0].mean() - o[110, 50:90, 2].mean()), 4)
        self.assertGreater(float(lum[60, 50:90].mean()), float(lum[110, 50:90].mean()))
        self.assertLess(float(lum[60, 50:90].mean()), 150.0)  # still reads as dark glass, never a bright sticker

    def test_window_polish_via_relief_pipeline_ignores_mortar(self) -> None:
        rng = np.random.default_rng(11)
        wall = np.full((64, 64, 4), (140, 132, 124, 128), dtype=np.uint8)
        wall[..., :3] = np.clip(wall[..., :3].astype(np.int16) + rng.integers(-6, 7, (64, 64, 1)), 0, 255).astype(np.uint8)
        wall[::8, :, :3] = (76, 72, 68)  # thin dark mortar courses
        wall[12:32, 10:24, :3] = (34, 38, 44)
        wall[12:32, 40:54, :3] = (34, 38, 44)
        out, mat, amode = tr3d.polish_texture_relief_rgba(wall, name="ARC_TEST_BLDWINA", scale=4, small_scale=4)
        self.assertEqual((mat, amode, out.shape), ("window_glass", "opaque_128", (256, 256, 4)))
        base, bmat, _ = tr3d.sculpt_texture_relief_rgba(wall, name="ARC_TEST_BLDWINA", scale=4, small_scale=4)
        pane = tr3d.detect_window_panes(base[..., :3].astype(np.float32) / 255.0)
        self.assertEqual(bmat, "architecture")
        self.assertTrue(pane[60:120, 50:86].all())
        self.assertFalse(pane[160:, :].any())  # mortar courses below the windows are never "glass"
        self.assertTrue((out[..., 3] == 128).all())

    def test_cornice_grit_coring_keeps_mortar_crisp(self) -> None:
        rng = np.random.default_rng(7)
        base = np.full((64, 64), 128.0) + rng.normal(0.0, 2.5, (64, 64))
        base[::8, :] = 70.0
        rgb = np.repeat(np.clip(np.round(base), 0, 255).astype(np.uint8)[..., None], 3, axis=2)
        out = tr3d.reduce_cornice_grit(rgb)
        rows = np.array([r for r in range(64) if r % 8 in (3, 4, 5)])
        self.assertLess(float(_luma(out)[rows].std()), 0.85 * float(_luma(rgb)[rows].std()))
        c_in = float(_luma(rgb)[rows].mean() - _luma(rgb)[::8].mean())
        c_out = float(_luma(out)[rows].mean() - _luma(out)[::8].mean())
        self.assertAlmostEqual(c_out, c_in, delta=0.05 * c_in)


@needs_imaging
class TestCarContactShadow(unittest.TestCase):
    def test_deeper_core_tighter_penumbra_inside_footprint(self) -> None:
        src = _car_shadow_src()
        base, _, _ = tr3d.sculpt_texture_relief_rgba(src, name="CARSHADOW", archive=GLOBALB, scale=4, small_scale=4)
        out, mat, amode = bpack.enhance_texture_rgba(src, name="CARSHADOW", archive=GLOBALB, scale=4, small_scale=4, relief_mode="polished")
        self.assertEqual((mat, amode, out.shape), ("car_contact_shadow", "cutout_0_128", (128, 256, 4)))
        a0, a1 = base[..., 3].astype(int), out[..., 3].astype(int)
        self.assertLessEqual(int(a1.max()), 128)
        self.assertEqual(int(a1[64, 128]), 128)
        self.assertTrue((out[..., :3] == 0).all())
        self.assertTrue((a1[a0 <= 2] == 0).all())  # never grows beyond the retail footprint
        self.assertGreater(int((a1 >= 124).sum()), int(1.25 * (a0 >= 124).sum()))
        self.assertLess(int(((a1 > 12) & (a1 < 116)).sum()), int(0.70 * ((a0 > 12) & (a0 < 116)).sum()))
        self.assertGreater(float(a1.mean()), float(a0.mean()))
        row = a1[64, 128:]
        self.assertTrue((np.diff(row) <= 1).all())  # monotone soft falloff, no rings
        self.assertGreater(int(((row > 8) & (row < 120)).sum()), 3)  # penumbra is tighter, but still soft

    def test_car_shadow_scope_and_denylist_guards(self) -> None:
        self.assertTrue(tr3d.is_car_contact_shadow("carshadow", "GLOBAL/GLOBALB.LZC"))
        for name, arc in (("SHADOWRAMP", GLOBALB), ("CARSHADOW", r"TRACKS\STREAML2RA.BUN"), ("SHD_CT_ROAD_A1", r"TRACKS\STREAML2RA.BUN")):
            self.assertFalse(tr3d.is_car_contact_shadow(name, arc), name)
        self.assertTrue(bpack.is_denylisted_texture("CARSHADOW", GLOBALB)[0])  # v2.5 default unchanged
        self.assertEqual(bpack.is_denylisted_texture("CARSHADOW", GLOBALB, allow_car_shadow=True), (False, ""))
        for name, arc in (("SHADOWRAMP", GLOBALB), ("SHADOW_CAR", GLOBALB), ("CARSHADOW", r"TRACKS\STREAML2RA.BUN"),
                          ("HUD_TACH", GLOBALB), ("LOADING_SPINNER", r"GLOBAL\INGAMEA.BUN"), ("ANY", r"FRONTEND\FRONTB.LZC"),
                          ("CARSHADOW", r"GLOBAL\HUDTEXRACE.BIN"), ("FONT_MAIN", GLOBALB)):
            self.assertTrue(bpack.is_denylisted_texture(name, arc, allow_car_shadow=True)[0], (name, arc))
        # SHD_* world shadow maps are never darkened: polished output == untouched v2.5 passthrough
        shd = np.full((32, 32, 4), (26, 26, 26, 0), dtype=np.uint8)
        shd[8:24, 8:24, 3] = 78
        p, pm, _ = tr3d.polish_texture_relief_rgba(shd, name="SHD_CP_BE_ROAD_A_03", archive=r"TRACKS\STREAML2RA.BUN", scale=4, small_scale=4)
        s, _, _ = tr3d.sculpt_texture_relief_rgba(shd, name="SHD_CP_BE_ROAD_A_03", archive=r"TRACKS\STREAML2RA.BUN", scale=4, small_scale=4)
        self.assertEqual(pm, "shadow_map")
        np.testing.assert_array_equal(p, s)


@needs_imaging
class TestFoliageCoverageMips(unittest.TestCase):
    def test_coverage_preserved_alpha_range_and_dilation(self) -> None:
        src = _foliage_src()
        chain = tr3d.build_coverage_preserving_mips(src)
        self.assertEqual([lv.shape[:2] for lv in chain], [(128 >> i, 128 >> i) for i in range(8)])
        np.testing.assert_array_equal(chain[0], src)
        target = float((src[..., 3] >= 64).mean())
        naive = src[..., 3].astype(np.float32)
        for i, lv in enumerate(chain[1:5], start=1):
            naive = naive.reshape(naive.shape[0] // 2, 2, naive.shape[1] // 2, 2).mean(axis=(1, 3))
            cov = float((lv[..., 3] >= 64).mean())
            self.assertLessEqual(int(lv[..., 3].max()), 128)
            self.assertAlmostEqual(cov, target, delta=0.03, msg=f"level {i}")
            if i >= 2:
                self.assertLess(float((naive >= 64).mean()), cov - 0.03)  # naive box mips thin out
            edge = (lv[..., 3] < 16) & (np.pad(lv[..., 3] >= 16, 1)[1:-1, 2:] | np.pad(lv[..., 3] >= 16, 1)[1:-1, :-2])
            if edge.any():
                self.assertLess(int(lv[..., 0][edge].max()), 120)  # green-dilated, never white fringe

    def test_pre_built_chain_encodes_into_one_mipmapped_dds(self) -> None:
        enc = imtt.find_rockport_dds()
        if enc is None:
            self.skipTest("rockport_dds encoder not built")
        chain = tr3d.build_coverage_preserving_mips(_foliage_src())
        with tempfile.TemporaryDirectory() as tmp:
            fw, _ = bpack.encode_batch_to_dds([([("aa-bb-00001", 0), ("aa-bb-00002", 1)], chain), ([("cc-00003", 0)], _foliage_src())], Path(tmp), enc)
            self.assertEqual(fw, 3)
            for stem, (w, mips) in {"aa-bb-00001": (128, 8), "aa-bb-00002": (64, 7), "cc-00003": (128, 8)}.items():
                b = (Path(tmp) / f"{stem}.dds").read_bytes()
                self.assertEqual((struct.unpack_from("<I", b, 16)[0], struct.unpack_from("<I", b, 28)[0]), (w, mips))
                exp = sum(max(1, ((w >> i) + 3) // 4) ** 2 * 16 for i in range(mips))
                self.assertEqual(len(b), 148 + exp)


def _drive(speed: float, n: int, *, z: float = 0.0, t0: float = 1000.0, jump_at: Optional[int] = None) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = [{"timestamp": t0 - 1.0, "flow_state": 3, "paused": False, "player_present": False,
                                   "world_loop_counter": 0, "pos": [999.0, 0.0, 0.0]}]
    for i in range(n):
        x = speed * i * 0.1 + (500.0 if jump_at is not None and i >= jump_at else 0.0)
        recs.append({"timestamp": t0 + i * 0.1, "flow_state": 6, "paused": False, "player_present": True,
                     "world_loop_counter": 100 + i, "pos": [x, 0.0, z]})
    return recs


class TestPineMatchedCheckpoints(unittest.TestCase):
    def test_track_distance_excludes_frontend_and_teleports(self) -> None:
        track = bench.load_pine_track(_drive(10.0, 101))
        self.assertEqual(len(track), 101)
        self.assertAlmostEqual(track[-1]["dist"], 100.0, delta=0.01)
        jumped = bench.load_pine_track(_drive(10.0, 101, jump_at=50))
        self.assertAlmostEqual(jumped[-1]["dist"], 99.0, delta=0.01)
        self.assertEqual(bench.load_pine_track([]), [])

    def test_position_match_pairs_same_block_despite_different_pace(self) -> None:
        lt, rt = bench.load_pine_track(_drive(10.0, 300)), bench.load_pine_track(_drive(8.0, 380))
        lf = bench.annotate_frames([Path(f"l{i}.png") for i in range(7)], lt, frame_times=[1003.0 + 5 * i for i in range(6)] + [1500.0])
        rf = bench.annotate_frames([Path(f"r{i}.png") for i in range(37)], rt, frame_times=[1001.0 + i for i in range(37)])
        self.assertEqual(len(lf), 6)  # frame far outside telemetry is dropped
        pairs = bench.match_checkpoints_by_position(lf, rf, n=3)
        self.assertEqual([p["method"] for p in pairs], ["position"] * 3)
        self.assertTrue(all(p["position_gap_m"] <= 4.1 for p in pairs))
        self.assertEqual([p["left"].name for p in pairs], ["l0.png", "l2.png", "l5.png"])
        self.assertEqual(len({p["right"] for p in pairs}), 3)
        # Diverged route (other street 500 m away) falls back to matched driven distance
        df = bench.annotate_frames([Path(f"d{i}.png") for i in range(37)], bench.load_pine_track(_drive(8.0, 380, z=500.0)),
                                   frame_times=[1001.0 + i for i in range(37)])
        dpairs = bench.match_checkpoints_by_position(lf, df, n=3)
        self.assertEqual([p["method"] for p in dpairs], ["distance"] * 3)
        self.assertTrue(all(p["distance_gap_m"] <= 4.1 for p in dpairs))
        self.assertEqual(bench.match_checkpoints_by_position([], rf), [])

    @needs_imaging
    def test_matched_checkpoint_pairs_on_run_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = []
            for side, speed, n, times in (("l", 10.0, 300, [1003.0 + 5 * i for i in range(6)]), ("r", 8.0, 380, [1001.0 + i for i in range(37)])):
                run = Path(tmp) / side / "run"
                (run / "frames").mkdir(parents=True)
                (run / "pine_telemetry.jsonl").write_text("\n".join(json.dumps(r) for r in _drive(speed, n)) + "\nnot-json\n", encoding="utf-8")
                for i, t in enumerate(times):
                    p = run / "frames" / f"snap_{i:03d}.png"
                    Image.new("RGB", (32, 18), (60 + i, 90, 120)).save(p)
                    os.utime(p, (t, t))
                runs.append(run)
            pairs = bench.matched_checkpoint_pairs(runs[0], runs[1])
            self.assertEqual([p[0] for p in pairs], bench.CHECKPOINT_LABELS)
            self.assertEqual(pairs[0][3]["method"], "label")
            self.assertEqual([p[3]["method"] for p in pairs[2:]], ["position"] * 3)
            self.assertEqual(pairs[4][1].name, "snap_005.png")


class TestFairControlAndBackCompat(unittest.TestCase):
    @staticmethod
    def _sections(ini: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        sec = ""
        for line in ini.splitlines():
            line = line.strip()
            if line.startswith("["):
                sec = line.strip("[]")
                out.setdefault(sec, {})
            elif "=" in line and not line.startswith(";"):
                k, v = (s.strip() for s in line.split("=", 1))
                out[sec].setdefault(k, []).append(v) if k == "Enable" else out[sec].__setitem__(k, v)
        return out

    def test_fair_control_matches_mod_settings_without_mod_content(self) -> None:
        for scale in (6, 8):
            mod, ctrl = prof.build_v21_metal_profile(scale=scale), bench.build_fair_control_profile(scale=scale)
            self.assertEqual(prof.validate_profile_ini(ctrl), [])
            m, c = self._sections(mod), self._sections(ctrl)
            diff = {k for k in m["EmuCore/GS"] if m["EmuCore/GS"][k] != c["EmuCore/GS"].get(k)}
            self.assertEqual(diff, {"LoadTextureReplacements", "LoadTextureReplacementsAsync", "PrecacheTextureReplacements"})
            self.assertEqual(c["EmuCore/GS"]["upscale_multiplier"], str(scale))
            self.assertEqual((m["EmuCore"], m["EmuCore/Speedhacks"]), (c["EmuCore"], c["EmuCore/Speedhacks"]))
            self.assertEqual(c["Patches"]["Enable"], list(bench.FAIR_CONTROL_PATCHES))
        self.assertTrue(set(bench.FAIR_CONTROL_PATCHES) <= set(prof.V21_DEFAULT_PATCHES))

    def test_cli_flags_validate_without_launching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bench.main(["--iso", "/nonexistent/nfsmw.iso", "--include-fair-control"]), 2)
            fake_iso = Path(tmp) / "fake.iso"
            fake_iso.write_bytes(b"\0")
            self.assertEqual(bench.main(["--iso", str(fake_iso), "--stock-control", "--replacements-dir", str(Path(tmp) / "missing")]), 2)
            self.assertEqual(bench.main(["--iso", str(fake_iso), "--mod-iso", str(Path(tmp) / "missing.iso")]), 2)
            if HAS_IMAGING:
                self.assertEqual(bpack.main(["--iso", "/nonexistent/nfsmw.iso", "--relief-mode", "polished"]), 1)

    @needs_imaging
    def test_backwards_compatible_modes(self) -> None:
        self.assertEqual(tr3d.RELIEF_MODES, ("faithful", "sculpted", "polished"))
        self.assertEqual(bpack.RELIEF_MODE_CHOICES, tr3d.RELIEF_MODES)
        self.assertEqual(bpack.DEFAULT_POLISHED_OUT_DIR.parts[-3:], ("pcsx2_textures_v29", "SLUS-21351", "replacements"))
        wall = np.full((32, 32, 4), (120, 110, 100, 128), dtype=np.uint8)
        wall[8:24, 8:24, :3] = (30, 32, 36)
        s, smat, _ = bpack.enhance_texture_rgba(wall, name="ARC_OLDBRICKA_WINA_LL", scale=4, small_scale=4, relief_mode="sculpted")
        ref, _, _ = tr3d.sculpt_texture_relief_rgba(wall, name="ARC_OLDBRICKA_WINA_LL", scale=4, small_scale=4)
        np.testing.assert_array_equal(s, ref)  # v2.5 sculpted output is byte-identical
        self.assertEqual(smat, "architecture")
        for nm in ("ARC_COLONIALHSE4WINDOW", "ARC_CITYEASTBLD7WIN1QUA", "BLD_GLASS_A", "XO_SHOP_WIN"):
            self.assertTrue(tr3d.is_window_texture(nm), nm)
        for nm in ("ARC_BRICK01", "WINDOWREFLECTIONIND", "ORG_TREE_A"):
            self.assertFalse(tr3d.is_window_texture(nm), nm)


@needs_imaging
class TestLiveRetailIso(unittest.TestCase):
    """Live retail ISO (opened read-only): CARSHADOW hash discovery + window/foliage mini pack build."""

    def test_car_shadow_hash_and_window_foliage_pack(self) -> None:
        iso = _find_live_iso()
        if iso is None:
            self.skipTest("Live PS2 ISO unavailable (offline CI mode).")
        arc = PS2NFSMWArchive(iso)
        shadows = [t for t in pth.iter_tpk_raw_textures(arc.read_entry(arc.find(GLOBALB)), GLOBALB) if tr3d.is_car_contact_shadow(t.name, t.archive)]
        self.assertEqual(len(shadows), 1)
        cs = shadows[0]
        self.assertEqual((cs.width, cs.height, pth.compute_pcsx2_stems(cs, include_sub_mips=False)[0]), (64, 32, CARSHADOW_STEM))
        self.assertEqual(bpack.is_denylisted_texture(cs.name, GLOBALB, allow_car_shadow=True), (False, ""))
        found: Dict[str, Any] = {}
        for sname, _, sbytes in pth.iter_streaml2ra_sections(arc.read_entry(arc.find(r"TRACKS\L2RA.BUN")), arc.read_entry(arc.find(r"TRACKS\STREAML2RA.BUN"))):
            for tex in pth.iter_tpk_raw_textures(sbytes, r"TRACKS\STREAML2RA.BUN", section=sname):
                if tex.name.upper() in ("ARC_COLONIALHSE1WINDOW2", "ORG_BUSH_C_AL") and tex.name.upper() not in found:
                    found[tex.name.upper()] = tex
            if len(found) == 2:
                break
        self.assertEqual(len(found), 2)
        items, expect = [], {}
        for tex in [cs] + list(found.values()):
            rgba = pth.decode_raw_tpk_rgba(tex, scale_alpha=False)
            out, mat, amode = bpack.enhance_texture_rgba(rgba, name=tex.name, archive=tex.archive, relief_mode="polished")
            self.assertLessEqual(int(out[..., 3].max()), 128)
            expect[tex.name.upper()] = mat
            img: Any = out
            if mat == "foliage":
                self.assertEqual(amode, "cutout_0_128")
                img = tr3d.build_coverage_preserving_mips(out)
                base_cov = float((out[..., 3] >= 64).mean())
                self.assertAlmostEqual(float((img[3][..., 3] >= 64).mean()), base_cov, delta=0.05)
            items.append(([(pth.compute_pcsx2_stems(tex, include_sub_mips=False)[0], 0)], img))
        self.assertEqual(expect, {"CARSHADOW": "car_contact_shadow", "ARC_COLONIALHSE1WINDOW2": "window_glass", "ORG_BUSH_C_AL": "foliage"})
        enc = imtt.find_rockport_dds()
        if enc is None:
            self.skipTest("rockport_dds encoder not built")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bpack.encode_batch_to_dds(items, Path(tmp), enc)[0], 3)
            for (targets, img) in items:
                b = (Path(tmp) / f"{targets[0][0]}.dds").read_bytes()
                self.assertEqual((b[:4], struct.unpack_from("<I", b, 128)[0]), (b"DDS ", 98))
                if isinstance(img, list):
                    self.assertEqual(struct.unpack_from("<I", b, 28)[0], len(img))


if __name__ == "__main__":
    unittest.main()
