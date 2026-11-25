#!/usr/bin/env python3
"""audio_inspect.py — Read-only PS2 NFSMW Audio Inspector (ABKC, Gnsu, SCHl).

Inspects Need for Speed: Most Wanted - Black Edition (PS2, SLUS-21351) audio
containers directly from an untouched ISO (strictly read-only 'rb' via
tools/zdir_tool.py) or from standalone extracted .ABK/.GIN/.MUS/.BIG/.MPC files:

  1. EA AEMS Sound Banks ('ABKC' + embedded 'BNKl' v5 + 'PT' sample headers)
     - 301 ABKC entries in SLUS-21351 ZDIR.BIN (2,350 active sample headers)
  2. EA Ginsu Granular Engine Synths ('Gnsu' v20)
     - 160 Gnsu entries in SLUS-21351 ZDIR.BIN (RPM curves, grain tables, sample rates)
  3. EA Streamed Audio ('SCHl' / 'GSTR' / 'PT' EA ADPCM & MicroTalk)
     - .BIG + .IDX stream banks (SOUND\\STREAMS\\NISAUDIO.BIG, SOUND\\SPEECH\\COPSPEECH_EN.BIG)
     - .MUS + .MPF Pathfinder v5 interactive music (SOUND\\PFDATA\\MW_MUSIC / MW_MUS_1..4)
     - .MPC EA FMV interleaved SCHl audio tracks (MOVIES\\*.MPC)

Pure Python 3 standard library (zero third-party dependencies).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from iso_inspect import SECTOR_SIZE  # noqa: E402
from zdir_tool import PS2NFSMWArchive, ZDirEntry  # noqa: E402

ABKC_MAGIC = b"ABKC"
BNKL_MAGIC = b"BNKl"
GNSU_MAGIC = b"Gnsu"
SCHL_MAGIC = b"SCHl"
MPFD_MAGIC = b"xDFP"
MPCH_MAGIC = b"MPCh"
GSTR_MAGIC = b"GSTR"

EA_CODEC_NAMES: Dict[int, str] = {
    0x00: "PCM / Uncompressed",
    0x04: "EA-MT (MicroTalk Speech)",
    0x07: "EA-XA",
    0x0A: "PS2 SPU-ADPCM / EA-XA",
    0x14: "PS2 SPU-ADPCM",
    0x1C: "EA-XAS",
}


def codec_display_name(codec_id: Optional[int], pt_kind: str = "PT") -> str:
    """Return human-readable EA audio codec name."""
    if codec_id is None:
        return "EA-XA v2 (GSTR default)" if pt_kind == "GSTR" else "Default / Unspecified"
    return EA_CODEC_NAMES.get(codec_id, f"EA_CODEC_0x{codec_id:02X}")


# ---------------------------------------------------------------------------
# EA PT / GSTR Tagged Audio Header Parser
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PTAudioMetadata:
    pt_kind: str  # "PT" or "GSTR"
    pt_version: Optional[int]
    stream_version: Optional[int]
    channels: int
    sample_rate: Optional[int]
    num_samples: Optional[int]
    loop_start: Optional[int]
    loop_length: Optional[int]
    data_offset: Optional[int]
    codec_id: Optional[int]
    codec_name: str
    flags_8c: Optional[int]
    duration_sec: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def parse_pt_header(buf: bytes, offset: int = 0) -> PTAudioMetadata:
    """Parse an EA 'PT' or 'GSTR' tagged audio property header."""
    if offset + 4 > len(buf):
        raise ValueError("Buffer too short for PT/GSTR header")

    head4 = buf[offset : offset + 4]
    if head4 == GSTR_MAGIC:
        pt_kind = "GSTR"
        pt_ver = struct.unpack_from("<I", buf, offset + 4)[0] if offset + 8 <= len(buf) else None
        q = offset + 8
    elif head4[:2] == b"PT":
        pt_kind = "PT"
        pt_ver = struct.unpack_from("<H", buf, offset + 2)[0]
        q = offset + 4
    else:
        raise ValueError(f"Expected PT or GSTR magic, got {head4!r}")

    stream_version: Optional[int] = None
    channels = 1
    sample_rate: Optional[int] = None
    num_samples: Optional[int] = None
    loop_start: Optional[int] = None
    loop_length: Optional[int] = None
    data_offset: Optional[int] = None
    codec_id: Optional[int] = None
    flags_8c: Optional[int] = None

    while q < len(buf):
        tag = buf[q]
        if tag == 0xFF:
            break
        if tag in (0xFC, 0xFD, 0xFE):
            q += 1
            continue
        if q + 1 >= len(buf):
            break
        ln = buf[q + 1]
        if q + 2 + ln > len(buf):
            break
        raw_val = buf[q + 2 : q + 2 + ln]
        val = int.from_bytes(raw_val, "big") if ln > 0 else 0
        if tag == 0x80:
            stream_version = val
        elif tag == 0x82:
            channels = val
        elif tag == 0x84:
            sample_rate = val
        elif tag == 0x85:
            num_samples = val
        elif tag == 0x86:
            loop_start = val
        elif tag == 0x87:
            loop_length = val
        elif tag == 0x88:
            data_offset = val
        elif tag == 0x8C:
            flags_8c = val
        elif tag == 0xA0:
            codec_id = val
        q += 2 + ln

    duration_sec: Optional[float] = None
    if num_samples is not None and sample_rate and sample_rate > 0:
        duration_sec = round(num_samples / float(sample_rate), 4)

    return PTAudioMetadata(
        pt_kind=pt_kind,
        pt_version=pt_ver,
        stream_version=stream_version,
        channels=channels,
        sample_rate=sample_rate,
        num_samples=num_samples,
        loop_start=loop_start,
        loop_length=loop_length,
        data_offset=data_offset,
        codec_id=codec_id,
        codec_name=codec_display_name(codec_id, pt_kind=pt_kind),
        flags_8c=flags_8c,
        duration_sec=duration_sec,
    )


# ---------------------------------------------------------------------------
# ABKC (AEMS Audio Bank + BNKl) Parser
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class AbkcSampleInfo:
    slot_index: int
    pt_offset: int
    channels: int
    sample_rate: Optional[int]
    num_samples: Optional[int]
    looped: bool
    loop_start: Optional[int]
    loop_length: Optional[int]
    data_offset: Optional[int]
    codec_id: Optional[int]
    codec_name: str
    duration_sec: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class AbkcBankInfo:
    path: str
    name_hash: Optional[str]
    byte_size: int
    version_hex: str
    num_modules: int
    num_banks: int
    bnkl_offset: int
    bnkl_chunk_size: int
    bnkl_version: int
    slot_count: int
    active_samples: int
    bnkl_header_size: int
    bnkl_payload_size: int
    sample_rates: List[int]
    channels: List[int]
    codecs: List[str]
    total_duration_sec: float
    tags: List[str]
    samples: List[AbkcSampleInfo]

    def to_dict(self, include_samples: bool = False) -> Dict[str, Any]:
        out = dataclasses.asdict(self)
        if not include_samples:
            out.pop("samples", None)
        return out


def _extract_ascii_tags(buf: bytes) -> List[str]:
    tags: List[str] = []
    seen = set()
    for m in re.finditer(rb"[A-Za-z][A-Za-z0-9_]{2,24}", buf):
        s = m.group(0).decode("ascii", errors="ignore")
        if s not in seen:
            seen.add(s)
            tags.append(s)
    return tags


def parse_abkc(
    data: bytes,
    path: str = "<buffer>",
    name_hash: Optional[int] = None,
) -> AbkcBankInfo:
    """Parse an EA 'ABKC' AEMS bank and its embedded 'BNKl' v5 sample table."""
    if len(data) < 0x40 or data[:4] != ABKC_MAGIC:
        raise ValueError(f"Not an ABKC bank: magic={data[:4]!r}")

    ver_hex = data[4:8].hex()
    num_modules, num_banks = struct.unpack_from("<HH", data, 8)
    total_size, bnkl_off, _tbl5c, _bnkl_off2, bnkl_chunk_size = struct.unpack_from("<5I", data, 0x14)
    t1_off, _t2_off, t3_off = struct.unpack_from("<3I", data, 0x30)

    if bnkl_off + 20 > len(data) or data[bnkl_off : bnkl_off + 4] != BNKL_MAGIC:
        raise ValueError(
            f"Missing BNKl sub-header in {path} at 0x{bnkl_off:x}: {data[bnkl_off:bnkl_off+4]!r}"
        )

    _btag, bnkl_ver, slot_count, bnkl_hdr_sz, bnkl_pay_sz = struct.unpack_from("<4sHHII", data, bnkl_off)
    slot_tbl_off = bnkl_off + 0x14
    if slot_tbl_off + slot_count * 4 > len(data):
        raise ValueError(f"Truncated BNKl slot offset table in {path} (slots={slot_count})")

    rel_offs = struct.unpack_from(f"<{slot_count}I", data, slot_tbl_off)
    samples: List[AbkcSampleInfo] = []
    srates_set = set()
    chans_set = set()
    codecs_set = set()
    total_dur = 0.0

    for idx, rel in enumerate(rel_offs):
        if rel == 0:
            continue
        pt_off = slot_tbl_off + idx * 4 + rel
        if pt_off + 4 > len(data):
            continue
        meta = parse_pt_header(data, pt_off)
        if meta.sample_rate:
            srates_set.add(meta.sample_rate)
        chans_set.add(meta.channels)
        codecs_set.add(meta.codec_name)
        if meta.duration_sec is not None:
            total_dur += meta.duration_sec
        samples.append(
            AbkcSampleInfo(
                slot_index=idx,
                pt_offset=pt_off,
                channels=meta.channels,
                sample_rate=meta.sample_rate,
                num_samples=meta.num_samples,
                looped=(meta.loop_start is not None or meta.loop_length is not None),
                loop_start=meta.loop_start,
                loop_length=meta.loop_length,
                data_offset=meta.data_offset,
                codec_id=meta.codec_id,
                codec_name=meta.codec_name,
                duration_sec=meta.duration_sec,
            )
        )

    tail_start = t3_off if 0 < t3_off < len(data) else (t1_off if 0 < t1_off < len(data) else len(data))
    tags = _extract_ascii_tags(data[tail_start:])

    return AbkcBankInfo(
        path=path,
        name_hash=f"0x{name_hash:08x}" if name_hash is not None else None,
        byte_size=len(data),
        version_hex=ver_hex,
        num_modules=num_modules,
        num_banks=num_banks,
        bnkl_offset=bnkl_off,
        bnkl_chunk_size=bnkl_chunk_size if bnkl_chunk_size else total_size - bnkl_off,
        bnkl_version=bnkl_ver,
        slot_count=slot_count,
        active_samples=len(samples),
        bnkl_header_size=bnkl_hdr_sz,
        bnkl_payload_size=bnkl_pay_sz,
        sample_rates=sorted(srates_set),
        channels=sorted(chans_set),
        codecs=sorted(codecs_set),
        total_duration_sec=round(total_dur, 4),
        tags=tags,
        samples=samples,
    )


# ---------------------------------------------------------------------------
# Gnsu (Ginsu Granular Engine Synth) Parser
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class GnsuSynthInfo:
    path: str
    name_hash: Optional[str]
    byte_size: int
    version: str
    min_rpm: float
    max_rpm: float
    num_rpm_steps: int
    num_grains: int
    total_samples: int
    sample_rate: int
    channels: int
    direction: str  # "accel", "decel", or "flat"
    rpm_table_offset: int
    grain_table_offset: int
    granule_data_offset: int
    granule_data_bytes: int
    duration_sec: float

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def parse_gnsu(
    data: bytes,
    path: str = "<buffer>",
    name_hash: Optional[int] = None,
) -> GnsuSynthInfo:
    """Parse an EA 'Gnsu' granular engine synth header, RPM pitch map, and grain table."""
    if len(data) < 0x20 or data[:4] != GNSU_MAGIC:
        raise ValueError(f"Not a Gnsu file: magic={data[:4]!r}")

    ver_raw, min_rpm, max_rpm, n_rpm, n_grain, total_samples, sample_rate = struct.unpack_from(
        "<4sff4I", data, 4
    )
    version = ver_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    rpm_table_off = 0x20
    rpm_entries = n_rpm + 1
    grain_table_off = rpm_table_off + rpm_entries * 4
    granule_data_off = grain_table_off + n_grain * 4
    if granule_data_off > len(data):
        raise ValueError(
            f"Truncated Gnsu tables in {path}: need {granule_data_off} B, have {len(data)} B"
        )

    rpm_vals = struct.unpack_from(f"<{rpm_entries}I", data, rpm_table_off) if rpm_entries > 0 else ()
    if len(rpm_vals) >= 2:
        if rpm_vals[-1] > rpm_vals[0]:
            direction = "accel"
        elif rpm_vals[-1] < rpm_vals[0]:
            direction = "decel"
        else:
            direction = "flat"
    else:
        direction = "flat"

    dur = round(total_samples / float(sample_rate), 4) if sample_rate > 0 else 0.0
    return GnsuSynthInfo(
        path=path,
        name_hash=f"0x{name_hash:08x}" if name_hash is not None else None,
        byte_size=len(data),
        version=version,
        min_rpm=round(min_rpm, 3),
        max_rpm=round(max_rpm, 3),
        num_rpm_steps=n_rpm,
        num_grains=n_grain,
        total_samples=total_samples,
        sample_rate=sample_rate,
        channels=1,
        direction=direction,
        rpm_table_offset=rpm_table_off,
        grain_table_offset=grain_table_off,
        granule_data_offset=granule_data_off,
        granule_data_bytes=len(data) - granule_data_off,
        duration_sec=dur,
    )


# ---------------------------------------------------------------------------
# SCHl Streamed Audio Parsers (.BIG+.IDX, .MUS+.MPF, .MPC, raw SCHl)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SchlStreamItem:
    index: int
    offset: int
    schl_size: int
    pt_kind: str
    channels: int
    sample_rate: Optional[int]
    num_samples: Optional[int]
    codec_id: Optional[int]
    codec_name: str
    duration_sec: Optional[float]
    mpf_duration_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SchlContainerInfo:
    path: str
    container_type: str  # "BIG+IDX", "MUS+MPF", "MPC", or "SCHl"
    byte_size: int
    stream_count: int
    inspected_streams: int
    channels: List[int]
    sample_rates: List[int]
    codecs: List[str]
    total_duration_sec: Optional[float]
    streams: List[SchlStreamItem]

    def to_dict(self, include_streams: bool = False) -> Dict[str, Any]:
        out = dataclasses.asdict(self)
        if not include_streams:
            out.pop("streams", None)
        return out


def parse_schl_chunk(buf: bytes, offset: int = 0, index: int = 0, mpf_dur_ms: Optional[int] = None) -> SchlStreamItem:
    """Parse a single 'SCHl' chunk header starting at `offset`."""
    if offset + 12 > len(buf) or buf[offset : offset + 4] != SCHL_MAGIC:
        raise ValueError(f"Expected SCHl magic at 0x{offset:x}, got {buf[offset:offset+4]!r}")
    schl_size = struct.unpack_from("<I", buf, offset + 4)[0]
    end = min(len(buf), offset + max(12, schl_size))
    meta = parse_pt_header(buf[:end], offset + 8)
    dur = meta.duration_sec
    if dur is None and mpf_dur_ms is not None:
        dur = round(mpf_dur_ms / 1000.0, 4)
    return SchlStreamItem(
        index=index,
        offset=offset,
        schl_size=schl_size,
        pt_kind=meta.pt_kind,
        channels=meta.channels,
        sample_rate=meta.sample_rate,
        num_samples=meta.num_samples,
        codec_id=meta.codec_id,
        codec_name=meta.codec_name,
        duration_sec=dur,
        mpf_duration_ms=mpf_dur_ms,
    )


def parse_idx_offsets(idx_data: bytes) -> List[int]:
    """Parse stream byte offsets from an EA .IDX stream index (NISAUDIO.IDX / COPSPEECH_EN.IDX)."""
    if len(idx_data) < 0x64:
        raise ValueError("IDX file too small")
    _ver, cnt = struct.unpack_from("<II", idx_data, 0)
    if 0x64 + cnt * 16 > len(idx_data):
        raise ValueError(f"Truncated IDX offset table (cnt={cnt}, len={len(idx_data)})")
    return [struct.unpack_from("<I", idx_data, 0x64 + i * 16)[0] for i in range(cnt)]


def parse_mpf_stream_table(mpf_data: bytes) -> List[Tuple[int, int]]:
    """Parse (byte_offset, duration_ms) entries from an EA Pathfinder v5 ('PFDx') .MPF file."""
    if len(mpf_data) < 0x48 or mpf_data[:4] != MPFD_MAGIC:
        raise ValueError(f"Not an EA PFDx .MPF file: {mpf_data[:4]!r}")
    offs = struct.unpack_from("<10I", mpf_data, 0x14)
    tbl_start, tbl_end = offs[8], offs[9]
    if tbl_end <= tbl_start or tbl_end > len(mpf_data):
        raise ValueError(f"Invalid PFDx stream table bounds [0x{tbl_start:x}..0x{tbl_end:x}]")
    out: List[Tuple[int, int]] = []
    for p in range(tbl_start, tbl_end - 7, 8):
        blk_off, dur_ms = struct.unpack_from("<II", mpf_data, p)
        out.append((blk_off * 128, dur_ms))
    return out


def _read_schl_at(read_slice: Callable[[int, int], bytes], off: int) -> bytes:
    head = read_slice(off, 128)
    if len(head) >= 8 and head[:4] == SCHL_MAGIC:
        sz = struct.unpack_from("<I", head, 4)[0]
        if 12 <= sz <= 4096 and sz > len(head):
            return read_slice(off, sz)
    return head


def inspect_schl_stream_table(
    path: str,
    container_type: str,
    byte_size: int,
    entries: List[Tuple[int, Optional[int]]],
    read_slice: Callable[[int, int], bytes],
    max_streams: int = 0,
) -> SchlContainerInfo:
    """Inspect SCHl headers at indexed offsets using a zero-copy slice reader."""
    limit = len(entries) if max_streams <= 0 else min(len(entries), max_streams)
    items: List[SchlStreamItem] = []
    chans = set()
    srates = set()
    codecs = set()
    total_dur = 0.0
    have_all_durs = True

    for idx in range(limit):
        off, dur_ms = entries[idx]
        if off + 12 > byte_size:
            continue
        hdr_bytes = _read_schl_at(read_slice, off)
        if hdr_bytes[:4] != SCHL_MAGIC:
            continue
        item = parse_schl_chunk(hdr_bytes, 0, index=idx, mpf_dur_ms=dur_ms)
        item.offset = off
        items.append(item)
        chans.add(item.channels)
        if item.sample_rate:
            srates.add(item.sample_rate)
        codecs.add(item.codec_name)
        if item.duration_sec is not None:
            total_dur += item.duration_sec
        else:
            have_all_durs = False

    if max_streams > 0 and limit < len(entries):
        if all(d is not None for _, d in entries):
            total_dur = sum(float(d or 0) for _, d in entries) / 1000.0
            have_all_durs = True
        else:
            have_all_durs = False

    return SchlContainerInfo(
        path=path,
        container_type=container_type,
        byte_size=byte_size,
        stream_count=len(entries),
        inspected_streams=len(items),
        channels=sorted(chans),
        sample_rates=sorted(srates),
        codecs=sorted(codecs),
        total_duration_sec=round(total_dur, 3) if have_all_durs else None,
        streams=items,
    )


def inspect_mpc_header(path: str, byte_size: int, read_slice: Callable[[int, int], bytes]) -> Optional[SchlContainerInfo]:
    """Locate and parse the initial SCHl audio header inside an EA MPC FMV container."""
    head = read_slice(0, min(byte_size, 65536))
    pos = head.find(SCHL_MAGIC)
    if pos < 0:
        return None
    item = parse_schl_chunk(head, pos, index=0)
    return SchlContainerInfo(
        path=path,
        container_type="MPC",
        byte_size=byte_size,
        stream_count=1,
        inspected_streams=1,
        channels=[item.channels],
        sample_rates=[item.sample_rate] if item.sample_rate else [],
        codecs=[item.codec_name],
        total_duration_sec=item.duration_sec,
        streams=[item],
    )


# ---------------------------------------------------------------------------
# High-Level ISO & Standalone File Inspection
# ---------------------------------------------------------------------------


def _iso_slice_reader(f: BinaryIO, arc: PS2NFSMWArchive, entry: ZDirEntry) -> Callable[[int, int], bytes]:
    base = (arc.zz_lbas[entry.zz_index] + entry.sector_in_zz) * SECTOR_SIZE

    def read_slice(off: int, length: int) -> bytes:
        if off >= entry.byte_size:
            return b""
        f.seek(base + off)
        return f.read(min(length, entry.byte_size - off))

    return read_slice


def inspect_iso(
    iso_path: Path,
    kind: str = "all",
    name_filter: str = "",
    max_streams: int = 0,
    include_mpc: bool = True,
) -> Dict[str, Any]:
    """Read-only audit of all ABKC, Gnsu, and SCHl audio assets in a PS2 NFSMW ISO."""
    arc = PS2NFSMWArchive(iso_path)
    needle = name_filter.lower().strip()

    def matches(path_str: str) -> bool:
        return not needle or needle in path_str.lower()

    abkc_banks: List[AbkcBankInfo] = []
    gnsu_synths: List[GnsuSynthInfo] = []
    schl_containers: List[SchlContainerInfo] = []

    if kind in ("all", "abkc"):
        for e in arc.entries:
            if e.magic_hex != "41424b43":
                continue
            label = e.resolved_path or f"<hash:0x{e.name_hash:08x}>"
            if not matches(label):
                continue
            data = arc.read_entry(e)
            abkc_banks.append(parse_abkc(data, path=label, name_hash=e.name_hash))

    if kind in ("all", "gnsu"):
        for e in arc.entries:
            if e.magic_hex != "476e7375":
                continue
            label = e.resolved_path or f"<hash:0x{e.name_hash:08x}>"
            if not matches(label):
                continue
            data = arc.read_entry(e)
            gnsu_synths.append(parse_gnsu(data, path=label, name_hash=e.name_hash))

    if kind in ("all", "schl"):
        with iso_path.open("rb") as f:
            for e in arc.entries:
                if not e.resolved_path or not e.resolved_path.upper().endswith(".BIG"):
                    continue
                if not matches(e.resolved_path):
                    continue
                idx_path = e.resolved_path[:-4] + ".IDX"
                try:
                    idx_entry = arc.find(idx_path)
                except KeyError:
                    continue
                offs = parse_idx_offsets(arc.read_entry(idx_entry))
                entries = [(off, None) for off in offs]
                schl_containers.append(
                    inspect_schl_stream_table(
                        path=e.resolved_path,
                        container_type="BIG+IDX",
                        byte_size=e.byte_size,
                        entries=entries,
                        read_slice=_iso_slice_reader(f, arc, e),
                        max_streams=max_streams,
                    )
                )

            for e in arc.entries:
                if not e.resolved_path or not e.resolved_path.upper().endswith(".MUS"):
                    continue
                if not matches(e.resolved_path):
                    continue
                mpf_path = e.resolved_path[:-4] + ".MPF"
                try:
                    mpf_entry = arc.find(mpf_path)
                except KeyError:
                    continue
                pairs = parse_mpf_stream_table(arc.read_entry(mpf_entry))
                entries = [(off, dur_ms) for off, dur_ms in pairs]
                schl_containers.append(
                    inspect_schl_stream_table(
                        path=e.resolved_path,
                        container_type="MUS+MPF",
                        byte_size=e.byte_size,
                        entries=entries,
                        read_slice=_iso_slice_reader(f, arc, e),
                        max_streams=max_streams,
                    )
                )

            if include_mpc:
                for e in arc.entries:
                    if e.magic_hex != "4d504368":
                        continue
                    label = e.resolved_path or f"<hash:0x{e.name_hash:08x}>"
                    if not matches(label):
                        continue
                    info = inspect_mpc_header(label, e.byte_size, _iso_slice_reader(f, arc, e))
                    if info is not None:
                        schl_containers.append(info)

    abkc_sample_rates = sorted({sr for b in abkc_banks for sr in b.sample_rates})
    gnsu_sample_rates = sorted({g.sample_rate for g in gnsu_synths})
    schl_sample_rates = sorted({sr for c in schl_containers for sr in c.sample_rates})

    return {
        "source": str(iso_path),
        "summary": {
            "abkc_bank_count": len(abkc_banks),
            "abkc_resolved_count": sum(1 for b in abkc_banks if not b.path.startswith("<hash:")),
            "abkc_total_slots": sum(b.slot_count for b in abkc_banks),
            "abkc_active_samples": sum(b.active_samples for b in abkc_banks),
            "abkc_sample_rates": abkc_sample_rates,
            "gnsu_synth_count": len(gnsu_synths),
            "gnsu_resolved_count": sum(1 for g in gnsu_synths if not g.path.startswith("<hash:")),
            "gnsu_sample_rates": gnsu_sample_rates,
            "gnsu_accel_count": sum(1 for g in gnsu_synths if g.direction == "accel"),
            "gnsu_decel_count": sum(1 for g in gnsu_synths if g.direction == "decel"),
            "schl_container_count": len(schl_containers),
            "schl_total_streams": sum(c.stream_count for c in schl_containers),
            "schl_big_streams": sum(c.stream_count for c in schl_containers if c.container_type == "BIG+IDX"),
            "schl_mus_segments": sum(c.stream_count for c in schl_containers if c.container_type == "MUS+MPF"),
            "schl_mpc_tracks": sum(c.stream_count for c in schl_containers if c.container_type == "MPC"),
            "schl_sample_rates": schl_sample_rates,
        },
        "abkc_banks": abkc_banks,
        "gnsu_synths": gnsu_synths,
        "schl_containers": schl_containers,
    }


def inspect_standalone_file(
    file_path: Path,
    mpf_path: Optional[Path] = None,
    idx_path: Optional[Path] = None,
    max_streams: int = 0,
) -> Dict[str, Any]:
    """Inspect a standalone extracted .ABK, .GIN, .MUS (+MPF), .BIG (+IDX), .MPC, or raw SCHl file."""
    size = file_path.stat().st_size
    with file_path.open("rb") as f:
        head = f.read(16)

        def read_slice(off: int, length: int) -> bytes:
            f.seek(off)
            return f.read(length)

        abkc_banks: List[AbkcBankInfo] = []
        gnsu_synths: List[GnsuSynthInfo] = []
        schl_containers: List[SchlContainerInfo] = []

        if head[:4] == ABKC_MAGIC:
            abkc_banks.append(parse_abkc(file_path.read_bytes(), path=str(file_path)))
        elif head[:4] == GNSU_MAGIC:
            gnsu_synths.append(parse_gnsu(file_path.read_bytes(), path=str(file_path)))
        elif mpf_path is not None or file_path.suffix.lower() == ".mus":
            mpf_file = mpf_path or file_path.with_suffix(".MPF")
            if not mpf_file.is_file():
                mpf_file = file_path.with_suffix(".mpf")
            pairs = parse_mpf_stream_table(mpf_file.read_bytes())
            schl_containers.append(
                inspect_schl_stream_table(
                    path=str(file_path),
                    container_type="MUS+MPF",
                    byte_size=size,
                    entries=[(o, d) for o, d in pairs],
                    read_slice=read_slice,
                    max_streams=max_streams,
                )
            )
        elif idx_path is not None or (head[:4] == SCHL_MAGIC and file_path.suffix.lower() == ".big"):
            idx_file = idx_path or file_path.with_suffix(".IDX")
            if not idx_file.is_file():
                idx_file = file_path.with_suffix(".idx")
            if idx_file.is_file():
                offs = parse_idx_offsets(idx_file.read_bytes())
                entries: List[Tuple[int, Optional[int]]] = [(o, None) for o in offs]
                ctype = "BIG+IDX"
            else:
                entries = [(0, None)]
                ctype = "SCHl"
            schl_containers.append(
                inspect_schl_stream_table(
                    path=str(file_path),
                    container_type=ctype,
                    byte_size=size,
                    entries=entries,
                    read_slice=read_slice,
                    max_streams=max_streams,
                )
            )
        elif head[:4] == SCHL_MAGIC:
            schl_containers.append(
                inspect_schl_stream_table(
                    path=str(file_path),
                    container_type="SCHl",
                    byte_size=size,
                    entries=[(0, None)],
                    read_slice=read_slice,
                    max_streams=max_streams,
                )
            )
        elif head[:4] == MPCH_MAGIC:
            info = inspect_mpc_header(str(file_path), size, read_slice)
            if info is not None:
                schl_containers.append(info)
        else:
            raise ValueError(f"Unrecognized PS2 audio file magic {head[:4]!r} in {file_path}")

    return {
        "source": str(file_path),
        "summary": {
            "abkc_bank_count": len(abkc_banks),
            "abkc_active_samples": sum(b.active_samples for b in abkc_banks),
            "gnsu_synth_count": len(gnsu_synths),
            "schl_container_count": len(schl_containers),
            "schl_total_streams": sum(c.stream_count for c in schl_containers),
        },
        "abkc_banks": abkc_banks,
        "gnsu_synths": gnsu_synths,
        "schl_containers": schl_containers,
    }


def _print_summary(report: Dict[str, Any], show_samples: bool = False, limit: int = 30) -> None:
    s = report["summary"]
    print(f"PS2 NFSMW Audio Inspection: {report['source']}")
    if "abkc_bank_count" in s and s["abkc_bank_count"]:
        print(
            f"  ABKC Banks  : {s['abkc_bank_count']} banks "
            f"({s.get('abkc_resolved_count', s['abkc_bank_count'])} resolved), "
            f"{s['abkc_active_samples']} active samples ({s.get('abkc_total_slots', s['abkc_active_samples'])} slots)"
        )
        if s.get("abkc_sample_rates"):
            srs = s["abkc_sample_rates"]
            print(f"    Sample Rates: {srs[0]:,}..{srs[-1]:,} Hz ({len(srs)} distinct)")
    if "gnsu_synth_count" in s and s["gnsu_synth_count"]:
        print(
            f"  Gnsu Synths : {s['gnsu_synth_count']} synths "
            f"({s.get('gnsu_resolved_count', s['gnsu_synth_count'])} resolved; "
            f"accel={s.get('gnsu_accel_count', 0)}, decel={s.get('gnsu_decel_count', 0)})"
        )
        if s.get("gnsu_sample_rates"):
            print(f"    Sample Rates: {', '.join(str(x) + ' Hz' for x in s['gnsu_sample_rates'])}")
    if "schl_container_count" in s and s["schl_container_count"]:
        print(
            f"  SCHl Streams: {s['schl_container_count']} containers, {s['schl_total_streams']} streams "
            f"(BIG={s.get('schl_big_streams', 0)}, MUS={s.get('schl_mus_segments', 0)}, MPC={s.get('schl_mpc_tracks', 0)})"
        )

    abkc_banks: List[AbkcBankInfo] = report["abkc_banks"]
    if abkc_banks:
        print(f"\nABKC Banks (showing {min(len(abkc_banks), limit)}/{len(abkc_banks)}):")
        for b in abkc_banks[:limit]:
            srs = "/".join(str(x) for x in b.sample_rates[:4])
            if len(b.sample_rates) > 4:
                srs += "/..."
            chs = ",".join(f"{c}ch" for c in b.channels)
            print(
                f"  {b.path:42s}  {b.byte_size:7,d} B  samples={b.active_samples:2d}/{b.slot_count:2d}  "
                f"ch={chs:5s}  dur={b.total_duration_sec:6.2f}s  sr=[{srs}]"
            )
            if show_samples:
                for smp in b.samples:
                    print(
                        f"    [{smp.slot_index:2d}] @0x{smp.pt_offset:04x}  {smp.channels}ch  "
                        f"{smp.sample_rate or 0:5d} Hz  {smp.num_samples or 0:7d} samp  "
                        f"dur={smp.duration_sec or 0:.3f}s  {smp.codec_name}"
                    )

    gnsu_synths: List[GnsuSynthInfo] = report["gnsu_synths"]
    if gnsu_synths:
        print(f"\nGnsu Synths (showing {min(len(gnsu_synths), limit)}/{len(gnsu_synths)}):")
        for g in gnsu_synths[:limit]:
            print(
                f"  {g.path:42s}  {g.byte_size:7,d} B  v{g.version}  "
                f"RPM={g.min_rpm:6.1f}..{g.max_rpm:6.1f} ({g.direction:5s})  "
                f"grains={g.num_grains:3d}  {g.sample_rate:5d} Hz  dur={g.duration_sec:.2f}s"
            )

    schl_containers: List[SchlContainerInfo] = report["schl_containers"]
    if schl_containers:
        print(f"\nSCHl Containers ({len(schl_containers)}):")
        for c in schl_containers:
            srs = ",".join(str(x) for x in c.sample_rates) or "default"
            chs = ",".join(f"{ch}ch" for ch in c.channels)
            dur_s = f"{c.total_duration_sec:8.2f}s" if c.total_duration_sec is not None else "   unknown"
            print(
                f"  {c.path:42s}  [{c.container_type:7s}]  {c.byte_size:11,d} B  "
                f"streams={c.stream_count:4d}  ch={chs:5s}  sr={srs:7s}  dur={dur_s}  "
                f"codecs={', '.join(c.codecs)}"
            )
            if show_samples:
                for st in c.streams[:limit]:
                    sdur = f"{st.duration_sec:.3f}s" if st.duration_sec is not None else "?"
                    print(
                        f"    [{st.index:3d}] @0x{st.offset:08x}  {st.pt_kind:4s}  {st.channels}ch  "
                        f"{st.sample_rate or 0:5d} Hz  {st.num_samples or 0:8d} samp  dur={sdur}  {st.codec_name}"
                    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only PS2 NFSMW audio inspector (ABKC banks, Gnsu synths, SCHl streams)."
    )
    parser.add_argument("source", nargs="?", type=Path, default=None, help="Path to PS2 .iso or extracted audio file")
    parser.add_argument("--iso", type=Path, default=None, help="Path to PS2 .iso (strictly read-only 'rb')")
    parser.add_argument("--in", dest="in_file", type=Path, default=None, help="Path to standalone .ABK/.GIN/.MUS/.BIG/.MPC")
    parser.add_argument("--mpf", type=Path, default=None, help="Companion .MPF for standalone .MUS")
    parser.add_argument("--idx", type=Path, default=None, help="Companion .IDX for standalone .BIG")
    parser.add_argument(
        "--kind",
        choices=("all", "abkc", "gnsu", "schl"),
        default="all",
        help="Filter ISO audit by audio container type",
    )
    parser.add_argument("--filter", type=str, default="", help="Case-insensitive substring filter on virtual path")
    parser.add_argument("--samples", action="store_true", help="Include per-sample / per-stream entries in output")
    parser.add_argument("--max-streams", type=int, default=0, help="Max SCHl sub-streams to parse per .BIG/.MUS (0=all)")
    parser.add_argument("--no-mpc", action="store_true", help="Skip MOVIES\\*.MPC FMV SCHl inspection in ISO mode")
    parser.add_argument("--limit", type=int, default=30, help="Max rows per section in summary text output")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    source = args.iso or args.in_file or args.source
    if source is None:
        parser.error("Specify a source .iso or standalone audio file (positional, --iso, or --in).")

    if source.suffix.lower() == ".iso":
        report = inspect_iso(
            source,
            kind=args.kind,
            name_filter=args.filter,
            max_streams=args.max_streams,
            include_mpc=not args.no_mpc,
        )
    else:
        report = inspect_standalone_file(
            source,
            mpf_path=args.mpf,
            idx_path=args.idx,
            max_streams=args.max_streams,
        )

    if args.json:
        payload = {
            "source": report["source"],
            "summary": report["summary"],
            "abkc_banks": [b.to_dict(include_samples=args.samples) for b in report["abkc_banks"]],
            "gnsu_synths": [g.to_dict() for g in report["gnsu_synths"]],
            "schl_containers": [c.to_dict(include_streams=args.samples) for c in report["schl_containers"]],
        }
        print(json.dumps(payload, indent=2))
    else:
        _print_summary(report, show_samples=args.samples, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
