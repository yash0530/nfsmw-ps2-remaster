"""Tests for the pure Apple Swift + Metal 4 Hardware Ray Tracing engine (`native_metal_rt/`)."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
    _HAS_PIL_NUMPY = True
except ImportError:
    _HAS_PIL_NUMPY = False

REPO_ROOT = Path(__file__).resolve().parent.parent
NATIVE_RT_DIR = REPO_ROOT / "native_metal_rt"
RESEARCH_DOC = REPO_ROOT / "docs" / "research" / "METAL4_HARDWARE_RAYTRACING.md"


def _has_apple_metal_rt_toolchain() -> bool:
    if os.uname().sysname != "Darwin":
        return False
    return shutil.which("swiftc") is not None and shutil.which("make") is not None


class TestMetalRayTracingStructure(unittest.TestCase):
    """Verify source files, build scripts, and documentation hygiene."""

    def test_required_files_exist(self) -> None:
        required = [
            NATIVE_RT_DIR / "Makefile",
            NATIVE_RT_DIR / "README.md",
            NATIVE_RT_DIR / "RayTracing.metal",
            NATIVE_RT_DIR / "RTSceneBuilder.swift",
            NATIVE_RT_DIR / "RTRenderer.swift",
            NATIVE_RT_DIR / "main.swift",
            RESEARCH_DOC,
        ]
        for path in required:
            self.assertTrue(path.is_file(), f"Missing required file: {path}")

    def test_shader_uses_metal_raytracing_and_atrous_denoiser(self) -> None:
        metal_src = (NATIVE_RT_DIR / "RayTracing.metal").read_text(encoding="utf-8")
        for symbol in (
            "#include <metal_raytracing>",
            "instance_acceleration_structure",
            "intersector<triangle_data, instancing>",
            "sampleGGXVNDF",
            "evaluateNishitaAtmosphere",
            "raytraceKernel",
            "denoiseAtrousKernel",
            "remodulateKernel",
            "bloomThresholdKernel",
            "bloomBlurKernel",
            "compositeKernel",
        ):
            self.assertIn(symbol, metal_src)

    def test_swift_uses_acceleration_structures_config_and_drive_mode(self) -> None:
        rt_src = (NATIVE_RT_DIR / "RTRenderer.swift").read_text(encoding="utf-8")
        self.assertIn("device.supportsRaytracing", rt_src)
        self.assertIn("MTLPrimitiveAccelerationStructureDescriptor", rt_src)
        self.assertIn("MTLInstanceAccelerationStructureDescriptor", rt_src)
        self.assertIn("makeAccelerationStructureCommandEncoder", rt_src)
        self.assertIn("useResources(blasList", rt_src)
        self.assertIn("denoiseAtrousKernel", rt_src)
        self.assertIn("remodulateKernel", rt_src)
        self.assertIn("configureDrivePose", rt_src)

        scene_src = (NATIVE_RT_DIR / "RTSceneBuilder.swift").read_text(encoding="utf-8")
        self.assertIn("RTLookdevConfig", scene_src)
        self.assertIn("RTCarProfile", scene_src)
        self.assertIn("build/flywheel/hq_m3_gtr/scene.json", scene_src)
        self.assertIn("build/flywheel/hq_carrera_gt/scene.json", scene_src)
        self.assertIn("build/flywheel/hq_gallardo/scene.json", scene_src)

    def test_no_hardcoded_personal_paths(self) -> None:
        files_to_check = list(NATIVE_RT_DIR.glob("*")) + [RESEARCH_DOC, Path(__file__)]
        for path in files_to_check:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"/Users/[A-Za-z0-9_.-]+/", f"Personal home path found in {path}")


class TestMetalRayTracingExecution(unittest.TestCase):
    """Build RockportMetalRT and execute offline synthetic hardware-RT renders on Apple Silicon."""

    @unittest.skipUnless(_has_apple_metal_rt_toolchain(), "Requires macOS with swiftc and make")
    def test_compile_and_synthetic_headless_config_and_drive_demo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rockport_rt_test_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            make_res = subprocess.run(
                ["make", "-C", str(NATIVE_RT_DIR), f"OUT_DIR={tmp_path}"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(make_res.returncode, 0, f"Build failed:\n{make_res.stdout}\n{make_res.stderr}")

            binary = tmp_path / "RockportMetalRT"
            self.assertTrue(binary.is_file(), "RockportMetalRT binary not created")

            cfg_json = tmp_path / "lookdev.json"
            cfg_json.write_text(
                json.dumps(
                    {
                        "preset": "golden_hour",
                        "sun_elevation_deg": 14.0,
                        "sun_azimuth_deg": -145.0,
                        "exposure": 1.05,
                        "cloud_coverage": 0.55,
                        "puddle_wetness": 0.65,
                        "clearcoat_roughness": 0.018,
                        "flake_strength": 0.004,
                        "bloom_intensity": 0.28,
                        "bloom_threshold": 1.5,
                        "denoise_passes": 4,
                        "camera_fov": 40.0,
                        "vignette": 0.12,
                        "contrast": 1.02,
                        "saturation": 1.08,
                    }
                ),
                encoding="utf-8",
            )

            out_png = tmp_path / "synth_golden.png"
            cmp_png = tmp_path / "synth_compare.png"
            run_res = subprocess.run(
                [
                    str(binary),
                    "--headless",
                    "--synthetic",
                    "--config",
                    str(cfg_json),
                    "--spp",
                    "4",
                    "--width",
                    "320",
                    "--height",
                    "180",
                    "--out",
                    str(out_png),
                    "--compare-out",
                    str(cmp_png),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            if run_res.returncode == 2 and "not supported" in run_res.stderr.lower():
                self.skipTest("Metal hardware ray tracing unsupported on this host GPU")

            self.assertEqual(run_res.returncode, 0, f"RT run failed:\n{run_res.stdout}\n{run_res.stderr}")
            self.assertIn("supportsRaytracing=true", run_res.stdout)
            self.assertIn("denoise=atrous4", run_res.stdout)
            self.assertTrue(out_png.is_file(), "Golden hour PNG missing")
            self.assertTrue(cmp_png.is_file(), "Split compare PNG missing")

            drive_png = tmp_path / "synth_drive_demo.png"
            drive_res = subprocess.run(
                [
                    str(binary),
                    "--headless",
                    "--synthetic",
                    "--car",
                    "carrera_gt",
                    "--drive-demo",
                    "--hud",
                    "--speed-kmh",
                    "165",
                    "--steer-deg",
                    "10",
                    "--spp",
                    "4",
                    "--width",
                    "320",
                    "--height",
                    "180",
                    "--out",
                    str(drive_png),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(drive_res.returncode, 0, f"Drive demo run failed:\n{drive_res.stdout}\n{drive_res.stderr}")
            self.assertIn("car=carrera_gt", drive_res.stdout)
            self.assertIn("hud=on", drive_res.stdout)
            self.assertTrue(drive_png.is_file(), "Drive demo PNG missing")

            raw_out = out_png.read_bytes()
            self.assertTrue(raw_out.startswith(b"\x89PNG\r\n\x1a\n"))
            ihdr_w, ihdr_h = struct.unpack(">II", raw_out[16:24])
            self.assertEqual((ihdr_w, ihdr_h), (320, 180))

            if _HAS_PIL_NUMPY:
                arr = np.array(Image.open(out_png))
                self.assertEqual(arr.shape, (180, 320, 3))
                self.assertGreater(float(arr.std()), 15.0, "Rendered RT image appears blank/flat")

                cmp_arr = np.array(Image.open(cmp_png))
                self.assertEqual(cmp_arr.shape, (180, 320, 3))
                left = cmp_arr[:, :158, :].astype(np.float32)
                right = cmp_arr[:, 162:, :].astype(np.float32)
                self.assertGreater(float(np.abs(left.mean() - right.mean()) + np.abs(left.std() - right.std())), 1.0)
            else:
                self.assertGreater(len(raw_out), 4096)
                self.assertIn(b"IDAT", raw_out)
                _ = zlib.crc32(raw_out[:16])


if __name__ == "__main__":
    unittest.main()
