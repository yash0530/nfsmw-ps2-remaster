#!/usr/bin/env python3
"""Offline tests for tools/benchmark_4car_race.py (+ race-balanced ELF options); pass with NFSMW_ISO=/nonexistent."""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import benchmark_4car_race as rb  # noqa: E402
import elf_disasm  # noqa: E402
import lint_pnach  # noqa: E402
import pcsx2_pine_driver as pine  # noqa: E402
import pcsx2_profile_2026 as profiles  # noqa: E402
from tests.test_elf_patches import _find_local_iso  # noqa: E402

try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

# Stock SLUS_213.51 moving-camera envmap throttle table @ 0x0054FEE0 (5 rows x 6 faces, read-only research dump)
STOCK_THROTTLE_ROWS = [[1, 1, 0, 1, 0, 0], [1, 0, 0, 0, 1, 1], [1, 1, 0, 0, 1, 0], [1, 0, 0, 1, 0, 1], [1, 0, 0, 0, 1, 1]]


def _samples(seconds: float, wlc_hz: float, fps: float, *, flow: int = rb.FLOW_RACING) -> List[Dict[str, Any]]:
    """Change-point samples like FrameSampler emits: one per display swap."""
    n = int(seconds * fps)
    return [{"t": 1000.0 + i / fps, "flow": flow, "wlc": int(i / fps * wlc_hz), "disp": 500 + i} for i in range(n + 1)]


def _records(seconds: float, *, ai: int = 3, racers: int = 4, active: bool = True) -> List[Dict[str, Any]]:
    return [{"timestamp": 1000.0 + i * 0.05, "flow_state": rb.FLOW_RACING, "race_active": active, "ai_racer_count": ai,
             "racer_count": racers, "world_loop_counter": int(i * 3), "display_frame_counter": 500 + i * 3,
             "player_present": True, "paused": False, "pos": [i * 1.0, 0.0, 0.0]} for i in range(int(seconds * 20))]


class TestHostSpeed(unittest.TestCase):
    def test_formula_catches_75pct_slow_motion(self) -> None:
        self.assertAlmostEqual(rb.host_speed_pct(60, 1.0), 100.0)
        self.assertAlmostEqual(rb.host_speed_pct(45, 1.0), 75.0)
        self.assertIsNone(rb.host_speed_pct(60, 0.0))

    def test_full_speed_vs_slow_mo_runs(self) -> None:
        full = rb.host_speed_metrics(_samples(30, 60, 60))
        self.assertAlmostEqual(full["avg_pct"], 100.0, delta=0.5)
        self.assertGreaterEqual(full["min_pct"], 97.0)
        self.assertEqual(full["pct_of_race_ge_98"], 100.0)
        slow = rb.host_speed_metrics(_samples(30, 45, 40))
        self.assertAlmostEqual(slow["avg_pct"], 75.0, delta=0.5)
        self.assertLess(slow["p10_pct"], 80.0)
        self.assertEqual(slow["pct_of_race_ge_98"], 0.0)

    def test_non_racing_and_counter_reset_excluded(self) -> None:
        s = _samples(5, 60, 60, flow=int(pine.GameFlowState.IN_FRONTEND)) + _samples(5, 60, 60)
        m = rb.host_speed_metrics(s)
        self.assertAlmostEqual(m["racing_sec"], 5.0, delta=0.1)
        reset = _samples(3, 60, 60)
        reset[90]["wlc"] = 0  # level reload: negative delta must not count
        self.assertAlmostEqual(rb.host_speed_metrics(reset)["avg_pct"], 100.0, delta=1.0)


