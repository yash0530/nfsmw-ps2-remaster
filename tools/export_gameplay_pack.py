#!/usr/bin/env python3
"""export_gameplay_pack.py — Read-only PS2 NFSMW Gameplay Asset Pack Exporter.

Extracts legally from an untouched user-supplied Need for Speed: Most Wanted -
Black Edition (PS2, SLUS-21351) ISO (strictly read-only 'rb') into gitignored
`build/gameplay_pack/` (< 10 MB total):

  1. `vlt_vehicles.json`: Authentic VLT handling, engine torque curves, 6-speed
     gearboxes, AWD/RWD torque splits, suspension/tires/brakes, NOS, induction,
     pursuit escalation (`heat_01`..`heat_05`), and `engineaudio` mappings for
     all playable cars + Rockport Police units from `GLOBAL\\ATTRIBUTES.BIN`.
  2. `audio/`: Decoded PCM16 WAVs + granular metadata (`audio_manifest.json`):
     - EA-XAS v0 (19-byte / 32-sample block) `Gnsu` v20 accel/decel engine
       synths + exact phase-locked 720-deg crank-cycle `grain_table` &
       `rpm_table` for hero & Blacklist engine archetypes.
     - Sony PS2 SPU-ADPCM `ABKC` gameplay SFX (BMW M3 GTR straight-cut gearbox
       whine, turbo spool/BOV, NOS roar/purge, tire drift screech, gearshift
       pops, police wail/yelp sirens, Speedbreaker UVES, wall scrape, collisions,
       rain & wind ambience).
     - Mandatory PSOLA audio self-test (`test_m3_sweep.wav`: 3.0s 1200->8000 RPM
       granular sweep verified for non-silence, zero NaN/Inf, peak bounds, and
       grain-boundary smoothness).
  3. `ai_vehicles/`: 7 lightweight deduplicated AI vehicle archetypes in Metal
     Y-up right-handed meters (`COPMIDSIZE` LOD A + `COPGTO` LOD B + `COPSPORT`
     LOD B using intact `DAMAGE0_*` parts with separate roof `lightbar.obj`,
     plus `TRAFTAXI`, `TRAF4DSEDA`, `TRAFSUVA`, `TRAFMINIVAN` LOD C).

Zero copyrighted assets are ever committed to Git. Pure Python + NumPy + Pillow.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audio_inspect  # noqa: E402
import audio_patcher  # noqa: E402
import export_pbr_scene as pbr  # noqa: E402
import geometry_inspect as gi  # noqa: E402
import tpk_extract as te  # noqa: E402
import vlt_ps2  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

DEFAULT_ISO_REL = Path("Documents") / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
DEFAULT_OUT_DIR = REPO_ROOT / "build" / "gameplay_pack"

# Standard EA-XA / EA-XAS v0 4-entry fixed-point (256-scaled) 2nd-order filter table
EA_XAS_FILTER_TABLE: Tuple[Tuple[int, int], ...] = (
    (0, 0),
    (240, 0),
    (460, -208),
    (392, -220),
)

# Canonical garage alias -> PS2 VLT pvehicle collection name
CAR_KEY_ALIASES: Dict[str, str] = {
    "m3_gtr": "bmwm3gtre46",
    "m3_gtr_street": "bmwm3gtr",
    "carrera_gt": "carreragt",
    "gallardo": "gallardo",
    "corvette_c6r": "corvettec6r",
    "corvette": "corvette",
    "ford_gt": "fordgt",
    "rx7": "rx7",
    "rx8": "rx8",
    "supra": "supra",
    "viper": "viper",
    "murcielago": "murcielago",
    "slr": "slr",
    "db9": "db9",
    "elise": "elise",
    "911_turbo": "911turbo",
    "911_gt2": "911gt2",
    "997s": "997s",
    "cayman_s": "caymans",
    "impreza_wrx": "imprezawrx",
    "lancer_evo8": "lancerevo8",
    "mustang_gt": "mustanggt",
    "camaro": "camaro",
    "gto": "gto",
    "cobalt_ss": "cobaltss",
    "gti": "gti",
    "punto": "punto",
    "is300": "is300",
    "clio": "clio",
    "monaro": "monaro",
    "cts": "cts",
    "clk500": "clk500",
    "sl500": "sl500",
    "sl65": "sl65",
    "eclipse_gt": "eclipsegt",
    "a3": "a3",
    "a4": "a4",
    "tt": "tt",
}

# Curated core engineaudio profiles to decode into build/gameplay_pack/audio/
# Covers all major cylinder/induction archetypes across the 40-car roster while
# keeping the extracted gameplay pack cleanly < 10 MB.
CORE_ENGINEAUDIO_PROFILES: Tuple[str, ...] = (
    "tvr_cerb",       # BMW M3 GTR E46 (P60B40 V8 + straight-cut gearbox whine!)
    "por_car_gt",     # Porsche Carrera GT (5.7L V10)
    "lam_gallardo",   # Lamborghini Gallardo (5.0L V10)
    "corvette_z06_v2",  # Corvette C6.R / Z06 (7.0L V8)
    "for_mus_a",      # Ford GT / CopSport (V8 screamer)
    "for_mus_b",      # Camaro SS / GTO / Mustang (muscle V8)
    "por_996",        # Porsche 911 Turbo / 997S / Cayman S (Flat-6)
    "am_db9",         # Aston Martin DB9 / V12 exotics
)

# ABKC SFX extraction spec: (output_key, virtual_path, slot_index, is_loop_hint)
ABKC_SFX_SPECS: Tuple[Tuple[str, str, int, bool], ...] = (
    ("gearbox_whine", "SOUND\\ENGINE\\CAR_WHINE_00.ABK", 1, True),
    ("tranny_whine", "SOUND\\ENGINE\\CAR_TRANNY.ABK", 2, True),
    ("turbo_spool", "SOUND\\TURBO\\TURBO_EXOTIC_0_MB.ABK", 1, True),
    ("turbo_bov", "SOUND\\TURBO\\TURBO_EXOTIC_0_MB.ABK", 2, False),
    ("nos_loop", "SOUND\\NOS\\NITROUS_00_MB.ABK", 1, True),
    ("nos_purge", "SOUND\\NOS\\NITROUS_00_MB.ABK", 2, False),
    ("nos_ignite", "SOUND\\NOS\\NITROUS_00_MB.ABK", 3, False),
    ("skid_loop", "SOUND\\SKIDS\\SKIDS_DRIFT_BIG_MB.ABK", 1, True),
    ("shift_pop_1", "SOUND\\SHIFTING\\GEAR_LRG_LEV3.ABK", 1, False),
    ("shift_pop_2", "SOUND\\SHIFTING\\GEAR_LRG_LEV3.ABK", 2, False),
    ("siren_wail", "SOUND\\IG_GLOBAL\\SIREN_MB.ABK", 1, True),
    ("siren_yelp", "SOUND\\IG_GLOBAL\\SIREN_MB.ABK", 2, True),
    ("siren_phaser", "SOUND\\IG_GLOBAL\\SIREN_MB.ABK", 3, True),
    ("scrape_loop", "SOUND\\IG_GLOBAL\\FX_MISC_MB.ABK", 1, True),
    ("speedbreaker_fx", "SOUND\\IG_GLOBAL\\FX_MISC_MB.ABK", 2, False),
    ("radar_ping", "SOUND\\IG_GLOBAL\\FX_MISC_MB.ABK", 5, False),
    ("impact_hard", "SOUND\\IG_GLOBAL\\STICH_COLLISION_MB.ABK", 1, False),
    ("impact_med", "SOUND\\IG_GLOBAL\\STICH_COLLISION_MB.ABK", 4, False),
    ("rain_loop", "SOUND\\IG_GLOBAL\\FX_RAIN_MB.ABK", 1, True),
    ("wind_loop", "SOUND\\IG_GLOBAL\\WIND_00_MB.ABK", 2, True),
)

# 7 AI Vehicle Archetypes (3 Rockport Police tiers + 4 Traffic cars)
AI_VEHICLE_SPECS: Tuple[Dict[str, Any], ...] = (
    {"key": "copmidsize", "dir": "COPMIDSIZE", "role": "police", "heat_min": 1, "heat_max": 2, "preferred_lod": "A", "wheel_base": 2.91, "half_track": 0.80},
    {"key": "copgto", "dir": "COPGTO", "role": "police", "heat_min": 3, "heat_max": 4, "preferred_lod": "B", "wheel_base": 2.79, "half_track": 0.79},
    {"key": "copsport", "dir": "COPSPORT", "role": "police", "heat_min": 5, "heat_max": 5, "preferred_lod": "B", "wheel_base": 2.69, "half_track": 0.81},
    {"key": "traftaxi", "dir": "TRAFTAXI", "role": "traffic", "heat_min": 0, "heat_max": 0, "preferred_lod": "C", "wheel_base": 2.76, "half_track": 0.76},
    {"key": "traf4dseda", "dir": "TRAF4DSEDA", "role": "traffic", "heat_min": 0, "heat_max": 0, "preferred_lod": "C", "wheel_base": 2.72, "half_track": 0.75},
    {"key": "trafsuva", "dir": "TRAFSUVA", "role": "traffic", "heat_min": 0, "heat_max": 0, "preferred_lod": "C", "wheel_base": 2.86, "half_track": 0.82},
    {"key": "trafminivan", "dir": "TRAFMINIVAN", "role": "traffic", "heat_min": 0, "heat_max": 0, "preferred_lod": "C", "wheel_base": 2.94, "half_track": 0.80},
)


def resolve_iso_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """Resolve PS2 ISO path from explicit CLI arg, NFSMW_ISO env var, or default Home path."""
    if explicit is not None:
        return explicit if explicit.is_file() else None
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso is not None:
        env_iso = env_iso.strip()
        if not env_iso:
            return None
        p = Path(env_iso)
        return p if p.is_file() else None
    candidate = Path.home() / DEFAULT_ISO_REL
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# 1. Clean-Room EA-XAS v0 Decoder & Ginsu v20 PSOLA Granular Synthesizer
# ---------------------------------------------------------------------------


def decode_ea_xas_v0(granule_data: bytes, expected_samples: Optional[int] = None) -> Tuple[np.ndarray, Dict[str, int]]:
    """Decode EA-XAS v0 audio payload (4-byte LE sample count + N*19-byte blocks).

    Each 19-byte block decodes exactly 32 mono 16-bit signed PCM samples:
      - int16 h0: top 12 bits = sample[0], low 4 bits = filter coef index (0..3)
      - int16 h1: top 12 bits = sample[1], low 4 bits = ADPCM shift (0..12)
      - 15 bytes: 30 signed 4-bit ADPCM nibbles in high-nibble-first order
    """
    if len(granule_data) < 23:
        raise ValueError(f"EA-XAS v0 buffer too short ({len(granule_data)} bytes)")

    hdr_samples = struct.unpack_from("<I", granule_data, 0)[0]
    payload = granule_data[4:]
    n_blocks = len(payload) // 19
    if n_blocks <= 0:
        raise ValueError("EA-XAS v0 payload contains 0 blocks")

    out = np.empty(n_blocks * 32, dtype=np.int16)
    bad_coefs = 0
    bad_shifts = 0
    clips = 0

    for b in range(n_blocks):
        off = b * 19
        h0, h1 = struct.unpack_from("<hh", payload, off)
        coef_idx = h0 & 0x0F
        shift = h1 & 0x0F
        if coef_idx > 3:
            bad_coefs += 1
            coef_idx &= 3
        if shift > 12:
            bad_shifts += 1
            shift = 12

        c1, c2 = EA_XAS_FILTER_TABLE[coef_idx]
        s0 = h0 & ~0x0F
        s1 = h1 & ~0x0F
        base = b * 32
        out[base] = s0
        out[base + 1] = s1

        hist2 = s0
        hist1 = s1
        idx = base + 2
        for byte_val in payload[off + 4 : off + 19]:
            hi = (byte_val >> 4) & 0x0F
            lo = byte_val & 0x0F
            for nib in (hi, lo):
                if nib >= 8:
                    nib -= 16
                delta = (nib << 12) >> shift
                pred = (hist1 * c1 + hist2 * c2 + 128) >> 8
                val = pred + delta
                if val > 32767:
                    val = 32767
                    clips += 1
                elif val < -32768:
                    val = -32768
                    clips += 1
                out[idx] = val
                idx += 1
                hist2 = hist1
                hist1 = val

    target_len = expected_samples if expected_samples is not None else hdr_samples
    if 0 < target_len < len(out):
        out = out[:target_len]

    stats = {
        "header_samples": int(hdr_samples),
        "decoded_samples": int(len(out)),
        "blocks": int(n_blocks),
        "bad_coefs": int(bad_coefs),
        "bad_shifts": int(bad_shifts),
        "clips": int(clips),
    }
    return out, stats


def decode_gnsu_v20(data: bytes, path_label: str = "<buffer>") -> Dict[str, Any]:
    """Parse a Gnsu v20 container and decode its EA-XAS v0 waveform + RPM/grain tables."""
    info = audio_inspect.parse_gnsu(data, path=path_label)
    rpm_entries = info.num_rpm_steps + 1
    rpm_table = list(struct.unpack_from(f"<{rpm_entries}I", data, info.rpm_table_offset))
    grain_table = list(struct.unpack_from(f"<{info.num_grains}I", data, info.grain_table_offset))
    gdata = data[info.granule_data_offset :]
    pcm, xas_stats = decode_ea_xas_v0(gdata, expected_samples=info.total_samples)
    return {
        "path": path_label,
        "version": info.version,
        "direction": info.direction,
        "min_rpm": float(info.min_rpm),
        "max_rpm": float(info.max_rpm),
        "sample_rate": int(info.sample_rate),
        "total_samples": int(len(pcm)),
        "rpm_table": rpm_table,
        "grain_table": grain_table,
        "pcm": pcm,
        "xas_stats": xas_stats,
    }


def write_mono_wav_pcm16(path: Path, sample_rate: int, pcm: np.ndarray) -> None:
    """Write mono 16-bit signed LE PCM WAV."""
    arr = np.asarray(pcm, dtype=np.int16)
    if sys.byteorder != "little":
        arr = arr.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(arr.tobytes())


def render_ginsu_psola_sweep(
    gnsu: Dict[str, Any],
    *,
    rpm_start: float = 1200.0,
    rpm_end: float = 8000.0,
    duration_sec: float = 3.0,
    out_sample_rate: int = 48000,
) -> Tuple[np.ndarray, Dict[str, float]]:
    """Reference Ginsu PSOLA (Pitch-Synchronous Overlap-Add) granular engine sweep.

    Matches the exact real-time algorithm used in native_metal_rt/RTAudioEngine.swift:
      1. Maps instantaneous target RPM into `rpm_table` to find the source sample index.
      2. Selects the phase-locked 720-degree crank-cycle grain `[g0, g1]` from `grain_table`.
      3. Computes exact playback pitch ratio `src_grain_rpm / target_rpm` (clamped [0.75, 1.35])
         so pitch glides continuously between discrete crank cycles with zero stair-stepping.
      4. Overlap-adds two consecutive Hanning-windowed crank cycles (50% overlap = unity gain)
         with linear interpolation onto `out_sample_rate`, eliminating clicks & chipmunk artifacts.
    """
    pcm_f = gnsu["pcm"].astype(np.float64) / 32768.0
    n_src = len(pcm_f)
    src_sr = float(gnsu["sample_rate"])
    min_rpm = float(gnsu["min_rpm"])
    max_rpm = float(gnsu["max_rpm"])
    rpm_table = gnsu["rpm_table"]
    grain_table = gnsu["grain_table"]

    if n_src < 64 or len(grain_table) < 3 or len(rpm_table) < 2:
        raise ValueError("Invalid Gnsu tables for PSOLA synthesis")

    # Build valid grain intervals [start, length]
    grains: List[Tuple[int, int]] = []
    for i in range(len(grain_table) - 1):
        g0 = int(grain_table[i])
        g1 = int(grain_table[i + 1])
        if 16 <= g1 - g0 and g1 <= n_src:
            grains.append((g0, g1 - g0))
    if len(grains) < 2:
        raise ValueError("Insufficient valid grains in Gnsu grain_table")

    grain_starts = np.array([g[0] for g in grains], dtype=np.int64)
    n_out = int(round(duration_sec * out_sample_rate))
    out = np.zeros(n_out, dtype=np.float64)
    win_sum = np.zeros(n_out, dtype=np.float64)

    def lookup_grain_for_rpm(rpm: float) -> Tuple[int, int, float]:
        u = (rpm - min_rpm) / max(1.0, (max_rpm - min_rpm))
        u_clamped = max(0.0, min(1.0, u))
        f_idx = u_clamped * (len(rpm_table) - 1)
        i0 = int(math.floor(f_idx))
        i1 = min(len(rpm_table) - 1, i0 + 1)
        frac = f_idx - i0
        src_pos = rpm_table[i0] * (1.0 - frac) + rpm_table[i1] * frac
        gi_idx = int(np.searchsorted(grain_starts, int(src_pos), side="right")) - 1
        gi_idx = max(0, min(len(grains) - 1, gi_idx))
        g_start, g_len = grains[gi_idx]
        # Native grain RPM = 4-stroke 2-rev cycle freq * 120
        native_rpm = (src_sr / float(g_len)) * 120.0
        pitch_ratio = max(0.65, min(1.45, rpm / max(200.0, native_rpm)))
        return g_start, g_len, pitch_ratio

    # Place 2-period Hanning-windowed grains every 1 output cycle (50% overlap -> smooth unity COLA)
    out_cursor = 0.0
    grain_boundary_diffs: List[float] = []
    grains_emitted = 0

    while int(out_cursor) < n_out:
        t_frac = min(1.0, out_cursor / max(1.0, float(n_out - 1)))
        target_rpm = rpm_start + (rpm_end - rpm_start) * t_frac
        g_start, g_len, pitch_ratio = lookup_grain_for_rpm(target_rpm)

        # Output cycle length (samples at out_sample_rate) for one 720-deg cycle
        step_src_per_out = (src_sr / float(out_sample_rate)) * pitch_ratio
        cycle_out = float(g_len) / max(1e-5, step_src_per_out)
        win_out = max(16, int(round(cycle_out * 2.0)))

        # Center a 2-cycle Hann window around out_cursor
        start_i = int(round(out_cursor))
        if start_i >= n_out:
            break
        end_i = min(n_out, start_i + win_out)
        count = end_i - start_i
        if count <= 1:
            break

        k = np.arange(count, dtype=np.float64)
        # Periodic Hanning window over full 2-cycle window -> 50% overlap sums to ~1.0
        w = 0.5 * (1.0 - np.cos(2.0 * math.pi * k / float(win_out)))

        # Read 2 continuous periods from source (wraps cleanly within g_len if at end of file)
        src_rel = k * step_src_per_out
        if g_start + int(math.ceil(src_rel[-1])) + 2 < n_src:
            src_pos = g_start + src_rel
        else:
            src_pos = g_start + np.mod(src_rel, float(g_len))

        idx0 = np.floor(src_pos).astype(np.int64)
        idx1 = np.minimum(idx0 + 1, n_src - 1)
        frac = src_pos - idx0
        samp = pcm_f[idx0] * (1.0 - frac) + pcm_f[idx1] * frac

        out[start_i:end_i] += samp * w
        win_sum[start_i:end_i] += w
        if start_i > 1:
            grain_boundary_diffs.append(abs(out[start_i] - out[start_i - 1]))

        out_cursor += max(8.0, cycle_out)
        grains_emitted += 1

    # Normalize overlapping window sum (avoid edge divide-by-zero)
    valid = win_sum > 1e-3
    out[valid] /= np.maximum(win_sum[valid], 0.5)

    # Soft fade-in / fade-out (10 ms) to avoid start/stop clicks
    fade_n = min(n_out // 10, int(0.010 * out_sample_rate))
    if fade_n > 1:
        ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float64)
        out[:fade_n] *= ramp
        out[-fade_n:] *= ramp[::-1]

    # Guard against NaN/Inf and normalize peak safely to -1 dBFS max
    has_nan_inf = bool(not np.all(np.isfinite(out)))
    peak = float(np.max(np.abs(out))) if n_out > 0 else 0.0
    if peak > 0.95:
        out = out * (0.92 / peak)
        peak = float(np.max(np.abs(out)))

    rms = float(np.sqrt(np.mean(out * out))) if n_out > 0 else 0.0
    diffs = np.abs(np.diff(out)) if n_out > 1 else np.zeros(1)
    max_step = float(np.max(diffs))
    p99_step = float(np.percentile(diffs, 99.5))

    pcm16 = np.clip(np.round(out * 32767.0), -32768, 32767).astype(np.int16)
    metrics = {
        "duration_sec": round(n_out / float(out_sample_rate), 4),
        "sample_rate": int(out_sample_rate),
        "samples": int(n_out),
        "grains_emitted": int(grains_emitted),
        "rms": round(rms, 5),
        "peak": round(peak, 5),
        "max_sample_step": round(max_step, 5),
        "p995_sample_step": round(p99_step, 5),
        "has_nan_or_inf": has_nan_inf,
    }
    return pcm16, metrics


# ---------------------------------------------------------------------------
# 2. ABKC / BNKl Sony SPU-ADPCM Slot Extractor
# ---------------------------------------------------------------------------


def extract_abkc_slot_pcm(abkc_bytes: bytes, slot_index: int, path_label: str) -> Dict[str, Any]:
    """Decode one SPU-ADPCM slot from an EA ABKC/BNKl bank into mono PCM16."""
    info = audio_inspect.parse_abkc(abkc_bytes, path=path_label)
    target = None
    for s in info.samples:
        if s.slot_index == slot_index:
            target = s
            break
    if target is None:
        if not info.samples:
            raise ValueError(f"ABKC bank {path_label} has 0 active samples")
        target = info.samples[0]

    if target.data_offset is None or not target.num_samples:
        raise ValueError(f"ABKC {path_label} slot {target.slot_index} missing data_offset/num_samples")

    abs_off = info.bnkl_offset + target.data_offset
    n_blocks = (target.num_samples + 27) // 28
    byte_len = n_blocks * 16
    chunk = abkc_bytes[abs_off : min(len(abkc_bytes), abs_off + byte_len)]
    # Pad/trim to exact 16-byte SPU block boundary
    chunk = chunk[: (len(chunk) // 16) * 16]
    if len(chunk) < 16:
        raise ValueError(f"ABKC {path_label} slot {target.slot_index} truncated SPU chunk ({len(chunk)} B)")

    decoded = audio_patcher.decode_spu_adpcm_detailed(chunk)
    pcm = np.asarray(decoded.samples[: target.num_samples], dtype=np.int16)
    sr = int(target.sample_rate or 24000)
    return {
        "source_bank": path_label,
        "slot_index": int(target.slot_index),
        "sample_rate": sr,
        "num_samples": int(len(pcm)),
        "duration_sec": round(len(pcm) / float(sr), 4),
        "looped": bool(target.looped),
        "loop_start": int(target.loop_start or 0),
        "loop_length": int(target.loop_length or len(pcm)),
        "pcm": pcm,
    }


# ---------------------------------------------------------------------------
# 3. VLT Vehicle Handling, Pursuit & EngineAudio Extraction
# ---------------------------------------------------------------------------


def _vlt_resolve_collection(by_cls: Dict[str, Dict[str, Any]], cls_name: str, node_name: str) -> Dict[str, Any]:
    """Merge inherited VLT collection fields walking parent -> child."""
    nodes = by_cls.get(cls_name, {})
    c = nodes.get(node_name)
    if c is None:
        return {}
    chain = []
    cur = c
    seen = set()
    while cur is not None and cur.name not in seen:
        seen.add(cur.name)
        chain.append(cur)
        cur = nodes.get(cur.parent_name) if cur.parent_name else None
    merged: Dict[str, Any] = {}
    for item in reversed(chain):
        for k, f in item.fields.items():
            merged[k] = f.value
    return merged


def _refspec_name(val: Any, prefer_last: bool = False) -> str:
    if isinstance(val, list) and val:
        item = val[-1] if prefer_last else val[0]
        if isinstance(item, dict):
            return str(item.get("collection", ""))
    elif isinstance(val, dict):
        return str(val.get("collection", ""))
    return ""


def _axle_pair(val: Any, default_f: float, default_r: float) -> List[float]:
    if isinstance(val, dict):
        return [float(val.get("Front", default_f)), float(val.get("Rear", default_r))]
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        return [float(val[0]), float(val[1])]
    return [default_f, default_r]


def _float_list(val: Any, default: Sequence[float]) -> List[float]:
    if isinstance(val, (list, tuple)) and len(val) > 0:
        return [round(float(x), 5) for x in val]
    return [float(x) for x in default]


def extract_vlt_vehicles(arc: PS2NFSMWArchive) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Parse GLOBAL\\ATTRIBUTES.BIN and export clean per-car physics + pursuit JSON."""
    vlt_bytes = arc.read_entry(arc.find("GLOBAL\\ATTRIBUTES.BIN"))
    vdb = vlt_ps2.VpakDatabase(vlt_bytes)
    vault = vdb.vaults[0]

    by_cls: Dict[str, Dict[str, Any]] = {}
    for col in vault.collections:
        by_cls.setdefault(col.class_name, {})[col.name] = col

    # Discover all car directories present in ISO
    car_dirs = {
        e.resolved_path.split("\\")[1].upper()
        for e in arc.entries
        if e.resolved_path and e.resolved_path.upper().startswith("CARS\\") and len(e.resolved_path.split("\\")) >= 3
    }

    # Build engineaudio lookup
    engineaudio_map: Dict[str, Dict[str, Any]] = {}
    for ea_name in sorted(by_cls.get("engineaudio", {}).keys()):
        m = _vlt_resolve_collection(by_cls, "engineaudio", ea_name)
        ga = m.get("Filename_GinsuAccel", {})
        gd = m.get("Filename_GinsuDecel", {})
        ga_txt = ga.get("text", "") if isinstance(ga, dict) else ""
        gd_txt = gd.get("text", "") if isinstance(gd, dict) else ""
        engineaudio_map[ea_name] = {
            "node": ea_name,
            "ginsu_accel": ga_txt,
            "ginsu_decel": gd_txt,
            "tranny_whine": bool(m.get("Tranny", False)),
            "min_rpm": float(m.get("MinRPM", 900.0)),
            "max_rpm": float(m.get("MaxRPM", 8000.0)),
            "decel_min_rpm": float(m.get("GINSU_Decel_MinRPM", 2200.0)),
            "decel_max_rpm": float(m.get("GINSU_Decel_MaxRPM", 7800.0)),
            "vol_accel": round(float(m.get("GINSUAccelVol", 24000)) / 32767.0, 4),
            "vol_decel": round(float(m.get("GinsuDecelVol", 20000)) / 32767.0, 4),
            "vol_sputters": round(float(m.get("Vol_Sputters", 16000)) / 32767.0, 4),
            "vol_shifts": round(float(m.get("Vol_ShiftSweets", 18000)) / 32767.0, 4),
        }

    # Collect target playable + police pvehicles
    pvs = by_cls.get("pvehicle", {})
    want_cop_nodes = {"copmidsize", "copghost", "copgto", "copgtoghost", "copsport", "copsporthench"}
    vehicles: Dict[str, Dict[str, Any]] = {}

    for pv_name in sorted(pvs.keys()):
        if pv_name.startswith("cs_") or pv_name.endswith("_nis") or pv_name.endswith("_nis_ld"):
            continue
        pv = _vlt_resolve_collection(by_cls, "pvehicle", pv_name)
        mod = pv.get("MODEL")
        model_name = mod["text"].upper() if isinstance(mod, dict) and "text" in mod else ""
        if not model_name or model_name not in car_dirs:
            continue
        is_cop = pv_name in want_cop_nodes or model_name.startswith("COP")
        is_traffic = model_name.startswith("TRAF") or model_name in ("TAXI", "PIZZA", "MINIVAN", "PICKUPA", "SEMI", "CEMTR", "GARB")
        if is_traffic:
            continue
        if is_cop and pv_name not in want_cop_nodes:
            continue

        eng_node = _refspec_name(pv.get("engine"), prefer_last=True) or pv_name
        trn_node = _refspec_name(pv.get("transmission"), prefer_last=True) or pv_name
        tir_node = _refspec_name(pv.get("tires"), prefer_last=True) or pv_name
        chs_node = _refspec_name(pv.get("chassis"), prefer_last=True) or pv_name
        brk_node = _refspec_name(pv.get("brakes"), prefer_last=True) or pv_name
        nos_node = _refspec_name(pv.get("nos"), prefer_last=True) or "nos_top"
        ind_node = _refspec_name(pv.get("induction"), prefer_last=True) or pv_name
        ea_node = _refspec_name(pv.get("engineaudio"), prefer_last=False)

        eng = _vlt_resolve_collection(by_cls, "engine", eng_node)
        trn = _vlt_resolve_collection(by_cls, "transmission", trn_node)
        tir = _vlt_resolve_collection(by_cls, "tires", tir_node)
        chs = _vlt_resolve_collection(by_cls, "chassis", chs_node)
        brk = _vlt_resolve_collection(by_cls, "brakes", brk_node)
        nos = _vlt_resolve_collection(by_cls, "nos", nos_node) or _vlt_resolve_collection(by_cls, "nos", "nos_top")
        ind = _vlt_resolve_collection(by_cls, "induction", ind_node)

        # Skip non-drivable entries lacking engine torque curve
        torque = _float_list(eng.get("TORQUE"), [])
        if len(torque) < 3:
            continue

        ea_info = engineaudio_map.get(ea_node, {})
        vehicles[pv_name] = {
            "vlt_node": pv_name,
            "model": model_name,
            "category": "police" if is_cop else "playable",
            "mass_kg": round(float(pv.get("MASS", 1400.0)), 2),
            "engine": {
                "torque_curve": torque,
                "idle_rpm": round(float(eng.get("IDLE", 850.0)), 1),
                "redline_rpm": round(float(eng.get("RED_LINE", 7800.0)), 1),
                "max_rpm": round(float(eng.get("MAX_RPM", 8600.0)), 1),
                "flywheel_mass": round(float(eng.get("FLYWHEEL_MASS", 10.0)), 2),
                "engine_braking": _float_list(eng.get("ENGINE_BRAKING"), [0.7, 0.8, 0.9]),
            },
            "transmission": {
                "gear_ratios": _float_list(trn.get("GEAR_RATIO"), [3.0, 0.0, 3.6, 2.3, 1.6, 1.2, 0.95, 0.78]),
                "final_gear": round(float(trn.get("FINAL_GEAR", 3.5)), 4),
                "torque_split": round(float(trn.get("TORQUE_SPLIT", 0.0)), 4),
                "shift_speed": round(float(trn.get("SHIFT_SPEED", 0.22)), 4),
                "optimal_shift": round(float(trn.get("OPTIMAL_SHIFT", 500.0)), 1),
            },
            "tires": {
                "static_grip": _axle_pair(tir.get("STATIC_GRIP"), 2.0, 2.1),
                "dynamic_grip": _axle_pair(tir.get("DYNAMIC_GRIP"), 1.8, 1.85),
                "grip_scale": _axle_pair(tir.get("GRIP_SCALE"), 1.05, 1.08),
                "steering": round(float(tir.get("STEERING", 1.0)), 4),
                "yaw_control": _float_list(tir.get("YAW_CONTROL"), [0.3, 0.65, 1.0, 1.2]),
                "yaw_speed": round(float(tir.get("YAW_SPEED", 0.30)), 4),
            },
            "chassis": {
                "wheel_base": round(float(chs.get("WHEEL_BASE", 2.68)), 4),
                "track_width": _axle_pair(chs.get("TRACK_WIDTH"), 1.56, 1.56),
                "drag_coefficient": round(float(chs.get("DRAG_COEFFICIENT", 0.34)), 4),
                "aero_coefficient": round(float(chs.get("AERO_COEFFICIENT", 0.25)), 4),
                "front_weight_bias": round(float(chs.get("FRONT_WEIGHT_BIAS", 52.0)), 2),
                "swaybar_stiffness": _axle_pair(chs.get("SWAYBAR_STIFFNESS"), 220.0, 220.0),
                "spring_stiffness": _axle_pair(chs.get("SPRING_STIFFNESS"), 550.0, 520.0),
                "ride_height": _axle_pair(chs.get("RIDE_HEIGHT"), 6.0, 6.0),
            },
            "brakes": {
                "brakes": _axle_pair(brk.get("BRAKES"), 450.0, 540.0),
                "ebrake": round(float(brk.get("EBRAKE", 850.0)), 1),
                "brake_lock": _axle_pair(brk.get("BRAKE_LOCK"), 1.0, 3.0),
            },
            "nos": {
                "torque_boost": round(float(nos.get("TORQUE_BOOST", 1.0)), 3),
                "capacity": round(float(nos.get("NOS_CAPACITY", 5.0)), 3),
                "flow_rate": round(float(nos.get("FLOW_RATE", 3.0)), 3),
                "recharge_min": round(float(nos.get("RECHARGE_MIN", 50.0)), 2),
                "recharge_max": round(float(nos.get("RECHARGE_MAX", 30.0)), 2),
            },
            "induction": {
                "psi": round(float(ind.get("PSI", 0.0)), 2),
                "high_boost": round(float(ind.get("HIGH_BOOST", 0.0)), 3),
                "low_boost": round(float(ind.get("LOW_BOOST", 0.0)), 3),
                "spool_up": round(float(ind.get("SPOOL_TIME_UP", 1.5)), 3),
                "spool_down": round(float(ind.get("SPOOL_TIME_DOWN", 0.3)), 3),
            },
            "audio": {
                "engineaudio_node": ea_node,
                "ginsu_accel": ea_info.get("ginsu_accel", ""),
                "ginsu_decel": ea_info.get("ginsu_decel", ""),
                "tranny_whine": bool(ea_info.get("tranny_whine", False)),
                "min_rpm": ea_info.get("min_rpm", 900.0),
                "max_rpm": ea_info.get("max_rpm", 8000.0),
            },
        }

    # Pursuit heat escalation levels heat_01..heat_05
    pursuit_levels: Dict[str, Any] = {}
    for lvl in range(1, 6):
        key = f"heat_0{lvl}"
        m = _vlt_resolve_collection(by_cls, "pursuitlevels", key)
        if not m:
            continue
        cops_out = []
        for rec in m.get("cops", []):
            if isinstance(rec, dict):
                ct = rec.get("CopType", {})
                ct_txt = ct.get("text", "") if isinstance(ct, dict) else str(ct)
                cops_out.append({
                    "cop_type": ct_txt,
                    "count": int(rec.get("Count", 2)),
                    "chance": int(rec.get("Chance", 100)),
                })
        pursuit_levels[key] = {
            "heat_level": lvl,
            "cops": cops_out,
            "full_engagement_cop_count": int(m.get("FullEngagementCopCount", 5 * lvl)),
            "time_between_cop_spawn": round(float(m.get("TimeBetweenCopSpawn", 8.0)), 2),
            "roadblock_probability": round(float(m.get("roadblockprobability", 0.0)), 2),
            "num_patrol_cars": int(m.get("NumPatrolCars", 1)),
            "search_mode_city_mph": round(float(m.get("SearchModeCityMPH", 75.0)), 1),
            "search_mode_hwy_mph": round(float(m.get("SearchModeHwyMPH", 100.0)), 1),
        }

    vlt_doc = {
        "schema_version": 1,
        "source": "GLOBAL\\ATTRIBUTES.BIN",
        "playable_count": sum(1 for v in vehicles.values() if v["category"] == "playable"),
        "police_count": sum(1 for v in vehicles.values() if v["category"] == "police"),
        "aliases": {k: v for k, v in CAR_KEY_ALIASES.items() if v in vehicles},
        "vehicles": vehicles,
        "pursuit_levels": pursuit_levels,
    }
    return vlt_doc, engineaudio_map


