#!/usr/bin/env python3
"""
Unit tests for tools/macos_video_tools.swift, tools/video_qa_pipeline.py,
and --record-video integration in tools/race_flywheel.py.

Runs 100% offline using native AVFoundation synthetic H.264 MP4 generation
(no PS2 ISO or live PCSX2 launch required).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402
import race_flywheel  # noqa: E402
import video_qa_pipeline  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_mock_pcsx2_env(root: Path) -> tuple[Path, Path, Path]:
    """Create an isolated mock PCSX2.app, user data dir, and fake ISO file."""
    app_dir = root / "PCSX2.app"
    bin_path = app_dir / "Contents" / "MacOS" / "PCSX2"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bin_path.chmod(0o755)
    (app_dir / "Contents" / "Info.plist").write_text(
        "<plist><dict><key>CFBundleShortVersionString</key><string>v2.6.3</string></dict></plist>",
        encoding="utf-8",
    )

    data_dir = root / "PCSX2Data"
    for sub in ("inis", "gamesettings", "patches", "memcards", "snaps", "logs", "resources"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    bios_dir = root / "ps2_bios"
    bios_dir.mkdir(parents=True, exist_ok=True)
    (bios_dir / "scph39001.bin").write_bytes(b"\x00" * 32)

    (data_dir / "memcards" / "Mcd001.ps2").write_bytes(b"MCD1_ORIGINAL_DATA")
    (data_dir / "memcards" / "Mcd002.ps2").write_bytes(b"MCD2_ORIGINAL_DATA")

    ini_path = data_dir / "inis" / "PCSX2.ini"
    ini_path.write_text(
        "\n".join(
            [
                "[UI]",
                "ConfirmShutdown = true",
                "PauseOnFocusLoss = true",
                "[Folders]",
                "Bios = ../ps2_bios",
                "Snapshots = snaps",
                "Logs = logs",
                "MemoryCards = memcards",
                "Videos = videos",
                "[Filenames]",
                "BIOS = scph39001.bin",
                "[EmuCore/GS]",
                "EnableVideoCapture = false",
                "CaptureContainer = avi",
                "VideoCaptureWidth = 640",
                "VideoCaptureHeight = 480",
                "VideoCaptureBitrate = 4000",
                "[Hotkeys]",
                "Screenshot = Keyboard/F8",
                "TogglePause = Keyboard/Space",
                "[Pad1]",
                "Type = DualShock2",
                "Cross = SDL-0/FaceSouth",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (data_dir / "gamesettings" / "SLUS-21351_2DAD2310.ini").write_text(
        "[EmuCore/GS]\nRenderer = 17\nupscale_multiplier = 6\n",
        encoding="utf-8",
    )
    (data_dir / "patches" / "SLUS-21351_2DAD2310.pnach").write_text(
        "gametitle=NFSMW\n",
        encoding="utf-8",
    )

    iso_path = root / "test.iso"
    iso_path.write_bytes(b"\x00" * 4096)
    return app_dir, data_dir, iso_path


class TestVideoToolsSwiftAndPipeline(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS AppKit/AVFoundation")
    def test_video_tools_compiles_synth_probe_transcode_and_extract_qa(self) -> None:
        bin_path = video_qa_pipeline.ensure_video_tools_binary()
        self.assertTrue(bin_path.is_file())
        self.assertTrue(os.access(bin_path, os.X_OK))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            synth_mp4 = root / "synthetic_session.mp4"

            # Synthesize a 6.5s video with:
            # - 1.2s black head (boot screen)
            # - active motion middle
            # - highlight clip spike at t=2.5s
            # - amber cast anomaly at t=3.5s
            # - 2.0s static/frozen tail
            syn = video_qa_pipeline.synthesize_test_video(
                synth_mp4,
                duration_sec=6.5,
                fps=15,
                width=320,
                height=180,
                black_head_sec=1.2,
                static_tail_sec=2.0,
                clip_at_sec=2.5,
                amber_at_sec=3.5,
                video_tools_bin=bin_path,
            )
            self.assertTrue(syn["ok"], syn)
            self.assertTrue(synth_mp4.is_file())
            self.assertGreater(synth_mp4.stat().st_size, 1024)

            # Probe valid MP4
            probe = video_qa_pipeline.probe_video(synth_mp4, video_tools_bin=bin_path)
            self.assertTrue(probe["valid"], probe)
            self.assertEqual(probe["width"], 320)
            self.assertEqual(probe["height"], 180)
            self.assertGreaterEqual(probe["duration_sec"], 6.0)
            self.assertTrue(probe["can_decode_frame"])

            # Probe truncated/corrupt MP4 fails cleanly
            bad_mp4 = root / "truncated.mp4"
            bad_mp4.write_bytes(synth_mp4.read_bytes()[:256])
            bad_probe = video_qa_pipeline.probe_video(bad_mp4, video_tools_bin=bin_path)
            self.assertFalse(bad_probe["valid"])

            # Transcode to compact H.264 MP4
            reenc_mp4 = root / "reencoded.mp4"
            tc = video_qa_pipeline.transcode_to_mp4(
                synth_mp4, reenc_mp4, preset="medium", video_tools_bin=bin_path
            )
            self.assertTrue(tc["ok"], tc)
            self.assertTrue(video_qa_pipeline.probe_video(reenc_mp4, video_tools_bin=bin_path)["valid"])

            # Run full extract-qa with 0.5s interval -> 13+ frames -> multi-page sheets with 2x2 grid
            qa_out = root / "qa_out"
            qa = video_qa_pipeline.extract_qa(
                synth_mp4,
                qa_out,
                interval_sec=0.5,
                sheet_cols=2,
                sheet_rows=2,
                tile_width=240,
                video_tools_bin=bin_path,
            )
            self.assertTrue(qa["ok"], qa)
            summary = qa["summary"]
            self.assertGreaterEqual(summary["valid_frames"], 10)
            self.assertEqual(summary["corrupt_frames_quarantined"], 0)
            self.assertGreater(summary["black_screen_frames"], 0)
            self.assertGreater(summary["black_screen_duration_sec"], 0.0)
            self.assertTrue(summary["motion_detected"])
            self.assertGreater(summary["max_motion_mae"], 5.0)
            self.assertGreater(summary["frozen_frame_pairs"], 0)
            self.assertGreater(summary["max_consecutive_frozen_sec"], 0.5)
            self.assertGreater(summary["highlight_clip_spike_count"], 0)
            self.assertGreater(summary["color_cast_anomaly_count"], 0)

            # Multi-page contact sheets exist and pass strict Pillow validation
            sheets = qa["qa_sheets"]
            self.assertGreaterEqual(len(sheets), 3)
            for sh in sheets:
                sh_path = Path(sh["path"])
                self.assertTrue(sh_path.is_file())
                ok, _, err = video_qa_pipeline.validate_and_quarantine_png(sh_path)
                self.assertTrue(ok, err)

            # Telemetry JSON written and valid
            tel_path = Path(qa["telemetry_path"])
            self.assertTrue(tel_path.is_file())
            parsed = json.loads(tel_path.read_text(encoding="utf-8"))
            self.assertEqual(len(parsed["timeline"]), summary["total_frames_extracted"])

    def test_corrupt_png_quarantine_in_frame_dir_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "burst_frames"
            src_dir.mkdir(parents=True, exist_ok=True)

            # Write 2 valid PNGs + 1 truncated corrupt PNG
            for idx, col in enumerate([(100, 140, 185), (145, 165, 120)], start=1):
                arr = np.full((90, 160, 3), col, dtype=np.uint8)
                arr[:30, :, :] = (220, 220, 220)
                Image.fromarray(arr).save(src_dir / f"frame_{idx:02d}_t{idx:.1f}s.png", format="PNG")

            corrupt_src = src_dir / "frame_03_t3.0s.png"
            arr = np.full((90, 160, 3), (120, 120, 120), dtype=np.uint8)
            Image.fromarray(arr).save(corrupt_src, format="PNG")
            raw = corrupt_src.read_bytes()
            corrupt_src.write_bytes(raw[: max(32, len(raw) // 3)])

            out_dir = root / "qa_burst_out"
            qa = video_qa_pipeline.extract_qa(src_dir, out_dir, interval_sec=1.0)
            self.assertTrue(qa["ok"])
            self.assertEqual(qa["summary"]["valid_frames"], 2)
            self.assertEqual(qa["summary"]["corrupt_frames_quarantined"], 1)
            self.assertIn("CORRUPT_FRAMES_QUARANTINED", qa["summary"]["health_flags"])

            # Corrupt PNG must be quarantined as .corrupt, never left as .png in qa_frames/
            qa_frames_dir = out_dir / "qa_frames"
            self.assertFalse((qa_frames_dir / "frame_03_t3.0s.png").exists())
            self.assertTrue((qa_frames_dir / "frame_03_t3.0s.png.corrupt").is_file())
            self.assertEqual(len(qa["qa_sheets"]), 1)

    def test_highlight_clip_threshold_catches_7_pct_sky_blowout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "clip_frames"
            src_dir.mkdir(parents=True, exist_ok=True)

            # Frame 1: clean balanced frame (0% clipped)
            arr1 = np.full((100, 100, 3), (110, 120, 130), dtype=np.uint8)
            arr1[50:, :, :] = (70, 75, 80)
            Image.fromarray(arr1).save(src_dir / "frame_01_t0.0s.png", format="PNG")

            # Frame 2: ~7.5% pure-white sky blowout (above 5.0% threshold, below old 12.0%)
            arr2 = np.full((100, 100, 3), (110, 120, 130), dtype=np.uint8)
            arr2[:10, :75, :] = (255, 255, 255)  # 750 / 10000 = 7.5%
            Image.fromarray(arr2).save(src_dir / "frame_02_t1.5s.png", format="PNG")

            out_dir = root / "qa_clip_out"
            qa = video_qa_pipeline.extract_qa(src_dir, out_dir, interval_sec=1.5)
            self.assertTrue(qa["ok"])
            self.assertEqual(qa["summary"]["highlight_clip_spike_count"], 1)
            self.assertIn("HIGHLIGHT_CLIPPING_SPIKES", qa["summary"]["health_flags"])
            self.assertIn("HIGHLIGHT_CLIP", qa["timeline"][1]["flags"])
            self.assertGreaterEqual(qa["timeline"][1]["highlight_clip_pct"], 5.0)
            self.assertLess(qa["timeline"][1]["highlight_clip_pct"], 12.0)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS AppKit/AVFoundation")
    def test_key_driver_supports_f12_and_v_hotkeys(self) -> None:
        bin_path = race_flywheel.ensure_key_driver_binary()
        res = subprocess.run(
            [str(bin_path), "--dry-run", "--json", "--actions", "f12,tap:v:120,tap:f12:120"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["actions"], ["f12", "tap:v:120", "tap:f12:120"])


class TestRaceFlywheelVideoWiring(unittest.TestCase):
    def test_sandbox_video_ini_wiring_keeps_global_ini_and_memcards_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root)
            global_ini = data_dir / "inis" / "PCSX2.ini"
            mcd1 = data_dir / "memcards" / "Mcd001.ps2"
            ini_sha = _sha256(global_ini)
            mcd1_sha = _sha256(mcd1)

            fake_iso_report = {
                "volume_id": "NFSMW",
                "entry_count": 10,
                "boot_elf": {"name": "SLUS_213.51", "pcsx2_crc": "2DAD2310"},
            }
            orig_inspect = race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso
            race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = (
                lambda _p, compute_sha256=False: fake_iso_report
            )
            try:
                flywheel_dir = root / "flywheel"
                iter_dir = flywheel_dir / "runs" / "iter_vid_ini"
                preflight = race_flywheel.pcsx2_live_verify.run_preflight(
                    iso_path, pcsx2_app=app_dir, pcsx2_data_dir=data_dir
                )
                sb = race_flywheel.prepare_flywheel_sandbox(
                    flywheel_dir=flywheel_dir,
                    iter_dir=iter_dir,
                    data_dir=data_dir,
                    preflight=preflight,
                    record_video=True,
                    video_mode="pcsx2",
                    video_hotkey="f12",
                    video_width=1280,
                    video_height=720,
                    video_bitrate=8000,
                )
            finally:
                race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect

            self.assertEqual(_sha256(global_ini), ini_sha)
            self.assertEqual(_sha256(mcd1), mcd1_sha)
            self.assertTrue(sb["record_video"])
            self.assertEqual(Path(sb["videos_dir"]), (iter_dir / "videos").resolve())

            sb_ini = Path(sb["sandbox_ini"]).read_text(encoding="utf-8")
            self.assertIn(f"Videos = {(iter_dir / 'videos').resolve()}", sb_ini)
            self.assertIn("EnableVideoCapture = true", sb_ini)
            self.assertIn("EnableAudioCapture = true", sb_ini)
            self.assertIn("CaptureContainer = mp4", sb_ini)
            self.assertIn("VideoCaptureWidth = 1280", sb_ini)
            self.assertIn("VideoCaptureHeight = 720", sb_ini)
            self.assertIn("VideoCaptureBitrate = 8000", sb_ini)
            self.assertIn("ToggleVideoCapture = Keyboard/F12", sb_ini)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS AppKit/AVFoundation")
    def test_mocked_run_race_with_record_video_end_to_end(self) -> None:
        video_tools_bin = video_qa_pipeline.ensure_video_tools_binary()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root)

            flywheel_dir = root / "flywheel"
            iter_dir = flywheel_dir / "runs" / "iter_vid_e2e"
            snaps_dir = iter_dir / "snaps"
            videos_dir = iter_dir / "videos"

            # Pre-generate a tiny valid H.264 MP4 that the mock key_driver copies into videos_dir on F12 toggle
            fixture_mp4 = root / "fixture_session.mp4"
            syn = video_qa_pipeline.synthesize_test_video(
                fixture_mp4,
                duration_sec=3.2,
                fps=12,
                width=320,
                height=180,
                black_head_sec=0.5,
                static_tail_sec=0.0,
                video_tools_bin=video_tools_bin,
            )
            self.assertTrue(syn["ok"], syn)

            pcsx2_bin = app_dir / "Contents" / "MacOS" / "PCSX2"
            pcsx2_bin.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import sys, time, signal",
                        "from pathlib import Path",
                        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))",
                        "args = sys.argv[1:]",
                        "log_path = Path(args[args.index('-logfile') + 1])",
                        "log_path.write_text('\\n'.join([",
                        "  '[0.1] PCSX2 v2.6.3',",
                        "  '[0.2] isoFile open ok: test.iso',",
                        "  '[0.3] Serial: SLUS-21351  CRC: 2DAD2310',",
                        "  '[0.4] Metal Graphics Driver Info:',",
                        "  '[0.5]     name = Apple M5 Pro',",
                        "  '[0.6] VM subsystems initialized in 200.00 ms',",
                        "  '[0.7] ELF cdrom0:\\\\SLUS_213.51;1 is executing.',",
                        "  '[0.8] Pad: DS2 Config Finished - P1/S1',",
                        "]) + '\\n', encoding='utf-8')",
                        "while True:",
                        "    time.sleep(0.1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            pcsx2_bin.chmod(0o755)

            toggle_log = root / "toggle_calls.txt"
            mock_driver = root / "mock_key_driver_vid.py"
            mock_driver.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, shutil, sys",
                        "from pathlib import Path",
                        "import numpy as np",
                        "from PIL import Image",
                        "args = sys.argv[1:]",
                        "actions = args[args.index('--actions') + 1] if '--actions' in args else ''",
                        f"with Path({str(toggle_log)!r}).open('a', encoding='utf-8') as f:",
                        "    f.write(actions + '\\n')",
                        "if 'f12' in actions.lower():",
                        f"    dst = Path({str(videos_dir)!r}) / 'SLUS-21351_capture.mp4'",
                        "    dst.parent.mkdir(parents=True, exist_ok=True)",
                        f"    shutil.copy2({str(fixture_mp4)!r}, dst)",
                        "if 'f8' in actions.lower():",
                        f"    snap = Path({str(snaps_dir)!r}) / 'snap_01.png'",
                        "    snap.parent.mkdir(parents=True, exist_ok=True)",
                        "    arr = np.full((180, 320, 3), 125, dtype=np.uint8)",
                        "    arr[:50, :, :] = (165, 185, 210)",
                        "    Image.fromarray(arr).save(snap, format='PNG')",
                        "print(json.dumps({'ok': True, 'accessibility_permitted': True, 'actions_executed': [actions]}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            mock_driver.chmod(0o755)

            fake_iso_report = {
                "volume_id": "NFSMW",
                "entry_count": 10,
                "boot_elf": {"name": "SLUS_213.51", "pcsx2_crc": "2DAD2310"},
            }
            orig_inspect = race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso
            race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = (
                lambda _p, compute_sha256=False: fake_iso_report
            )
            try:
                report = race_flywheel.run_race(
                    iso_path=iso_path,
                    flywheel_dir=flywheel_dir,
                    iter_name="iter_vid_e2e",
                    iter_dir=iter_dir,
                    pcsx2_app=app_dir,
                    pcsx2_data_dir=data_dir,
                    key_driver_bin=mock_driver,
                    video_tools_bin=video_tools_bin,
                    script="f8",
                    labels=["menu"],
                    boot_ready_timeout=5.0,
                    post_script_wait=1.2,
                    record_video=True,
                    video_mode="auto",
                    video_interval=1.0,
                    video_hotkey="f12",
                )
            finally:
                race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect

            self.assertTrue(report["passed"], report)
            self.assertTrue(report["global_pcsx2_ini_untouched"])
            self.assertTrue(report["real_memcards_untouched"])
            self.assertTrue(report["video"]["video_valid"], report["video"])
            self.assertTrue((iter_dir / "session_video.mp4").is_file())
            self.assertTrue((iter_dir / "qa_telemetry.json").is_file())
            self.assertTrue((iter_dir / "qa_sheet_01.png").is_file())
            self.assertTrue(report["video_qa"]["ok"])
            self.assertTrue(report["video_qa"]["summary"]["motion_detected"])

            # Verify F12 toggle was invoked BOTH at start and cleanly before shutdown!
            calls = toggle_log.read_text(encoding="utf-8").splitlines()
            f12_calls = [c for c in calls if "f12" in c.lower()]
            self.assertEqual(len(f12_calls), 2, f"Expected start + stop F12 toggles, got: {calls}")

            # Verify CLI extract-qa works directly on session_video.mp4
            cli_out = root / "cli_qa"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = video_qa_pipeline.main(
                    ["extract-qa", str(iter_dir / "session_video.mp4"), "--out-dir", str(cli_out), "--interval", "1.0", "--json"]
                )
            self.assertEqual(rc, 0)
            cli_payload = json.loads(buf.getvalue())
            self.assertTrue(cli_payload["ok"])
            self.assertTrue((cli_out / "qa_sheet_01.png").is_file())

    def test_repo_clean_audit_video_files(self) -> None:
        issues = audit_repo_clean.audit_files(
            REPO_ROOT,
            [
                Path("tools/macos_video_tools.swift"),
                Path("tools/video_qa_pipeline.py"),
                Path("tools/macos_key_driver.swift"),
                Path("tools/race_flywheel.py"),
                Path("tests/test_video_qa_pipeline.py"),
                Path("tests/test_race_flywheel.py"),
            ],
        )
        self.assertEqual(issues, [], [i.format_human() for i in issues])


if __name__ == "__main__":
    unittest.main()
