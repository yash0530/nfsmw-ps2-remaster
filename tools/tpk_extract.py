#!/usr/bin/env python3
"""Pure-Python + NumPy PS2 Graphics Synthesizer TPK texture decoder and extractor.

Decodes embedded PlayStation 2 EA/NFSMW Texture Pack (``0xB3300000``) resources—
both uncompressed packs (such as ``GLOBAL\\HUDTEXRACE.BIN`` and ``GLOBAL\\INGAMEA.BUN``)
and streamed EA/JDLZ/HUFF packs (such as ``GLOBAL\\DYNTEX.BIN``, ``CARS\\TEXTURES.BIN``,
and ``CARS\\WHEELS\\TEXTURES.BIN``)—directly into RGBA PNG images inside a gitignored
workspace directory (default: ``build/tpk_extracted/``).

Supported PS2 GS pixel storage modes (PSM):
- ``0x00`` (``PSMCT32``): 32-bit RGBA8888 (linear or block32-swizzled)
- ``0x01`` (``PSMCT24``): 24-bit RGB888 expanded to opaque RGBA8888
- ``0x02`` / ``0x0A`` (``PSMCT16`` / ``PSMCT16S``): 16-bit ABGR1555 -> RGBA8888
- ``0x13`` (``PSMT8``): 8-bit indexed with CSM1 CLUT swizzle & GS pixel swizzle
- ``0x14`` (``PSMT4``): 4-bit nibble-indexed with GS VRAM block/column unswizzle

The original game ISO is opened strictly read-only (``'rb'`` via ``zdir_tool``)
and never modified.

Usage:
  python3 tools/tpk_extract.py --iso "/path/to/NFSMW.iso" --out build/tpk_extracted
  python3 tools/tpk_extract.py --iso "/path/to/NFSMW.iso" --targets "GLOBAL\\HUDTEXRACE.BIN" "GLOBAL\\DYNTEX.BIN"
  python3 tools/tpk_extract.py --file path/to/extracted_tpk.bin --out build/tpk_extracted
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import tpk_inspect as tpki  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

# PS2 GS Pixel Storage Modes
PS2_PSMCT32 = 0x00
PS2_PSMCT24 = 0x01
PS2_PSMCT16 = 0x02
PS2_PSMCT16S = 0x0A
PS2_PSMT8 = 0x13
PS2_PSMT4 = 0x14
PS2_PSMT8H = 0x1B
PS2_PSMT4HL = 0x24
PS2_PSMT4HH = 0x2C

DEFAULT_EXTRACT_TARGETS: Tuple[str, ...] = (
    r"GLOBAL\HUDTEXRACE.BIN",
    r"GLOBAL\DYNTEX.BIN",
    r"CARS\TEXTURES.BIN",
    r"CARS\WHEELS\TEXTURES.BIN",
)

EXTENDED_EXTRACT_TARGETS: Tuple[str, ...] = DEFAULT_EXTRACT_TARGETS + (
    r"GLOBAL\HUDTEXDRAG.BIN",
    r"GLOBAL\INGAMEA.BUN",
    r"GLOBAL\INGAMEB.BUN",
    r"GLOBAL\GLOBALA.BUN",
)

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ExtractedTexture:
    """Decoded PS2 texture metadata and optional RGBA payload / file path."""

    archive: str
    container_index: int
    texture_index: int
    name: str
    name_hash: int
    width: int
    height: int
    ps2_psm: int
    ps2_psm_name: str
    clut_psm: int
    palette_entries: int
    is_swizzled: bool
    image_size: int
    palette_size: int
    compression: str
    png_path: Optional[str] = None
    image: Optional[Image.Image] = None

    def to_manifest_dict(self) -> Dict[str, Any]:
        return {
            "archive": self.archive,
            "container_index": self.container_index,
            "texture_index": self.texture_index,
            "name": self.name,
            "name_hash": f"0x{self.name_hash:08x}",
            "width": self.width,
            "height": self.height,
            "ps2_psm": self.ps2_psm,
            "ps2_psm_name": self.ps2_psm_name,
            "clut_psm": self.clut_psm,
            "palette_entries": self.palette_entries,
            "is_swizzled": self.is_swizzled,
            "image_size": self.image_size,
            "palette_size": self.palette_size,
            "compression": self.compression,
            "png_path": self.png_path,
        }


def scale_gs_alpha(alpha: int) -> int:
    """Convert PS2 GS 7-bit alpha (0x00..0x80) to standard 8-bit (0x00..0xFF)."""
    if alpha <= 0:
        return 0
    val = alpha * 2
    return 255 if val >= 255 else val


def unswizzle_clut_csm1(
    palette_bytes: bytes,
    entries: int = 256,
    clut_psm: int = PS2_PSMCT32,
    scale_alpha: bool = True,
) -> np.ndarray:
    """Decode and unswizzle a PS2 GS CLUT into a ``(entries, 4)`` uint8 RGBA array.

    PS2 CSM1 palettes swap blocks 1 and 2 inside every 32-entry tile for 8-bit
    (256-entry) palettes: ``src_idx = idx ^ 0x18`` whenever ``(idx & 0x18) in (8, 16)``.
    4-bit (16-entry) palettes are stored linearly.
    """
    if entries <= 0:
        entries = 16 if len(palette_bytes) <= 64 else 256

    if clut_psm in (PS2_PSMCT16, PS2_PSMCT16S):
        count = len(palette_bytes) // 2
        raw16 = np.frombuffer(palette_bytes[: count * 2], dtype="<u2")
        r = ((raw16 & 0x001F) << 3).astype(np.uint8)
        g = (((raw16 >> 5) & 0x001F) << 3).astype(np.uint8)
        b = (((raw16 >> 10) & 0x001F) << 3).astype(np.uint8)
        a = np.where((raw16 >> 15) & 1, 255, 0).astype(np.uint8)
        linear = np.stack([r, g, b, a], axis=1)
    else:
        count = len(palette_bytes) // 4
        linear = np.frombuffer(palette_bytes[: count * 4], dtype=np.uint8).reshape(-1, 4).copy()
        if scale_alpha and len(linear) > 0:
            a16 = linear[:, 3].astype(np.uint16) * 2
            linear[:, 3] = np.clip(a16, 0, 255).astype(np.uint8)

    out = np.zeros((entries, 4), dtype=np.uint8)
    avail = len(linear)
    if entries <= 16:
        copy_n = min(entries, avail)
        out[:copy_n] = linear[:copy_n]
        return out

    # CSM1 swizzle for 256-entry CLUTs (blocks of 8 entries swap 1<->2 per 32)
    idx = np.arange(entries, dtype=np.int32)
    bits = idx & 0x18
    src_idx = np.where((bits == 8) | (bits == 16), idx ^ 0x18, idx)
    valid = src_idx < avail
    out[valid] = linear[src_idx[valid]]
    return out


def swizzle_clut_csm1(palette_rgba: np.ndarray, halving_alpha: bool = True) -> bytes:
    """Inverse of ``unswizzle_clut_csm1`` for generating PS2 CSM1 RGBA32 palettes."""
    pal = np.asarray(palette_rgba, dtype=np.uint8).reshape(-1, 4).copy()
    entries = len(pal)
    if halving_alpha:
        pal[:, 3] = ((pal[:, 3].astype(np.uint16) + 1) // 2).clip(0, 128).astype(np.uint8)
    if entries <= 16:
        return pal.tobytes()

    idx = np.arange(entries, dtype=np.int32)
    bits = idx & 0x18
    swiz_idx = np.where((bits == 8) | (bits == 16), idx ^ 0x18, idx)
    out = np.zeros_like(pal)
    out[swiz_idx] = pal[idx]
    return out.tobytes()


@lru_cache(maxsize=64)
def _lut_psmt8(width: int, height: int) -> np.ndarray:
    """Return cached linear-byte LUT for PS2 ``PSMT8`` unswizzling."""
    x = np.arange(width, dtype=np.int32)[None, :]
    y = np.arange(height, dtype=np.int32)[:, None]

    block_loc = (y & ~0xF) * width + (x & ~0xF) * 2
    swap_sel = (((y + 2) >> 2) & 0x1) * 4
    pos_y = (((y & ~3) >> 1) + (y & 1)) & 0x7
    col_loc = pos_y * width * 2 + ((x + swap_sel) & 0x7) * 4
    byte_num = ((y >> 1) & 1) + ((x >> 2) & 2)
    return (block_loc + col_loc + byte_num).reshape(-1)


def unswizzle8(buf: bytes, width: int, height: int) -> bytes:
    """Unswizzle a PS2 GS ``PSMT8`` buffer of size ``width * height`` bytes."""
    expected = width * height
    lut = _lut_psmt8(width, height)
    arr = np.frombuffer(buf, dtype=np.uint8)
    if len(arr) < expected:
        padded = np.zeros(max(expected, int(lut.max()) + 1), dtype=np.uint8)
        padded[: len(arr)] = arr
        arr = padded
    return arr[lut].tobytes()


def swizzle8(indices: bytes, width: int, height: int) -> bytes:
    """Swizzle linear 8-bit indices (``width * height``) into PS2 ``PSMT8`` layout."""
    lut = _lut_psmt8(width, height)
    src = np.frombuffer(indices, dtype=np.uint8)
    out = np.zeros(max(width * height, int(lut.max()) + 1), dtype=np.uint8)
    out[lut] = src[: len(lut)]
    return out[: width * height].tobytes()


@lru_cache(maxsize=64)
def _lut_psmt4(width: int, height: int) -> Tuple[np.ndarray, np.ndarray]:
    """Return cached ``(byte_offset, nibble_shift)`` LUTs for PS2 ``PSMT4``.

    EA NFSMW packs 4-bit ``PSMT4`` textures inside PS2 GS VRAM uploaded via ``PSMCT32``.
    Combining the byte address of ``unswizzle8(width // 2, height)`` with the
    intra-column bit remap gives a strict 1-to-1 bijection over ``width x height``
    nibbles for all power-of-two textures ``>= 32x16``:
      - ``x3 = (x >> 3) & 1``, ``x4 = (x >> 4) & 1``
      - ``y_u8 = (y & ~3) | (x3 << 1) | (y & 1)``
      - ``nib_shift = ((y >> 1) & 1) * 4``
      - ``swap_row_xor = (((y >> 2) & 1) * 4) ^ ((((y >> 2) & 1) ^ ((y >> 1) & 1)) * 2)``
      - ``xb = (((x & 1) << 3) | ((x3 ^ x4) << 2) | (((x >> 2) & 1) << 1) | ((x >> 1) & 1)) ^ swap_row_xor``
      - ``x_u8 = ((x >> 5) << 4) | xb``
    """
    w_u = width // 2
    x = np.arange(width, dtype=np.int32)[None, :]
    y = np.arange(height, dtype=np.int32)[:, None]

    x3 = (x >> 3) & 1
    x4 = (x >> 4) & 1
    row4 = (y >> 2) & 1
    row2 = (y >> 1) & 1
    swap_row_xor = (row4 * 4) ^ ((row4 ^ row2) * 2)

    y_u8 = (y & ~3) | (x3 << 1) | (y & 1)
    xb = (
        ((x & 1) << 3)
        | ((x3 ^ x4) << 2)
        | (((x >> 2) & 1) << 1)
        | ((x >> 1) & 1)
    ) ^ swap_row_xor
    x_u8 = ((x >> 5) << 4) | xb

    block_loc = (y_u8 & ~0xF) * w_u + (x_u8 & ~0xF) * 2
    swap_sel = (((y_u8 + 2) >> 2) & 1) * 4
    pos_y = (((y_u8 & ~3) >> 1) + (y_u8 & 1)) & 0x7
    col_loc = pos_y * w_u * 2 + ((x_u8 + swap_sel) & 0x7) * 4
    byte_num = ((y_u8 >> 1) & 1) + ((x_u8 >> 2) & 2)

    swiz = (block_loc + col_loc + byte_num).reshape(-1)
    nib_shift = np.broadcast_to((row2 * 4), (height, width)).reshape(-1).astype(np.uint8)
    return swiz, nib_shift


def unswizzle4(buf: bytes, width: int, height: int) -> bytes:
    """Unswizzle a PS2 GS ``PSMT4`` buffer into ``width * height`` 1-byte indices (0..15)."""
    expected_bytes = (width * height + 1) // 2
    if width < 32 or height < 16 or (width % 2) != 0:
        return decode_linear_4bit(buf, width, height)

    lut, nib_shift = _lut_psmt4(width, height)
    arr = np.frombuffer(buf, dtype=np.uint8)
    needed = max(expected_bytes, int(lut.max()) + 1)
    if len(arr) < needed:
        padded = np.zeros(needed, dtype=np.uint8)
        padded[: len(arr)] = arr
        arr = padded
    indices = ((arr[lut] >> nib_shift) & 0x0F).astype(np.uint8)
    return indices.tobytes()


def swizzle4(indices: bytes, width: int, height: int) -> bytes:
    """Swizzle linear 4-bit indices (1 byte per pixel, values 0..15) into PS2 ``PSMT4`` layout."""
    if width < 32 or height < 16 or (width % 2) != 0:
        src = np.frombuffer(indices, dtype=np.uint8)[: width * height] & 0x0F
        pairs = src.reshape(-1, 2)
        return (pairs[:, 0] | (pairs[:, 1] << 4)).tobytes()

    lut, nib_shift = _lut_psmt4(width, height)
    src = (np.frombuffer(indices, dtype=np.uint8)[: width * height] & 0x0F).astype(np.uint8)
    out = np.zeros(max((width * height) // 2, int(lut.max()) + 1), dtype=np.uint8)
    low_mask = nib_shift == 0
    out[lut[low_mask]] |= src[low_mask]
    out[lut[~low_mask]] |= (src[~low_mask] << 4).astype(np.uint8)
    return out[: (width * height) // 2].tobytes()


def decode_linear_4bit(buf: bytes, width: int, height: int) -> bytes:
    """Decode linear packed 4-bit pixel pairs (low nibble first) into 1-byte indices."""
    total = width * height
    arr = np.frombuffer(buf, dtype=np.uint8)
    needed_bytes = (total + 1) // 2
    if len(arr) < needed_bytes:
        padded = np.zeros(needed_bytes, dtype=np.uint8)
        padded[: len(arr)] = arr
        arr = padded
    arr = arr[:needed_bytes]
    out = np.empty(needed_bytes * 2, dtype=np.uint8)
    out[0::2] = arr & 0x0F
    out[1::2] = (arr >> 4) & 0x0F
    return out[:total].tobytes()


def decode_ps2_texture(
    image_bytes: bytes,
    palette_bytes: bytes,
    width: int,
    height: int,
    psm: int,
    clut_psm: int = PS2_PSMCT32,
    num_palette_entries: int = 0,
    is_swizzled: bool = True,
    scale_alpha: bool = True,
) -> Image.Image:
    """Decode raw PS2 pixel + CLUT buffers into a ``PIL.Image.Image`` (``RGBA``)."""
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid texture dimensions: {width}x{height}")

    if psm == PS2_PSMCT32:
        expected = width * height * 4
        arr = np.frombuffer(image_bytes[:expected], dtype=np.uint8)
        if len(arr) < expected:
            padded = np.zeros(expected, dtype=np.uint8)
            padded[: len(arr)] = arr
            arr = padded
        rgba = arr.reshape(height, width, 4).copy()
        if scale_alpha:
            rgba[:, :, 3] = np.clip(rgba[:, :, 3].astype(np.uint16) * 2, 0, 255).astype(np.uint8)
        return Image.fromarray(rgba)

    if psm == PS2_PSMCT24:
        expected = width * height * 3
        arr = np.frombuffer(image_bytes[:expected], dtype=np.uint8)
        if len(arr) < expected:
            padded = np.zeros(expected, dtype=np.uint8)
            padded[: len(arr)] = arr
            arr = padded
        rgb = arr.reshape(height, width, 3)
        alpha = np.full((height, width, 1), 255, dtype=np.uint8)
        rgba = np.concatenate([rgb, alpha], axis=2)
        return Image.fromarray(rgba)

    if psm in (PS2_PSMCT16, PS2_PSMCT16S):
        expected = width * height * 2
        arr = np.frombuffer(image_bytes[:expected], dtype="<u2")
        if len(arr) < width * height:
            padded = np.zeros(width * height, dtype="<u2")
            padded[: len(arr)] = arr
            arr = padded
        arr = arr.reshape(height, width)
        r = ((arr & 0x001F) << 3).astype(np.uint8)
        g = (((arr >> 5) & 0x001F) << 3).astype(np.uint8)
        b = (((arr >> 10) & 0x001F) << 3).astype(np.uint8)
        a = np.where((arr >> 15) & 1, 255, 0).astype(np.uint8)
        return Image.fromarray(np.stack([r, g, b, a], axis=2))

    if psm in (PS2_PSMT8, PS2_PSMT8H):
        if is_swizzled and psm == PS2_PSMT8 and width >= 16 and height >= 16:
            idx_bytes = unswizzle8(image_bytes, width, height)
        else:
            idx_bytes = image_bytes[: width * height]
            if len(idx_bytes) < width * height:
                idx_bytes = idx_bytes + b"\x00" * (width * height - len(idx_bytes))
        indices = np.frombuffer(idx_bytes, dtype=np.uint8).reshape(height, width)
        entries = num_palette_entries if num_palette_entries > 0 else 256
        pal = unswizzle_clut_csm1(palette_bytes, entries=entries, clut_psm=clut_psm, scale_alpha=scale_alpha)
        rgba = pal[np.minimum(indices, len(pal) - 1)]
        return Image.fromarray(rgba)

    if psm in (PS2_PSMT4, PS2_PSMT4HL, PS2_PSMT4HH):
        if is_swizzled and psm == PS2_PSMT4 and width >= 32 and height >= 16:
            idx_bytes = unswizzle4(image_bytes, width, height)
        else:
            idx_bytes = decode_linear_4bit(image_bytes, width, height)
        indices = np.frombuffer(idx_bytes, dtype=np.uint8).reshape(height, width)
        entries = num_palette_entries if num_palette_entries > 0 else 16
        pal = unswizzle_clut_csm1(palette_bytes, entries=entries, clut_psm=clut_psm, scale_alpha=scale_alpha)
        rgba = pal[np.minimum(indices, len(pal) - 1)]
        return Image.fromarray(rgba)

    raise ValueError(f"Unsupported PS2 GS pixel storage mode: 0x{psm:02x}")


def _sanitize_name(name: str, fallback: str) -> str:
    clean = SAFE_FILENAME_RE.sub("_", name.strip()).strip("._")
    return clean if clean else fallback


def _archive_slug(source: str) -> str:
    norm = source.replace("\\", "/").strip("/")
    no_ext = norm.rsplit(".", 1)[0] if "." in norm else norm
    return SAFE_FILENAME_RE.sub("_", no_ext.replace("/", "_")).strip("_") or "tpk_archive"


def _iter_linear_chunks(buf: bytes, start: int, end: int) -> Iterable[Tuple[int, int, int]]:
    """Yield ``(chunk_id, chunk_pos, chunk_size)`` across sequential EA chunks."""
    pos = start
    while pos + 8 <= end:
        cid, csz = struct.unpack_from("<II", buf, pos)
        if csz > end - (pos + 8):
            break
        yield cid, pos, csz
        pos += 8 + csz
        if cid == 0 and csz == 0:
            break


def _find_tpk_roots(buf: bytes, start: int, end: int, out: List[Tuple[int, int]]) -> None:
    """Recursively locate ``CHUNK_TPK_ROOT`` (``0xB3300000``) chunks inside an EA stream."""
    for cid, pos, csz in _iter_linear_chunks(buf, start, end):
        data_start = pos + 8
        data_end = data_start + csz
        if cid == tpki.CHUNK_TPK_ROOT:
            out.append((pos, csz))
        elif (cid & 0x80000000) != 0 and cid != 0xFFFFFFFF and csz >= 8:
            # Recurse into EA container chunks (e.g., track bundle section containers)
            first_sub_sz = struct.unpack_from("<I", buf, data_start + 4)[0]
            if first_sub_sz <= csz - 8:
                _find_tpk_roots(buf, data_start, data_end, out)


def _decode_texture_pair(
    ti_bytes: bytes,
    pi_bytes: bytes,
) -> Dict[str, Any]:
    """Parse raw 124-byte ``TextureInfo`` and 84-byte ``TextureInfoPlatInfo``."""
    raw_name = ti_bytes[12:36].split(b"\x00", 1)[0]
    name = raw_name.decode("ascii", errors="replace").strip()
    name_hash, _class_hash, _parent_hash = struct.unpack_from("<III", ti_bytes, 36)
    image_offset, palette_offset, image_size, palette_size, _base_img_size = struct.unpack_from(
        "<IIIII", ti_bytes, 48
    )
    width, height = struct.unpack_from("<HH", ti_bytes, 68)
    _shift_w, _shift_h, _img_comp, _pal_comp, num_pal_entries, _mip_levels = struct.unpack_from(
        "<BBBBHB", ti_bytes, 72
    )
    psm = pi_bytes[4]
    clut_psm = pi_bytes[5]
    img0_w, img0_h, _tbp, _tbw, is_swizzled = struct.unpack_from("<hhHBB", pi_bytes, 16)
    if width == 0 and img0_w > 0:
        width = img0_w
    if height == 0 and img0_h > 0:
        height = img0_h
    return {
        "name": name or f"tex_{name_hash:08x}",
        "name_hash": name_hash,
        "image_offset": image_offset,
        "palette_offset": palette_offset,
        "image_size": image_size,
        "palette_size": palette_size,
        "width": width,
        "height": height,
        "num_palette_entries": num_pal_entries,
        "psm": psm,
        "psm_name": tpki.PS2_GS_PSM_NAMES.get(psm, f"PSM_0x{psm:02x}"),
        "clut_psm": clut_psm,
        "is_swizzled": bool(is_swizzled),
    }


def extract_tpk_bytes(
    data: bytes,
    source: str = "<memory>",
    out_dir: Optional[Path] = None,
    name_filter: Optional[str] = None,
    limit: Optional[int] = None,
    scale_alpha: bool = True,
    keep_images: bool = False,
) -> List[ExtractedTexture]:
    """Extract and decode all TPK containers embedded in ``data``.

    When ``out_dir`` is provided, writes PNG files and a ``manifest.json`` into
    ``out_dir`` (the caller may pass an archive-specific directory such as
    ``build/tpk_extracted/GLOBAL_HUDTEXRACE``).
    """
    roots: List[Tuple[int, int]] = []
    _find_tpk_roots(data, 0, len(data), roots)
    if not roots:
        return []

    filter_lc = name_filter.lower() if name_filter else None
    extracted: List[ExtractedTexture] = []
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    for c_idx, (root_pos, root_sz) in enumerate(roots):
        root_start = root_pos + 8
        root_end = root_start + root_sz

        tex_infos_raw: List[bytes] = []
        plat_infos_raw: List[bytes] = []
        stream_entries: List[Tuple[int, int, int, int]] = []
        sp_start: Optional[int] = None
        sp_size = 0

        for cid, pos, csz in _iter_linear_chunks(data, root_start, root_end):
            c_start, c_end = pos + 8, pos + 8 + csz
            if cid == tpki.CHUNK_TPK_INFO:
                for scid, spos, scsz in _iter_linear_chunks(data, c_start, c_end):
                    s_start = spos + 8
                    if scid == tpki.CHUNK_TPK_STREAM_TABLE:
                        for i in range(scsz // tpki.STREAM_ENTRY_SIZE):
                            hsh, off, csz_e, ucsz_e = struct.unpack_from(
                                "<IIII", data, s_start + i * tpki.STREAM_ENTRY_SIZE
                            )
                            stream_entries.append((hsh, off, csz_e, ucsz_e))
                    elif scid == tpki.CHUNK_TPK_TEX_INFO:
                        for i in range(scsz // tpki.TEXTURE_INFO_SIZE):
                            off_i = s_start + i * tpki.TEXTURE_INFO_SIZE
                            tex_infos_raw.append(data[off_i : off_i + tpki.TEXTURE_INFO_SIZE])
                    elif scid == tpki.CHUNK_TPK_TEX_PLAT:
                        for i in range(scsz // tpki.TEXTURE_PLAT_SIZE):
                            off_i = s_start + i * tpki.TEXTURE_PLAT_SIZE
                            plat_infos_raw.append(data[off_i : off_i + tpki.TEXTURE_PLAT_SIZE])
            elif cid == tpki.CHUNK_TPK_DATA:
                for dcid, dpos, dcsz in _iter_linear_chunks(data, c_start, c_end):
                    if dcid == tpki.CHUNK_TPK_DATA_PAYLOAD:
                        sp_start = dpos + 8
                        sp_size = dcsz

        # Streamed TPK entries (offset/size in stream table relative to outer `data`)
        if stream_entries:
            for t_idx, (hsh, off, csz_e, _ucsz_e) in enumerate(stream_entries):
                if limit is not None and len(extracted) >= limit:
                    break
                if off + csz_e > len(data):
                    continue
                dec, algo = tpki.decompress_ea_block(data[off : off + csz_e])
                tail_needed = tpki.TEXTURE_INFO_SIZE + tpki.TEXTURE_PLAT_SIZE
                if len(dec) < tail_needed:
                    continue
                ti_bytes = dec[-tail_needed : -tpki.TEXTURE_PLAT_SIZE]
                pi_bytes = dec[-tpki.TEXTURE_PLAT_SIZE :]
                meta = _decode_texture_pair(ti_bytes, pi_bytes)
                if meta["name_hash"] == 0:
                    meta["name_hash"] = hsh
                if filter_lc and filter_lc not in meta["name"].lower():
                    continue
                img_sz = meta["image_size"]
                pal_sz = meta["palette_size"]
                img_bytes = dec[:img_sz]
                pal_bytes = dec[img_sz : img_sz + pal_sz] if pal_sz > 0 else b""
                img = decode_ps2_texture(
                    img_bytes,
                    pal_bytes,
                    width=meta["width"],
                    height=meta["height"],
                    psm=meta["psm"],
                    clut_psm=meta["clut_psm"],
                    num_palette_entries=meta["num_palette_entries"],
                    is_swizzled=meta["is_swizzled"],
                    scale_alpha=scale_alpha,
                )
                entry = _record_extracted(
                    archive=source,
                    container_index=c_idx,
                    texture_index=t_idx,
                    meta=meta,
                    compression=algo,
                    img=img,
                    out_dir=out_dir,
                    keep_images=keep_images,
                )
                extracted.append(entry)
            continue

        # Uncompressed TPK payload
        if sp_start is None or not tex_infos_raw:
            continue

        parsed_metas: List[Dict[str, Any]] = []
        max_end = 0
        for i, ti_b in enumerate(tex_infos_raw):
            pi_b = plat_infos_raw[i] if i < len(plat_infos_raw) else b"\x00" * tpki.TEXTURE_PLAT_SIZE
            meta = _decode_texture_pair(ti_b, pi_b)
            parsed_metas.append(meta)
            max_end = max(
                max_end,
                meta["image_offset"] + meta["image_size"],
                (meta["palette_offset"] + meta["palette_size"]) if meta["palette_size"] > 0 else 0,
            )

        # EA pads the 0x33320002 payload up to a 128-byte boundary (120 bytes of 0x11)
        # relative to the start of CHUNK_TPK_ROOT so PS2 DMA transfers stay aligned.
        rel_sp = sp_start - root_pos
        pad_128 = ((rel_sp + 127) & ~127) - rel_sp
        if (
            pad_128 > 0
            and sp_size >= max_end + pad_128
            and data[sp_start : sp_start + pad_128] == b"\x11" * pad_128
        ):
            payload_base = sp_start + pad_128
        else:
            payload_base = sp_start
        payload_end = sp_start + sp_size

        for t_idx, meta in enumerate(parsed_metas):
            if limit is not None and len(extracted) >= limit:
                break
            if filter_lc and filter_lc not in meta["name"].lower():
                continue
            ioff = payload_base + meta["image_offset"]
            poff = payload_base + meta["palette_offset"]
            if ioff + meta["image_size"] > payload_end:
                continue
            img_bytes = data[ioff : ioff + meta["image_size"]]
            if meta["palette_size"] > 0 and poff + meta["palette_size"] <= payload_end:
                pal_bytes = data[poff : poff + meta["palette_size"]]
            else:
                pal_bytes = b""
            img = decode_ps2_texture(
                img_bytes,
                pal_bytes,
                width=meta["width"],
                height=meta["height"],
                psm=meta["psm"],
                clut_psm=meta["clut_psm"],
                num_palette_entries=meta["num_palette_entries"],
                is_swizzled=meta["is_swizzled"],
                scale_alpha=scale_alpha,
            )
            entry = _record_extracted(
                archive=source,
                container_index=c_idx,
                texture_index=t_idx,
                meta=meta,
                compression="RAW",
                img=img,
                out_dir=out_dir,
                keep_images=keep_images,
            )
            extracted.append(entry)

    if out_dir is not None:
        manifest = {
            "source": source,
            "containers": len(roots),
            "texture_count": len(extracted),
            "textures": [e.to_manifest_dict() for e in extracted],
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return extracted


def _record_extracted(
    archive: str,
    container_index: int,
    texture_index: int,
    meta: Dict[str, Any],
    compression: str,
    img: Image.Image,
    out_dir: Optional[Path],
    keep_images: bool,
) -> ExtractedTexture:
    png_path_str: Optional[str] = None
    if out_dir is not None:
        safe_name = _sanitize_name(meta["name"], f"tex_{meta['name_hash']:08x}")
        prefix = f"c{container_index}_" if container_index > 0 else ""
        fname = f"{prefix}{texture_index:03d}_{safe_name}_{meta['name_hash']:08x}.png"
        dest = out_dir / fname
        img.save(dest, format="PNG")
        png_path_str = str(dest)

    return ExtractedTexture(
        archive=archive,
        container_index=container_index,
        texture_index=texture_index,
        name=meta["name"],
        name_hash=meta["name_hash"],
        width=meta["width"],
        height=meta["height"],
        ps2_psm=meta["psm"],
        ps2_psm_name=meta["psm_name"],
        clut_psm=meta["clut_psm"],
        palette_entries=meta["num_palette_entries"],
        is_swizzled=meta["is_swizzled"],
        image_size=meta["image_size"],
        palette_size=meta["palette_size"],
        compression=compression,
        png_path=png_path_str,
        image=img if keep_images else None,
    )


def extract_iso_tpks(
    iso_path: Path,
    targets: Optional[Sequence[str]] = None,
    out_dir: Path = Path("build/tpk_extracted"),
    name_filter: Optional[str] = None,
    limit: Optional[int] = None,
    scale_alpha: bool = True,
) -> Dict[str, Any]:
    """Read-only extraction of PS2 TPK archives from an NFSMW ISO into ``out_dir``."""
    target_list = list(targets) if targets else list(DEFAULT_EXTRACT_TARGETS)
    archive = PS2NFSMWArchive(iso_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_archives: List[Dict[str, Any]] = []
    all_textures: List[Dict[str, Any]] = []
    missing_targets: List[str] = []

    for vpath in target_list:
        zentry = archive.find(vpath)
        if zentry is None:
            missing_targets.append(vpath)
            continue
        raw = archive.read_entry(zentry)
        slug = _archive_slug(vpath)
        sub_out = out_dir / slug
        items = extract_tpk_bytes(
            raw,
            source=vpath,
            out_dir=sub_out,
            name_filter=name_filter,
            limit=limit,
            scale_alpha=scale_alpha,
            keep_images=False,
        )
        summary_archives.append(
            {
                "target": vpath,
                "slug": slug,
                "out_dir": str(sub_out),
                "texture_count": len(items),
            }
        )
        all_textures.extend(item.to_manifest_dict() for item in items)

    index_payload = {
        "iso_name": iso_path.name,
        "archives": summary_archives,
        "missing_targets": missing_targets,
        "total_textures": len(all_textures),
        "textures": all_textures,
    }
    (out_dir / "index.json").write_text(json.dumps(index_payload, indent=2) + "\n", encoding="utf-8")
    return index_payload


def _default_iso_candidate() -> Optional[Path]:
    if "NFSMW_ISO" in os.environ:
        env_iso = os.environ["NFSMW_ISO"].strip()
        if not env_iso:
            return None
        p = Path(env_iso).expanduser()
        return p if p.is_file() else None
    candidate = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    return candidate if candidate.is_file() else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Decode PS2 NFSMW TPK archives to RGBA PNGs.")
    src_group = parser.add_mutually_exclusive_group()
    src_group.add_argument("--iso", type=Path, help="Path to untouched NFSMW PS2 ISO (read strictly 'rb').")
    src_group.add_argument("--file", type=Path, help="Standalone TPK/BIN/BUN file to extract.")
    parser.add_argument(
        "--targets",
        nargs="+",
        help="ISO virtual paths to extract (default: HUDTEXRACE, DYNTEX, CARS/TEXTURES, CARS/WHEELS/TEXTURES).",
    )
    parser.add_argument(
        "--all-known",
        action="store_true",
        help="Include HUDTEXDRAG, INGAMEA.BUN, INGAMEB.BUN, GLOBALA.BUN in addition to default targets.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("build/tpk_extracted"),
        help="Output directory inside gitignored build/ (default: build/tpk_extracted).",
    )
    parser.add_argument("--filter", dest="name_filter", help="Case-insensitive substring filter on texture name.")
    parser.add_argument("--limit", type=int, help="Maximum textures to extract per archive.")
    parser.add_argument(
        "--raw-alpha",
        action="store_true",
        help="Keep raw PS2 GS 0x00..0x80 alpha instead of scaling to 0x00..0xFF.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    args = parser.parse_args(argv)

    scale_alpha = not args.raw_alpha

    if args.file is not None:
        raw = args.file.read_bytes()
        slug = _archive_slug(args.file.name)
        items = extract_tpk_bytes(
            raw,
            source=args.file.name,
            out_dir=args.out / slug,
            name_filter=args.name_filter,
            limit=args.limit,
            scale_alpha=scale_alpha,
        )
        res = {
            "source": args.file.name,
            "out_dir": str(args.out / slug),
            "texture_count": len(items),
        }
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Extracted {len(items)} textures from {args.file.name} -> {args.out / slug}")
        return 0

    iso_path = args.iso or _default_iso_candidate()
    if iso_path is None or not iso_path.is_file():
        print("Error: --iso path not provided and default ISO not found (set NFSMW_ISO or pass --iso).", file=sys.stderr)
        return 2

    targets = args.targets
    if targets is None:
        targets = list(EXTENDED_EXTRACT_TARGETS if args.all_known else DEFAULT_EXTRACT_TARGETS)

    res = extract_iso_tpks(
        iso_path=iso_path,
        targets=targets,
        out_dir=args.out,
        name_filter=args.name_filter,
        limit=args.limit,
        scale_alpha=scale_alpha,
    )
    if args.json:
        compact = {k: v for k, v in res.items() if k != "textures"}
        print(json.dumps(compact, indent=2))
    else:
        for a in res["archives"]:
            print(f"  {a['target']:30s} -> {a['texture_count']:4d} PNGs ({a['out_dir']})")
        print(f"Total decoded PS2 textures: {res['total_textures']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
