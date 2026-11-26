#!/usr/bin/env python3
"""export_music_pack.py — Read-only PS2 NFSMW Authentic EA TRAX & Score Exporter.

Legally decodes the authentic Need for Speed: Most Wanted - Black Edition
(PS2, SLUS-21351) soundtrack directly from a user-supplied untouched ISO
(strictly read-only 'rb') into gitignored `build/music_pack/`:

  - Decodes `SOUND\\PFDATA\\MW_MUSIC.MUS` + `MW_MUSIC.MPF` (`PFDx` Pathfinder v5)
    32 kHz Stereo EA-XA v2 (`adpcm_ea_r3`): `SCHl`/`GSTR` + `SCDl` big-endian
    `(nsamp, offL, offR)` + 15-byte 28-sample ADPCM frames + `0xEE` 61-byte
    big-endian PCM escape blocks.
  - Resolves 100% accurate Artist / Title / Album / Category metadata by joining
    `GLOBAL\\ATTRIBUTES.BIN` `music` class `PathEvent & 0xFFFFFF` against
    `MW_MUSIC.MPF` `sec[2]` bytecode (`words[3] & 0xFFFFFF` -> `node_id =
    words[10] & 0xFFFF` -> `sec[0]` `trk_offs[node_id] * 4` -> `stream_idx =
    u16[+0x08] >> 1`), guaranteeing zero title swaps across all 26/26 licensed
    EA TRAX (including the `ev[10..12]` permutation on *We Control*,
    *Skinnyman*, and *Decadence*) plus the 14 Paul Linford & Chris Vrenna
    interactive pursuit/race score tracks (`streams[31..44]`).
  - Parallelizes decoding + macOS built-in `afconvert -f m4af -d aac -b 128000`
    encoding across CPU cores into `build/music_pack/tracks/track_XX.m4a` and
    writes `build/music_pack/manifest.json`.

Zero copyrighted audio assets are ever committed to Git.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import struct
import subprocess
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
import vlt_ps2  # noqa: E402
from iso_inspect import SECTOR_SIZE  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

DEFAULT_ISO_REL = Path("Documents") / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
DEFAULT_OUT_DIR = REPO_ROOT / "build" / "music_pack"

MUS_VIRTUAL_PATH = "SOUND\\PFDATA\\MW_MUSIC.MUS"
MPF_VIRTUAL_PATH = "SOUND\\PFDATA\\MW_MUSIC.MPF"
ATTR_VIRTUAL_PATH = "GLOBAL\\ATTRIBUTES.BIN"

# Standard Electronic Arts EA-XA / adpcm_ea_r3 2nd-order fixed-point predictor table (256-scaled)
EA_ADPCM_C1: Tuple[int, int, int, int] = (0, 240, 460, 392)
EA_ADPCM_C2: Tuple[int, int, int, int] = (0, 0, -208, -220)

# Authentic NFSMW interactive pursuit (31..39) and race/chase score (40..44) streams
PURSUIT_SCORE_STREAM_RANGE = range(31, 40)  # 9 adaptive pursuit themes (ev[35..43])
RACE_SCORE_STREAM_RANGE = range(40, 45)     # 5 interactive chase/race score themes (ev[44..48])
STINGER_STREAM_RANGE = range(45, 53)        # 8 short Busted/Evaded/Event stingers (3.4-6.0s)


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
# 1. Clean-Room EA-XA v2 (`adpcm_ea_r3`) Stream Decoder
# ---------------------------------------------------------------------------


def decode_ea_r3_channel(
    buf: bytes | memoryview,
    start: int,
    end: int,
    nsamp: int,
    h1: int = 0,
    h2: int = 0,
) -> Tuple[np.ndarray, int, int, Dict[str, int]]:
    """Decode one mono channel slice of an EA-XA v2 (`adpcm_ea_r3`) `SCDl` chunk.

    Supports both:
      - Standard 15-byte ADPCM blocks (28 samples):
          byte 0 = `(coef_idx << 4) | shift_raw`, `shift = 20 - shift_raw`
          bytes 1..14 = 28 signed 4-bit nibbles (high nibble first)
          `v = clamp16(((nib << shift) + h1 * c1 + h2 * c2 + 0x80) >> 8)`
      - `0xEE` 61-byte big-endian raw PCM escape blocks (28 samples):
          byte 0 = `0xEE`
          bytes 1..4 = `(h1, h2)` big-endian int16 predictor state for subsequent blocks
          bytes 5..60 = 28 big-endian int16 PCM samples
    """
    if nsamp < 0:
        raise ValueError(f"Negative sample count: {nsamp}")
    if start < 0 or end < start or end > len(buf):
        raise ValueError(f"Invalid channel slice bounds [{start}..{end}] for buffer len {len(buf)}")

    out = np.zeros(nsamp, dtype=np.int16)
    c1_tbl = EA_ADPCM_C1
    c2_tbl = EA_ADPCM_C2

    adpcm_blocks = 0
    escape_blocks = 0
    clips = 0
    bad_coefs = 0

    oi = 0
    q = start
    while oi < nsamp:
        if q >= end:
            raise ValueError(
                f"Truncated EA-XA R3 channel stream at offset {q} (decoded {oi}/{nsamp} samples)"
            )
        hdr = buf[q]
        q += 1
        if hdr == 0xEE:
            if q + 60 > end:
                raise ValueError(
                    f"Truncated 0xEE escape block at offset {q - 1} (need 60 bytes, have {end - q})"
                )
            e_h1, e_h2 = struct.unpack_from(">hh", buf, q)
            q += 4
            samps = struct.unpack_from(">28h", buf, q)
            q += 56
            take = min(28, nsamp - oi)
            out[oi : oi + take] = samps[:take]
            oi += take
            h1 = e_h1
            h2 = e_h2
            escape_blocks += 1
        else:
            if q + 14 > end:
                raise ValueError(
                    f"Truncated 15-byte ADPCM block at offset {q - 1} (need 14 bytes, have {end - q})"
                )
            ci = (hdr >> 4) & 0x0F
            if ci > 3:
                bad_coefs += 1
                ci &= 3
            lo = hdr & 0x0F
            sh = 20 - lo
            c1 = c1_tbl[ci]
            c2 = c2_tbl[ci]
            adpcm_blocks += 1

            blk_end = q + 14
            if oi + 28 <= nsamp:
                for p in range(q, blk_end):
                    bv = buf[p]
                    n0 = (bv >> 4) & 0x0F
                    if n0 >= 8:
                        n0 -= 16
                    v = ((n0 << sh) + h1 * c1 + h2 * c2 + 0x80) >> 8
                    if v > 32767:
                        v = 32767
                        clips += 1
                    elif v < -32768:
                        v = -32768
                        clips += 1
                    h2 = h1
                    h1 = v
                    out[oi] = v
                    oi += 1

                    n1 = bv & 0x0F
                    if n1 >= 8:
                        n1 -= 16
                    v = ((n1 << sh) + h1 * c1 + h2 * c2 + 0x80) >> 8
                    if v > 32767:
                        v = 32767
                        clips += 1
                    elif v < -32768:
                        v = -32768
                        clips += 1
                    h2 = h1
                    h1 = v
                    out[oi] = v
                    oi += 1
            else:
                for p in range(q, blk_end):
                    bv = buf[p]
                    for nib in ((bv >> 4) & 0x0F, bv & 0x0F):
                        if nib >= 8:
                            nib -= 16
                        v = ((nib << sh) + h1 * c1 + h2 * c2 + 0x80) >> 8
                        if v > 32767:
                            v = 32767
                            clips += 1
                        elif v < -32768:
                            v = -32768
                            clips += 1
                        h2 = h1
                        h1 = v
                        if oi < nsamp:
                            out[oi] = v
                            oi += 1
            q = blk_end

    stats = {
        "adpcm_blocks": adpcm_blocks,
        "escape_blocks": escape_blocks,
        "clips": clips,
        "bad_coefs": bad_coefs,
    }
    return out, h1, h2, stats


def decode_ea_r3_stream(stream_bytes: bytes) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Decode a full `SCHl`..`SCDl`..`SCEl` EA-XA v2 (`adpcm_ea_r3`) stream into stereo/mono int16 PCM.

    Returns `(pcm, stats)` where `pcm` has shape `(num_frames, channels)` (dtype `int16`).
    """
    if len(stream_bytes) < 16 or stream_bytes[:4] != b"SCHl":
        raise ValueError(f"Expected SCHl stream header, got {stream_bytes[:4]!r}")

    schl_item = audio_inspect.parse_schl_chunk(stream_bytes, 0)
    sample_rate = int(schl_item.sample_rate or 32000)
    channels = int(schl_item.channels or 2)
    if channels not in (1, 2):
        raise ValueError(f"Unsupported channel count {channels} in EA-XA R3 stream")

    mv = memoryview(stream_bytes)
    pos = 0
    scdls: List[Tuple[int, int]] = []
    total_len = len(stream_bytes)
    while pos + 8 <= total_len:
        tag = stream_bytes[pos : pos + 4]
        sz = struct.unpack_from("<I", stream_bytes, pos + 4)[0]
        if sz < 8 or pos + sz > total_len:
            if tag == b"SCEl":
                break
            raise ValueError(f"Invalid chunk {tag!r} size {sz} at offset 0x{pos:x} (len={total_len})")
        if tag == b"SCDl":
            scdls.append((pos, sz))
        elif tag == b"SCEl":
            break
        pos += sz

    if not scdls:
        raise ValueError("EA-XA R3 stream contains zero SCDl audio chunks")

    h1_l = h2_l = 0
    h1_r = h2_r = 0
    chunks_l: List[np.ndarray] = []
    chunks_r: List[np.ndarray] = []
    boundary_diffs: List[int] = []
    total_adpcm_blocks = 0
    total_escape_blocks = 0
    total_clips = 0
    total_bad_coefs = 0

    for p, sz in scdls:
        if channels == 2:
            if sz < 20:
                raise ValueError(f"Truncated stereo SCDl header at 0x{p:x} (sz={sz})")
            nsamp, off_l, off_r = struct.unpack_from(">3I", stream_bytes, p + 8)
            data_base = p + 20
            chunk_end = p + sz
            end_l = (data_base + off_r) if off_r > off_l else chunk_end
            c_l, h1_l, h2_l, st_l = decode_ea_r3_channel(mv, data_base + off_l, end_l, nsamp, h1_l, h2_l)
            c_r, h1_r, h2_r, st_r = decode_ea_r3_channel(mv, data_base + off_r, chunk_end, nsamp, h1_r, h2_r)
            if chunks_l and nsamp > 0:
                boundary_diffs.append(abs(int(c_l[0]) - int(chunks_l[-1][-1])))
                boundary_diffs.append(abs(int(c_r[0]) - int(chunks_r[-1][-1])))
            chunks_l.append(c_l)
            chunks_r.append(c_r)
            total_adpcm_blocks += st_l["adpcm_blocks"] + st_r["adpcm_blocks"]
            total_escape_blocks += st_l["escape_blocks"] + st_r["escape_blocks"]
            total_clips += st_l["clips"] + st_r["clips"]
            total_bad_coefs += st_l["bad_coefs"] + st_r["bad_coefs"]
        else:
            if sz < 16:
                raise ValueError(f"Truncated mono SCDl header at 0x{p:x} (sz={sz})")
            nsamp, off_l = struct.unpack_from(">2I", stream_bytes, p + 8)
            data_base = p + 16
            chunk_end = p + sz
            c_l, h1_l, h2_l, st_l = decode_ea_r3_channel(mv, data_base + off_l, chunk_end, nsamp, h1_l, h2_l)
            if chunks_l and nsamp > 0:
                boundary_diffs.append(abs(int(c_l[0]) - int(chunks_l[-1][-1])))
            chunks_l.append(c_l)
            total_adpcm_blocks += st_l["adpcm_blocks"]
            total_escape_blocks += st_l["escape_blocks"]
            total_clips += st_l["clips"]
            total_bad_coefs += st_l["bad_coefs"]

    left_i16 = np.concatenate(chunks_l)
    if channels == 2:
        right_i16 = np.concatenate(chunks_r)
        pcm = np.column_stack((left_i16, right_i16))
    else:
        right_i16 = left_i16
        pcm = left_i16[:, np.newaxis]

    l_f = left_i16.astype(np.float64) / 32768.0
    r_f = right_i16.astype(np.float64) / 32768.0
    rms_l = float(np.sqrt(np.mean(l_f * l_f))) if len(l_f) else 0.0
    rms_r = float(np.sqrt(np.mean(r_f * r_f))) if len(r_f) else 0.0
    peak = float(max(np.max(np.abs(l_f)), np.max(np.abs(r_f)))) if len(l_f) else 0.0
    dc_l = float(np.mean(l_f)) if len(l_f) else 0.0
    dc_r = float(np.mean(r_f)) if len(r_f) else 0.0

    if channels == 2 and len(l_f) > 1 and np.std(l_f) > 1e-9 and np.std(r_f) > 1e-9:
        corr_lr = float(np.corrcoef(l_f, r_f)[0, 1])
    else:
        corr_lr = 1.0 if channels == 1 else 0.0

    intra_l = float(np.mean(np.abs(np.diff(left_i16.astype(np.int32))))) if len(left_i16) > 1 else 0.0
    intra_r = float(np.mean(np.abs(np.diff(right_i16.astype(np.int32))))) if len(right_i16) > 1 else 0.0
    intra_mean = 0.5 * (intra_l + intra_r) if channels == 2 else intra_l
    boundary_mean = float(np.mean(boundary_diffs)) if boundary_diffs else 0.0
    has_nan_or_inf = bool(not (np.all(np.isfinite(l_f)) and np.all(np.isfinite(r_f))))

    stats: Dict[str, Any] = {
        "sample_rate": sample_rate,
        "channels": channels,
        "total_frames": int(len(left_i16)),
        "duration_sec": round(len(left_i16) / float(sample_rate), 4),
        "scdl_chunks": len(scdls),
        "adpcm_blocks": int(total_adpcm_blocks),
        "escape_blocks": int(total_escape_blocks),
        "clips": int(total_clips),
        "bad_coefs": int(total_bad_coefs),
        "rms_l": round(rms_l, 5),
        "rms_r": round(rms_r, 5),
        "peak": round(peak, 5),
        "dc_l": round(dc_l, 6),
        "dc_r": round(dc_r, 6),
        "corr_lr": round(corr_lr, 5),
        "boundary_mean_diff": round(boundary_mean, 2),
        "intra_mean_diff": round(intra_mean, 2),
        "has_nan_or_inf": has_nan_or_inf,
    }
    return pcm, stats


