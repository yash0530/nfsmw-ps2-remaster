#!/usr/bin/env python3
"""v2.6 tests: curvature-selective 3D car smoothing, Blacklist roster, and 3D depth/clearcoat lighting.

Offline tests pass with ``NFSMW_ISO=/nonexistent`` (Linux & macOS GitHub CI safe); live ISO tests
open the untouched retail SLUS-21351 ISO strictly read-only ('rb') and skip cleanly when absent.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import geometry_compiler as gc  # noqa: E402
import lighting_ps2 as lp  # noqa: E402
import optin_car_geometry as oc  # noqa: E402
import rockport_cli as rcli  # noqa: E402
import subdivide_car_mesh as scm  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

LIVE_ISO = oc.resolve_iso()
live = unittest.skipUnless(LIVE_ISO is not None, "NFSMW ISO not present; skipping live read-only test")


def _hybrid_flat_and_curved_mesh(nx_flat: int = 4, nx_arch: int = 8, nz: int = 4) -> gc.CompiledMesh:
    """Flat panel smoothly stitched to a curved quarter-cylinder arch (for Red-Green watertight tests)."""
    r = 0.6
    cols = [(-1.0 + i * 0.25, 0.0, 0.0, 1.0) for i in range(nx_flat)] + [
        (r * math.sin(a), r * (math.cos(a) - 1.0), math.sin(a), math.cos(a))
        for a in ((math.pi / 2.0) * (j / nx_arch) for j in range(nx_arch + 1))
    ]
    nc = len(cols)
    verts = [(x, y, iz * 0.25) for iz in range(nz + 1) for x, y, _, _ in cols]
    norms = [(nx, ny, 0.0) for _ in range(nz + 1) for _, _, nx, ny in cols]
    uvs = [(ic / max(1, nc - 1), iz / max(1, nz)) for iz in range(nz + 1) for ic in range(nc)]
    tris = [t for iz in range(nz) for ic in range(nc - 1)
            for a in (iz * nc + ic,) for t in ((a, a + 1, a + nc, 0, 0), (a + 1, a + nc + 1, a + nc, 0, 0))]
    return gc.CompiledMesh(verts, norms, uvs, tris)


def _curved_tyre(radius: float = 0.33, width: float = 0.25, seg: int = 24) -> gc.CompiledMesh:
    angles = [2 * math.pi * i / seg for i in range(seg) for _ in (0, 1)]
    verts = [(radius * math.cos(a), (k % 2) * width, radius * math.sin(a)) for k, a in enumerate(angles)]
    norms = [(math.cos(a), 0.0, math.sin(a)) for a in angles]
    uvs = [(k // 2 / seg, float(k % 2)) for k in range(2 * seg)]
    nxt = [2 * ((i + 1) % seg) for i in range(seg)]
    tris = [t for i in range(seg) for t in ((2 * i, nxt[i], 2 * i + 1, 0, 0), (2 * i + 1, nxt[i], nxt[i] + 1, 0, 0))]
    return gc.CompiledMesh(verts, norms, uvs, tris)


def _edge_counts(mesh: gc.CompiledMesh) -> dict[tuple[scm.PosKey, scm.PosKey], int]:
    pks = [scm._pos_key(v) for v in mesh.vertices]
    counts: dict[tuple[scm.PosKey, scm.PosKey], int] = defaultdict(int)
    for i0, i1, i2, _, _ in mesh.triangles:
        for a, b in ((pks[i0], pks[i1]), (pks[i1], pks[i2]), (pks[i2], pks[i0])):
            if a != b:
                counts[(a, b) if a <= b else (b, a)] += 1
    return counts


class TestV26SelectiveCarGeometryOffline(unittest.TestCase):
    def test_selective_pn_subdivides_curved_leaves_flat_lean_and_watertight(self) -> None:
        src = _hybrid_flat_and_curved_mesh()
        orig_interior = sum(1 for c in _edge_counts(src).values() if c == 2)
        sub, stats = scm.subdivide_compiled_mesh_selective(
            src, crease_deg=38.0, min_curve_deg=6.0, min_edge_len=0.01, max_output_tris=220, preserve_aabb=True
        )
        self.assertTrue(stats.input_triangles < stats.output_triangles <= 220)
        self.assertLess(stats.output_triangles, stats.input_triangles * 4)
        new_ec = _edge_counts(sub)
        self.assertGreater(sum(1 for c in new_ec.values() if c == 2), orig_interior)
        self.assertEqual(sum(1 for c in new_ec.values() if c > 2), 0)
        omin, omax = gc._compute_aabb(src.vertices)
        nmin, nmax = gc._compute_aabb(sub.vertices)
        for ax in range(3):
            self.assertGreaterEqual(nmin[ax], omin[ax] - 1e-5)
            self.assertLessEqual(nmax[ax], omax[ax] + 1e-5)

    def test_limit_tier_guards_secondary_damage_markers_hub_and_shared_packs(self) -> None:
        pack = gc.build_synthetic_geometry_pack("CARS\\V26CAR\\GEOMETRY.BIN", "V26CAR", [
            ("V26CAR_KIT00_BODY_B", _hybrid_flat_and_curved_mesh(3, 6, 4), [1], [2]),
            ("V26CAR_KIT00_FRONT_TIRE_B", _curved_tyre(), [3], [4]),
            ("V26CAR_KIT00_DAMAGE1_BODY_B", _curved_tyre(0.5), [5], [6]),
        ])
        new, rep = oc.smooth_car_pack(pack, "V26CAR", budget=oc.LIMIT_BUDGET)
        self.assertTrue(rep["ok"] and new is not None, rep.get("failures"))
        self.assertGreater(rep["added_tris"], 100)
        parts_by_name = {p["solid"]: p for p in rep["parts"]}
        self.assertEqual(parts_by_name["V26CAR_KIT00_BODY_B"]["status"], "smoothed")
        self.assertEqual(parts_by_name["V26CAR_KIT00_FRONT_TIRE_B"]["status"], "smoothed")
        self.assertNotIn("V26CAR_KIT00_DAMAGE1_BODY_B", parts_by_name)
        p_dmg1 = next(p for p in oc.gi.parse_geometry_pack(pack)[0].parts if "DAMAGE1" in p.name)
        self.assertEqual(oc.eligibility(pack, p_dmg1, oc.LIMIT_BUDGET), "secondary_damage_state_skipped")
        self.assertEqual(oc.smooth_car_pack(pack, "WHEELS", budget=oc.LIMIT_BUDGET)[1]["failures"],
                         ["shared_pack_cached_in_globalmemoryfile"])
        self.assertGreaterEqual(len(oc.BLACKLIST_PRESET), 20)


class TestV26LightingAndProfileOffline(unittest.TestCase):
    def test_rockport2026_3d_depth_lighting_recipe_schema_and_bounds(self) -> None:
        recipe_path = REPO_ROOT / "patches" / "lighting" / "rockport2026_3d_depth.json"
        data = json.loads(recipe_path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["id"], "rockport2026_3d_depth")
        patches = data["patches"]
        self.assertGreaterEqual(len(patches), 120)
        seen_offsets: set[int] = set()
        sun_profs: set[str] = set()
        mats: set[str] = set()
        for p in patches:
            off = int(p["expected_offset"], 16)
            self.assertNotIn(off, seen_offsets, f"Duplicate offset {p['expected_offset']}")
            seen_offsets.add(off)
            if p["section"] == "SunInfos":
                sun_profs.add(p["profile"])
                if p.get("member") == "Colour":
                    self.assertEqual(p["new_value"][3], 128)  # PS2 GS opaque alpha = 0x80
            elif p["section"] == "LightMaterials":
                mats.add(p["material"])
                if p["field"] in ("SpecularPower", "EnvmapMinScale", "EnvmapMaxScale"):
                    self.assertGreater(float(p["new_value"]), 0.0)
        self.assertEqual(sun_profs, {"MIDDAY", "PARKLAND", "SUNSET", "PROTOTYPE", "E3DEMO"})
        self.assertTrue({"ROAD", "METPAINTSILVER", "METPAINTBLUE", "REGPAINTRED", "CHROME", "WINDSHIELD"} <= mats)

    def test_rockport2026_3d_limit_profile_and_elf_patch_valid(self) -> None:
        prof_path = REPO_ROOT / "patches" / "profiles" / "rockport2026_3d_limit.json"
        info = rcli.validate_build_profile(prof_path, repo_root=REPO_ROOT)
        self.assertTrue(info["valid"], info["errors"])
        self.assertIn("high_detail_reflection_scenery", info["elf_patch_ids"])
        self.assertIn("crisp_3d_depth_lighting_v26", info["elf_patch_ids"])
        self.assertEqual(info["vlt_recipes"][0]["id"], "authentic_measured_v24")
        self.assertEqual(info["lighting_recipes"][0]["id"], "rockport2026_3d_depth")
        self.assertGreaterEqual(info["zz_override_count"], 20)
        # Verify ELF patch sun-key/fill contrast boost and balanced g_MaxRoadDampness in [0.45, 0.52]
        elf_spec = json.loads((REPO_ROOT / "patches" / "elf_patches.json").read_text(encoding="utf-8"))
        patch = next(p for p in elf_spec["patches"] if p["id"] == "crisp_3d_depth_lighting_v26")
        by_va = {int(w["vaddr"], 16): struct.unpack("<f", struct.pack("<I", int(w["patched"], 16)))[0]
                 for w in patch["writes"]}
        self.assertGreater(by_va[0x00547490] / by_va[0x005474B0], 6.5 / 2.1)
        self.assertTrue(0.45 <= by_va[0x005A2C80] <= 0.52, by_va[0x005A2C80])


class TestV26LiveRetailVerification(unittest.TestCase):
    @live
    def test_live_bmw_and_cop_limit_tier_read_only(self) -> None:
        real_open = Path.open

        def guarded(self: Path, mode: str = "r", *a, **k):  # type: ignore[no-untyped-def]
            assert self != LIVE_ISO or mode == "rb", f"ISO opened with mode {mode!r}"
            return real_open(self, mode, *a, **k)

        with tempfile.TemporaryDirectory() as td, mock.patch.object(Path, "open", guarded):
            m = oc.run(LIVE_ISO, ("BMWM3GTRE46", "CARRERAGT", "COPGTO"), Path(td), budget=oc.LIMIT_BUDGET, write=False)
            for r in m["cars"]:
                self.assertTrue(r["ok"], (r["car"], r.get("failures")))
                self.assertLessEqual(r["pack_bytes"], oc.SAFE_CAR_PACK_CAP)
            bmw = m["cars"][0]
            self.assertGreaterEqual(bmw["added_tris"], 5000)
            parts = {p["solid"]: p for p in bmw["parts"]}
            self.assertGreater(parts["BMWM3GTRE46_KIT00_BODY_B"]["added_tris"], 3500)
            self.assertLessEqual(parts["BMWM3GTRE46_KIT00_BODY_B"]["new_bytes"], oc.LIMIT_MAX_SOLID)
            self.assertGreater(parts["BMWM3GTRE46_KIT00_FRONT_TIRE_B"]["added_tris"], 1000)

    @live
    def test_live_rockport2026_3d_depth_patches_globalb_and_memfile_inplace(self) -> None:
        arc = PS2NFSMWArchive(LIVE_ISO)
        raw_lzc = arc.read_entry(arc.find(r"GLOBAL\GLOBALB.LZC"))
        raw_mf = arc.read_entry(arc.find(r"GLOBAL\GLOBALMEMORYFILE.BIN"))
        bun = lp.lz_decompress(raw_lzc)
        recipe = json.loads((REPO_ROOT / "patches" / "lighting" / "rockport2026_3d_depth.json").read_text("utf-8"))
        patched_bun, audit = lp.apply_lighting_recipes(bun, [recipe])
        self.assertEqual(len(patched_bun), len(bun))
        self.assertEqual(len(audit), len(recipe["patches"]))
        repacked_lzc = lp.compress_jdlz_compact(patched_bun)
        self.assertEqual(lp.jdlz_decompress(repacked_lzc), patched_bun)
        patched_mf, mf_audit = lp.patch_globalmemoryfile_globalb(raw_mf, repacked_lzc)
        self.assertEqual(mf_audit["mode"], "in-place")
        self.assertEqual(len(patched_mf), len(raw_mf))


if __name__ == "__main__":
    unittest.main()