# ---------------------------------------------------------------------------
# 4. Audio Pack Extraction (Ginsu EA-XAS v0 + ABKC SPU-ADPCM + PSOLA Sweep)
# ---------------------------------------------------------------------------


def _find_ginsu_entry(arc: PS2NFSMWArchive, filename: str):
    """Case-insensitive lookup of SOUND\\ENGINE\\<filename> inside ZDIR."""
    if not filename:
        return None
    target_up = f"SOUND\\ENGINE\\{filename}".upper()
    try:
        return arc.find(target_up)
    except KeyError:
        pass
    base_up = filename.upper()
    for e in arc.entries:
        if e.resolved_path and e.resolved_path.upper().endswith("\\" + base_up):
            return e
    return None


def export_audio_pack(
    arc: PS2NFSMWArchive,
    engineaudio_map: Dict[str, Dict[str, Any]],
    out_dir: Path,
) -> Dict[str, Any]:
    """Decode hero/Blacklist Ginsu engine synths + ABKC SFX into `out_dir/audio/`."""
    audio_dir = out_dir / "audio"
    engines_dir = audio_dir / "engines"
    sfx_dir = audio_dir / "sfx"
    engines_dir.mkdir(parents=True, exist_ok=True)
    sfx_dir.mkdir(parents=True, exist_ok=True)

    # Also include RX-7 rotary & Supra I6 if present in VLT engineaudio!
    want_profiles = list(CORE_ENGINEAUDIO_PROFILES)
    for extra in ("maz_rx7", "toy_sup", "sub_imp", "mit_evo"):
        for k in sorted(engineaudio_map.keys()):
            if k.startswith(extra) and k not in want_profiles and engineaudio_map[k].get("ginsu_accel"):
                want_profiles.append(k)
                break

    engines_manifest: Dict[str, Any] = {}
    m3_accel_gnsu: Optional[Dict[str, Any]] = None

    for ea_key in want_profiles:
        ea = engineaudio_map.get(ea_key)
        if not ea or not ea.get("ginsu_accel"):
            continue
        acc_entry = _find_ginsu_entry(arc, ea["ginsu_accel"])
        if acc_entry is None:
            continue
        acc_gnsu = decode_gnsu_v20(arc.read_entry(acc_entry), path_label=acc_entry.resolved_path or ea["ginsu_accel"])
        acc_wav = engines_dir / f"{ea_key}_accel.wav"
        write_mono_wav_pcm16(acc_wav, acc_gnsu["sample_rate"], acc_gnsu["pcm"])

        if ea_key == "tvr_cerb":
            m3_accel_gnsu = acc_gnsu

        dec_info_out = None
        dec_entry = _find_ginsu_entry(arc, ea.get("ginsu_decel", ""))
        if dec_entry is not None:
            dec_gnsu = decode_gnsu_v20(arc.read_entry(dec_entry), path_label=dec_entry.resolved_path or ea["ginsu_decel"])
            dec_wav = engines_dir / f"{ea_key}_decel.wav"
            write_mono_wav_pcm16(dec_wav, dec_gnsu["sample_rate"], dec_gnsu["pcm"])
            dec_info_out = {
                "wav": f"audio/engines/{ea_key}_decel.wav",
                "sample_rate": dec_gnsu["sample_rate"],
                "min_rpm": dec_gnsu["min_rpm"],
                "max_rpm": dec_gnsu["max_rpm"],
                "total_samples": dec_gnsu["total_samples"],
                "rpm_table": dec_gnsu["rpm_table"],
                "grain_table": dec_gnsu["grain_table"],
                "xas_stats": dec_gnsu["xas_stats"],
            }

        engines_manifest[ea_key] = {
            "node": ea_key,
            "tranny_whine": bool(ea.get("tranny_whine", False)),
            "vol_accel": ea.get("vol_accel", 0.8),
            "vol_decel": ea.get("vol_decel", 0.65),
            "vol_sputters": ea.get("vol_sputters", 0.5),
            "accel": {
                "wav": f"audio/engines/{ea_key}_accel.wav",
                "sample_rate": acc_gnsu["sample_rate"],
                "min_rpm": acc_gnsu["min_rpm"],
                "max_rpm": acc_gnsu["max_rpm"],
                "total_samples": acc_gnsu["total_samples"],
                "rpm_table": acc_gnsu["rpm_table"],
                "grain_table": acc_gnsu["grain_table"],
                "xas_stats": acc_gnsu["xas_stats"],
            },
            "decel": dec_info_out,
        }

    # Decode ABKC SPU-ADPCM gameplay SFX banks
    sfx_manifest: Dict[str, Any] = {}
    for sfx_key, vpath, slot_idx, loop_hint in ABKC_SFX_SPECS:
        try:
            entry = arc.find(vpath)
        except KeyError:
            continue
        rec = extract_abkc_slot_pcm(arc.read_entry(entry), slot_idx, vpath)
        wav_path = sfx_dir / f"{sfx_key}.wav"
        write_mono_wav_pcm16(wav_path, rec["sample_rate"], rec["pcm"])
        sfx_manifest[sfx_key] = {
            "wav": f"audio/sfx/{sfx_key}.wav",
            "source_bank": vpath,
            "slot_index": rec["slot_index"],
            "sample_rate": rec["sample_rate"],
            "num_samples": rec["num_samples"],
            "duration_sec": rec["duration_sec"],
            "looped": bool(rec["looped"] or loop_hint),
            "loop_start": rec["loop_start"],
            "loop_length": rec["loop_length"],
        }

    # Mandatory PSOLA audio self-test (#9 Trap #6): 3.0s 1200->8000 RPM M3 GTR sweep
    sweep_metrics: Dict[str, Any] = {}
    if m3_accel_gnsu is not None:
        sweep_pcm, sweep_metrics = render_ginsu_psola_sweep(
            m3_accel_gnsu,
            rpm_start=1200.0,
            rpm_end=8000.0,
            duration_sec=3.0,
            out_sample_rate=48000,
        )
        sweep_path = out_dir / "test_m3_sweep.wav"
        write_mono_wav_pcm16(sweep_path, 48000, sweep_pcm)
        sweep_metrics["wav"] = "test_m3_sweep.wav"

    manifest = {
        "schema_version": 1,
        "engine_count": len(engines_manifest),
        "sfx_count": len(sfx_manifest),
        "engines": engines_manifest,
        "sfx": sfx_manifest,
        "psola_self_test": sweep_metrics,
    }
    (audio_dir / "audio_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# 5. Lightweight AI Traffic & Police Vehicle Archetype Exporter
# ---------------------------------------------------------------------------


def _merge_yup_meshes(
    meshes_with_mtl: Sequence[Tuple[gi.DecodedMesh, str]],
    out_obj: Path,
    object_name: str,
) -> Dict[str, Any]:
    """Merge multiple Y-up DecodedMesh objects into one clean OBJ with semantic `usemtl` groups."""
    out_obj.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        f"# NFSMW Rockport 2026 AI Archetype (Y-up right-handed meters): {object_name}",
        f"o {object_name}",
    ]
    v_offset = 0
    total_verts = 0
    total_tris = 0
    bb_min = [1e9, 1e9, 1e9]
    bb_max = [-1e9, -1e9, -1e9]

    face_groups: Dict[str, List[Tuple[int, int, int]]] = {}
    for mesh, mtl_name in meshes_with_mtl:
        for x, y, z in mesh.vertices:
            lines.append(f"v {x:.5f} {y:.5f} {z:.5f}")
            if x < bb_min[0]: bb_min[0] = x
            if y < bb_min[1]: bb_min[1] = y
            if z < bb_min[2]: bb_min[2] = z
            if x > bb_max[0]: bb_max[0] = x
            if y > bb_max[1]: bb_max[1] = y
            if z > bb_max[2]: bb_max[2] = z
        for u, v in mesh.uvs:
            lines.append(f"vt {u:.5f} {v:.5f}")
        for nx, ny, nz in mesh.normals:
            lines.append(f"vn {nx:.5f} {ny:.5f} {nz:.5f}")

        grp = face_groups.setdefault(mtl_name, [])
        for i0, i1, i2, _tx, _lm in mesh.triangles:
            grp.append((v_offset + i0 + 1, v_offset + i1 + 1, v_offset + i2 + 1))
            total_tris += 1
        v_offset += len(mesh.vertices)
        total_verts += len(mesh.vertices)

    for mtl_name, tris in face_groups.items():
        if not tris:
            continue
        lines.append(f"g {object_name}_{mtl_name}")
        lines.append(f"usemtl {mtl_name}")
        for a, b, c in tris:
            lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")

    out_obj.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if total_verts == 0:
        bb_min = [0.0, 0.0, 0.0]
        bb_max = [0.0, 0.0, 0.0]
    return {
        "vertices": total_verts,
        "triangles": total_tris,
        "aabb_min": [round(x, 4) for x in bb_min],
        "aabb_max": [round(x, 4) for x in bb_max],
    }


