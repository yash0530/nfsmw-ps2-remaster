#!/usr/bin/env python3
"""Unit and integration tests for PS2 GS TPK pixel decoding, perceptual dump matching,
and CC0/HUD replacement rule deployment.

All core tests run fully offline in CI using synthetic PS2 TPK archives and
generated images without requiring the NFSMW ISO. An optional live ISO read-only
smoke test executes when an ISO is present on the machine (skipped cleanly in CI).
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import match_texture_dumps as mtd  # noqa: E402
import tpk_extract as tx  # noqa: E402
import tpk_inspect as tpki  # noqa: E402


def _ea_chunk(chunk_id: int, payload: bytes) -> bytes:
    return struct.pack("<II", chunk_id & 0xFFFFFFFF, len(payload)) + payload


def _make_tex_info_and_plat(
    name: str,
    name_hash: int,
    width: int,
    height: int,
    image_offset: int,
    palette_offset: int,
    image_size: int,
    palette_size: int,
    psm: int,
    clut_psm: int = tx.PS2_PSMCT32,
    num_palette_entries: int = 0,
    is_swizzled: bool = True,
) -> Tuple[bytes, bytes]:
    ti = bytearray(tpki.TEXTURE_INFO_SIZE)
    raw_name = name.encode("ascii")[:23]
    ti[12 : 12 + len(raw_name)] = raw_name
    struct.pack_into("<III", ti, 36, name_hash, 0x11111111, 0)
    struct.pack_into("<IIIII", ti, 48, image_offset, palette_offset, image_size, palette_size, image_size)
    struct.pack_into("<HHBBBBHB", ti, 68, width, height, 4, 4, 0, 0, num_palette_entries, 1)

    pi = bytearray(tpki.TEXTURE_PLAT_SIZE)
    struct.pack_into("<I", pi, 0, 0x00010000)
    pi[4] = psm & 0xFF
    pi[5] = clut_psm & 0xFF
    struct.pack_into("<hhHBB", pi, 16, width, height, 0, 2, 1 if is_swizzled else 0)
    return bytes(ti), bytes(pi)


def _build_synthetic_uncompressed_tpk(
    textures: List[Dict[str, object]],
    with_128_pad: bool = True,
) -> bytes:
    ti_blob = bytearray()
    pi_blob = bytearray()
    payload_blob = bytearray()

    for spec in textures:
        img_bytes: bytes = spec["img_bytes"]  # type: ignore[assignment]
        pal_bytes: bytes = spec["pal_bytes"]  # type: ignore[assignment]
        img_off = len(payload_blob)
        payload_blob.extend(img_bytes)
        pal_off = len(payload_blob) if pal_bytes else 0
        payload_blob.extend(pal_bytes)

        ti, pi = _make_tex_info_and_plat(
            name=str(spec["name"]),
            name_hash=int(spec["name_hash"]),  # type: ignore[arg-type]
            width=int(spec["width"]),  # type: ignore[arg-type]
            height=int(spec["height"]),  # type: ignore[arg-type]
            image_offset=img_off,
            palette_offset=pal_off,
            image_size=len(img_bytes),
            palette_size=len(pal_bytes),
            psm=int(spec["psm"]),  # type: ignore[arg-type]
            clut_psm=int(spec.get("clut_psm", tx.PS2_PSMCT32)),  # type: ignore[arg-type]
            num_palette_entries=int(spec.get("num_palette_entries", 0)),  # type: ignore[arg-type]
            is_swizzled=bool(spec.get("is_swizzled", True)),
        )
        ti_blob.extend(ti)
        pi_blob.extend(pi)

    info_chunk = _ea_chunk(
        tpki.CHUNK_TPK_INFO,
        _ea_chunk(tpki.CHUNK_TPK_TEX_INFO, bytes(ti_blob))
        + _ea_chunk(tpki.CHUNK_TPK_TEX_PLAT, bytes(pi_blob)),
    )
    data_info = _ea_chunk(tpki.CHUNK_TPK_DATA_INFO, b"\x00" * 24)

    # Calculate offset from root(0) to sp_start so we can replicate EA's 128-byte 0x11 padding.
    header_before_sp = 8 + len(info_chunk) + 8 + len(data_info) + 8
    pad_len = (((header_before_sp + 127) & ~127) - header_before_sp) if with_128_pad else 0
    padded_payload = (b"\x11" * pad_len) + bytes(payload_blob)
    data_chunk = _ea_chunk(
        tpki.CHUNK_TPK_DATA,
        data_info + _ea_chunk(tpki.CHUNK_TPK_DATA_PAYLOAD, padded_payload),
    )
    return _ea_chunk(tpki.CHUNK_TPK_ROOT, info_chunk + data_chunk)


def _build_synthetic_streamed_tpk(spec: Dict[str, object]) -> bytes:
    img_bytes: bytes = spec["img_bytes"]  # type: ignore[assignment]
    pal_bytes: bytes = spec["pal_bytes"]  # type: ignore[assignment]
    ti, pi = _make_tex_info_and_plat(
        name=str(spec["name"]),
        name_hash=int(spec["name_hash"]),  # type: ignore[arg-type]
        width=int(spec["width"]),  # type: ignore[arg-type]
        height=int(spec["height"]),  # type: ignore[arg-type]
        image_offset=0,
        palette_offset=len(img_bytes),
        image_size=len(img_bytes),
        palette_size=len(pal_bytes),
        psm=int(spec["psm"]),  # type: ignore[arg-type]
        clut_psm=int(spec.get("clut_psm", tx.PS2_PSMCT32)),  # type: ignore[arg-type]
        num_palette_entries=int(spec.get("num_palette_entries", 0)),  # type: ignore[arg-type]
        is_swizzled=bool(spec.get("is_swizzled", True)),
    )
    block = img_bytes + pal_bytes + ti + pi
    # Pre-compute root prefix size to find exact file offset of appended stream block
    dummy_entry = struct.pack("<IIIIII", int(spec["name_hash"]), 0, len(block), len(block), 0, 0)  # type: ignore[arg-type]
    dummy_info = _ea_chunk(tpki.CHUNK_TPK_INFO, _ea_chunk(tpki.CHUNK_TPK_STREAM_TABLE, dummy_entry))
    block_off = 8 + len(dummy_info)
    real_entry = struct.pack("<IIIIII", int(spec["name_hash"]), block_off, len(block), len(block), 0, 0)  # type: ignore[arg-type]
    real_info = _ea_chunk(tpki.CHUNK_TPK_INFO, _ea_chunk(tpki.CHUNK_TPK_STREAM_TABLE, real_entry))
    root = _ea_chunk(tpki.CHUNK_TPK_ROOT, real_info)
    assert len(root) == block_off
    return root + block


def _find_live_iso() -> Optional[Path]:
    if "NFSMW_ISO" in os.environ:
        env_iso = os.environ["NFSMW_ISO"].strip()
        if not env_iso:
            return None
        p = Path(env_iso).expanduser()
        return p if p.is_file() else None
    cand = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    return cand if cand.is_file() else None


class TestPS2GSPixelAndClutDecoding(unittest.TestCase):
    def test_csm1_clut_tile_swap_and_gs_alpha_scaling(self) -> None:
        pal_rgba = np.zeros((256, 4), dtype=np.uint8)
        for i in range(256):
            pal_rgba[i] = [i, (i * 3) & 0xFF, (255 - i) & 0xFF, 255 if (i % 2 == 0) else 128]
        # Halve alpha (0x80 for 255, 0x40 for 128) and swizzle into PS2 CSM1 layout
        swizzled = tx.swizzle_clut_csm1(pal_rgba, halving_alpha=True)
        raw_words = np.frombuffer(swizzled, dtype=np.uint8).reshape(256, 4)
        # Verify tile 1 (entries 8..15) ended up at indices 16..23 in swizzled bytes
        self.assertEqual(raw_words[16, 0], 8)
        self.assertEqual(raw_words[8, 0], 16)
        self.assertEqual(raw_words[16, 3], 128)
        self.assertEqual(raw_words[17, 3], 64)

        decoded = tx.unswizzle_clut_csm1(swizzled, entries=256, scale_alpha=True)
        # 128 * 2 -> clamped 255; 64 * 2 -> 128
        expected = pal_rgba.copy()
        np.testing.assert_array_equal(decoded, expected)

    def test_psmt8_swizzle_unswizzle_and_decode_exact(self) -> None:
        w, h = 64, 64
        # Build asymmetric pattern with gradients + diagonal stripes so any bit error fails loudly
        yy, xx = np.mgrid[0:h, 0:w]
        indices = ((xx * 3 + yy * 5 + (xx > yy) * 41) % 256).astype(np.uint8)
        pal_rgba = np.zeros((256, 4), dtype=np.uint8)
        for i in range(256):
            pal_rgba[i] = [(i * 7) & 0xFF, (i * 11) & 0xFF, (i * 13) & 0xFF, 255]

        swiz_img = tx.swizzle8(indices.tobytes(), w, h)
        swiz_pal = tx.swizzle_clut_csm1(pal_rgba, halving_alpha=True)
        self.assertEqual(tx.unswizzle8(swiz_img, w, h), indices.tobytes())

        img = tx.decode_ps2_texture(
            swiz_img, swiz_pal, width=w, height=h, psm=tx.PS2_PSMT8, num_palette_entries=256, is_swizzled=True
        )
        expected_rgba = pal_rgba[indices]
        np.testing.assert_array_equal(np.asarray(img), expected_rgba)

    def test_psmt8_linear_unswizzled_decode(self) -> None:
        w, h = 8, 128
        yy, xx = np.mgrid[0:h, 0:w]
        indices = ((yy * 2 + xx) % 256).astype(np.uint8)
        pal_rgba = np.stack(
            [
                np.arange(256, dtype=np.uint8),
                np.full(256, 120, dtype=np.uint8),
                (255 - np.arange(256, dtype=np.uint8)),
                np.full(256, 255, dtype=np.uint8),
            ],
            axis=1,
        )
        swiz_pal = tx.swizzle_clut_csm1(pal_rgba, halving_alpha=True)
        img = tx.decode_ps2_texture(
            indices.tobytes(), swiz_pal, width=w, height=h, psm=tx.PS2_PSMT8, num_palette_entries=256, is_swizzled=False
        )
        np.testing.assert_array_equal(np.asarray(img), pal_rgba[indices])

    def test_psmt4_swizzle_unswizzle_all_block_geometries(self) -> None:
        pal_rgba = np.array(
            [
                [0, 0, 0, 0] if i == 0 else [(i * 17) & 0xFF, (255 - i * 15) & 0xFF, (i * 29) & 0xFF, 255]
                for i in range(16)
            ],
            dtype=np.uint8,
        )
        swiz_pal = tx.swizzle_clut_csm1(pal_rgba, halving_alpha=True)
        for w, h in [(32, 32), (64, 64), (128, 32), (128, 128)]:
            with self.subTest(size=(w, h)):
                yy, xx = np.mgrid[0:h, 0:w]
                # Asymmetric arrowhead + quadrant marker pattern
                indices = (((xx // 3) + (yy // 5) + ((xx > w // 2) * 3) + ((yy > xx) * 5)) % 16).astype(np.uint8)
                swiz_img = tx.swizzle4(indices.tobytes(), w, h)
                self.assertEqual(len(swiz_img), (w * h) // 2)
                unswz = np.frombuffer(tx.unswizzle4(swiz_img, w, h), dtype=np.uint8).reshape(h, w)
                np.testing.assert_array_equal(unswz, indices)

                decoded = tx.decode_ps2_texture(
                    swiz_img, swiz_pal, width=w, height=h, psm=tx.PS2_PSMT4, num_palette_entries=16, is_swizzled=True
                )
                np.testing.assert_array_equal(np.asarray(decoded), pal_rgba[indices])

    def test_psmct32_and_psmct24_decode(self) -> None:
        w, h = 16, 16
        raw32 = np.zeros((h, w, 4), dtype=np.uint8)
        raw32[:, :, 0] = 200
        raw32[:, :, 1] = 100
        raw32[:, :, 2] = 50
        raw32[:, :, 3] = 0x80
        img32 = tx.decode_ps2_texture(
            raw32.tobytes(), b"", width=w, height=h, psm=tx.PS2_PSMCT32, is_swizzled=False, scale_alpha=True
        )
        arr32 = np.asarray(img32)
        self.assertTrue((arr32[:, :, 3] == 255).all())

        raw24 = raw32[:, :, :3].tobytes()
        img24 = tx.decode_ps2_texture(
            raw24, b"", width=w, height=h, psm=tx.PS2_PSMCT24, is_swizzled=False
        )
        self.assertTrue((np.asarray(img24)[:, :, 3] == 255).all())


class TestSyntheticTPKExtraction(unittest.TestCase):
    def test_extract_uncompressed_padded_and_streamed_tpks(self) -> None:
        pal16 = np.array([[i * 16, 255 - i * 16, (i * 37) % 255, 255] for i in range(16)], dtype=np.uint8)
        yy, xx = np.mgrid[0:64, 0:64]
        idx4 = ((xx + 2 * yy) % 16).astype(np.uint8)
        swiz4 = tx.swizzle4(idx4.tobytes(), 64, 64)
        swiz_pal4 = tx.swizzle_clut_csm1(pal16, halving_alpha=True)

        pal256 = np.array([[i, (i * 5) & 0xFF, (i * 9) & 0xFF, 255] for i in range(256)], dtype=np.uint8)
        yy8, xx8 = np.mgrid[0:32, 0:32]
        idx8 = ((xx8 * 7 + yy8 * 13) % 256).astype(np.uint8)
        swiz8 = tx.swizzle8(idx8.tobytes(), 32, 32)
        swiz_pal8 = tx.swizzle_clut_csm1(pal256, halving_alpha=True)

        uncomp_tpk = _build_synthetic_uncompressed_tpk(
            [
                {
                    "name": "METER_BACKING",
                    "name_hash": 0x11223344,
                    "width": 64,
                    "height": 64,
                    "img_bytes": swiz4,
                    "pal_bytes": swiz_pal4,
                    "psm": tx.PS2_PSMT4,
                    "num_palette_entries": 16,
                }
            ],
            with_128_pad=True,
        )
        streamed_tpk = _build_synthetic_streamed_tpk(
            {
                "name": "ROAD_ASPHALT_MAIN",
                "name_hash": 0x55667788,
                "width": 32,
                "height": 32,
                "img_bytes": swiz8,
                "pal_bytes": swiz_pal8,
                "psm": tx.PS2_PSMT8,
                "num_palette_entries": 256,
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            items1 = tx.extract_tpk_bytes(uncomp_tpk, source="SYNTH_HUD.BIN", out_dir=tmp_dir / "hud")
            items2 = tx.extract_tpk_bytes(streamed_tpk, source="SYNTH_ROAD.BIN", out_dir=tmp_dir / "road")

            self.assertEqual(len(items1), 1)
            self.assertEqual(items1[0].name, "METER_BACKING")
            self.assertTrue(Path(items1[0].png_path or "").is_file())
            with Image.open(items1[0].png_path) as im1:
                np.testing.assert_array_equal(np.asarray(im1), pal16[idx4])

            self.assertEqual(len(items2), 1)
            self.assertEqual(items2[0].name, "ROAD_ASPHALT_MAIN")
            self.assertTrue(Path(items2[0].png_path or "").is_file())
            with Image.open(items2[0].png_path) as im2:
                np.testing.assert_array_equal(np.asarray(im2), pal256[idx8])


class TestPerceptualMatcherAndDeployment(unittest.TestCase):
    @staticmethod
    def _make_sample_sprites() -> Dict[str, Image.Image]:
        # 1. Tachometer ring
        tach = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        d1 = ImageDraw.Draw(tach)
        d1.arc([14, 14, 114, 114], start=140, end=40, fill=(245, 245, 250, 255), width=12)

        # 2. Directional arrow
        arrow = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(arrow)
        d2.polygon([(32, 6), (54, 54), (32, 42), (10, 54)], fill=(255, 255, 255, 255))

        # 3. Asphalt road noise + center divider stripes (2:1)
        rng = np.random.default_rng(42)
        asphalt_arr = np.zeros((64, 128, 4), dtype=np.uint8)
        asphalt_arr[:, :, :3] = rng.integers(38, 72, size=(64, 128, 3), dtype=np.uint8)
        asphalt_arr[:, :, 3] = 255
        asphalt_arr[28:36, 16:48, :3] = [215, 185, 65]
        asphalt_arr[28:36, 76:108, :3] = [215, 185, 65]
        asphalt = Image.fromarray(asphalt_arr)

        # 4. Pure-alpha mask arc (black RGB + curved alpha silhouette)
        mask = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d4 = ImageDraw.Draw(mask)
        d4.arc([8, 8, 56, 56], start=180, end=330, fill=(0, 0, 0, 255), width=8)

        # 5. Diagonal carbon-weave tile
        yy, xx = np.mgrid[0:64, 0:64]
        weave = (((xx + yy) % 8 < 4) * 45 + 30).astype(np.uint8)
        carbon_arr = np.stack([weave, weave, weave, np.full_like(weave, 255)], axis=2)
        carbon = Image.fromarray(carbon_arr)

        return {
            "METER_BACKING": tach,
            "RADAR_DIRECTIONARROW": arrow,
            "ROAD_ASPHALT_01": asphalt,
            "HEATMETERMASKTOP": mask,
            "CAR_CARBON_HOOD": carbon,
        }

    def test_dhash_phash_robustness_under_resize_and_jpeg_recompress(self) -> None:
        sprites = self._make_sample_sprites()
        for name, orig in sprites.items():
            sig_orig = mtd.compute_signature(orig)
            # Resized + slight RGB lossy JPEG roundtrip on opaque or 2x bilinear RGBA resize
            resized = orig.resize((orig.width * 2, orig.height * 2), Image.Resampling.BILINEAR)
            sig_res = mtd.compute_signature(resized)
            self.assertLessEqual(mtd.hamming_distance(sig_orig.dhash, sig_res.dhash), 6, msg=name)
            self.assertLessEqual(mtd.hamming_distance(sig_orig.phash, sig_res.phash), 6, msg=name)

            # JPEG compression roundtrip on opaque composited image (for world surfaces)
            if name in ("ROAD_ASPHALT_01", "CAR_CARBON_HOOD"):
                buf = io.BytesIO()
                resized.convert("RGB").save(buf, format="JPEG", quality=82)
                buf.seek(0)
                with Image.open(buf) as j_im:
                    sig_jpg = mtd.compute_signature(j_im)
                self.assertLessEqual(mtd.hamming_distance(sig_orig.dhash, sig_jpg.dhash), 6, msg=name)

    def test_match_and_deploy_pipeline_offline_with_real_rules(self) -> None:
        rules_path = REPO_ROOT / "assets_cc0" / "material_pack_rules.json"
        self.assertTrue(rules_path.is_file())
        sprites = self._make_sample_sprites()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tpk_dir = root / "tpk_extracted"
            hud_arc = tpk_dir / "GLOBAL_HUDTEXRACE"
            hud_arc.mkdir(parents=True)
            manifest_texs = []
            hashes = {
                "METER_BACKING": "20865c0a",
                "RADAR_DIRECTIONARROW": "24196ad4",
                "ROAD_ASPHALT_01": "9076f58f",
                "HEATMETERMASKTOP": "aabbccdd",
                "CAR_CARBON_HOOD": "deadbeef",
            }
            for idx_i, (name, im) in enumerate(sprites.items()):
                p = hud_arc / f"{idx_i:03d}_{name}_{hashes[name]}.png"
                im.save(p)
                manifest_texs.append(
                    {
                        "archive": "SYNTH",
                        "name": name,
                        "name_hash": f"0x{hashes[name]}",
                        "width": im.width,
                        "height": im.height,
                        "ps2_psm_name": "PSMT4",
                        "png_path": str(p),
                    }
                )
            (hud_arc / "manifest.json").write_text(
                json.dumps({"textures": manifest_texs}, indent=2), encoding="utf-8"
            )

            # Build fake PCSX2 dump directory (resized dumps + one invalid non-PCSX2 stem)
            dumps_dir = root / "dumps"
            dumps_dir.mkdir()
            sprites["METER_BACKING"].resize((256, 256), Image.Resampling.BILINEAR).save(
                dumps_dir / "20865c0a-90000001.png"
            )
            sprites["RADAR_DIRECTIONARROW"].resize((96, 96), Image.Resampling.BICUBIC).save(
                dumps_dir / "24196ad4-90000002.png"
            )
            sprites["ROAD_ASPHALT_01"].resize((256, 128), Image.Resampling.BILINEAR).save(
                dumps_dir / "9076f58f.png"
            )
            sprites["CAR_CARBON_HOOD"].resize((128, 128), Image.Resampling.BILINEAR).save(
                dumps_dir / "deadbeef-90000003.png"
            )
            sprites["HEATMETERMASKTOP"].resize((128, 128), Image.Resampling.BILINEAR).save(
                dumps_dir / "not_a_pcsx2_hash.png"
            )

            # Build synthetic HUD kit + 4K/high-res CC0 baked assets
            hud_dir = root / "hud_pack"
            for theme_name in ("motorsport2026", "rockport_neon"):
                tdir = hud_dir / theme_name
                tdir.mkdir(parents=True)
                Image.new("RGBA", (512, 512), (240, 240, 240, 255)).save(tdir / "hud_tach_dial.png")
                Image.new("RGBA", (256, 256), (0, 200, 255, 255)).save(tdir / "hud_player_arrow.png")
                Image.new("RGBA", (256, 256), (255, 60, 0, 255)).save(tdir / "hud_heat_4.png")

            baked_dir = root / "cc0_baked"
            baked_dir.mkdir()
            Image.new("RGBA", (1024, 1024), (55, 55, 58, 255)).save(baked_dir / "asphalt_02_dry_4k.png")
            Image.new("RGBA", (1024, 1024), (32, 32, 35, 255)).save(baked_dir / "carbon_fiber_4k.png")

            matches_json = root / "matched_hashes.json"
            report = mtd.match_dumps_directory(
                dumps_dir=dumps_dir,
                tpk_dir=tpk_dir,
                rules_path=rules_path,
                out_path=matches_json,
                theme="rockport_neon",
            )
            self.assertEqual(report["dump_count"], 5)
            self.assertEqual(report["matched_count"], 5)
            self.assertEqual(report["rule_matched_count"], 5)

            by_dump = {m["dump_name"]: m for m in report["matches"]}
            self.assertEqual(by_dump["20865c0a-90000001.png"]["best_match"]["tpk_name"], "METER_BACKING")
            self.assertEqual(by_dump["20865c0a-90000001.png"]["rule"]["rule_id"], "hud_tach_dial")
            self.assertEqual(by_dump["20865c0a-90000001.png"]["rule"]["asset"], "rockport_neon/hud_tach_dial.png")
            self.assertEqual(by_dump["9076f58f.png"]["rule"]["rule_id"], "road_asphalt_dry")
            self.assertEqual(by_dump["deadbeef-90000003.png"]["rule"]["rule_id"], "car_carbon_fiber")
            self.assertEqual(by_dump["not_a_pcsx2_hash.png"]["best_match"]["tpk_name"], "HEATMETERMASKTOP")

            staged_dir = root / "staged_replacements"
            dep = mtd.deploy_matches(
                matches_path=matches_json,
                hud_dir=hud_dir,
                baked_dir=baked_dir,
                out_dir=staged_dir,
            )
            self.assertEqual(dep["staged_count"], 4)
            self.assertEqual(dep["skipped_count"], 1)  # not_a_pcsx2_hash.png skipped by strict naming
            staged_files = {p.name for p in staged_dir.glob("*.png")}
            self.assertEqual(
                staged_files,
                {"20865c0a-90000001.png", "24196ad4-90000002.png", "9076f58f.png", "deadbeef-90000003.png"},
            )
            # Check aspect-preserving 2:1 crop & mod-4 alignment on road replacement
            with Image.open(staged_dir / "9076f58f.png") as road_im:
                self.assertEqual(road_im.width, road_im.height * 2)
                self.assertEqual(road_im.width % 4, 0)
                self.assertEqual(road_im.height % 4, 0)


class TestOptionalLiveISOReadOnly(unittest.TestCase):
    @unittest.skipUnless(_find_live_iso() is not None, "NFSMW PS2 ISO not found locally")
    def test_live_iso_hudtexrace_extraction_readonly(self) -> None:
        iso_path = _find_live_iso()
        assert iso_path is not None
        st_before = iso_path.stat()

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "tpk_out"
            summary = tx.extract_iso_tpks(
                iso_path=iso_path,
                targets=[r"GLOBAL\HUDTEXRACE.BIN"],
                out_dir=out_dir,
            )
            self.assertEqual(summary["total_textures"], 23)
            by_name = {t["name"]: t for t in summary["textures"]}
            for req in ("METER_BACKING", "RADAR_BACKING", "RADAR_DIRECTIONARROW", "COPS_ICON", "HEATMETERMASKTOP"):
                self.assertIn(req, by_name)
                p = Path(by_name[req]["png_path"])
                self.assertTrue(p.is_file())
                with Image.open(p) as im:
                    self.assertEqual(im.size, (by_name[req]["width"], by_name[req]["height"]))
                    # Verify non-empty alpha coverage
                    alpha = np.asarray(im.convert("RGBA"))[:, :, 3]
                    self.assertGreater(int((alpha > 0).sum()), 16)

        st_after = iso_path.stat()
        self.assertEqual((st_before.st_size, st_before.st_mtime_ns), (st_after.st_size, st_after.st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
