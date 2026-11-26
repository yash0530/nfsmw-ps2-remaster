#!/usr/bin/env python3
"""Unit & integration tests for tools/export_gameplay_pack.py, RTPhysics.swift, and RTAudioEngine.swift.

Passes cleanly both WITH and WITHOUT an untouched PS2 ISO (`NFSMW_ISO=/nonexistent`).
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import unittest
import wave
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_gameplay_pack as gp  # noqa: E402


def _make_synthetic_xas_v0_block(s0: int, s1: int, coef_idx: int = 1, shift: int = 12, nibbles: Sequence[int] = ()) -> bytes:
    """Pack a single 19-byte EA-XAS v0 block (32 samples)."""
    h0 = (s0 & ~0x0F) | (coef_idx & 0x0F)
    h1 = (s1 & ~0x0F) | (shift & 0x0F)
    # Clamp signed int16 packing
    h0_i16 = ((h0 + 32768) % 65536) - 32768
    h1_i16 = ((h1 + 32768) % 65536) - 32768
    nibs = list(nibbles) + [0] * (30 - len(nibbles))
    raw = bytearray(struct.pack("<hh", h0_i16, h1_i16))
    for i in range(0, 30, 2):
        hi = nibs[i] & 0x0F
        lo = nibs[i + 1] & 0x0F
        raw.append((hi << 4) | lo)
    return bytes(raw)


class TestGameplayPackSynthetic(unittest.TestCase):
    """Pure offline tests that run identically in CI with NFSMW_ISO=/nonexistent."""

    def test_decode_ea_xas_v0_exact_block(self) -> None:
        # Coef 0 = (0, 0), shift = 8 -> delta = (nib << 12) >> 8 = nib * 16
        blk = _make_synthetic_xas_v0_block(160, 320, coef_idx=0, shift=8, nibbles=[1, -1, 2, -2, 7, -8])
        gdata = struct.pack("<I", 32) + blk
        pcm, stats = gp.decode_ea_xas_v0(gdata)
        self.assertEqual(len(pcm), 32)
        self.assertEqual(stats["bad_coefs"], 0)
        self.assertEqual(stats["bad_shifts"], 0)
        self.assertEqual(int(pcm[0]), 160)
        self.assertEqual(int(pcm[1]), 320)
        self.assertEqual(int(pcm[2]), 16)
        self.assertEqual(int(pcm[3]), -16)
        self.assertEqual(int(pcm[4]), 32)
        self.assertEqual(int(pcm[5]), -32)
        self.assertEqual(int(pcm[6]), 112)
        self.assertEqual(int(pcm[7]), -128)

    def test_decode_ea_xas_v0_rejects_truncated(self) -> None:
        with self.assertRaises(ValueError):
            gp.decode_ea_xas_v0(b"\x10\x00\x00\x00" + b"\x00" * 10)

    def test_psola_sweep_synthetic_engine(self) -> None:
        """Build a synthetic multi-harmonic engine chirp with exact 720-deg crank grain boundaries."""
        sr = 24000
        min_rpm = 1200.0
        max_rpm = 8000.0
        # Generate continuous crank-phase engine sweep from 1200 to 8000 RPM over 5 seconds
        n_src = sr * 5
        t = np.arange(n_src, dtype=np.float64) / float(sr)
        rpm_inst = min_rpm + (max_rpm - min_rpm) * (t / 5.0)
        # 4-stroke 2-rev cycle frequency = rpm / 120 Hz; V8 firing = 4th harmonic + harmonics
        cyc_freq = rpm_inst / 120.0
        phase = np.cumsum(cyc_freq / float(sr))  # cycles (integer crossings = 720-deg boundaries)
        wave_f = (
            0.45 * np.sin(2.0 * math.pi * 4.0 * phase)
            + 0.30 * np.sin(2.0 * math.pi * 8.0 * phase)
            + 0.15 * np.sin(2.0 * math.pi * 12.0 * phase)
        )
        pcm = np.clip(np.round(wave_f * 26000.0), -32768, 32767).astype(np.int16)

        # Extract exact integer-cycle grain boundaries
        cyc_int = np.floor(phase).astype(np.int64)
        grain_table = [0] + (np.nonzero(np.diff(cyc_int) > 0)[0] + 1).tolist()
        rpm_table = [int(round(i * (n_src - 1) / 50.0)) for i in range(51)]

        synth_gnsu = {
            "sample_rate": sr,
            "min_rpm": min_rpm,
            "max_rpm": max_rpm,
            "rpm_table": rpm_table,
            "grain_table": grain_table,
            "pcm": pcm,
        }
        out_pcm, metrics = gp.render_ginsu_psola_sweep(
            synth_gnsu,
            rpm_start=1200.0,
            rpm_end=8000.0,
            duration_sec=3.0,
            out_sample_rate=48000,
        )
        self.assertEqual(len(out_pcm), 48000 * 3)
        self.assertFalse(metrics["has_nan_or_inf"])
        self.assertGreater(metrics["rms"], 0.15)
        self.assertLessEqual(metrics["peak"], 0.96)
        self.assertGreater(metrics["grains_emitted"], 80)
        self.assertLess(metrics["p995_sample_step"], 0.25)

        # Verify spectral centroid rises monotonically from Q1 -> Q4
        x = out_pcm.astype(np.float64) / 32768.0
        q = len(x) // 4
        centroids = []
        for i in range(4):
            seg = x[i * q : (i + 1) * q]
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            freqs = np.fft.rfftfreq(len(seg), 1.0 / 48000.0)
            centroids.append(float(np.sum(freqs * spec) / max(1e-9, np.sum(spec))))
        self.assertLess(centroids[0], centroids[1])
        self.assertLess(centroids[1], centroids[2])
        self.assertLess(centroids[2], centroids[3])

    def test_swift_physics_and_audio_typecheck(self) -> None:
        """Verify RTPhysics.swift and RTAudioEngine.swift typecheck cleanly on macOS."""
        if platform.system() != "Darwin" or shutil.which("swiftc") is None:
            self.skipTest("macOS swiftc toolchain unavailable")
        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        cmd = [
            "swiftc",
            "-typecheck",
            "-target",
            "arm64-apple-macosx14.0",
            "-sdk",
            sdk,
            "-framework",
            "Foundation",
            "-framework",
            "simd",
            "-framework",
            "AVFoundation",
            "-framework",
            "GameController",
            str(REPO_ROOT / "native_metal_rt" / "RTPhysics.swift"),
            str(REPO_ROOT / "native_metal_rt" / "RTAudioEngine.swift"),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"swiftc typecheck failed:\n{res.stderr}")


class TestGameplayPackExported(unittest.TestCase):
    """Validates build/gameplay_pack/ when exported from a local untouched PS2 ISO."""

    def setUp(self) -> None:
        self.pack_dir = REPO_ROOT / "build" / "gameplay_pack"
        self.iso = gp.resolve_iso_path()
        if not (self.pack_dir / "manifest.json").is_file():
            if self.iso is None:
                self.skipTest("Neither build/gameplay_pack nor local PS2 ISO present")
            gp.export_gameplay_pack(self.iso, self.pack_dir)

    def test_pack_size_and_manifest(self) -> None:
        mf = json.loads((self.pack_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertLess(mf["total_mb"], 10.0)
        self.assertGreaterEqual(mf["vlt"]["playable_count"], 40)
        self.assertGreaterEqual(mf["vlt"]["police_count"], 3)
        self.assertEqual(mf["vlt"]["pursuit_levels"], 5)
        self.assertGreaterEqual(mf["audio"]["engine_count"], 8)
        self.assertGreaterEqual(mf["audio"]["sfx_count"], 15)
        self.assertEqual(mf["ai_vehicles"]["archetype_count"], 7)

    def test_vlt_vehicles_hero_and_cop_stats(self) -> None:
        vlt = json.loads((self.pack_dir / "vlt_vehicles.json").read_text(encoding="utf-8"))
        vehs = vlt["vehicles"]
        self.assertIn("bmwm3gtre46", vehs)
        self.assertIn("carreragt", vehs)
        self.assertIn("gallardo", vehs)
        self.assertIn("copmidsize", vehs)
        self.assertIn("copgto", vehs)
        self.assertIn("copsport", vehs)

        m3 = vehs["bmwm3gtre46"]
        self.assertAlmostEqual(m3["mass_kg"], 1300.0, delta=5.0)
        self.assertAlmostEqual(m3["transmission"]["torque_split"], 0.0, delta=0.01)
        self.assertTrue(m3["audio"]["tranny_whine"])
        self.assertEqual(len(m3["engine"]["torque_curve"]), 9)

        gal = vehs["gallardo"]
        self.assertAlmostEqual(gal["transmission"]["torque_split"], 0.5, delta=0.05)

    def test_m3_sweep_wav_self_test(self) -> None:
        wav_path = self.pack_dir / "test_m3_sweep.wav"
        self.assertTrue(wav_path.is_file())
        with wave.open(str(wav_path), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            sr = wf.getframerate()
            self.assertEqual(sr, 48000)
            raw = wf.readframes(wf.getnframes())
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
        self.assertEqual(len(x), 48000 * 3)
        self.assertTrue(np.all(np.isfinite(x)))
        rms = float(np.sqrt(np.mean(x * x)))
        peak = float(np.max(np.abs(x)))
        p995 = float(np.percentile(np.abs(np.diff(x)), 99.5))
        self.assertGreater(rms, 0.20)
        self.assertLessEqual(peak, 0.96)
        self.assertLess(p995, 0.25)

        # Verify spectral centroid rises monotonically across Q1..Q4
        q = len(x) // 4
        centroids = []
        for i in range(4):
            seg = x[i * q : (i + 1) * q]
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            freqs = np.fft.rfftfreq(len(seg), 1.0 / float(sr))
            centroids.append(float(np.sum(freqs * spec) / max(1e-9, np.sum(spec))))
        self.assertLess(centroids[0], centroids[1])
        self.assertLess(centroids[1], centroids[2])
        self.assertLess(centroids[2], centroids[3])

    def test_audio_manifest_zero_xas_errors(self) -> None:
        am = json.loads((self.pack_dir / "audio" / "audio_manifest.json").read_text(encoding="utf-8"))
        for key in ("tvr_cerb", "por_car_gt", "lam_gallardo"):
            self.assertIn(key, am["engines"])
            eng = am["engines"][key]
            self.assertEqual(eng["accel"]["xas_stats"]["bad_coefs"], 0)
            self.assertEqual(eng["accel"]["xas_stats"]["bad_shifts"], 0)
            self.assertTrue((self.pack_dir / eng["accel"]["wav"]).is_file())
            self.assertIsNotNone(eng["decel"])
            self.assertTrue((self.pack_dir / eng["decel"]["wav"]).is_file())
        for sfx_key in ("gearbox_whine", "turbo_spool", "turbo_bov", "nos_loop", "skid_loop", "siren_wail", "speedbreaker_fx"):
            self.assertIn(sfx_key, am["sfx"])
            self.assertTrue((self.pack_dir / am["sfx"][sfx_key]["wav"]).is_file())

    def test_ai_vehicles_grounded_and_valid_skins(self) -> None:
        ai = json.loads((self.pack_dir / "ai_vehicles" / "manifest.json").read_text(encoding="utf-8"))
        archs = ai["archetypes"]
        for key in ("copmidsize", "copgto", "copsport", "traftaxi", "traf4dseda", "trafsuva", "trafminivan"):
            self.assertIn(key, archs)
            a = archs[key]
            self.assertTrue((self.pack_dir / a["body_obj"]).is_file())
            self.assertGreater(a["total_triangles"], 400)
            self.assertLessEqual(abs(a["body"]["aabb_min"][1]), 0.05)
            if a["role"] == "police":
                self.assertIsNotNone(a["lightbar_obj"])
                self.assertTrue((self.pack_dir / a["lightbar_obj"]).is_file())
                self.assertIsNotNone(a["lightbar_center_yup"])
            if a["skin_png"]:
                skin_p = self.pack_dir / a["skin_png"]
                self.assertTrue(skin_p.is_file())
                with Image.open(skin_p) as im:
                    im.load()
                    self.assertGreater(im.width, 0)
                    self.assertGreater(im.height, 0)


if __name__ == "__main__":
    unittest.main()
