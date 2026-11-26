#!/usr/bin/env python3
"""Unit & integration tests for tools/export_music_pack.py and RTAudioEngine.swift music/reverb.

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
import tempfile
import unittest
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402
import export_music_pack as emp  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic EA-XA v2 (`adpcm_ea_r3`) Stream Builders (Zero ISO Required)
# ---------------------------------------------------------------------------


def _make_gstr_schl_chunk(sample_rate: int = 32000, channels: int = 2) -> bytes:
    """Build a valid EA SCHl + GSTR header chunk matching MW_MUSIC.MUS."""
    pt = bytearray(b"GSTR\x00\x00\x00\x00")
    # Tag 0x82: channels (1 byte BE)
    pt += bytes([0x82, 1, channels & 0xFF])
    # Tag 0x84: sample_rate (2 bytes BE)
    pt += bytes([0x84, 2]) + struct.pack(">H", sample_rate)
    # Terminator 0xFF + pad to 4-byte boundary
    pt.append(0xFF)
    while (8 + len(pt)) % 4 != 0:
        pt.append(0x00)
    total_sz = 8 + len(pt)
    return b"SCHl" + struct.pack("<I", total_sz) + bytes(pt)


def _pack_r3_adpcm_block(coef_idx: int, shift_raw: int, nibbles: Sequence[int]) -> bytes:
    """Pack a single 15-byte EA-XA R3 ADPCM block (28 signed 4-bit nibbles, high nibble first)."""
    nibs = list(nibbles) + [0] * (28 - len(nibbles))
    if len(nibs) != 28:
        raise ValueError("Expected 28 nibbles")
    hdr = ((coef_idx & 0x0F) << 4) | (shift_raw & 0x0F)
    out = bytearray([hdr])
    for i in range(0, 28, 2):
        hi = nibs[i] & 0x0F
        lo = nibs[i + 1] & 0x0F
        out.append((hi << 4) | lo)
    return bytes(out)


def _pack_r3_escape_block(h1: int, h2: int, samples: Sequence[int]) -> bytes:
    """Pack a 61-byte 0xEE big-endian raw PCM escape block."""
    if len(samples) != 28:
        raise ValueError("Expected 28 PCM samples for 0xEE block")
    return bytes([0xEE]) + struct.pack(">hh", h1, h2) + struct.pack(">28h", *samples)


def _encode_greedy_r3_channel(pcm: Sequence[int], h1: int = 0, h2: int = 0) -> Tuple[bytes, int, int]:
    """Encode a mono int16 sequence into valid 15-byte EA-XA R3 ADPCM blocks (coef 1, shift_raw 4)."""
    out = bytearray()
    c1, c2 = emp.EA_ADPCM_C1[1], emp.EA_ADPCM_C2[1]
    shift_raw = 4
    sh = 20 - shift_raw
    step = 1 << (sh - 8)  # 256

    n_blocks = (len(pcm) + 27) // 28
    for b in range(n_blocks):
        nibs: List[int] = []
        for k in range(28):
            idx = b * 28 + k
            target = int(pcm[idx]) if idx < len(pcm) else int(pcm[-1])
            pred = (h1 * c1 + h2 * c2 + 0x80) >> 8
            err = target - pred
            q = int(round(err / float(step)))
            q = max(-8, min(7, q))
            nibs.append(q)
            val = ((q << sh) + h1 * c1 + h2 * c2 + 0x80) >> 8
            val = max(-32768, min(32767, val))
            h2 = h1
            h1 = val
        out += _pack_r3_adpcm_block(1, shift_raw, nibs)
    return bytes(out), h1, h2


def _make_stereo_scdl_chunk(nsamp: int, ch_l: bytes, ch_r: bytes) -> bytes:
    """Build a valid stereo SCDl chunk with big-endian (nsamp, offL, offR)."""
    pad_l = b"\x00" * (len(ch_l) & 1)
    off_l = 0
    off_r = len(ch_l) + len(pad_l)
    body = struct.pack(">3I", nsamp, off_l, off_r) + ch_l + pad_l + ch_r
    pad_end = b"\x00" * ((4 - ((8 + len(body)) % 4)) % 4)
    total_sz = 8 + len(body) + len(pad_end)
    return b"SCDl" + struct.pack("<I", total_sz) + body + pad_end


# ---------------------------------------------------------------------------
# 1. Pure Offline Unit Tests (Always Pass with NFSMW_ISO=/nonexistent)
# ---------------------------------------------------------------------------


class TestMusicPackOffline(unittest.TestCase):
    """Offline-safe EA-XA R3 codec, hygiene, and Swift RTAudioEngine tests."""

    def test_decode_ea_r3_standard_adpcm_exact(self) -> None:
        # coef_idx=0 -> c1=0, c2=0; shift_raw=8 -> sh=12 -> ((nib << 12) + 0x80) >> 8 = nib * 16
        blk_l = _pack_r3_adpcm_block(0, 8, [1, -1, 2, -2, 7, -8, 0, 3])
        blk_r = _pack_r3_adpcm_block(0, 8, [-1, 1, -2, 2, -7, 7, 4, -4])
        stream = (
            _make_gstr_schl_chunk(32000, 2)
            + _make_stereo_scdl_chunk(28, blk_l, blk_r)
            + b"SCEl" + struct.pack("<I", 8)
        )
        pcm, stats = emp.decode_ea_r3_stream(stream)
        self.assertEqual(pcm.shape, (28, 2))
        self.assertEqual(stats["sample_rate"], 32000)
        self.assertEqual(stats["channels"], 2)
        self.assertEqual(stats["adpcm_blocks"], 2)
        self.assertEqual(stats["escape_blocks"], 0)
        self.assertEqual(stats["bad_coefs"], 0)
        self.assertEqual(pcm[:8, 0].tolist(), [16, -16, 32, -32, 112, -128, 0, 48])
        self.assertEqual(pcm[:8, 1].tolist(), [-16, 16, -32, 32, -112, 112, 64, -64])

    def test_decode_ea_r3_0xee_escape_and_predictor_handoff(self) -> None:
        # Block 0: 0xEE raw PCM escape block carrying exact BE16 samples and seeding (h1=1000, h2=500)
        # Block 1: ADPCM block with coef_idx=1 (c1=240, c2=0), shift_raw=12 (sh=8, delta=nib), nibs=[0]*28
        #          Verify first sample = (1000 * 240 + 0x80) >> 8 = 938 (proves h1 handoff from 0xEE!)
        raw_l = [i * 40 for i in range(28)]
        raw_r = [-i * 40 for i in range(28)]
        ee_l = _pack_r3_escape_block(1000, 500, raw_l)
        ee_r = _pack_r3_escape_block(-1000, -500, raw_r)
        adpcm_zero = _pack_r3_adpcm_block(1, 12, [0] * 28)

        stream = (
            _make_gstr_schl_chunk(32000, 2)
            + _make_stereo_scdl_chunk(56, ee_l + adpcm_zero, ee_r + adpcm_zero)
            + b"SCEl" + struct.pack("<I", 8)
        )
        pcm, stats = emp.decode_ea_r3_stream(stream)
        self.assertEqual(pcm.shape, (56, 2))
        self.assertEqual(stats["escape_blocks"], 2)
        self.assertEqual(stats["adpcm_blocks"], 2)
        self.assertEqual(pcm[:28, 0].tolist(), raw_l)
        self.assertEqual(pcm[:28, 1].tolist(), raw_r)
        # Verify exact predictor handoff from 0xEE header (h1=1000 -> (1000*240+128)>>8 = 938)
        self.assertEqual(int(pcm[28, 0]), (1000 * 240 + 0x80) >> 8)
        self.assertEqual(int(pcm[28, 1]), (-1000 * 240 + 0x80) >> 8)

    def test_decode_ea_r3_dc_rms_and_chunk_boundary_invariants(self) -> None:
        """Encode a continuous 440Hz/660Hz stereo sine across 4 SCDl chunks and verify zero-click continuity."""
        sr = 32000
        chunk_frames = 28 * 12  # 336 samples/chunk
        n_chunks = 4
        total_frames = chunk_frames * n_chunks
        t = np.arange(total_frames, dtype=np.float64) / float(sr)
        sig_l = np.round(14000.0 * np.sin(2.0 * math.pi * 440.0 * t)).astype(np.int16)
        sig_r = np.round(14000.0 * np.sin(2.0 * math.pi * 440.0 * t + 0.35)).astype(np.int16)

        parts = [_make_gstr_schl_chunk(sr, 2)]
        h1_l = h2_l = h1_r = h2_r = 0
        for ci in range(n_chunks):
            s0 = ci * chunk_frames
            s1 = s0 + chunk_frames
            enc_l, h1_l, h2_l = _encode_greedy_r3_channel(sig_l[s0:s1], h1_l, h2_l)
            enc_r, h1_r, h2_r = _encode_greedy_r3_channel(sig_r[s0:s1], h1_r, h2_r)
            parts.append(_make_stereo_scdl_chunk(chunk_frames, enc_l, enc_r))
        parts.append(b"SCEl" + struct.pack("<I", 8))

        pcm, stats = emp.decode_ea_r3_stream(b"".join(parts))
        self.assertEqual(pcm.shape, (total_frames, 2))
        self.assertFalse(stats["has_nan_or_inf"])
        self.assertGreater(stats["rms_l"], 0.25)
        self.assertGreater(stats["rms_r"], 0.25)
        self.assertLessEqual(stats["peak"], 0.60)
        self.assertLess(abs(stats["dc_l"]), 0.015)
        self.assertLess(abs(stats["dc_r"]), 0.015)
        self.assertGreater(stats["corr_lr"], 0.85)
        self.assertLessEqual(stats["boundary_mean_diff"], stats["intra_mean_diff"] * 2.0)

    def test_decode_ea_r3_rejects_invalid_and_truncated(self) -> None:
        with self.assertRaises(ValueError):
            emp.decode_ea_r3_stream(b"BAD!" + b"\x00" * 32)
        with self.assertRaises(ValueError):
            emp.decode_ea_r3_stream(_make_gstr_schl_chunk(32000, 2) + b"SCEl" + struct.pack("<I", 8))
        with self.assertRaises(ValueError):
            # Truncated 0xEE escape block
            emp.decode_ea_r3_channel(bytes([0xEE]) + b"\x00" * 20, 0, 21, 28)
        with self.assertRaises(ValueError):
            # Truncated 15-byte ADPCM block
            emp.decode_ea_r3_channel(bytes([0x14]) + b"\x00" * 5, 0, 6, 28)

    def test_audio_hygiene_ban_enforced(self) -> None:
        for ext in (".m4a", ".caf", ".aac", ".mp3"):
            self.assertIn(ext, audit_repo_clean.PROHIBITED_EXTENSIONS)

        gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pat in ("*.m4a", "*.caf", "*.aac", "*.mp3"):
            self.assertIn(pat, gitignore_text)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("song.m4a", "fx.caf", "stream.aac", "trax.mp3"):
                (root / name).write_bytes(b"dummy")
            _, issues = audit_repo_clean.audit_repository(root, target_dir=root)
            cats = {i.path: i.category for i in issues}
            for name in ("song.m4a", "fx.caf", "stream.aac", "trax.mp3"):
                self.assertEqual(cats.get(name), "prohibited-extension")

    def test_owned_files_hygiene_clean(self) -> None:
        owned = [
            Path("tools/audit_repo_clean.py"),
            Path("tools/export_music_pack.py"),
            Path("tests/test_music_pack.py"),
            Path("native_metal_rt/RTAudioEngine.swift"),
            Path("Makefile"),
        ]
        issues = audit_repo_clean.audit_files(REPO_ROOT, owned)
        self.assertEqual(issues, [], f"Hygiene issues: {[i.format_human() for i in issues]}")

    def test_swift_audio_engine_typecheck_and_smoke(self) -> None:
        """Verify RTAudioEngine.swift compiles cleanly with zero warnings & passes runtime smoke test."""
        if platform.system() != "Darwin" or shutil.which("swiftc") is None:
            self.skipTest("macOS swiftc toolchain unavailable")

        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        base_flags = [
            "-target",
            "arm64-apple-macosx14.0",
            "-sdk",
            sdk,
            "-framework",
            "Foundation",
            "-framework",
            "AudioToolbox",
            "-framework",
            "CoreAudio",
            "-framework",
            "AVFoundation",
            "-framework",
            "GameController",
        ]

        # 1. Strict typecheck (0 errors, 0 warnings)
        tc_cmd = [
            "swiftc",
            "-typecheck",
            "-warnings-as-errors",
            *base_flags,
            str(REPO_ROOT / "native_metal_rt" / "RTPhysics.swift"),
            str(REPO_ROOT / "native_metal_rt" / "RTAudioEngine.swift"),
        ]
        tc_res = subprocess.run(tc_cmd, capture_output=True, text=True)
        self.assertEqual(tc_res.returncode, 0, f"swiftc -typecheck failed:\n{tc_res.stderr}")

        # 2. Compiled Swift smoke test covering headless no-op, missing pack, track cycling, mute, pursuit mode, and tunnel occlusion
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Create synthetic manifest for offline-guaranteed verification
            synth_dir = td_path / "synth_music_pack"
            synth_dir.mkdir(parents=True)
            synth_manifest = {
                "schema_version": 1,
                "tracks": [
                    {"index": 0, "stream_index": 1, "artist": "Hush", "title": "Fired Up", "category": "ea_trax", "is_pursuit": False, "file": "tracks/track_01.m4a", "duration_sec": 199.0},
                    {"index": 1, "stream_index": 16, "artist": "Styles Of Beyond", "title": "Nine Thou (Superstars Remix)", "category": "ea_trax", "is_pursuit": False, "file": "tracks/track_16.m4a", "duration_sec": 227.9},
                    {"index": 2, "stream_index": 31, "artist": "Paul Linford & Chris Vrenna", "title": "Pursuit Theme 01", "category": "pursuit", "is_pursuit": True, "file": "tracks/track_31.m4a", "duration_sec": 116.2},
                ],
            }
            (synth_dir / "manifest.json").write_text(json.dumps(synth_manifest), encoding="utf-8")

            smoke_swift = td_path / "main.swift"
            smoke_swift.write_text(
                """
