#!/usr/bin/env python3
"""
tests/test_lighting_ps2.py — Unit & integration tests for PS2 NFSMW lighting pipeline
(`GLOBAL\\GLOBALB.LZC` SunInfos / LightMaterials / SunTrack & `GLOBAL\\GLOBALMEMORYFILE.BIN`).

Runs 100% offline in CI using clean-room synthetic bChunk/JDLZ/MemoryFile fixtures,
plus optional strictly read-only live verification when a local SLUS-21351 ISO exists.
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import audit_repo_clean  # noqa: E402
import lighting_ps2  # noqa: E402
from jdlz import is_jdlz, jdlz_compress, jdlz_decompress  # noqa: E402
from zdir_tool import PS2NFSMWArchive, bstringhash  # noqa: E402


def _find_local_iso() -> Optional[Path]:
    env_iso = os.environ.get("NFSMW_ISO")
    candidates = []
    if env_iso:
        candidates.append(Path(env_iso))
    candidates.append(
        Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    )
    for p in candidates:
        if p.is_file():
            return p
    return None


def _retail_mips_jdlz_sim(blob: bytes) -> bytes:
    """Exact opcode simulator for retail SLUS_213.51 `JLZDecompress` (`0x0031C778`)."""
    if blob[:4] != b"JDLZ" or blob[4] != 2:
        raise ValueError("Expected JDLZ v2 header")
    uncomp_size = struct.unpack_from("<I", blob, 8)[0]
    hdr_sz = blob[5]
    src = memoryview(blob)
    s = hdr_sz

    # SLUS_213.51 reads control FIRST, runtype SECOND
    flags1 = src[s]
    s += 1
    flags2 = src[s]
    s += 1
    bits1 = 8
    bits2 = 8
    out = bytearray()

    while len(out) < uncomp_size:
        if flags1 & 1:
            if flags2 & 1:
                b0 = src[s]
                b1 = src[s + 1]
                s += 2
                off = (b0 & 0x0F) + 1
                length = ((b0 >> 4) << 8) + b1 + 3
            else:
                b0 = src[s]
                b1 = src[s + 1]
                s += 2
                length = (b0 & 0x1F) + 3
                off = ((b0 >> 5) << 8) + b1 + 17
            for _ in range(length):
                out.append(out[-off])
                if len(out) == uncomp_size:
                    break
            flags2 >>= 1
            bits2 -= 1
            if bits2 == 0 and len(out) < uncomp_size:
                flags2 = src[s]
                s += 1
                bits2 = 8
        else:
            out.append(src[s])
            s += 1

        flags1 >>= 1
        bits1 -= 1
        if bits1 == 0 and len(out) < uncomp_size:
            flags1 = src[s]
            s += 1
            bits1 = 8

    return bytes(out)


def _pack_bchunk(chunk_id: int, payload: bytes) -> bytes:
    return struct.pack("<II", chunk_id, len(payload)) + payload


def _build_light_material_chunk(name: str, diff_rgb=(0.9, 0.57, 0.56), spec_scale=0.5) -> bytes:
    payload = bytearray(lighting_ps2.LIGHT_MATERIAL_PAYLOAD_SIZE)
    name_hash = bstringhash(name)
    struct.pack_into("<5I", payload, 0, 1, 3, 3, name_hash, lighting_ps2.LIGHT_MATERIAL_VERSION)
    raw_name = name.encode("ascii")[:27]
    payload[0x14 : 0x14 + len(raw_name)] = raw_name
    vals = {
        "DiffuseMinScale": 1.0,
        "DiffuseMinR": diff_rgb[0],
        "DiffuseMinG": diff_rgb[1],
        "DiffuseMinB": diff_rgb[2],
        "DiffuseMaxScale": 1.0,
        "DiffuseMaxR": diff_rgb[0],
        "DiffuseMaxG": diff_rgb[1],
        "DiffuseMaxB": diff_rgb[2],
        "DiffuseMinA": 1.0,
        "DiffuseMaxA": 1.0,
        "SpecularPower": 2.0,
        "SpecularMinScale": spec_scale,
        "SpecularMinR": 1.0,
        "SpecularMinG": 1.0,
        "SpecularMinB": 1.0,
        "SpecularMaxScale": spec_scale,
        "SpecularMaxR": 1.0,
        "SpecularMaxG": 1.0,
        "SpecularMaxB": 1.0,
        "EnvmapPower": 1.0,
        "EnvmapMinScale": 1.5,
        "EnvmapMinR": 1.0,
        "EnvmapMinG": 1.0,
        "EnvmapMinB": 1.0,
        "EnvmapMaxScale": 0.5,
        "EnvmapMaxR": 1.0,
        "EnvmapMaxG": 1.0,
        "EnvmapMaxB": 1.0,
        "MetallicScale": 0.0,
        "SpecularHotSpot": 1.0,
    }
    for fname, foff in lighting_ps2.LIGHT_MATERIAL_FLOAT_FIELDS:
        struct.pack_into("<f", payload, foff, vals[fname])
    return _pack_bchunk(lighting_ps2.BCHUNK_LIGHT_MATERIALS, bytes(payload))


def _build_sun_info_record(
    name: str,
    pos: Tuple[float, float, float],
    with_layers: bool = True,
) -> bytes:
    rec = bytearray(lighting_ps2.SUN_INFO_SIZE)
    struct.pack_into("<II", rec, 0, lighting_ps2.SUN_INFO_VERSION, bstringhash(name))
    raw_name = name.encode("ascii")[:23]
    rec[8 : 8 + len(raw_name)] = raw_name
    struct.pack_into("<3f", rec, 0x20, *pos)
    struct.pack_into("<3f", rec, 0x2C, *pos)
    if with_layers:
        layer_specs = [
            (0, 1, 1.0, 60.0, 0.0, 0.0, (255, 200, 100, 128), 0, 0.0),
            (0, 1, 0.5, 320.0, 0.0, 0.0, (130, 110, 25, 128), 0, 0.0),
            (2, 1, 0.7, 800.0, -9.0, 28.0, (159, 122, 75, 128), 0, 0.2),
            (2, 1, 0.32, 800.0, 28.0, 9.0, (166, 119, 72, 128), 16384, 0.2),
        ]
        for li, (tex, alpha, iscale, sz, ox, oy, rgba, ang, sweep) in enumerate(layer_specs):
            lbase = lighting_ps2.SUN_INFO_HEADER_SIZE + li * lighting_ps2.SUN_LAYER_SIZE
            struct.pack_into("<II4f4Bif", rec, lbase, tex, alpha, iscale, sz, ox, oy, *rgba, ang, sweep)
    return bytes(rec)


def _build_synthetic_globalb_and_memfile(pad_sun_to_force_align: bool = False) -> Tuple[bytes, bytes, Dict[str, int]]:
    chunks = bytearray()
    chunks.extend(_build_light_material_chunk("ROAD", diff_rgb=(0.9, 0.57, 0.56), spec_scale=1.0))
    chunks.extend(_build_light_material_chunk("CHROME", diff_rgb=(1.0, 1.0, 1.0), spec_scale=0.0))

    if pad_sun_to_force_align:
        # 176 * 2 = 352 (0x160). Adding an 8-byte payload bChunk (total 16 = 0x10) places SunInfos chunk at
        # 0x170 (payload at 0x178), forcing LoaderSun ((chunk + 0x17) & ~0xf = 0x180) to skip 8 alignment bytes!
        chunks.extend(_pack_bchunk(0x12345678, b"\xAA" * 8))
        sun_payload_prefix = b"\x00" * 8
    else:
        # Insert SunTrack chunk (288 bytes) just like GLOBALB.BUN
        st_payload = bytearray(lighting_ps2.SUN_TRACK_RECORD_SIZE)
        st_title = b"Most Wanted World"
        st_loc = b"Location2\\Track_2000"
        st_tag = b"L2RA"
        st_reg = b"Location2\\RegionA"
        st_payload[0 : len(st_title)] = st_title
        st_payload[32 : 32 + len(st_loc)] = st_loc
        st_payload[64 : 64 + len(st_tag)] = st_tag
        st_payload[72 : 72 + len(st_reg)] = st_reg
        struct.pack_into("<II", st_payload, 0x90, bstringhash("PARKLAND"), 113)
        chunks.extend(_pack_bchunk(lighting_ps2.BCHUNK_SUN_TRACK, bytes(st_payload)))
        sun_payload_prefix = b""

    sun_recs = (
        sun_payload_prefix
        + _build_sun_info_record("PARKLAND", (4500.0, -300.0, 2400.0), with_layers=False)
        + _build_sun_info_record("SUNSET", (4500.0, -300.0, 1700.0), with_layers=True)
        + _build_sun_info_record("MIDDAY", (5300.0, -3550.0, 3000.0), with_layers=True)
    )
    chunks.extend(_pack_bchunk(lighting_ps2.BCHUNK_SUN_INFOS, sun_recs))
    bun = bytes(chunks)
    lzc = lighting_ps2.compress_jdlz_compact(bun)

    # Build EA MemoryFile (magic 0x53219999, 4 entries: dummy0, ATTRIBUTES, GLOBALB.LZC, tail)
    mf = bytearray(16 + 4 * 20)
    blobs = [
        (0x11111111, b"DUMMY_ENTRY_ZERO_PAYLOAD"),
        (bstringhash(r"GLOBAL\ATTRIBUTES.BIN"), b"VPAK" + b"\x01" * 40),
        (lighting_ps2.GLOBALB_LZC_HASH, lzc),
        (0x22222222, b"TAIL_ENTRY_BYTES_123456"),
    ]
    rows = []
    for idx, (h, blob) in enumerate(blobs):
        while len(mf) % 16 != 0:
            mf.append(0)
        off = len(mf)
        mf.extend(blob)
        if idx + 1 < len(blobs):
            while len(mf) % 16 != 0:
                mf.append(0)
        rows.append((h, off, len(blob), len(blob), 0x00A00000 + off))

    struct.pack_into("<4I", mf, 0, 0, 0, lighting_ps2.MEMORY_FILE_MAGIC, len(blobs))
    for i, r in enumerate(rows):
        struct.pack_into("<5I", mf, 16 + i * 20, *r)

    return bun, bytes(mf), {"globalb_offset": rows[2][1], "tail_offset": rows[3][1]}


class TestLightingPs2Offline(unittest.TestCase):
    def test_parse_sun_infos_light_materials_and_sun_track(self):
        bun, _mf, _meta = _build_synthetic_globalb_and_memfile(pad_sun_to_force_align=False)
        db = lighting_ps2.GlobalBDatabase(bun)

        self.assertEqual([s.name for s in db.sun_infos], ["PARKLAND", "SUNSET", "MIDDAY"])
        self.assertEqual([m.name for m in db.light_materials], ["ROAD", "CHROME"])
        self.assertEqual(len(db.sun_track), 1)
        self.assertEqual(db.sun_track[0].tag, "L2RA")
        self.assertEqual(db.sun_track[0].sun_name, "PARKLAND")

        mid = db.find_sun("MIDDAY")
        self.assertEqual(mid.position, [5300.0, -3550.0, 3000.0])
        self.assertEqual(mid.layers[0].colour, [255, 200, 100, 128])
        self.assertEqual(mid.layers[3].angle, 16384)
        self.assertFalse(mid.layers[4].is_active())

        road = db.find_material("ROAD")
        self.assertAlmostEqual(road.fields["DiffuseMinR"], 0.9, places=5)
        self.assertAlmostEqual(road.fields["DiffuseMinG"], 0.57, places=5)
        self.assertAlmostEqual(road.fields["DiffuseMinB"], 0.56, places=5)

    def test_loader_sun_alignment_padding_handled(self):
        bun, _mf, _meta = _build_synthetic_globalb_and_memfile(pad_sun_to_force_align=True)
        profiles = lighting_ps2.parse_sun_infos(bun)
        self.assertEqual([p.name for p in profiles], ["PARKLAND", "SUNSET", "MIDDAY"])
        self.assertEqual(profiles[0].file_offset % 16, 0)
        self.assertEqual(profiles[2].position, [5300.0, -3550.0, 3000.0])

    def test_apply_lighting_recipes_atomic_verification(self):
        bun, _mf, _meta = _build_synthetic_globalb_and_memfile()
        db = lighting_ps2.GlobalBDatabase(bun)
        mid = db.find_sun("MIDDAY")
        road = db.find_material("ROAD")

        recipe = {
            "schema_version": 1,
            "id": "unit_test_recipe",
            "patches": [
                {
                    "section": "SunInfos",
                    "profile": "MIDDAY",
                    "field": "Position",
                    "expected_offset": f"0x{mid.file_offset + 0x20:08x}",
                    "expected_orig": [5300.0, -3550.0, 3000.0],
                    "new_value": [5400.0, -3200.0, 3900.0],
                },
                {
                    "section": "SunInfos",
                    "profile": "MIDDAY",
                    "field": "SunLayers",
                    "index": 1,
                    "member": "Colour",
                    "expected_offset": f"0x{mid.layers[1].file_offset + 0x18:08x}",
                    "expected_orig": [130, 110, 25, 128],
                    "new_value": [180, 220, 255, 128],
                },
                {
                    "section": "LightMaterials",
                    "material": "ROAD",
                    "field": "DiffuseMinRGB",
                    "expected_offset": f"0x{road.field_offset('DiffuseMinRGB'):08x}",
                    "expected_orig": [0.9, 0.57, 0.56],
                    "new_value": [0.96, 0.98, 1.02],
                },
            ],
        }

        patched_bun, audit = lighting_ps2.apply_lighting_recipes(bun, [recipe])
        self.assertEqual(len(patched_bun), len(bun))
        self.assertEqual(len(audit), 3)

        db2 = lighting_ps2.GlobalBDatabase(patched_bun)
        self.assertEqual(db2.find_sun("MIDDAY").position, [5400.0, -3200.0, 3900.0])
        self.assertEqual(db2.find_sun("MIDDAY").layers[1].colour, [180, 220, 255, 128])
        road2 = db2.find_material("ROAD")
        self.assertAlmostEqual(road2.fields["DiffuseMinR"], 0.96, places=5)
        self.assertAlmostEqual(road2.fields["DiffuseMinG"], 0.98, places=5)
        self.assertAlmostEqual(road2.fields["DiffuseMinB"], 1.02, places=5)

        # Negative cases: missing offset, wrong offset, wrong orig, overlap -> must fail cleanly
        bad_missing_off = json.loads(json.dumps(recipe))
        del bad_missing_off["patches"][0]["expected_offset"]
        with self.assertRaises(ValueError):
            lighting_ps2.apply_lighting_recipes(bun, [bad_missing_off])

        bad_wrong_off = json.loads(json.dumps(recipe))
        bad_wrong_off["patches"][0]["expected_offset"] = "0x00000000"
        with self.assertRaises(ValueError):
            lighting_ps2.apply_lighting_recipes(bun, [bad_wrong_off])

        bad_wrong_orig = json.loads(json.dumps(recipe))
        bad_wrong_orig["patches"][1]["expected_orig"] = [255, 255, 255, 128]
        with self.assertRaises(ValueError):
            lighting_ps2.apply_lighting_recipes(bun, [bad_wrong_orig])

        bad_overlap = {
            "id": "overlap",
            "patches": [
                recipe["patches"][2],
                {
                    "section": "LightMaterials",
                    "material": "ROAD",
                    "field": "DiffuseMinG",
                    "expected_offset": f"0x{road.field_offset('DiffuseMinG'):08x}",
                    "expected_orig": 0.57,
                    "new_value": 1.0,
                },
            ],
        }
        with self.assertRaises(ValueError):
            lighting_ps2.apply_lighting_recipes(bun, [bad_overlap])

    def test_compress_jdlz_compact_retail_mips_equivalence(self):
        bun, _mf, _meta = _build_synthetic_globalb_and_memfile()
        # Construct an extended payload exercising RLE near matches, far matches, and flag rollovers
        sample = bun * 4 + bytes(range(256)) + (b"\x5A" * 300) + bun
        comp = lighting_ps2.compress_jdlz_compact(sample, max_chain=128)
        self.assertTrue(is_jdlz(comp))
        self.assertEqual(jdlz_decompress(comp), sample)
        self.assertEqual(_retail_mips_jdlz_sim(comp), sample)
        self.assertLessEqual(len(comp), len(jdlz_compress(sample, max_chain=32)))

    def test_memoryfile_in_place_and_repacked_shifted(self):
        bun, mf, meta = _build_synthetic_globalb_and_memfile()
        info = lighting_ps2.inspect_globalb_in_memfile(mf)
        self.assertEqual(info["entry_index"], 2)
        self.assertEqual(info["storage_format"], "JDLZ")
        self.assertEqual(info["lz_uncompressed_size"], len(bun))

        # 1. Smaller/equal payload -> in-place update
        smaller_lzc = lighting_ps2.compress_jdlz_compact(bun[: len(bun) - 32] + b"\x00" * 32)
        self.assertLessEqual(len(smaller_lzc), info["slot_capacity"])
        mf_inplace, audit_in = lighting_ps2.patch_globalmemoryfile_globalb(mf, smaller_lzc)
        self.assertEqual(audit_in["mode"], "in-place")
        self.assertEqual(len(mf_inplace), len(mf))
        ent2 = lighting_ps2.parse_memory_file(mf_inplace)[2]
        self.assertEqual(mf_inplace[ent2.offset : ent2.offset + ent2.memory_size], smaller_lzc)
        self.assertEqual(
            mf_inplace[meta["tail_offset"] : meta["tail_offset"] + 23],
            b"TAIL_ENTRY_BYTES_123456",
        )

        # 2. Enlarged payload exceeding slot_capacity -> repacked-shifted mode preserving sibling payloads
        enlarged_lzc = lighting_ps2.compress_jdlz_compact(bun + bytes((i * 73) & 0xFF for i in range(600)))
        self.assertGreater(len(enlarged_lzc), info["slot_capacity"])
        mf_shifted, audit_sh = lighting_ps2.patch_globalmemoryfile_globalb(mf, enlarged_lzc)
        self.assertEqual(audit_sh["mode"], "repacked-shifted")
        self.assertGreater(len(mf_shifted), len(mf))

        entries_after = lighting_ps2.parse_memory_file(mf_shifted)
        self.assertEqual(len(entries_after), 4)
        self.assertEqual(entries_after[2].offset, entries_after[2].offset & ~0x0F)
        self.assertEqual(entries_after[3].offset, entries_after[3].offset & ~0x0F)
        self.assertEqual(
            mf_shifted[entries_after[2].offset : entries_after[2].offset + entries_after[2].memory_size],
            enlarged_lzc,
        )
        self.assertEqual(
            mf_shifted[entries_after[3].offset : entries_after[3].offset + entries_after[3].memory_size],
            b"TAIL_ENTRY_BYTES_123456",
        )
        self.assertEqual(
            mf_shifted[entries_after[0].offset : entries_after[0].offset + entries_after[0].memory_size],
            b"DUMMY_ENTRY_ZERO_PAYLOAD",
        )

        # 3. Raw uncompressed BUN must be rejected for GLOBALMEMORYFILE.BIN
        with self.assertRaises(ValueError):
            lighting_ps2.patch_globalmemoryfile_globalb(mf, bun)

    def test_cli_inspect_and_patch_synthetic(self):
        bun, mf, _meta = _build_synthetic_globalb_and_memfile()
        db = lighting_ps2.GlobalBDatabase(bun)
        road = db.find_material("ROAD")

        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            in_gb = td / "GLOBALB.LZC"
            in_mf = td / "GLOBALMEMORYFILE.BIN"
            out_gb = td / "OUT_GLOBALB.LZC"
            out_mf = td / "OUT_GLOBALMEMORYFILE.BIN"
            recipe_p = td / "recipe.json"

            in_gb.write_bytes(lighting_ps2.compress_jdlz_compact(bun))
            in_mf.write_bytes(mf)
            recipe_p.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "cli_test",
                        "patches": [
                            {
                                "section": "LightMaterials",
                                "material": "ROAD",
                                "field": "DiffuseMinRGB",
                                "expected_offset": f"0x{road.field_offset('DiffuseMinRGB'):08x}",
                                "expected_orig": [0.9, 0.57, 0.56],
                                "new_value": [0.99, 0.99, 1.0],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lighting_ps2.main(
                    ["inspect", "--in-globalb", str(in_gb), "--in-memfile", str(in_mf), "--json"]
                )
            self.assertEqual(rc, 0)
            insp = json.loads(buf.getvalue())
            self.assertEqual(insp["sun_infos_count"], 3)
            self.assertEqual(insp["light_materials_total"], 2)
            self.assertEqual(insp["memoryfile_globalb"]["storage_format"], "JDLZ")

            # Dry-run writes nothing
            with redirect_stdout(io.StringIO()):
                rc_dry = lighting_ps2.main(
                    [
                        "patch",
                        "--in-globalb",
                        str(in_gb),
                        "--in-memfile",
                        str(in_mf),
                        "--recipe",
                        str(recipe_p),
                        "--out-globalb",
                        str(out_gb),
                        "--memfile-out",
                        str(out_mf),
                        "--dry-run",
                    ]
                )
            self.assertEqual(rc_dry, 0)
            self.assertFalse(out_gb.exists())
            self.assertFalse(out_mf.exists())

            # Real patch writes JDLZ + synced MemoryFile
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc_real = lighting_ps2.main(
                    [
                        "patch",
                        "--in-globalb",
                        str(in_gb),
                        "--in-memfile",
                        str(in_mf),
                        "--recipe",
                        str(recipe_p),
                        "--out-globalb",
                        str(out_gb),
                        "--memfile-out",
                        str(out_mf),
                        "--json",
                    ]
                )
            self.assertEqual(rc_real, 0)
            rep = json.loads(buf2.getvalue())
            self.assertEqual(rep["patch_count"], 1)
            self.assertTrue(out_gb.is_file())
            self.assertTrue(out_mf.is_file())
            patched_gb = out_gb.read_bytes()
            self.assertEqual(_retail_mips_jdlz_sim(patched_gb), jdlz_decompress(patched_gb))
            out_entries = lighting_ps2.parse_memory_file(out_mf.read_bytes())
            self.assertEqual(
                out_mf.read_bytes()[out_entries[2].offset : out_entries[2].offset + out_entries[2].memory_size],
                patched_gb,
            )

    def test_repo_lighting_recipes_valid_and_clean(self):
        lighting_dir = REPO_ROOT / "patches" / "lighting"
        recipe_files = sorted(lighting_dir.glob("*.json"))
        names = {p.name for p in recipe_files}
        self.assertIn("rockport2026_daylight.json", names)
        self.assertIn("rockport2026_golden_hour.json", names)
        self.assertIn("rockport2026_gt7_daylight.json", names)
        self.assertIn("rockport2026_forza_autumn.json", names)
        self.assertIn("rockport2026_forza_ultra.json", names)

        for rp in recipe_files:
            data = json.loads(rp.read_text(encoding="utf-8"))
            self.assertEqual(data.get("schema_version"), 1, rp.name)
            self.assertEqual(data.get("target", {}).get("game"), "SLUS-21351", rp.name)
            self.assertEqual(data.get("target", {}).get("virtual_path"), r"GLOBAL\GLOBALB.LZC", rp.name)
            patches = data.get("patches", [])
            self.assertGreater(len(patches), 0, rp.name)
            seen_ranges = []
            for p in patches:
                self.assertIn(p.get("section"), ("SunInfos", "LightMaterials"), rp.name)
                self.assertIn("expected_offset", p, rp.name)
                self.assertIn("expected_orig", p, rp.name)
                self.assertIn("new_value", p, rp.name)
                off = int(p["expected_offset"], 16)
                val = p["new_value"]
                sz = 12 if (isinstance(val, list) and len(val) == 3) else 4
                end = off + sz
                for s0, e0 in seen_ranges:
                    self.assertFalse(off < e0 and s0 < end, f"{rp.name}: overlapping offset 0x{off:x}")
                seen_ranges.append((off, end))

        owned_paths = [
            REPO_ROOT / "tools" / "lighting_ps2.py",
            REPO_ROOT / "docs" / "research" / "PS2_LIGHTING_SUNINFOS.md",
            REPO_ROOT / "tests" / "test_lighting_ps2.py",
            *recipe_files,
        ]
        for p in owned_paths:
            self.assertTrue(p.is_file(), f"Missing owned deliverable: {p}")
        issues = audit_repo_clean.audit_files(
            REPO_ROOT, [p.relative_to(REPO_ROOT) for p in owned_paths]
        )
        self.assertEqual(issues, [], f"Repo hygiene issues found: {issues}")


@unittest.skipUnless(_find_local_iso() is not None, "Local SLUS-21351 ISO not available")
class TestLightingPs2LiveISO(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iso_path = _find_local_iso()
        assert cls.iso_path is not None
        cls.arc = PS2NFSMWArchive(cls.iso_path)
        cls.raw_lzc = cls.arc.read_entry(cls.arc.find(r"GLOBAL\GLOBALB.LZC"))
        cls.raw_bun = jdlz_decompress(cls.raw_lzc)
        cls.raw_mf = cls.arc.read_entry(cls.arc.find(r"GLOBAL\GLOBALMEMORYFILE.BIN"))

    def test_live_stock_globalb_and_memfile_parity(self):
        self.assertEqual(len(self.raw_lzc), 518707)
        self.assertEqual(len(self.raw_bun), 871900)
        db = lighting_ps2.GlobalBDatabase(self.raw_bun)
        self.assertEqual(
            [s.name for s in db.sun_infos],
            ["PARKLAND", "SUNSET", "PROTOTYPE", "E3DEMO", "MIDDAY"],
        )
        self.assertEqual(len(db.light_materials), 156)
        self.assertEqual(len(db.sun_track), 18)

        mf_info = lighting_ps2.inspect_globalb_in_memfile(self.raw_mf)
        self.assertEqual(mf_info["entry_index"], 2)
        self.assertEqual(mf_info["offset"], "0x000abec0")
        self.assertEqual(mf_info["slot_capacity"], 518720)
        self.assertEqual(mf_info["storage_format"], "JDLZ")
        entries = lighting_ps2.parse_memory_file(self.raw_mf)
        ent2 = entries[2]
        self.assertEqual(self.raw_mf[ent2.offset : ent2.offset + ent2.memory_size], self.raw_lzc)

    def test_live_recipes_apply_and_pack_in_place(self):
        for fname in (
            "rockport2026_daylight.json",
            "rockport2026_golden_hour.json",
            "rockport2026_gt7_daylight.json",
            "rockport2026_forza_autumn.json",
            "rockport2026_forza_ultra.json",
        ):
            rp = REPO_ROOT / "patches" / "lighting" / fname
            recipe = json.loads(rp.read_text(encoding="utf-8"))
            patched_bun, audit = lighting_ps2.apply_lighting_recipes(self.raw_bun, [recipe])
            self.assertEqual(len(patched_bun), len(self.raw_bun))
            self.assertEqual(len(audit), len(recipe["patches"]))

            comp = lighting_ps2.compress_jdlz_compact(patched_bun)
            self.assertLessEqual(len(comp), 518720, f"{fname} exceeded GLOBALMEMORYFILE.BIN slot!")
            self.assertEqual(jdlz_decompress(comp), patched_bun)

            mf_patched, mf_audit = lighting_ps2.patch_globalmemoryfile_globalb(self.raw_mf, comp)
            self.assertEqual(mf_audit["mode"], "in-place")
            self.assertEqual(len(mf_patched), len(self.raw_mf))

        # CLI end-to-end verification into a temporary directory
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            out_gb = td / "GLOBALB.LZC"
            out_mf = td / "GLOBALMEMORYFILE.BIN"
            with redirect_stdout(io.StringIO()):
                rc = lighting_ps2.main(
                    [
                        "patch",
                        "--iso",
                        str(self.iso_path),
                        "--recipe",
                        str(REPO_ROOT / "patches" / "lighting" / "rockport2026_daylight.json"),
                        "--out-globalb",
                        str(out_gb),
                        "--memfile-out",
                        str(out_mf),
                    ]
                )
            self.assertEqual(rc, 0)
            entries = lighting_ps2.parse_memory_file(out_mf.read_bytes())
            gb_bytes = out_gb.read_bytes()
            self.assertEqual(
                out_mf.read_bytes()[entries[2].offset : entries[2].offset + entries[2].memory_size],
                gb_bytes,
            )


if __name__ == "__main__":
    unittest.main()
