#!/usr/bin/env python3
"""audio_patcher.py — Clean-Room PS2 NFSMW Audio Encoder, Patcher & CC0 Synth.

Path 1 PS2 Max-Ceiling Audio pipeline for Need for Speed: Most Wanted - Black
Edition (PS2, SLUS-21351):

  1. Clean-room Sony PS2 SPU-ADPCM (VAG / EA 0x14 / BNKl SPU-RAM) 16-byte block
     encoder & decoder:
       - 5-filter predictor selection minimizing closed-loop squared error per
         28-sample frame
       - Loop flags: 0x00 (normal), 0x01 (one-shot end), 0x02 (loop body),
         0x03 (loop end+repeat), 0x06 (loop start+repeat), 0x07 (single-block
         loop / VAG mute-end)
       - Exact 28-sample / 16-byte block padding & SNR verification
  2. Gnsu v20 Engine Synth Patcher:
       - Tunes min_rpm, max_rpm, sample_rate (exhaust pitch/presence), and
         monotonic rpm_table grain/pitch response curves for hero cars
         (BMW M3 GTR, Carrera GT, Gallardo, Camaro SS)
       - Supports dry-run diff summaries & round-trip verification via audio_inspect
  3. ABKC / BNKl Sample Patcher:
       - Replaces PCM WAV -> PS2 SPU-ADPCM samples inside ABKC banks
       - Safely updates PT TLV metadata (sample_rate, num_samples, loop_start,
         loop_length, data_offset), BNKl header/payload sizes, 128-byte aligned
         t1/t2/t3 chunk bounds, and t3 string relocation pointers while keeping
         all untouched samples bit-for-bit intact
  4. Procedural CC0-1.0 Synthesizer (synth-cc0):
       - Generates 100% original procedural WAV assets (GT7/FH6-inspired UI tones,
         turbo blow-off flutter, crisp gearshift transient, straight-cut gearbox
         harmonic loop) inside gitignored build/audio_wave3/cc0_wav/

Pure Python 3 standard library (wave, array, struct, math), with optional NumPy
acceleration for SPU-ADPCM batch/frame computations when installed.

NON-NEGOTIABLE SAFETY: Never opens any .iso for writing; all ISO reads use
strictly read-only 'rb' mode and respect os.environ.get("NFSMW_ISO").
"""

from __future__ import annotations

import argparse
import array
import dataclasses
import json
import math
import os
import struct
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np  # type: ignore

    HAS_NUMPY = True
except ImportError:  # pragma: no cover - optional acceleration
    np = None  # type: ignore
    HAS_NUMPY = False

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import audio_inspect  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

# Sony PS2 SPU-ADPCM (VAG) 5-predictor filter coefficients (64-scaled fixed point)
SPU_FILTER_FIXED: Tuple[Tuple[int, int], ...] = (
    (0, 0),
    (60, 0),
    (115, -52),
    (98, -55),
    (122, -60),
)
SPU_FILTER_FLOAT: Tuple[Tuple[float, float], ...] = tuple(
    (k0 / 64.0, k1 / 64.0) for k0, k1 in SPU_FILTER_FIXED
)

SPU_FLAG_NORMAL = 0x00
SPU_FLAG_END_MUTE = 0x01
SPU_FLAG_LOOP_BODY = 0x02
SPU_FLAG_LOOP_END = 0x03
SPU_FLAG_LOOP_START = 0x06
SPU_FLAG_VAG_END = 0x07

VALID_SPU_FLAGS = frozenset(
    {
        SPU_FLAG_NORMAL,
        SPU_FLAG_END_MUTE,
        SPU_FLAG_LOOP_BODY,
        SPU_FLAG_LOOP_END,
        SPU_FLAG_LOOP_START,
        SPU_FLAG_VAG_END,
    }
)

DEFAULT_ISO_REL = Path("Documents") / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"


def resolve_iso_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """Resolve PS2 ISO path from explicit arg, NFSMW_ISO env var, or default Home path."""
    if explicit is not None:
        return explicit if explicit.is_file() else None
    if "NFSMW_ISO" in os.environ:
        env_val = os.environ["NFSMW_ISO"].strip()
        if not env_val:
            return None
        p = Path(env_val)
        return p if p.is_file() else None
    default_iso = Path.home() / DEFAULT_ISO_REL
    return default_iso if default_iso.is_file() else None


def _clamp_i16(val: float) -> int:
    iv = int(round(val))
    if iv > 32767:
        return 32767
    if iv < -32768:
        return -32768
    return iv


# ---------------------------------------------------------------------------
# WAV I/O & PCM Helpers (Pure Python stdlib wave + array)
# ---------------------------------------------------------------------------


def read_wav_pcm16(path: Path) -> Tuple[int, int, List[List[int]]]:
    """Read an uncompressed 16-bit (or 8-bit) PCM WAV file -> (sample_rate, channels, channel_lists)."""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)

    if channels not in (1, 2):
        raise ValueError(f"Unsupported channel count {channels} in {path} (expected 1 or 2)")

    if sampwidth == 2:
        arr = array.array("h")
        arr.frombytes(raw)
        if sys.byteorder != "little":
            arr.byteswap()
        samples = list(arr)
    elif sampwidth == 1:
        samples = [((b - 128) << 8) for b in raw]
    else:
        raise ValueError(f"Unsupported WAV sample width {sampwidth * 8}-bit in {path} (expected 8 or 16)")

    if channels == 1:
        return sample_rate, 1, [samples]
    return sample_rate, 2, [samples[0::2], samples[1::2]]


def write_wav_pcm16(path: Path, sample_rate: int, channels_pcm: Sequence[Sequence[int]]) -> None:
    """Write mono or stereo 16-bit signed little-endian PCM WAV."""
    if not channels_pcm or len(channels_pcm) not in (1, 2):
        raise ValueError("channels_pcm must contain 1 or 2 channels")
    n_ch = len(channels_pcm)
    n_frames = len(channels_pcm[0])
    if n_ch == 2 and len(channels_pcm[1]) != n_frames:
        raise ValueError("Stereo channels must have equal sample counts")

    if n_ch == 1:
        interleaved = [_clamp_i16(x) for x in channels_pcm[0]]
    else:
        interleaved = []
        ch0, ch1 = channels_pcm[0], channels_pcm[1]
        for i in range(n_frames):
            interleaved.append(_clamp_i16(ch0[i]))
            interleaved.append(_clamp_i16(ch1[i]))

    arr = array.array("h", interleaved)
    if sys.byteorder != "little":
        arr.byteswap()

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(n_ch)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(arr.tobytes())


# ---------------------------------------------------------------------------
# Clean-Room Sony PS2 SPU-ADPCM (VAG / EA 0x14) Encoder & Decoder
# ---------------------------------------------------------------------------


def _find_best_frame_encoding(
    frame: Sequence[float],
    s1_init: float,
    s2_init: float,
) -> Tuple[int, int, List[int], float, float, float]:
    """Select (predictor 0..4, shift 0..12) minimizing closed-loop squared error on 28 samples."""
    best_sse = float("inf")
    best_pred = 0
    best_shift = 0
    best_nibs = [0] * 28
    best_s1 = s1_init
    best_s2 = s2_init

    for pred_idx, (f0, f1) in enumerate(SPU_FILTER_FLOAT):
        # Quick open-loop peak residual estimate to narrow candidate shifts
        t1, t2 = s1_init, s2_init
        max_abs_res = 0.0
        for sample in frame:
            est = sample - (t1 * f0 + t2 * f1)
            a = abs(est)
            if a > max_abs_res:
                max_abs_res = a
            t2 = t1
            t1 = sample

        # Nominal shift where max_abs_res fits inside [-8, 7] * (4096 >> shift)
        nom_shift = 12
        scaled_peak = int(math.ceil(max_abs_res))
        while nom_shift > 0 and (scaled_peak >> (12 - nom_shift)) > 7:
            nom_shift -= 1
        while nom_shift < 12 and scaled_peak > 0 and (scaled_peak >> (12 - nom_shift)) < 4:
            # Check if a tighter shift gives better resolution without clipping
            if (scaled_peak >> (12 - (nom_shift + 1))) <= 7:
                nom_shift += 1
            else:
                break

        # Evaluate nominal shift and immediate neighbors with full closed-loop IIR feedback
        cand_shifts = sorted({max(0, min(12, nom_shift + d)) for d in (-1, 0, 1)})
        for shift in cand_shifts:
            cs1, cs2 = s1_init, s2_init
            sse = 0.0
            nibs = [0] * 28
            step_scale = (1 << shift) / 4096.0
            for i, target in enumerate(frame):
                pred_val = cs1 * f0 + cs2 * f1
                diff = target - pred_val
                q = int(round(diff * step_scale))
                if q > 7:
                    q = 7
                elif q < -8:
                    q = -8
                nibs[i] = q
                recon = ((q << 12) >> shift) + pred_val
                if recon > 32767.0:
                    recon = 32767.0
                elif recon < -32768.0:
                    recon = -32768.0
                err = target - recon
                sse += err * err
                if sse >= best_sse:
                    break
                cs2 = cs1
                cs1 = recon
            else:
                if sse < best_sse:
                    best_sse = sse
                    best_pred = pred_idx
                    best_shift = shift
                    best_nibs = nibs
                    best_s1 = cs1
                    best_s2 = cs2

    return best_pred, best_shift, best_nibs, best_s1, best_s2, best_sse


