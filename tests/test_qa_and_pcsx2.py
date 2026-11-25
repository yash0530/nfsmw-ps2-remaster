#!/usr/bin/env python3
"""
Unit tests for NFSMW: Rockport 2026 QA guardrails, PCSX2 v2.6.3 configurator,
.pnach linter, and unified rockport_cli.py launcher.

Runs 100% offline with Python 3 stdlib only (no PS2 ISO or live PCSX2 install required).
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402
import lint_pnach  # noqa: E402
import rockport_cli  # noqa: E402
import setup_pcsx2_macos  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestSetupPcsx2Macos(unittest.TestCase):
    def test_default_ini_matches_pcsx2_v263_schema(self) -> None:
        ini = setup_pcsx2_macos.build_gamesettings_ini(
            upscale=6,
            anisotropy=16,
            ee_cycle_rate=1,
            enable_dump=False,
            precache=True,
            enabled_patches=["Widescreen 16:9", "Remove Brown Filter", "Remove Motion Blur"],
        )
        errors = setup_pcsx2_macos.validate_gamesettings_ini(ini)
        self.assertEqual(errors, [], f"Unexpected schema errors: {errors}")

        # Verify critical audited PCSX2 v2.6.3 keys & values
        self.assertIn("Renderer = 17", ini)
        self.assertNotIn("19 = Metal", ini)
        self.assertIn("EECycleRate = 1", ini)
        self.assertIn("CASMode = 2", ini)
        self.assertIn("ShadeBoost_Brightness = 50", ini)
        self.assertIn("ShadeBoost_Contrast = 54", ini)
        self.assertIn("ShadeBoost_Saturation = 56", ini)
        self.assertIn("ShadeBoost_Gamma = 48", ini)
        self.assertIn("dithering_ps2 = 0", ini)
        self.assertIn("accurate_blending_unit = 2", ini)
        self.assertIn("LoadTextureReplacements = true", ini)
        self.assertIn("PrecacheTextureReplacements = true", ini)
        self.assertIn("DumpReplaceableTextures = false", ini)
        self.assertIn("UserHacks = true", ini)
        self.assertIn("UserHacks_BilinearHack = 2", ini)
        self.assertIn("UserHacks_HalfPixelOffset = 5", ini)
        self.assertIn("UserHacks_native_scaling = 2", ini)
        self.assertIn("UserHacks_CPUCLUTRender = 1", ini)
        self.assertIn("UserHacks_GPUTargetCLUTMode = 1", ini)

        # Multi-key [Patches] section preserved
        self.assertIn("[Patches]", ini)
        self.assertIn("Enable = Widescreen 16:9", ini)
        self.assertIn("Enable = Remove Brown Filter", ini)
        self.assertIn("Enable = Remove Motion Blur", ini)

    def test_renderer_variants_and_invalid_values(self) -> None:
        vk_ini = setup_pcsx2_macos.build_gamesettings_ini(
            upscale=4, anisotropy=8, ee_cycle_rate=0, enable_dump=True, precache=False, renderer=14
        )
        self.assertIn("Renderer = 14", vk_ini)
        self.assertIn("DumpReplaceableTextures = true", vk_ini)
        self.assertIn("PrecacheTextureReplacements = false", vk_ini)
        self.assertEqual(setup_pcsx2_macos.validate_gamesettings_ini(vk_ini), [])

        auto_ini = setup_pcsx2_macos.build_gamesettings_ini(
            upscale=6, anisotropy=16, ee_cycle_rate=2, enable_dump=False, precache=True, renderer=-1
        )
        self.assertIn("Renderer = -1", auto_ini)
        self.assertEqual(setup_pcsx2_macos.validate_gamesettings_ini(auto_ini), [])

        # Old wrong Metal ID (19) must be rejected
        with self.assertRaises(ValueError):
            setup_pcsx2_macos.build_gamesettings_ini(
                upscale=6, anisotropy=16, ee_cycle_rate=1, enable_dump=False, precache=True, renderer=19
            )
        # Out-of-range EE cycle rate must be rejected
        with self.assertRaises(ValueError):
            setup_pcsx2_macos.build_gamesettings_ini(
                upscale=6, anisotropy=16, ee_cycle_rate=5, enable_dump=False, precache=True
            )

    def test_schema_validator_catches_misspelled_keys_and_bad_enums(self) -> None:
        bad_ini = """
