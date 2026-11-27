#!/usr/bin/env python3
"""Offline (Linux + macOS CI safe) tests for the v2.1 PCSX2 2026 profile + live benchmark helpers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import benchmark_pcsx2_live as bench  # noqa: E402
import pcsx2_profile_2026 as prof  # noqa: E402

HAS_IMAGING = all(importlib.util.find_spec(m) is not None for m in ("PIL", "numpy"))


def _kv(ini: str) -> List[str]:
    return [line.strip() for line in ini.splitlines() if "=" in line and not line.startswith(";")]


class TestPcsx2Profile2026(unittest.TestCase):
    def test_v21_defaults(self) -> None:
        ini = prof.build_v21_metal_profile()
        self.assertEqual(prof.validate_profile_ini(ini), [])
        kv = _kv(ini)
        for expected in ("Renderer = 17", "dithering_ps2 = 3", "TriFilter = 2", "MaxAnisotropy = 16", "mipmap = true",
                         "hw_mipmap = true", "upscale_multiplier = 6", "accurate_blending_unit = 3",
                         "ShadeBoost = true", "ShadeBoost_Brightness = 50", "ShadeBoost_Contrast = 52",
                         "ShadeBoost_Saturation = 54", "ShadeBoost_Gamma = 50", "CASMode = 2", "CASSharpness = 38",
                         "fxaa = true", "TVShader = 0", "LoadTextureReplacementsAsync = true",
                         "PrecacheTextureReplacements = true", "EECycleRate = 3", "AspectRatio = 16:9"):
            self.assertIn(expected, kv)
        hacks = {k: v for k, v in prof.SLUS21351_HW_FIXES.items() if k.startswith("UserHacks_")}
        self.assertEqual(len(hacks), 5)
        for k, v in prof.SLUS21351_HW_FIXES.items():
            self.assertIn(f"{k} = {v}", kv)
        enabled = [line.split("=", 1)[1].strip() for line in kv if line.startswith("Enable =")]
        self.assertEqual(enabled, prof.V21_DEFAULT_PATCHES)
        self.assertNotIn("AsyncTextureReplacements", ini.replace("LoadTextureReplacementsAsync", ""))

    def test_scale8_and_rgss(self) -> None:
        kv = _kv(prof.build_v21_metal_profile(scale=8, rgss=True))
        for expected in ("upscale_multiplier = 8", "TVShader = 6", "fxaa = false", "CASMode = 0"):
            self.assertIn(expected, kv)

    def test_original_baseline(self) -> None:
        ini = prof.build_original_ps2_baseline_profile()
        self.assertEqual(prof.validate_profile_ini(ini), [])
        kv = _kv(ini)
        for expected in ("upscale_multiplier = 1", "AspectRatio = 4:3", "EnablePatches = false", "EECycleRate = 0",
                         "UserHacks = false", "ShadeBoost = false"):
            self.assertIn(expected, kv)
        self.assertFalse(any(line.startswith("Enable =") for line in kv))

    def test_bad_args_and_misspelled_keys_rejected(self) -> None:
        for kwargs in ({"scale": True}, {"scale": "6"}, {"scale": 0}, {"anisotropy": 3}, {"ee_cycle_rate": 5},
                       {"renderer": 99}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                prof.build_v21_metal_profile(**kwargs)  # type: ignore[arg-type]
        errors = prof.validate_profile_ini("[EmuCore/GS]\nAsyncTextureReplacements = true\n")
        self.assertTrue(any("unknown key 'AsyncTextureReplacements'" in e for e in errors))
        self.assertTrue(prof.validate_profile_ini("[EmuCore/GS]\nfxaa = yes\nfxaa = true\n"))

    def test_cli_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sub" / "SLUS-21351_0518D274.ini"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(prof.main(["--scale", "8", "--rgss", "--out", str(out), "--json"]), 0)
            text = out.read_text(encoding="utf-8")
            self.assertEqual(prof.validate_profile_ini(text), [])
            self.assertIn("upscale_multiplier = 8", text)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(prof.main(["--scale", "0"]), 2)

    def test_v241_clean_preset_and_pacing_keys(self) -> None:
        ini = prof.build_v241_clean_profile()
        self.assertEqual(prof.validate_profile_ini(ini), [])
        kv = _kv(ini)
        for expected in ("VsyncEnable = true", "VsyncQueueSize = 2", "FramerateNTSC = 60",
                         "SkipDuplicateFrames = false", "accurate_blending_unit = 1",
                         "CASMode = 0", "CASSharpness = 0", "fxaa = true", "TVShader = 0",
                         "ShadeBoost_Brightness = 43", "ShadeBoost_Contrast = 55",
                         "PrecacheTextureReplacements = true",
                         "Enable = Disable All Post-Processing (Zero Double-Lining)"):
            self.assertIn(expected, kv)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "SLUS-21351_2DAD2310.ini"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(prof.main(["--preset", "v24_clean", "--scale", "6", "--out", str(out), "--json"]), 0)
            self.assertIn("VsyncEnable = true", out.read_text(encoding="utf-8"))
        self.assertTrue(prof.validate_profile_ini("[EmuCore/GS]\nVsyncQueueSize = 9\n"))
        self.assertTrue(prof.validate_profile_ini("[EmuCore/GS]\nFramerateNTSC = 200\n"))


def _stream(fps: int, n: int = 40) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = [{"timestamp": i * 0.1, "flow_state": 3, "paused": False, "player_present": False,
                                   "world_loop_counter": 0} for i in range(5)]  # frontend ticks
    t, wlc = 0.5, 100
    for i in range(n):
        paused = 15 <= i < 20  # pause menu: counter frozen, must be excluded
        recs.append({"timestamp": round(t, 3), "flow_state": 6, "paused": paused, "player_present": True,
                     "world_loop_counter": wlc, "speed_mph": 50.0, "pos": [float(i), 0.0, 0.0],
                     "sim_timestep": 1.0 / 30.0})
        t += 0.1
        wlc += 0 if paused else fps // 10
    return recs


class TestBenchmarkHelpersOffline(unittest.TestCase):
    def test_fps_metrics_30_and_60(self) -> None:
        for fps in (30, 60):
            m = bench.compute_pine_fps_metrics(_stream(fps))
            self.assertAlmostEqual(m["avg_fps"], fps, delta=0.05)
            self.assertAlmostEqual(m["p50_fps"], fps, delta=0.05)
            self.assertAlmostEqual(m["p95_fps"], fps, delta=0.05)
            self.assertGreaterEqual(m["window_count"], 5)
            self.assertAlmostEqual(m["active_racing_sec"], 3.3, delta=0.05)  # 33 valid 0.1s steps
            self.assertEqual(m["avg_speed_mph"], 50.0)
            self.assertAlmostEqual(m["sim_timestep_s"], 1.0 / 30.0, places=6)
            self.assertGreater(m["distance_traveled_m"], 30.0)
        empty = bench.compute_pine_fps_metrics([])
        self.assertIsNone(empty["avg_fps"])
        self.assertEqual(empty["window_count"], 0)
        # Dual-counter check: true eDisplayFrame (30 FPS) vs 60 Hz WorldLoopCounter sim rate
        dual = _stream(60)
        df = 10
        for r in dual:
            r["display_frame_counter"] = df
            if r.get("flow_state") == 6 and not r.get("paused"):
                df += 3  # 30 FPS display while world_loop_counter advances 60 Hz
        dm = bench.compute_pine_fps_metrics(dual)
        self.assertEqual(dm["fps_source"], "eDisplayFrame(0x0054B5E4)")
        self.assertAlmostEqual(dm["avg_fps"], 30.0, delta=0.1)
        self.assertAlmostEqual(dm["sim_loop_hz"], 60.0, delta=0.1)

    @unittest.skipUnless(HAS_IMAGING, "Pillow + numpy required")
    def test_stitch_letterboxes_without_stretching(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            left, right, out = Path(tmp) / "l.png", Path(tmp) / "r.png", Path(tmp) / "cmp.png"
            Image.new("RGB", (640, 480), (200, 60, 60)).save(left)
            Image.new("RGB", (1280, 720), (60, 200, 60)).save(right)
            meta = bench.stitch_side_by_side(left, right, out, title="t", left_label="A", right_label="B",
                                             left_meta="30 FPS", right_meta="60 FPS")
            self.assertTrue(meta["valid"])
            self.assertEqual(meta["left_metrics"]["width"], 640)
            with Image.open(out) as im:
                im.verify()
            with Image.open(out) as im:
                im.load()
                self.assertEqual(im.size, (meta["width"], meta["height"]))
                y = 52 + 34 + 270
                self.assertEqual(im.getpixel((16 + 60, y)), bench.PANEL_BG)  # 4:3 pillarbox bar
                self.assertEqual(im.getpixel((16 + 480, y)), (200, 60, 60))
                self.assertEqual(im.getpixel((16 + 960 + 16 + 5, y)), (60, 200, 60))  # 16:9 fills panel

    @unittest.skipUnless(HAS_IMAGING, "Pillow + numpy required")
    def test_validate_png_strict_quarantines_truncated(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "snap.png"
            Image.new("RGB", (64, 64), (10, 120, 200)).save(png)
            self.assertTrue(bench.validate_png_strict(png)[0])
            png.write_bytes(png.read_bytes()[:40])
            ok, new_path, err = bench.validate_png_strict(png)
            self.assertFalse(ok)
            self.assertTrue(err)
            self.assertTrue(new_path.name.endswith(".corrupt"))
            self.assertFalse(png.exists())

    @unittest.skipUnless(HAS_IMAGING, "Pillow + numpy required")
    def test_select_five_checkpoints(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            qa, frames = run / "video_qa" / "qa_frames", run / "frames"
            qa.mkdir(parents=True)
            frames.mkdir()
            Image.new("RGB", (64, 48), (0, 0, 0)).save(qa / "qa_0000.png")  # black: excluded
            for i in range(1, 11):
                Image.new("RGB", (64, 48), (20 * i, 80, 80)).save(qa / f"qa_{i:04d}.png")
            for i in range(3):
                Image.new("RGB", (64, 36), (90, 90, 30 * i + 40)).save(frames / f"snap_{i}.png")
            picks = bench.select_five_checkpoints(run)
            self.assertEqual([label for label, _ in picks], bench.CHECKPOINT_LABELS)
            self.assertNotIn(qa / "qa_0000.png", [p for _, p in picks])
            self.assertEqual(picks[0][1].parent, qa)
            self.assertEqual([p.parent for _, p in picks[2:]], [frames] * 3)

            solo = Path(tmp) / "solo"
            (solo / "frames").mkdir(parents=True)
            Image.new("RGB", (32, 32), (120, 120, 120)).save(solo / "frames" / "only.png")
            self.assertEqual(len(bench.select_five_checkpoints(solo)), 5)
            self.assertEqual(bench.select_five_checkpoints(Path(tmp) / "missing"), [])


class TestLivePcsx2Guard(unittest.TestCase):
    def test_live_preflight_guard(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("live PCSX2 benchmark is macOS-only")
        iso = Path(os.environ.get("NFSMW_ISO", ""))
        if not os.environ.get("NFSMW_ISO") or not iso.is_file():
            self.skipTest("NFSMW_ISO not set or missing")
        import pcsx2_live_verify as lv

        if not lv.DEFAULT_PCSX2_APP.exists():
            self.skipTest("PCSX2 v2.6.3 app not installed")
        self.assertTrue(lv.run_preflight(iso).get("ok"))


if __name__ == "__main__":
    unittest.main()
