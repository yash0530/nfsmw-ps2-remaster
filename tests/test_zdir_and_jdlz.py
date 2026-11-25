#!/usr/bin/env python3
"""
Unit tests for NFSMW: Rockport 2026 PS2 archive (`ZDIR.BIN`/`ZZDATA`),
EA `bStringHash`, EA `bCalculateCrc32` ZDIR checksum, pure-Python `JDLZ` codec,
Modded ISO Builder v0.2 (multi-duplicate overrides, ZZDATA3 append + ISO9660
directory/PVD patching, build profiles), and `make_release_patch.py`.

All core tests run 100% offline in CI using synthetic ISO9660/ZDIR fixtures.
Optional live read-only tests automatically run when a clean SLUS-21351 ISO
is available via `NFSMW_ISO` or the default local `Documents/PS2Games` path.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from build_modded_iso import (
    apply_elf_patches,
    apply_zz_overrides,
    load_build_profile,
    patch_iso9660_file_size,
    patch_iso9660_volume_space_size,
)
from iso_inspect import SECTOR_SIZE, parse_iso9660
from jdlz import (
    is_jdlz,
    jdlz_compress,
    jdlz_decompress,
    lz_decompress,
    parse_lz_header,
    raww_wrap,
)
from make_release_patch import apply_patch, create_patch
from zdir_tool import (
    PS2NFSMWArchive,
    bcalculate_crc32,
    bstringhash,
    compute_zdir_checksum,
    load_zdir_names,
    normalize_virtual_path,
)


def _discover_live_iso() -> Path:
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso:
        return Path(env_iso).expanduser()
    return Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"


LIVE_ISO = _discover_live_iso()


def _retail_mips_jdlz_sim(src: bytes) -> bytes:
    """Strict 1:1 simulator of SLUS_213.51 `JLZDecompress` (@0x0031C778).

    Verifies that `remaining` (s2) NEVER underflows below 0 when reloading
    control/runtype flags, proving compatibility with retail PS2 hardware/PCSX2.
    """
    assert src[:4] == b"JDLZ" and src[4] == 2
    usize, csize = struct.unpack_from("<II", src, 8)
    s1 = 18
    s2 = csize - 18
    s5 = src[16] | 0x100
    s4 = src[17] | 0x100
    out = bytearray(usize)
    s3 = 0
    while s2 != 0:
        assert s2 > 0, f"Retail JLZDecompress underflow: s2={s2}"
        if s5 & 1:
            b0, b1 = src[s1], src[s1 + 1]
            if s4 & 1:
                run = (((b0 >> 4) << 8) | b1) + 3
                off = (b0 & 0x0F) + 1
            else:
                off = (((b0 >> 5) << 8) | b1) + 17
                run = (b0 & 0x1F) + 3
            for _ in range(run):
                out[s3] = out[s3 - off]
                s3 += 1
            s1 += 2
            s2 -= 2
            s4 >>= 1
        else:
            s2 -= 1
            if s3 < usize:
                out[s3] = src[s1]
                s3 += 1
                s1 += 1
        s5 >>= 1
        if s5 == 1:
            assert s2 > 0, f"Retail JLZDecompress underflow reloading control (s3={s3}/{usize})"
            s5 = src[s1] | 0x100
            s1 += 1
            s2 -= 1
        if s4 == 1:
            assert s2 > 0, f"Retail JLZDecompress underflow reloading runtype (s3={s3}/{usize})"
            s4 = src[s1] | 0x100
            s1 += 1
            s2 -= 1
    assert s2 == 0 and s3 == usize
    return bytes(out)


def _make_iso_dir_record(name_bytes: bytes, lba: int, size: int, is_dir: bool) -> bytes:
    """Construct a valid ISO9660 directory record with both-endian LBA & size."""
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


def build_synthetic_nfsmw_iso(
    out_path: Path,
    free_tail_sectors: int = 4,
    put_trailing_file_after_zz3: bool = False,
) -> Dict[str, object]:
    """Create a compact, valid ISO9660 image with ZDIR.BIN and ZZDATA0..3.BIN for testing.

    Layout:
      Sector 16 : Primary Volume Descriptor (CD001)
      Sector 17 : Volume Descriptor Set Terminator
      Sector 18 : Root directory (`SLUS_213.51`, `NFS`)
      Sector 19 : `NFS` directory (`ZDIR.BIN`, `ZZDATA0..3.BIN`)
      Sector 20 : `SLUS_213.51` (1 sector synthetic ELF)
      Sector 21 : `NFS/ZDIR.BIN` (1 sector, sorted ascending by name_hash)
      Sector 22 : `NFS/ZZDATA0.BIN` (2 sectors)
      Sector 24 : `NFS/ZZDATA1.BIN` (2 sectors)
      Sector 26 : `NFS/ZZDATA2.BIN` (10 sectors — holds single, 3x dup, and 0-byte shared slot)
      Sector 36 : `NFS/ZZDATA3.BIN` (2 sectors — last file on disc unless put_trailing_file_after_zz3)
      Sector 38..38+free_tail_sectors-1 : optional free tail (or trailing blocker file)
    """
    # Payloads inside ZZDATA
    attr_payload = b"VPAK" + b"ATTR_ORIGINAL_DATA" * 100  # 1,804 B (1 sector)
    dyntex_payload = b"\x00\x00\x30\xb3" + b"DYNTEX_ORIG_" * 200  # 2,404 B (2 sectors)
    neighbor_payload = b"ABKC" + b"SOUND_NEIGHBOR" * 50  # 704 B (1 sector)
    zz3_payload = b"MPCh" + b"VIDEO_ZZ3_ORIG" * 80  # 1,124 B (1 sector)

    zz0_sectors = 2
    zz1_sectors = 2
    zz2_sectors = 10
    zz3_sectors = 2

    # Build ZZDATA2 image (10 sectors):
    #   sec 0..0 : GLOBAL\ATTRIBUTES.BIN (1 sector)
    #   sec 1..2 : GLOBAL\DYNTEX.BIN copy #1 (2 sectors)
    #   sec 3..3 : SOUND\NEIGHBOR.ABK (1 sector) + 0-byte shared entry GLOBAL\EMPTY_SHARED.BIN at sec 3!
    #   sec 4..5 : GLOBAL\DYNTEX.BIN copy #2 (2 sectors)
    #   sec 6..7 : GLOBAL\DYNTEX.BIN copy #3 (2 sectors)
    #   sec 8..9 : unused padding
    zz2_buf = bytearray(zz2_sectors * SECTOR_SIZE)
    zz2_buf[0 * SECTOR_SIZE : 0 * SECTOR_SIZE + len(attr_payload)] = attr_payload
    zz2_buf[1 * SECTOR_SIZE : 1 * SECTOR_SIZE + len(dyntex_payload)] = dyntex_payload
    zz2_buf[3 * SECTOR_SIZE : 3 * SECTOR_SIZE + len(neighbor_payload)] = neighbor_payload
    zz2_buf[4 * SECTOR_SIZE : 4 * SECTOR_SIZE + len(dyntex_payload)] = dyntex_payload
    zz2_buf[6 * SECTOR_SIZE : 6 * SECTOR_SIZE + len(dyntex_payload)] = dyntex_payload

    zz0_buf = bytearray(zz0_sectors * SECTOR_SIZE)
    zz1_buf = bytearray(zz1_sectors * SECTOR_SIZE)
    zz3_buf = bytearray(zz3_sectors * SECTOR_SIZE)
    zz3_buf[0 : len(zz3_payload)] = zz3_payload

    cum_bases = [0, zz0_sectors, zz0_sectors + zz1_sectors, zz0_sectors + zz1_sectors + zz2_sectors]

    raw_records = [
        # (name_hash, zz_index, sec_in_zz, sec_cum, size, checksum)
        (bstringhash(r"GLOBAL\ATTRIBUTES.BIN"), 2, 0, cum_bases[2] + 0, len(attr_payload), compute_zdir_checksum(attr_payload)),
        (bstringhash(r"GLOBAL\DYNTEX.BIN"), 2, 1, cum_bases[2] + 1, len(dyntex_payload), compute_zdir_checksum(dyntex_payload)),
        (bstringhash(r"GLOBAL\DYNTEX.BIN"), 2, 4, cum_bases[2] + 4, len(dyntex_payload), compute_zdir_checksum(dyntex_payload)),
        (bstringhash(r"GLOBAL\DYNTEX.BIN"), 2, 6, cum_bases[2] + 6, len(dyntex_payload), compute_zdir_checksum(dyntex_payload)),
        (bstringhash(r"SOUND\NEIGHBOR.ABK"), 2, 3, cum_bases[2] + 3, len(neighbor_payload), compute_zdir_checksum(neighbor_payload)),
        (bstringhash(r"GLOBAL\EMPTY_SHARED.BIN"), 2, 3, cum_bases[2] + 3, 0, compute_zdir_checksum(b"")),
        (bstringhash(r"MOVIES\TEST.MPC"), 3, 0, cum_bases[3] + 0, len(zz3_payload), compute_zdir_checksum(zz3_payload)),
    ]
    raw_records.sort(key=lambda r: r[0])

    zdir_bytes = b"".join(struct.pack("<6I", *r) for r in raw_records)

    # Synthetic minimal ELF (2048 bytes)
    elf_buf = bytearray(SECTOR_SIZE)
    elf_buf[:4] = b"\x7fELF"
    struct.pack_into("<I", elf_buf, 24, 0x00100000)  # e_entry
    struct.pack_into("<I", elf_buf, 28, 52)          # e_phoff
    struct.pack_into("<HH", elf_buf, 42, 32, 1)      # e_phentsize, e_phnum
    struct.pack_into("<8I", elf_buf, 52, 1, 0x100, 0x00100000, 0x00100000, 0x400, 0x400, 5, 0x1000)

    root_lba = 18
    nfs_lba = 19
    elf_lba = 20
    zdir_lba = 21
    zz0_lba = 22
    zz1_lba = zz0_lba + zz0_sectors
    zz2_lba = zz1_lba + zz1_sectors
    zz3_lba = zz2_lba + zz2_sectors
    zz3_end = zz3_lba + zz3_sectors

    blocker_lba = zz3_end
    total_sectors = zz3_end + max(free_tail_sectors, 1 if put_trailing_file_after_zz3 else 0)

    # Build NFS directory sector
    nfs_dir = bytearray()
    nfs_dir += _make_iso_dir_record(b"\x00", nfs_lba, SECTOR_SIZE, True)
    nfs_dir += _make_iso_dir_record(b"\x01", root_lba, SECTOR_SIZE, True)
    nfs_dir += _make_iso_dir_record(b"ZDIR.BIN;1", zdir_lba, len(zdir_bytes), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA0.BIN;1", zz0_lba, len(zz0_buf), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA1.BIN;1", zz1_lba, len(zz1_buf), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA2.BIN;1", zz2_lba, len(zz2_buf), False)
    nfs_dir += _make_iso_dir_record(b"ZZDATA3.BIN;1", zz3_lba, len(zz3_buf), False)

    # Build Root directory sector
    root_dir = bytearray()
    root_dir += _make_iso_dir_record(b"\x00", root_lba, SECTOR_SIZE, True)
    root_dir += _make_iso_dir_record(b"\x01", root_lba, SECTOR_SIZE, True)
    root_dir += _make_iso_dir_record(b"SLUS_213.51;1", elf_lba, len(elf_buf), False)
    root_dir += _make_iso_dir_record(b"NFS", nfs_lba, SECTOR_SIZE, True)
    if put_trailing_file_after_zz3:
        root_dir += _make_iso_dir_record(b"BLOCKER.BIN;1", blocker_lba, 128, False)

    # Build Primary Volume Descriptor (sector 16)
    pvd = bytearray(SECTOR_SIZE)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = b"NFSMW_SYNTHETIC_TEST".ljust(32, b" ")
    struct.pack_into("<I", pvd, 80, total_sectors)
    struct.pack_into(">I", pvd, 84, total_sectors)
    struct.pack_into("<H", pvd, 128, SECTOR_SIZE)
    struct.pack_into(">H", pvd, 130, SECTOR_SIZE)
    root_rec = _make_iso_dir_record(b"\x00", root_lba, SECTOR_SIZE, True)
    pvd[156 : 156 + len(root_rec)] = root_rec

    # Volume Descriptor Set Terminator (sector 17)
    vdst = bytearray(SECTOR_SIZE)
    vdst[0] = 255
    vdst[1:6] = b"CD001"
    vdst[6] = 1

    iso = bytearray(total_sectors * SECTOR_SIZE)
    iso[16 * SECTOR_SIZE : 17 * SECTOR_SIZE] = pvd
    iso[17 * SECTOR_SIZE : 18 * SECTOR_SIZE] = vdst
    iso[root_lba * SECTOR_SIZE : root_lba * SECTOR_SIZE + len(root_dir)] = root_dir
    iso[nfs_lba * SECTOR_SIZE : nfs_lba * SECTOR_SIZE + len(nfs_dir)] = nfs_dir
    iso[elf_lba * SECTOR_SIZE : elf_lba * SECTOR_SIZE + len(elf_buf)] = elf_buf
    iso[zdir_lba * SECTOR_SIZE : zdir_lba * SECTOR_SIZE + len(zdir_bytes)] = zdir_bytes
    iso[zz0_lba * SECTOR_SIZE : zz0_lba * SECTOR_SIZE + len(zz0_buf)] = zz0_buf
    iso[zz1_lba * SECTOR_SIZE : zz1_lba * SECTOR_SIZE + len(zz1_buf)] = zz1_buf
    iso[zz2_lba * SECTOR_SIZE : zz2_lba * SECTOR_SIZE + len(zz2_buf)] = zz2_buf
    iso[zz3_lba * SECTOR_SIZE : zz3_lba * SECTOR_SIZE + len(zz3_buf)] = zz3_buf
    if put_trailing_file_after_zz3:
        iso[blocker_lba * SECTOR_SIZE : blocker_lba * SECTOR_SIZE + 128] = b"B" * 128

    out_path.write_bytes(iso)
    return {
        "vol_sectors": total_sectors,
        "zz3_lba": zz3_lba,
        "zz3_orig_size": len(zz3_buf),
        "cum_bases": cum_bases,
        "neighbor_payload": neighbor_payload,
    }


class TestBStringHashAndDictionary(unittest.TestCase):
    def test_known_retail_hashes(self):
        self.assertEqual(bstringhash(r"MOVIES\BLACKLIST_12_ENGLISH_NTSC.MPC"), 0x0035FC77)
        self.assertEqual(bstringhash(r"SOUND\ENGINE\SWTN_CAR_16_MB.ABK"), 0x00621487)
        self.assertEqual(bstringhash(r"CARS\GALLARDO\VINYLS.BIN"), 0x00766252)
        self.assertEqual(bstringhash(r"GLOBAL\DYNTEX.BIN"), 0x29EE288F)
        self.assertEqual(bstringhash(r"GLOBAL\GAMEPLAY.BIN"), 0x51EAFEC3)
        self.assertEqual(bstringhash(r"GLOBAL\GAMEPLAY.LZC"), 0x51EB2B73)
        self.assertEqual(bstringhash(r"GLOBAL\GLOBALB.LZC"), 0x4C939B36)
        self.assertEqual(bstringhash(r"FRONTEND\FRONTB.LZC"), 0x14C5A53D)
        self.assertEqual(bstringhash(r"LANGUAGES\LANGUAGETEXTURES.BIN"), 0xDA18A301)

    def test_normalize_virtual_path(self):
        self.assertEqual(normalize_virtual_path("global/attributes.bin"), r"GLOBAL\ATTRIBUTES.BIN")
        self.assertEqual(normalize_virtual_path(r"discu:\Global/DyNTex.bin"), r"GLOBAL\DYNTEX.BIN")

    def test_zdir_names_dictionary_integrity(self):
        names_file = TOOLS_DIR / "zdir_names.txt"
        self.assertTrue(names_file.is_file(), "tools/zdir_names.txt must exist")
        names = load_zdir_names(names_file)
        self.assertGreaterEqual(len(names), 1300)
        self.assertIn(r"GLOBAL\ATTRIBUTES.BIN", names)
        self.assertIn(r"GLOBAL\DYNTEX.BIN", names)
        self.assertIn(r"FRONTEND\FRONTB.LZC", names)
        self.assertIn(r"CARS\GALLARDO\VINYLS.BIN", names)
        for p in names:
            self.assertEqual(p, p.upper())
            self.assertNotIn("/", p)


class TestZDirChecksum(unittest.TestCase):
    @staticmethod
    def _reference_crc32_bitwise(data: bytes) -> int:
        """Slow bit-by-bit reference implementation of EA bCalculateCrc32."""
        crc = 0xFFFFFFFF
        length = len(data)
        seq = bytearray(data)
        n = length
        while n > 0:
            seq.append(n & 0xFF)
            n >>= 8
        for byte in seq:
            crc ^= byte << 24
            for _ in range(8):
                if crc & 0x80000000:
                    crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                else:
                    crc = (crc << 1) & 0xFFFFFFFF
        return ~crc & 0xFFFFFFFF

    def test_empty_returns_sentinel_ffffffff(self):
        self.assertEqual(compute_zdir_checksum(b""), 0xFFFFFFFF)

    def test_matches_bitwise_reference(self):
        for payload in (
            b"A",
            b"123456789",
            b"\x00" * 16,
            b"VPAK" + bytes(range(256)),
            b"Rockport 2026" * 73,
        ):
            self.assertEqual(compute_zdir_checksum(payload), self._reference_crc32_bitwise(payload))
            self.assertEqual(bcalculate_crc32(payload), self._reference_crc32_bitwise(payload))

    def test_sensitive_to_data_and_length(self):
        a = compute_zdir_checksum(b"hello")
        b = compute_zdir_checksum(b"hellp")
        c = compute_zdir_checksum(b"hello\x00")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)


class TestJDLZCodec(unittest.TestCase):
    def test_roundtrip_and_retail_mips_sim_edge_cases(self):
        rng = random.Random(20260401)
        cases = [
            b"",
            b"X",
            b"XY",
            b"XYZ",
            b"AAAAAAAA",
            b"A" * 4098,
            b"A" * 5000,
            b"\x00" * 9000 + b"\xff" + b"\x00" * 4105,
            b"ABCD" * 500,
            b"0123456789ABCDEF" * 300,
            bytes(range(256)) * 16,
            bytes(rng.getrandbits(8) for _ in range(7)),
            bytes(rng.getrandbits(8) for _ in range(8)),
            bytes(rng.getrandbits(8) for _ in range(17)),
            bytes(rng.getrandbits(8) for _ in range(4096)),
            # Force simultaneous bits1==8 & bits2==8 at stream end
            b"ABCD" + (b"ABCD" * 8),
            b"XYZ" + (b"XYZ" * 16),
            # Exercise far window distances up to 2064
            (b"HEADER_BLOCK_1234567890" + bytes(rng.getrandbits(8) for _ in range(1900)) + b"HEADER_BLOCK_1234567890") * 3,
        ]
        for idx, blob in enumerate(cases):
            compressed = jdlz_compress(blob)
            self.assertTrue(is_jdlz(compressed), f"Case {idx} missing valid JDLZ header")
            hdr = parse_lz_header(compressed)
            self.assertEqual(hdr.uncompressed_size, len(blob))
            self.assertEqual(hdr.compressed_size, len(compressed))
            self.assertEqual(jdlz_decompress(compressed), blob, f"Case {idx} Python decode failed")
            self.assertEqual(_retail_mips_jdlz_sim(compressed), blob, f"Case {idx} retail MIPS sim failed")

    def test_trailing_ea_pad_byte_compatibility(self):
        blob = b"Need for Speed Most Wanted Rockport 2026! " * 40
        comp = bytearray(jdlz_compress(blob))
        # Append 1 trailing 0x00 byte and increment header compressed_size just like EA's tool
        comp.append(0x00)
        struct.pack_into("<I", comp, 12, len(comp))
        self.assertEqual(jdlz_decompress(bytes(comp)), blob)

    def test_compression_reduces_repetitive_payload(self):
        blob = (b"VPAK\x00\x00\x00\x00" + b"\x11" * 64 + b"ROCKPORT_2026_TEXTURE_CHUNK_") * 200
        comp = jdlz_compress(blob)
        self.assertLess(len(comp), len(blob) // 5)
        self.assertEqual(jdlz_decompress(comp), blob)

    def test_raww_wrap_and_lz_decompress(self):
        blob = b"raw-payload-12345"
        wrapped = raww_wrap(blob)
        self.assertEqual(wrapped[:4], b"RAWW")
        self.assertEqual(lz_decompress(wrapped), blob)

    def test_invalid_jdlz_raises(self):
        with self.assertRaises(ValueError):
            jdlz_decompress(b"NOPE" + b"\x00" * 20)
        with self.assertRaises(ValueError):
            jdlz_decompress(struct.pack("<4sBBHII", b"JDLZ", 99, 16, 0, 10, 20) + b"\x00\x00\x00\x00")
        with self.assertRaises(ValueError):
            jdlz_decompress(struct.pack("<4sBBHII", b"JDLZ", 2, 16, 0, 100, 999) + b"\x00\x00")


class TestSyntheticISOBuilderV02(unittest.TestCase):
    def test_multi_duplicate_in_place_updates_all_rows_and_checksums(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            iso_path = td_path / "synth.iso"
            build_synthetic_nfsmw_iso(iso_path, free_tail_sectors=4)

            # New DYNTEX payload fits in its 2-sector original slot (<= 4096 B)
            new_dyntex = b"\x00\x00\x30\xb3" + b"MODDED_DYNTEX_V02!" * 150
            override_file = td_path / "dyntex_mod.bin"
            override_file.write_bytes(new_dyntex)

            with iso_path.open("r+b") as f:
                _, vol_sectors, entries = parse_iso9660(f)
                by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
                applied, layout = apply_zz_overrides(
                    f, by_path, {r"GLOBAL\DYNTEX.BIN": override_file}, vol_sectors=vol_sectors
                )

            self.assertEqual(len(applied), 1)
            self.assertEqual(applied[0]["duplicate_rows_updated"], 3)
            self.assertEqual(applied[0]["mode"], "in-place")
            self.assertEqual(layout["zzdata3_appended_sectors"], 0)
            self.assertFalse(layout["pvd_updated"])

            # Verify via PS2NFSMWArchive that ALL 3 duplicate rows match new data + checksum!
            arc = PS2NFSMWArchive(iso_path)
            dup_entries = arc.find_all(r"GLOBAL\DYNTEX.BIN")
            self.assertEqual(len(dup_entries), 3)
            expected_chk = compute_zdir_checksum(new_dyntex)
            for e in dup_entries:
                self.assertEqual(e.zz_index, 2)
                self.assertEqual(e.byte_size, len(new_dyntex))
                self.assertEqual(e.checksum, expected_chk)
                self.assertEqual(arc.read_entry(e), new_dyntex)
                ok, stored, calc = arc.verify_entry_checksum(e)
                self.assertTrue(ok)
                self.assertEqual(stored, calc)

    def test_append_zzdata3_within_free_tail_updates_dir_record_not_pvd(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            iso_path = td_path / "synth_tail.iso"
            meta = build_synthetic_nfsmw_iso(iso_path, free_tail_sectors=6)

            # ATTRIBUTES.BIN original slot is 1 sector (2048 B); grow to 3 sectors (5000 B)
            # Fits inside the 6 free tail sectors without growing PVD!
            big_attr = b"VPAK" + b"EXPANDED_ATTRIBUTES_PAYLOAD" * 200  # 5,404 B -> 3 sectors
            ov_file = td_path / "attr_big.bin"
            ov_file.write_bytes(big_attr)

            with iso_path.open("r+b") as f:
                _, vol_sectors, entries = parse_iso9660(f)
                by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
                applied, layout = apply_zz_overrides(
                    f, by_path, {r"GLOBAL\ATTRIBUTES.BIN": ov_file}, vol_sectors=vol_sectors
                )

            self.assertEqual(applied[0]["mode"], "append-zzdata3")
            self.assertEqual(layout["zzdata3_appended_sectors"], 3)
            self.assertEqual(layout["zzdata3_new_size"], meta["zz3_orig_size"] + 3 * SECTOR_SIZE)
            self.assertFalse(layout["pvd_updated"])
            self.assertEqual(layout["vol_sectors_new"], meta["vol_sectors"])

            # Verify ISO9660 directory record has BOTH LE and BE lengths updated
            with iso_path.open("rb") as f:
                _, new_vol, new_entries = parse_iso9660(f)
                self.assertEqual(new_vol, meta["vol_sectors"])
                zz3 = next(e for e in new_entries if e["path"].upper() == "NFS/ZZDATA3.BIN")
                self.assertEqual(zz3["size"], meta["zz3_orig_size"] + 3 * SECTOR_SIZE)
                f.seek(zz3["dir_record_sector"] * SECTOR_SIZE + zz3["dir_record_offset"])
                rec = f.read(20)
                self.assertEqual(struct.unpack_from("<I", rec, 10)[0], zz3["size"])
                self.assertEqual(struct.unpack_from(">I", rec, 14)[0], zz3["size"])

            # Verify ZDIR row fields and full readback
            arc = PS2NFSMWArchive(iso_path)
            entry = arc.find(r"GLOBAL\ATTRIBUTES.BIN")
            self.assertEqual(entry.zz_index, 3)
            self.assertEqual(entry.sector_in_zz, 2)
            self.assertEqual(entry.sector_cumulative, meta["cum_bases"][3] + 2)
            self.assertEqual(entry.byte_size, len(big_attr))
            self.assertEqual(arc.read_entry(entry), big_attr)
            ok, _, _ = arc.verify_entry_checksum(entry)
            self.assertTrue(ok)

    def test_append_zzdata3_past_volume_end_extends_pvd_and_repoints_all_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            iso_path = td_path / "synth_grow.iso"
            meta = build_synthetic_nfsmw_iso(iso_path, free_tail_sectors=1)

            # Grow 3x duplicated GLOBAL\DYNTEX.BIN to 5 sectors (9,500 B), extending past vol_sectors!
            huge_dyntex = b"\x00\x00\x30\xb3" + b"HUGE_4K_DYNTEX_" * 630  # 9,454 B -> 5 sectors
            ov_file = td_path / "dyntex_huge.bin"
            ov_file.write_bytes(huge_dyntex)

            with iso_path.open("r+b") as f:
                _, vol_sectors, entries = parse_iso9660(f)
                by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
                applied, layout = apply_zz_overrides(
                    f, by_path, {r"GLOBAL\DYNTEX.BIN": ov_file}, vol_sectors=vol_sectors
                )

            self.assertEqual(applied[0]["duplicate_rows_updated"], 3)
            self.assertEqual(applied[0]["mode"], "append-zzdata3")
            self.assertEqual(layout["zzdata3_appended_sectors"], 5)
            self.assertTrue(layout["pvd_updated"])
            expected_vol = meta["zz3_lba"] + (meta["zz3_orig_size"] // SECTOR_SIZE) + 5
            self.assertEqual(layout["vol_sectors_new"], expected_vol)
            self.assertEqual(iso_path.stat().st_size, expected_vol * SECTOR_SIZE)

            # Verify PVD LE & BE Volume Space Size match!
            with iso_path.open("rb") as f:
                f.seek(16 * SECTOR_SIZE)
                pvd = f.read(SECTOR_SIZE)
                self.assertEqual(struct.unpack_from("<I", pvd, 80)[0], expected_vol)
                self.assertEqual(struct.unpack_from(">I", pvd, 84)[0], expected_vol)

            # Verify ALL 3 ZDIR duplicate rows repointed cleanly and pass checksum verification!
            arc = PS2NFSMWArchive(iso_path)
            dups = arc.find_all(r"GLOBAL\DYNTEX.BIN")
            self.assertEqual(len(dups), 3)
            for e in dups:
                self.assertEqual(e.zz_index, 3)
                self.assertEqual(e.sector_in_zz, 2)
                self.assertEqual(e.sector_cumulative, meta["cum_bases"][3] + 2)
                self.assertEqual(e.byte_size, len(huge_dyntex))
                self.assertEqual(arc.read_entry(e), huge_dyntex)
                ok, _, _ = arc.verify_entry_checksum(e)
                self.assertTrue(ok)

            # Verify every single entry in ZDIR.BIN still passes checksum verification!
            for e in arc.entries:
                ok, _, _ = arc.verify_entry_checksum(e)
                self.assertTrue(ok, f"Entry 0x{e.name_hash:08X} checksum failed after ZZDATA3 append")

    def test_zero_byte_shared_slot_appends_without_clobbering_neighbor(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            iso_path = td_path / "synth_shared.iso"
            meta = build_synthetic_nfsmw_iso(iso_path, free_tail_sectors=4)

            new_data = b"NEW_DATA_FOR_PREVIOUSLY_EMPTY_FILE"
            ov_file = td_path / "empty_now_filled.bin"
            ov_file.write_bytes(new_data)

            with iso_path.open("r+b") as f:
                _, vol_sectors, entries = parse_iso9660(f)
                by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
                applied, _ = apply_zz_overrides(
                    f, by_path, {r"GLOBAL\EMPTY_SHARED.BIN": ov_file}, vol_sectors=vol_sectors
                )

            self.assertEqual(applied[0]["mode"], "append-zzdata3")
            arc = PS2NFSMWArchive(iso_path)
            # Neighbor file that shared sector 3 in ZZDATA2 MUST be 100% intact!
            neighbor = arc.find(r"SOUND\NEIGHBOR.ABK")
            self.assertEqual(arc.read_entry(neighbor), meta["neighbor_payload"])
            filled = arc.find(r"GLOBAL\EMPTY_SHARED.BIN")
            self.assertEqual(filled.zz_index, 3)
            self.assertEqual(arc.read_entry(filled), new_data)

    def test_refuses_append_when_disabled_or_file_follows_zzdata3(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            iso_path = td_path / "synth_no_append.iso"
            build_synthetic_nfsmw_iso(iso_path, free_tail_sectors=4)

            big_file = td_path / "big.bin"
            big_file.write_bytes(b"X" * 5000)

            with iso_path.open("r+b") as f:
                _, vol_sectors, entries = parse_iso9660(f)
                by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
                with self.assertRaises(ValueError):
                    apply_zz_overrides(
                        f, by_path, {r"GLOBAL\ATTRIBUTES.BIN": big_file},
                        vol_sectors=vol_sectors, allow_append=False,
                    )

            # Now test ISO with a blocker file positioned after ZZDATA3.BIN
            iso_blocked = td_path / "synth_blocked.iso"
            build_synthetic_nfsmw_iso(iso_blocked, put_trailing_file_after_zz3=True)
            with iso_blocked.open("r+b") as f:
                _, vol_sectors, entries = parse_iso9660(f)
                by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
                with self.assertRaises(ValueError):
                    apply_zz_overrides(
                        f, by_path, {r"GLOBAL\ATTRIBUTES.BIN": big_file},
                        vol_sectors=vol_sectors, allow_append=True,
                    )

    def test_rockport2026_v02_profile_schema_and_elf_ids(self):
        profile_path = REPO_ROOT / "patches" / "profiles" / "rockport2026_v0.2.json"
        self.assertTrue(profile_path.is_file())
        profile = load_build_profile(profile_path)
        self.assertEqual(profile["profile_id"], "rockport2026_v0.2")
        self.assertEqual(profile["target"]["serial"], "SLUS-21351")
        self.assertTrue(profile["allow_zzdata3_append"])

        spec_path = REPO_ROOT / profile["elf_patches"]["spec"]
        self.assertTrue(spec_path.is_file())
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec_ids = {p["id"] for p in spec["patches"]}
        for pid in profile["elf_patches"]["include_ids"]:
            self.assertIn(pid, spec_ids)


class TestMakeReleasePatch(unittest.TestCase):
    def _create_mock_xdelta3(self, directory: Path) -> Path:
        """Create a tiny Python mock xdelta3 script that encodes/decodes XOR-based test patches."""
        script = directory / "mock_xdelta3.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, zlib, json, base64\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "mode = 'e' if '-e' in args else 'd'\n"
            "s_idx = args.index('-s')\n"
            "src_p = Path(args[s_idx + 1])\n"
            "in_p = Path(args[s_idx + 2])\n"
            "out_p = Path(args[s_idx + 3])\n"
            "if mode == 'e':\n"
            "    payload = zlib.compress(in_p.read_bytes())\n"
            "    out_p.write_bytes(b'MOCKXD3!' + payload)\n"
            "else:\n"
            "    raw = in_p.read_bytes()\n"
            "    assert raw.startswith(b'MOCKXD3!')\n"
            "    out_p.write_bytes(zlib.decompress(raw[8:]))\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def test_create_apply_and_sha256_verification(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            mock_xd = self._create_mock_xdelta3(td_path)
            src = td_path / "orig.iso"
            mod = td_path / "modded.iso"
            pat = td_path / "release.xdelta"
            rebuilt = td_path / "rebuilt.iso"

            src.write_bytes(b"ORIGINAL_ISO_BYTES_" * 200)
            mod.write_bytes(b"MODIFIED_ISO_BYTES_" * 200)

            manifest = create_patch(
                source=src,
                modified=mod,
                output=pat,
                force=True,
                verify=True,
                xdelta3_bin=str(mock_xd),
            )
            self.assertTrue(pat.is_file())
            manifest_file = pat.with_suffix(".xdelta.json")
            self.assertTrue(manifest_file.is_file())
            # Ensure manifest stores only basenames (no personal paths)
            self.assertEqual(manifest["source"]["filename"], "orig.iso")
            self.assertEqual(manifest["modified"]["filename"], "modded.iso")
            self.assertEqual(manifest["patch"]["filename"], "release.xdelta")

            # Apply cleanly
            report = apply_patch(
                source=src, patch=pat, output=rebuilt, xdelta3_bin=str(mock_xd)
            )
            self.assertTrue(report["output_sha256_verified"])
            self.assertEqual(rebuilt.read_bytes(), mod.read_bytes())

            # Wrong source file MUST fail SHA-256 verification
            wrong_src = td_path / "wrong.iso"
            wrong_src.write_bytes(b"DIFFERENT_DISC_REVISION" * 100)
            with self.assertRaises(ValueError):
                apply_patch(
                    source=wrong_src, patch=pat, output=td_path / "fail1.iso", xdelta3_bin=str(mock_xd)
                )

            # Corrupt patch file MUST fail SHA-256 verification
            corrupt_pat = td_path / "corrupt.xdelta"
            corrupt_pat.write_bytes(pat.read_bytes() + b"\xff")
            with self.assertRaises(ValueError):
                apply_patch(
                    source=src, patch=corrupt_pat, output=td_path / "fail2.iso",
                    manifest_path=manifest_file, xdelta3_bin=str(mock_xd),
                )


@unittest.skipUnless(LIVE_ISO.is_file(), "Clean SLUS-21351 ISO not present locally; skipping live read-only tests")
class TestLiveReadOnlySLUS21351(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arc = PS2NFSMWArchive(LIVE_ISO)

    def test_zdir_dictionary_resolution_rate(self):
        self.assertEqual(len(self.arc.entries), 1447)
        resolved_rows = sum(1 for e in self.arc.entries if e.resolved_path)
        self.assertGreaterEqual(resolved_rows, 1350)

    def test_real_zdir_checksums_including_duplicates_and_empty(self):
        for vpath in (
            r"GLOBAL\ATTRIBUTES.BIN",
            r"GLOBAL\GAMEPLAY.BIN",
            r"GLOBAL\DYNTEX.BIN",
            r"GLOBAL\FRONTENDMEMORYFILE.BIN",
            r"LANGUAGES\LANGUAGETEXTURES.BIN",
        ):
            rows = self.arc.find_all(vpath)
            self.assertGreaterEqual(len(rows), 1)
            for row in rows:
                ok, stored, calc = self.arc.verify_entry_checksum(row)
                self.assertTrue(ok, f"Checksum mismatch for {vpath} row {row.index}: 0x{stored:08X} != 0x{calc:08X}")

        # Verify a real 0-byte retail entry has 0xFFFFFFFF sentinel
        empty_entries = [e for e in self.arc.entries if e.byte_size == 0]
        self.assertEqual(len(empty_entries), 90)
        for e in empty_entries[:10]:
            ok, stored, calc = self.arc.verify_entry_checksum(e)
            self.assertTrue(ok)
            self.assertEqual(stored, 0xFFFFFFFF)

    def test_real_jdlz_decompression_and_roundtrip(self):
        # FRONTEND\FRONTB.LZC decompresses cleanly to a GeometryPack (0x80134000)
        frontb_raw = self.arc.read_entry(self.arc.find(r"FRONTEND\FRONTB.LZC"))
        self.assertTrue(is_jdlz(frontb_raw))
        frontb_dec = jdlz_decompress(frontb_raw)
        self.assertEqual(len(frontb_dec), 5_081_216)
        self.assertEqual(frontb_dec[:4], b"\x00\x40\x13\x80")

        # GLOBAL\GAMEPLAY.LZC decompresses BYTE-FOR-BYTE identical to GLOBAL\GAMEPLAY.BIN!
        gp_lzc = self.arc.read_entry(self.arc.find(r"GLOBAL\GAMEPLAY.LZC"))
        gp_bin = self.arc.read_entry(self.arc.find(r"GLOBAL\GAMEPLAY.BIN"))
        self.assertEqual(jdlz_decompress(gp_lzc), gp_bin)

        # Round-trip re-compression of real PS2 data passes both Python decoder and retail MIPS simulator
        sample = gp_bin[:65536]
        recomp = jdlz_compress(sample)
        self.assertLess(len(recomp), len(sample))
        self.assertEqual(jdlz_decompress(recomp), sample)
        self.assertEqual(_retail_mips_jdlz_sim(recomp), sample)


if __name__ == "__main__":
    unittest.main()
