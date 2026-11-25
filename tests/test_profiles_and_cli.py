#!/usr/bin/env python3
"""
Unit & integration tests for NFSMW: Rockport 2026 preset build profiles and
the upgraded `rockport_cli.py` (`profiles` & `build` subcommands).

Runs 100% offline with Python 3 stdlib only (no retail PS2 ISO or live PCSX2 install required).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402
import build_modded_iso  # noqa: E402
import iso_inspect  # noqa: E402
import lint_pnach  # noqa: E402
import rockport_cli  # noqa: E402
import setup_pcsx2_macos  # noqa: E402

SECTOR_SIZE = iso_inspect.SECTOR_SIZE


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_iso_dir_record(name_bytes: bytes, lba: int, size: int, is_dir: bool) -> bytes:
    rec_len = 33 + len(name_bytes)
    if rec_len % 2 == 1:
        rec_len += 1
    buf = bytearray(rec_len)
    buf[0] = rec_len
    struct.pack_into("<I", buf, 2, lba)
    struct.pack_into(">I", buf, 6, lba)
    struct.pack_into("<I", buf, 10, size)
    struct.pack_into(">I", buf, 14, size)
    buf[25] = 0x02 if is_dir else 0x00
    struct.pack_into("<H", buf, 28, 1)
    struct.pack_into(">H", buf, 30, 1)
    buf[32] = len(name_bytes)
    buf[33 : 33 + len(name_bytes)] = name_bytes
    return bytes(buf)


def _build_synthetic_elf_for_real_patches(elf_spec: dict, patch_ids: Optional[List[str]] = None) -> bytes:
    """Create a synthetic ELF matching real patches/elf_patches.json CRCs & expected_orig words."""
    pt_vaddr = int(elf_spec["target"]["pt_load_vaddr"], 16)
    pt_off = int(elf_spec["target"]["pt_load_file_offset"], 16)
    target_crc = int(elf_spec["target"]["elf_pcsx2_crc"], 16)

    patches = elf_spec["patches"]
    if patch_ids is not None:
        id_set = set(patch_ids)
        patches = [p for p in patches if p["id"] in id_set]

    max_end = pt_off + 0x2000
    for p in patches:
        for w in p["writes"]:
            off = int(w["vaddr"], 16) - pt_vaddr + pt_off
            max_end = max(max_end, off + 4)

    # Extra 4 bytes at the tail allow exact XOR adjustment to target_crc (0x0518D274)
    total_len = ((max_end + 4 + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE
    filesz = total_len - pt_off
    buf = bytearray(total_len)
    buf[0:16] = b"\x7fELF\x01\x01\x01" + b"\x00" * 9
    struct.pack_into(
        "<HHIIIIIHHHHHH",
        buf,
        16,
        2,
        8,
        1,
        pt_vaddr,
        52,
        0,
        0x20924001,
        52,
        32,
        1,
        0,
        0,
        0,
    )
    struct.pack_into(
        "<IIIIIIII",
        buf,
        52,
        1,
        pt_off,
        pt_vaddr,
        pt_vaddr,
        filesz,
        filesz,
        7,
        0x1000,
    )

    for p in patches:
        for w in p["writes"]:
            off = int(w["vaddr"], 16) - pt_vaddr + pt_off
            struct.pack_into("<I", buf, off, int(w["expected_orig"], 16))

    # Adjust final 32-bit word so PCSX2 XOR CRC matches target_crc exactly
    tail_off = total_len - 4
    cur_crc = iso_inspect.compute_pcsx2_elf_crc(bytes(buf))
    struct.pack_into("<I", buf, tail_off, cur_crc ^ target_crc)
    assert iso_inspect.compute_pcsx2_elf_crc(bytes(buf)) == target_crc
    return bytes(buf)


def _build_synthetic_iso_with_elf(out_path: Path, elf_bytes: bytes) -> None:
    """Create a compact valid ISO9660 disc with SLUS_213.51 + NFS/ZDIR.BIN + NFS/ZZDATA0..3.BIN."""
    assert len(elf_bytes) % SECTOR_SIZE == 0
    elf_sectors = len(elf_bytes) // SECTOR_SIZE
    zdir_bytes = b"\x00" * 24  # 1 empty 24-byte placeholder record
    zz_buf = b"\x00" * SECTOR_SIZE

    root_lba = 18
    nfs_lba = 19
    elf_lba = 20
    zdir_lba = elf_lba + elf_sectors
    zz0_lba = zdir_lba + 1
    zz1_lba = zz0_lba + 1
    zz2_lba = zz1_lba + 1
    zz3_lba = zz2_lba + 1
    total_sectors = zz3_lba + 1

    nfs_dir = bytearray()
    nfs_dir += _make_iso_dir_record(b"\x00", nfs_lba, SECTOR_SIZE, True)
    nfs_dir += _make_iso_dir_record(b"\x01", root_lba, SECTOR_SIZE, True)
    nfs_dir += _make_iso_dir_record(b"ZDIR.BIN;1", zdir_lba, len(zdir_bytes), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA0.BIN;1", zz0_lba, len(zz_buf), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA1.BIN;1", zz1_lba, len(zz_buf), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA2.BIN;1", zz2_lba, len(zz_buf), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA3.BIN;1", zz3_lba, len(zz_buf), False)

    root_dir = bytearray()
    root_dir += _make_iso_dir_record(b"\x00", root_lba, SECTOR_SIZE, True)
    root_dir += _make_iso_dir_record(b"\x01", root_lba, SECTOR_SIZE, True)
    root_dir += _make_iso_dir_record(b"SLUS_213.51;1", elf_lba, len(elf_bytes), False)
    root_dir += _make_iso_dir_record(b"NFS", nfs_lba, SECTOR_SIZE, True)

    pvd = bytearray(SECTOR_SIZE)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = b"NFSMW_SYNTHETIC_TEST".ljust(32, b" ")
    struct.pack_into("<I", pvd, 80, total_sectors)
    struct.pack_into(">I", pvd, 84, total_sectors)
    struct.pack_into("<H", pvd, 128, SECTOR_SIZE)
    struct.pack_into(">H", pvd, 130, SECTOR_SIZE)
    pvd[156:190] = _make_iso_dir_record(b"\x00", root_lba, SECTOR_SIZE, True)[:34]

    vdt = bytearray(SECTOR_SIZE)
    vdt[0] = 255
    vdt[1:6] = b"CD001"
    vdt[6] = 1

    img = bytearray(total_sectors * SECTOR_SIZE)
    img[16 * SECTOR_SIZE : 17 * SECTOR_SIZE] = pvd
    img[17 * SECTOR_SIZE : 18 * SECTOR_SIZE] = vdt
    img[root_lba * SECTOR_SIZE : root_lba * SECTOR_SIZE + len(root_dir)] = root_dir
    img[nfs_lba * SECTOR_SIZE : nfs_lba * SECTOR_SIZE + len(nfs_dir)] = nfs_dir
    img[elf_lba * SECTOR_SIZE : elf_lba * SECTOR_SIZE + len(elf_bytes)] = elf_bytes
    img[zdir_lba * SECTOR_SIZE : zdir_lba * SECTOR_SIZE + len(zdir_bytes)] = zdir_bytes
    for lba in (zz0_lba, zz1_lba, zz2_lba, zz3_lba):
        img[lba * SECTOR_SIZE : (lba + 1) * SECTOR_SIZE] = zz_buf

    out_path.write_bytes(bytes(img))


class TestBuildProfilesInventory(unittest.TestCase):
    """Validate all curated preset build profiles in patches/profiles/."""

    def test_all_repo_profiles_exist_and_validate_cleanly(self) -> None:
        report = rockport_cli.collect_profiles(repo_root=REPO_ROOT)
        self.assertEqual(report["status"], "pass", f"Profile validation failed: {report}")
        self.assertEqual(report["invalid_count"], 0)
        self.assertGreaterEqual(report["profile_count"], 4)

        by_id = {p["profile_id"]: p for p in report["profiles"]}
        expected_ids = {
            "rockport2026_v0.2",
            "rockport2026_purist_hd",
            "rockport2026_ultrawide_21_9",
            "rockport2026_sandbox",
        }
        self.assertTrue(expected_ids.issubset(by_id.keys()))

        # Ensure output filenames are unique across all profiles
        out_names = [p["output_filename"] for p in report["profiles"]]
        self.assertEqual(len(out_names), len(set(out_names)))

        # Verify each profile also loads via build_modded_iso.load_build_profile
        for entry in report["profiles"]:
            loaded = build_modded_iso.load_build_profile(Path(entry["file"]))
            self.assertEqual(loaded["profile_id"], entry["profile_id"])
            self.assertTrue(entry["valid"], f"{entry['profile_id']} errors: {entry['errors']}")

    def test_curated_presets_patch_and_vlt_definitions(self) -> None:
        report = rockport_cli.collect_profiles(repo_root=REPO_ROOT)
        by_id = {p["profile_id"]: p for p in report["profiles"]}

        # 1. Purist HD: pure visuals/LOD/16:9/60fps without gameplay VLT changes
        purist = by_id["rockport2026_purist_hd"]
        self.assertEqual(
            purist["elf_patch_ids"],
            [
                "remove_brown_filter",
                "remove_motion_blur",
                "widescreen_16_9_native",
                "high_lod_cars",
                "full_rate_car_reflections",
                "extended_scenery_draw_distance",
                "screen_efx_60fps_timing",
            ],
        )
        self.assertEqual(purist["aspect"], "16:9")
        self.assertEqual(purist["pcsx2_aspect_ratio"], "16:9")
        purist_vlt_names = {Path(v["recipe"]).name for v in purist["vlt_recipes"]}
        self.assertTrue(purist_vlt_names.issubset({"modern_visual_look.json"}))

        # 2. Ultrawide 21:9: ultrawide_21_9_native instead of widescreen_16_9_native + 4 VLT recipes
        uw = by_id["rockport2026_ultrawide_21_9"]
        self.assertIn("ultrawide_21_9_native", uw["elf_patch_ids"])
        self.assertNotIn("widescreen_16_9_native", uw["elf_patch_ids"])
        self.assertEqual(uw["elf_patch_count"], 7)
        self.assertEqual(uw["aspect"], "21:9")
        self.assertEqual(uw["pcsx2_aspect_ratio"], "Stretch")
        self.assertEqual(uw["vlt_recipe_count"], 4)
        self.assertEqual(uw["vlt_edit_count"], 75)

        # 3. Sandbox: visuals/LOD/60fps + skip_intro + unlock_all + disable_ai_catchup + 4 VLT recipes
        sb = by_id["rockport2026_sandbox"]
        self.assertEqual(sb["elf_patch_count"], 10)
        for req in (
            "widescreen_16_9_native",
            "skip_intro_movies",
            "unlock_all_cars_and_parts",
            "disable_ai_catchup",
            "screen_efx_60fps_timing",
        ):
            self.assertIn(req, sb["elf_patch_ids"])
        self.assertEqual(sb["aspect"], "16:9")
        self.assertEqual(sb["pcsx2_aspect_ratio"], "16:9")
        self.assertEqual(sb["vlt_recipe_count"], 4)
        self.assertEqual(sb["vlt_edit_count"], 75)

    def test_owned_files_pass_repo_hygiene_audit(self) -> None:
        owned = [
            Path("patches/profiles/rockport2026_purist_hd.json"),
            Path("patches/profiles/rockport2026_ultrawide_21_9.json"),
            Path("patches/profiles/rockport2026_sandbox.json"),
            Path("tools/rockport_cli.py"),
            Path("tests/test_profiles_and_cli.py"),
        ]
        issues = audit_repo_clean.audit_files(REPO_ROOT, owned)
        self.assertEqual(issues, [], f"Hygiene audit failed: {issues}")


class TestProfileValidatorGuardrails(unittest.TestCase):
    """Verify profile validator catches conflicts, missing refs, and duplicates."""

    def _make_temp_repo(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / "patches" / "profiles").mkdir(parents=True)
        (root / "patches" / "vlt").mkdir(parents=True)
        elf_spec = REPO_ROOT / "patches" / "elf_patches.json"
        (root / "patches" / "elf_patches.json").write_text(elf_spec.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "patches" / "vlt" / "ok.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "ok",
                    "target": {"virtual_path": "GLOBAL\\ATTRIBUTES.BIN"},
                    "patches": [{"expected_offset": "0x00001000"}],
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_rejects_mutually_exclusive_elf_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_temp_repo(tmp)
            bad_profile = {
                "schema_version": 1,
                "profile_id": "bad_aspect",
                "name": "Conflicting Aspect",
                "output_filename": "bad.iso",
                "elf_patches": {
                    "spec": "patches/elf_patches.json",
                    "include_ids": ["widescreen_16_9_native", "ultrawide_21_9_native"],
                },
                "vlt_recipes": [],
            }
            (root / "patches" / "profiles" / "bad.json").write_text(json.dumps(bad_profile), encoding="utf-8")
            report = rockport_cli.collect_profiles(repo_root=root)
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["invalid_count"], 1)
            errs = "\n".join(report["profiles"][0]["errors"])
            self.assertIn("Mutually exclusive", errs)

    def test_rejects_missing_vlt_recipe_and_unknown_elf_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_temp_repo(tmp)
            bad_profile = {
                "schema_version": 1,
                "profile_id": "bad_refs",
                "name": "Bad References",
                "output_filename": "bad_refs.iso",
                "elf_patches": {
                    "spec": "patches/elf_patches.json",
                    "include_ids": ["does_not_exist"],
                },
                "vlt_recipes": [
                    {"virtual_path": "GLOBAL\\ATTRIBUTES.BIN", "recipe": "patches/vlt/missing.json"},
                ],
            }
            (root / "patches" / "profiles" / "bad_refs.json").write_text(json.dumps(bad_profile), encoding="utf-8")
            report = rockport_cli.collect_profiles(repo_root=root)
            self.assertEqual(report["status"], "fail")
            errs = "\n".join(report["profiles"][0]["errors"])
            self.assertIn("Unknown ELF patch ID", errs)
            self.assertIn("Missing VLT recipe", errs)

    def test_rejects_overlapping_vlt_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_temp_repo(tmp)
            (root / "patches" / "vlt" / "dup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "dup",
                        "target": {"virtual_path": "GLOBAL\\ATTRIBUTES.BIN"},
                        "patches": [{"expected_offset": "0x00001000"}],
                    }
                ),
                encoding="utf-8",
            )
            bad_profile = {
                "schema_version": 1,
                "profile_id": "vlt_overlap",
                "name": "Overlapping VLT",
                "output_filename": "overlap.iso",
                "elf_patches": {"spec": "patches/elf_patches.json", "include_ids": ["widescreen_16_9_native"]},
                "vlt_recipes": [
                    {"virtual_path": "GLOBAL\\ATTRIBUTES.BIN", "recipe": "patches/vlt/ok.json"},
                    {"virtual_path": "GLOBAL\\ATTRIBUTES.BIN", "recipe": "patches/vlt/dup.json"},
                ],
            }
            (root / "patches" / "profiles" / "overlap.json").write_text(json.dumps(bad_profile), encoding="utf-8")
            report = rockport_cli.collect_profiles(repo_root=root)
            self.assertEqual(report["status"], "fail")
            errs = "\n".join(report["profiles"][0]["errors"])
            self.assertIn("Overlapping VLT offset", errs)


class TestRockportProfilesAndBuildCli(unittest.TestCase):
    """Test `rockport_cli.py profiles` and `rockport_cli.py build`."""

    def test_profiles_cli_human_and_json(self) -> None:
        buf_human = io.StringIO()
        with redirect_stdout(buf_human):
            rc_h = rockport_cli.main(["profiles"])
        self.assertEqual(rc_h, 0)
        out_h = buf_human.getvalue()
        self.assertIn("rockport2026_purist_hd", out_h)
        self.assertIn("rockport2026_ultrawide_21_9", out_h)
        self.assertIn("rockport2026_sandbox", out_h)
        self.assertIn("Stretch", out_h)
        self.assertIn("VALID", out_h)

        buf_json = io.StringIO()
        with redirect_stdout(buf_json):
            rc_j = rockport_cli.main(["profiles", "--json"])
        self.assertEqual(rc_j, 0)
        payload = json.loads(buf_json.getvalue())
        self.assertEqual(payload["status"], "pass")
        self.assertGreaterEqual(payload["profile_count"], 4)

    def test_build_dry_run_16_9_and_21_9_aspect_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pcsx2_dir = Path(tmp) / "PCSX2"
            missing_iso = Path(tmp) / "missing.iso"

            # 1. v0.2 default 16:9 dry-run
            buf1 = io.StringIO()
            with redirect_stdout(buf1):
                rc1 = rockport_cli.main(
                    [
                        "build",
                        "--profile",
                        "rockport2026_v0.2",
                        "--source",
                        str(missing_iso),
                        "--install-pcsx2",
                        "--pcsx2-dir",
                        str(pcsx2_dir),
                        "--upscale",
                        "6",
                        "--ee-cycle-rate",
                        "2",
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(rc1, 0)
            res1 = json.loads(buf1.getvalue())
            self.assertEqual(res1["status"], "dry-run")
            self.assertTrue(res1["verify_source_sha256"])
            self.assertIn("--verify-source-sha256", res1["build_command"])
            self.assertEqual(res1["display_aspect"], "16:9")
            self.assertEqual(res1["pcsx2_aspect_ratio"], "16:9")
            self.assertIsNotNone(res1["setup_command"])
            self.assertIn("6", res1["setup_command"])

            # 2. 21:9 ultrawide dry-run -> maps to PCSX2 AspectRatio = Stretch & enables 21:9 companion section
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc2 = rockport_cli.main(
                    [
                        "build",
                        "--profile",
                        "rockport2026_ultrawide_21_9",
                        "--source",
                        str(missing_iso),
                        "--install-pcsx2",
                        "--pcsx2-dir",
                        str(pcsx2_dir),
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(rc2, 0)
            res2 = json.loads(buf2.getvalue())
            self.assertEqual(res2["display_aspect"], "21:9")
            self.assertEqual(res2["pcsx2_aspect_ratio"], "Stretch")
            self.assertIn("--enable-patch", res2["setup_command"])
            self.assertIn("Widescreen 21:9 (Runtime Confirm)", res2["setup_command"])

    def test_build_refuses_missing_source_and_same_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "source.iso"
            iso.write_bytes(b"ISO")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc_same = rockport_cli.main(
                    ["build", "--profile", "rockport2026_v0.2", "--source", str(iso), "--output", str(iso)]
                )
            self.assertEqual(rc_same, 2)

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc_missing = rockport_cli.main(
                    ["build", "--profile", "rockport2026_v0.2", "--source", str(Path(tmp) / "nope.iso")]
                )
            self.assertEqual(rc_missing, 1)

    def test_build_mocked_subprocess_with_real_pcsx2_install_and_aspect_stretch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_iso = root / "stock.iso"
            src_iso.write_bytes(b"STOCK_ISO_STUB")
            src_hash = _sha256(src_iso)

            out_iso = root / "build" / "mod_21_9.iso"
            pcsx2_dir = root / "PCSX2"
            (pcsx2_dir / "inis").mkdir(parents=True)
            global_ini = pcsx2_dir / "inis" / "PCSX2.ini"
            global_ini.write_text("[EmuCore/GS]\nRenderer = 2\nAspectRatio = 4:3\n", encoding="utf-8")
            global_hash = _sha256(global_ini)

            mod_crc = "ABCD1234"
            recorded_cmds: List[List[str]] = []

            def fake_subprocess_run(cmd: List[str], **kwargs):
                recorded_cmds.append(list(cmd))
                script_name = Path(cmd[1]).name
                if script_name == "build_modded_iso.py":
                    out_iso.parent.mkdir(parents=True, exist_ok=True)
                    out_iso.write_bytes(b"MODDED_ISO_STUB")
                    pnach_path = out_iso.parent / f"SLUS-21351_{mod_crc}.pnach"
                    pnach_path.write_text(
                        "gametitle=NFSMW Mod\n"
                        "comment=Test companion\n\n"
                        "[Widescreen 21:9 (Runtime Confirm)]\n"
                        "gsaspectratio=21:9\n"
                        "patch=1,EE,00AAFE54,word,00000001\n",
                        encoding="utf-8",
                    )
                    manifest = {
                        "builder_version": "0.2",
                        "profile_id": "rockport2026_ultrawide_21_9",
                        "source_iso": src_iso.name,
                        "source_elf_crc": "0518D274",
                        "source_sha256_before": src_hash,
                        "source_sha256_after": src_hash,
                        "output_iso": out_iso.name,
                        "output_elf_crc": mod_crc,
                        "copy_mode": "mock-copy",
                        "companion_pnach": pnach_path.name,
                        "applied_elf_patches": [{"id": "ultrawide_21_9_native"}],
                        "applied_zz_overrides": [],
                        "iso_layout_update": {},
                    }
                    out_iso.with_suffix(".build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    return subprocess.CompletedProcess(cmd, 0, stdout="mock build ok\n", stderr="")

                if script_name == "setup_pcsx2_macos.py":
                    # Execute real setup_pcsx2_macos.main() against temp pcsx2_dir!
                    setup_buf = io.StringIO()
                    with redirect_stdout(setup_buf):
                        rc_setup = setup_pcsx2_macos.main(cmd[2:])
                    return subprocess.CompletedProcess(cmd, rc_setup, stdout=setup_buf.getvalue(), stderr="")

                raise AssertionError(f"Unexpected subprocess command: {cmd}")

            out_buf = io.StringIO()
            with mock.patch("rockport_cli.subprocess.run", side_effect=fake_subprocess_run):
                with redirect_stdout(out_buf):
                    rc = rockport_cli.main(
                        [
                            "build",
                            "--profile",
                            "rockport2026_ultrawide_21_9",
                            "--source",
                            str(src_iso),
                            "--output",
                            str(out_iso),
                            "--install-pcsx2",
                            "--pcsx2-dir",
                            str(pcsx2_dir),
                            "--upscale",
                            "6",
                            "--ee-cycle-rate",
                            "2",
                        ]
                    )

            self.assertEqual(rc, 0)
            self.assertEqual(len(recorded_cmds), 2)
            self.assertIn("--verify-source-sha256", recorded_cmds[0])
            self.assertIn("--enable-patch", recorded_cmds[1])
            self.assertIn("Widescreen 21:9 (Runtime Confirm)", recorded_cmds[1])

            # Verify global PCSX2.ini and source ISO were NEVER touched
            self.assertEqual(_sha256(global_ini), global_hash)
            self.assertEqual(_sha256(src_iso), src_hash)

            # Verify per-game INI matches PCSX2 v2.6.3 schema with Stretch + enabled 21:9 patch
            ini_path = pcsx2_dir / "gamesettings" / f"SLUS-21351_{mod_crc}.ini"
            self.assertTrue(ini_path.is_file())
            ini_text = ini_path.read_text(encoding="utf-8")
            self.assertEqual(setup_pcsx2_macos.validate_gamesettings_ini(ini_text), [])
            self.assertIn("AspectRatio = Stretch", ini_text)
            self.assertIn("upscale_multiplier = 6", ini_text)
            self.assertIn("EECycleRate = 2", ini_text)
            self.assertIn("Enable = Widescreen 21:9 (Runtime Confirm)", ini_text)
            self.assertTrue((pcsx2_dir / "patches" / f"SLUS-21351_{mod_crc}.pnach").is_file())

            summary = out_buf.getvalue()
            self.assertIn(mod_crc, summary)
            self.assertIn("AspectRatio=Stretch", summary)

    def test_real_end_to_end_synthetic_iso_build_and_pcsx2_install(self) -> None:
        """Full unmocked subprocess integration using a compact synthetic ISO9660 image."""
        elf_spec = json.loads((REPO_ROOT / "patches" / "elf_patches.json").read_text(encoding="utf-8"))
        purist_profile = json.loads(
            (REPO_ROOT / "patches" / "profiles" / "rockport2026_purist_hd.json").read_text(encoding="utf-8")
        )
        elf_bytes = _build_synthetic_elf_for_real_patches(
            elf_spec, patch_ids=purist_profile["elf_patches"]["include_ids"]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_iso = root / "synthetic_stock.iso"
            _build_synthetic_iso_with_elf(src_iso, elf_bytes)
            src_sha_before = _sha256(src_iso)

            out_iso = root / "out" / "purist_hd_mod.iso"
            pcsx2_dir = root / "PCSX2"
            (pcsx2_dir / "inis").mkdir(parents=True)
            global_ini = pcsx2_dir / "inis" / "PCSX2.ini"
            global_ini.write_text("[EmuCore/GS]\nRenderer = 2\n", encoding="utf-8")
            global_before = _sha256(global_ini)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rockport_cli.main(
                    [
                        "build",
                        "--profile",
                        "rockport2026_purist_hd",
                        "--source",
                        str(src_iso),
                        "--output",
                        str(out_iso),
                        "--install-pcsx2",
                        "--pcsx2-dir",
                        str(pcsx2_dir),
                        "--upscale",
                        "6",
                        "--ee-cycle-rate",
                        "2",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0, buf.getvalue())
            res = json.loads(buf.getvalue())
            self.assertEqual(res["status"], "ok")
            self.assertTrue(out_iso.is_file())
            self.assertEqual(_sha256(src_iso), src_sha_before)
            self.assertEqual(_sha256(global_ini), global_before)
            self.assertEqual(res["source_sha256_before"], res["source_sha256_after"])
            self.assertEqual(len(res["applied_elf_patches"]), 7)

            mod_crc = res["output_elf_crc"]
            self.assertNotEqual(mod_crc, "0518D274")

            # Companion .pnach must lint cleanly
            comp_pnach = Path(res["companion_pnach"])
            self.assertTrue(comp_pnach.is_file())
            self.assertEqual(lint_pnach.lint_pnach_file(comp_pnach), [])

            # Installed gamesettings INI must validate cleanly against PCSX2 v2.6.3
            ini_path = Path(res["pcsx2"]["ini_path"])
            self.assertTrue(ini_path.is_file())
            ini_text = ini_path.read_text(encoding="utf-8")
            self.assertEqual(setup_pcsx2_macos.validate_gamesettings_ini(ini_text), [])
            self.assertIn("AspectRatio = 16:9", ini_text)
            self.assertIn("upscale_multiplier = 6", ini_text)
            self.assertIn("EECycleRate = 2", ini_text)
            self.assertIn("Enable = Widescreen 16:9 (Runtime Confirm)", ini_text)


class TestV03ProfilesAndLightingDualSync(unittest.TestCase):
    """Validate v0.3 daylight/golden-hour profiles and VLT+Lighting GLOBALMEMORYFILE.BIN dual sync."""

    def test_v03_and_golden_hour_profiles_validate_with_lighting_and_roster(self) -> None:
        report = rockport_cli.collect_profiles(repo_root=REPO_ROOT)
        self.assertEqual(report["status"], "pass", f"Profiles failed: {report}")
        self.assertGreaterEqual(report["profile_count"], 6)
        by_id = {p["profile_id"]: p for p in report["profiles"]}

        for pid, expected_light_count in (
            ("rockport2026_v0.3", 37),
            ("rockport2026_golden_hour", 52),
        ):
            self.assertIn(pid, by_id)
            prof = by_id[pid]
            self.assertTrue(prof["valid"], f"{pid} invalid: {prof['errors']}")
            self.assertEqual(prof["elf_patch_count"], 7)
            self.assertEqual(prof["vlt_recipe_count"], 5)
            self.assertEqual(prof["vlt_edit_count"], 360)
            self.assertEqual(prof["lighting_recipe_count"], 1)
            self.assertEqual(prof["lighting_edit_count"], expected_light_count)

    def test_validator_rejects_missing_lighting_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "patches" / "profiles").mkdir(parents=True)
            (root / "patches" / "elf_patches.json").write_text(
                (REPO_ROOT / "patches" / "elf_patches.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            bad = {
                "schema_version": 1,
                "profile_id": "bad_light",
                "name": "Bad Lighting",
                "output_filename": "bad_light.iso",
                "elf_patches": {"spec": "patches/elf_patches.json", "include_ids": ["remove_brown_filter"]},
                "lighting_recipes": ["patches/lighting/nonexistent.json"],
            }
            (root / "patches" / "profiles" / "bad_light.json").write_text(json.dumps(bad), encoding="utf-8")
            report = rockport_cli.collect_profiles(repo_root=root)
            self.assertEqual(report["status"], "fail")
            self.assertIn("Missing lighting recipe", "\n".join(report["profiles"][0]["errors"]))

    def test_stage_lighting_recipes_chains_existing_memfile_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            src_iso = td / "src.iso"
            src_iso.write_bytes(b"ISO")
            existing_mf = td / "vlt_memfile.bin"
            existing_mf.write_bytes(b"VLT_SYNCED_MF")
            staging = td / "staging"
            recorded: List[List[str]] = []

            def fake_run(cmd, **kwargs):
                recorded.append(list(cmd))
                out_lzc = Path(cmd[cmd.index("--out-globalb") + 1])
                out_bun = Path(cmd[cmd.index("--out-bun") + 1])
                out_mf = Path(cmd[cmd.index("--memfile-out") + 1])
                out_lzc.parent.mkdir(parents=True, exist_ok=True)
                out_lzc.write_bytes(b"JDLZ_STUB")
                out_bun.write_bytes(b"BUN_STUB")
                out_mf.write_bytes(b"DUAL_MEMFILE_STUB")
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            with mock.patch("build_modded_iso.subprocess.run", side_effect=fake_run):
                staged = build_modded_iso.stage_lighting_recipes(
                    src_iso,
                    ["patches/lighting/rockport2026_daylight.json"],
                    REPO_ROOT,
                    staging,
                    existing_memfile=existing_mf,
                )

            self.assertEqual(len(recorded), 1)
            self.assertIn("--in-memfile", recorded[0])
            self.assertEqual(recorded[0][recorded[0].index("--in-memfile") + 1], str(existing_mf))
            self.assertIn(r"GLOBAL\GLOBALB.LZC", staged)
            self.assertIn(r"GLOBAL\GLOBALB.BUN", staged)
            self.assertIn(r"GLOBAL\GLOBALMEMORYFILE.BIN", staged)


if __name__ == "__main__":
    unittest.main()
