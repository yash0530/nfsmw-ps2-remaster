"""Tests for the pure Apple Swift + Metal 4 Hardware Ray Tracing engine (`native_metal_rt/`)."""

from __future__ import annotations

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
BUILD_OUT_DIR = REPO_ROOT / "build" / "metal_rt_wave3_test"


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

    def test_shader_uses_metal_raytracing_primitives(self) -> None:
        metal_src = (NATIVE_RT_DIR / "RayTracing.metal").read_text(encoding="utf-8")
        self.assertIn("#include <metal_raytracing>", metal_src)
        self.assertIn("instance_acceleration_structure", metal_src)
        self.assertIn("intersector<triangle_data, instancing>", metal_src)
        self.assertIn("raytraceKernel", metal_src)
        self.assertIn("bloomThresholdKernel", metal_src)
        self.assertIn("bloomBlurKernel", metal_src)
        self.assertIn("compositeKernel", metal_src)

    def test_swift_uses_acceleration_structures_and_runtime_check(self) -> None:
        rt_src = (NATIVE_RT_DIR / "RTRenderer.swift").read_text(encoding="utf-8")
        self.assertIn("device.supportsRaytracing", rt_src)
        self.assertIn("MTLPrimitiveAccelerationStructureDescriptor", rt_src)
        self.assertIn("MTLInstanceAccelerationStructureDescriptor", rt_src)
        self.assertIn("makeAccelerationStructureCommandEncoder", rt_src)
        self.assertIn("useResources(blasList", rt_src)

    def test_no_hardcoded_personal_paths(self) -> None:
        files_to_check = list(NATIVE_RT_DIR.glob("*")) + [RESEARCH_DOC, Path(__file__)]
        for path in files_to_check:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            # Ensure no personal /Users/<username> home paths are leaked
            self.assertNotRegex(text, r"/Users/[A-Za-z0-9_.-]+/", f"Personal home path found in {path}")


class TestMetalRayTracingExecution(unittest.TestCase):
    """Build RockportMetalRT and execute offline synthetic hardware-RT renders on Apple Silicon."""

    @unittest.skipUnless(_has_apple_metal_rt_toolchain(), "Requires macOS with swiftc and make")
    def test_compile_and_synthetic_headless_render(self) -> None:
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

            out_png = tmp_path / "synth_golden.png"
            cmp_png = tmp_path / "synth_compare.png"
            run_res = subprocess.run(
                [
                    str(binary),
                    "--headless",
                    "--synthetic",
                    "--preset",
                    "golden_hour",
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
            self.assertTrue(out_png.is_file(), "Golden hour PNG missing")
            self.assertTrue(cmp_png.is_file(), "Split compare PNG missing")

            # Verify PNG signature & IHDR dimensions via pure stdlib
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
                # Verify left (raster baseline) vs right (hardware RT) produce meaningfully distinct pixels
                self.assertGreater(float(np.abs(left.mean() - right.mean()) + np.abs(left.std() - right.std())), 1.0)
            else:
                self.assertGreater(len(raw_out), 4096)
                self.assertIn(b"IDAT", raw_out)
                _ = zlib.crc32(raw_out[:16])


if __name__ == "__main__":
    unittest.main()