class TestDisplayMetrics(unittest.TestCase):
    def test_locked_60_has_no_stutters(self) -> None:
        d = rb.display_metrics(rb.frame_times_ms(_samples(20, 60, 60)))
        for k in ("avg_fps", "p50_fps", "p10_fps", "p1_low_fps", "min_fps"):
            self.assertAlmostEqual(d[k], 60.0, delta=0.5, msg=k)
        self.assertEqual(d["stutters_gt_25ms"], 0)

    def test_stutters_and_p1_low(self) -> None:
        ft = [1000 / 60] * 1500 + [40.0] * 30
        d = rb.display_metrics(ft)
        self.assertEqual(d["stutters_gt_25ms"], 30)
        self.assertAlmostEqual(d["p1_low_fps"], 25.0, delta=0.1)
        self.assertAlmostEqual(d["max_frame_ms"], 40.0)
        self.assertLess(d["min_fps"], 60.0)

    def test_multi_swap_poll_is_split_and_gaps_reset(self) -> None:
        s = [{"t": 0.0, "flow": 6, "wlc": 0, "disp": 1}, {"t": 0.01, "flow": 6, "wlc": 1, "disp": 2},
             {"t": 0.0433, "flow": 6, "wlc": 3, "disp": 4}, {"t": 0.05, "flow": 3, "wlc": 3, "disp": 5},
             {"t": 0.06, "flow": 6, "wlc": 4, "disp": 6}]
        ft = rb.frame_times_ms(s)
        self.assertEqual(len(ft), 2)
        self.assertAlmostEqual(ft[0], 16.65, places=2)

    def test_40fps_race(self) -> None:
        d = rb.display_metrics(rb.frame_times_ms(_samples(20, 45, 40)))
        self.assertAlmostEqual(d["avg_fps"], 40.0, delta=0.5)


class TestRaceDetectionAndGate(unittest.TestCase):
    GOOD_HOST = {"avg_pct": 99.9, "min_pct": 97.5}
    GOOD_DISP = {"avg_fps": 59.9, "p10_fps": 58.0}

    def test_four_car_vs_solo_freeroam(self) -> None:
        self.assertTrue(rb.race_detection(_records(5))["four_car_race"])
        solo = rb.race_detection(_records(5, ai=0, racers=0, active=False))
        self.assertFalse(solo["four_car_race"])
        self.assertEqual(solo["opponents"], 0)
        self.assertTrue(rb.race_detection(_records(5, ai=0, racers=4))["four_car_race"])
        self.assertFalse(rb.race_detection(_records(5, ai=999, racers=-1))["four_car_race"])  # garbage counts

    def test_gate_thresholds(self) -> None:
        race = rb.race_detection(_records(5))
        self.assertTrue(rb.evaluate_gate(self.GOOD_HOST, self.GOOD_DISP, race)["pass"])
        bug = rb.evaluate_gate({"avg_pct": 75.0, "min_pct": 70.0}, {"avg_fps": 40.0, "p10_fps": 36.0}, race)
        self.assertEqual(bug["failures"], ["host_speed_avg_pct", "host_speed_min_pct", "display_fps_avg", "display_fps_p10"])
        cases = {"host_speed_min_pct": ({**self.GOOD_HOST, "min_pct": 94.9}, self.GOOD_DISP),
                 "display_fps_p10": (self.GOOD_HOST, {**self.GOOD_DISP, "p10_fps": 54.9}),
                 "display_fps_avg": (self.GOOD_HOST, {**self.GOOD_DISP, "avg_fps": None})}
        for name, (h, d) in cases.items():
            self.assertEqual(rb.evaluate_gate(h, d, race)["failures"], [name])
        solo = rb.race_detection(_records(5, ai=0, racers=1))
        self.assertEqual(rb.evaluate_gate(self.GOOD_HOST, self.GOOD_DISP, solo)["failures"], ["four_car_race"])


