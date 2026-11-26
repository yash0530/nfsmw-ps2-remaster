#!/usr/bin/env python3
"""v2.3 tests: HD HUD/loading-icon guards + strictly opt-in per-car VU1 smoothing.

Offline tests need no ISO (NFSMW_ISO unset or ``/nonexistent``); live tests open the untouched ISO
read-only and skip cleanly when absent. HUD tests skip when numpy/Pillow are unavailable (``python3 -s``).
"""

from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import geometry_compiler as gc  # noqa: E402
import optin_car_geometry as oc  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

try:
    import numpy as np
    import build_hud_ui_pack as hud
except ImportError:  # numpy/Pillow live outside the -s (no user site) interpreter
    np = hud = None

LIVE_ISO = oc.resolve_iso()
TYRE = "SYNTH_KIT00_FRONT_TIRE_B"
live = unittest.skipUnless(LIVE_ISO is not None, "NFSMW ISO not present; skipping live read-only test")


def _icon(n: int = 32):
    """Synthetic PS2 loading icon: opaque ring + soft glow, GS alpha in [0..128]."""
    yy, xx = np.mgrid[0:n, 0:n]
    r = np.hypot(yy - n / 2 + 0.5, xx - n / 2 + 0.5)
    a = np.where((r > n * 0.18) & (r < n * 0.32), 128, np.where(r < n * 0.38, 40, 0))
    return np.dstack([(xx * 8) % 256, (yy * 8) % 256, np.full_like(xx, 200), a]).astype(np.uint8)