# ---------------------------------------------------------------------------
# 2. Exact VLT ATTRIBUTES.BIN + MPF Pathfinder v5 Track Metadata Mapper
# ---------------------------------------------------------------------------


def _vlt_text(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("text", "")).strip()
    if val is None:
        return ""
    return str(val).strip()


def parse_mpf_track_catalog(
    mpf_data: bytes,
    vlt_bytes: bytes,
    *,
    include_stingers: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int]]]:
    """Map all 26 licensed EA TRAX + 14 pursuit/race interactive score streams.

    Joins `GLOBAL\\ATTRIBUTES.BIN` `music` class (`PathEvent & 0xFFFFFF`) against
    `MW_MUSIC.MPF` `sec[2]` bytecode (`words[3] & 0xFFFFFF`) -> `node_id =
    words[10] & 0xFFFF` -> `sec[0]` `trk_offs[node_id] * 4` ->
    `stream_idx = u16[+0x08] >> 1`, fixing the `ev[10..12]` permutation on
    *We Control*, *Skinnyman*, and *Decadence*.
    """
    if len(mpf_data) < 0x48 or mpf_data[:4] != audio_inspect.MPFD_MAGIC:
        raise ValueError(f"Invalid MW_MUSIC.MPF header: {mpf_data[:4]!r}")

    offs = struct.unpack_from("<10I", mpf_data, 0x14)
    streams = audio_inspect.parse_mpf_stream_table(mpf_data)

    n_nodes = (offs[1] - offs[0]) // 2
    trk_offs = struct.unpack_from(f"<{n_nodes}H", mpf_data, offs[0])

    n_events = (offs[3] - offs[2]) // 2
    ev_offs = struct.unpack_from(f"<{n_events}H", mpf_data, offs[2])

    # Parse VLT `music` class collections
    vdb = vlt_ps2.VpakDatabase(vlt_bytes)
    vault = vdb.vaults[0]
    vlt_by_pe: Dict[int, Dict[str, Any]] = {}
    for col in vault.collections:
        if col.class_name != "music":
            continue
        eff = vdb.get_effective_fields(col)
        pe_fv = eff.get("PathEvent")
        pe_raw = int(pe_fv.value) if pe_fv is not None and isinstance(pe_fv.value, int) else 0
        if pe_raw == 0:
            continue
        vlt_by_pe[pe_raw & 0xFFFFFF] = {
            "vlt_node": col.name,
            "path_event": pe_raw,
            "artist": _vlt_text(eff["Artist"].value) if "Artist" in eff else "",
            "title": _vlt_text(eff["SongName"].value) if "SongName" in eff else "",
            "album": _vlt_text(eff["Album"].value) if "Album" in eff else "",
            "def_play": _vlt_text(eff["DefPlay"].value) if "DefPlay" in eff else "IG",
        }

    # Resolve licensed EA TRAX via sec[2] bytecode -> node_id -> sec[0] -> stream_idx
    licensed_by_stream: Dict[int, Dict[str, Any]] = {}
    for ei, eoff in enumerate(ev_offs):
        byte_off = eoff * 4
        next_off = ev_offs[ei + 1] * 4 if ei + 1 < n_events else offs[4]
        n_words = max(0, (next_off - byte_off) // 4)
        if n_words < 11:
            continue
        words = struct.unpack_from(f"<{n_words}I", mpf_data, byte_off)
        ev_id = words[3] & 0xFFFFFF
        if ev_id not in vlt_by_pe:
            continue
        node_id = words[10] & 0xFFFF
        if node_id >= n_nodes:
            raise ValueError(f"MPF event {ei} references out-of-range node_id {node_id} (>= {n_nodes})")
        node_byte = trk_offs[node_id] * 4
        if node_byte + 0x0A > len(mpf_data):
            raise ValueError(f"MPF node {node_id} offset 0x{node_byte:x} out of bounds")
        stream_idx = struct.unpack_from("<H", mpf_data, node_byte + 0x08)[0] >> 1
        if stream_idx >= len(streams):
            raise ValueError(f"MPF node {node_id} resolved invalid stream_idx {stream_idx}")

        meta = vlt_by_pe[ev_id]
        licensed_by_stream[stream_idx] = {
            "stream_index": stream_idx,
            "mpf_event_index": ei,
            "mpf_node_id": node_id,
            "path_event": meta["path_event"],
            "vlt_node": meta["vlt_node"],
            "artist": meta["artist"],
            "title": meta["title"],
            "album": meta["album"],
            "def_play": meta["def_play"],
            "category": "ea_trax",
            "is_pursuit": False,
            "mpf_duration_ms": int(streams[stream_idx][1]),
            "file": f"tracks/track_{stream_idx:02d}.m4a",
        }

    # Build deterministic ordered catalog:
    #   1) 26 licensed EA TRAX sorted by stream_index (1..26)
    #   2) 9 adaptive pursuit score themes (31..39)
    #   3) 5 interactive race/chase score themes (40..44)
    #   4) optional 8 short stingers (45..52)
    catalog: List[Dict[str, Any]] = []
    for sidx in sorted(licensed_by_stream.keys()):
        entry = dict(licensed_by_stream[sidx])
        entry["index"] = len(catalog)
        catalog.append(entry)

    # Map MPF events for score/stinger streams where available
    score_event_map: Dict[int, Tuple[int, int, int]] = {}
    for ei, eoff in enumerate(ev_offs):
        byte_off = eoff * 4
        next_off = ev_offs[ei + 1] * 4 if ei + 1 < n_events else offs[4]
        n_words = max(0, (next_off - byte_off) // 4)
        if n_words < 6:
            continue
        words = struct.unpack_from(f"<{n_words}I", mpf_data, byte_off)
        ev_id = words[3] & 0xFFFFFF
        for wi, w in enumerate(words):
            if w == 0x30400 and wi + 1 < n_words:
                nid = words[wi + 1] & 0xFFFF
                if nid < n_nodes:
                    nb = trk_offs[nid] * 4
                    if nb + 0x0A <= len(mpf_data):
                        sidx = struct.unpack_from("<H", mpf_data, nb + 0x08)[0] >> 1
                        score_event_map.setdefault(sidx, (ei, nid, ev_id))

    for num, sidx in enumerate(PURSUIT_SCORE_STREAM_RANGE, start=1):
        if sidx >= len(streams):
            continue
        ei, nid, ev_id = score_event_map.get(sidx, (-1, -1, 0))
        catalog.append(
            {
                "index": len(catalog),
                "stream_index": sidx,
                "mpf_event_index": ei,
                "mpf_node_id": nid,
                "path_event": ev_id,
                "vlt_node": f"pursuit_theme_{num:02d}",
                "artist": "Paul Linford & Chris Vrenna",
                "title": f"Pursuit Theme {num:02d}",
                "album": "Need for Speed: Most Wanted Original Score",
                "def_play": "IG",
                "category": "pursuit",
                "is_pursuit": True,
                "mpf_duration_ms": int(streams[sidx][1]),
                "file": f"tracks/track_{sidx:02d}.m4a",
            }
        )

    for num, sidx in enumerate(RACE_SCORE_STREAM_RANGE, start=1):
        if sidx >= len(streams):
            continue
        ei, nid, ev_id = score_event_map.get(sidx, (-1, -1, 0))
        catalog.append(
            {
                "index": len(catalog),
                "stream_index": sidx,
                "mpf_event_index": ei,
                "mpf_node_id": nid,
                "path_event": ev_id,
                "vlt_node": f"score_theme_{num:02d}",
                "artist": "Paul Linford & Chris Vrenna",
                "title": f"Interactive Score {num:02d}",
                "album": "Need for Speed: Most Wanted Original Score",
                "def_play": "IG",
                "category": "score",
                "is_pursuit": True,
                "mpf_duration_ms": int(streams[sidx][1]),
                "file": f"tracks/track_{sidx:02d}.m4a",
            }
        )

    if include_stingers:
        for num, sidx in enumerate(STINGER_STREAM_RANGE, start=1):
            if sidx >= len(streams):
                continue
            ei, nid, ev_id = score_event_map.get(sidx, (-1, -1, 0))
            catalog.append(
                {
                    "index": len(catalog),
                    "stream_index": sidx,
                    "mpf_event_index": ei,
                    "mpf_node_id": nid,
                    "path_event": ev_id,
                    "vlt_node": f"stinger_{num:02d}",
                    "artist": "Paul Linford & Chris Vrenna",
                    "title": f"Pursuit Stinger {num:02d}",
                    "album": "Need for Speed: Most Wanted Original Score",
                    "def_play": "IG",
                    "category": "stinger",
                    "is_pursuit": True,
                    "mpf_duration_ms": int(streams[sidx][1]),
                    "file": f"tracks/track_{sidx:02d}.m4a",
                }
            )

    return catalog, streams


# ---------------------------------------------------------------------------
# 3. WAV Writing & macOS `afconvert` AAC-LC 128 kbps Encoding Worker
# ---------------------------------------------------------------------------


def write_stereo_wav_pcm16(path: Path, sample_rate: int, pcm: np.ndarray) -> None:
    """Write interleaved stereo (or mono) 16-bit signed LE PCM WAV."""
    arr = np.asarray(pcm, dtype=np.int16)
    if arr.ndim == 1:
        channels = 1
    else:
        channels = int(arr.shape[1])
    if sys.byteorder != "little":
        arr = arr.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(np.ascontiguousarray(arr).tobytes())


def encode_wav_to_m4a(wav_path: Path, m4a_path: Path, bitrate: int = 128000) -> None:
    """Encode a PCM WAV to AAC-LC M4A using macOS built-in `afconvert`."""
    afconvert = shutil.which("afconvert")
    if not afconvert:
        raise RuntimeError("macOS `afconvert` binary not found in PATH")
    tmp_m4a = m4a_path.with_name(f".tmp_{m4a_path.name}")
    try:
        cmd = [
            afconvert,
            "-f",
            "m4af",
            "-d",
            "aac",
            "-b",
            str(int(bitrate)),
            str(wav_path),
            str(tmp_m4a),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not tmp_m4a.is_file() or tmp_m4a.stat().st_size == 0:
            raise RuntimeError(f"afconvert failed ({res.returncode}): {res.stderr.strip()}")
        os.replace(tmp_m4a, m4a_path)
    finally:
        if tmp_m4a.exists():
            tmp_m4a.unlink(missing_ok=True)


def _export_single_track_worker(job: Dict[str, Any]) -> Dict[str, Any]:
    """ProcessPool worker: read one stream slice from ISO, decode EA-XA R3, encode M4A."""
    iso_path = Path(job["iso_path"])
    mus_base = int(job["mus_base"])
    stream_start = int(job["stream_start"])
    stream_end = int(job["stream_end"])
    out_m4a = Path(job["out_m4a"])
    meta = dict(job["meta"])
    force = bool(job["force"])
    bitrate = int(job["bitrate"])
    prev_entry = job.get("prev_entry")

    if not force and out_m4a.is_file() and out_m4a.stat().st_size > 4096:
        fbytes = int(out_m4a.stat().st_size)
        if isinstance(prev_entry, dict) and prev_entry.get("file_bytes") == fbytes and prev_entry.get("decode_stats"):
            meta["duration_sec"] = prev_entry.get("duration_sec", round(meta["mpf_duration_ms"] / 1000.0, 4))
            meta["sample_rate"] = prev_entry.get("sample_rate", 32000)
            meta["channels"] = prev_entry.get("channels", 2)
            meta["file_bytes"] = fbytes
            meta["decode_stats"] = prev_entry["decode_stats"]
            meta["skipped"] = True
            return meta
        meta["duration_sec"] = round(meta["mpf_duration_ms"] / 1000.0, 4)
        meta["sample_rate"] = 32000
        meta["channels"] = 2
        meta["file_bytes"] = fbytes
        meta["skipped"] = True
        return meta

    t0 = time.perf_counter()
    with iso_path.open("rb") as f:
        f.seek(mus_base + stream_start)
        raw = f.read(stream_end - stream_start)

    pcm, stats = decode_ea_r3_stream(raw)

    tmp_wav = out_m4a.with_name(f".tmp_{out_m4a.stem}.wav")
    try:
        write_stereo_wav_pcm16(tmp_wav, stats["sample_rate"], pcm)
        encode_wav_to_m4a(tmp_wav, out_m4a, bitrate=bitrate)
    finally:
        if tmp_wav.exists():
            tmp_wav.unlink(missing_ok=True)

    meta["duration_sec"] = stats["duration_sec"]
    meta["sample_rate"] = stats["sample_rate"]
    meta["channels"] = stats["channels"]
    meta["file_bytes"] = int(out_m4a.stat().st_size)
    meta["decode_stats"] = {
        "scdl_chunks": stats["scdl_chunks"],
        "adpcm_blocks": stats["adpcm_blocks"],
        "escape_blocks": stats["escape_blocks"],
        "clips": stats["clips"],
        "bad_coefs": stats["bad_coefs"],
        "rms_l": stats["rms_l"],
        "rms_r": stats["rms_r"],
        "peak": stats["peak"],
        "dc_l": stats["dc_l"],
        "dc_r": stats["dc_r"],
        "corr_lr": stats["corr_lr"],
        "boundary_mean_diff": stats["boundary_mean_diff"],
        "intra_mean_diff": stats["intra_mean_diff"],
    }
    meta["elapsed_sec"] = round(time.perf_counter() - t0, 3)
    meta["skipped"] = False
    return meta


def export_music_pack(
    iso_path: Path,
    out_dir: Path,
    *,
    workers: int = 0,
    limit: int = 0,
    force: bool = False,
    bitrate: int = 128000,
    include_stingers: bool = False,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Export NFSMW licensed EA TRAX + pursuit/race score tracks into `out_dir`."""
    t_start = time.perf_counter()
    arc = PS2NFSMWArchive(iso_path)
    mpf_data = arc.read_entry(arc.find(MPF_VIRTUAL_PATH))
    vlt_bytes = arc.read_entry(arc.find(ATTR_VIRTUAL_PATH))
    mus_entry = arc.find(MUS_VIRTUAL_PATH)
    mus_base = (arc.zz_lbas[mus_entry.zz_index] + mus_entry.sector_in_zz) * SECTOR_SIZE

    catalog, streams = parse_mpf_track_catalog(
        mpf_data, vlt_bytes, include_stingers=include_stingers
    )
    if limit > 0:
        catalog = catalog[:limit]

    tracks_dir = out_dir / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)

    # Load any existing manifest to reuse decode_stats when skipping already-encoded M4As
    prev_by_stream: Dict[int, Dict[str, Any]] = {}
    manifest_path = out_dir / "manifest.json"
    if manifest_path.is_file() and not force:
        try:
            old_mf = json.loads(manifest_path.read_text(encoding="utf-8"))
            for t in old_mf.get("tracks", []):
                if isinstance(t, dict) and "stream_index" in t:
                    prev_by_stream[int(t["stream_index"])] = t
        except Exception:
            pass

    jobs: List[Dict[str, Any]] = []
    for item in catalog:
        sidx = int(item["stream_index"])
        s_off = int(streams[sidx][0])
        e_off = int(streams[sidx + 1][0]) if sidx + 1 < len(streams) else int(mus_entry.byte_size)
        out_m4a = out_dir / item["file"]
        jobs.append(
            {
                "iso_path": str(iso_path),
                "mus_base": mus_base,
                "stream_start": s_off,
                "stream_end": e_off,
                "out_m4a": str(out_m4a),
                "meta": item,
                "force": force,
                "bitrate": bitrate,
                "prev_entry": prev_by_stream.get(sidx),
            }
        )

    n_workers = workers if workers > 0 else min(max(1, os.cpu_count() or 4), len(jobs) or 1, 16)
    results: List[Dict[str, Any]] = []

    if n_workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            res = _export_single_track_worker(job)
            results.append(res)
            if not quiet:
                tag = "SKIP" if res.get("skipped") else f"{res.get('elapsed_sec', 0):.2f}s"
                print(
                    f"  [{res['index'] + 1:2d}/{len(jobs):2d}] stream={res['stream_index']:2d} "
                    f"({res['category']:7s}) {res['artist']} — {res['title']} "
                    f"[{res['duration_sec']:.1f}s, {res['file_bytes']/1e6:.2f} MB, {tag}]"
                )
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=n_workers) as pool:
            fut_to_idx = {pool.submit(_export_single_track_worker, j): j["meta"]["index"] for j in jobs}
            done_count = 0
            for fut in concurrent.futures.as_completed(fut_to_idx):
                res = fut.result()
                results.append(res)
                done_count += 1
                if not quiet:
                    tag = "SKIP" if res.get("skipped") else f"{res.get('elapsed_sec', 0):.2f}s"
                    print(
                        f"  [{done_count:2d}/{len(jobs):2d}] stream={res['stream_index']:2d} "
                        f"({res['category']:7s}) {res['artist']} — {res['title']} "
                        f"[{res['duration_sec']:.1f}s, {res['file_bytes']/1e6:.2f} MB, {tag}]"
                    )

    results.sort(key=lambda r: int(r["index"]))
    # Strip transient worker timing keys from persisted manifest entries
    clean_tracks: List[Dict[str, Any]] = []
    skipped_count = 0
    encoded_count = 0
    for r in results:
        if r.get("skipped"):
            skipped_count += 1
        else:
            encoded_count += 1
        entry = {k: v for k, v in r.items() if k not in ("elapsed_sec", "skipped")}
        clean_tracks.append(entry)

    total_bytes = sum(int(t["file_bytes"]) for t in clean_tracks)
    total_duration_sec = sum(float(t["duration_sec"]) for t in clean_tracks)
    licensed_count = sum(1 for t in clean_tracks if t["category"] == "ea_trax")
    score_count = sum(1 for t in clean_tracks if t["category"] in ("pursuit", "score"))
    stinger_count = sum(1 for t in clean_tracks if t["category"] == "stinger")

    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "source_mus": MUS_VIRTUAL_PATH,
        "source_mpf": MPF_VIRTUAL_PATH,
        "codec_source": "EA-XA v2 (adpcm_ea_r3)",
        "codec_output": f"AAC-LC {bitrate // 1000}kbps (M4A)",
        "sample_rate": 32000,
        "channels": 2,
        "licensed_count": licensed_count,
        "score_count": score_count,
        "stinger_count": stinger_count,
        "total_tracks": len(clean_tracks),
        "total_duration_sec": round(total_duration_sec, 2),
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024.0 * 1024.0), 2),
        "tracks": clean_tracks,
    }

    tmp_mf = manifest_path.with_name(".tmp_manifest.json")
    tmp_mf.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_mf, manifest_path)

    elapsed = round(time.perf_counter() - t_start, 2)
    if not quiet:
        print(
            f"Exported {len(clean_tracks)} tracks ({licensed_count} EA TRAX + {score_count} score"
            + (f" + {stinger_count} stingers" if stinger_count else "")
            + f", {encoded_count} encoded, {skipped_count} skipped) -> {out_dir} "
            f"({manifest['total_mb']:.2f} MB, {total_duration_sec/60.0:.1f} min) in {elapsed:.2f}s"
        )

    return {
        "manifest": manifest,
        "encoded_count": encoded_count,
        "skipped_count": skipped_count,
        "elapsed_sec": elapsed,
        "manifest_path": str(manifest_path),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export NFSMW EA TRAX & interactive score tracks from untouched PS2 ISO into build/music_pack."
    )
    parser.add_argument(
        "--iso",
        type=Path,
        default=None,
        help="Path to untouched PS2 NFSMW Black Edition ISO (default: $NFSMW_ISO or ~/Documents/PS2Games/...)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output music pack directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel worker processes (default: auto up to 16 CPU cores)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of tracks exported (0 = all 26 EA TRAX + 14 score tracks)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-decode and re-encode tracks even if .m4a already exists",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        default=128000,
        help="AAC output bitrate in bps (default: 128000)",
    )
    parser.add_argument(
        "--include-stingers",
        action="store_true",
        help="Also export the 8 short pursuit stingers (streams 45..52)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout")
    args = parser.parse_args(argv)

    iso_path = resolve_iso_path(args.iso)
    if iso_path is None:
        print(
            "ERROR: Untouched PS2 NFSMW Black Edition (SLUS-21351) ISO not found. "
            "Pass --iso <path> or set NFSMW_ISO.",
            file=sys.stderr,
        )
        return 1

    out_dir = args.out.resolve() if not args.out.is_absolute() else args.out
    res = export_music_pack(
        iso_path,
        out_dir,
        workers=args.workers,
        limit=args.limit,
        force=args.force,
        bitrate=args.bitrate,
        include_stingers=args.include_stingers,
        quiet=args.json,
    )
    if args.json:
        summary = {k: v for k, v in res["manifest"].items() if k != "tracks"}
        summary["encoded_count"] = res["encoded_count"]
        summary["skipped_count"] = res["skipped_count"]
        summary["elapsed_sec"] = res["elapsed_sec"]
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
