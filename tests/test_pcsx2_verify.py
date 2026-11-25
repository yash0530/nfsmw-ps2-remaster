#!/usr/bin/env python3
"""
Unit tests for tools/pcsx2_live_verify.py and Rockport 2026 post-FX shaders.

Runs 100% offline in CI by default (no PS2 ISO or live PCSX2 launch required).
Set `NFSMW_LIVE_PCSX2=1` to enable the optional live PCSX2 v2.6.3 boot test.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
SHADERS_DIR = REPO_ROOT / "shaders"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402
import pcsx2_live_verify  # noqa: E402


SAMPLE_PCSX2_V263_LOG = """\
[    0.1260] PCSX2 v2.6.3
[    0.1265]   Operating System = Darwin 27.2.0 x86_64
[    0.1265]   Processor        = Apple M5 Pro (Rosetta)
[    0.7583] BIOS Found: USA     v01.60(07/02/2002)  Console 20020207-164243
[    0.7596] isoFile open ok: /tmp/NFSMW_BlackEdition_Rockport2026_v0.3.iso
[    0.7860] Disc changed to NFSMW_BlackEdition_Rockport2026_v0.3.iso.
[    0.7860]   Name: Need for Speed - Most Wanted [Black Edition]
[    0.7860]   Serial: SLUS-21351
[    0.7860]   Version: 1.01
[    0.7860]   CRC: 2DAD2310
[    0.7860] Loading game settings from '/tmp/PCSX2/gamesettings/SLUS-21351_2DAD2310.ini'...
[    0.7863] [Unsafe Settings] Cycle rate/skip is not at default, this may crash or make games run too slow.
[    0.7863] OSD [unsafe_settings_warning]: Cycle rate/skip is not at default, this may crash or make games run too slow.
[    1.4357] Metal Graphics Driver Info:
[    1.4358] <AGXG17SDevice: 0x7fd78f830800>
[    1.4358]     name = Apple M5 Pro
[    1.4358]     Shader Version:    Metal 2.3
[    2.0095] ELF Loading: cdrom0:\\SLUS_213.51;1, Game CRC = 2DAD2310, EntryPoint = 0x00436128
[    4.3486] ELF cdrom0:\\SLUS_213.51;1 with entry point at 0x00436128 is executing.
[    4.3490] Found 4 game patches in /tmp/PCSX2/patches/SLUS-21351_2DAD2310.pnach.
[    4.3491] Enabled patch: Widescreen 16:9 (Runtime Confirm)
[    4.3494] OSD [LoadPatches]: 1 game patch is active.
[   14.6704] FMV started
[   27.8285] OSD [GSScreenshot_/tmp/snap.png]: Saved screenshot to 'snap.png'.
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_mock_pcsx2_env(root: Path, *, with_bios: bool = True) -> tuple[Path, Path, Path]:
    """Build a lightweight mock PCSX2.app, data dir, and fake ISO file (no real BIOS/ISO)."""
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
    for sub in ("inis", "gamesettings", "patches", "snaps", "logs"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    bios_dir = root / "ps2_bios"
    if with_bios:
        bios_dir.mkdir(parents=True, exist_ok=True)
        (bios_dir / "scph39001.bin").write_bytes(b"\x00" * 16)

    ini_path = data_dir / "inis" / "PCSX2.ini"
    ini_path.write_text(
        "\n".join(
            [
                "[Folders]",
                "Bios = ../ps2_bios",
                "Snapshots = snaps",
                "Logs = logs",
                "[Filenames]",
                "BIOS = scph39001.bin",
                "[EmuCore/GS]",
                "Renderer = 17",
                "[GameList]",
                "RecursivePaths = /tmp/games",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (data_dir / "gamesettings" / "SLUS-21351_2DAD2310.ini").write_text(
        "[EmuCore/GS]\nRenderer = 17\nupscale_multiplier = 6\nCASMode = 2\nfxaa = true\nShadeBoost = true\n"
        "[Patches]\nEnable = Widescreen 16:9 (Runtime Confirm)\n",
        encoding="utf-8",
    )
    (data_dir / "patches" / "SLUS-21351_2DAD2310.pnach").write_text(
        "gametitle=NFSMW\n[Widescreen 16:9 (Runtime Confirm)]\npatch=1,EE,00AAFE54,word,00000001\n",
        encoding="utf-8",
    )

    iso_path = root / "test.iso"
    iso_path.write_bytes(b"\x00" * 4096)
    return app_dir, data_dir, iso_path


class TestPcsx2LogParser(unittest.TestCase):
    def test_parses_real_metal_boot_markers(self) -> None:
        parsed = pcsx2_live_verify.parse_pcsx2_log(
            SAMPLE_PCSX2_V263_LOG,
            expected_serial="SLUS-21351",
            expected_crc="2DAD2310",
        )
        self.assertEqual(parsed["pcsx2_version"], "v2.6.3")
        self.assertEqual(parsed["host_cpu"], "Apple M5 Pro (Rosetta)")
        self.assertTrue(parsed["bios_loaded"])
        self.assertTrue(parsed["iso_mounted"])
        self.assertTrue(parsed["elf_booted"])
        self.assertTrue(parsed["fmv_started"])
        self.assertEqual(parsed["detected_serial"], "SLUS-21351")
        self.assertEqual(parsed["detected_crc"], "2DAD2310")
        self.assertTrue(parsed["serial_matched"])
        self.assertTrue(parsed["elf_crc_matched"])
        self.assertTrue(parsed["gamesettings_ini_loaded"])
        self.assertEqual(parsed["pnach_patches_found_count"], 4)
        self.assertEqual(parsed["pnach_patches_applied_count"], 1)
        self.assertEqual(parsed["pnach_enabled_names"], ["Widescreen 16:9 (Runtime Confirm)"])
        self.assertTrue(parsed["metal_gs_created"])
        self.assertEqual(parsed["gs_renderer"], "Metal")
        self.assertEqual(parsed["metal_device_name"], "Apple M5 Pro")
        self.assertEqual(parsed["metal_shader_version"], "Metal 2.3")
        self.assertEqual(parsed["screenshots_saved"], ["snap.png"])
        self.assertTrue(parsed["active_emulation"])
        self.assertEqual(parsed["fatal_errors"], [])
        self.assertGreaterEqual(len(parsed["warnings"]), 2)

    def test_detects_fatal_errors(self) -> None:
        bad_log = "[0.5000] BIOS Not Found\n[0.6000] Failed to create GS device\n"
        parsed = pcsx2_live_verify.parse_pcsx2_log(bad_log)
        self.assertFalse(parsed["bios_loaded"])
        self.assertFalse(parsed["active_emulation"])
        self.assertEqual(len(parsed["fatal_errors"]), 2)

    def test_derive_serial_from_elf(self) -> None:
        self.assertEqual(pcsx2_live_verify.derive_serial_from_elf("SLUS_213.51"), "SLUS-21351")
        self.assertEqual(pcsx2_live_verify.derive_serial_from_elf("SLES_538.57"), "SLES-53857")
        self.assertIsNone(pcsx2_live_verify.derive_serial_from_elf("BADNAME.ELF"))


class TestPreflightAndSandbox(unittest.TestCase):
    def test_preflight_and_sandbox_do_not_modify_global_ini_or_copy_bios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root, with_bios=True)
            global_ini = data_dir / "inis" / "PCSX2.ini"
            sha_before = _sha256(global_ini)

            fake_iso_report = {
                "volume_id": "NFSMW",
                "entry_count": 25,
                "boot_elf": {"name": "SLUS_213.51", "pcsx2_crc": "2DAD2310"},
            }
            orig_inspect = pcsx2_live_verify.iso_inspect.inspect_iso
            pcsx2_live_verify.iso_inspect.inspect_iso = lambda _p, compute_sha256=False: fake_iso_report
            try:
                out_dir = root / "out"
                report = pcsx2_live_verify.verify_live_pcsx2(
                    iso_path=iso_path,
                    pcsx2_app=app_dir,
                    pcsx2_data_dir=data_dir,
                    out_dir=out_dir,
                    dry_run=True,
                )
                self.assertTrue(report["passed"])
                self.assertTrue(report["preflight"]["ok"])
                self.assertEqual(report["preflight"]["iso"]["serial"], "SLUS-21351")
                self.assertEqual(report["preflight"]["iso"]["elf_crc"], "2DAD2310")
                self.assertTrue(report["preflight"]["per_game"]["gamesettings_ini_exists"])
                self.assertTrue(report["preflight"]["per_game"]["pnach_exists"])
                self.assertEqual(_sha256(global_ini), sha_before)

                sandbox_home, snaps_dir = pcsx2_live_verify.prepare_sandbox_home(
                    out_dir, data_dir, global_ini, report["preflight"]
                )
                sandbox_ini = (
                    sandbox_home / "Library" / "Application Support" / "PCSX2" / "inis" / "PCSX2.ini"
                ).read_text(encoding="utf-8")
                self.assertIn(f"Snapshots = {snaps_dir}", sandbox_ini)
                self.assertIn(f"Bios = {(root / 'ps2_bios').resolve()}", sandbox_ini)
                self.assertIn("RecursivePaths = \n", sandbox_ini)
                # Ensure no BIOS files were copied into out_dir
                copied_bins = list(out_dir.rglob("*.bin"))
                self.assertEqual(copied_bins, [])
            finally:
                pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect

    def test_preflight_flags_missing_bios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, data_dir, iso_path = _create_mock_pcsx2_env(root, with_bios=False)
            fake_iso_report = {
                "volume_id": "NFSMW",
                "entry_count": 25,
                "boot_elf": {"name": "SLUS_213.51", "pcsx2_crc": "2DAD2310"},
            }
            orig_inspect = pcsx2_live_verify.iso_inspect.inspect_iso
            pcsx2_live_verify.iso_inspect.inspect_iso = lambda _p, compute_sha256=False: fake_iso_report
            try:
                pre = pcsx2_live_verify.run_preflight(
                    iso_path=iso_path,
                    pcsx2_app=app_dir,
                    pcsx2_data_dir=data_dir,
                )
                self.assertFalse(pre["ok"])
                self.assertTrue(any("BIOS directory" in iss for iss in pre["issues"]))
            finally:
                pcsx2_live_verify.iso_inspect.inspect_iso = orig_inspect


class TestScreenshotAndProcessCleanup(unittest.TestCase):
    def test_analyze_screenshot_valid_black_and_truncated(self) -> None:
        from PIL import Image  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            valid_png = d / "valid.png"
            img = Image.new("RGB", (64, 64), (10, 20, 30))
            for x in range(32):
                for y in range(64):
                    img.putpixel((x, y), (220, 180, 90))
            img.save(valid_png)

            black_png = d / "black.png"
            Image.new("RGB", (64, 64), (0, 0, 0)).save(black_png)

            trunc_png = d / "trunc.png"
            trunc_png.write_bytes(valid_png.read_bytes()[:32])

            v_info = pcsx2_live_verify.analyze_screenshot(valid_png)
            self.assertTrue(v_info["is_valid_render"])
            self.assertEqual(v_info["width"], 64)
            self.assertGreater(v_info["non_black_coverage"], 0.4)

            b_info = pcsx2_live_verify.analyze_screenshot(black_png)
            self.assertFalse(b_info["is_valid_render"])
            self.assertEqual(b_info["non_black_coverage"], 0.0)

            t_info = pcsx2_live_verify.analyze_screenshot(trunc_png)
            self.assertFalse(t_info["is_valid_render"])
            self.assertIn("error", t_info)

    def test_terminate_process_cleanly(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        res = pcsx2_live_verify.terminate_process_cleanly(proc, term_timeout=2.0)
        self.assertFalse(res["orphan_remains"])
        self.assertEqual(res["signal_used"], "SIGTERM")


class TestShaderValidation(unittest.TestCase):
    def _assert_balanced_braces(self, text: str, name: str) -> None:
        depth = 0
        for ch in text:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                self.assertGreaterEqual(depth, 0, f"Unmatched '}}' in {name}")
        self.assertEqual(depth, 0, f"Unclosed '{{' in {name}")

    def test_slang_ssgi_shader_structure(self) -> None:
        slang_path = SHADERS_DIR / "rockport2026_ssgi.slang"
        self.assertTrue(slang_path.is_file())
        text = slang_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#version 450"))
        self.assertIn("#pragma stage vertex", text)
        self.assertIn("#pragma stage fragment", text)
        self.assertIn("#pragma parameter SSGI_STRENGTH", text)
        self.assertIn("#pragma parameter SSR_WETNESS", text)
        self.assertIn("uniform sampler2D Source;", text)
        self.assertIn("computeSSGI", text)
        self.assertIn("computeWetRoadSSR", text)
        self.assertIn("bilateralStabilize", text)
        self._assert_balanced_braces(text, slang_path.name)
        self.assertIsNone(audit_repo_clean.PERSONAL_PATH_RE.search(text))

    def test_glsl_aces_grade_shader_structure(self) -> None:
        glsl_path = SHADERS_DIR / "rockport2026_aces_grade.glsl"
        self.assertTrue(glsl_path.is_file())
        text = glsl_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#version 450"))
        self.assertIn("ACES_INPUT_MAT", text)
        self.assertIn("rrtAndOdtFit", text)
        self.assertIn("acesFilmicToneMap", text)
        self.assertIn("sampleSoftBloom", text)
        self.assertIn("applyDehaze", text)
        self.assertIn("applyMicroContrast", text)
        self._assert_balanced_braces(text, glsl_path.name)
        self.assertIsNone(audit_repo_clean.PERSONAL_PATH_RE.search(text))


class TestLivePcsx2Optional(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("NFSMW_LIVE_PCSX2") == "1",
        "Set NFSMW_LIVE_PCSX2=1 to run live PCSX2 v2.6.3 Metal boot verification",
    )
    def test_live_pcsx2_boot_modded_iso(self) -> None:
        iso = pcsx2_live_verify.DEFAULT_ISO
        self.assertTrue(iso.is_file(), f"Modded ISO missing: {iso}")
        with tempfile.TemporaryDirectory() as tmp:
            rep = pcsx2_live_verify.verify_live_pcsx2(
                iso_path=iso,
                out_dir=Path(tmp),
                boot_wait_sec=18.0,
                skip_screenshot=True,
            )
            self.assertTrue(rep["passed"], f"Live PCSX2 failed: {rep}")
            self.assertTrue(rep["parsed_log"]["metal_gs_created"])
            self.assertTrue(rep["parsed_log"]["elf_crc_matched"])
            self.assertTrue(rep["global_pcsx2_ini_untouched"])


if __name__ == "__main__":
    unittest.main()