def _tyre(radius: float = 0.33, width: float = 0.25, seg: int = 24, dx: float = 0.0) -> gc.CompiledMesh:
    angles = [2 * math.pi * i / seg for i in range(seg) for _ in (0, 1)]
    verts = [(radius * math.cos(a) + dx, (k % 2) * width, radius * math.sin(a)) for k, a in enumerate(angles)]
    norms = [(math.cos(a), 0.0, math.sin(a)) for a in angles]
    uvs = [(k // 2 / seg, float(k % 2)) for k in range(2 * seg)]
    nxt = [2 * ((i + 1) % seg) for i in range(seg)]
    tris = [t for i in range(seg) for t in ((2 * i, nxt[i], 2 * i + 1, 0, 0), (2 * i + 1, nxt[i], nxt[i] + 1, 0, 0))]
    return gc.CompiledMesh(verts, norms, uvs, tris)


def _pack() -> bytes:
    return gc.build_synthetic_geometry_pack("CARS\\SYNTH\\GEOMETRY.BIN", "SYNTH", [
        ("SYNTH_KIT00_BODY_B", _tyre(1.2, 1.8, 32), [1], [2]), (TYRE, _tyre(), [3], [4]),
        ("SYNTH_KIT00_TRIM_C", _tyre(0.2), [5], [6])])


def _fails(orig: bytes, new: bytes, expected: dict) -> str:
    return " ".join(oc.validate_pack(orig, new, expected, oc.CarBudget())["failures"])


@unittest.skipIf(hud is None, "numpy/Pillow unavailable")
class TestHudLoadingIcons(unittest.TestCase):
    def test_loading_icon_classification_and_scale(self) -> None:
        self.assertEqual(hud.classify("LOAD_LED_ON", r"GLOBAL\GLOBALB.LZC"), "loading")
        self.assertEqual(hud.classify("GENERIC_ICON_LOAD", r"FRONTEND\FRONTB.LZC"), "loading")
        self.assertIsNone(hud.classify("ROAD_ASPHALT_01", r"GLOBAL\GLOBALB.LZC"))  # world texture stays in v2.2
        self.assertEqual(hud.classify("FONT_DIGITS", r"LANGUAGES\LANGUAGETEXTURES.BIN"), "font")
        self.assertEqual((hud.choose_scale(32, 32, "loading"), hud.choose_scale(256, 512, "loading")), (8, 4))

    def test_upscale_preserves_ps2_alpha_range(self) -> None:
        src = _icon()
        hd = hud.upscale_ps2_rgba(src, 4)
        self.assertEqual(hd.shape, (128, 128, 4))
        self.assertEqual(int(hd[:, :, 3].max()), 128)  # GS-opaque stays 128, never promoted to 255
        self.assertEqual(int(hd[:8, :8, 3].max()), 0)  # transparent corner never bleeds
        v = hud.validate_hd(src, hd, 4)
        self.assertTrue(v["ok"], v)
        self.assertEqual((v["hd_alpha_max"], v["bleed_texels"]), (128, 0))

    def test_validator_rejects_shifted_garbled_and_misshaped_icons(self) -> None:
        src = _icon()
        hd = hud.upscale_ps2_rgba(src, 4)
        self.assertFalse(hud.validate_hd(src, np.roll(hd, 4, axis=1), 4)["ok"])  # one source texel off
        self.assertFalse(hud.validate_hd(src, np.random.default_rng(7).integers(0, 129, hd.shape, dtype=np.uint8), 4)["ok"])
        self.assertFalse(hud.validate_hd(src, hd[:-4], 4)["ok"])

    def test_bc7_alpha_clamped_to_ps2_range(self) -> None:
        lo0 = 0x40 | (127 << 49) | (1 << 63)  # mode 6: e0 alpha=255 (->128), e1 alpha=1 (->0)
        lo1 = 0x40 | (127 << 56) | (1 << 63)  # mode 6: e0 alpha=1 (->0), e1 alpha=255 (->128)
        raw = struct.pack("<QQ", lo0, 1) + struct.pack("<QQ", lo1, 1)  # all-e0 indices in both blocks
        blocks, fixed = hud.clamp_bc7_ps2_alpha(np.frombuffer(raw, dtype=np.uint8).reshape(2, 16))
        dec = hud.decode_bc7_mode6(blocks, 8, 4)
        self.assertEqual(fixed, 4)
        self.assertTrue((dec[:, :4, 3] == 128).all() and (dec[:, 4:, 3] == 0).all())

    @live
    def test_live_loading_icons_extract_and_validate(self) -> None:
        arc, names = PS2NFSMWArchive(LIVE_ISO), set()
        for vp in (r"GLOBAL\GLOBALB.LZC", r"FRONTEND\FRONTB.LZC"):
            for rt in hud.iter_raw_textures(arc.read_entry(arc.find(vp)), vp):
                if hud.classify(rt.name, vp) == "loading":
                    src, s = hud.decode_raw(rt), hud.choose_scale(rt.width, rt.height, "loading")
                    hd = hud.upscale_ps2_rgba(src, s)
                    self.assertTrue(hud.swizzle_coherence(rt)["ok"], rt.name)
                    self.assertTrue(hud.validate_hd(src, hd, s)["ok"], rt.name)
                    self.assertLessEqual(int(hd[:, :, 3].max()), 128, rt.name)
                    names.add(rt.name)
        self.assertTrue({"LOAD_LED_ON", "GENERIC_AUTOSAVE", "GENERIC_ICON_LOAD"} <= names, names)


class TestOptInCars(unittest.TestCase):
    def test_synthetic_car_smooths_and_validates(self) -> None:
        pack = _pack()
        self.assertEqual(oc.pack_table_errors(pack, pack), [])
        new, rep = oc.smooth_car_pack(pack, "synth")
        self.assertTrue(rep["ok"] and new is not None, rep.get("failures"))
        self.assertEqual([p["status"] for p in rep["parts"]], ["smoothed", "smoothed"])
        self.assertEqual(rep["added_tris"], 3 * (64 + 48))
        self.assertLessEqual(rep["pack_bytes"], rep["pack_cap"])

    def test_per_car_budget_guards(self) -> None:
        pack = _pack()
        new, rep = oc.smooth_car_pack(pack, "SYNTH", budget=oc.CarBudget(max_added_tris=200))
        self.assertIsNone(new)
        self.assertTrue(any(f.startswith("car_added_tris") for f in rep["failures"]), rep["failures"])
        new, rep = oc.smooth_car_pack(pack, "SYNTH", budget=oc.CarBudget(max_solid_tris=100))
        self.assertEqual((new, rep["failures"]), (None, ["no_eligible_parts"]))
        self.assertTrue(all(p["reason"].startswith("tri_budget") for p in rep["parts"]))
        new, rep = oc.smooth_car_pack(pack, "SYNTH", budget=oc.CarBudget(max_pack_bytes=len(pack)))
        self.assertIsNone(new)
        self.assertTrue(rep["failures"][0].startswith(("compile_budget", "pack_bytes")), rep["failures"])
        self.assertEqual(oc.smooth_car_pack(pack, "WHEELS")[1]["failures"], ["shared_pack_cached_in_globalmemoryfile"])

    def test_damage_lod_and_missing_part_gate(self) -> None:
        blob = gc._make_bchunk(gc.CHUNK_SOLID_PACK, gc._make_bchunk(gc.CHUNK_ESOLID_HDR, bytes(0xA4))
                               + gc._make_bchunk(0x00134017, bytes(12)))
        part = types.SimpleNamespace(name="X_KIT00_BODY_B", chunk_offset=0, strip_poly_total=10)
        self.assertEqual(oc.eligibility(blob, part, oc.CarBudget()), "damage_morph_tables_index_original_strips")
        part.name = "X_KIT00_BODY_A"
        self.assertEqual(oc.eligibility(blob, part, oc.CarBudget()), "not_lod_b")
        self.assertEqual(oc.eligibility(blob, None, oc.CarBudget()), "part_not_found")

    def test_wheel_hub_and_aabb_integrity_rejection(self) -> None:
        pack = _pack()
        bad, stats = gc.replace_solids_in_pack(pack, {TYRE: _tyre(dx=0.005)})
        fails = _fails(pack, oc.restore_authored_aabb(pack, bad, [TYRE]), {TYRE: stats[TYRE].strip_polys})
        self.assertIn("wheel_hub_cx_drift", fails)
        self.assertIn("vu1_aabb_drift", fails)
        good, _ = oc.smooth_car_pack(pack, "SYNTH", parts=["KIT00_FRONT_TIRE_B"])
        info = oc.gi.parse_geometry_pack(good)[0]
        tyre, trim = (next(p for p in info.parts if p.name.endswith(k)) for k in ("TIRE_B", "TRIM_C"))
        expected = {TYRE: tyre.strip_poly_total}
        self.assertEqual(_fails(pack, good, expected), "")
        buf = bytearray(good)
        struct.pack_into("<f", buf, oc._spans(good, tyre.chunk_offset)[gc.CHUNK_ESOLID_HDR][0] + 0x20, -0.5)
        self.assertIn("esolid_header_changed", _fails(pack, bytes(buf), expected))
        buf = bytearray(good)
        buf[trim.chunk_offset + oc._solid_len(good, trim.chunk_offset) - 4] ^= 0xFF
        self.assertIn("untouched_sibling_changed", _fails(pack, bytes(buf), expected))

    def test_cli_is_opt_in_and_iso_optional(self) -> None:
        with mock.patch.dict(os.environ, {"NFSMW_ISO": "/nonexistent"}), mock.patch("sys.stderr"):
            self.assertIsNone(oc.resolve_iso())
            self.assertEqual(oc.main([]), 2)  # nothing runs without an explicit car/preset
            self.assertEqual(oc.main(["--car", "BMWM3GTRE46"]), 2)

    @live
    def test_live_hero_cars_validate_and_promote_read_only(self) -> None:
        real_open = Path.open

        def guarded(self: Path, mode: str = "r", *a, **k):  # type: ignore[no-untyped-def]
            assert self != LIVE_ISO or mode == "rb", f"ISO opened with mode {mode!r}"
            return real_open(self, mode, *a, **k)

        with tempfile.TemporaryDirectory() as td, mock.patch.object(Path, "open", guarded):
            m = oc.run(LIVE_ISO, oc.HERO_CARS, Path(td))
            self.assertEqual(len(m["overrides"]), 4)
            for r in m["cars"]:
                self.assertTrue(r["ok"] and r["promoted"], (r["car"], r.get("failures")))
                self.assertLessEqual(r["pack_bytes"], oc.RETAIL_MAX_CAR_PACK)
                parts = {p["solid"].split("_KIT00_")[1]: p for p in r["parts"]}
                self.assertEqual(parts["BODY_B"]["reason"], "damage_morph_tables_index_original_strips")
                self.assertEqual(parts["FRONT_TIRE_B"]["status"], "smoothed")
                self.assertTrue((Path(td) / "CARS" / r["car"] / "GEOMETRY.BIN").is_file())


if __name__ == "__main__":
    unittest.main()
