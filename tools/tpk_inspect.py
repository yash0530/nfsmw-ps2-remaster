#!/usr/bin/env python3
"""tpk_inspect.py — Read-only PS2 EA Black Box (0xB3300000) TPK Texture Container Inspector.

Inspects Need for Speed: Most Wanted (PS2, SLUS-21351) TPK texture archives either
directly from an untouched ISO (strictly read-only 'rb' via zdir_tool.PS2NFSMWArchive)
or from a standalone .BIN/.BUN/.TPK file.

Supported TPK layouts:
  1. Uncompressed TPKs (0x33310004 TextureInfo + 0x33310005 PS2 GS TexturePlatInfo),
     e.g. GLOBAL\\HUDTEXRACE.BIN, GLOBAL\\GLOBALB.BUN, GLOBAL\\INGAMEB.BUN.
  2. Streamed/Compressed TPKs (0x33310003 stream table pointing to per-texture HUFF
     [EA 0x30FB/0x32FB/0x34FB Huffman] or JDLZ v2 blocks whose trailing 208 bytes hold
     TextureInfo [124 B] + TexturePlatInfo [84 B]),
     e.g. CARS\\TEXTURES.BIN, CARS\\WHEELS\\TEXTURES.BIN, GLOBAL\\DYNTEX.BIN.

Pure Python 3 standard library (zero third-party dependencies).
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# EA Black Box TPK chunk IDs
CHUNK_PADDING = 0x00000000
CHUNK_TPK_ROOT = 0xB3300000
CHUNK_TPK_INFO = 0xB3310000
CHUNK_TPK_HEADER = 0x33310001
CHUNK_TPK_HASH_TABLE = 0x33310002
CHUNK_TPK_STREAM_TABLE = 0x33310003
CHUNK_TPK_TEX_INFO = 0x33310004
CHUNK_TPK_TEX_PLAT = 0x33310005
CHUNK_TPK_DATA = 0xB3320000
CHUNK_TPK_DATA_INFO = 0x33320001
CHUNK_TPK_DATA_PAYLOAD = 0x33320002

TEXTURE_INFO_SIZE = 124
TEXTURE_PLAT_SIZE = 84
STREAM_ENTRY_SIZE = 24
TPK_HEADER_SIZE = 124

DEFAULT_ISO_TARGETS: Tuple[str, ...] = (
    r"GLOBAL\HUDTEXRACE.BIN",
    r"CARS\TEXTURES.BIN",
    r"CARS\WHEELS\TEXTURES.BIN",
    r"GLOBAL\DYNTEX.BIN",
)

# PlayStation 2 Graphics Synthesizer Pixel Storage Mode (PSM) definitions
PS2_GS_PSM_NAMES: Dict[int, str] = {
    0x00: "PSMCT32",
    0x01: "PSMCT24",
    0x02: "PSMCT16",
    0x0A: "PSMCT16S",
    0x13: "PSMT8",
    0x14: "PSMT4",
    0x1B: "PSMT8H",
    0x24: "PSMT4HL",
    0x2C: "PSMT4HH",
}

PS2_GS_PSM_BPP: Dict[int, int] = {
    0x00: 32,
    0x01: 24,
    0x02: 16,
    0x0A: 16,
    0x13: 8,
    0x14: 4,
    0x1B: 8,
    0x24: 4,
    0x2C: 4,
}


def ps2_psm_name(psm: Optional[int]) -> str:
    """Return canonical PS2 GS Pixel Storage Mode name (e.g. PSMT4, PSMT8, PSMCT32)."""
    if psm is None:
        return "UNKNOWN"
    return PS2_GS_PSM_NAMES.get(psm, f"PSM_0x{psm:02X}")


# ---------------------------------------------------------------------------
# Pure-Python EA Black Box Decompressors (JDLZ v2 & EA HUFF 0x30FB/32FB/34FB)
# ---------------------------------------------------------------------------


def decompress_jdlz(buf: bytes) -> bytes:
    """Decompress an EA Black Box JDLZ v2 compressed block."""
    if len(buf) < 16 or buf[:4] != b"JDLZ":
        raise ValueError("Invalid JDLZ magic")
    version = buf[4]
    if version != 2:
        raise ValueError(f"Unsupported JDLZ version: {version}")
    hdr_len = buf[5]
    if hdr_len < 16 or hdr_len > len(buf):
        raise ValueError(f"Invalid JDLZ header length: {hdr_len}")

    out_len = struct.unpack_from("<I", buf, 8)[0]
    out = bytearray(out_len)
    in_pos = hdr_len
    out_pos = 0
    flags1 = 1
    flags2 = 1
    buf_len = len(buf)

    while out_pos < out_len and in_pos < buf_len:
        if flags1 == 1:
            flags1 = buf[in_pos] | 0x100
            in_pos += 1
        if flags2 == 1:
            if in_pos >= buf_len:
                break
            flags2 = buf[in_pos] | 0x100
            in_pos += 1

        if flags1 & 1:
            if in_pos + 1 >= buf_len:
                break
            b0 = buf[in_pos]
            b1 = buf[in_pos + 1]
            in_pos += 2
            if flags2 & 1:
                length = (b1 | ((b0 & 0xF0) << 4)) + 3
                dist = (b0 & 0x0F) + 1
            else:
                dist = (b1 | ((b0 & 0xE0) << 3)) + 17
                length = (b0 & 0x1F) + 3
            if dist > out_pos:
                raise ValueError(f"JDLZ back-reference out of range: dist={dist}, pos={out_pos}")
            end = min(out_pos + length, out_len)
            if dist >= length:
                out[out_pos:end] = out[out_pos - dist : out_pos - dist + (end - out_pos)]
                out_pos = end
            else:
                while out_pos < end:
                    out[out_pos] = out[out_pos - dist]
                    out_pos += 1
            flags2 >>= 1
        else:
            if in_pos >= buf_len:
                break
            out[out_pos] = buf[in_pos]
            in_pos += 1
            out_pos += 1
        flags1 >>= 1

    return bytes(out)


def decompress_ea_huff(buf: bytes) -> bytes:
    """Decompress an EA HUFF block (optional 16-byte HUFF wrapper + 0x30FB/0x32FB/0x34FB)."""
    if len(buf) >= 16 and buf[:4] == b"HUFF":
        hdr_len = buf[5]
        if hdr_len < 16 or hdr_len > len(buf):
            raise ValueError(f"Invalid HUFF header length: {hdr_len}")
        src = buf[hdr_len:]
    else:
        src = buf

    src_len = len(src)
    if src_len < 6:
        raise ValueError("EA HUFF buffer truncated")

    bit_buf = 0
    bit_cnt = 0
    byte_pos = 0

    def get_bits(n: int) -> int:
        nonlocal bit_buf, bit_cnt, byte_pos
        while bit_cnt < n:
            b = src[byte_pos] if byte_pos < src_len else 0
            byte_pos += 1
            bit_buf = ((bit_buf << 8) | b) & 0xFFFFFFFFFFFFFFFF
            bit_cnt += 8
        bit_cnt -= n
        return (bit_buf >> bit_cnt) & ((1 << n) - 1)

    def get_var() -> int:
        z = 0
        while get_bits(1) == 0:
            z += 1
            if z > 24:
                raise ValueError("Corrupt EA HUFF variable-length integer")
        nb = z + 2
        return ((1 << nb) - 4) + get_bits(nb)

    method = get_bits(16)
    if (method & 0x00FF) != 0xFB or ((method >> 8) & 0xF0) != 0x30:
        raise ValueError(f"Unsupported EA HUFF method word: 0x{method:04X}")
    if method & 0x0100:
        _ = get_bits(24)
    dst_size = get_bits(32) if (method & 0x8000) else get_bits(24)
    esc = get_bits(8)

    numcodes = [0] * 17
    hw = 0
    hc = 0
    tot = 0
    while hw < 16 and (hc << (16 - hw)) < 0x10000:
        num = get_var()
        hw += 1
        numcodes[hw] = num
        tot += num
        hc = (hc * 2) + num
        if tot > 256:
            raise ValueError("EA HUFF symbol count exceeds 256")

    if tot == 0 or (hc << (16 - hw)) != 0x10000:
        raise ValueError("Incomplete or corrupt EA HUFF canonical tree")

    data_values: List[int] = []
    defined = [False] * 256
    dat = 0xFF
    for _ in range(tot):
        skip = get_var() + 1
        while skip > 0:
            dat = (dat + 1) & 0xFF
            if not defined[dat]:
                skip -= 1
        defined[dat] = True
        data_values.append(dat)

    max_w = hw
    max_code = [0] * (max_w + 1)
    base_idx = [0] * (max_w + 1)
    code = 0
    idx = 0
    for w in range(1, max_w + 1):
        max_code[w] = code + numcodes[w]
        base_idx[w] = idx - code
        idx += numcodes[w]
        code = (code + numcodes[w]) << 1

    # 10-bit fast prefix lookup table covers ~98% of symbols in O(1)
    qbits = 10
    qmask = (1 << qbits) - 1
    quick_sym = [-1] * (1 << qbits)
    quick_len = [0] * (1 << qbits)
    code = 0
    idx = 0
    for w in range(1, min(qbits, max_w) + 1):
        cnt = numcodes[w]
        shift = qbits - w
        span = 1 << shift
        for k in range(cnt):
            s = data_values[idx]
            idx += 1
            start = (code + k) << shift
            quick_sym[start : start + span] = [s] * span
            quick_len[start : start + span] = [w] * span
        code = (code + cnt) << 1

    out = bytearray()
    out_append = out.append
    out_extend = out.extend
    while len(out) < dst_size:
        while bit_cnt < 24:
            b = src[byte_pos] if byte_pos < src_len else 0
            byte_pos += 1
            bit_buf = ((bit_buf << 8) | b) & 0xFFFFFFFFFFFFFFFF
            bit_cnt += 8
        peek = (bit_buf >> (bit_cnt - qbits)) & qmask
        sym = quick_sym[peek]
        if sym >= 0:
            bit_cnt -= quick_len[peek]
        else:
            c = peek
            bit_cnt -= qbits
            sym = -1
            for w in range(qbits + 1, max_w + 1):
                bit_cnt -= 1
                c = (c << 1) | ((bit_buf >> bit_cnt) & 1)
                if c < max_code[w]:
                    sym = data_values[base_idx[w] + c]
                    break
            if sym < 0:
                raise ValueError("Invalid EA HUFF code in bitstream")

        if sym != esc:
            out_append(sym)
        else:
            num = get_var()
            if num == 0:
                if get_bits(1) == 1:
                    break
                out_append(get_bits(8))
            else:
                rep = out[-1] if out else 0
                out_extend(bytes([rep]) * num)

    # Apply post-processing delta unfiltering for 0x32FB (1D) and 0x34FB (2D)
    delta_mode = (method >> 8) & 0x06
    if delta_mode == 2:
        acc = 0
        for i in range(len(out)):
            acc = (acc + out[i]) & 0xFF
            out[i] = acc
    elif delta_mode == 4:
        acc1 = 0
        acc2 = 0
        for i in range(len(out)):
            acc1 = (acc1 + out[i]) & 0xFF
            acc2 = (acc2 + acc1) & 0xFF
            out[i] = acc2

    return bytes(out)


def decompress_ea_block(buf: bytes) -> Tuple[bytes, str]:
    """Decompress a single EA per-texture block (HUFF, JDLZ, or RAW)."""
    if len(buf) >= 4:
        magic = buf[:4]
        if magic == b"HUFF":
            return decompress_ea_huff(buf), "HUFF"
        if magic == b"JDLZ":
            return decompress_jdlz(buf), "JDLZ"
    return buf, "RAW"


# ---------------------------------------------------------------------------
# TPK Data Structures & Binary Parser
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TPKTextureEntry:
    index: int
    name: str
    name_hash: int
    class_name_hash: int
    width: int
    height: int
    mip_count: int
    ps2_psm: Optional[int]
    ps2_psm_name: str
    bpp: int
    image_compression_type: int
    palette_entries: int
    image_size: int
    palette_size: int
    image_offset: int
    palette_offset: int
    storage: str  # "uncompressed", "HUFF", "JDLZ", "streamed_skipped"
    compressed_size: Optional[int] = None
    uncompressed_size: Optional[int] = None
    stream_offset: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["name_hash_hex"] = f"0x{self.name_hash:08X}"
        d["class_name_hash_hex"] = f"0x{self.class_name_hash:08X}"
        d["ps2_psm_hex"] = f"0x{self.ps2_psm:02X}" if self.ps2_psm is not None else None
        return d


@dataclasses.dataclass
class TPKContainer:
    source: str
    tpk_index: int
    chunk_offset: int
    chunk_size: int
    version: int
    pack_name: str
    filename: str
    filename_hash: int
    layout: str  # "uncompressed" or "streamed"
    hash_count: int
    textures: List[TPKTextureEntry]
    data_payload_size: int = 0
    decode_errors: List[str] = dataclasses.field(default_factory=list)

    def format_counts(self) -> Dict[str, int]:
        counts: collections.Counter = collections.Counter(t.ps2_psm_name for t in self.textures)
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def resolution_counts(self) -> Dict[str, int]:
        counts: collections.Counter = collections.Counter(f"{t.width}x{t.height}" for t in self.textures)
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "tpk_index": self.tpk_index,
            "chunk_offset": self.chunk_offset,
            "chunk_size": self.chunk_size,
            "version": self.version,
            "pack_name": self.pack_name,
            "filename": self.filename,
            "filename_hash": f"0x{self.filename_hash:08X}",
            "layout": self.layout,
            "hash_count": self.hash_count,
            "texture_count": len(self.textures),
            "data_payload_size": self.data_payload_size,
            "format_counts": self.format_counts(),
            "resolution_counts": self.resolution_counts(),
            "decode_errors": list(self.decode_errors),
            "textures": [t.to_dict() for t in self.textures],
        }


def _decode_cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def _parse_texture_info_and_plat(
    index: int,
    info_bytes: bytes,
    plat_bytes: Optional[bytes],
    storage: str,
    compressed_size: Optional[int] = None,
    uncompressed_size: Optional[int] = None,
    stream_offset: Optional[int] = None,
) -> TPKTextureEntry:
    if len(info_bytes) < TEXTURE_INFO_SIZE:
        raise ValueError(f"TextureInfo record truncated ({len(info_bytes)} < {TEXTURE_INFO_SIZE})")

    name = _decode_cstr(info_bytes[12:36])
    (
        name_hash,
        class_name_hash,
        _parent_hash,
        image_offset,
        palette_offset,
        image_size,
        palette_size,
        _base_image_size,
    ) = struct.unpack_from("<8I", info_bytes, 36)

    (
        width,
        height,
        _shift_w,
        _shift_h,
        image_cmp,
        _pal_cmp,
        num_pal_entries,
        num_mips,
    ) = struct.unpack_from("<HHBBBBHB", info_bytes, 68)

    ps2_psm: Optional[int] = None
    if plat_bytes is not None and len(plat_bytes) >= 24:
        ps2_psm = plat_bytes[4]
        # Cross-check width/height fallback if header dims were 0
        if width == 0 or height == 0:
            gs_w, gs_h = struct.unpack_from("<HH", plat_bytes, 16)
            width = width or gs_w
            height = height or gs_h

    # Infer PSM from image_compression_type if TexturePlatInfo wasn't present
    if ps2_psm is None:
        if image_cmp == 0x04:
            ps2_psm = 0x14
        elif image_cmp in (0x08, 0x80):
            ps2_psm = 0x13
        elif image_cmp == 0x20:
            ps2_psm = 0x00

    psm_str = ps2_psm_name(ps2_psm)
    bpp = PS2_GS_PSM_BPP.get(ps2_psm if ps2_psm is not None else -1, 0)

    return TPKTextureEntry(
        index=index,
        name=name,
        name_hash=name_hash,
        class_name_hash=class_name_hash,
        width=int(width),
        height=int(height),
        mip_count=max(1, int(num_mips)),
        ps2_psm=ps2_psm,
        ps2_psm_name=psm_str,
        bpp=bpp,
        image_compression_type=int(image_cmp),
        palette_entries=int(num_pal_entries),
        image_size=int(image_size),
        palette_size=int(palette_size),
        image_offset=int(image_offset),
        palette_offset=int(palette_offset),
        storage=storage,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        stream_offset=stream_offset,
    )


def _iter_ea_chunks(buf: bytes, start: int, end: int) -> List[Tuple[int, int, int]]:
    """Iterate EA chunk headers (chunk_id, payload_start, payload_size) within [start, end)."""
    out: List[Tuple[int, int, int]] = []
    pos = start
    while pos + 8 <= end:
        cid, csize = struct.unpack_from("<II", buf, pos)
        payload_start = pos + 8
        if payload_start + csize > end:
            break
        out.append((cid, payload_start, csize))
        pos = payload_start + csize
    return out


def parse_tpk_chunk(
    full_buffer: bytes,
    chunk_offset: int,
    payload_size: int,
    source: str = "<memory>",
    tpk_index: int = 0,
    decompress_streamed: bool = True,
) -> TPKContainer:
    """Parse a single 0xB3300000 TPK container chunk."""
    chunk_end = chunk_offset + 8 + payload_size
    top_children = _iter_ea_chunks(full_buffer, chunk_offset + 8, chunk_end)

    version = 0
    pack_name = ""
    filename = ""
    filename_hash = 0
    hashes: List[int] = []
    stream_entries: List[Tuple[int, int, int, int]] = []
    tex_info_raw = b""
    tex_plat_raw = b""
    data_payload_size = 0

    for cid, pstart, psize in top_children:
        if cid == CHUNK_TPK_INFO:
            info_children = _iter_ea_chunks(full_buffer, pstart, pstart + psize)
            for scid, spstart, spsize in info_children:
                sub = full_buffer[spstart : spstart + spsize]
                if scid == CHUNK_TPK_HEADER and len(sub) >= 100:
                    version = struct.unpack_from("<I", sub, 0)[0]
                    pack_name = _decode_cstr(sub[4:32])
                    filename = _decode_cstr(sub[32:96])
                    filename_hash = struct.unpack_from("<I", sub, 96)[0]
                elif scid == CHUNK_TPK_HASH_TABLE:
                    count = len(sub) // 8
                    hashes = [struct.unpack_from("<I", sub, i * 8)[0] for i in range(count)]
                elif scid == CHUNK_TPK_STREAM_TABLE:
                    count = len(sub) // STREAM_ENTRY_SIZE
                    for i in range(count):
                        thash, off, csize, usize, _f1, _f2 = struct.unpack_from(
                            "<6I", sub, i * STREAM_ENTRY_SIZE
                        )
                        stream_entries.append((thash, off, csize, usize))
                elif scid == CHUNK_TPK_TEX_INFO:
                    tex_info_raw = sub
                elif scid == CHUNK_TPK_TEX_PLAT:
                    tex_plat_raw = sub
        elif cid == CHUNK_TPK_DATA:
            data_children = _iter_ea_chunks(full_buffer, pstart, pstart + psize)
            for scid, _spstart, spsize in data_children:
                if scid == CHUNK_TPK_DATA_PAYLOAD:
                    data_payload_size = spsize

    textures: List[TPKTextureEntry] = []
    decode_errors: List[str] = []

    if tex_info_raw:
        layout = "uncompressed"
        n_info = len(tex_info_raw) // TEXTURE_INFO_SIZE
        n_plat = len(tex_plat_raw) // TEXTURE_PLAT_SIZE if tex_plat_raw else 0
        for i in range(n_info):
            ti = tex_info_raw[i * TEXTURE_INFO_SIZE : (i + 1) * TEXTURE_INFO_SIZE]
            tp = tex_plat_raw[i * TEXTURE_PLAT_SIZE : (i + 1) * TEXTURE_PLAT_SIZE] if i < n_plat else None
            textures.append(_parse_texture_info_and_plat(i, ti, tp, storage="uncompressed"))
    elif stream_entries:
        layout = "streamed"
        for i, (thash, off, csize, usize) in enumerate(stream_entries):
            abs_off = chunk_offset + off
            if not decompress_streamed:
                textures.append(
                    TPKTextureEntry(
                        index=i,
                        name=f"<streamed:0x{thash:08X}>",
                        name_hash=thash,
                        class_name_hash=0,
                        width=0,
                        height=0,
                        mip_count=1,
                        ps2_psm=None,
                        ps2_psm_name="COMPRESSED",
                        bpp=0,
                        image_compression_type=0,
                        palette_entries=0,
                        image_size=0,
                        palette_size=0,
                        image_offset=0,
                        palette_offset=0,
                        storage="streamed_skipped",
                        compressed_size=csize,
                        uncompressed_size=usize,
                        stream_offset=off,
                    )
                )
                continue

            if abs_off + csize > len(full_buffer):
                decode_errors.append(
                    f"Entry #{i} (0x{thash:08X}): stream offset 0x{abs_off:X}+{csize} out of bounds"
                )
                continue

            blk = full_buffer[abs_off : abs_off + csize]
            try:
                raw, codec = decompress_ea_block(blk)
                if len(raw) < TEXTURE_INFO_SIZE + TEXTURE_PLAT_SIZE:
                    raise ValueError(f"Decompressed block too small ({len(raw)} bytes)")
                ti = raw[-(TEXTURE_INFO_SIZE + TEXTURE_PLAT_SIZE) : -TEXTURE_PLAT_SIZE]
                tp = raw[-TEXTURE_PLAT_SIZE:]
                entry = _parse_texture_info_and_plat(
                    i,
                    ti,
                    tp,
                    storage=codec,
                    compressed_size=csize,
                    uncompressed_size=usize,
                    stream_offset=off,
                )
                if entry.name_hash != thash:
                    decode_errors.append(
                        f"Entry #{i}: header hash 0x{entry.name_hash:08X} != table hash 0x{thash:08X}"
                    )
                textures.append(entry)
            except Exception as exc:
                decode_errors.append(f"Entry #{i} (0x{thash:08X}): {exc}")
    else:
        layout = "empty"

    return TPKContainer(
        source=source,
        tpk_index=tpk_index,
        chunk_offset=chunk_offset,
        chunk_size=payload_size + 8,
        version=version,
        pack_name=pack_name,
        filename=filename,
        filename_hash=filename_hash,
        layout=layout,
        hash_count=len(hashes),
        textures=textures,
        data_payload_size=data_payload_size,
        decode_errors=decode_errors,
    )


def parse_tpk_bytes(
    data: bytes,
    source: str = "<memory>",
    decompress_streamed: bool = True,
) -> List[TPKContainer]:
    """Scan a binary buffer (BIN, BUN, or TPK) and parse all top-level 0xB3300000 TPK chunks."""
    containers: List[TPKContainer] = []
    pos = 0
    end = len(data)
    tpk_idx = 0
    while pos + 8 <= end:
        cid, csize = struct.unpack_from("<II", data, pos)
        if pos + 8 + csize > end:
            break
        if cid == CHUNK_TPK_ROOT:
            containers.append(
                parse_tpk_chunk(
                    full_buffer=data,
                    chunk_offset=pos,
                    payload_size=csize,
                    source=source,
                    tpk_index=tpk_idx,
                    decompress_streamed=decompress_streamed,
                )
            )
            tpk_idx += 1
        pos += 8 + csize
    return containers


def inspect_iso_tpks(
    iso_path: Path,
    targets: Sequence[str] = DEFAULT_ISO_TARGETS,
    decompress_streamed: bool = True,
) -> List[TPKContainer]:
    """Read-only inspect one or more virtual TPK archives directly from a PS2 NFSMW ISO."""
    tools_dir = str(Path(__file__).resolve().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from zdir_tool import PS2NFSMWArchive  # local stdlib module

    arc = PS2NFSMWArchive(iso_path)
    results: List[TPKContainer] = []
    for vpath in targets:
        entry = arc.find(vpath)
        raw = arc.read_entry(entry)
        containers = parse_tpk_bytes(
            raw,
            source=vpath,
            decompress_streamed=decompress_streamed,
        )
        results.extend(containers)
    return results


# ---------------------------------------------------------------------------
# CLI Reporting & Entrypoint
# ---------------------------------------------------------------------------


def _filter_textures(textures: List[TPKTextureEntry], query: str) -> List[TPKTextureEntry]:
    if not query:
        return textures
    needle = query.lower()
    return [
        t
        for t in textures
        if needle in t.name.lower()
        or needle in f"{t.name_hash:08x}"
        or needle in t.ps2_psm_name.lower()
        or needle in f"{t.width}x{t.height}"
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tpk_inspect.py",
        description=(
            "Read-only PS2 EA Black Box (0xB3300000) TPK Texture Container Inspector "
            "for Need for Speed: Most Wanted (SLUS-21351)."
        ),
    )
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument(
        "--iso",
        type=Path,
        help="Path to untouched PS2 NFSMW .iso (opened strictly read-only 'rb').",
    )
    src_group.add_argument(
        "--file",
        type=Path,
        nargs="+",
        help="Path(s) to standalone local .BIN/.BUN/.TPK file(s).",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=list(DEFAULT_ISO_TARGETS),
        help=(
            "Virtual archive paths inside ZDIR.BIN when using --iso "
            f"(default: {', '.join(DEFAULT_ISO_TARGETS)})."
        ),
    )
    parser.add_argument(
        "--filter",
        type=str,
        default="",
        help="Case-insensitive filter substring on texture name, hex hash, PSM format, or WxH.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print per-archive counts and pixel format breakdown without listing every texture.",
    )
    parser.add_argument(
        "--no-decompress",
        action="store_true",
        help="Skip HUFF/JDLZ decompression on streamed TPKs (faster hash/size-only scan).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full catalog as JSON to stdout.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional file path to write JSON catalog (e.g. build/tpk_catalog.json).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    decompress = not args.no_decompress

    containers: List[TPKContainer] = []
    if args.iso is not None:
        iso_path = args.iso.expanduser().resolve()
        if not iso_path.is_file():
            print(f"ERROR: ISO not found: {iso_path}", file=sys.stderr)
            return 1
        containers = inspect_iso_tpks(iso_path, targets=args.targets, decompress_streamed=decompress)
    else:
        for fp in args.file:
            resolved = fp.expanduser().resolve()
            if not resolved.is_file():
                print(f"ERROR: File not found: {resolved}", file=sys.stderr)
                return 1
            containers.extend(
                parse_tpk_bytes(
                    resolved.read_bytes(),
                    source=resolved.name,
                    decompress_streamed=decompress,
                )
            )

    # Apply optional texture filter to displayed view
    filtered_containers: List[TPKContainer] = []
    for c in containers:
        if args.filter:
            ft = _filter_textures(c.textures, args.filter)
            filtered_containers.append(dataclasses.replace(c, textures=ft))
        else:
            filtered_containers.append(c)

    total_textures = sum(len(c.textures) for c in filtered_containers)
    total_formats: collections.Counter = collections.Counter()
    for c in filtered_containers:
        for t in c.textures:
            total_formats[t.ps2_psm_name] += 1

    payload = {
        "schema_version": "1.0",
        "container_count": len(filtered_containers),
        "total_textures": total_textures,
        "format_totals": dict(sorted(total_formats.items(), key=lambda kv: (-kv[1], kv[0]))),
        "containers": [c.to_dict() for c in filtered_containers],
    }

    if args.out:
        out_path = args.out.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(
        f"PS2 TPK Catalog: {len(filtered_containers)} container(s), "
        f"{total_textures} texture(s) | formats={payload['format_totals']}"
    )
    for c in filtered_containers:
        print(
            f"\n[{c.source}] TPK#{c.tpk_index} pack='{c.pack_name}' ver={c.version} "
            f"layout={c.layout} textures={len(c.textures)} (hashes={c.hash_count}) "
            f"formats={c.format_counts()}"
        )
        if c.filename:
            print(f"  source_tpk: {c.filename} (0x{c.filename_hash:08X})")
        if c.decode_errors:
            print(f"  decode_errors: {len(c.decode_errors)}")
            for err in c.decode_errors[:5]:
                print(f"    ! {err}")
        if not args.summary:
            print(
                f"  {'IDX':>4}  {'HASH':<10}  {'SIZE':>9}  {'MIPS':>4}  {'PSM':<9}  "
                f"{'PAL':>4}  {'STORE':<12}  NAME"
            )
            for t in c.textures:
                dim = f"{t.width}x{t.height}" if t.width and t.height else "?"
                print(
                    f"  {t.index:4d}  0x{t.name_hash:08X}  {dim:>9}  {t.mip_count:4d}  "
                    f"{t.ps2_psm_name:<9}  {t.palette_entries:4d}  {t.storage:<12}  {t.name}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