import Foundation

func assertCond(_ cond: Bool, _ msg: String) {
    if !cond {
        fputs("FAIL: \\(msg)\\n", stderr)
        exit(1)
    }
}

let args = CommandLine.arguments
let synthDir = URL(fileURLWithPath: args[1])
let missingDir = URL(fileURLWithPath: args[1]).appendingPathComponent("does_not_exist")
let realDirPath = args.count > 2 ? args[2] : ""

// A. Missing pack graceful no-op
do {
    let eng = RTAudioEngine(packDir: missingDir, musicPackDir: missingDir, headless: false)
    assertCond(!eng.isMusicPackLoaded, "missing pack should not report loaded")
    assertCond(eng.currentMusicTrack == nil, "missing pack should have nil track")
    assertCond(eng.nextMusicTrack() == nil, "missing pack next should be nil")
    assertCond(eng.prevMusicTrack() == nil, "missing pack prev should be nil")
    assertCond(eng.selectMusicTrack(index: 0) == nil, "missing pack select should be nil")
    eng.setTunnelOcclusion(0.8)
    eng.start()
    assertCond(!eng.isRunning, "missing pack start should stay non-running")
    eng.stop()
}

// B. Headless silent no-op with manifest metadata
do {
    let eng = RTAudioEngine(packDir: missingDir, musicPackDir: synthDir, headless: true)
    assertCond(eng.isMusicPackLoaded, "headless should load manifest metadata")
    assertCond(eng.loadedMusicTrackCount == 3, "expected 3 synthetic tracks")
    eng.start()
    assertCond(!eng.isRunning, "headless start MUST remain silent no-op (isRunning == false)")

    let t0 = eng.currentMusicTrack
    assertCond(t0?.artist == "Hush" && t0?.title == "Fired Up" && t0?.index == 0 && t0?.total == 2 && t0?.isPursuit == false, "initial track mismatch")

    let t1 = eng.nextMusicTrack()
    assertCond(t1?.artist == "Styles Of Beyond" && t1?.index == 1 && t1?.total == 2, "next track mismatch")

    let t0Back = eng.prevMusicTrack()
    assertCond(t0Back?.artist == "Hush" && t0Back?.index == 0, "prev track mismatch")

    assertCond(!eng.isMusicMuted, "initially unmuted")
    assertCond(eng.toggleMusicMute() == true && eng.isMusicMuted == true, "mute toggle on failed")
    assertCond(eng.toggleMusicMute() == false && eng.isMusicMuted == false, "mute toggle off failed")

    eng.setTunnelOcclusion(0.0)
    assertCond(abs(eng.reverbWetDryMix - 4.0) < 0.01, "open sky wetDry should be 4%")
    eng.setTunnelOcclusion(1.0)
    assertCond(abs(eng.reverbWetDryMix - 42.0) < 0.01, "tunnel wetDry should be 42%")
    eng.setTunnelOcclusion(0.5)
    assertCond(abs(eng.reverbWetDryMix - 23.0) < 0.01, "mid occlusion wetDry should be 23%")

    eng.setPursuitMusicMode(true)
    let tp = eng.currentMusicTrack
    assertCond(tp?.isPursuit == true && tp?.title == "Pursuit Theme 01" && tp?.total == 1, "pursuit mode switch failed")
    eng.setPursuitMusicMode(false)
    assertCond(eng.currentMusicTrack?.isPursuit == false && eng.currentMusicTrack?.artist == "Hush", "return from pursuit mode failed")

    eng.triggerStingerDuck(durationSeconds: 1.0, duckFactor: 0.25)
    eng.stop()
    assertCond(!eng.isRunning, "headless stop clean")
}