[EmuCore/GS]
Renderer = 19
UserHacks_cpuclutrender = 1
CASMode = 9
"""
        errs = setup_pcsx2_macos.validate_gamesettings_ini(bad_ini)
        self.assertEqual(len(errs), 3)
        joined = "\n".join(errs)
        self.assertIn("Renderer", joined)
        self.assertIn("UserHacks_cpuclutrender", joined)
        self.assertIn("CASMode", joined)

    def test_extract_default_pnach_sections_skips_optional_and_experimental(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "SLUS-21351_0518D274.pnach"
            p.write_text(
                "\n".join(
                    [
                        "gametitle=NFSMW",
                        "comment=Test",
                        "[Widescreen 16:9]",
                        "patch=1,EE,00AAFDD4,word,00000001",
                        "[Remove Brown Filter]",
                        "patch=1,EE,00550D58,word,00000001",
                        "[Remove Distance Brume (Optional)]",
                        "patch=0,EE,005500F0,word,00000000",
                        "[60 FPS (Experimental)]",
                        "patch=0,EE,005500F4,word,00000000",
                        "[Ultrawide 21:9]",
                        "patch=1,EE,0054BA38,word,3F800000",
                    ]
                ),
                encoding="utf-8",
            )
            sections = setup_pcsx2_macos.extract_default_pnach_sections(p)
            self.assertEqual(sections, ["Widescreen 16:9", "Remove Brown Filter"])

    def test_cli_install_leaves_global_pcsx2_ini_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pcsx2_dir = Path(tmp) / "PCSX2"
            inis_dir = pcsx2_dir / "inis"
            inis_dir.mkdir(parents=True)
            global_ini = inis_dir / "PCSX2.ini"
            global_ini.write_text("[EmuCore/GS]\nRenderer = 2\n", encoding="utf-8")
            before_hash = _sha256(global_ini)

            extra_pnach = Path(tmp) / "SLUS-21351_016D10FB.pnach"
            extra_pnach.write_text(
                "gametitle=Modded\ncomment=Test\n\n[Widescreen 16:9 (Runtime Flag)]\n"
                "patch=1,EE,00AAFDD4,word,00000001\n",
                encoding="utf-8",
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = setup_pcsx2_macos.main(
                    [
                        "--pcsx2-dir",
                        str(pcsx2_dir),
                        "--crcs",
                        "0518D274",
                        "016D10FB",
                        "--extra-pnach",
                        str(extra_pnach),
                        "--renderer",
                        "metal",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(_sha256(global_ini), before_hash)

            mod_ini = (pcsx2_dir / "gamesettings" / "SLUS-21351_016D10FB.ini").read_text(encoding="utf-8")
            self.assertIn("Renderer = 17", mod_ini)
            self.assertIn("Enable = Widescreen 16:9 (Runtime Flag)", mod_ini)
            self.assertEqual(setup_pcsx2_macos.validate_gamesettings_ini(mod_ini), [])
            self.assertTrue((pcsx2_dir / "textures" / "SLUS-21351" / "dumps").is_dir())
            self.assertTrue((pcsx2_dir / "textures" / "SLUS-21351" / "replacements").is_dir())


class TestAuditRepoClean(unittest.TestCase):
    def test_clean_directory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "patches").mkdir()
            (root / "assets_cc0").mkdir()
            (root / "README.md").write_text(
                "Example placeholders: /Users/<username>/file and /home/<name>/file and C:\\Users\\<name>\\file\n",
                encoding="utf-8",
            )
            (root / "patches" / "valid.json").write_text('{"ok": true}\n', encoding="utf-8")
            (root / "assets_cc0" / "manifest.json").write_text('{"assets": []}\n', encoding="utf-8")

            files, issues = audit_repo_clean.audit_repository(root, target_dir=root)
            self.assertEqual(len(files), 3)
            self.assertEqual(issues, [])

    def test_prohibited_extensions_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("disc.iso", "zdir.bin", "car.tpk", "tex.dds", "model.pth", "patch.xdelta", "mod.irx"):
                (root / name).write_bytes(b"stub")

            _, issues = audit_repo_clean.audit_repository(root, target_dir=root)
            cats = [i.category for i in issues]
            self.assertEqual(cats.count("prohibited-extension"), 7)

    def test_oversized_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            big = root / "huge.txt"
            big.write_bytes(b"A" * 4096)

            _, issues = audit_repo_clean.audit_repository(root, target_dir=root, max_file_bytes=1024)
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].category, "file-too-large")

    def test_hardcoded_personal_paths_fail(self) -> None:
        # Build test strings dynamically so this test file itself contains no literal personal paths.
        mac_path = "/" + "Users/" + "alice/Documents/game.iso"
        lin_path = "/" + "home/" + "bob/work/repo"
        win_path = "C:\\" + "Users\\" + "charlie\\Desktop\\file.txt"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text(
                f"mac: {mac_path}\nlinux: {lin_path}\nwin: {win_path}\n",
                encoding="utf-8",
            )
            _, issues = audit_repo_clean.audit_repository(root, target_dir=root)
            personal = [i for i in issues if i.category == "personal-path"]
            self.assertEqual(len(personal), 3)

    def test_invalid_json_in_patches_and_assets_cc0_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "patches").mkdir()
            (root / "assets_cc0").mkdir()
            (root / "patches" / "broken.json").write_text("{bad json", encoding="utf-8")
            (root / "assets_cc0" / "broken.json").write_text("[1, 2, ]", encoding="utf-8")

            _, issues = audit_repo_clean.audit_repository(root, target_dir=root)
            bad_json = [i for i in issues if i.category == "invalid-json"]
            self.assertEqual(len(bad_json), 2)

    def test_cli_exit_codes_and_live_repo_tracked_files_clean(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = audit_repo_clean.main(["--repo-root", str(REPO_ROOT)])
        self.assertEqual(rc, 0, f"Live repo audit failed:\n{buf.getvalue()}")

        # Also verify our own newly written QA files pass direct file audit
        owned = [
            Path("tools/setup_pcsx2_macos.py"),
            Path("tools/audit_repo_clean.py"),
            Path("tools/lint_pnach.py"),
            Path("tools/rockport_cli.py"),
            Path(".github/workflows/ci.yml"),
            Path("tests/test_qa_and_pcsx2.py"),
        ]
        issues = audit_repo_clean.audit_files(REPO_ROOT, owned)
        self.assertEqual(issues, [], f"Owned QA files failed hygiene audit: {issues}")


class TestLintPnach(unittest.TestCase):
    VALID_PNACH = """gametitle=Need for Speed - Most Wanted [Black Edition] (NTSC-U) [SLUS-21351]
