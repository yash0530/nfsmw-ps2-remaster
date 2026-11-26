#!/usr/bin/env python3
"""Integration tests for Hour 3 (v0.9.0) Playable Gameplay Loop in RockportMetalRT.

Verifies:
1. Canonical keymap (WASD/Arrows, Space handbrake, Shift/Z NOS, X/Tab Speedbreaker,
   F instant pursuit, V landmark, G car cycle, Esc quit — NO 'q' quit!).
2. CLI flags (--pursuit, --nos, --speedbreaker, --no-ai, --no-audio) & headless silent audio.
3. Full Swift+Metal integration of RTPhysics, RTAudioEngine, RTGameWorld, RTAIVehicleLoader,
   and aiRoadProbeKernel.
4. Live Apple Silicon Metal RT execution both offline (--synthetic, works with NFSMW_ISO=/nonexistent)
   and against full rockport_full.rkpt when present locally.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
NATIVE_RT_DIR = REPO_ROOT / "native_metal_rt"
DEFAULT_BIN = REPO_ROOT / "build" / "metal_rt_v06" / "RockportMetalRT"
FULL_RKPT = REPO_ROOT / "build" / "rockport_world" / "rockport_full.rkpt"


def _has_apple_metal_rt_toolchain() -> bool:
    if sys.platform != "darwin":
        return False
    if shutil.which("swiftc") is None or shutil.which("make") is None:
        return False
    try:
        sdk = subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-version"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return int(sdk.split(".")[0]) >= 26
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


class TestGameplayIntegrationContract(unittest.TestCase):
    """Offline-safe structural & contract checks (pass with NFSMW_ISO=/nonexistent)."""

    def test_required_gameplay_sources_present(self) -> None:
        required = [
            "Makefile",
            "RTPhysics.swift",
            "RTAudioEngine.swift",
            "RTGameWorld.swift",
            "RTAIVehicleLoader.swift",
            "RTHudTypes.swift",
            "RTHud.metal",
            "RTRenderer.swift",
            "main.swift",
            "RayTracing.metal",
        ]
        for name in required:
            path = NATIVE_RT_DIR / name
            self.assertTrue(path.is_file(), f"Missing native_metal_rt/{name}")

    def test_makefile_links_audio_and_gamepad_frameworks(self) -> None:
        mk = (NATIVE_RT_DIR / "Makefile").read_text(encoding="utf-8")
        for token in (
            "RTPhysics.swift",
            "RTAudioEngine.swift",
            "RTGameWorld.swift",
            "RTAIVehicleLoader.swift",
            "-framework AVFoundation",
            "-framework GameController",
        ):
            self.assertIn(token, mk, f"Makefile missing {token}")

    def test_canonical_keymap_and_cli_flags(self) -> None:
        src = (NATIVE_RT_DIR / "main.swift").read_text(encoding="utf-8")
        for token in (
            "override func flagsChanged(with event: NSEvent)",
            "r.nosShiftHeld = event.modifierFlags.contains(.shift)",
            "case 49:  r.handbrakeHeld = true",
            "case 48:  r.speedbreakerHeld = true",
            'case "z": r.nosKeyHeld = true',
            'case "x": r.speedbreakerHeld = true',
            'case "f":',
            "r.triggerInstantPursuit()",
            'case "v":',
            'case "g":',
            'case "--pursuit":',
            'case "--nos":',
            'case "--speedbreaker":',
            'case "--no-ai":',
            'case "--no-audio":',
            "let audioHeadless = opts.headless || opts.noAudio",
        ):
            self.assertIn(token, src, f"main.swift missing canonical gameplay binding: {token}")

        # Verify 'q' is NOT bound to quit (Esc only)
        self.assertNotIn('case "q", "\\u{1b}"', src)
        self.assertNotRegex(src, r'case\s+"q"\s*:\s*NSApp\.terminate')

    def test_renderer_wires_all_gameplay_subsystems(self) -> None:
        src = (NATIVE_RT_DIR / "RTRenderer.swift").read_text(encoding="utf-8")
        for token in (
            "let physicsEngine: RTPhysicsEngine",
            "let audioEngine: RTAudioEngine",
            "let gameWorld: RTGameWorld",
            "RTAIVehicleLoader.load",
            "writeAITLASInstances",
            "aiRoadProbeKernel",
            "encodeAIRoadProbe",
            "sceneDataBuffer ?? aiSceneDataFallbackBuffer, offset: 0, index: 14",
            "gameWorld.copLights",
            "gameWorld.packMinimap",
            "gameWorld.fillHudExt",
            "physicsEngine.step(",
            "audioEngine.update(",
        ):
            self.assertIn(token, src, f"RTRenderer.swift missing integration symbol: {token}")

        gw_src = (NATIVE_RT_DIR / "RTGameWorld.swift").read_text(encoding="utf-8")
        self.assertIn("clampToRoadCorridor", gw_src)
        self.assertIn("isRollingLead", gw_src)

    def test_no_hardcoded_personal_paths(self) -> None:
        files = [
            NATIVE_RT_DIR / "Makefile",
            NATIVE_RT_DIR / "RTPhysics.swift",
            NATIVE_RT_DIR / "RTAudioEngine.swift",
            NATIVE_RT_DIR / "RTGameWorld.swift",
            NATIVE_RT_DIR / "RTAIVehicleLoader.swift",
            NATIVE_RT_DIR / "RTRenderer.swift",
            NATIVE_RT_DIR / "main.swift",
            Path(__file__),
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"/Users/[A-Za-z0-9_.-]+/", f"Hardcoded personal path in {path}")


class TestGameplayIntegrationRuntime(unittest.TestCase):
    """End-to-end Apple Silicon Metal RT execution tests (offline synthetic + live Rockport)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp_dir_obj = None
        if not _has_apple_metal_rt_toolchain():
            cls.bin_path = None
            return
        if DEFAULT_BIN.is_file():
            cls.bin_path = DEFAULT_BIN
            return
        cls._tmp_dir_obj = tempfile.TemporaryDirectory(prefix="rockport_gameplay_int_")
        out_dir = Path(cls._tmp_dir_obj.name)
        res = subprocess.run(
            ["make", "-C", str(NATIVE_RT_DIR), f"OUT_DIR={out_dir}"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile RockportMetalRT:\n{res.stdout}\n{res.stderr}")
        cls.bin_path = out_dir / "RockportMetalRT"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._tmp_dir_obj is not None:
            cls._tmp_dir_obj.cleanup()

    @unittest.skipUnless(_has_apple_metal_rt_toolchain(), "Requires macOS Apple Silicon Metal RT toolchain")
    def test_cli_help_lists_canonical_keymap_and_flags(self) -> None:
        assert self.bin_path is not None
        res = subprocess.run(
            [str(self.bin_path), "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        for token in ("--pursuit", "--nos", "--speedbreaker", "--no-ai", "--no-audio", "Speedbreaker", "Instant Pursuit", "Esc: Quit"):
            self.assertIn(token, res.stdout)
        self.assertNotIn("Q/Esc: Quit", res.stdout)

    @unittest.skipUnless(_has_apple_metal_rt_toolchain(), "Requires macOS Apple Silicon Metal RT toolchain")
    def test_synthetic_offline_drive_bench_nos_and_speedbreaker(self) -> None:
        """Runs 100% offline with NFSMW_ISO=/nonexistent (zero ISO or .rkpt required)."""
        assert self.bin_path is not None
        with tempfile.TemporaryDirectory(prefix="rockport_synth_bench_") as tmp:
            out_png = Path(tmp) / "synth_nos.png"
            env = dict(os.environ)
            env["NFSMW_ISO"] = "/nonexistent"
            res = subprocess.run(
                [
                    str(self.bin_path),
                    "--synthetic",
                    "--drive-bench",
                    "12",
                    "--nos",
                    "--no-ai",
                    "--no-audio",
                    "--width",
                    "640",
                    "--height",
                    "360",
                    "--out",
                    str(out_png),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 2 and ("not supported" in res.stderr.lower() or "no metal" in res.stderr.lower()):
                self.skipTest("Metal hardware ray tracing unsupported on this host GPU")
            self.assertEqual(res.returncode, 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
            self.assertIn("supportsRaytracing=true", res.stdout)
            self.assertIn("aiInsts=0", res.stdout)
            self.assertTrue(out_png.is_file())
            with Image.open(out_png) as im:
                im.verify()
            with Image.open(out_png) as im:
                im.load()
                self.assertEqual(im.size, (640, 360))
                arr = np.asarray(im.convert("RGB"), dtype=np.float32)
                self.assertGreater(float(arr.mean()), 8.0)

    @unittest.skipUnless(
        _has_apple_metal_rt_toolchain() and FULL_RKPT.is_file(),
        "Requires macOS Apple Silicon and local build/rockport_world/rockport_full.rkpt",
    )
    def test_full_rockport_pursuit_nos_framegen_and_no_ai(self) -> None:
        """Live integration test: 20 AI traffic+police, Heat 3 pursuit, NOS, MetalFX 2x Frame-Gen."""
        assert self.bin_path is not None
        with tempfile.TemporaryDirectory(prefix="rockport_live_int_") as tmp:
            pursuit_png = Path(tmp) / "pursuit_nos_fg.png"
            res = subprocess.run(
                [
                    str(self.bin_path),
                    "--world",
                    str(FULL_RKPT),
                    "--car",
                    "m3_gtr",
                    "--landmark",
                    "rosewood",
                    "--drive-bench",
                    "30",
                    "--pursuit",
                    "--nos",
                    "--frame-gen",
                    "on",
                    "--out",
                    str(pursuit_png),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 2 and ("not supported" in res.stderr.lower() or "no metal" in res.stderr.lower()):
                self.skipTest("Metal hardware ray tracing unsupported on this host GPU")
            self.assertEqual(res.returncode, 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
            out = res.stdout

            def grab_int(pattern: str) -> int:
                m = re.search(pattern, out)
                self.assertIsNotNone(m, f"Missing {pattern} in:\n{out}")
                assert m is not None
                return int(m.group(1))

            def grab_float(pattern: str) -> float:
                m = re.search(pattern, out)
                self.assertIsNotNone(m, f"Missing {pattern} in:\n{out}")
                assert m is not None
                return float(m.group(1))

            ai_traffic = grab_int(r"aiTraffic=(\d+)")
            ai_cops = grab_int(r"aiCops=(\d+)")
            heat = grab_int(r"heat=(\d+)")
            speed = grab_float(r"speed=([0-9.]+)kmh")
            vram_mb = grab_float(r"vram=([0-9.]+)MB")
            fps = grab_float(r"\(([0-9.]+)\s+FPS\)")

            self.assertGreaterEqual(ai_traffic, 12, out)
            self.assertGreaterEqual(ai_cops, 2, out)
            self.assertGreaterEqual(heat, 1, out)
            self.assertGreaterEqual(speed, 150.0, out)
            self.assertLessEqual(vram_mb, 600.0, out)
            self.assertGreaterEqual(fps, 80.0, out)
            self.assertIn("frameGen=on", out)

            with Image.open(pursuit_png) as im:
                im.verify()
            with Image.open(pursuit_png) as im:
                im.load()
                self.assertEqual(im.size, (1920, 1080))
                arr = np.asarray(im.convert("RGB"), dtype=np.float32)
                mean_luma = float(arr.mean())
                self.assertGreater(mean_luma, 25.0, "Frame unexpectedly black")
                self.assertLess(mean_luma, 200.0, "Frame unexpectedly overexposed/blown out")

            # Verify --no-ai cleanly zeroes AI instances while keeping world RT intact
            no_ai_png = Path(tmp) / "no_ai.png"
            res_no_ai = subprocess.run(
                [
                    str(self.bin_path),
                    "--world",
                    str(FULL_RKPT),
                    "--car",
                    "m3_gtr",
                    "--landmark",
                    "rosewood",
                    "--drive-bench",
                    "12",
                    "--no-ai",
                    "--frame-gen",
                    "on",
                    "--width",
                    "960",
                    "--height",
                    "540",
                    "--out",
                    str(no_ai_png),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_no_ai.returncode, 0, res_no_ai.stderr)
            self.assertIn("aiInsts=0", res_no_ai.stdout)
            self.assertIn("aiTraffic=0", res_no_ai.stdout)
            self.assertIn("aiCops=0", res_no_ai.stdout)


if __name__ == "__main__":
    unittest.main()
