#!/usr/bin/env python3
"""Unit & integration tests for tools/extract_rt_car.py (40-Car On-Demand RT Garage)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import extract_rt_car as erc  # noqa: E402
import tpk_extract as te  # noqa: E402
from zdir_tool import bstringhash  # noqa: E402


class TestExtractRTCarOffline(unittest.TestCase):
    def test_catalog_has_exact_40_playable_cars_and_aliases(self) -> None:
        self.assertEqual(len(erc.PLAYABLE_CAR_CATALOG), 40)
        for expected in (
            "BMWM3GTRE46", "CARRERAGT", "GALLARDO", "CORVETTEC6R",
            "FORDGT", "RX7", "MURCIELAGO", "SLR", "VIPER", "SUPRA",
        ):
            self.assertIn(expected, erc.PLAYABLE_CAR_CATALOG)
        self.assertEqual(erc.canonicalize_car_name("m3_gtr"), "BMWM3GTRE46")
        self.assertEqual(erc.canonicalize_car_name("carrera_gt"), "CARRERAGT")
        self.assertEqual(erc.canonicalize_car_name("ford_gt"), "FORDGT")
        self.assertEqual(erc.canonicalize_car_name("c6r"), "CORVETTEC6R")
        with self.assertRaises(KeyError):
            erc.canonicalize_car_name("NONEXISTENT_SPACESHIP")

    def test_hash_matching_solves_23_char_texture_truncation(self) -> None:
        """EA truncates TextureInfo.name to 23 ASCII chars; bstringhash must distinguish ON vs OFF."""
        car = "CORVETTEC6R"
        fake_texs = [
            te.ExtractedTexture(
                archive=car, container_index=0, texture_index=0,
                name="CORVETTEC6R_KIT00_HEADL", name_hash=bstringhash("CORVETTEC6R_KIT00_HEADLIGHT_OFF"),
                width=64, height=64, ps2_psm=0, ps2_psm_name="PSMCT32", clut_psm=0,
                palette_entries=0, is_swizzled=False, image_size=0, palette_size=0, compression="RAW",
            ),
            te.ExtractedTexture(
                archive=car, container_index=0, texture_index=1,
                name="CORVETTEC6R_KIT00_HEADL", name_hash=bstringhash("CORVETTEC6R_KIT00_HEADLIGHT_ON"),
                width=64, height=64, ps2_psm=0, ps2_psm_name="PSMCT32", clut_psm=0,
                palette_entries=0, is_swizzled=False, image_size=0, palette_size=0, compression="RAW",
            ),
            te.ExtractedTexture(
                archive=car, container_index=0, texture_index=2,
                name="CORVETTEC6R_KIT00_BRAKE", name_hash=bstringhash("CORVETTEC6R_KIT00_BRAKELIGHT_ON"),
                width=64, height=64, ps2_psm=0, ps2_psm_name="PSMCT32", clut_psm=0,
                palette_entries=0, is_swizzled=False, image_size=0, palette_size=0, compression="RAW",
            ),
            te.ExtractedTexture(
                archive=car, container_index=0, texture_index=3,
                name="CORVETTEC6R_MISC", name_hash=bstringhash("CORVETTEC6R_MISC"),
                width=64, height=64, ps2_psm=0, ps2_psm_name="PSMCT32", clut_psm=0,
                palette_entries=0, is_swizzled=False, image_size=0, palette_size=0, compression="RAW",
            ),
        ]
        matched = erc.match_car_textures(car, fake_texs)
        self.assertEqual(matched["car_headlight"].name_hash, bstringhash("CORVETTEC6R_KIT00_HEADLIGHT_ON"))
        self.assertEqual(matched["car_taillight"].name_hash, bstringhash("CORVETTEC6R_KIT00_BRAKELIGHT_ON"))
        self.assertEqual(matched["car_misc"].name, "CORVETTEC6R_MISC")

    def test_synthetic_car_extraction_schema_and_wheel_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "FORDGT"
            m = erc.extract_synthetic_rt_car("ford_gt", out_dir)
            self.assertEqual(m["car_id"], "FORDGT")
            self.assertIn("wheel_bounds", m)
            self.assertGreater(m["wheel_bounds"]["front_axle_z"], 0.5)
            self.assertLess(m["wheel_bounds"]["rear_axle_z"], -0.5)
            self.assertTrue((out_dir / "scene.json").is_file())
            loaded = json.loads((out_dir / "scene.json").read_text(encoding="utf-8"))
            self.assertEqual(len(loaded["parts"]), 5)
            for p in loaded["parts"]:
                self.assertTrue((out_dir / p["obj"]).is_file())


class TestExtractRTCarLiveISO(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.iso_path = erc.resolve_iso_path()

    def test_verify_all_40_cars_in_iso(self) -> None:
        if self.iso_path is None:
            self.skipTest("PS2 ISO not present in environment")
        rep = erc.verify_all_40_manifest(self.iso_path)
        self.assertEqual(rep["mode"], "iso_verified")
        self.assertEqual(rep["verified_in_iso"], 40)
        self.assertEqual(rep["missing"], [])
        for row in rep["cars"]:
            self.assertTrue(row["has_body_b"], f"Missing BODY_B for {row['car']}")
            self.assertGreaterEqual(len(row["matched_slots"]), 4, f"Too few texture slots on {row['car']}")

    def test_extract_flagship_cars_end_to_end(self) -> None:
        if self.iso_path is None:
            self.skipTest("PS2 ISO not present in environment")
        from zdir_tool import PS2NFSMWArchive

        arc = PS2NFSMWArchive(self.iso_path)
        mlook = erc._load_light_material_lookup(arc)
        with tempfile.TemporaryDirectory() as td:
            for car in ("BMWM3GTRE46", "CARRERAGT", "GALLARDO", "CORVETTEC6R", "FORDGT", "RX7", "MURCIELAGO"):
                out_dir = Path(td) / car
                m = erc.extract_rt_car(car, archive=arc, mat_lookup=mlook, out_dir=out_dir, subdivide=True)
                self.assertEqual(m["car_id"], car)
                self.assertGreater(m["summary"]["total_triangles"], 8000, f"Low tri count for {car}")
                wb = m["wheel_bounds"]
                self.assertGreater(wb["front_axle_z"] - wb["rear_axle_z"], 2.0)
                self.assertGreater(wb["wheel_radius_f"], 0.28)
                self.assertTrue((out_dir / "scene.json").is_file())


if __name__ == "__main__":
    unittest.main()