comment=Verified test patch suite

[Widescreen 16:9]
gsaspectratio=16:9
author=Tester
description=Enables 16:9 widescreen
patch=1,EE,00AAFDD4,word,00000001
patch=1,EE,0054BA38,word,3F400000

[Ultrawide 21:9 (Optional)]
gsaspectratio=21:9
patch=1,EE,0054BA38,word,3F800000
"""

    def test_valid_pnach_and_real_repo_pnach_pass(self) -> None:
        self.assertEqual(lint_pnach.lint_pnach_text(self.VALID_PNACH, "valid.pnach"), [])

        real_files = lint_pnach.collect_pnach_targets([], REPO_ROOT)
        self.assertGreaterEqual(len(real_files), 1)
        for pf in real_files:
            errs = lint_pnach.lint_pnach_file(pf)
            self.assertEqual(errs, [], f"Repo .pnach failed lint ({pf}): {errs}")

    def test_missing_headers_fail(self) -> None:
        text = "[Only Section]\npatch=1,EE,00550D58,word,00000001\n"
        errs = lint_pnach.lint_pnach_text(text)
        codes = [e.code for e in errs]
        self.assertEqual(codes.count("missing-header"), 2)

    def test_orphan_patch_outside_section_fails(self) -> None:
        text = "gametitle=T\ncomment=C\npatch=1,EE,00550D58,word,00000001\n"
        errs = lint_pnach.lint_pnach_text(text)
        self.assertTrue(any(e.code == "orphan-patch" for e in errs))

    def test_unaligned_word_and_out_of_bounds_ee_ram_fail(self) -> None:
        text = """gametitle=T
comment=C
[Bad Addresses]
patch=1,EE,00550D59,word,00000001
patch=1,EE,02000000,word,00000001
patch=1,EE,80000000,word,00000001
"""
        errs = lint_pnach.lint_pnach_text(text)
        codes = [e.code for e in errs]
        self.assertIn("unaligned-address", codes)
        self.assertEqual(codes.count("addr-out-of-bounds"), 2)

    def test_conflicting_and_duplicate_writes_within_same_section_fail(self) -> None:
        text = """gametitle=T
comment=C
[Conflict]
patch=1,EE,00550D58,word,00000001
patch=1,EE,00550D58,word,00000002
patch=1,EE,00550D60,word,00000001
patch=1,EE,00550D60,word,00000001
"""
        errs = lint_pnach.lint_pnach_text(text)
        codes = [e.code for e in errs]
        self.assertIn("conflicting-write", codes)
        self.assertIn("duplicate-write", codes)

    def test_invalid_mode_cpu_hex_fail(self) -> None:
        text = """gametitle=T
