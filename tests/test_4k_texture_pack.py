#!/usr/bin/env python3
"""
tests/test_4k_texture_pack.py — Unit and integration tests for Wave 3 Path 1:
  - assets_cc0/car_livery_rules.json schema & hygiene
  - tools/install_macos_texture_tools.py toolchain inspection
  - tools/build_4k_texture_pack.py procedural CC0 texture generation + BC7 DDS validation
  - tools/dds_encoder_rs native Rust compilation + cargo test + BC7/BC3/BC1/RGBA8 CLI encoding
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from PIL import Image  # noqa: E402

import build_4k_texture_pack as pack_mod  # noqa: E402
import install_macos_texture_tools as tools_mod  # noqa: E402


class TestCarLiveryRulesAndTools(unittest.TestCase):
    def test_car_livery_rules_json_valid_and_references_generators(self) -> None:
        rules_path = REPO_ROOT / "assets_cc0" / "car_livery_rules.json"
        self.assertTrue(rules_path.is_file(), "assets_cc0/car_livery_rules.json must exist")
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("schema_version"), "1.0")
        rules = data.get("rules", [])
        self.assertGreaterEqual(len(rules), 7)

        seen_ids = set()
        for rule in rules:
            rid = rule["id"]
            self.assertNotIn(rid, seen_ids)
            seen_ids.add(rid)
            self.assertIn(rule["asset"], pack_mod.PROCEDURAL_GENERATORS)
            self.assertTrue(rule["dds_asset"].endswith(".dds"))
            self.assertEqual(rule["dds_format"], "bc7")
            self.assertTrue(rule["name_patterns"])
            self.assertIn("pbr_hints", rule)

    def test_install_macos_texture_tools_inspect(self) -> None:
        report = tools_mod.inspect_toolchain()
        self.assertEqual(report["schema_version"], "1.0")
        self.assertFalse(
            report["rogue_crate_target_present"],
            "tools/dds_encoder_rs/target/ must never exist inside tracked source dir",
        )
        self.assertIn("rockport_dds", report["binaries"])
        self.assertIn("PIL", report["python_packages"])
        self.assertTrue(report["python_packages"]["PIL"]["available"])

    def test_procedural_generators_produce_valid_rgba_images(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nfsmw_gen_test_") as tmp:
            out_dir = Path(tmp) / "gen"
            paths = pack_mod.generate_procedural_textures(out_dir, size=128)
            self.assertEqual(len(paths), len(pack_mod.PROCEDURAL_GENERATORS))
            for p in paths:
                self.assertTrue(p.is_file())
                with Image.open(p) as im:
                    self.assertEqual(im.size, (128, 128))
                    self.assertEqual(im.mode, "RGBA")

    def test_validate_bc7_dds_detects_bad_size_and_bad_magic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nfsmw_dds_val_") as tmp:
            bad = Path(tmp) / "bad.dds"
            bad.write_bytes(b"NOPE" + b"\x00" * 200)
            res = pack_mod.validate_bc7_dds(bad)
            self.assertFalse(res.valid)
            self.assertIn("Invalid DDS magic", res.error or "")

            # Valid 4x4 1-mip header with wrong payload length should fail
            hdr = bytearray(148)
            hdr[0:4] = b"DDS "
            struct.pack_into("<I", hdr, 4, 124)
            struct.pack_into("<I", hdr, 12, 4)
            struct.pack_into("<I", hdr, 16, 4)
            struct.pack_into("<I", hdr, 28, 3)  # mipCount=3 -> 4x4, 2x2, 1x1 => 3 blocks = 48 bytes
            struct.pack_into("<I", hdr, 76, 32)
            hdr[84:88] = b"DX10"
            struct.pack_into("<5I", hdr, 128, 98, 3, 0, 1, 0)
            bad_sz = Path(tmp) / "wrong_size.dds"
            bad_sz.write_bytes(bytes(hdr) + b"\x00" * 16)  # only 1 block instead of 3
            res2 = pack_mod.validate_bc7_dds(bad_sz)
            self.assertFalse(res2.valid)
            self.assertIn("Byte size mismatch", res2.error or "")


@unittest.skipUnless(shutil.which("cargo") is not None, "cargo not installed; skipping native Rust DDS tests")
class TestRustDdsEncoderIntegration(unittest.TestCase):
    def test_cargo_unit_tests_and_no_rogue_target(self) -> None:
        manifest = REPO_ROOT / "tools" / "dds_encoder_rs" / "Cargo.toml"
        target_dir = REPO_ROOT / "build" / "dds_wave3" / "target"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = str(target_dir)

        proc = subprocess.run(
            ["cargo", "test", "--manifest-path", str(manifest)],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"cargo test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertFalse(
            (REPO_ROOT / "tools" / "dds_encoder_rs" / "target").exists(),
            "tools/dds_encoder_rs/target/ must not be created",
        )

    def test_end_to_end_bc7_encode_from_tga_and_build_pack_smoke(self) -> None:
        encoder_bin = tools_mod.find_rockport_dds()
        if encoder_bin is None:
            encoder_bin = tools_mod.build_rockport_dds()

        with tempfile.TemporaryDirectory(prefix="nfsmw_bc7_smoke_") as tmp:
            tmp_path = Path(tmp)
            # 1. Test TGA (uncompressed 32-bit) -> rockport_dds -> BC7 DDS validation
            img = Image.new("RGBA", (64, 32))
            px = img.load()
            for y in range(32):
                for x in range(64):
                    px[x, y] = ((x * 4) % 256, (y * 8) % 256, 160, 255 if x % 3 else 128)
            tga_path = tmp_path / "sample.tga"
            img.save(tga_path, format="TGA")
            dds_path = tmp_path / "sample.dds"

            proc = subprocess.run(
                [
                    str(encoder_bin),
                    "--input",
                    str(tga_path),
                    "--output",
                    str(dds_path),
                    "--format",
                    "bc7",
                    "--mipmaps",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            val = pack_mod.validate_bc7_dds(dds_path, require_srgb=False)
            self.assertTrue(val.valid, val.error)
            self.assertEqual((val.width, val.height), (64, 32))
            self.assertEqual(val.mip_count, 7)  # 64->32->16->8->4->2->1
            self.assertEqual(val.dxgi_format, 98)

            # 2. Test BC3, BC1, RGBA8 CLI outputs
            for fmt, exp_dxgi in (("bc3", 77), ("bc1", 71), ("rgba8", 28)):
                out_f = tmp_path / f"sample_{fmt}.dds"
                p2 = subprocess.run(
                    [
                        str(encoder_bin),
                        "-i",
                        str(tga_path),
                        "-o",
                        str(out_f),
                        "-f",
                        fmt,
                        "--dx10",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(p2.returncode, 0, p2.stderr)
                hdr = out_f.read_bytes()[:148]
                self.assertEqual(hdr[0:4], b"DDS ")
                self.assertEqual(hdr[84:88], b"DX10")
                dxgi = struct.unpack_from("<I", hdr, 128)[0]
                self.assertEqual(dxgi, exp_dxgi)

            # 3. Smoke-run build_pack at 128px without baked/HUD dirs
            gen_dir = tmp_path / "generated"
            pack_dir = tmp_path / "pack"
            manifest_out = tmp_path / "pack_manifest.json"
            report = pack_mod.build_pack(
                size=128,
                generated_dir=gen_dir,
                dds_out_dir=pack_dir,
                manifest_path=manifest_out,
                cc0_baked_dir=None,
                hud_pack_dir=None,
                staged_manifest_path=None,
                include_baked=False,
                include_hud=False,
            )
            self.assertTrue(report["all_valid"])
            self.assertEqual(report["generated_count"], 7)
            self.assertEqual(report["encoded_count"], 7)
            for item in report["encoded"]:
                self.assertEqual(item["dxgi_format"], 98)
                self.assertEqual(item["mip_count"], 8)  # 128 -> 1x1
                self.assertTrue(item["valid"])


if __name__ == "__main__":
    unittest.main()
