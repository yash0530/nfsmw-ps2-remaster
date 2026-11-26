#!/usr/bin/env python3
"""tests/test_car_fleet.py — Offline & Read-Only Live ISO Tests for tools/upgrade_car_fleet.py.

Tests:
  1. Offline `--synthetic` batch PS2 car fleet upgrade + Gnsu synth tuning + manifest schema
     + PS2 EE/VU1 budget validation + geometry_inspect 0-error verification.
  2. Offline `--synthetic --export-hq-rt` Y-up Metal 4 RT scene export (L2 body + L1 wheels,
     tires resting at Y=0, export_pbr_scene.py scene.json compatibility).
  3. Graceful handling of missing car names and `NFSMW_ISO=/nonexistent` + path hygiene.
  4. Live read-only ISO tests (skipped cleanly when `NFSMW_ISO=/nonexistent` or ISO absent):
     - Batch fleet upgrade + geometry_inspect validation across iconic cars
     - 58K-poly BMW M3 GTR HQ RT export (58,240 body tris, 2,500 tris/wheel, tires at Y=0)
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
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audio_inspect  # noqa: E402
import geometry_inspect as gi  # noqa: E402
import upgrade_car_fleet as ucf  # noqa: E402


def _get_live_iso_or_none() -> Optional[Path]:
    return ucf.resolve_iso_path(None)


class TestCarFleetOfflineSynthetic(unittest.TestCase):
    def test_synthetic_fleet_upgrade_and_manifest_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "geom_wave4"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ucf.main(["--synthetic", "--out-dir", str(out_dir)])
            self.assertEqual(rc, 0)

            manifest_path = out_dir / "fleet_manifest.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["mode"], "synthetic")
            self.assertAlmostEqual(manifest["crease_deg"], 38.0)
            self.assertEqual(manifest["budgets"]["max_solid_bytes"], 768 * 1024)
            self.assertEqual(manifest["budgets"]["max_polys"], 32767)
            self.assertEqual(manifest["budgets"]["max_pack_bytes"], 5 * 1024 * 1024)

            summary = manifest["summary"]
            self.assertEqual(summary["cars_upgraded"], 1)
            self.assertEqual(summary["audio_synths_tuned"], 1)
            self.assertEqual(summary["total_overrides"], 2)

            overrides = manifest["overrides"]
            self.assertIn(r"CARS\SYNTHCAR\GEOMETRY.BIN", overrides)
            self.assertIn(r"SOUND\ENGINE\SYNTH_CAR.GIN", overrides)
            for vpath, fpath in overrides.items():
                resolved = Path(fpath) if Path(fpath).is_absolute() else (REPO_ROOT / fpath)
                self.assertTrue(resolved.is_file(), f"Missing override file for {vpath}: {resolved}")

            car_entry = manifest["cars"][0]
            self.assertEqual(car_entry["status"], "upgraded")
            self.assertEqual(car_entry["parse_errors"], 0)
            self.assertEqual(car_entry["upgraded_triangles"], car_entry["orig_triangles"] * 4)
            self.assertLessEqual(car_entry["solid_bytes"], 768 * 1024)
            self.assertLessEqual(car_entry["upgraded_triangles"], 32767)
            self.assertLessEqual(car_entry["upgraded_pack_bytes"], 5 * 1024 * 1024)

            # Validate repacked binary directly with geometry_inspect
            bin_path = Path(car_entry["upgraded_file"])
            if not bin_path.is_absolute():
                bin_path = REPO_ROOT / bin_path
            pack_info, meshes = gi.parse_geometry_pack(bin_path.read_bytes(), decode_parts=[0, 1])
            self.assertEqual(pack_info.solid_count, 2)
            self.assertEqual(pack_info.parts[0].strip_poly_total, car_entry["upgraded_triangles"])
            self.assertEqual(pack_info.parts[0].header_polys, car_entry["upgraded_triangles"])
            self.assertEqual(len(meshes[0].triangles), car_entry["upgraded_triangles"])

            # Validate tuned synthetic Gnsu synth
            gin_entry = manifest["audio"][0]
            self.assertEqual(gin_entry["status"], "tuned")
            gin_path = Path(gin_entry["upgraded_file"])
            if not gin_path.is_absolute():
                gin_path = REPO_ROOT / gin_path
            insp = audio_inspect.inspect_standalone_file(gin_path)
            self.assertEqual(len(insp["gnsu_synths"]), 1)
            self.assertAlmostEqual(insp["gnsu_synths"][0].max_rpm, 8350.0, places=1)

            # Validate VU1 hardware invariants in manifest summary & car entry
            vu1_sum = summary["vu1_verification"]
            self.assertEqual(vu1_sum["bad_tag"], 0)
            self.assertEqual(vu1_sum["bad_b2"], 0)
            self.assertEqual(vu1_sum["bad_b3"], 0)
            self.assertEqual(vu1_sum["bad_q"], 0)
            self.assertEqual(vu1_sum["parse_errors"], 0)
            self.assertLessEqual(vu1_sum["max_vu1_aabb_err"], 0.0002)

    def test_synthetic_include_kits_and_prefer_lod_a(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "geom_wave5_synth"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ucf.main(
                    [
                        "--synthetic",
                        "--include-kits",
                        "--prefer-lod-a",
                        "--no-audio",
                        "--out-dir",
                        str(out_dir),
                    ]
                )
            self.assertEqual(rc, 0)
            manifest = json.loads((out_dir / "fleet_manifest.json").read_text(encoding="utf-8"))
            summary = manifest["summary"]
            self.assertEqual(summary["cars_upgraded"], 1)
            self.assertEqual(summary["total_solids_upgraded"], 3)  # KIT00_BODY_B + KIT01_BODY_B + FRONT_TIRE_B
            car_entry = manifest["cars"][0]
            self.assertEqual(car_entry["solids_upgraded"], 3)
            upgraded_names = {s["solid_name"] for s in car_entry["solids"]}
            self.assertEqual(
                upgraded_names,
                {"SYNTHCAR_KIT00_BODY_B", "SYNTHCAR_KIT01_BODY_B", "SYNTHCAR_KIT00_FRONT_TIRE_B"},
            )
            vu1 = car_entry["vu1_verification"]
            self.assertEqual((vu1["bad_tag"], vu1["bad_b2"], vu1["bad_b3"], vu1["bad_q"]), (0, 0, 0, 0))
            self.assertEqual(vu1["max_vu1_strip_overshoot"], 0.0)
            self.assertLessEqual(vu1["max_vu1_aabb_err"], 0.0002)

    def test_synthetic_hq_rt_export_schema_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "geom_wave4"
            hq_dir = Path(td) / "hq_m3_gtr"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ucf.main(
                    [
                        "--synthetic",
                        "--skip-fleet",
                        "--out-dir",
                        str(out_dir),
                        "--export-hq-rt",
                        "--hq-out",
                        str(hq_dir),
                    ]
                )
            self.assertEqual(rc, 0)

            scene_path = hq_dir / "scene.json"
            self.assertTrue(scene_path.is_file())
            scene = json.loads(scene_path.read_text(encoding="utf-8"))

            self.assertEqual(scene["schema_version"], 1)
            self.assertEqual(scene["coordinate_system"], "yup_right_handed_meters")
            self.assertEqual(scene["mode"], "synthetic_hq_rt")
            for key in ("camera", "textures", "materials", "lighting_presets", "parts", "summary"):
                self.assertIn(key, scene)

            hq_sub = scene["summary"]["hq_subdivision"]
            self.assertEqual(hq_sub["body_levels"], 2)
            self.assertEqual(hq_sub["wheel_levels"], 1)
            self.assertEqual(hq_sub["body_hq_triangles"], hq_sub["body_orig_triangles"] * 16)
            self.assertEqual(hq_sub["wheel_hq_triangles_each"], hq_sub["wheel_orig_triangles"] * 4)

            wheel_ys = []
            for part in scene["parts"]:
                obj_file = hq_dir / part["obj"]
                self.assertTrue(obj_file.is_file(), f"Missing OBJ {obj_file}")
                txt = obj_file.read_text(encoding="utf-8")
                self.assertIn("usemtl ", txt)
                self.assertIn("vn ", txt)
                if part["role"] == "wheel":
                    for line in txt.splitlines():
                        if line.startswith("v "):
                            wheel_ys.append(float(line.split()[2]))

            self.assertTrue(wheel_ys)
            self.assertAlmostEqual(min(wheel_ys), 0.0, delta=1e-4)

    def test_budget_skip_and_missing_iso_and_hygiene(self) -> None:
        # 1. Verify NFSMW_ISO=/nonexistent returns None cleanly
        old_env = os.environ.get("NFSMW_ISO")
        try:
            os.environ["NFSMW_ISO"] = "/nonexistent/does_not_exist.iso"
            self.assertIsNone(ucf.resolve_iso_path(None))
            with self.assertRaises(FileNotFoundError):
                ucf.main(["--out-dir", "/tmp/unused"])
        finally:
            if old_env is None:
                os.environ.pop("NFSMW_ISO", None)
            else:
                os.environ["NFSMW_ISO"] = old_env

        # 2. Verify no hardcoded personal home paths in owned tracked files
        for path in (TOOLS_DIR / "upgrade_car_fleet.py", Path(__file__)):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"/Users/[A-Za-z0-9_.-]+/", f"Personal path leaked in {path}")


class TestCarFleetLiveISO(unittest.TestCase):
    def test_live_iso_fleet_upgrade_and_hq_58k_rt_export(self) -> None:
        iso_path = _get_live_iso_or_none()
        if iso_path is None:
            self.skipTest("SLUS-21351 ISO not available locally (NFSMW_ISO unset/nonexistent)")

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "geom_wave4"
            hq_dir = Path(td) / "hq_m3_gtr"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ucf.main(
                    [
                        "--iso",
                        str(iso_path),
                        "--out-dir",
                        str(out_dir),
                        "--cars",
                        "BMWM3GTRE46",
                        "CARRERAGT",
                        "NONEXISTENTCAR",
                        "--export-hq-rt",
                        "--hq-out",
                        str(hq_dir),
                    ]
                )
            self.assertEqual(rc, 0)

            manifest = json.loads((out_dir / "fleet_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["mode"], "live_iso")
            self.assertEqual(manifest["summary"]["cars_upgraded"], 2)
            self.assertEqual(manifest["summary"]["cars_missing"], 1)
            self.assertEqual(manifest["summary"]["audio_synths_tuned"], 4)

            by_car = {c["car"]: c for c in manifest["cars"]}
            self.assertEqual(by_car["NONEXISTENTCAR"]["status"], "missing_in_zdir")
            self.assertEqual(by_car["BMWM3GTRE46"]["upgraded_triangles"], 14560)
            self.assertEqual(by_car["CARRERAGT"]["upgraded_triangles"], 11312)

            for car_key in ("BMWM3GTRE46", "CARRERAGT"):
                c = by_car[car_key]
                bin_path = Path(c["upgraded_file"])
                if not bin_path.is_absolute():
                    bin_path = REPO_ROOT / bin_path
                pinfo, decoded = gi.parse_geometry_pack(
                    bin_path.read_bytes(), decode_parts=[c["solid_index"]]
                )
                self.assertEqual(pinfo.solid_count, c["solid_count"])
                self.assertEqual(len(decoded[c["solid_index"]].triangles), c["upgraded_triangles"])

            # Verify 58K-poly HQ RT scene
            scene = json.loads((hq_dir / "scene.json").read_text(encoding="utf-8"))
            hq_sub = scene["summary"]["hq_subdivision"]
            self.assertEqual(hq_sub["body_orig_triangles"], 3640)
            self.assertEqual(hq_sub["body_hq_triangles"], 58240)
            self.assertEqual(hq_sub["wheel_orig_triangles"], 625)
            self.assertEqual(hq_sub["wheel_hq_triangles_each"], 2500)

            wheel_ys = []
            for part in scene["parts"]:
                obj_file = hq_dir / part["obj"]
                self.assertTrue(obj_file.is_file())
                if part["role"] == "wheel":
                    for line in obj_file.read_text(encoding="utf-8").splitlines():
                        if line.startswith("v "):
                            wheel_ys.append(float(line.split()[2]))
            self.assertAlmostEqual(min(wheel_ys), 0.0, delta=1e-4)

    def test_live_iso_gti_widebody_kits_and_copmidsize_lod_a(self) -> None:
        iso_path = _get_live_iso_or_none()
        if iso_path is None:
            self.skipTest("SLUS-21351 ISO not available locally (NFSMW_ISO unset/nonexistent)")

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "geom_wave5_sample"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ucf.main(
                    [
                        "--iso",
                        str(iso_path),
                        "--out-dir",
                        str(out_dir),
                        "--cars",
                        "GTI",
                        "COPMIDSIZE",
                        "TRAF4DSEDA",
                        "--include-kits",
                        "--prefer-lod-a",
                        "--no-audio",
                    ]
                )
            self.assertEqual(rc, 0)
            manifest = json.loads((out_dir / "fleet_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["cars_upgraded"], 3)
            vu1 = manifest["summary"]["vu1_verification"]
            self.assertEqual((vu1["bad_tag"], vu1["bad_b2"], vu1["bad_b3"], vu1["bad_q"]), (0, 0, 0, 0))
            self.assertEqual(vu1["max_vu1_aabb_err"], 0.0)

            by_car = {c["car"]: c for c in manifest["cars"]}
            # VW Golf GTI: all 5 body kits KIT00..KIT04 + tire + hoods upgraded under 5 MiB
            gti_names = {s["solid_name"] for s in by_car["GTI"]["solids"]}
            for kit in ("KIT00", "KIT01", "KIT02", "KIT03", "KIT04"):
                self.assertIn(f"GTI_{kit}_BODY_B", gti_names)
            self.assertIn("GTI_KIT00_FRONT_TIRE_B", gti_names)
            self.assertLessEqual(by_car["GTI"]["upgraded_pack_bytes"], 5 * 1024 * 1024)

            # COPMIDSIZE: LOD A primary bodies upgraded
            cop_names = {s["solid_name"] for s in by_car["COPMIDSIZE"]["solids"]}
            self.assertIn("COPMIDSIZE_KIT00_DAMAGE0_BODY_A", cop_names)
            self.assertIn("COPMIDSIZE_KIT00_DAMAGE1_BODY_A", cop_names)

            # TRAF4DSEDA: LOD C traffic body upgraded
            traf_names = {s["solid_name"] for s in by_car["TRAF4DSEDA"]["solids"]}
            self.assertIn("TRAF4DSEDA_KIT00_BODY_C", traf_names)

            # Verify --all-cars discovery excludes GLOBALMEMORYFILE.BIN-cached part packs by default
            from zdir_tool import PS2NFSMWArchive
            arc = PS2NFSMWArchive(iso_path)
            discovered = ucf.discover_zdir_car_packs(arc, include_parts=False)
            self.assertEqual(len(discovered), 84)
            for cached_pack in ucf.GLOBALMEMORYFILE_CACHED_PART_PACKS:
                self.assertNotIn(cached_pack, discovered)


if __name__ == "__main__":
    unittest.main()
