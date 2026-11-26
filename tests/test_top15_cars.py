#!/usr/bin/env python3
"""Offline & live ISO verification suite for the Top-15 Flagship & Blacklist RT Cars."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
NATIVE_RT_DIR = REPO_ROOT / "native_metal_rt"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import extract_rt_car as erc  # noqa: E402

EXPECTED_TOP15 = (
    "BMWM3GTRE46",
    "CARRERAGT",
    "GALLARDO",
    "CORVETTEC6R",
    "FORDGT",
    "RX7",
    "SLR",
    "DB9",
    "VIPER",
    "CORVETTE",
    "CLK500",
    "MUSTANGGT",
    "LANCEREVO8",
    "CAYMANS",
    "SUPRA",
)


class TestTop15CarsOffline(unittest.TestCase):
    """Offline structural checks (safe under NFSMW_ISO=/nonexistent)."""

    def test_top15_roster_in_extract_catalog_and_swift_catalog(self) -> None:
        self.assertEqual(tuple(erc.TOP15_FLAGSHIP_CARS), EXPECTED_TOP15)
        scene_src = (NATIVE_RT_DIR / "RTSceneBuilder.swift").read_text(encoding="utf-8")
        for car in EXPECTED_TOP15:
            self.assertIn(car, erc.PLAYABLE_CAR_CATALOG, f"{car} missing from extract_rt_car catalog")
            self.assertIn(f'canonicalName: "{car}"', scene_src, f"{car} missing from RTSceneBuilder RTCarCatalog")

    def test_scene_builder_prefers_car_cache_over_legacy_flywheel(self) -> None:
        scene_src = (NATIVE_RT_DIR / "RTSceneBuilder.swift").read_text(encoding="utf-8")
        fn_start = scene_src.index("static func ensureCachedScenePath")
        fn_end = scene_src.index("enum RTCarProfile", fn_start)
        fn_body = scene_src[fn_start:fn_end]

        idx_cache = fn_body.index("if fm.fileExists(atPath: cacheScene)")
        for legacy in (
            "build/flywheel/hq_m3_gtr/scene.json",
            "build/flywheel/hq_carrera_gt/scene.json",
            "build/flywheel/hq_gallardo/scene.json",
        ):
            self.assertIn(legacy, fn_body, f"Fallback literal {legacy} must remain intact")
            self.assertLess(
                idx_cache,
                fn_body.index(legacy),
                f"build/car_cache/<CAR>/scene.json must be preferred before legacy {legacy}",
            )

    def test_synthetic_top15_representative_cars(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for car in ("CARRERAGT", "RX7", "DB9"):
                out = Path(td) / car
                m = erc.extract_synthetic_rt_car(car, out)
                self.assertEqual(m["car_id"], car)
                self.assertEqual(m["wheel_bounds"]["ground_y"], 0.0)
                self.assertTrue((out / "scene.json").is_file())


class TestTop15CarsLiveCache(unittest.TestCase):
    """Live ISO cache validation for all 15/15 flagship & Blacklist cars."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.iso_path = erc.resolve_iso_path()

    def _require_live_iso(self) -> None:
        if self.iso_path is None:
            self.skipTest("PS2 ISO not present in environment")

    def test_all_top15_cached_with_valid_meshes_grounding_lights_and_textures(self) -> None:
        self._require_live_iso()
        cache_root = erc.DEFAULT_CACHE_ROOT
        for car in EXPECTED_TOP15:
            car_dir = cache_root / car
            scene_path = car_dir / "scene.json"
            self.assertTrue(scene_path.is_file(), f"Missing cached scene.json for {car} at {scene_path}")

            manifest = json.loads(scene_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest.get("car_id"), car)
            parts = manifest.get("parts", [])
            self.assertGreaterEqual(len(parts), 12, f"Too few parts for {car}")

            # 1. Validate wheel bounds
            wb = manifest.get("wheel_bounds", {})
            self.assertAlmostEqual(float(wb.get("ground_y", -1.0)), 0.0, delta=0.005)
            wheelbase = float(wb.get("front_axle_z", 0.0)) - float(wb.get("rear_axle_z", 0.0))
            self.assertGreaterEqual(wheelbase, 2.05, f"Invalid wheelbase {wheelbase} for {car}")
            self.assertLessEqual(wheelbase, 3.35, f"Invalid wheelbase {wheelbase} for {car}")
            self.assertGreater(float(wb.get("wheel_radius_f", 0.0)), 0.28, f"Small front radius on {car}")
            self.assertGreater(float(wb.get("wheel_radius_r", 0.0)), 0.28, f"Small rear radius on {car}")
            self.assertGreater(float(wb.get("half_track_f", 0.0)), 0.60, f"Invalid half_track_f on {car}")
            self.assertGreater(float(wb.get("half_track_r", 0.0)), 0.60, f"Invalid half_track_r on {car}")

            # 2. Validate OBJ files, tire grounding (|y_min| < 0.02m), and lights
            wheel_min_y = 999.0
            wheel_count = 0
            hl_tris = 0
            tl_tris = 0
            body_tris = 0
            for part in parts:
                obj_path = car_dir / part["obj"]
                self.assertTrue(obj_path.is_file(), f"Missing OBJ {obj_path}")
                mats = part.get("materials", {})
                hl_tris += int(mats.get("headlight_emissive", 0)) + int(mats.get("headlight_glass", 0))
                tl_tris += int(mats.get("taillight_emissive", 0))
                if "BODY" in part["name"].upper():
                    body_tris += int(part.get("triangle_count", 0))
                if part.get("role") == "wheel":
                    wheel_count += 1
                    ys = [
                        float(line.split()[2])
                        for line in obj_path.read_text(encoding="utf-8").splitlines()
                        if line.startswith("v ")
                    ]
                    self.assertTrue(ys, f"Empty wheel OBJ {obj_path}")
                    wheel_min_y = min(wheel_min_y, min(ys))

            self.assertEqual(wheel_count, 4, f"Expected 4 wheels on {car}, found {wheel_count}")
            self.assertLess(abs(wheel_min_y), 0.02, f"Wheels not grounded on {car}: y_min={wheel_min_y:.4f}")
            self.assertGreater(body_tris, 5000, f"Too few body triangles on {car}: {body_tris}")
            self.assertGreater(tl_tris, 0, f"Missing taillight geometry on {car}")
            if car not in erc.POPUP_HEADLIGHT_CARS:
                self.assertGreater(hl_tris, 0, f"Missing headlight geometry on {car}")

            # 3. Validate all referenced car textures are non-corrupt Pillow-decodable PNGs
            textures = manifest.get("textures", {})
            self.assertIn("car_taillight", textures, f"Missing car_taillight texture on {car}")
            self.assertIn("car_misc", textures, f"Missing car_misc texture on {car}")
            if car not in erc.POPUP_HEADLIGHT_CARS:
                self.assertIn("car_headlight", textures, f"Missing car_headlight texture on {car}")
            for slot, rel_path in textures.items():
                tex_path = Path(rel_path)
                if not tex_path.is_absolute():
                    tex_path = REPO_ROOT / tex_path
                if not tex_path.is_file():
                    # Optional shared CC0 textures live in build/cc0_baked
                    self.fail(f"Missing texture {slot} -> {tex_path} for {car}")
                self.assertGreater(tex_path.stat().st_size, 100, f"Empty texture {tex_path}")
                with Image.open(tex_path) as im:
                    im.verify()
                with Image.open(tex_path) as im:
                    im.load()
                    w, h = im.size
                    self.assertGreaterEqual(w, 32, f"Tiny texture width on {tex_path}")
                    self.assertGreaterEqual(h, 32, f"Tiny texture height on {tex_path}")

    def test_carrera_gt_seam_and_mirror_cure_properties(self) -> None:
        self._require_live_iso()
        cgt_dir = erc.DEFAULT_CACHE_ROOT / "CARRERAGT"
        manifest = json.loads((cgt_dir / "scene.json").read_text(encoding="utf-8"))
        parts_by_name = {p["name"]: p for p in manifest["parts"]}

        body_part = parts_by_name["CARRERAGT_KIT00_BODY_B"]
        self.assertNotIn(
            "car_paint_silver",
            body_part["materials"],
            "CARRERAGT BODY_B must unify DIABLOHP into car_paint_hero (zero fender-door material split)",
        )
        mirror_part = parts_by_name["CARRERAGT_KIT00_LEFT_SIDE_MIRROR_B"]
        self.assertGreaterEqual(int(mirror_part["triangle_count"]), 1000)

        # Verify smooth vertical normal gradient across the left front fender behind the front wheel
        vs, ns = [], []
        for line in (cgt_dir / body_part["obj"]).read_text(encoding="utf-8").splitlines():
            if line.startswith("v "):
                vs.append(tuple(map(float, line.split()[1:4])))
            elif line.startswith("vn "):
                ns.append(tuple(map(float, line.split()[1:4])))
        fender_nys = [
            n[1]
            for v, n in zip(vs, ns)
            if v[0] < -0.85 and 0.35 <= v[1] <= 0.68 and 0.68 <= v[2] <= 0.86
        ]
        self.assertTrue(fender_nys, "No front-fender vertices found on CARRERAGT")
        self.assertGreater(
            max(fender_nys) - min(fender_nys),
            0.18,
            "CARRERAGT front fender normals must exhibit smooth vertical barrel curvature (not a flat sheet)",
        )


if __name__ == "__main__":
    unittest.main()