// C. Real build/music_pack verification when present on disk
if !realDirPath.isEmpty && FileManager.default.fileExists(atPath: realDirPath + "/manifest.json") {
    let realURL = URL(fileURLWithPath: realDirPath)
    let eng = RTAudioEngine(packDir: missingDir, musicPackDir: realURL, headless: false)
    assertCond(eng.isMusicPackLoaded, "real music pack should load")
    assertCond(eng.loadedMusicTrackCount >= 26, "real pack should have >= 26 tracks, got \\(eng.loadedMusicTrackCount)")

    let first = eng.currentMusicTrack
    assertCond(first?.artist == "Hush" && first?.title == "Fired Up", "track 0 should be Hush - Fired Up")

    // Verify permutation fix tracks (stream 10=index 9 Decadence, stream 11=index 10 We Control, stream 12=index 11 Skinnyman)
    let dec = eng.selectMusicTrack(index: 9)
    assertCond(dec?.artist == "Disturbed" && dec?.title == "Decadence", "index 9 should be Disturbed - Decadence")
    let wc = eng.selectMusicTrack(index: 10)
    assertCond(wc?.artist == "Hyper" && wc?.title == "We Control", "index 10 should be Hyper - We Control")
    let sk = eng.selectMusicTrack(index: 11)
    assertCond(sk?.artist == "Static-X" && sk?.title == "Skinnyman", "index 11 should be Static-X - Skinnyman")

    // Mute before starting live AVAudioEngine so test runs silently while exercising full AVAudioFile + AVAudioUnitReverb graph
    eng.isMusicMuted = true
    eng.setTunnelOcclusion(0.0)
    eng.start()
    _ = eng.nextMusicTrack()
    eng.setTunnelOcclusion(1.0)
    assertCond(abs(eng.reverbWetDryMix - 42.0) < 0.01, "live tunnel wetDry should be 42%")
    eng.setPursuitMusicMode(true)
    assertCond(eng.currentMusicTrack?.isPursuit == true, "live pursuit switch failed")
    eng.setPursuitMusicMode(false)
    assertCond(eng.currentMusicTrack?.isPursuit == false, "live pursuit exit failed")
    Thread.sleep(forTimeInterval: 0.05)
    eng.stop()
    assertCond(!eng.isRunning, "engine should stop cleanly")
    print("REAL_PACK_OK tracks=\\(eng.loadedMusicTrackCount)")
}

