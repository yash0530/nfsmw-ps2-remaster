#!/usr/bin/env python3
"""Unit and smoke tests for Path 2 Native Metal PBR scene exporter and renderer."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
NATIVE_METAL_DIR = REPO_ROOT / "native_metal"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_pbr_scene as eps  # noqa: E402


class TestMetalPBRExporterAndRenderer(unittest.TestCase):
    def test_ps2_to_yup_proper_rotation(self) -> None:
        # (+X=fwd, +Y=left, +Z=up) -> (-y, z - ground, x), det == +1
        v = eps.ps2_vec_to_yup((2.0, 0.5, 1.0), ground_z_ps2=0.2)
        self.assertEqual(v, (-0.5, 0.8, 2.0))
        n = eps.ps2_norm_to_yup((0.0, 0.0, 2.0))
        self.assertAlmostEqual(n[0], 0.0, places=6)
        self.assertAlmostEqual(n[1], 1.0, places=6)
        self.assertAlmostEqual(n[2], 0.0, places=6)

    def test_lighting_presets_loaded_from_repo_patches(self) -> None:
        presets = eps.load_lighting_presets(REPO_ROOT)
        self.assertIn("golden_hour", presets)
        self.assertIn("daylight", presets)
        self.assertIn("night_rain", presets)
        self.assertIn("rockport2026_golden_hour.json", presets["golden_hour"]["source_patch"])
        self.assertIn("rockport2026_daylight.json", presets["daylight"]["source_patch"])
        for key in ("golden_hour", "daylight", "night_rain"):
            sdir = presets[key]["sun_direction"]
            mag = sum(c * c for c in sdir) ** 0.5
            self.assertAlmostEqual(mag, 1.0, places=4)

    def test_synthetic_scene_export_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "scene_synth"
            rc = eps.main(["--synthetic", "--out", str(out_dir)])
            self.assertEqual(rc, 0)

            scene_path = out_dir / "scene.json"
            self.assertTrue(scene_path.is_file())
            manifest = json.loads(scene_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["coordinate_system"], "yup_right_handed_meters")
            self.assertEqual(manifest["mode"], "synthetic")
            self.assertGreaterEqual(manifest["summary"]["part_count"], 9)
            self.assertGreater(manifest["summary"]["total_triangles"], 500)

            part_names = {p["name"] for p in manifest["parts"]}
            self.assertIn("SPOILER_STYLE01_B", part_names)
            for w in ("FL", "FR", "RL", "RR"):
                self.assertIn(f"ENKEI_STYLE01_17_25_B_WHEEL_{w}", part_names)

            # Verify every referenced OBJ exists and uses semantic `usemtl` tags
            for part in manifest["parts"]:
                obj_file = out_dir / part["obj"]
                self.assertTrue(obj_file.is_file(), f"Missing {obj_file}")
                txt = obj_file.read_text(encoding="utf-8")
                self.assertIn("usemtl ", txt)
                self.assertIn("vn ", txt)

    def test_nonexistent_iso_fails_cleanly_without_fallback_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                eps.export_iso_scene(Path("/nonexistent/does_not_exist.iso"), Path(td))

    def test_metal_shader_syntax_when_xcrun_metal_installed(self) -> None:
        shader = NATIVE_METAL_DIR / "Shaders.metal"
        self.assertTrue(shader.is_file())
        if platform.system() != "Darwin":
            self.skipTest("xcrun metal check requires macOS")
        ver = subprocess.run(
            ["xcrun", "--sdk", "macosx", "metal", "--version"],
            capture_output=True,
            text=True,
        )
        if ver.returncode != 0:
            self.skipTest("Xcode MetalToolchain component not installed; runtime fallback covered below")
        with tempfile.TemporaryDirectory() as td:
            air = Path(td) / "Shaders.air"
            res = subprocess.run(
                ["xcrun", "--sdk", "macosx", "metal", "-c", str(shader), "-o", str(air)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"Metal compile failed:\n{res.stderr}")
            self.assertTrue(air.is_file())

    def test_compile_and_headless_smoke_render_on_macos_metal(self) -> None:
        if platform.system() != "Darwin" or not Path("/usr/bin/swiftc").is_file():
            self.skipTest("Native Metal smoke test requires macOS + /usr/bin/swiftc")

        # Verify Metal GPU exists before compiling/running
        probe = subprocess.run(
            ["swift", "-e", "import Metal; exit(MTLCreateSystemDefaultDevice() == nil ? 1 : 0)"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            self.skipTest("No Metal GPU available in this environment")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            build_out = tmp / "metal_bin"
            scene_out = tmp / "scene_synth"
            png_out = tmp / "smoke.png"

            # Export synthetic scene
            self.assertEqual(eps.main(["--synthetic", "--out", str(scene_out)]), 0)

            # Build RockportMetalPBR into temp dir
            mk = subprocess.run(
                ["make", "-C", str(NATIVE_METAL_DIR), f"OUT_DIR={build_out}"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(mk.returncode, 0, f"make failed:\n{mk.stdout}\n{mk.stderr}")
            binary = build_out / "RockportMetalPBR"
            self.assertTrue(binary.is_file())

            # Run headless render from a neutral working directory (tests shader path resolution!)
            run = subprocess.run(
                [
                    str(binary),
                    "--headless",
                    "--scene",
                    str(scene_out / "scene.json"),
                    "--out",
                    str(png_out),
                    "--width",
                    "640",
                    "--height",
                    "360",
                    "--preset",
                    "golden_hour",
                ],
                cwd=str(tmp),
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, f"Headless render failed:\n{run.stdout}\n{run.stderr}")
            self.assertTrue(png_out.is_file())
            self.assertGreater(png_out.stat().st_size, 10_000)

            # Verify PNG header & non-flat pixel variance if Pillow is installed
            raw = png_out.read_bytes()
            self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
            try:
                from PIL import Image
                import numpy as np

                arr = np.asarray(Image.open(png_out), dtype=np.float32)
                self.assertEqual(arr.shape[:2], (360, 640))
                self.assertGreater(float(arr.std()), 10.0)
            except ImportError:
                pass


if __name__ == "__main__":
    unittest.main()
