#!/usr/bin/env python3
"""
Unit tests for tools/macos_key_driver.swift and tools/race_flywheel.py.

Runs 100% offline in CI by default (no PS2 ISO or live PCSX2 launch required).
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
                "[Filenames]",
                "BIOS = scph39001.bin",
                "[Hotkeys]",
                "Screenshot = Keyboard/F8",
                "TogglePause = Keyboard/Space",
                "ToggleTurbo = Keyboard/Tab",
                "HoldTurbo = Keyboard/Period",
                "[Pad1]",
                "Type = DualShock2",
                "Up = SDL-0/DPadUp",
                "Cross = SDL-0/FaceSouth",
                "LUp = SDL-0/-LeftY",
                "[GameList]",
                "RecursivePaths = /tmp/games",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (data_dir / "gamesettings" / "SLUS-21351_2DAD2310.ini").write_text(
        "\n".join(
            [
                "[EmuCore/GS]",
                "Renderer = 17",
                "upscale_multiplier = 6",
                "DumpReplaceableTextures = false",
                "[Patches]",
                "Enable = Widescreen 16:9 (Runtime Confirm)",
                "Enable = Remove Brown Filter",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (data_dir / "patches" / "SLUS-21351_2DAD2310.pnach").write_text(
        "gametitle=NFSMW\n[Widescreen 16:9 (Runtime Confirm)]\npatch=1,EE,00AAFE54,word,00000001\n",
        encoding="utf-8",
    )

    iso_path = root / "test.iso"
    iso_path.write_bytes(b"\x00" * 4096)
    return app_dir, data_dir, iso_path


def _write_test_png(path: Path, rgb: tuple[int, int, int], with_edges: bool = True) -> None:
    arr = np.zeros((180, 320, 3), dtype=np.uint8)
    arr[:, :] = rgb
    if with_edges:
        # Deterministic high-contrast grid & bright sky band
        arr[:45, :, :] = np.clip(np.array(rgb) + 45, 0, 255).astype(np.uint8)
        arr[60:120:4, 40:280:4, :] = np.clip(np.array(rgb) - 55, 0, 255).astype(np.uint8)
        arr[140:150, 120:160, :] = (255, 255, 255)
        arr[140:150, 180:220, :] = (2, 2, 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path, format="PNG")


class TestKeyDriverCompilationAndCLI(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS AppKit/AVFoundation")
    def test_key_driver_compiles_help_and_dry_run(self) -> None:
        bin_path = race_flywheel.ensure_key_driver_binary()
        self.assertTrue(bin_path.is_file())
        self.assertTrue(os.access(bin_path, os.X_OK))

        help_res = subprocess.run(
            [str(bin_path), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(help_res.returncode, 0)
        self.assertIn("tap:<key>:<ms>", help_res.stdout)
        self.assertIn("hold:<k1+k2>:<ms>", help_res.stdout)
        self.assertIn("f8", help_res.stdout)

        dry_res = subprocess.run(
            [
                str(bin_path),
                "--dry-run",
                "--json",
                "--actions",
                "wait:200,tap:k:120",
                "--actions",
                "hold:k+q+a:1200,f8",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(dry_res.returncode, 0, dry_res.stderr)
        payload = json.loads(dry_res.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(
            payload["actions"],
            ["wait:200", "tap:k:120", "hold:k+q+a:1200", "f8"],
        )
        self.assertIn("accessibility_permitted", payload)

        bad_res = subprocess.run(
            [str(bin_path), "--dry-run", "--actions", "tap:unknownkey:100"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(bad_res.returncode, 0)


class TestFlywheelSandboxIsolation(unittest.TestCase):
    def test_sandbox_never_touches_real_ini_or_memcards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root)
            global_ini = data_dir / "inis" / "PCSX2.ini"
            mcd1 = data_dir / "memcards" / "Mcd001.ps2"
            mcd2 = data_dir / "memcards" / "Mcd002.ps2"
            ini_sha = _sha256(global_ini)
            mcd1_sha = _sha256(mcd1)
            mcd2_sha = _sha256(mcd2)

            repl_src = root / "custom_repl"
            repl_src.mkdir(parents=True, exist_ok=True)
            (repl_src / "1234ABCD-5678EF90.dds").write_bytes(b"DDS_MOCK")
            (repl_src / "hud.png").write_bytes(b"PNG_MOCK")
            (repl_src / "ignore.txt").write_text("skip me", encoding="utf-8")

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
                iter_dir = flywheel_dir / "runs" / "iter_001"
                preflight = race_flywheel.pcsx2_live_verify.run_preflight(
                    iso_path, pcsx2_app=app_dir, pcsx2_data_dir=data_dir
                )
                self.assertTrue(preflight["ok"])

                sb = race_flywheel.prepare_flywheel_sandbox(
                    flywheel_dir=flywheel_dir,
                    iter_dir=iter_dir,
                    data_dir=data_dir,
                    preflight=preflight,
                    dump_textures=True,
                    replacements_dir=repl_src,
                    shadeboost=(52, 56, 58, 49),
                    cas_sharpness=65,
                    upscale=6,
                )

                # Real files untouched
                self.assertEqual(_sha256(global_ini), ini_sha)
                self.assertEqual(_sha256(mcd1), mcd1_sha)
                self.assertEqual(_sha256(mcd2), mcd2_sha)

                # Memcards copied into sandbox_memcards
                sb_mcd1 = Path(sb["sandbox_memcards_dir"]) / "Mcd001.ps2"
                self.assertEqual(sb_mcd1.read_bytes(), b"MCD1_ORIGINAL_DATA")

                # Verify sandbox PCSX2.ini overrides
                sb_ini = Path(sb["sandbox_ini"]).read_text(encoding="utf-8")
                self.assertIn(f"MemoryCards = {Path(sb['sandbox_memcards_dir'])}", sb_ini)
                self.assertIn(f"Snapshots = {Path(sb['snaps_dir'])}", sb_ini)
                self.assertIn(f"Logs = {Path(sb['logs_dir'])}", sb_ini)
                self.assertIn(f"GameSettings = {Path(sb['sandbox_gamesettings_dir'])}", sb_ini)
                self.assertIn(f"Textures = {Path(sb['sandbox_textures_dir'])}", sb_ini)
                self.assertIn(f"Bios = {(root / 'ps2_bios').resolve()}", sb_ini)
                self.assertIn("ConfirmShutdown = false", sb_ini)
                self.assertIn("PauseOnFocusLoss = false", sb_ini)
                self.assertIn("Screenshot = Keyboard/F8", sb_ini)
                self.assertNotIn("TogglePause = Keyboard/Space", sb_ini)
                self.assertNotIn("SDL-0/", sb_ini)
                for expected_line in (
                    "Type = DualShock2",
                    "Up = Keyboard/Up",
                    "LUp = Keyboard/W",
                    "Down = Keyboard/Down",
                    "LDown = Keyboard/S",
                    "Left = Keyboard/Left",
                    "LLeft = Keyboard/A",
                    "Right = Keyboard/Right",
                    "LRight = Keyboard/D",
                    "Cross = Keyboard/K",
                    "R2 = Keyboard/2",
                    "Square = Keyboard/J",
                    "L2 = Keyboard/1",
                    "Circle = Keyboard/L",
                    "Triangle = Keyboard/I",
                    "Start = Keyboard/G",
                    "Select = Keyboard/H",
                    "L1 = Keyboard/Q",
                    "R1 = Keyboard/E",
                ):
                    self.assertIn(expected_line, sb_ini)

                # Verify sandbox gamesettings INI overrides & preserved [Patches] Enable lines
                gs_text = Path(sb["target_gamesettings_ini"]).read_text(encoding="utf-8")
                self.assertIn("DumpReplaceableTextures = true", gs_text)
                self.assertIn("ShadeBoost = true", gs_text)
                self.assertIn("ShadeBoost_Brightness = 52", gs_text)
                self.assertIn("CASMode = 2", gs_text)
                self.assertIn("CASSharpness = 65", gs_text)
                self.assertIn("LoadTextureReplacements = true", gs_text)
                self.assertIn("Enable = Widescreen 16:9 (Runtime Confirm)", gs_text)
                self.assertIn("Enable = Remove Brown Filter", gs_text)

                # Verify replacement textures synced
                repl_dst = Path(sb["sandbox_textures_dir"]) / "SLUS-21351" / "replacements"
                self.assertTrue((repl_dst / "1234ABCD-5678EF90.dds").is_file())
                self.assertTrue((repl_dst / "hud.png").is_file())
                self.assertFalse((repl_dst / "ignore.txt").exists())

                # Subsequent run without --reset-memcards preserves sandbox memcard progress
                sb_mcd1.write_bytes(b"MCD1_RACE_PROGRESS")
                race_flywheel.prepare_flywheel_sandbox(
                    flywheel_dir=flywheel_dir,
                    iter_dir=iter_dir,
                    data_dir=data_dir,
                    preflight=preflight,
                    reset_memcards=False,
                )
                self.assertEqual(sb_mcd1.read_bytes(), b"MCD1_RACE_PROGRESS")

                # With --reset-memcards, sandbox memcard is refreshed cleanly
                race_flywheel.prepare_flywheel_sandbox(
                    flywheel_dir=flywheel_dir,
                    iter_dir=iter_dir,
                    data_dir=data_dir,
                    preflight=preflight,
                    reset_memcards=True,
                )
                self.assertEqual(sb_mcd1.read_bytes(), b"MCD1_ORIGINAL_DATA")

                # Verify --screenshot-key p sets Screenshot = Keyboard/P in sandbox PCSX2.ini
                sb_p = race_flywheel.prepare_flywheel_sandbox(
                    flywheel_dir=flywheel_dir,
                    iter_dir=iter_dir,
                    data_dir=data_dir,
                    preflight=preflight,
                    screenshot_key="p",
                )
                sb_p_ini = Path(sb_p["sandbox_ini"]).read_text(encoding="utf-8")
                self.assertIn("Screenshot = Keyboard/P", sb_p_ini)
            finally:
                race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect


class TestMetricsMontageAndFlow(unittest.TestCase):
    def test_analyze_metrics_and_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            amber_png = root / "amber.png"
            black_png = root / "black.png"
            _write_test_png(amber_png, (200, 155, 85), with_edges=True)
            _write_test_png(black_png, (0, 0, 0), with_edges=False)

            amber_m = race_flywheel.analyze_frame_metrics(amber_png)
            self.assertTrue(amber_m["is_valid_render"])
            self.assertGreater(amber_m["amber_rb_ratio"], 1.8)
            self.assertGreater(amber_m["laplacian_sharpness"], 10.0)
            self.assertGreater(amber_m["non_black_coverage"], 0.9)
            self.assertGreater(amber_m["p99"], amber_m["p5"])
            self.assertGreater(amber_m["highlight_clip_pct"], 0.0)
            self.assertGreater(amber_m["shadow_crush_pct"], 0.0)

            black_m = race_flywheel.analyze_frame_metrics(black_png)
            self.assertFalse(black_m["is_valid_render"])
            self.assertEqual(black_m["non_black_coverage"], 0.0)
            self.assertEqual(black_m["shadow_crush_pct"], 100.0)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = race_flywheel.main(["analyze", str(amber_png), str(black_png), "--json"])
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(len(parsed["frames"]), 2)

    def test_montage_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pngs = []
            for idx, col in enumerate([(180, 140, 90), (110, 135, 160), (140, 145, 140), (95, 115, 140)]):
                p = root / f"f{idx}.png"
                _write_test_png(p, col, with_edges=True)
                pngs.append(p)

            out_png = root / "montage.png"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = race_flywheel.main(
                    [
                        "montage",
                        "--out",
                        str(out_png),
                        "--labels",
                        "menu,grid,straight,corner",
                        *[str(p) for p in pngs],
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertTrue(out_png.is_file())
            with Image.open(out_png) as im:
                self.assertEqual(im.mode, "RGB")
                self.assertGreater(im.size[0], 1600)
                self.assertGreater(im.size[1], 900)

    def test_truncated_png_graceful_handling_and_flush_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid_png = root / "valid.png"
            trunc_png = root / "truncated.png"
            _write_test_png(valid_png, (140, 150, 165), with_edges=True)
            # Write a valid PNG then truncate its tail to simulate interrupted F8 write
            _write_test_png(trunc_png, (180, 140, 90), with_edges=True)
            raw_bytes = trunc_png.read_bytes()
            trunc_png.write_bytes(raw_bytes[: max(48, len(raw_bytes) // 3)])

            trunc_m = race_flywheel.analyze_frame_metrics(trunc_png)
            self.assertTrue(trunc_m["exists"])
            self.assertIn("error", trunc_m)
            self.assertFalse(trunc_m["is_valid_render"])
            self.assertFalse(
                race_flywheel.pcsx2_live_verify._wait_for_complete_png(trunc_png, timeout_sec=0.1)
            )
            self.assertTrue(
                race_flywheel.pcsx2_live_verify._wait_for_complete_png(valid_png, timeout_sec=0.1)
            )

            montage_out = root / "montage_with_truncated.png"
            res = race_flywheel.build_montage(
                [valid_png, trunc_png],
                montage_out,
                labels=["valid", "truncated"],
            )
            self.assertEqual(res["tile_count"], 2)
            self.assertEqual(res["unreadable_count"], 1)
            self.assertTrue(montage_out.is_file())

            # Verify _wait_for_snaps_flush passes once Saved screenshot & valid PNG appear
            snaps_dir = root / "snaps"
            snaps_dir.mkdir(parents=True, exist_ok=True)
            emu_log = root / "pcsx2_emulog.txt"
            snap_file = snaps_dir / "shot_01.png"
            _write_test_png(snap_file, (120, 130, 140), with_edges=True)
            emu_log.write_text(
                "Saving screenshot to 'shot_01.png'.\nSaved screenshot to 'shot_01.png'.\n",
                encoding="utf-8",
            )
            rc = race_flywheel._wait_for_snaps_flush(
                snaps_dir,
                set(),
                emu_log,
                expected_count=1,
                timeout_sec=2.0,
            )
            self.assertIsNone(rc)

    def test_mocked_run_race_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root)

            # Mock PCSX2 binary: parses -logfile path, writes Metal/ELF/DS2 markers, sleeps until SIGTERM.
            pcsx2_bin = app_dir / "Contents" / "MacOS" / "PCSX2"
            pcsx2_bin.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import sys, time, signal",
                        "from pathlib import Path",
                        "running = True",
                        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))",
                        "args = sys.argv[1:]",
                        "log_path = Path(args[args.index('-logfile') + 1])",
                        "log_path.write_text('\\n'.join([",
                        "  '[0.1] PCSX2 v2.6.3',",
                        "  '[0.2] isoFile open ok: test.iso',",
                        "  '[0.3] Serial: SLUS-21351  CRC: 2DAD2310',",
                        "  '[0.4] Metal Graphics Driver Info:',",
                        "  '[0.5]     name = Apple M5 Pro',",
                        "  '[0.6] ELF cdrom0:\\\\SLUS_213.51;1 is executing.',",
                        "  '[0.7] UpdateVSyncRate: Mode Changed to NTSC.',",
                        "  '[0.8] Pad: DS2 Config Finished - P1/S1',",
                        "]) + '\\n', encoding='utf-8')",
                        "while running:",
                        "    time.sleep(0.1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            pcsx2_bin.chmod(0o755)

            flywheel_dir = root / "flywheel"
            iter_dir = flywheel_dir / "runs" / "iter_mock"
            snaps_dir = iter_dir / "snaps"

            # Mock key_driver binary: writes a valid PNG into snaps_dir and outputs JSON.
            mock_driver = root / "mock_key_driver.py"
            mock_driver.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, sys",
                        "from pathlib import Path",
                        "import numpy as np",
                        "from PIL import Image",
                        f"snap = Path({str(snaps_dir)!r}) / 'snap_01.png'",
                        "snap.parent.mkdir(parents=True, exist_ok=True)",
                        "arr = np.full((180, 320, 3), 120, dtype=np.uint8)",
                        "arr[:60, :, :] = (160, 180, 210)",
                        "arr[80:140, 80:240, :] = (210, 170, 90)",
                        "Image.fromarray(arr).save(snap, format='PNG')",
                        "print(json.dumps({'ok': True, 'accessibility_permitted': True, 'actions_executed': ['f8']}))",
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
                    iter_name="iter_mock",
                    iter_dir=iter_dir,
                    pcsx2_app=app_dir,
                    pcsx2_data_dir=data_dir,
                    key_driver_bin=mock_driver,
                    script="f8",
                    labels=["menu"],
                    boot_ready_timeout=5.0,
                    post_script_wait=1.5,
                )
            finally:
                race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect

            self.assertTrue(report["passed"], report)
            self.assertTrue(report["global_pcsx2_ini_untouched"])
            self.assertTrue(report["real_memcards_untouched"])
            self.assertFalse(report["shutdown"]["orphan_remains"])
            self.assertEqual(len(report["frames"]), 1)
            self.assertTrue((iter_dir / "frames" / "frame_01_menu.png").is_file())
            self.assertTrue((iter_dir / "montage.png").is_file())
            self.assertTrue((iter_dir / "race_report.json").is_file())
            self.assertTrue((flywheel_dir / "history.json").is_file())

    def test_mocked_run_race_quarantines_truncated_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root)

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
                        "  '[0.6] ELF cdrom0:\\\\SLUS_213.51;1 is executing.',",
                        "]) + '\\n', encoding='utf-8')",
                        "while True:",
                        "    time.sleep(0.1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            pcsx2_bin.chmod(0o755)

            flywheel_dir = root / "flywheel"
            iter_dir = flywheel_dir / "runs" / "iter_corrupt"
            snaps_dir = iter_dir / "snaps"

            mock_driver = root / "mock_key_driver_corrupt.py"
            mock_driver.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json",
                        "from pathlib import Path",
                        "import numpy as np",
                        "from PIL import Image",
                        f"snap = Path({str(snaps_dir)!r}) / 'snap_corrupt.png'",
                        "snap.parent.mkdir(parents=True, exist_ok=True)",
                        "arr = np.full((180, 320, 3), 120, dtype=np.uint8)",
                        "Image.fromarray(arr).save(snap, format='PNG')",
                        "raw = snap.read_bytes()",
                        "snap.write_bytes(raw[: max(48, len(raw) // 3)])",
                        "print(json.dumps({'ok': True, 'accessibility_permitted': True, 'actions_executed': ['f8']}))",
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
                    iter_name="iter_corrupt",
                    iter_dir=iter_dir,
                    pcsx2_app=app_dir,
                    pcsx2_data_dir=data_dir,
                    key_driver_bin=mock_driver,
                    script="f8",
                    labels=["corrupt_shot"],
                    boot_ready_timeout=5.0,
                    post_script_wait=0.2,
                )
            finally:
                race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect

            self.assertEqual(len(report["frames"]), 1)
            frame = report["frames"][0]
            self.assertTrue(frame.get("quarantined_corrupt"))
            self.assertFalse(frame.get("is_valid_render"))
            self.assertTrue((snaps_dir / "snap_corrupt.png.corrupt").is_file())
            self.assertFalse((snaps_dir / "snap_corrupt.png").exists())
            self.assertEqual(list((iter_dir / "frames").glob("*.png")), [])

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS AppKit/AVFoundation")
    def test_mocked_run_race_pine_autopilot_with_video_end_to_end(self) -> None:
        import video_qa_pipeline

        video_tools_bin = video_qa_pipeline.ensure_video_tools_binary()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root)
            global_ini = data_dir / "inis" / "PCSX2.ini"
            mcd1 = data_dir / "memcards" / "Mcd001.ps2"
            ini_sha = _sha256(global_ini)
            mcd1_sha = _sha256(mcd1)

            flywheel_dir = root / "flywheel"
            iter_dir = flywheel_dir / "runs" / "iter_pine_autopilot"
            snaps_dir = iter_dir / "snaps"
            videos_dir = iter_dir / "videos"

            fixture_mp4 = root / "fixture_pine.mp4"
            syn = video_qa_pipeline.synthesize_test_video(
                fixture_mp4,
                duration_sec=2.5,
                fps=12,
                width=320,
                height=180,
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
                        "  '[0.6] VM subsystems initialized in 150.00 ms',",
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

            order_log = root / "event_order.txt"

            mock_driver = root / "mock_key_driver_pine.py"
            mock_driver.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, shutil, sys",
                        "from pathlib import Path",
                        "args = sys.argv[1:]",
                        "actions = args[args.index('--actions') + 1] if '--actions' in args else ''",
                        f"with Path({str(order_log)!r}).open('a', encoding='utf-8') as f:",
                        "    f.write('key_driver:' + actions + '\\n')",
                        "if 'f12' in actions.lower():",
                        f"    dst = Path({str(videos_dir)!r}) / 'SLUS-21351_pine.mp4'",
                        "    dst.parent.mkdir(parents=True, exist_ok=True)",
                        f"    shutil.copy2({str(fixture_mp4)!r}, dst)",
                        "print(json.dumps({'ok': True, 'accessibility_permitted': True, 'actions_executed': [actions]}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            mock_driver.chmod(0o755)

            mock_pine = root / "mock_pcsx2_pine_driver.py"
            mock_pine.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, sys",
                        "from pathlib import Path",
                        "import numpy as np",
                        "from PIL import Image",
                        "args = sys.argv[1:]",
                        "assert '--slot' in args and 'autopilot' in args, args",
                        "assert args.index('--slot') < args.index('autopilot'), args",
                        "assert '--menu-auto-advance' in args, args",
                        "assert '--menu-mode' in args and args[args.index('--menu-mode') + 1] == 'freeroam', args",
                        "summary_out = Path(args[args.index('--summary-out') + 1])",
                        "telemetry_out = Path(args[args.index('--telemetry-out') + 1])",
                        f"with Path({str(order_log)!r}).open('a', encoding='utf-8') as f:",
                        "    f.write('pine_autopilot\\n')",
                        "telemetry_out.write_text(json.dumps({'speed_mph': 88.4}) + '\\n', encoding='utf-8')",
                        "summary = {",
                        "  'ticks': 120, 'racing_ticks': 100, 'elapsed_sec': 5.0,",
                        "  'distance_traveled_m': 245.6, 'max_speed_mph': 92.3, 'avg_speed_mph': 74.1,",
                        "  'stuck_recoveries': 1, 'wrong_way_recoveries': 0, 'reverse_verified': True,",
                        "  'obstacles_avoided': 3, 'race_started': True, 'flow_states_seen': ['IN_FRONTEND', 'RACING']",
                        "}",
                        "summary_out.write_text(json.dumps(summary, indent=2), encoding='utf-8')",
                        f"snap = Path({str(snaps_dir)!r}) / 'snap_pine_01.png'",
                        "snap.parent.mkdir(parents=True, exist_ok=True)",
                        "arr = np.full((180, 320, 3), 130, dtype=np.uint8)",
                        "arr[:50, :, :] = (160, 185, 215)",
                        "Image.fromarray(arr).save(snap, format='PNG')",
                        "print(json.dumps(summary))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            mock_pine.chmod(0o755)

            fake_iso_report = {
                "volume_id": "NFSMW",
                "entry_count": 10,
                "boot_elf": {"name": "SLUS_213.51", "pcsx2_crc": "2DAD2310"},
            }
            orig_inspect = race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso
            orig_flush = race_flywheel._wait_for_snaps_flush

            def _logged_flush(*f_args, **f_kwargs):
                with order_log.open("a", encoding="utf-8") as f:
                    f.write("snaps_flush\n")
                return orig_flush(*f_args, **f_kwargs)

            race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = (
                lambda _p, compute_sha256=False: fake_iso_report
            )
            race_flywheel._wait_for_snaps_flush = _logged_flush
            try:
                report = race_flywheel.run_race(
                    iso_path=iso_path,
                    flywheel_dir=flywheel_dir,
                    iter_name="iter_pine_autopilot",
                    iter_dir=iter_dir,
                    pcsx2_app=app_dir,
                    pcsx2_data_dir=data_dir,
                    key_driver_bin=mock_driver,
                    video_tools_bin=video_tools_bin,
                    pine_driver_script=mock_pine,
                    pine_autopilot=True,
                    pine_slot=28019,
                    autopilot_duration=10.0,
                    autopilot_f8_interval=5.0,
                    autopilot_menu_mode="freeroam",
                    record_video=True,
                    video_mode="auto",
                    video_interval=1.0,
                    boot_ready_timeout=5.0,
                    post_script_wait=1.0,
                )
            finally:
                race_flywheel.pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect
                race_flywheel._wait_for_snaps_flush = orig_flush

            self.assertTrue(report["passed"], report)
            self.assertTrue(report["global_pcsx2_ini_untouched"])
            self.assertTrue(report["real_memcards_untouched"])
            self.assertEqual(_sha256(global_ini), ini_sha)
            self.assertEqual(_sha256(mcd1), mcd1_sha)

            # Sandbox PCSX2.ini has EnablePINE = true and PINESlot = 28019 (implied by --pine-autopilot)
            sb_ini = Path(report["sandbox"]["sandbox_ini"]).read_text(encoding="utf-8")
            self.assertIn("EnablePINE = true", sb_ini)
            self.assertIn("PINESlot = 28019", sb_ini)

            # Verify pine_summary recorded in report & history.json
            self.assertEqual(report["pine_summary"]["distance_traveled_m"], 245.6)
            self.assertTrue(report["pine_summary"]["reverse_verified"])
            self.assertTrue((iter_dir / "pine_summary.json").is_file())
            self.assertTrue((iter_dir / "pine_telemetry.jsonl").is_file())
            history = json.loads((flywheel_dir / "history.json").read_text(encoding="utf-8"))
            self.assertTrue(history[-1]["pine_autopilot"])
            self.assertEqual(history[-1]["pine_summary"]["max_speed_mph"], 92.3)

            # Verify exact lifecycle order: F12 start -> pine_autopilot -> F12 stop -> snaps_flush!
            events = order_log.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(events), 4, events)
            self.assertIn("f12", events[0].lower())
            idx_pilot = events.index("pine_autopilot")
            idx_flush = events.index("snaps_flush")
            f12_indices = [i for i, e in enumerate(events) if "f12" in e.lower()]
            self.assertEqual(len(f12_indices), 2, events)
            self.assertLess(f12_indices[0], idx_pilot, events)
            self.assertLess(idx_pilot, f12_indices[1], events)
            self.assertLess(f12_indices[1], idx_flush, events)

    def test_repo_clean_audit_owned_files(self) -> None:
        issues = audit_repo_clean.audit_files(
            REPO_ROOT,
            [
                Path("tools/macos_key_driver.swift"),
                Path("tools/race_flywheel.py"),
                Path("tests/test_race_flywheel.py"),
            ],
        )
        self.assertEqual(issues, [], [i.format_human() for i in issues])


if __name__ == "__main__":
    unittest.main()
