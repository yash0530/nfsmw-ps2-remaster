#!/usr/bin/env python3
"""v2.8 tests: golden-hour 3D world exposure rebalance, clearcoat/envmap glow, 5K 4xRGSS profile, and 28-car roster.

Offline tests pass with ``NFSMW_ISO=/nonexistent`` (100% Linux + macOS CI safe); live ISO smoke tests
open the untouched retail SLUS-21351 ISO strictly read-only ('rb') and skip cleanly when absent.
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import bake_world_3d_lighting as b3d  # noqa: E402
import lighting_ps2 as lp  # noqa: E402
import optin_car_geometry as oc  # noqa: E402
import pcsx2_profile_2026 as prof  # noqa: E402
import rockport_cli as rcli  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

LIVE_ISO = oc.resolve_iso()
live = unittest.skipUnless(LIVE_ISO is not None, "NFSMW ISO not present; skipping live read-only test")
V28_NEW_CARS = ("GTI", "IS300", "A3", "ECLIPSEGT")


def _kv_map(ini: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in ini.splitlines():
        line = raw.strip()
        if "=" in line and not line.startswith(";") and not line.startswith("Enable"):
            k, v = (p.strip() for p in line.split("=", 1))
            out[k] = v
    return out


class TestV28ProfileAndLightingOffline(unittest.TestCase):
    def test_5k_rgss_preset_and_backwards_compat(self) -> None:
        ini = prof.build_v28_5k_rgss_profile()
        self.assertEqual(prof.validate_profile_ini(ini), [])
        kv = _kv_map(ini)
        expected = {
            "upscale_multiplier": "8", "TVShader": "6", "fxaa": "false",
            "CASMode": "2", "CASSharpness": "48", "ShadeBoost": "true",
            "ShadeBoost_Brightness": "54", "ShadeBoost_Contrast": "53",
            "ShadeBoost_Saturation": "55", "ShadeBoost_Gamma": "50",
            "PrecacheTextureReplacements": "false", "LoadTextureReplacementsAsync": "true",
        }
        for k, v in expected.items():
            self.assertEqual(kv.get(k), v, f"mismatch for {k}")
        self.assertIn("Crisp 3D Sun-Shadow Contrast and Grounded Damp Road Reflections (v2.6 Optional)", ini)
        self.assertNotIn("Warm Late-Autumn Lighting & Fog (Optional)", ini)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "SLUS-21351_0518D274.ini"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(prof.main(["--preset", "5k_rgss", "--out", str(out), "--json"]), 0)
            meta = json.loads(buf.getvalue())
            self.assertEqual((meta["preset"], meta["scale"], meta["rgss"], meta["valid"]), ("5k_rgss", 8, True, True))
            self.assertEqual(_kv_map(out.read_text(encoding="utf-8"))["PrecacheTextureReplacements"], "false")

        # v2.1 defaults remain untouched for backwards compatibility
        v21_kv = _kv_map(prof.build_v21_metal_profile())
        self.assertEqual((v21_kv["upscale_multiplier"], v21_kv["PrecacheTextureReplacements"], v21_kv["ShadeBoost_Brightness"]),
                         ("6", "true", "50"))

    def test_world_baker_golden_hour_floor_and_no_crushed_undersides(self) -> None:
        cfg = b3d.BakeConfig()
        self.assertTrue(0.62 <= cfg.min_factor <= 0.68, f"unexpected min_factor={cfg.min_factor}")
        self.assertLessEqual(cfg.underside_darken, 0.18)

        # Sunlit (+X), shaded (-X), and overpass underside (-Z) quads with neutral 140 gray + alpha 127
        pts = np.array([
            [10, 0, 10], [10, 10, 10], [10, 0, 20], [10, 10, 20],
            [0, 10, 10], [0, 0, 10], [0, 10, 20], [0, 0, 20],
            [20, 10, 5], [26, 10, 5], [20, 0, 5], [26, 0, 5],
        ], dtype=np.float32)
        tris = np.array([[0, 1, 2], [2, 1, 3], [4, 5, 6], [6, 5, 7], [8, 9, 10], [10, 9, 11]], dtype=np.int32)
        rgba = np.full((len(pts), 4), [140, 140, 140, 127], dtype=np.uint8)
        out, _ = b3d.compute_3d_vertex_colors(pts, tris, rgba, config=cfg, is_local_solid=True, aabb_min_z=-20.0)
        np.testing.assert_array_equal(out[:, 3], rgba[:, 3])

        luma = 0.299 * out[:, 0] + 0.587 * out[:, 1] + 0.114 * out[:, 2]
        sun_l, shd_l, und_l = float(luma[2]), float(luma[6]), float(luma[8])
        self.assertGreater(sun_l - 140.0, 14.0)
        self.assertTrue(-22.0 < shd_l - 140.0 < -6.0, f"shadow delta={shd_l - 140.0}")
        self.assertGreater(sun_l - shd_l, 25.0)
        self.assertGreater(int(out[2, 0]), int(out[2, 2]))  # warm sunlit gold
        self.assertGreater(int(out[6, 2]), int(out[6, 0]))  # cool slate shadow
        self.assertTrue(105.0 <= und_l <= 130.0, f"underside luma={und_l} (must shade cleanly without black crush)")

    def test_elf_pnach_and_globalb_golden_hour_clearcoat_tuning(self) -> None:
        elf_spec = json.loads((REPO_ROOT / "patches" / "elf_patches.json").read_text(encoding="utf-8"))
        patch = next(p for p in elf_spec["patches"] if p["id"] == "crisp_3d_depth_lighting_v26")
        self.assertEqual(len(patch["writes"]), 11)
        floats = {int(w["vaddr"], 16): struct.unpack("<f", struct.pack("<I", int(w["patched"], 16)))[0]
                  for w in patch["writes"]}
        self.assertAlmostEqual(floats[0x00547490], 8.80, places=4)
        self.assertAlmostEqual(floats[0x005474A0], 0.88, places=4)
        self.assertAlmostEqual(floats[0x005474A4], 0.905, places=3)
        self.assertAlmostEqual(floats[0x005474A8], 0.96, places=4)
        self.assertAlmostEqual(floats[0x005474B0], 2.15, places=4)
        self.assertAlmostEqual(floats[0x005A2C80], 0.58, places=4)
        self.assertAlmostEqual(floats[0x00547490] / floats[0x005474B0], 4.093, delta=0.05)

        pnach = (REPO_ROOT / "patches" / "pnach" / "SLUS-21351_0518D274.pnach").read_text(encoding="utf-8")
        for token in ("00547490,word,410CCCCD", "005474B0,word,4009999A", "005A2C80,word,3F147AE1"):
            self.assertIn(token, pnach)

        gb = json.loads((REPO_ROOT / "patches" / "lighting" / "rockport2026_3d_depth.json").read_text(encoding="utf-8"))
        sun_l0 = [p for p in gb["patches"] if p["section"] == "SunInfos" and p.get("index") == 0 and p.get("member") == "IntensityScale"]
        self.assertEqual(len(sun_l0), 3)
        self.assertTrue(all(float(p["new_value"]) >= 1.1 for p in sun_l0))
        met_env = [p for p in gb["patches"] if p.get("material", "").startswith("METPAINT") and p["field"] == "EnvmapMaxScale"]
        self.assertEqual(len(met_env), 8)
        self.assertTrue(all(float(p["new_value"]) >= 0.85 for p in met_env))
        ws_env = next(p for p in gb["patches"] if p.get("material") == "WINDSHIELD" and p["field"] == "EnvmapMaxScale")
        self.assertGreaterEqual(float(ws_env["new_value"]), 1.1)

    def test_28_car_roster_and_profile_overrides(self) -> None:
        self.assertEqual(len(oc.BLACKLIST_PRESET), 28)
        self.assertEqual(len(set(oc.BLACKLIST_PRESET)), 28)
        for car in V28_NEW_CARS:
            self.assertIn(car, oc.BLACKLIST_PRESET)
        prof_path = REPO_ROOT / "patches" / "profiles" / "rockport2026_3d_limit.json"
        info = rcli.validate_build_profile(prof_path, repo_root=REPO_ROOT)
        self.assertTrue(info["valid"], info["errors"])
        raw = json.loads(prof_path.read_text(encoding="utf-8"))
        vpaths = {ov["virtual_path"]: ov["file"] for ov in raw["zz_overrides"]}
        self.assertEqual(vpaths.get(r"TRACKS\STREAML2RA.BUN"), "build/world3d_v28/STREAML2RA.BUN")
        for car in oc.BLACKLIST_PRESET:
            self.assertEqual(vpaths.get(f"CARS\\{car}\\GEOMETRY.BIN"), f"build/optin_cars_v28/CARS/{car}/GEOMETRY.BIN")


class TestV28LiveISOSmoke(unittest.TestCase):
    @live
    def test_live_gti_and_streaml2ra_golden_hour_smoke(self) -> None:
        real_open = Path.open

        def guarded(self: Path, mode: str = "r", *a, **k):  # type: ignore[no-untyped-def]
            assert self != LIVE_ISO or mode == "rb", f"ISO opened with mode {mode!r}"
            return real_open(self, mode, *a, **k)

        with tempfile.TemporaryDirectory() as td, mock.patch.object(Path, "open", guarded):
            m = oc.run(LIVE_ISO, ("GTI",), Path(td), budget=oc.LIMIT_BUDGET, write=False)
            gti = m["cars"][0]
            self.assertTrue(gti["ok"], gti.get("failures"))
            self.assertLessEqual(gti["pack_bytes"], oc.SAFE_CAR_PACK_CAP)
            parts = {p["solid"]: p for p in gti["parts"]}
            self.assertGreater(parts["GTI_KIT00_BODY_B"]["added_tris"], 3000)
            self.assertGreater(parts["GTI_KIT00_FRONT_TIRE_B"]["added_tris"], 500)

            arc = PS2NFSMWArchive(LIVE_ISO)
            l2ra = arc.read_entry(arc.find(r"TRACKS\L2RA.BUN"))
            stream = arc.read_entry(arc.find(r"TRACKS\STREAML2RA.BUN"))
            patched, rep = b3d.bake_streaml2ra_bytes(l2ra, stream, district="rosewood")
            assert patched is not None
            self.assertEqual((len(patched), rep["alpha_bytes_modified"]), (len(stream), 0))
            self.assertGreaterEqual(rep["luma_after_mean"], 138.0)
            self.assertGreaterEqual(rep["sunlit_mean_delta"], 14.0)
            self.assertTrue(-16.0 <= rep["shadow_mean_delta"] <= -8.0, rep["shadow_mean_delta"])
            self.assertTrue(-22.0 <= rep["underside_mean_delta"] <= -14.0, rep["underside_mean_delta"])

            raw_lzc = arc.read_entry(arc.find(r"GLOBAL\GLOBALB.LZC"))
            raw_mf = arc.read_entry(arc.find(r"GLOBAL\GLOBALMEMORYFILE.BIN"))
            recipe = json.loads((REPO_ROOT / "patches" / "lighting" / "rockport2026_3d_depth.json").read_text("utf-8"))
            pbun, _ = lp.apply_lighting_recipes(lp.lz_decompress(raw_lzc), [recipe])
            comp = lp.compress_jdlz_compact(pbun)
            _, mf_audit = lp.patch_globalmemoryfile_globalb(raw_mf, comp)
            self.assertEqual(mf_audit["mode"], "in-place")


if __name__ == "__main__":
    unittest.main()