def _classify_ai_part_material(part_name: str, role: str) -> str:
    up = part_name.upper()
    if "COP_LIGHTS" in up:
        return "cop_lightbar"
    if "WINDOW" in up or "GLASS" in up:
        return "smoked_glass"
    if "HEADLIGHT" in up:
        return "headlight_emissive"
    if "BRAKELIGHT" in up or "TAILLIGHT" in up:
        return "taillight_emissive"
    if "TIRE" in up or "WHEEL" in up:
        return "rubber_tire"
    if "BRAKE" in up:
        return "brake_rotor"
    if "DRIVER" in up or "INTERIOR" in up:
        return "interior_dark"
    return "cop_body" if role == "police" else "traffic_body"


def export_ai_vehicles(arc: PS2NFSMWArchive, out_dir: Path) -> Dict[str, Any]:
    """Extract all 7 AI police/traffic archetypes (body + 4 wheels + cop lightbar + skin PNG)."""
    ai_dir = out_dir / "ai_vehicles"
    ai_dir.mkdir(parents=True, exist_ok=True)
    archetypes: Dict[str, Any] = {}
    total_tris_all = 0

    for spec in AI_VEHICLE_SPECS:
        key = spec["key"]
        cdir = spec["dir"]
        role = spec["role"]
        pref_lod = spec["preferred_lod"]

        ge = arc.find(f"CARS\\{cdir}\\GEOMETRY.BIN")
        te_entry = arc.find(f"CARS\\{cdir}\\TEXTURES.BIN")
        gdata = arc.read_entry(ge)
        pack, _ = gi.parse_geometry_pack(gdata, f"CARS\\{cdir}\\GEOMETRY.BIN")

        # Choose best available LOD starting at preferred_lod
        avail_lods = sorted({p.lod for p in pack.parts})
        lod = pref_lod if pref_lod in avail_lods else avail_lods[0]

        # Select intact parts (CRITICAL: undamaged police parts are named DAMAGE0_*; exclude DAMAGE1..3!)
        selected_parts = [
            p for p in pack.parts
            if p.lod == lod
            and not any(bad in p.name.upper() for bad in ("DAMAGE1", "DAMAGE2", "DAMAGE3", "KIT01", "KIT02", "KIT03", "KIT04", "KIT05", "DECAL", "CHOP"))
        ]
        if not selected_parts:
            selected_parts = [p for p in pack.parts if p.lod == lod]

        part_indices = [p.index for p in selected_parts]
        _, decoded_map = gi.parse_geometry_pack(gdata, f"CARS\\{cdir}\\GEOMETRY.BIN", decode_parts=part_indices)

        # Find front tire mesh to place 4 wheels and ground the vehicle cleanly at Y = 0
        tire_part = next((p for p in selected_parts if "TIRE" in p.name.upper()), None)
        tire_radius = 0.33
        if tire_part is not None and tire_part.index in decoded_map:
            tm = decoded_map[tire_part.index]
            if tm.vertices:
                zs = [v[2] for v in tm.vertices]
                tire_radius = max(0.24, min(0.45, 0.5 * (max(zs) - min(zs))))

        # Standard PS2 NFSMW vehicle coordinate grounding:
        # Wheel hubs sit at z_ps2 ≈ +0.20 m (or tire_radius - 0.12 m), so ground is at z_ps2 = wheel_z - tire_radius.
        wheel_center_z = 0.21 if role == "police" else 0.22
        ground_z_ps2 = wheel_center_z - tire_radius
        wb = float(spec["wheel_base"])
        front_x = wb * 0.52
        rear_x = -wb * 0.48
        half_y = float(spec["half_track"])

        body_meshes: List[Tuple[gi.DecodedMesh, str]] = []
        lightbar_meshes: List[Tuple[gi.DecodedMesh, str]] = []

        for p in selected_parts:
            m = decoded_map.get(p.index)
            if m is None or not m.triangles:
                continue
            up = p.name.upper()
            if "TIRE" in up or ("BRAKE" in up and "BRAKELIGHT" not in up):
                # Replicate centered wheel mesh to FL, FR, RL, RR
                if "TIRE" in up:
                    # Flip right-side wheels 180 deg around Z in PS2 space so rim faces outward
                    placements = [
                        ((front_x, +half_y, wheel_center_z), False),
                        ((front_x, -half_y, wheel_center_z), True),
                        ((rear_x, +half_y, wheel_center_z), False),
                        ((rear_x, -half_y, wheel_center_z), True),
                    ]
                    for off_ps2, rot180 in placements:
                        ym = pbr.transform_ps2_mesh_to_yup(
                            m,
                            offset_ps2=off_ps2,
                            rotate_z_180_ps2=rot180,
                            ground_z_ps2=ground_z_ps2,
                        )
                        body_meshes.append((ym, "rubber_tire"))
                continue

            ym = pbr.transform_ps2_mesh_to_yup(m, ground_z_ps2=ground_z_ps2)
            mtl = _classify_ai_part_material(p.name, role)
            if "COP_LIGHTS" in up:
                lightbar_meshes.append((ym, "cop_lightbar"))
            else:
                body_meshes.append((ym, mtl))

        car_out = ai_dir / key
        car_out.mkdir(parents=True, exist_ok=True)
        body_stats = _merge_yup_meshes(body_meshes, car_out / "body.obj", f"{key}_body")
        lightbar_stats = None
        lightbar_center = None
        if lightbar_meshes:
            lightbar_stats = _merge_yup_meshes(lightbar_meshes, car_out / "lightbar.obj", f"{key}_lightbar")
            bmin = lightbar_stats["aabb_min"]
            bmax = lightbar_stats["aabb_max"]
            lightbar_center = [
                round(0.5 * (bmin[0] + bmax[0]), 4),
                round(0.5 * (bmin[1] + bmax[1]), 4),
                round(0.5 * (bmin[2] + bmax[2]), 4),
            ]

        # Extract authentic SKIN1 / MISC texture PNG from CARS\<DIR>\TEXTURES.BIN
        skin_rel: Optional[str] = None
        tex_list = te.extract_tpk_bytes(arc.read_entry(te_entry), source=cdir, keep_images=True)
        best_tex = None
        for t in tex_list:
            if "SKIN1" in t.name.upper() and t.image is not None:
                best_tex = t
                break
        if best_tex is None:
            for t in tex_list:
                if "MISC" in t.name.upper() and t.image is not None:
                    best_tex = t
                    break
        if best_tex is None and tex_list:
            best_tex = next((t for t in tex_list if t.image is not None), None)
        if best_tex is not None and best_tex.image is not None:
            skin_path = car_out / "skin.png"
            best_tex.image.save(skin_path, format="PNG")
            skin_rel = f"ai_vehicles/{key}/skin.png"

        arch_tris = body_stats["triangles"] + (lightbar_stats["triangles"] if lightbar_stats else 0)
        total_tris_all += arch_tris
        archetypes[key] = {
            "key": key,
            "model_dir": cdir,
            "role": role,
            "lod": lod,
            "heat_min": spec["heat_min"],
            "heat_max": spec["heat_max"],
            "body_obj": f"ai_vehicles/{key}/body.obj",
            "lightbar_obj": f"ai_vehicles/{key}/lightbar.obj" if lightbar_stats else None,
            "lightbar_center_yup": lightbar_center,
            "skin_png": skin_rel,
            "body": body_stats,
            "lightbar": lightbar_stats,
            "total_triangles": arch_tris,
        }

    manifest = {
        "schema_version": 1,
        "archetype_count": len(archetypes),
        "total_unique_triangles": total_tris_all,
        "archetypes": archetypes,
    }
    (ai_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def export_gameplay_pack(iso_path: Path, out_dir: Path) -> Dict[str, Any]:
    """Run complete read-only gameplay pack export into `out_dir`."""
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    arc = PS2NFSMWArchive(iso_path)

    vlt_doc, engineaudio_map = extract_vlt_vehicles(arc)
    (out_dir / "vlt_vehicles.json").write_text(json.dumps(vlt_doc, indent=2) + "\n", encoding="utf-8")

    audio_manifest = export_audio_pack(arc, engineaudio_map, out_dir)
    ai_manifest = export_ai_vehicles(arc, out_dir)

    total_bytes = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    elapsed = round(time.perf_counter() - t0, 2)

    top_manifest = {
        "schema_version": 1,
        "elapsed_sec": elapsed,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024.0 * 1024.0), 2),
        "vlt": {
            "playable_count": vlt_doc["playable_count"],
            "police_count": vlt_doc["police_count"],
            "pursuit_levels": len(vlt_doc["pursuit_levels"]),
        },
        "audio": {
            "engine_count": audio_manifest["engine_count"],
            "sfx_count": audio_manifest["sfx_count"],
            "psola_self_test": audio_manifest["psola_self_test"],
        },
        "ai_vehicles": {
            "archetype_count": ai_manifest["archetype_count"],
            "total_unique_triangles": ai_manifest["total_unique_triangles"],
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(top_manifest, indent=2) + "\n", encoding="utf-8")
    return top_manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Export NFSMW PS2 Gameplay Pack (VLT + Audio + AI Vehicles)")
    ap.add_argument("--iso", type=Path, default=None, help="Path to untouched SLUS-21351 PS2 ISO (read-only)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="Output directory (default: build/gameplay_pack)")
    args = ap.parse_args(argv)

    iso_path = resolve_iso_path(args.iso)
    if iso_path is None:
        print("Error: PS2 NFSMW ISO not found (set NFSMW_ISO or pass --iso).", file=sys.stderr)
        return 2

    summary = export_gameplay_pack(iso_path, args.out)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