print("SWIFT_SMOKE_PASS")
""",
                encoding="utf-8",
            )

            smoke_bin = td_path / "audio_smoke"
            build_cmd = [
                "swiftc",
                "-warnings-as-errors",
                *base_flags,
                str(REPO_ROOT / "native_metal_rt" / "RTPhysics.swift"),
                str(REPO_ROOT / "native_metal_rt" / "RTAudioEngine.swift"),
                str(smoke_swift),
                "-o",
                str(smoke_bin),
            ]
            build_res = subprocess.run(build_cmd, capture_output=True, text=True)
            self.assertEqual(build_res.returncode, 0, f"swiftc smoke build failed:\n{build_res.stderr}")

            real_pack = REPO_ROOT / "build" / "music_pack"
            run_args = [str(smoke_bin), str(synth_dir)]
            if (real_pack / "manifest.json").is_file():
                run_args.append(str(real_pack))
            run_res = subprocess.run(run_args, capture_output=True, text=True)
            self.assertEqual(
                run_res.returncode,
                0,
                f"Swift smoke binary failed:\nstdout={run_res.stdout}\nstderr={run_res.stderr}",
            )
            self.assertIn("SWIFT_SMOKE_PASS", run_res.stdout)


# ---------------------------------------------------------------------------
# 2. Live ISO & Exported Music Pack Verification Tests
# ---------------------------------------------------------------------------


EXPECTED_LICENSED_BY_STREAM = {
    1: ("Hush", "Fired Up"),
    2: ("The Perceptionists", "Let's Move"),
    3: ("DJ Spooky and Dave Lombardo", "B-Side Wins Again feat. Chuck D"),
    4: ("Bullet For My Valentine", "Hand Of Blood"),
    5: ("Lupe Fiasco", "Tilted"),
    6: ("The Prodigy", "You'll Be Under My Wheels"),
    7: ("Celldweller", "One Good Reason"),
    8: ("Rock", "I Am Rock"),
    9: ("Avenged Sevenfold", "Blinded In Chains"),
    # Critical ev[10..12] permutation fix verified via MPF node_id indirection:
    10: ("Disturbed", "Decadence"),
    11: ("Hyper", "We Control"),
    12: ("Static-X", "Skinnyman"),
    13: ("Suni Clay", "In A Hood Near You"),
    14: ("T.I. Presents The P$$C", "Do Ya Thang"),
    15: ("Celldweller feat. Styles Of Beyond", "Shapeshifter"),
    16: ("Styles Of Beyond", "Nine Thou (Superstars Remix)"),
    17: ("Juvenile", "Sets Go Up"),
    18: ("Mastodon", "Blood And Thunder"),
    19: ("Jamiroquai", "Feels Just Like It Should (Timo Maas Remix)"),
    20: ("The Roots and BT", "Tao Of The Machine (Scott Humphrey's Remix)"),
    21: ("Ils", "Feed The Addiction"),
    22: ("Diesel Boy + Kaos", "Barrier Break"),
    23: ("Evol Intent, Mayhem & Thinktank", "Broken Sword"),
    24: ("Stratus", "You Must Follow (Evol Intent VIP)"),
    25: ("Paul Linford and Chris Vrenna", "Most Wanted Mash Up"),
    26: ("Paul Linford and Chris Vrenna", "The Mann"),
}


class TestMusicPackLiveISO(unittest.TestCase):
    """Validates live ISO MPF+VLT mapping and exported build/music_pack/ when available."""

    def test_live_iso_mpf_vlt_26_licensed_and_14_score_mapping(self) -> None:
        iso = emp.resolve_iso_path()
        if iso is None:
            self.skipTest("Local untouched PS2 ISO unavailable (offline mode)")

        arc = emp.PS2NFSMWArchive(iso)
        mpf_data = arc.read_entry(arc.find(emp.MPF_VIRTUAL_PATH))
        vlt_bytes = arc.read_entry(arc.find(emp.ATTR_VIRTUAL_PATH))
        catalog, streams = emp.parse_mpf_track_catalog(mpf_data, vlt_bytes)

        self.assertEqual(len(streams), 53)
        self.assertEqual(len(catalog), 40)

        licensed = [t for t in catalog if t["category"] == "ea_trax"]
        score = [t for t in catalog if t["category"] in ("pursuit", "score")]
        self.assertEqual(len(licensed), 26)
        self.assertEqual(len(score), 14)

        by_stream = {t["stream_index"]: (t["artist"], t["title"]) for t in licensed}
        self.assertEqual(by_stream, EXPECTED_LICENSED_BY_STREAM)

        # Verify score streams cover [31..44] with is_pursuit=True
        score_streams = sorted(t["stream_index"] for t in score)
        self.assertEqual(score_streams, list(range(31, 45)))
        self.assertTrue(all(t["is_pursuit"] for t in score))

    def test_exported_music_pack_manifest_and_m4a_validity(self) -> None:
        pack_dir = REPO_ROOT / "build" / "music_pack"
        mf_path = pack_dir / "manifest.json"
        if not mf_path.is_file():
            self.skipTest("build/music_pack/manifest.json not exported yet")

        mf = json.loads(mf_path.read_text(encoding="utf-8"))
        self.assertEqual(mf["schema_version"], 1)
        self.assertEqual(mf["sample_rate"], 32000)
        self.assertEqual(mf["channels"], 2)
        self.assertEqual(mf["licensed_count"], 26)
        self.assertEqual(mf["score_count"], 14)
        self.assertEqual(mf["total_tracks"], 40)
        self.assertEqual(len(mf["tracks"]), 40)

        licensed_seen = {}
        for t in mf["tracks"]:
            m4a = pack_dir / t["file"]
            self.assertTrue(m4a.is_file(), f"Missing track file: {m4a}")
            self.assertGreater(m4a.stat().st_size, 100_000, f"Track too small: {m4a}")
            self.assertEqual(t["sample_rate"], 32000)
            self.assertEqual(t["channels"], 2)
            self.assertGreater(t["duration_sec"], 60.0)

            ds = t.get("decode_stats", {})
            self.assertEqual(ds.get("bad_coefs", 0), 0)
            min_rms = 0.20 if t["category"] == "ea_trax" else 0.02
            self.assertGreater(ds.get("rms_l", 0.0), min_rms)
            self.assertGreater(ds.get("rms_r", 0.0), min_rms)
            self.assertLess(abs(ds.get("dc_l", 0.0)), 0.03)
            self.assertLess(abs(ds.get("dc_r", 0.0)), 0.03)
            self.assertLessEqual(
                ds.get("boundary_mean_diff", 0.0),
                max(1.0, ds.get("intra_mean_diff", 1.0)) * 1.25,
            )

            if t["category"] == "ea_trax":
                licensed_seen[t["stream_index"]] = (t["artist"], t["title"])
                self.assertFalse(t["is_pursuit"])
            else:
                self.assertTrue(t["is_pursuit"])

        self.assertEqual(licensed_seen, EXPECTED_LICENSED_BY_STREAM)

        # Verify actual macOS AAC M4A decodability via built-in `afinfo` on sample tracks (incl. permutation trio)
        if platform.system() == "Darwin" and shutil.which("afinfo") is not None:
            for sidx in (1, 10, 11, 12, 16, 31, 44):
                m4a = pack_dir / "tracks" / f"track_{sidx:02d}.m4a"
                res = subprocess.run(["afinfo", str(m4a)], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"afinfo failed on {m4a}: {res.stderr}")
                self.assertIn("32000 Hz", res.stdout)
                self.assertIn("2 ch", res.stdout)
                self.assertIn("aac", res.stdout.lower())

    def test_limit_and_skip_existing_without_force(self) -> None:
        iso = emp.resolve_iso_path()
        if iso is None:
            self.skipTest("Local untouched PS2 ISO unavailable (offline mode)")

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "mini_pack"
            r1 = emp.export_music_pack(iso, out_dir, workers=2, limit=2, force=False, quiet=True)
            self.assertEqual(r1["encoded_count"], 2)
            self.assertEqual(r1["skipped_count"], 0)
            self.assertEqual(r1["manifest"]["total_tracks"], 2)

            # Second run without --force should skip both tracks immediately and preserve decode_stats
            r2 = emp.export_music_pack(iso, out_dir, workers=2, limit=2, force=False, quiet=True)
            self.assertEqual(r2["encoded_count"], 0)
            self.assertEqual(r2["skipped_count"], 2)
            self.assertTrue(all("decode_stats" in t for t in r2["manifest"]["tracks"]))


if __name__ == "__main__":
    unittest.main()