class TestAnalyzeRunOffline(unittest.TestCase):
    def _run(self, tmp: Path, wlc_hz: float, fps: float, with_sampler: bool = True) -> Dict[str, Any]:
        recs = _records(32)
        for i, r in enumerate(recs):
            r["world_loop_counter"], r["display_frame_counter"] = int(i * 0.05 * wlc_hz), int(i * 0.05 * fps)
        (tmp / "pine_telemetry.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        if with_sampler:
            (tmp / "frame_samples.jsonl").write_text("".join(json.dumps(s) + "\n" for s in _samples(32, wlc_hz, fps)))
        return rb.analyze_run(tmp)

    def test_full_speed_passes_and_slow_mo_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ok = self._run(Path(d), 60, 60)
            self.assertEqual(ok["sample_source"], "frame_sampler_250hz")
            self.assertTrue(ok["gate"]["pass"], ok["gate"])
        with tempfile.TemporaryDirectory() as d:
            bad = self._run(Path(d), 45, 40)
            self.assertFalse(bad["gate"]["pass"])
            self.assertAlmostEqual(bad["host_speed"]["avg_pct"], 75.0, delta=1.0)
        with tempfile.TemporaryDirectory() as d:
            fallback = self._run(Path(d), 60, 60, with_sampler=False)
            self.assertEqual(fallback["sample_source"], "telemetry_20hz")
            self.assertAlmostEqual(fallback["host_speed"]["avg_pct"], 100.0, delta=1.0)


class _FakeClient:
    def __init__(self, triples: List[tuple]) -> None:
        self.triples = list(triples)

    def read_batch_raw(self, ops: Any) -> List[bytes]:
        return [struct.pack("<I", v) for v in self.triples.pop(0)]


class TestSamplerAndMenu(unittest.TestCase):
    def test_frame_sampler_keeps_change_points_only(self) -> None:
        clock = iter(float(i) for i in range(10))
        fs = rb.FrameSampler(_FakeClient([(6, 1, 1), (6, 1, 1), (6, 2, 2), (6, 2, 3)]), clock=lambda: next(clock))
        for _ in range(4):
            fs.poll_once()
        self.assertEqual([s["disp"] for s in fs.samples], [1, 2, 3])

    def test_quickrace_route_and_custom_keys(self) -> None:
        nav = pine.FrontendMenuNavigator("quickrace")
        sink = pine.NullInputSink()
        for t in range(0, 40):
            nav.step(float(t) / 2, int(pine.GameFlowState.IN_FRONTEND), sink)
        self.assertIn("start_race:k", nav.steps_sent)
        self.assertEqual(rb.parse_menu_keys("2:k, 5:right,6.5:k")[1][:2], (5.0, "right"))
        for bad in ("", "5:k,2:k", "1:k;rm"):
            with self.assertRaises(ValueError):
                rb.parse_menu_keys(bad)


class TestVariants(unittest.TestCase):
    spec = rb.load_patch_spec()
    by_id = {p["id"]: p for p in spec["patches"]}

    def test_variant_patch_ids_are_baked_or_opt_in(self) -> None:
        for name, v in rb.VARIANTS.items():
            for pid in v.get("restore", []):
                self.assertTrue(self.by_id[pid]["enabled_by_default"], f"{name}: restore {pid} not baked")
            for pid in v.get("apply", []):
                self.assertFalse(self.by_id[pid]["enabled_by_default"], f"{name}: apply {pid} is default")

    def test_race_balanced_restores_before_applying(self) -> None:
        w = dict(rb.variant_writes("race_balanced", self.spec))
        expect = {0x0020F5B0: 0x10400034, 0x0059FF28: 0xFFFFFFFF, 0x0059FF2C: 0xFFFFFFFF, 0x003DB110: 0x2A020009,
                  0x003DB178: 0x3C01408B, 0x003DB17C: 0x34213333, 0x00505178: 0x42200000}
        for a, v in expect.items():
            self.assertEqual(w[a], v, hex(a))
        self.assertEqual(sum(1 for a in w if 0x0054FEE0 <= a < 0x0054FF58), 5)
        self.assertEqual(rb.variant_writes(rb.BASELINE, self.spec), [])

    def test_variant_pnach_and_profiles_valid_and_never_touch_v241_look(self) -> None:
        self.assertEqual(rb.build_variant_profile(rb.BASELINE), profiles.build_v241_clean_profile())
        look_keys = {"ShadeBoost", "ShadeBoost_Brightness", "ShadeBoost_Contrast", "ShadeBoost_Saturation",
                     "ShadeBoost_Gamma", "CASMode", "CASSharpness", "fxaa", "VsyncEnable", "TVShader"}
        for name, v in rb.VARIANTS.items():
            writes = rb.variant_writes(name, self.spec)
            self.assertNotIn(0x00550D28, dict(writes), f"{name} re-enables RT9 double-lining")
            sec, text = rb.variant_pnach_section(name, writes)
            ini = rb.build_variant_profile(name, section=sec)
            self.assertEqual(profiles.validate_profile_ini(ini), [], name)
            self.assertIn("Enable = Disable All Post-Processing (Zero Double-Lining)", ini)
            for keys in v.get("ini", {}).values():
                self.assertFalse(look_keys & set(keys), name)
                for k, val in keys.items():
                    self.assertIn(f"{k} = {val}", ini)
            if sec:
                errs = lint_pnach.lint_pnach_text("gametitle=t\ncomment=c\n" + text)
                self.assertEqual(errs, [], f"{name}: {errs}")
        self.assertIn("EECycleRate = 2", rb.build_variant_profile("ee_rate_2"))


class TestBalancedElfOptions(unittest.TestCase):
    @staticmethod
    def f32(w: int) -> float:
        return struct.unpack("<f", struct.pack("<I", w))[0]

    def test_float_and_mips_encodings(self) -> None:
        self.assertEqual(self.f32(0x408B3333) * 2, self.f32(0x410B3333))  # 4.35f == 8.7f / 2 bit-exactly
        self.assertEqual(self.f32(0x42200000), 40.0)
        self.assertEqual(self.f32(0x42F00000), 120.0)
        self.assertEqual(elf_disasm.disassemble_instruction(0x2A020009, 0x003DB110), "slti    $v0, $s0, 0x9")
        self.assertEqual(elf_disasm.disassemble_instruction(0x3C01408B, 0x003DB178), "lui     $at, 0x408b")

    def test_four_face_table_adds_one_face_per_row(self) -> None:
        rows = [r[:] for r in STOCK_THROTTLE_ROWS]
        for w in rb.load_patch_spec()["patches"][-3]["writes"]:
            off = (int(w["vaddr"], 16) - 0x0054FEE0) // 4
            self.assertEqual(rows[off // 6][off % 6], int(w["expected_orig"], 16))
            rows[off // 6][off % 6] = int(w["patched"], 16)
        self.assertEqual([sum(r) for r in STOCK_THROTTLE_ROWS], [3] * 5)
        self.assertEqual([sum(r) for r in rows], [4] * 5)
        self.assertTrue(all(r[0] == 1 and r[2] == 0 for r in rows))  # face 0 every frame, face 2 never (as full table)


class TestVisualParity(unittest.TestCase):
    def test_compare_parity_flags_double_lining_and_exposure(self) -> None:
        base = {"luma": 100.0, "contrast": 40.0, "edge": 10.0}
        self.assertTrue(rb.compare_parity(base, dict(base))["ok"])
        self.assertFalse(rb.compare_parity(base, {**base, "edge": 13.0})["ok"])  # ghost edges
        self.assertFalse(rb.compare_parity(base, {**base, "edge": 8.0})["ok"])  # blur / lower LOD textures
        self.assertFalse(rb.compare_parity(base, {**base, "luma": 110.0})["ok"])

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_crop_stats_on_generated_frames(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as d:
            flat, lined = Path(d) / "a.png", Path(d) / "b.png"
            Image.new("RGB", (1280, 720), (90, 90, 90)).save(flat)
            im = Image.new("RGB", (1280, 720), (90, 90, 90))
            for x in range(0, 1280, 16):
                ImageDraw.Draw(im).line([(x, 0), (x, 719)], fill=(200, 200, 200))
            im.save(lined)
            a, b = rb.crop_stats(flat), rb.crop_stats(lined)
            self.assertAlmostEqual(a["luma"], 90.0, delta=0.5)
            self.assertEqual(a["edge"], 0.0)
            self.assertGreater(b["edge"], 1.0)


class TestLiveRetailResearchInvariants(unittest.TestCase):
    """Read-only SLUS_213.51 checks of the bottleneck research (skipped without the retail ISO)."""

    def test_stock_tables_and_untouched_split_screen_path(self) -> None:
        iso = _find_local_iso()
        if iso is None:
            self.skipTest("retail SLUS-21351 ISO not present (NFSMW_ISO)")
        elf = elf_disasm.ElfImage.from_iso(iso)
        rows = [[elf.read_u32(0x0054FEE0 + 4 * (6 * r + f)) for f in range(6)] for r in range(5)]
        self.assertEqual(rows, STOCK_THROTTLE_ROWS)
        self.assertEqual([elf.read_f32(0x00505178 + 4 * i) for i in range(4)], [120.0, 25.0, 20.0, 10.0])
        dis = {a: elf_disasm.disassemble_instruction(elf.read_u32(a), a) for a in (0x0020F64C, 0x003DB0FC, 0x003EC73C)}
        self.assertEqual(dis[0x0020F64C], "li      $a1, 5")  # eDisplayFrame % 5 row select
        self.assertEqual(dis[0x003DB0FC], "slti    $v0, $s0, 0x17")  # split-screen cull (not touched)
        self.assertEqual(dis[0x003EC73C], "lwc1    $f0, 0x5178($v0)")


if __name__ == "__main__":
    unittest.main()
