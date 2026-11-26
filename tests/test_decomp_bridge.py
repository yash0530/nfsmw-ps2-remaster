#!/usr/bin/env python3
"""Unit tests for tools/decomp_bridge.py (Path 2 C++20/Metal bridge & PBR manifest)."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import decomp_bridge as db  # noqa: E402


class TestDecompBridge(unittest.TestCase):
    """Validate deterministic header/manifest generation, schema, and C++20/C11 syntax."""

    def test_builtin_materials_count_and_hashes(self) -> None:
        self.assertEqual(len(db.STOCK_PS2_LIGHT_MATERIALS), 156)
        names = [row[0] for row in db.STOCK_PS2_LIGHT_MATERIALS]
        self.assertEqual(len(set(names)), 156)

        known_hashes = {
            "ROAD": 0x001C3745,
            "METPAINTBLUE": 0xC2462169,
            "CHROME": 0x54949AFD,
            "WINDSHIELD": 0x471A1DCA,
            "BRAKELIGHT": 0x05BC3A3C,
        }
        for mat_name, expected in known_hashes.items():
            self.assertEqual(db.bstringhash(mat_name), expected, msg=mat_name)

    def test_offline_export_deterministic_without_symbols(self) -> None:
        patches_dir = REPO_ROOT / "patches"
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            out1 = Path(td1)
            out2 = Path(td2)
            s1 = db.generate_bridge(patches_dir, out1, symbols_path=None)
            s2 = db.generate_bridge(patches_dir, out2, symbols_path=Path("/nonexistent/symbols.txt"))

            self.assertEqual(s1["hooks_resolved_from_file"], 0)
            self.assertEqual(s2["hooks_resolved_from_file"], 0)
            self.assertEqual(s1["symbols_source"], "builtin_verified_fallback")
            self.assertEqual(s2["symbols_source"], "builtin_verified_fallback")

            for fname in ("Rockport2026Config.hpp", "Rockport2026Lighting.h", "bridge_manifest.json"):
                b1 = (out1 / fname).read_bytes()
                b2 = (out2 / fname).read_bytes()
                self.assertEqual(b1, b2, msg=f"{fname} must be deterministic across offline runs")

    def test_symbol_file_resolution_matches_builtin(self) -> None:
        patches_dir = REPO_ROOT / "patches"
        with tempfile.TemporaryDirectory() as td:
            sym_file = Path(td) / "symbols_test.txt"
            lines = [
                f"{mangled} = 0x{vaddr:x}; // type:func"
                for _, mangled, vaddr, _, _ in db.BUILTIN_SLUS21351_HOOKS
            ]
            sym_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            out_dir = Path(td) / "out"
            summary = db.generate_bridge(patches_dir, out_dir, symbols_path=sym_file)
            self.assertEqual(summary["hooks_count"], len(db.BUILTIN_SLUS21351_HOOKS))
            self.assertEqual(summary["hooks_resolved_from_file"], len(db.BUILTIN_SLUS21351_HOOKS))

            live_symbols = REPO_ROOT / "build" / "symbols_slus21351.txt"
            if live_symbols.is_file():
                live_out = Path(td) / "live_out"
                live_summary = db.generate_bridge(patches_dir, live_out, symbols_path=live_symbols)
                self.assertEqual(live_summary["hooks_resolved_from_file"], len(db.BUILTIN_SLUS21351_HOOKS))
                m_live = json.loads((live_out / "bridge_manifest.json").read_text(encoding="utf-8"))
                m_syn = json.loads((out_dir / "bridge_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(m_live["slus21351_hooks"], m_syn["slus21351_hooks"])

    def test_manifest_schema_and_pbr_physical_bounds(self) -> None:
        patches_dir = REPO_ROOT / "patches"
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            db.generate_bridge(patches_dir, out_dir, symbols_path=None)
            manifest = json.loads((out_dir / "bridge_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["target"]["serial"], "SLUS-21351")
            expected_total_vlt = sum(
                len(json.loads(p.read_text(encoding="utf-8")).get("patches", []))
                for p in (patches_dir / "vlt").glob("*.json")
            )
            self.assertGreaterEqual(manifest["total_vlt_deltas"], 360)
            self.assertEqual(manifest["total_vlt_deltas"], expected_total_vlt)
            self.assertEqual(manifest["light_materials_count"], 156)
            self.assertEqual(len(manifest["light_materials"]), 156)

            for preset_name in ("daylight", "golden_hour", "night_rain"):
                preset = manifest["lighting_presets"][preset_name]
                d = preset["sun_uniforms"]["sun_direction_intensity"]
                norm = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)
                self.assertAlmostEqual(norm, 1.0, places=4, msg=f"{preset_name} sun_dir not unit")
                self.assertGreater(d[3], 0.0)

            by_name = {m["name"]: m for m in manifest["light_materials"]}
            self.assertEqual(by_name["ROAD"]["daylight_overrides"]["DiffuseMaxRGB"], [0.96, 0.98, 1.02])
            self.assertGreater(by_name["BRAKELIGHT"]["pbr"]["emissive"][0], 1.0)
            self.assertGreater(by_name["HEADLIGHT"]["pbr"]["emissive"][2], 1.0)
            self.assertEqual(by_name["CHROME"]["pbr"]["metallic"], 1.0)
            self.assertEqual(by_name["METPAINTBLUE"]["pbr"]["clearcoat"], 1.0)

            for m in manifest["light_materials"]:
                p = m["pbr"]
                self.assertTrue(0.04 <= p["roughness"] <= 0.96, msg=m["name"])
                self.assertTrue(0.0 <= p["metallic"] <= 1.0, msg=m["name"])
                self.assertTrue(0.0 <= p["clearcoat"] <= 1.0, msg=m["name"])
                self.assertGreaterEqual(p["ior"], 1.0, msg=m["name"])
                self.assertTrue(all(v >= 0.0 for v in p["emissive"]), msg=m["name"])

    def test_cpp20_and_c11_syntax_compile(self) -> None:
        clangxx = shutil.which("clang++")
        clang = shutil.which("clang")
        if clangxx is None and clang is None:
            self.skipTest("clang++ / clang not installed in environment")

        patches_dir = REPO_ROOT / "patches"
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            db.generate_bridge(patches_dir, out_dir, symbols_path=None)

            if clangxx is not None:
                cpp_stub = out_dir / "verify_bridge.cpp"
                cpp_stub.write_text(
                    '\n'.join(
                        [
                            '#include "Rockport2026Config.hpp"',
                            '#include "Rockport2026Lighting.h"',
                            "",
                            "using namespace rockport2026::config;",
                            "",
                            "static_assert(kNumTotalVltDeltas == 360u);",
                            "static_assert(kCameraDeltas.size() == 18u);",
                            "static_assert(kHandlingDeltas.size() == 25u);",
                            "static_assert(kRosterHandlingDeltas.size() == 285u);",
                            'static_assert(find_hook_vaddr("SetCurrentSunInfo") == 0x001FADC0u);',
                            'static_assert(find_hook_vaddr("UpdateAllScreenEFX") == 0x003DE740u);',
                            'static_assert(find_hook_vaddr("eDisplayFrame") == 0x0020B2E8u);',
                            "static_assert(ROCKPORT_PBR_MATERIAL_COUNT == 156u);",
                            "static_assert(kRockportSunGoldenHour.sun_direction_intensity.x > 0.9f);",
                            "static_assert(kRockportPBRMaterials[17].metallic == 1.0f);",
                            "",
                            "int main() { return 0; }",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                res = subprocess.run(
                    [clangxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", str(cpp_stub)],
                    cwd=out_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, msg=f"clang++ failed:\n{res.stderr}")

            if clang is not None:
                c_stub = out_dir / "verify_lighting.c"
                c_stub.write_text(
                    '\n'.join(
                        [
                            '#include "Rockport2026Lighting.h"',
                            "_Static_assert(sizeof(RockportSunUniforms) == 80, \"sun size\");",
                            "_Static_assert(sizeof(RockportVisualLookUniforms) == 32, \"vl size\");",
                            "_Static_assert(sizeof(RockportPBRMaterial) == 64, \"mat size\");",
                            "int main(void) { return (int)kRockportPBRMaterials[0].pbr_category; }",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                res_c = subprocess.run(
                    [clang, "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", str(c_stub)],
                    cwd=out_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res_c.returncode, 0, msg=f"clang C11 failed:\n{res_c.stderr}")

    def test_cli_main_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc = db.main(["--no-symbols", "--out-dir", td, "--json"])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "Rockport2026Config.hpp").is_file())
            self.assertTrue((Path(td) / "Rockport2026Lighting.h").is_file())
            self.assertTrue((Path(td) / "bridge_manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
