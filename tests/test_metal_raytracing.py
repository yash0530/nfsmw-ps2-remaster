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
            NATIVE_RT_DIR / "RTWorldLoader.swift",
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
            "svgfTemporalKernel",
            "physicsProbeKernel",
            "denoiseAtrousKernel",
            "remodulateKernel",
            "bloomThresholdKernel",
            "bloomBlurKernel",
            "compositeKernel",
            "resolveWorldSurface",
            "intersection_query<triangle_data, instancing>",
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

            # v0.6.0 Gate A+C: End-to-end synthetic mini-.rkpt v1 world + Bindless MTLHeap + MetalFX --drive-bench
            rkpt_path = tmp_path / "mini_rockport.rkpt"
            _write_synthetic_mini_rkpt(rkpt_path)

            bench_png = tmp_path / "rkpt_drive_bench.png"
            bench_res = subprocess.run(
                [
                    str(binary),
                    "--headless",
                    "--synthetic",
                    "--world",
                    str(rkpt_path),
                    "--car",
                    "m3_gtr",
                    "--drive-bench",
                    "10",
                    "--denoise",
                    "metalfx",
                    "--camera-mode",
                    "chase",
                    "--width",
                    "320",
                    "--height",
                    "180",
                    "--output",
                    str(bench_png),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bench_res.returncode, 0, f"RKPT drive-bench failed:\n{bench_res.stdout}\n{bench_res.stderr}")
            self.assertIn("Loaded RKPT world mini_rockport.rkpt", bench_res.stdout)
            self.assertIn("Metal RT DriveBench", bench_res.stdout)
            self.assertIn("FPS)", bench_res.stdout)
            self.assertTrue(bench_png.is_file(), "RKPT drive-bench PNG missing")

            # Also verify SVGF fallback + overhead camera mode on .rkpt world
            svgf_png = tmp_path / "rkpt_overhead_svgf.png"
            svgf_res = subprocess.run(
                [
                    str(binary),
                    "--headless",
                    "--synthetic",
                    "--world",
                    str(rkpt_path),
                    "--drive-bench",
                    "4",
                    "--denoise",
                    "svgf",
                    "--camera-mode",
                    "overhead",
                    "--width",
                    "320",
                    "--height",
                    "180",
                    "--output",
                    str(svgf_png),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(svgf_res.returncode, 0, f"RKPT SVGF run failed:\n{svgf_res.stdout}\n{svgf_res.stderr}")
            self.assertIn("denoise=svgf", svgf_res.stdout)
            self.assertTrue(svgf_png.is_file(), "RKPT overhead SVGF PNG missing")

            if _HAS_PIL_NUMPY:
                b_arr = np.array(Image.open(bench_png))
                self.assertEqual(b_arr.shape, (180, 320, 3))
                self.assertGreater(float(b_arr.std()), 12.0, "RKPT MetalFX drive-bench frame appears blank/flat")
                s_arr = np.array(Image.open(svgf_png))
                self.assertEqual(s_arr.shape, (180, 320, 3))
                self.assertGreater(float(s_arr.std()), 10.0, "RKPT SVGF frame appears blank/flat")


def _write_synthetic_mini_rkpt(out_path: Path) -> None:
    """Write a byte-exact locked .rkpt v1 mini-world (road + wall + alpha-cutout fence + 2 RGBA8 textures)."""
    # 2 textures: 16x16 asphalt road + 16x16 alpha-cutout checkerboard fence
    w, h = 16, 16
    road_px = bytearray(w * h * 4)
    fence_px = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 4
            stripe = 220 if (x in (7, 8)) else (55 + ((x * 13 + y * 7) & 31))
            road_px[i : i + 4] = bytes([stripe, stripe, min(255, stripe + 5), 255])
            is_bar = (x % 4 == 0) or (y % 4 == 0)
            fence_px[i : i + 4] = bytes([70, 140, 65, 255] if is_bar else [0, 0, 0, 0])

    tex_blob = bytes(road_px) + bytes(fence_px)
    tex_records = [
        struct.pack("<4IQ2I", 0x11111111, w, h, 0, 0, len(road_px), 2),
        struct.pack("<4IQ2I", 0x22222222, w, h, 0, len(road_px), len(fence_px), 3),
    ]
    materials = [
        # mat 0: ROAD_ASPHALT (surfaceClass=1, flags=DRIVABLE_ROAD|WET_REFLECTIVE)
        struct.pack("<3I4fI", 0, 1, 0x14, 0.45, 0.0, 0.0, 0.35, 0x11111111),
        # mat 1: CONCRETE_WALL (surfaceClass=0)
        struct.pack("<3I4fI", 0xFFFFFFFF, 0, 0x00, 0.78, 0.0, 0.0, 0.25, 0),
        # mat 2: FOLIAGE_CUTOUT (surfaceClass=4, flags=ALPHA_CUTOUT|DOUBLE_SIDED)
        struct.pack("<3I4fI", 1, 4, 0x03, 0.72, 0.0, 0.0, 0.20, 0x22222222),
    ]

    verts = bytearray()
    indices: list[int] = []

    def add_quad(pts, nrm, mat_idx):
        base = len(verts) // 36
        uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        for p, uv in zip(pts, uvs):
            verts.extend(struct.pack("<3f3f2fHBB", p[0], p[1], p[2], nrm[0], nrm[1], nrm[2], uv[0], uv[1], mat_idx, 255, 0))
        indices.extend([base, base + 1, base + 2, base, base + 2, base + 3])

    # Mesh 0: Drivable road slab at Y = 2.0m (tests GPU BVH physics snap from spawnPos.y=3.5 -> 2.0!)
    v0_off, i0_off = 0, 0
    add_quad([(-12.0, 2.0, -80.0), (12.0, 2.0, -80.0), (12.0, 2.0, 200.0), (-12.0, 2.0, 200.0)], (0.0, 1.0, 0.0), 0)
    m0_vc, m0_ic = (len(verts) // 36) - v0_off, len(indices) - i0_off

    # Mesh 1: Solid wall along X = +10m
    v1_off, i1_off = len(verts) // 36, len(indices)
    add_quad([(10.0, 2.0, -80.0), (10.0, 2.0, 200.0), (10.0, 10.0, 200.0), (10.0, 10.0, -80.0)], (-1.0, 0.0, 0.0), 1)
    m1_vc, m1_ic = (len(verts) // 36) - v1_off, len(indices) - i1_off

    # Mesh 2: Alpha-cutout foliage/fence panel along X = -8m
    v2_off, i2_off = len(verts) // 36, len(indices)
    add_quad([(-8.0, 2.0, -20.0), (-8.0, 2.0, 60.0), (-8.0, 7.0, 60.0), (-8.0, 7.0, -20.0)], (1.0, 0.0, 0.0), 2)
    m2_vc, m2_ic = (len(verts) // 36) - v2_off, len(indices) - i2_off

    meshes = [
        struct.pack("<6I3f3f", 0xA001, v0_off, m0_vc, i0_off, m0_ic, 0x02, -12.0, 2.0, -80.0, 12.0, 2.0, 200.0),
        struct.pack("<6I3f3f", 0xA002, v1_off, m1_vc, i1_off, m1_ic, 0x04, 10.0, 2.0, -80.0, 10.0, 10.0, 200.0),
        struct.pack("<6I3f3f", 0xA003, v2_off, m2_vc, i2_off, m2_ic, 0x01, -8.0, 2.0, -20.0, -8.0, 7.0, 60.0),
    ]
    ident_4x3 = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    instances = [
        struct.pack("<4I12f", 0, 0, 0, 0, *ident_4x3),
        struct.pack("<4I12f", 1, 0, 0, 0, *ident_4x3),
        struct.pack("<4I12f", 2, 0, 0, 0, *ident_4x3),
    ]
    sections = [
        struct.pack("<8s3I3ff3f3fI", b"MINI_01\x00", 1, 0, len(instances), 0.0, 2.0, 60.0, 160.0, -12.0, 2.0, -80.0, 12.0, 10.0, 200.0, 0)
    ]

    sec_bytes = b"".join(sections)
    mesh_bytes = b"".join(meshes)
    inst_bytes = b"".join(instances)
    mat_bytes = b"".join(materials)
    tex_rec_bytes = b"".join(tex_records)
    idx_bytes = struct.pack(f"<{len(indices)}I", *indices)

    off_sec = 160
    off_mesh = off_sec + len(sec_bytes)
    off_inst = off_mesh + len(mesh_bytes)
    off_mat = off_inst + len(inst_bytes)
    off_tex = off_mat + len(mat_bytes)
    off_vert = off_tex + len(tex_rec_bytes)
    off_idx = off_vert + len(verts)
    off_texdata = off_idx + len(idx_bytes)

    header = struct.pack(
        "<4s9I3f3f3ff8Q3fI",
        b"RKPT", 1, 0,
        len(sections), len(meshes), len(instances), len(materials), len(tex_records),
        len(verts) // 36, len(indices),
        -12.0, 2.0, -80.0,
        12.0, 10.0, 200.0,
        0.0, 3.5, 0.0, 0.0,
        off_sec, off_mesh, off_inst, off_mat, off_tex, off_vert, off_idx, off_texdata,
        0.0, 0.0, 0.0, 0,
    )
    assert len(header) == 160
    out_path.write_bytes(header + sec_bytes + mesh_bytes + inst_bytes + mat_bytes + tex_rec_bytes + bytes(verts) + idx_bytes + tex_blob)


if __name__ == "__main__":
    unittest.main()