def encode_spu_adpcm(
    pcm_samples: Sequence[int],
    *,
    looped: bool = False,
    loop_start_sample: int = 0,
    lead_in_zero_block: bool = False,
    end_flag: Optional[int] = None,
    use_numpy: Optional[bool] = None,
) -> bytes:
    """Encode mono 16-bit PCM samples into 16-byte Sony PS2 SPU-ADPCM (VAG) blocks.

    Args:
        pcm_samples: Input mono PCM samples (16-bit signed range).
        looped: If True, emit PS2 SPU loop flags (0x06 loop-start, 0x02 body, 0x03 loop-end).
        loop_start_sample: Sample index within `pcm_samples` where loop starts (rounded to 28-sample frame).
        lead_in_zero_block: If True, prepend one silent 16-byte block (retail BNKl hardware convention).
        end_flag: Explicit final block flag override (e.g. 0x01, 0x03, or 0x07).
        use_numpy: Optional numpy pre-conversion toggle (falls back to pure stdlib cleanly).
    """
    if end_flag is not None and end_flag not in VALID_SPU_FLAGS:
        raise ValueError(f"Invalid SPU-ADPCM end_flag 0x{end_flag:02x}")

    want_np = HAS_NUMPY if use_numpy is None else (bool(use_numpy) and HAS_NUMPY)
    if want_np:
        arr = np.asarray(pcm_samples, dtype=np.float64)
        padded_len = ((len(arr) + 27) // 28) * 28
        if padded_len == 0:
            padded_len = 28
        if len(arr) < padded_len:
            arr = np.pad(arr, (0, padded_len - len(arr)), mode="constant")
        samples_list: List[float] = arr.tolist()
    else:
        samples_list = [float(x) for x in pcm_samples]
        rem = len(samples_list) % 28
        if len(samples_list) == 0:
            samples_list = [0.0] * 28
        elif rem != 0:
            samples_list.extend([0.0] * (28 - rem))

    num_audio_frames = len(samples_list) // 28
    loop_start_frame = max(0, min(num_audio_frames - 1, loop_start_sample // 28))

    out = bytearray()
    if lead_in_zero_block:
        out.extend(b"\x00" * 16)

    s1 = 0.0
    s2 = 0.0
    for frame_idx in range(num_audio_frames):
        frame = samples_list[frame_idx * 28 : (frame_idx + 1) * 28]
        pred, shift, nibs, s1, s2, _sse = _find_best_frame_encoding(frame, s1, s2)

        is_last = frame_idx == num_audio_frames - 1
        if looped:
            if num_audio_frames == 1:
                flag = SPU_FLAG_VAG_END if end_flag is None else end_flag
            elif is_last:
                flag = SPU_FLAG_LOOP_END if end_flag is None else end_flag
            elif frame_idx == loop_start_frame:
                flag = SPU_FLAG_LOOP_START
            elif frame_idx > loop_start_frame:
                flag = SPU_FLAG_LOOP_BODY
            else:
                flag = SPU_FLAG_NORMAL
        else:
            if is_last:
                flag = SPU_FLAG_END_MUTE if end_flag is None else end_flag
            else:
                flag = SPU_FLAG_NORMAL

        out.append(((pred & 0x0F) << 4) | (shift & 0x0F))
        out.append(flag)
        for k in range(0, 28, 2):
            lo = nibs[k] & 0x0F
            hi = nibs[k + 1] & 0x0F
            out.append(lo | (hi << 4))

    return bytes(out)


@dataclasses.dataclass
class SpuDecodeResult:
    samples: List[int]
    block_count: int
    flags: List[int]
    loop_start_block: Optional[int]
    loop_end_block: Optional[int]


def decode_spu_adpcm_detailed(data: bytes) -> SpuDecodeResult:
    """Decode Sony PS2 SPU-ADPCM (VAG) 16-byte blocks into 16-bit signed PCM + block flags."""
    if len(data) % 16 != 0:
        raise ValueError(f"SPU-ADPCM payload length {len(data)} is not a multiple of 16 bytes")

    s1 = 0.0
    s2 = 0.0
    out: List[int] = []
    flags: List[int] = []
    loop_start_blk: Optional[int] = None
    loop_end_blk: Optional[int] = None

    block_count = len(data) // 16
    for blk_idx in range(block_count):
        off = blk_idx * 16
        ps = data[off]
        fl = data[off + 1]
        flags.append(fl)
        if fl in (SPU_FLAG_LOOP_START, SPU_FLAG_VAG_END) and loop_start_blk is None and (fl & 0x04):
            loop_start_blk = blk_idx
        if fl & 0x01:
            loop_end_blk = blk_idx

        shift = ps & 0x0F
        pred = (ps >> 4) & 0x0F
        if pred > 4:
            pred = 0
        f0, f1 = SPU_FILTER_FLOAT[pred]

        for b in data[off + 2 : off + 16]:
            for nib in (b & 0x0F, (b >> 4) & 0x0F):
                if nib >= 8:
                    nib -= 16
                sample = ((nib << 12) >> shift) + s1 * f0 + s2 * f1
                s2 = s1
                s1 = sample
                out.append(_clamp_i16(sample))

    return SpuDecodeResult(
        samples=out,
        block_count=block_count,
        flags=flags,
        loop_start_block=loop_start_blk,
        loop_end_block=loop_end_blk,
    )


def decode_spu_adpcm(data: bytes) -> List[int]:
    """Decode Sony PS2 SPU-ADPCM (VAG) 16-byte blocks into mono 16-bit PCM list."""
    return decode_spu_adpcm_detailed(data).samples


def compute_snr_db(reference: Sequence[int], reconstructed: Sequence[int]) -> float:
    """Compute Signal-to-Noise Ratio in dB between reference and reconstructed PCM."""
    n = min(len(reference), len(reconstructed))
    if n == 0:
        return 0.0
    sig_pow = 0.0
    err_pow = 0.0
    for i in range(n):
        r = float(reference[i])
        d = r - float(reconstructed[i])
        sig_pow += r * r
        err_pow += d * d
    if sig_pow == 0.0:
        return 99.0 if err_pow == 0.0 else 0.0
    if err_pow == 0.0:
        return 99.0
    return 10.0 * math.log10(sig_pow / err_pow)


# ---------------------------------------------------------------------------
# Gnsu v20 Engine Synth Patcher (RPM bounds, grain curve, exhaust presence)
# ---------------------------------------------------------------------------

HERO_CAR_GNSU_PRESETS: Dict[str, Dict[str, Any]] = {
    "bmw_m3_gtr": {
        "car_name": "BMW M3 GTR (E46)",
        "vlt_engineaudio": "tvr_cerb",
        "virtual_path": "SOUND\\ENGINE\\GIN_TVR_CERBERA.GIN",
        "decel_virtual_path": "SOUND\\ENGINE\\GIN_TVR_CERBERA_DCL.GIN",
        "alt_virtual_path": "SOUND\\ENGINE\\GIN_BMW_M3_LAGSECA_V2.GIN",
        "min_rpm": 1200.0,
        "max_rpm": 8350.0,
        "rate_scale": 1.045,
        "curve_gamma": 0.92,
        "notes": "Extends P60B40 V8 ceiling to 8350 RPM with sharper mid-range grain response and +4.5% upper-harmonic bark.",
    },
    "carrera_gt": {
        "car_name": "Porsche Carrera GT",
        "vlt_engineaudio": "por_car_gt",
        "virtual_path": "SOUND\\ENGINE\\GIN_POR_CARRERA_GT.GIN",
        "decel_virtual_path": "SOUND\\ENGINE\\GIN_POR_CARRERA_GT_DCL.GIN",
        "min_rpm": 1150.0,
        "max_rpm": 8450.0,
        "rate_scale": 1.035,
        "curve_gamma": 0.90,
        "notes": "F1-derived V10 howl extended to 8450 RPM with snappy top-end grain sweep.",
    },
    "gallardo": {
        "car_name": "Lamborghini Gallardo",
        "vlt_engineaudio": "lam_gallardo",
        "virtual_path": "SOUND\\ENGINE\\GIN_GALLARDO.GIN",
        "decel_virtual_path": "SOUND\\ENGINE\\GIN_GALLARDO_DCL.GIN",
        "min_rpm": 1350.0,
        "max_rpm": 8500.0,
        "rate_scale": 1.030,
        "curve_gamma": 0.93,
        "notes": "Odd-firing 5.0L V10 screamer tuned for aggressive 8500 RPM peak & tight grain pacing.",
    },
    "camaro_ss": {
        "car_name": "Chevrolet Camaro SS",
        "vlt_engineaudio": "for_mus_b",
        "virtual_path": "SOUND\\ENGINE\\GIN_MUSTSAL_RAMP_V2_CD.GIN",
        "decel_virtual_path": "SOUND\\ENGINE\\GIN_MUSTANG_SALEEN_DCL.GIN",
        "min_rpm": 950.0,
        "max_rpm": 6400.0,
        "rate_scale": 0.955,
        "curve_gamma": 1.06,
        "notes": "Deepens pushrod V8 exhaust presence (-4.5% sample rate) and expands stock 5154 RPM cap to 6400 RPM.",
    },
}


@dataclasses.dataclass
class GnsuPatchResult:
    diff: Dict[str, Any]
    patched_bytes: Optional[bytes]
    verified_info: Optional[Dict[str, Any]]


def _shape_rpm_table(
    rpm_vals: Sequence[int],
    *,
    total_samples: int,
    curve_gamma: float,
) -> List[int]:
    """Remap rpm_table sample positions by normalized exponent `curve_gamma` preserving direction & endpoints."""
    if len(rpm_vals) < 2 or abs(curve_gamma - 1.0) < 1e-6:
        return list(rpm_vals)

    start_val = rpm_vals[0]
    end_val = rpm_vals[-1]
    span = end_val - start_val
    if span == 0:
        return list(rpm_vals)

    out: List[int] = [start_val]
    is_accel = span > 0
    for idx in range(1, len(rpm_vals) - 1):
        u = (rpm_vals[idx] - start_val) / float(span)
        u = max(0.0, min(1.0, u))
        u_shaped = u ** curve_gamma
        val = int(round(start_val + u_shaped * span))
        val = max(0, min(total_samples, val))
        if is_accel:
            val = max(out[-1], min(end_val, val))
        else:
            val = min(out[-1], max(end_val, val))
        out.append(val)
    out.append(end_val)
    return out


def patch_gnsu_bytes(
    data: bytes,
    *,
    min_rpm: Optional[float] = None,
    max_rpm: Optional[float] = None,
    sample_rate: Optional[int] = None,
    rate_scale: Optional[float] = None,
    curve_gamma: float = 1.0,
    preset: Optional[str] = None,
    dry_run: bool = False,
    path_label: str = "<buffer>",
) -> GnsuPatchResult:
    """Tune a Gnsu v20 granular synth container and round-trip verify with audio_inspect.parse_gnsu."""
    info = audio_inspect.parse_gnsu(data, path=path_label)
    if info.version != "20":
        raise ValueError(f"Unsupported Gnsu version {info.version!r} (expected '20')")

    if preset is not None:
        if preset not in HERO_CAR_GNSU_PRESETS:
            raise ValueError(f"Unknown Gnsu preset {preset!r}; choose from {sorted(HERO_CAR_GNSU_PRESETS)}")
        p_cfg = HERO_CAR_GNSU_PRESETS[preset]
        if min_rpm is None:
            min_rpm = float(p_cfg["min_rpm"])
        if max_rpm is None:
            max_rpm = float(p_cfg["max_rpm"])
        if rate_scale is None and sample_rate is None:
            rate_scale = float(p_cfg["rate_scale"])
        if abs(curve_gamma - 1.0) < 1e-6:
            curve_gamma = float(p_cfg["curve_gamma"])

    if curve_gamma <= 0.0:
        raise ValueError(f"curve_gamma must be > 0, got {curve_gamma}")

    _, old_min_rpm, old_max_rpm, n_rpm, n_grain, total_samples, old_sr = struct.unpack_from("<4sff4I", data, 4)

    new_min_rpm = float(old_min_rpm if min_rpm is None else min_rpm)
    new_max_rpm = float(old_max_rpm if max_rpm is None else max_rpm)
    if new_min_rpm <= 0.0 or new_max_rpm <= new_min_rpm:
        raise ValueError(f"Invalid RPM bounds: min_rpm={new_min_rpm}, max_rpm={new_max_rpm}")

    if sample_rate is not None:
        new_sr = int(sample_rate)
    elif rate_scale is not None:
        new_sr = int(round(old_sr * float(rate_scale)))
    else:
        new_sr = int(old_sr)
    new_sr = max(8000, min(48000, new_sr))

    rpm_entries = n_rpm + 1
    old_rpm_vals = list(struct.unpack_from(f"<{rpm_entries}I", data, info.rpm_table_offset))
    new_rpm_vals = _shape_rpm_table(old_rpm_vals, total_samples=total_samples, curve_gamma=curve_gamma)

    mid_idx = len(old_rpm_vals) // 2
    diff: Dict[str, Any] = {
        "format": "Gnsu",
        "version": info.version,
        "path": path_label,
        "preset": preset,
        "direction": info.direction,
        "byte_size": len(data),
        "num_rpm_steps": n_rpm,
        "num_grains": n_grain,
        "total_samples": total_samples,
        "min_rpm": {"before": round(old_min_rpm, 3), "after": round(new_min_rpm, 3)},
        "max_rpm": {"before": round(old_max_rpm, 3), "after": round(new_max_rpm, 3)},
        "sample_rate": {"before": old_sr, "after": new_sr},
        "curve_gamma": round(curve_gamma, 4),
        "rpm_table_mid": {
            "index": mid_idx,
            "before": old_rpm_vals[mid_idx] if old_rpm_vals else 0,
            "after": new_rpm_vals[mid_idx] if new_rpm_vals else 0,
        },
        "dry_run": dry_run,
    }

    if dry_run:
        return GnsuPatchResult(diff=diff, patched_bytes=None, verified_info=None)

    buf = bytearray(data)
    struct.pack_into("<ff", buf, 8, new_min_rpm, new_max_rpm)
    struct.pack_into("<I", buf, 0x1C, new_sr)
    if rpm_entries > 0:
        struct.pack_into(f"<{rpm_entries}I", buf, info.rpm_table_offset, *new_rpm_vals)

    patched = bytes(buf)
    verified = audio_inspect.parse_gnsu(patched, path=path_label)
    if verified.direction != info.direction:
        raise RuntimeError(f"Gnsu direction changed unexpectedly: {info.direction} -> {verified.direction}")
    if verified.granule_data_bytes != info.granule_data_bytes:
        raise RuntimeError("Gnsu granule payload length altered unexpectedly")

    return GnsuPatchResult(diff=diff, patched_bytes=patched, verified_info=verified.to_dict())


# ---------------------------------------------------------------------------
# ABKC / BNKl Sample Patcher
# ---------------------------------------------------------------------------


def _parse_pt_tlv_records(buf: bytes, pt_offset: int) -> Tuple[int, int, List[Tuple[int, bytes]], int]:
    """Parse raw PT header -> (pt_version, padded_end_offset, [(tag, raw_val_bytes)], end_marker_off).

    Preserves unknown/single-byte tags (0xFC/0xFD/0xFE encoded with empty bytes) so round-trip
    serialization retains every EA property flag intact.
    """
    if pt_offset + 4 > len(buf) or buf[pt_offset : pt_offset + 2] != b"PT":
        raise ValueError(f"Expected PT magic at 0x{pt_offset:x}")
    pt_ver = struct.unpack_from("<H", buf, pt_offset + 2)[0]
    q = pt_offset + 4
    records: List[Tuple[int, bytes]] = []
    while q < len(buf):
        tag = buf[q]
        if tag == 0xFF:
            q += 1
            break
        if tag in (0xFC, 0xFD, 0xFE):
            records.append((tag, b""))
            q += 1
            continue
        if q + 1 >= len(buf):
            break
        ln = buf[q + 1]
        if q + 2 + ln > len(buf):
            break
        records.append((tag, buf[q + 2 : q + 2 + ln]))
        q += 2 + ln

    raw_end = q
    padded_end = raw_end + ((4 - (raw_end - pt_offset) % 4) % 4)
    return pt_ver, padded_end, records, raw_end


def _encode_be_uint(val: int, min_bytes: int = 1) -> bytes:
    if val == 0:
        raw = b"\x00"
    else:
        raw = val.to_bytes((val.bit_length() + 7) // 8, "big")
    if len(raw) < min_bytes:
        raw = (b"\x00" * (min_bytes - len(raw))) + raw
    return raw


def _serialize_pt_tlv_records(pt_ver: int, records: Sequence[Tuple[int, bytes]]) -> bytes:
    """Serialize PT\x05\x00 + TLV records + 0xFF + 4-byte padding."""
    out = bytearray(b"PT") + struct.pack("<H", pt_ver)
    for tag, val_bytes in records:
        if tag in (0xFC, 0xFD, 0xFE):
            out.append(tag)
        else:
            out.append(tag)
            out.append(len(val_bytes))
            out.extend(val_bytes)
    out.append(0xFF)
    while len(out) % 4 != 0:
        out.append(0x00)
    return bytes(out)


def _set_or_update_pt_tag(
    records: List[Tuple[int, bytes]],
    tag: int,
    value: int,
    *,
    min_bytes: int = 1,
    insert_before_tags: Tuple[int, ...] = (0x88, 0x89, 0x8A, 0xFC, 0xFD, 0xFE),
) -> None:
    encoded = _encode_be_uint(value, min_bytes=min_bytes)
    for i, (t, _) in enumerate(records):
        if t == tag:
            records[i] = (tag, encoded)
            return
    insert_idx = len(records)
    for i, (t, _) in enumerate(records):
        if t in insert_before_tags:
            insert_idx = i
            break
    records.insert(insert_idx, (tag, encoded))


def _remove_pt_tag(records: List[Tuple[int, bytes]], tag: int) -> None:
    records[:] = [(t, v) for (t, v) in records if t != tag]


def _get_pt_tag_int(records: Sequence[Tuple[int, bytes]], tag: int) -> Optional[int]:
    for t, v in records:
        if t == tag:
            return int.from_bytes(v, "big") if v else 0
    return None


@dataclasses.dataclass
class AbkcPatchResult:
    diff: Dict[str, Any]
    patched_bytes: Optional[bytes]
    verified_bank: Optional[Dict[str, Any]]
    snr_db: Optional[List[float]]


def patch_abkc_slot(
    abkc_data: bytes,
    slot_index: int,
    channels_pcm: Sequence[Sequence[int]],
    sample_rate: int,
    *,
    looped: bool = False,
    loop_start_sample: int = 0,
    lead_in_zero_block: bool = True,
    dry_run: bool = False,
    path_label: str = "<buffer>",
    use_numpy: Optional[bool] = None,
) -> AbkcPatchResult:
    """Replace an SPU-ADPCM sample slot inside an ABKC/BNKl bank with PCM audio.

    Keeps all untouched slots and their encoded payloads completely intact. Updates
    PT TLV fields, BNKl slot offsets, BNKl header/payload sizes, ABKC header totals,
    128-byte aligned t1/t2/t3 chunk boundaries, and t3 string relocation offsets.
    """
    bank_info = audio_inspect.parse_abkc(abkc_data, path=path_label)
    bo = bank_info.bnkl_offset
    _btag, bnkl_ver, slot_count, old_hdr_sz, old_pay_sz, unk10 = struct.unpack_from("<4sHHIII", abkc_data, bo)
    slot_tbl_off = bo + 0x14

    if slot_index < 0 or slot_index >= slot_count:
        raise IndexError(f"slot_index {slot_index} out of range [0..{slot_count - 1}]")

    rel_offs = list(struct.unpack_from(f"<{slot_count}I", abkc_data, slot_tbl_off))
    if rel_offs[slot_index] == 0:
        raise ValueError(f"Slot {slot_index} in {path_label} is empty (rel_offset == 0)")

    # Parse every active slot's PT record list and locate old payload region
    slot_records: Dict[int, Tuple[int, List[Tuple[int, bytes]]]] = {}
    spu_offsets: List[int] = []
    for idx, rel in enumerate(rel_offs):
        if rel == 0:
            continue
        pt_off = slot_tbl_off + idx * 4 + rel
        pt_ver, _end, recs, _raw_end = _parse_pt_tlv_records(abkc_data, pt_off)
        slot_records[idx] = (pt_ver, recs)
        codec_id = _get_pt_tag_int(recs, 0xA0)
        d88 = _get_pt_tag_int(recs, 0x88)
        d89 = _get_pt_tag_int(recs, 0x89)
        if codec_id in (None, 0x14, 0x0A):
            if d88 is not None and d88 >= old_hdr_sz:
                spu_offsets.append(d88)
            if d89 is not None and d89 >= old_hdr_sz:
                spu_offsets.append(d89)

    target_ver, target_recs = slot_records[slot_index]
    target_codec = _get_pt_tag_int(target_recs, 0xA0)
    old_data_off = _get_pt_tag_int(target_recs, 0x88)
    old_data_off_r = _get_pt_tag_int(target_recs, 0x89)
    if old_data_off is None or old_data_off < old_hdr_sz:
        raise ValueError(
            f"Slot {slot_index} in {path_label} is not an SPU-RAM ADPCM payload slot "
            f"(codec={target_codec}, data_offset={old_data_off}, bnkl_header_size={old_hdr_sz})"
        )

    n_ch = len(channels_pcm)
    if n_ch not in (1, 2):
        raise ValueError(f"Expected 1 or 2 PCM channels, got {n_ch}")

    # Encode each channel to 16-byte Sony SPU-ADPCM blocks
    encoded_chans: List[bytes] = []
    snrs: List[float] = []
    for ch_samples in channels_pcm:
        enc = encode_spu_adpcm(
            ch_samples,
            looped=looped,
            loop_start_sample=loop_start_sample,
            lead_in_zero_block=lead_in_zero_block,
            use_numpy=use_numpy,
        )
        encoded_chans.append(enc)
        dec = decode_spu_adpcm(enc[16:] if lead_in_zero_block else enc)
        snrs.append(round(compute_snr_db(ch_samples, dec), 2))

    new_ch_bytes = len(encoded_chans[0])
    encoded_total_samples = (new_ch_bytes // 16) * 28
    new_payload_blob = b"".join(encoded_chans)

    # Determine whether old_data_off is aliased by another slot
    other_offsets = set()
    for idx, (_ver, recs) in slot_records.items():
        if idx == slot_index:
            continue
        for tag in (0x88, 0x89):
            ov = _get_pt_tag_int(recs, tag)
            if ov is not None:
                other_offsets.add(ov)

    is_aliased = old_data_off in other_offsets or (old_data_off_r is not None and old_data_off_r in other_offsets)
    old_pay_start_abs = bo + old_hdr_sz
    old_pay_bytes = abkc_data[old_pay_start_abs : old_pay_start_abs + old_pay_sz]

    sorted_bounds = sorted(set(spu_offsets) | {old_hdr_sz + old_pay_sz})
    if is_aliased:
        # Safe append mode so aliased sibling slots keep their exact original audio
        new_rel_to_pay_start = len(old_pay_bytes)
        new_pay_bytes = old_pay_bytes + new_payload_blob
        splice_old_off = None
        payload_delta = len(new_payload_blob)
    else:
        pos_in_bounds = sorted_bounds.index(old_data_off)
        old_end = sorted_bounds[pos_in_bounds + 1]
        if n_ch == 2 and old_data_off_r is not None and old_data_off_r == old_end:
            pos_r = sorted_bounds.index(old_data_off_r)
            old_end = sorted_bounds[pos_r + 1]
        rel_start = old_data_off - old_hdr_sz
        rel_end = old_end - old_hdr_sz
        new_rel_to_pay_start = rel_start
        new_pay_bytes = old_pay_bytes[:rel_start] + new_payload_blob + old_pay_bytes[rel_end:]
        splice_old_off = old_data_off
        payload_delta = len(new_payload_blob) - (rel_end - rel_start)

    old_slot_info = next(s for s in bank_info.samples if s.slot_index == slot_index)
    diff: Dict[str, Any] = {
        "format": "ABKC",
        "path": path_label,
        "slot_index": slot_index,
        "channels": {"before": old_slot_info.channels, "after": n_ch},
        "sample_rate": {"before": old_slot_info.sample_rate, "after": sample_rate},
        "num_samples": {"before": old_slot_info.num_samples, "after": encoded_total_samples},
        "looped": {"before": old_slot_info.looped, "after": looped},
        "adpcm_bytes": len(new_payload_blob),
        "payload_delta_bytes": payload_delta,
        "aliased_append": is_aliased,
        "snr_db": snrs,
        "dry_run": dry_run,
    }

    if dry_run:
        return AbkcPatchResult(diff=diff, patched_bytes=None, verified_bank=None, snr_db=snrs)

    # First pass: compute new PT headers assuming data_offset offsets relative to old_hdr_sz,
    # then adjust all SPU data_offsets once we know the exact new_hdr_sz (16-byte aligned).
    new_pt_blobs_pass1: Dict[int, bytes] = {}
    pt_start_cursor = slot_tbl_off + slot_count * 4
    cursor = pt_start_cursor
    for idx in range(slot_count):
        if idx not in slot_records:
            continue
        ver, recs = slot_records[idx]
        recs_copy = list(recs)
        if idx == slot_index:
            _set_or_update_pt_tag(recs_copy, 0x82, n_ch, min_bytes=1)
            _set_or_update_pt_tag(recs_copy, 0x84, sample_rate, min_bytes=2)
            _set_or_update_pt_tag(recs_copy, 0x85, encoded_total_samples, min_bytes=2)
            if looped:
                ls_frame_smp = (1 if lead_in_zero_block else 0) * 28 + (loop_start_sample // 28) * 28
                ll_smp = max(28, encoded_total_samples - ls_frame_smp - 1)
                _set_or_update_pt_tag(recs_copy, 0x86, ls_frame_smp, min_bytes=1)
                _set_or_update_pt_tag(recs_copy, 0x87, ll_smp, min_bytes=2)
            else:
                _remove_pt_tag(recs_copy, 0x86)
                _remove_pt_tag(recs_copy, 0x87)
            _set_or_update_pt_tag(recs_copy, 0x88, old_hdr_sz + new_rel_to_pay_start, min_bytes=4)
            if n_ch == 2:
                _set_or_update_pt_tag(recs_copy, 0x89, old_hdr_sz + new_rel_to_pay_start + new_ch_bytes, min_bytes=4)
            else:
                _remove_pt_tag(recs_copy, 0x89)
        blob = _serialize_pt_tlv_records(ver, recs_copy)
        new_pt_blobs_pass1[idx] = blob
        cursor += len(blob)

    # Preserve any non-SPU data embedded inside [pt_end .. old_hdr_sz) (e.g. codec 0x0A EA-XA blocks)
    old_pt_end = bo + old_hdr_sz
    # Find end of original PT headers
    last_orig_pt_end = pt_start_cursor
    for idx in range(slot_count):
        if rel_offs[idx] != 0:
            pt_off = slot_tbl_off + idx * 4 + rel_offs[idx]
            _, p_end, _, _ = _parse_pt_tlv_records(abkc_data, pt_off)
            if p_end > last_orig_pt_end:
                last_orig_pt_end = p_end
    embedded_hdr_tail = abkc_data[last_orig_pt_end:old_pt_end]

    # Align new_hdr_sz to 16 bytes
    raw_new_hdr_end = cursor + len(embedded_hdr_tail)
    pad_hdr = (16 - ((raw_new_hdr_end - bo) % 16)) % 16
    new_hdr_sz = (raw_new_hdr_end - bo) + pad_hdr
    hdr_delta = new_hdr_sz - old_hdr_sz

    # Second pass: finalize PT blobs with shifted data_offsets (0x88, 0x89)
    new_rel_offs = [0] * slot_count
    pt_section = bytearray()
    for idx in range(slot_count):
        if idx not in slot_records:
            continue
        ver, recs = slot_records[idx]
        recs_final = list(recs)
        if idx == slot_index:
            _set_or_update_pt_tag(recs_final, 0x82, n_ch, min_bytes=1)
            _set_or_update_pt_tag(recs_final, 0x84, sample_rate, min_bytes=2)
            _set_or_update_pt_tag(recs_final, 0x85, encoded_total_samples, min_bytes=2)
            if looped:
                ls_frame_smp = (1 if lead_in_zero_block else 0) * 28 + (loop_start_sample // 28) * 28
                ll_smp = max(28, encoded_total_samples - ls_frame_smp - 1)
                _set_or_update_pt_tag(recs_final, 0x86, ls_frame_smp, min_bytes=1)
                _set_or_update_pt_tag(recs_final, 0x87, ll_smp, min_bytes=2)
            else:
                _remove_pt_tag(recs_final, 0x86)
                _remove_pt_tag(recs_final, 0x87)
            _set_or_update_pt_tag(recs_final, 0x88, new_hdr_sz + new_rel_to_pay_start, min_bytes=4)
            if n_ch == 2:
                _set_or_update_pt_tag(recs_final, 0x89, new_hdr_sz + new_rel_to_pay_start + new_ch_bytes, min_bytes=4)
            else:
                _remove_pt_tag(recs_final, 0x89)
        else:
            # Untouched slot: adjust data_offset if it lives in SPU payload or shifted by payload_delta
            for tag in (0x88, 0x89):
                ov = _get_pt_tag_int(recs_final, tag)
                if ov is not None and ov >= old_hdr_sz:
                    nv = ov + hdr_delta
                    if splice_old_off is not None and ov > splice_old_off:
                        nv += payload_delta
                    _set_or_update_pt_tag(recs_final, tag, nv, min_bytes=4)

        pt_off = pt_start_cursor + len(pt_section)
        new_rel_offs[idx] = pt_off - (slot_tbl_off + idx * 4)
        pt_section.extend(_serialize_pt_tlv_records(ver, recs_final))

    # Reassemble ABKC: [0..bo) + BNKl + padded payload to 128-byte boundary + relocated t1/t2/t3 tail
    out = bytearray(abkc_data[:bo])
    out.extend(
        struct.pack("<4sHHIII", b"BNKl", bnkl_ver, slot_count, new_hdr_sz, len(new_pay_bytes), unk10)
    )
    out.extend(struct.pack(f"<{slot_count}I", *new_rel_offs))
    out.extend(pt_section)
    out.extend(embedded_hdr_tail)
    out.extend(b"\x00" * pad_hdr)
    out.extend(new_pay_bytes)

    # Align to 128 bytes before t1 (matching all 301 SLUS-21351 ABKC banks)
    pad_t1 = (128 - (len(out) % 128)) % 128
    out.extend(b"\x00" * pad_t1)

    old_t1, old_t2, old_t3 = struct.unpack_from("<3I", abkc_data, 0x30)
    new_t1 = len(out)
    tail_delta = new_t1 - old_t1
    new_t2 = (old_t2 + tail_delta) if old_t2 else 0
    new_t3 = (old_t3 + tail_delta) if old_t3 else 0

    tail_bytes = bytearray(abkc_data[old_t1:])
    if old_t3 and old_t3 >= old_t1 and (old_t3 - old_t1 + 4) <= len(tail_bytes):
        t3_rel = old_t3 - old_t1
        cnt3 = struct.unpack_from("<I", tail_bytes, t3_rel)[0]
        for i in range(cnt3):
            rec_off = t3_rel + 4 + i * 12
            if rec_off + 12 > len(tail_bytes):
                break
            str_off = struct.unpack_from("<I", tail_bytes, rec_off + 4)[0]
            if str_off >= old_t3:
                struct.pack_into("<I", tail_bytes, rec_off + 4, str_off + tail_delta)

    out.extend(tail_bytes)
    new_total_size = len(out)
    new_bnkl_chunk_size = new_t1 - bo
    struct.pack_into("<II", out, 0x14, new_total_size, bo)
    struct.pack_into("<II", out, 0x20, bo, new_bnkl_chunk_size)
    struct.pack_into("<3I", out, 0x30, new_t1, new_t2, new_t3)

    patched = bytes(out)
    verified = audio_inspect.parse_abkc(patched, path=path_label)
    if verified.active_samples != bank_info.active_samples:
        raise RuntimeError("ABKC active sample count changed unexpectedly after patch")
    diff["byte_size"] = {"before": len(abkc_data), "after": len(patched)}

    return AbkcPatchResult(
        diff=diff,
        patched_bytes=patched,
        verified_bank=verified.to_dict(include_samples=True),
        snr_db=snrs,
    )


# ---------------------------------------------------------------------------
# 100% Original Procedural CC0-1.0 Synthesizer (synth-cc0)
# ---------------------------------------------------------------------------


def _synth_ui_click(sr: int = 32000) -> List[int]:
    """Crisp GT7/FH6-inspired glass-carbon UI focus click (45 ms, mono)."""
    n = int(round(sr * 0.045))
    out: List[int] = []
    for i in range(n):
        t = i / float(sr)
        env = math.exp(-t * 115.0) * (1.0 - math.exp(-t * 2500.0))
        body = math.sin(2.0 * math.pi * 1850.0 * t) * 0.62 + math.sin(2.0 * math.pi * 3700.0 * t) * 0.28
        snap = math.sin(2.0 * math.pi * 5400.0 * t) * math.exp(-t * 260.0) * 0.30
        out.append(_clamp_i16((body + snap) * env * 22000.0))
    return out


def _synth_ui_confirm(sr: int = 32000) -> List[int]:
    """Modern ascending two-stage racing UI confirm chime (180 ms, mono)."""
    n = int(round(sr * 0.180))
    out: List[int] = []
    t_split = 0.042
    for i in range(n):
        t = i / float(sr)
        # Note 1: A5 (880 Hz), Note 2: E6 (1318.5 Hz) + shimmer octave
        e1 = math.exp(-t * 36.0) * (1.0 - math.exp(-t * 900.0))
        s1 = math.sin(2.0 * math.pi * 880.0 * t) * 0.55 + math.sin(2.0 * math.pi * 1760.0 * t) * 0.20
        s2 = 0.0
        if t >= t_split:
            dt = t - t_split
            e2 = math.exp(-dt * 24.0) * (1.0 - math.exp(-dt * 900.0))
            s2 = (
                math.sin(2.0 * math.pi * 1318.51 * dt) * 0.65
                + math.sin(2.0 * math.pi * 2637.02 * dt) * 0.22
                + math.sin(2.0 * math.pi * 1975.53 * dt) * 0.14
            ) * e2
        out.append(_clamp_i16((s1 * e1 + s2) * 19000.0))
    return out


def _synth_ui_back(sr: int = 32000) -> List[int]:
    """Soft descending UI cancel/back tone (140 ms, mono)."""
    n = int(round(sr * 0.140))
    phase = 0.0
    out: List[int] = []
    for i in range(n):
        t = i / float(sr)
        freq = 920.0 * math.exp(-t * 7.5) + 260.0
        phase += 2.0 * math.pi * freq / float(sr)
        env = math.exp(-t * 28.0) * (1.0 - math.exp(-t * 700.0))
        sig = math.sin(phase) * 0.78 + math.sin(phase * 1.5) * 0.22
        out.append(_clamp_i16(sig * env * 19000.0))
    return out


def _lcg_noise(seed: int, count: int) -> List[float]:
    """Deterministic CC0 white noise generator in [-1.0, 1.0]."""
    state = seed & 0xFFFFFFFF
    out: List[float] = []
    for _ in range(count):
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        out.append((state / 2147483648.0) - 1.0)
    return out


def _synth_turbo_bov_flutter(sr: int = 24000) -> List[int]:
    """Turbo blow-off valve compressor-surge flutter (520 ms, mono)."""
    n = int(round(sr * 0.520))
    noise = _lcg_noise(0x20260400, n)
    lp1 = 0.0
    lp2 = 0.0
    hp_lp = 0.0
    whistle_phase = 0.0
    out: List[int] = []
    for i in range(n):
        t = i / float(sr)
        # Flutter LFO slows from ~24 Hz to ~13 Hz as boost bleeds down
        flutter_freq = 24.0 - 11.0 * (t / 0.520)
        lfo = 0.55 + 0.45 * math.sin(2.0 * math.pi * flutter_freq * t)
        env = math.exp(-t * 5.2) * (1.0 - math.exp(-t * 180.0))
        # 2-pole smoothed air rush (band-shaped for warm boost bleed & high SPU-ADPCM fidelity)
        lp1 = lp1 * 0.84 + noise[i] * 0.16
        lp2 = lp2 * 0.84 + lp1 * 0.16
        hp_lp = hp_lp * 0.96 + lp2 * 0.04
        air = lp2 - hp_lp
        # Spool-down turbo whistle sweep 2.8 kHz -> 1.05 kHz
        w_freq = 1050.0 + 1750.0 * math.exp(-t * 4.5)
        whistle_phase += 2.0 * math.pi * w_freq / float(sr)
        whistle = math.sin(whistle_phase) * 0.48 + math.sin(whistle_phase * 2.0) * 0.14
        sig = (air * 1.65 + whistle) * lfo * env
        out.append(_clamp_i16(sig * 24000.0))
    return out


def _synth_gearshift_transient(sr: int = 24000) -> List[int]:
    """Crisp sequential/DCT gearshift mechanical transient + exhaust bark (160 ms, mono)."""
    n = int(round(sr * 0.160))
    noise = _lcg_noise(0x517F2026, n)
    lp_bark = 0.0
    out: List[int] = []
    for i in range(n):
        t = i / float(sr)
        # Mechanical dog-ring click at t=0 + secondary engagement snap at t=38ms
        thump = math.sin(2.0 * math.pi * 105.0 * t) * math.exp(-t * 45.0) * 0.70
        ring1 = (math.sin(2.0 * math.pi * 1850.0 * t) + 0.5 * math.sin(2.0 * math.pi * 3420.0 * t)) * math.exp(
            -t * 110.0
        ) * 0.42
        ring2 = 0.0
        if t >= 0.038:
            dt = t - 0.038
            ring2 = math.sin(2.0 * math.pi * 1420.0 * dt) * math.exp(-dt * 95.0) * 0.35
        lp_bark = lp_bark * 0.80 + noise[i] * 0.20
        bark = lp_bark * math.exp(-t * 28.0) * 0.65
        out.append(_clamp_i16((thump + ring1 + ring2 + bark) * 23000.0))
    return out


def _synth_straight_cut_whine_loop(sr: int = 32000) -> List[int]:
    """Seamless loopable straight-cut dog-box transmission harmonic cue (~560 ms, multiple of 28 samples)."""
    # Choose sample count as exact multiple of 28 (SPU-ADPCM block size) and integer cycles for zero-seam loop
    n = 17920  # 640 * 28 = 0.560 s at 32000 Hz
    cycles_fund = 620  # 620 / 0.560 = 1107.14 Hz gear-mesh fundamental
    cycles_mod = 14    # 25 Hz subtle shaft-order modulation
    out: List[int] = []
    for i in range(n):
        u = i / float(n)
        mod = 1.0 + 0.10 * math.sin(2.0 * math.pi * cycles_mod * u)
        sig = (
            math.sin(2.0 * math.pi * cycles_fund * u) * 0.58
            + math.sin(2.0 * math.pi * (cycles_fund * 2) * u) * 0.26
            + math.sin(2.0 * math.pi * (cycles_fund * 3) * u) * 0.12
            + math.sin(2.0 * math.pi * (cycles_fund - cycles_mod) * u) * 0.08
        ) * mod
        out.append(_clamp_i16(sig * 17000.0))
    return out


CC0_SYNTH_RECIPES: Dict[str, Dict[str, Any]] = {
    "ui_click": {
        "filename": "ui_click.wav",
        "sample_rate": 32000,
        "looped": False,
        "generator": _synth_ui_click,
        "target_bank": "SOUND\\GLOBAL\\FE_COMMON_MB.ABK",
        "target_slot": 1,
        "description": "Modern GT7/FH6-inspired glass-carbon UI focus click",
    },
    "ui_confirm": {
        "filename": "ui_confirm.wav",
        "sample_rate": 32000,
        "looped": False,
        "generator": _synth_ui_confirm,
        "target_bank": "SOUND\\GLOBAL\\FE_COMMON_MB.ABK",
        "target_slot": 2,
        "description": "Crisp ascending two-stage racing UI confirm chime",
    },
    "ui_back": {
        "filename": "ui_back.wav",
        "sample_rate": 32000,
        "looped": False,
        "generator": _synth_ui_back,
        "target_bank": "SOUND\\GLOBAL\\FE_COMMON_MB.ABK",
        "target_slot": 3,
        "description": "Soft descending UI cancel/back tone",
    },
    "turbo_bov_flutter": {
        "filename": "turbo_bov_flutter.wav",
        "sample_rate": 24000,
        "looped": False,
        "generator": _synth_turbo_bov_flutter,
        "target_bank": "SOUND\\TURBO\\TURBO_MED_2_MB.ABK",
        "target_slot": 2,
        "description": "Turbocharger compressor-surge blow-off flutter",
    },
    "gearshift_transient": {
        "filename": "gearshift_transient.wav",
        "sample_rate": 24000,
        "looped": False,
        "generator": _synth_gearshift_transient,
        "target_bank": "SOUND\\SHIFTING\\GEAR_SML_BASE.ABK",
        "target_slot": 1,
        "description": "Sequential dog-ring gearshift snap & exhaust bark transient",
    },
    "straight_cut_whine_loop": {
        "filename": "straight_cut_whine_loop.wav",
        "sample_rate": 32000,
        "looped": True,
        "generator": _synth_straight_cut_whine_loop,
        "target_bank": "SOUND\\ENGINE\\CAR_WHINE_00.ABK",
        "target_slot": 1,
        "description": "Seamless looped straight-cut racing transmission harmonic cue",
    },
}


def generate_cc0_wavs(out_dir: Path, *, use_numpy: Optional[bool] = None) -> Dict[str, Any]:
    """Generate all 6 procedural CC0-1.0 WAV assets and measure SPU-ADPCM encode/decode SNR."""
    wav_dir = out_dir / "cc0_wav"
    wav_dir.mkdir(parents=True, exist_ok=True)
    generated: List[Dict[str, Any]] = []

    for cue_id, spec in CC0_SYNTH_RECIPES.items():
        sr = int(spec["sample_rate"])
        looped = bool(spec["looped"])
        pcm = spec["generator"](sr)
        wav_path = wav_dir / spec["filename"]
        write_wav_pcm16(wav_path, sr, [pcm])

        adpcm = encode_spu_adpcm(pcm, looped=looped, lead_in_zero_block=False, use_numpy=use_numpy)
        dec = decode_spu_adpcm(adpcm)
        snr = compute_snr_db(pcm, dec)

        generated.append(
            {
                "id": cue_id,
                "license": "CC0-1.0",
                "wav_path": str(wav_path),
                "sample_rate": sr,
                "num_samples": len(pcm),
                "duration_sec": round(len(pcm) / float(sr), 4),
                "looped": looped,
                "spu_adpcm_bytes": len(adpcm),
                "spu_adpcm_snr_db": round(snr, 2),
                "target_bank": spec["target_bank"],
                "target_slot": spec["target_slot"],
                "description": spec["description"],
            }
        )

    summary = {
        "license": "CC0-1.0",
        "out_dir": str(wav_dir),
        "asset_count": len(generated),
        "assets": generated,
    }
    (out_dir / "cc0_synth_report.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def run_iso_roundtrip_demo(iso_path: Path, out_dir: Path, *, use_numpy: Optional[bool] = None) -> Dict[str, Any]:
    """Extract sample ABKC & Gnsu files read-only from ISO into `out_dir`, patch, and verify via audio_inspect."""
    arc = PS2NFSMWArchive(iso_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_dir = out_dir / "cc0_wav"

    # 1. ABKC round-trip on SOUND\GLOBAL\FE_COMMON_MB.ABK (UI click/confirm/back) + CAR_WHINE_00.ABK (loop)
    abkc_reports: List[Dict[str, Any]] = []
    abkc_jobs = [
        ("SOUND\\GLOBAL\\FE_COMMON_MB.ABK", "FE_COMMON_MB_patched.ABK", [(1, "ui_click", False), (2, "ui_confirm", False), (3, "ui_back", False)]),
        ("SOUND\\ENGINE\\CAR_WHINE_00.ABK", "CAR_WHINE_00_patched.ABK", [(1, "straight_cut_whine_loop", True)]),
    ]
    for vpath, out_name, edits in abkc_jobs:
        entry = arc.find(vpath)
        buf = arc.read_entry(entry)
        slot_diffs = []
        for slot_idx, cue_id, is_looped in edits:
            wav_path = wav_dir / CC0_SYNTH_RECIPES[cue_id]["filename"]
            sr, _ch, chans_pcm = read_wav_pcm16(wav_path)
            res = patch_abkc_slot(
                buf,
                slot_index=slot_idx,
                channels_pcm=chans_pcm,
                sample_rate=sr,
                looped=is_looped,
                path_label=vpath,
                use_numpy=use_numpy,
            )
            assert res.patched_bytes is not None
            buf = res.patched_bytes
            slot_diffs.append(res.diff)

        patched_path = out_dir / out_name
        patched_path.write_bytes(buf)
        inspected = audio_inspect.inspect_standalone_file(patched_path)
        abkc_reports.append(
            {
                "virtual_path": vpath,
                "patched_file": str(patched_path),
                "slot_diffs": slot_diffs,
                "verified_bank": inspected["abkc_banks"][0].to_dict(include_samples=False),
            }
        )

    # 2. Gnsu round-trip on hero cars
    gnsu_reports: List[Dict[str, Any]] = []
    for preset_name, cfg in HERO_CAR_GNSU_PRESETS.items():
        vpath = cfg["virtual_path"]
        entry = arc.find(vpath)
        raw_gin = arc.read_entry(entry)
        res = patch_gnsu_bytes(raw_gin, preset=preset_name, path_label=vpath)
        assert res.patched_bytes is not None
        out_gin = out_dir / f"{preset_name}_patched.GIN"
        out_gin.write_bytes(res.patched_bytes)
        inspected = audio_inspect.inspect_standalone_file(out_gin)
        gnsu_reports.append(
            {
                "preset": preset_name,
                "virtual_path": vpath,
                "patched_file": str(out_gin),
                "diff": res.diff,
                "verified_synth": inspected["gnsu_synths"][0].to_dict(),
            }
        )

    report = {"iso": str(iso_path), "abkc_roundtrips": abkc_reports, "gnsu_roundtrips": gnsu_reports}
    (out_dir / "iso_roundtrip_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_source_bytes(in_file: Optional[Path], iso: Optional[Path], virtual_path: Optional[str]) -> Tuple[bytes, str]:
    if in_file is not None:
        return in_file.read_bytes(), str(in_file)
    if virtual_path is not None:
        iso_path = resolve_iso_path(iso)
        if iso_path is None:
            raise FileNotFoundError("ISO path not found (pass --iso or set NFSMW_ISO)")
        arc = PS2NFSMWArchive(iso_path)
        entry = arc.find(virtual_path)
        return arc.read_entry(entry), virtual_path
    raise ValueError("Provide either --in <file> or --virtual-path <ZDIR path> (with optional --iso)")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clean-room PS2 NFSMW audio encoder, Gnsu/ABKC patcher, and CC0 procedural synthesizer."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # spu-encode
    p_enc = sub.add_parser("spu-encode", help="Encode 16-bit mono WAV -> PS2 SPU-ADPCM 16-byte blocks")
    p_enc.add_argument("--in-wav", required=True, type=Path, help="Input PCM WAV path")
    p_enc.add_argument("--out", required=True, type=Path, help="Output raw SPU-ADPCM binary path")
    p_enc.add_argument("--loop", action="store_true", help="Emit SPU loop flags (0x06/0x02/0x03)")
    p_enc.add_argument("--loop-start", type=int, default=0, help="Loop start sample index (default: 0)")
    p_enc.add_argument("--lead-in-zero", action="store_true", help="Prepend 16-byte silent hardware lead-in block")
    p_enc.add_argument("--no-numpy", action="store_true", help="Force pure-stdlib path without NumPy")
    p_enc.add_argument("--json", action="store_true", help="Print JSON summary")

    # spu-decode
    p_dec = sub.add_parser("spu-decode", help="Decode PS2 SPU-ADPCM 16-byte blocks -> 16-bit mono WAV")
    p_dec.add_argument("--in-bin", required=True, type=Path, help="Input SPU-ADPCM binary path")
    p_dec.add_argument("--out-wav", required=True, type=Path, help="Output PCM WAV path")
    p_dec.add_argument("--sample-rate", type=int, default=32000, help="Output WAV sample rate (default: 32000)")
    p_dec.add_argument("--json", action="store_true", help="Print JSON summary")

    # patch-gnsu
    p_gin = sub.add_parser("patch-gnsu", help="Tune a Gnsu v20 engine synth (.GIN)")
    p_gin.add_argument("--in", dest="in_file", type=Path, default=None, help="Input standalone .GIN file")
    p_gin.add_argument("--iso", type=Path, default=None, help="Read-only PS2 ISO path")
    p_gin.add_argument("--virtual-path", type=str, default=None, help="ZDIR virtual path (e.g. SOUND\\ENGINE\\GIN_TVR_CERBERA.GIN)")
    p_gin.add_argument("--preset", choices=sorted(HERO_CAR_GNSU_PRESETS), default=None, help="Hero car tuning preset")
    p_gin.add_argument("--min-rpm", type=float, default=None, help="Override minimum RPM")
    p_gin.add_argument("--max-rpm", type=float, default=None, help="Override maximum RPM")
    p_gin.add_argument("--sample-rate", type=int, default=None, help="Explicit output sample rate (Hz)")
    p_gin.add_argument("--rate-scale", type=float, default=None, help="Scale sample rate for exhaust presence/pitch")
    p_gin.add_argument("--curve-gamma", type=float, default=1.0, help="RPM-table grain response curve exponent")
    p_gin.add_argument("--out", type=Path, default=None, help="Output patched .GIN path (inside build/)")
    p_gin.add_argument("--diff-out", type=Path, default=None, help="Optional JSON diff output path")
    p_gin.add_argument("--dry-run", action="store_true", help="Compute diff only without writing patched binary")
    p_gin.add_argument("--json", action="store_true", help="Print JSON report")

    # patch-abkc
    p_abk = sub.add_parser("patch-abkc", help="Replace an SPU-ADPCM sample slot inside an ABKC/BNKl bank")
    p_abk.add_argument("--in", dest="in_file", type=Path, default=None, help="Input standalone .ABK file")
    p_abk.add_argument("--iso", type=Path, default=None, help="Read-only PS2 ISO path")
    p_abk.add_argument("--virtual-path", type=str, default=None, help="ZDIR virtual path (e.g. SOUND\\GLOBAL\\FE_COMMON_MB.ABK)")
    p_abk.add_argument("--slot", required=True, type=int, help="Target BNKl slot index")
    p_abk.add_argument("--wav", required=True, type=Path, help="Replacement PCM WAV file")
    p_abk.add_argument("--loop", action="store_true", help="Mark replacement slot as looped")
    p_abk.add_argument("--loop-start", type=int, default=0, help="Loop start sample index (default: 0)")
    p_abk.add_argument("--no-lead-in", action="store_true", help="Omit 16-byte silent hardware lead-in block")
    p_abk.add_argument("--no-numpy", action="store_true", help="Force pure-stdlib encoder path")
    p_abk.add_argument("--out", type=Path, default=None, help="Output patched .ABK path (inside build/)")
    p_abk.add_argument("--diff-out", type=Path, default=None, help="Optional JSON diff output path")
    p_abk.add_argument("--dry-run", action="store_true", help="Compute diff & SNR without writing patched binary")
    p_abk.add_argument("--json", action="store_true", help="Print JSON report")

    # synth-cc0
    p_syn = sub.add_parser("synth-cc0", help="Generate 100%% original CC0 procedural WAV assets")
    p_syn.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "build" / "audio_wave3",
        help="Output base directory (default: build/audio_wave3)",
    )
    p_syn.add_argument("--iso", type=Path, default=None, help="Optional read-only ISO for round-trip ABKC/Gnsu verification")
    p_syn.add_argument("--verify-iso", action="store_true", help="Run read-only ISO extract + patch + verify into out-dir")
    p_syn.add_argument("--no-numpy", action="store_true", help="Force pure-stdlib encoder path")
    p_syn.add_argument("--json", action="store_true", help="Print JSON report")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "spu-encode":
        sr, ch, chans = read_wav_pcm16(args.in_wav)
        if ch != 1:
            # Mix stereo down to mono for standalone single-channel SPU block output
            mono = [_clamp_i16((chans[0][i] + chans[1][i]) * 0.5) for i in range(len(chans[0]))]
        else:
            mono = chans[0]
        enc = encode_spu_adpcm(
            mono,
            looped=args.loop,
            loop_start_sample=args.loop_start,
            lead_in_zero_block=args.lead_in_zero,
            use_numpy=False if args.no_numpy else None,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(enc)
        dec = decode_spu_adpcm(enc[16:] if args.lead_in_zero else enc)
        snr = round(compute_snr_db(mono, dec), 2)
        payload = {
            "in_wav": str(args.in_wav),
            "out_bin": str(args.out),
            "sample_rate": sr,
            "input_samples": len(mono),
            "encoded_bytes": len(enc),
            "looped": args.loop,
            "snr_db": snr,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Encoded {args.in_wav} -> {args.out} ({len(enc)} B, {snr:.2f} dB SNR)")
        return 0

    if args.command == "spu-decode":
        raw = args.in_bin.read_bytes()
        res = decode_spu_adpcm_detailed(raw)
        write_wav_pcm16(args.out_wav, args.sample_rate, [res.samples])
        payload = {
            "in_bin": str(args.in_bin),
            "out_wav": str(args.out_wav),
            "sample_rate": args.sample_rate,
            "decoded_samples": len(res.samples),
            "block_count": res.block_count,
            "loop_start_block": res.loop_start_block,
            "loop_end_block": res.loop_end_block,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Decoded {args.in_bin} -> {args.out_wav} ({len(res.samples)} samples, {res.block_count} blocks)")
        return 0

    if args.command == "patch-gnsu":
        vpath = args.virtual_path
        if args.in_file is None and vpath is None and args.preset is not None:
            vpath = HERO_CAR_GNSU_PRESETS[args.preset]["virtual_path"]
        raw, label = _load_source_bytes(args.in_file, args.iso, vpath)
        res = patch_gnsu_bytes(
            raw,
            min_rpm=args.min_rpm,
            max_rpm=args.max_rpm,
            sample_rate=args.sample_rate,
            rate_scale=args.rate_scale,
            curve_gamma=args.curve_gamma,
            preset=args.preset,
            dry_run=args.dry_run,
            path_label=label,
        )
        if not args.dry_run and args.out is not None and res.patched_bytes is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(res.patched_bytes)
        if args.diff_out is not None:
            args.diff_out.parent.mkdir(parents=True, exist_ok=True)
            args.diff_out.write_text(json.dumps(res.diff, indent=2) + "\n", encoding="utf-8")
        out_obj = {"diff": res.diff, "verified": res.verified_info}
        if args.json:
            print(json.dumps(out_obj, indent=2))
        else:
            d = res.diff
            print(
                f"Gnsu Patch ({d['path']}): RPM {d['min_rpm']['before']}..{d['max_rpm']['before']} -> "
                f"{d['min_rpm']['after']}..{d['max_rpm']['after']}, sr {d['sample_rate']['before']} -> "
                f"{d['sample_rate']['after']} Hz, gamma={d['curve_gamma']}"
            )
        return 0

    if args.command == "patch-abkc":
        raw, label = _load_source_bytes(args.in_file, args.iso, args.virtual_path)
        sr, _ch, chans = read_wav_pcm16(args.wav)
        res = patch_abkc_slot(
            raw,
            slot_index=args.slot,
            channels_pcm=chans,
            sample_rate=sr,
            looped=args.loop,
            loop_start_sample=args.loop_start,
            lead_in_zero_block=not args.no_lead_in,
            dry_run=args.dry_run,
            path_label=label,
            use_numpy=False if args.no_numpy else None,
        )
        if not args.dry_run and args.out is not None and res.patched_bytes is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(res.patched_bytes)
        if args.diff_out is not None:
            args.diff_out.parent.mkdir(parents=True, exist_ok=True)
            args.diff_out.write_text(json.dumps(res.diff, indent=2) + "\n", encoding="utf-8")
        out_obj = {"diff": res.diff, "verified_bank": res.verified_bank}
        if args.json:
            print(json.dumps(out_obj, indent=2))
        else:
            d = res.diff
            print(
                f"ABKC Patch ({d['path']} slot {d['slot_index']}): sr {d['sample_rate']['before']} -> "
                f"{d['sample_rate']['after']} Hz, smp {d['num_samples']['before']} -> "
                f"{d['num_samples']['after']}, SNR={d['snr_db']} dB"
            )
        return 0

    if args.command == "synth-cc0":
        use_np = False if args.no_numpy else None
        synth_rep = generate_cc0_wavs(args.out_dir, use_numpy=use_np)
        iso_rep = None
        if args.verify_iso or args.iso is not None:
            iso_path = resolve_iso_path(args.iso)
            if iso_path is None:
                raise FileNotFoundError("ISO requested via --verify-iso/--iso but not found")
            iso_rep = run_iso_roundtrip_demo(iso_path, args.out_dir, use_numpy=use_np)
        payload = {"synth": synth_rep, "iso_roundtrip": iso_rep}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Generated {synth_rep['asset_count']} CC0-1.0 WAVs in {synth_rep['out_dir']}")
            for a in synth_rep["assets"]:
                print(
                    f"  - {a['id']:24s} {a['sample_rate']:5d} Hz  {a['num_samples']:6d} smp  "
                    f"SPU-ADPCM={a['spu_adpcm_bytes']:6d} B  SNR={a['spu_adpcm_snr_db']:5.2f} dB"
                )
            if iso_rep is not None:
                print(
                    f"Verified ISO round-trips: {len(iso_rep['abkc_roundtrips'])} ABKC banks, "
                    f"{len(iso_rep['gnsu_roundtrips'])} Gnsu hero synths"
                )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
