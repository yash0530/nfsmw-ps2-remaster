#!/usr/bin/env python3
"""Cryptographic zero-edit ISO round-trip tests for tools/build_modded_iso.py.

1. Offline (always runs in CI): proves zero-patch round-trip on a synthetic ISO9660 image
   produces a byte-for-byte SHA-256 identical copy while keeping the source strictly read-only.
2. Live local (runs when untouched SLUS-21351 ISO is present; skipped when NFSMW_ISO=/nonexistent):
   proves zero-edit round-trip against the real retail ISO (`cef019c5...`, opened `'rb'`),
   writing to a temp directory and deleting the copy immediately.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
for _d in (REPO_ROOT / "tools", REPO_ROOT / "tests"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import build_modded_iso  # noqa: E402
from iso_inspect import SECTOR_SIZE, compute_pcsx2_elf_crc, parse_iso9660, sha256_file  # noqa: E402
from test_zdir_and_jdlz import build_synthetic_nfsmw_iso  # noqa: E402

EXPECTED_RETAIL_SHA256_PREFIX = "cef019c5"


def _find_local_iso() -> Optional[Path]:
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso is not None:
        p = Path(env_iso).expanduser()
        return p if p.is_file() else None
    default_iso = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    return default_iso if default_iso.is_file() else None


def _write_zero_patch_spec(iso_path: Path, spec_path: Path) -> str:
    with iso_path.open("rb") as f:
        _, _, entries = parse_iso9660(f)
        elf_meta = next(e for e in entries if not e["is_dir"] and e["path"].upper() == "SLUS_213.51")
        f.seek(elf_meta["lba"] * SECTOR_SIZE)
        crc = f"{compute_pcsx2_elf_crc(f.read(elf_meta['size'])):08X}"
    spec = {
        "schema_version": 1,
        "target": {
            "serial": "SLUS-21351",
            "elf_name": "SLUS_213.51",
            "elf_pcsx2_crc": crc,
            "pt_load_vaddr": "0x00100000",
            "pt_load_file_offset": "0x00000100",
        },
        "patches": [],
    }
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return crc


class TestZeroEditIsoRoundTrip(unittest.TestCase):
    def test_synthetic_iso_zero_edit_cli_roundtrip_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nfsmw_iso_rt_synth_") as td:
            td_path = Path(td)
            src_iso, dst_iso = td_path / "source_synth.iso", td_path / "roundtrip_synth.iso"
            spec_path = td_path / "empty_patches.json"
            build_synthetic_nfsmw_iso(src_iso, free_tail_sectors=2)
            orig_crc = _write_zero_patch_spec(src_iso, spec_path)
            src_sha_before, src_size = sha256_file(src_iso), src_iso.stat().st_size

            # Mark source read-only to guarantee zero writes to source
            src_iso.chmod(0o444)
            try:
                cmd = [
                    sys.executable, str(REPO_ROOT / "tools" / "build_modded_iso.py"),
                    "--source", str(src_iso), "--output", str(dst_iso),
                    "--patches", str(spec_path), "--only", "--verify-source-sha256",
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            finally:
                src_iso.chmod(0o644)

            self.assertEqual(proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
            self.assertEqual(dst_iso.stat().st_size, src_size)
            self.assertEqual(sha256_file(dst_iso), src_sha_before)
            self.assertEqual(sha256_file(src_iso), src_sha_before)

            manifest = json.loads(dst_iso.with_suffix(".build-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_elf_crc"], orig_crc)
            self.assertEqual(manifest["output_elf_crc"], orig_crc)
            self.assertEqual(manifest["applied_elf_patches"], [])
            self.assertEqual(manifest["applied_zz_overrides"], [])
            self.assertFalse(manifest["iso_layout_update"]["pvd_updated"])
            self.assertEqual(manifest["iso_layout_update"]["zzdata3_appended_sectors"], 0)

    def test_synthetic_iso_zero_edit_stream_copy_path_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nfsmw_iso_rt_stream_") as td:
            td_path = Path(td)
            src_iso, dst_iso = td_path / "source_stream.iso", td_path / "roundtrip_stream.iso"
            build_synthetic_nfsmw_iso(src_iso, free_tail_sectors=1)
            src_sha = sha256_file(src_iso)

            with mock.patch.object(build_modded_iso.platform, "system", return_value="Linux"):
                copy_mode = build_modded_iso.clone_iso_non_destructively(src_iso, dst_iso)
            self.assertEqual(copy_mode, "stream-copy")

            with dst_iso.open("r+b") as f_dst:
                _, vol_sectors, entries = parse_iso9660(f_dst)
                by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
                applied_zz, layout = build_modded_iso.apply_zz_overrides(f_dst, by_path, {}, vol_sectors=vol_sectors)
            self.assertEqual(applied_zz, [])
            self.assertFalse(layout["pvd_updated"])
            self.assertEqual(sha256_file(dst_iso), src_sha)

    def test_live_untouched_slus21351_iso_zero_edit_roundtrip(self) -> None:
        src_iso = _find_local_iso()
        if src_iso is None:
            self.skipTest("Untouched local SLUS-21351 ISO not present (or offline CI mode)")

        src_sha_before = sha256_file(src_iso)
        self.assertTrue(
            src_sha_before.startswith(EXPECTED_RETAIL_SHA256_PREFIX),
            f"Expected retail SLUS-21351 ISO SHA-256 prefix {EXPECTED_RETAIL_SHA256_PREFIX}, got {src_sha_before}",
        )
        src_size = src_iso.stat().st_size

        with tempfile.TemporaryDirectory(prefix="nfsmw_iso_rt_live_") as td:
            dst_iso = Path(td) / "SLUS-21351_zero_edit_roundtrip.iso"
            cmd = [
                sys.executable, str(REPO_ROOT / "tools" / "build_modded_iso.py"),
                "--source", str(src_iso), "--output", str(dst_iso),
                "--only", "--verify-source-sha256",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
            self.assertEqual(dst_iso.stat().st_size, src_size)
            self.assertEqual(sha256_file(dst_iso), src_sha_before)
            self.assertEqual(sha256_file(src_iso), src_sha_before)

            manifest = json.loads(dst_iso.with_suffix(".build-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_elf_crc"], "0518D274")
            self.assertEqual(manifest["output_elf_crc"], "0518D274")
            self.assertEqual(manifest["applied_elf_patches"], [])
            self.assertEqual(manifest["applied_zz_overrides"], [])
            self.assertFalse(manifest["iso_layout_update"]["pvd_updated"])


if __name__ == "__main__":
    unittest.main()
