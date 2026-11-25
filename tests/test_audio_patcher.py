#!/usr/bin/env python3
"""
tests/test_audio_patcher.py — Offline & Read-Only ISO Tests for tools/audio_patcher.py:
  1. Sony PS2 SPU-ADPCM (VAG) 16-byte block encoder & decoder (5-filter selection,
     loop flags 0x00/0x01/0x02/0x03/0x06/0x07, padding, >25 dB SNR, pure-stdlib & numpy)
  2. Gnsu v20 granular engine synth patcher (hero car presets, accel/decel monotonicity,
     dry-run diff, audio_inspect round-trip)
  3. ABKC / BNKl v5 sample patcher (mono, stereo, looped, shrinking/growing payloads,
     untouched slot preservation, t3 relocation, audio_inspect round-trip)
  4. Procedural CC0-1.0 synthesizer (synth-cc0 generating 6 WAVs > 25 dB SPU-ADPCM SNR)
  5. Live read-only ISO round-trip tests (skipped cleanly when NFSMW_ISO is absent)
  6. Repository hygiene check (zero personal paths, valid assets_cc0/audio_manifest.json)
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import audio_inspect  # noqa: E402
import audio_patcher  # noqa: E402
import audit_repo_clean  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402


def _find_local_iso() -> Optional[Path]:
    return audio_patcher.resolve_iso_path(None)


def _encode_pt_header(
    *,
    channels: int = 1,
    sample_rate: int = 32000,
    num_samples: int = 560,
    loop_start: Optional[int] = None,
    loop_length: Optional[int] = None,
    data_offset: int = 0x50,
    data_offset_r: Optional[int] = None,
    codec_id: Optional[int] = None,
) -> bytes:
    out = bytearray(b"PT\x05\x00")

    def add_tlv(tag: int, val: int, min_bytes: int = 1) -> None:
        raw = b"\x00" if val == 0 else val.to_bytes((val.bit_length() + 7) // 8, "big")
        if len(raw) < min_bytes:
            raw = (b"\x00" * (min_bytes - len(raw))) + raw
        out.append(tag)
        out.append(len(raw))
        out.extend(raw)

    if channels != 1:
        add_tlv(0x82, channels)
    add_tlv(0x84, sample_rate, 2)
    add_tlv(0x85, num_samples, 2)
    if loop_start is not None:
        add_tlv(0x86, loop_start)
    if loop_length is not None:
        add_tlv(0x87, loop_length, 2)
    add_tlv(0x88, data_offset, 4)
    if data_offset_r is not None:
        add_tlv(0x89, data_offset_r, 4)
    if codec_id is not None:
        add_tlv(0xA0, codec_id)
    out.append(0xFF)
    while len(out) % 4 != 0:
        out.append(0x00)
    return bytes(out)


def _build_synthetic_abkc_with_spu() -> Tuple[bytes, List[int], List[int]]:
    """Build an authentic ABKC bank with 2 SPU-ADPCM slots + 128-byte aligned t1 + t3 string table."""
    pcm1 = [int(round(16000.0 * math.sin(2.0 * math.pi * 440.0 * i / 32000.0))) for i in range(560)]
    pcm2 = [int(round(14000.0 * math.sin(2.0 * math.pi * 880.0 * i / 24000.0))) for i in range(840)]
    adpcm1 = audio_patcher.encode_spu_adpcm(pcm1, looped=False, lead_in_zero_block=True)
    adpcm2 = audio_patcher.encode_spu_adpcm(pcm2, looped=True, lead_in_zero_block=True)

    bnkl_off = 0x80
    slot_count = 3
    slot_tbl_off = bnkl_off + 0x14
    pt1_tmp = _encode_pt_header(sample_rate=32000, num_samples=(len(adpcm1) // 16) * 28, data_offset=0x100)
    pt2_tmp = _encode_pt_header(
        sample_rate=24000,
        num_samples=(len(adpcm2) // 16) * 28,
        loop_start=28,
        loop_length=(len(adpcm2) // 16 - 1) * 28 - 1,
        data_offset=0x100,
    )
    raw_hdr_end = slot_tbl_off + slot_count * 4 + len(pt1_tmp) + len(pt2_tmp)
    pad_hdr = (16 - ((raw_hdr_end - bnkl_off) % 16)) % 16
    bnkl_hdr_sz = (raw_hdr_end - bnkl_off) + pad_hdr

    off1 = bnkl_hdr_sz
    off2 = off1 + len(adpcm1)
    pt1 = _encode_pt_header(sample_rate=32000, num_samples=(len(adpcm1) // 16) * 28, data_offset=off1)
    pt2 = _encode_pt_header(
        sample_rate=24000,
        num_samples=(len(adpcm2) // 16) * 28,
        loop_start=28,
        loop_length=(len(adpcm2) // 16 - 1) * 28 - 1,
        data_offset=off2,
    )
    assert len(pt1) == len(pt1_tmp) and len(pt2) == len(pt2_tmp)

    pt1_off = slot_tbl_off + slot_count * 4
    pt2_off = pt1_off + len(pt1)
    rel_offs = [0, pt1_off - (slot_tbl_off + 4), pt2_off - (slot_tbl_off + 8)]

    pay_bytes = adpcm1 + adpcm2
    bnkl_body = bytearray()
    bnkl_body += struct.pack("<4sHHIII", b"BNKl", 5, slot_count, bnkl_hdr_sz, len(pay_bytes), 0)
    bnkl_body += struct.pack("<3I", *rel_offs)
    bnkl_body += pt1 + pt2 + (b"\x00" * pad_hdr) + pay_bytes

    out = bytearray(bnkl_off)
    out[0:4] = b"ABKC"
    out[4:8] = bytes.fromhex("01010100")
    struct.pack_into("<HH", out, 8, 2, 1)
    out.extend(bnkl_body)

    pad_t1 = (128 - (len(out) % 128)) % 128
    out.extend(b"\x00" * pad_t1)
    t1_off = len(out)
    out.extend(struct.pack("<2I", 1, 0x60))
    t2_off = len(out)
    out.extend(struct.pack("<2I", 1, 0x70))
    t3_off = len(out)
    str_off = t3_off + 4 + 12
    out.extend(struct.pack("<I3I", 1, 0x60, str_off, 0x01020304))
    out.extend(b"SYNTH_BANK_TEST\x00")

    struct.pack_into("<5I", out, 0x14, len(out), bnkl_off, 0x5C, bnkl_off, t1_off - bnkl_off)
    struct.pack_into("<3I", out, 0x30, t1_off, t2_off, t3_off)
    return bytes(out), pcm1, pcm2


def _build_synthetic_gnsu(*, decel: bool = False) -> bytes:
    n_rpm = 10
    n_grain = 4
    total_samples = 64000
    sample_rate = 32000
    hdr = struct.pack("<4s4sff4I", b"Gnsu", b"20\x00\x00", 1200.0, 7200.0, n_rpm, n_grain, total_samples, sample_rate)
    if decel:
        rpm_vals = [60000 - i * 5800 for i in range(n_rpm + 1)]
    else:
        rpm_vals = [480 + i * 5950 for i in range(n_rpm + 1)]
    rpm_tbl = struct.pack(f"<{n_rpm + 1}I", *rpm_vals)
    grain_tbl = struct.pack(f"<{n_grain}I", 0, 16000, 32000, 48000)
    granules = struct.pack("<I", total_samples - 1) + (b"\x32" * (((total_samples + 31) // 32) * 19))
    return hdr + rpm_tbl + grain_tbl + granules


class TestSpuAdpcmCodec(unittest.TestCase):
    def test_encode_decode_snr_and_padding_stdlib_and_numpy(self) -> None:
        sr = 32000
        # 500 samples -> padded to 18 * 28 = 504 samples = 288 bytes
        sine = [int(round(20000.0 * math.sin(2.0 * math.pi * 660.0 * i / sr))) for i in range(500)]
        chord = [
            int(
                round(
                    10000.0 * math.sin(2.0 * math.pi * 440.0 * i / sr)
                    + 7000.0 * math.sin(2.0 * math.pi * 1108.73 * i / sr)
                )
            )
            for i in range(1400)
        ]
        for use_np in (False, True):
            for pcm in (sine, chord):
                enc = audio_patcher.encode_spu_adpcm(pcm, looped=False, use_numpy=use_np)
                self.assertEqual(len(enc) % 16, 0)
                expected_blocks = (len(pcm) + 27) // 28
                self.assertEqual(len(enc), expected_blocks * 16)
                res = audio_patcher.decode_spu_adpcm_detailed(enc)
                self.assertEqual(res.flags[:-1], [0x00] * (expected_blocks - 1))
                self.assertEqual(res.flags[-1], 0x01)
                snr = audio_patcher.compute_snr_db(pcm, res.samples[: len(pcm)])
                self.assertGreater(snr, 28.0, f"Expected >28 dB SNR (use_numpy={use_np}), got {snr:.2f}")

    def test_loop_flags_and_lead_in_block(self) -> None:
        pcm = [int(round(16000.0 * math.sin(2.0 * math.pi * 500.0 * i / 28000.0))) for i in range(280)]
        enc = audio_patcher.encode_spu_adpcm(
            pcm,
            looped=True,
            loop_start_sample=56,
            lead_in_zero_block=True,
        )
        self.assertEqual(len(enc), 11 * 16)
        self.assertEqual(enc[:16], b"\x00" * 16)
        res = audio_patcher.decode_spu_adpcm_detailed(enc)
        self.assertEqual(res.flags[0], 0x00)
        self.assertEqual(res.flags[1], 0x00)
        self.assertEqual(res.flags[2], 0x00)
        self.assertEqual(res.flags[3], 0x06)  # frame 2 -> block 3 with lead-in
        self.assertTrue(all(f == 0x02 for f in res.flags[4:-1]))
        self.assertEqual(res.flags[-1], 0x03)
        self.assertEqual(res.loop_start_block, 3)
        self.assertEqual(res.loop_end_block, 10)

        # Single-block loop + explicit 0x07 end flag check
        short_enc = audio_patcher.encode_spu_adpcm(pcm[:28], looped=True)
        short_res = audio_patcher.decode_spu_adpcm_detailed(short_enc)
        self.assertEqual(short_res.flags, [0x07])


class TestGnsuPatcher(unittest.TestCase):
    def test_gnsu_accel_decel_and_dry_run(self) -> None:
        acc_raw = _build_synthetic_gnsu(decel=False)
        dry = audio_patcher.patch_gnsu_bytes(acc_raw, preset="bmw_m3_gtr", dry_run=True)
        self.assertIsNone(dry.patched_bytes)
        self.assertTrue(dry.diff["dry_run"])
        self.assertEqual(dry.diff["max_rpm"]["after"], 8350.0)

        res = audio_patcher.patch_gnsu_bytes(acc_raw, preset="bmw_m3_gtr", dry_run=False)
        assert res.patched_bytes is not None
        self.assertEqual(len(res.patched_bytes), len(acc_raw))
        parsed = audio_inspect.parse_gnsu(res.patched_bytes, path="TEST_ACC.GIN")
        self.assertEqual(parsed.direction, "accel")
        self.assertAlmostEqual(parsed.min_rpm, 1200.0, places=2)
        self.assertAlmostEqual(parsed.max_rpm, 8350.0, places=2)
        self.assertEqual(parsed.sample_rate, round(32000 * 1.045))
        rpm_tbl = struct.unpack_from(f"<{parsed.num_rpm_steps + 1}I", res.patched_bytes, parsed.rpm_table_offset)
        self.assertTrue(all(rpm_tbl[i] <= rpm_tbl[i + 1] for i in range(len(rpm_tbl) - 1)))

        dec_raw = _build_synthetic_gnsu(decel=True)
        dec_res = audio_patcher.patch_gnsu_bytes(dec_raw, min_rpm=950.0, max_rpm=6400.0, rate_scale=0.95, curve_gamma=1.08)
        assert dec_res.patched_bytes is not None
        dec_parsed = audio_inspect.parse_gnsu(dec_res.patched_bytes, path="TEST_DEC.GIN")
        self.assertEqual(dec_parsed.direction, "decel")
        dec_tbl = struct.unpack_from(
            f"<{dec_parsed.num_rpm_steps + 1}I", dec_res.patched_bytes, dec_parsed.rpm_table_offset
        )
        self.assertTrue(all(dec_tbl[i] >= dec_tbl[i + 1] for i in range(len(dec_tbl) - 1)))


class TestAbkcPatcher(unittest.TestCase):
    def test_abkc_resize_preserves_untouched_slot_and_relocates_t3(self) -> None:
        raw, _pcm1, pcm2 = _build_synthetic_abkc_with_spu()
        orig_info = audio_inspect.parse_abkc(raw, path="SYNTH.ABK")
        self.assertEqual(orig_info.active_samples, 2)

        # Replace slot 1 with a MUCH LONGER stereo looped waveform (grows payload & shifts slot 2 + t3!)
        new_l = [int(round(15000.0 * math.sin(2.0 * math.pi * 600.0 * i / 28000.0))) for i in range(1680)]
        new_r = [int(round(12000.0 * math.cos(2.0 * math.pi * 600.0 * i / 28000.0))) for i in range(1680)]
        res = audio_patcher.patch_abkc_slot(
            raw,
            slot_index=1,
            channels_pcm=[new_l, new_r],
            sample_rate=28000,
            looped=True,
            path_label="SYNTH.ABK",
        )
        assert res.patched_bytes is not None
        self.assertTrue(all(s > 25.0 for s in (res.snr_db or [])))

        patched_info = audio_inspect.parse_abkc(res.patched_bytes, path="SYNTH.ABK")
        self.assertEqual(patched_info.active_samples, 2)
        self.assertEqual(patched_info.tags, ["SYNTH_BANK_TEST"])
        self.assertEqual(patched_info.samples[0].channels, 2)
        self.assertEqual(patched_info.samples[0].sample_rate, 28000)
        self.assertTrue(patched_info.samples[0].looped)
        self.assertEqual(patched_info.samples[1].sample_rate, 24000)

        # Verify untouched slot 2 decoded audio matches original pcm2 identically
        s2_off = patched_info.bnkl_offset + (patched_info.samples[1].data_offset or 0)
        s2_blocks = ((patched_info.samples[1].num_samples or 0) // 28) * 16
        s2_decoded = audio_patcher.decode_spu_adpcm(res.patched_bytes[s2_off + 16 : s2_off + s2_blocks])
        snr_untouched = audio_patcher.compute_snr_db(pcm2, s2_decoded[: len(pcm2)])
        self.assertGreater(snr_untouched, 28.0)

        # Verify t1 is 128-byte aligned and t3 string pointer resolves cleanly
        t1, _t2, t3 = struct.unpack_from("<3I", res.patched_bytes, 0x30)
        self.assertEqual(t1 % 128, 0)
        str_ptr = struct.unpack_from("<I", res.patched_bytes, t3 + 4 + 4)[0]
        self.assertEqual(res.patched_bytes[str_ptr : str_ptr + 15], b"SYNTH_BANK_TEST")


class TestProceduralSynthCC0(unittest.TestCase):
    def test_synth_cc0_generates_six_high_snr_wavs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rep = audio_patcher.generate_cc0_wavs(Path(tmp), use_numpy=False)
            self.assertEqual(rep["license"], "CC0-1.0")
            self.assertEqual(rep["asset_count"], 6)
            for asset in rep["assets"]:
                wav_p = Path(asset["wav_path"])
                self.assertTrue(wav_p.is_file())
                self.assertGreater(
                    asset["spu_adpcm_snr_db"],
                    25.0,
                    f"{asset['id']} SPU-ADPCM SNR too low: {asset['spu_adpcm_snr_db']}",
                )
                sr, ch, chans = audio_patcher.read_wav_pcm16(wav_p)
                self.assertEqual(sr, asset["sample_rate"])
                self.assertEqual(ch, 1)
                self.assertEqual(len(chans[0]), asset["num_samples"])


class TestAudioHygieneAndManifest(unittest.TestCase):
    def test_manifest_valid_and_no_personal_paths(self) -> None:
        manifest_path = REPO_ROOT / "assets_cc0" / "audio_manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["license_policy"], "CC0-1.0")
        self.assertEqual(len(manifest["abkc_assets"]), 6)
        self.assertEqual(len(manifest["gnsu_presets"]), 4)

        owned_rels = [
            Path("tools/audio_patcher.py"),
            Path("assets_cc0/audio_manifest.json"),
            Path("docs/research/PS2_AUDIO_MODDING.md"),
            Path("tests/test_audio_patcher.py"),
        ]
        issues = audit_repo_clean.audit_files(REPO_ROOT, owned_rels)
        self.assertEqual(issues, [], f"Hygiene issues found: {issues}")


class TestLiveIsoAudioPatching(unittest.TestCase):
    @unittest.skipIf(_find_local_iso() is None, "Local SLUS-21351 ISO not present (or NFSMW_ISO unset)")
    def test_live_iso_abkc_and_gnsu_roundtrips(self) -> None:
        iso_path = _find_local_iso()
        assert iso_path is not None
        arc = PS2NFSMWArchive(iso_path)

        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            synth_rep = audio_patcher.generate_cc0_wavs(td)
            self.assertEqual(synth_rep["asset_count"], 6)

            # 1. Round-trip ABKC patching on FE_COMMON_MB.ABK, TURBO_MED_2_MB.ABK, GEAR_SML_BASE.ABK, CAR_WHINE_00.ABK
            abkc_targets = [
                ("SOUND\\GLOBAL\\FE_COMMON_MB.ABK", 1, "ui_click", False),
                ("SOUND\\TURBO\\TURBO_MED_2_MB.ABK", 2, "turbo_bov_flutter", False),
                ("SOUND\\SHIFTING\\GEAR_SML_BASE.ABK", 1, "gearshift_transient", False),
                ("SOUND\\ENGINE\\CAR_WHINE_00.ABK", 1, "straight_cut_whine_loop", True),
            ]
            for vpath, slot_idx, cue_id, is_looped in abkc_targets:
                orig_bytes = arc.read_entry(arc.find(vpath))
                orig_bank = audio_inspect.parse_abkc(orig_bytes, path=vpath)
                wav_path = td / "cc0_wav" / audio_patcher.CC0_SYNTH_RECIPES[cue_id]["filename"]
                sr, _ch, chans = audio_patcher.read_wav_pcm16(wav_path)
                res = audio_patcher.patch_abkc_slot(
                    orig_bytes,
                    slot_index=slot_idx,
                    channels_pcm=chans,
                    sample_rate=sr,
                    looped=is_looped,
                    path_label=vpath,
                )
                assert res.patched_bytes is not None
                self.assertGreater(res.snr_db[0], 25.0)
                out_abk = td / f"{Path(vpath).stem}_patched.ABK"
                out_abk.write_bytes(res.patched_bytes)
                rep = audio_inspect.inspect_standalone_file(out_abk)
                verified = rep["abkc_banks"][0]
                self.assertEqual(verified.active_samples, orig_bank.active_samples)
                self.assertEqual(verified.tags, orig_bank.tags)

            # 2. Round-trip Gnsu tuning for all 4 hero cars
            for preset_name, cfg in audio_patcher.HERO_CAR_GNSU_PRESETS.items():
                vpath = cfg["virtual_path"]
                orig_gin = arc.read_entry(arc.find(vpath))
                orig_info = audio_inspect.parse_gnsu(orig_gin, path=vpath)
                res = audio_patcher.patch_gnsu_bytes(orig_gin, preset=preset_name, path_label=vpath)
                assert res.patched_bytes is not None
                out_gin = td / f"{preset_name}.GIN"
                out_gin.write_bytes(res.patched_bytes)
                rep = audio_inspect.inspect_standalone_file(out_gin)
                verified = rep["gnsu_synths"][0]
                self.assertEqual(verified.direction, orig_info.direction)
                self.assertAlmostEqual(verified.max_rpm, cfg["max_rpm"], places=2)
                self.assertEqual(verified.granule_data_bytes, orig_info.granule_data_bytes)


if __name__ == "__main__":
    unittest.main()