comment=C
[Broken Syntax]
patch=9,EE,00550D58,word,00000001
patch=1,VU0,00550D5C,word,00000001
patch=1,EE,550D60,word,00000001
patch=1,EE,00550D64,word,ZZZZZZZZ
"""
        errs = lint_pnach.lint_pnach_text(text)
        codes = {e.code for e in errs}
        self.assertTrue({"bad-mode", "bad-cpu", "bad-address", "bad-value"}.issubset(codes))


class TestRockportCli(unittest.TestCase):
    def test_dump_mode_toggle_preserves_comments_patches_and_never_touches_global_ini(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pcsx2_dir = Path(tmp) / "PCSX2"
            inis_dir = pcsx2_dir / "inis"
            gs_dir = pcsx2_dir / "gamesettings"
            inis_dir.mkdir(parents=True)
            gs_dir.mkdir(parents=True)

            global_ini = inis_dir / "PCSX2.ini"
            global_ini.write_text("[EmuCore/GS]\nDumpReplaceableTextures = false\n", encoding="utf-8")
            global_before = _sha256(global_ini)

            game_ini = gs_dir / "SLUS-21351_0518D274.ini"
            game_ini.write_text(
                setup_pcsx2_macos.build_gamesettings_ini(
                    upscale=6,
                    anisotropy=16,
                    ee_cycle_rate=1,
                    enable_dump=False,
                    precache=True,
                    enabled_patches=["Widescreen 16:9", "Remove Brown Filter"],
                ),
                encoding="utf-8",
            )

            # Toggle ON
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rockport_cli.main(["dump-mode", "on", "--crc", "0518D274", "--pcsx2-dir", str(pcsx2_dir)])
            self.assertEqual(rc, 0)
            self.assertEqual(_sha256(global_ini), global_before)

            text_on = game_ini.read_text(encoding="utf-8")
            self.assertIn("DumpReplaceableTextures = true", text_on)
            self.assertIn("Enable = Widescreen 16:9", text_on)
            self.assertIn("Enable = Remove Brown Filter", text_on)
            self.assertEqual(setup_pcsx2_macos.validate_gamesettings_ini(text_on), [])

            # Toggle OFF (targeting all existing profiles without --crc)
            with redirect_stdout(io.StringIO()):
                rc_off = rockport_cli.main(["dump-mode", "off", "--pcsx2-dir", str(pcsx2_dir)])
            self.assertEqual(rc_off, 0)
            self.assertEqual(_sha256(global_ini), global_before)

            text_off = game_ini.read_text(encoding="utf-8")
            self.assertIn("DumpReplaceableTextures = false", text_off)
            self.assertEqual(setup_pcsx2_macos.validate_gamesettings_ini(text_off), [])

    def test_status_json_on_synthetic_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcsx2_dir = root / "PCSX2"
            (pcsx2_dir / "gamesettings").mkdir(parents=True)
            (pcsx2_dir / "patches").mkdir(parents=True)
            (pcsx2_dir / "textures" / "SLUS-21351" / "dumps").mkdir(parents=True)
            (pcsx2_dir / "textures" / "SLUS-21351" / "replacements").mkdir(parents=True)

            (pcsx2_dir / "gamesettings" / "SLUS-21351_0518D274.ini").write_text(
                setup_pcsx2_macos.build_gamesettings_ini(
                    upscale=6, anisotropy=16, ee_cycle_rate=1, enable_dump=False, precache=True
                ),
                encoding="utf-8",
            )
            (pcsx2_dir / "textures" / "SLUS-21351" / "dumps" / "aabbccdd-11223344.png").write_bytes(b"PNG")
            (pcsx2_dir / "textures" / "SLUS-21351" / "replacements" / "aabbccdd-11223344.dds").write_bytes(b"DDS ")

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rockport_cli.main(
                    [
                        "status",
                        "--source-iso",
                        str(root / "missing.iso"),
                        "--build-dir",
                        str(root / "build"),
                        "--pcsx2-dir",
                        str(pcsx2_dir),
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertFalse(payload["source_iso"]["exists"])
            self.assertEqual(payload["textures"]["pcsx2_dumps"]["png"], 1)
            self.assertEqual(payload["textures"]["pcsx2_replacements"]["dds"], 1)
            self.assertEqual(len(payload["pcsx2"]["profiles"]), 1)
            self.assertEqual(payload["pcsx2"]["profiles"][0]["renderer"], "17")
            self.assertEqual(payload["pcsx2"]["profiles"][0]["schema_warnings"], [])

    def test_verify_passes_on_clean_synthetic_repo_and_fails_on_bad_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "tools").mkdir()
            (repo / "patches" / "pnach").mkdir(parents=True)
            (repo / "tools" / "hello.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
            (repo / "patches" / "pnach" / "SLUS-21351_0518D274.pnach").write_text(
                TestLintPnach.VALID_PNACH, encoding="utf-8"
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rockport_cli.main(["verify", "--repo-root", str(repo), "--skip-tests"])
            self.assertEqual(rc, 0, buf.getvalue())

            # Introduce prohibited binary file -> verify must fail
            (repo / "bad_asset.tpk").write_bytes(b"TPK!")
            err_buf = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err_buf):
                rc_fail = rockport_cli.main(["verify", "--repo-root", str(repo), "--skip-tests"])
            self.assertNotEqual(rc_fail, 0)


if __name__ == "__main__":
    unittest.main()
