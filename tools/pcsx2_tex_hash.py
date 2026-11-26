#!/usr/bin/env python3
"""Offline pure-math Sony PS2 GS VRAM pixel swizzle & PCSX2 v2.6.3 texture hasher.

Computes exact PCSX2 v2.6.3 replacement stems ``<tex_hash>-<clut_hash>-<flags_8hex>``
100% offline directly from untouched PS2 NFSMW (SLUS-21351) TPK archives—zero
PCSX2 binary or third-party hash dependency.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

import numpy as np

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import tpk_extract as te  # noqa: E402
import tpk_inspect as ti  # noqa: E402
from jdlz import is_jdlz, lz_decompress  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

PSM_CT32, PSM_T8, PSM_T4 = 0x00, 0x13, 0x14

# Official Sony PS2 GS 8 KB page -> 32 x 256-byte block tables
BLOCK_TABLE_32: Tuple[Tuple[int, ...], ...] = (
    (0, 1, 4, 5, 16, 17, 20, 21), (2, 3, 6, 7, 18, 19, 22, 23),
    (8, 9, 12, 13, 24, 25, 28, 29), (10, 11, 14, 15, 26, 27, 30, 31),
)
BLOCK_TABLE_8: Tuple[Tuple[int, ...], ...] = BLOCK_TABLE_32
BLOCK_TABLE_4: Tuple[Tuple[int, ...], ...] = (
    (0, 2, 8, 10), (1, 3, 9, 11), (4, 6, 12, 14), (5, 7, 13, 15),
    (16, 18, 24, 26), (17, 19, 25, 27), (20, 22, 28, 30), (21, 23, 29, 31),
)

# Official Cyan4973 XXH3_64bits (v0.8.x, seed=0) constants & 192-byte secret
_M64, _M32 = 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF
_P32_1, _P32_2, _P32_3 = 0x9E3779B1, 0x85EBCA77, 0xC2B2AE3D
_P64_1, _P64_2, _P64_3 = 0x9E3779B185EBCA87, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9
_P64_4, _P64_5, _P_MX1, _P_MX2 = 0x85EBCA77C2B2AE63, 0x27D4EB2F165667C5, 0x165667919E3779F9, 0x9FB21C651E98DF25
_XXH3_SECRET = bytes.fromhex(
    "b8fe6c3923a44bbe7c01812cf721ad1cded46de9839097db7240a4a4b7b3671f"
    "cb79e64eccc0e578825ad07dccff7221b8084674f743248ee03590e6813a264c"
    "3c2852bb91c300cb88d0658b1b532ea371644897a20df94e3819ef46a9deacd8"
    "a8fa763fe39c343ff9dcbbc7c70b4f1d8a51e04bcdb45931c89f7ec9d9787364"
    "eac5ac8334d3ebc3c581a0fffa1363eb170ddd51b7f0da49d316552629d4689e"
    "2b16be587d47a1fc8ff8b8d17ad031ce45cb3a8f95160428afd7fbcabb4b407e"
)
_S_STRIPES = np.array([np.frombuffer(_XXH3_SECRET, dtype="<u8", count=8, offset=s * 8) for s in range(16)], dtype=np.uint64)
_S_SCRAMBLE = np.frombuffer(_XXH3_SECRET, dtype="<u8", count=8, offset=128)
_S_LAST = np.frombuffer(_XXH3_SECRET, dtype="<u8", count=8, offset=121)
_SWAP = np.array([1, 0, 3, 2, 5, 4, 7, 6], dtype=np.intp)


def _u32(b: bytes, o: int) -> int: return struct.unpack_from("<I", b, o)[0]
def _u64(b: bytes, o: int) -> int: return struct.unpack_from("<Q", b, o)[0]
def _mul128(a: int, b: int) -> int: p = a * b; return (p & _M64) ^ ((p >> 64) & _M64)
def _aval(h: int) -> int: h = ((h ^ (h >> 37)) * _P_MX1) & _M64; return h ^ (h >> 32)
def _aval64(h: int) -> int: h = ((h ^ (h >> 33)) * _P64_2) & _M64; h = ((h ^ (h >> 29)) * _P64_3) & _M64; return h ^ (h >> 32)
def _mix16(d: bytes, do: int, so: int) -> int: return _mul128(_u64(d, do) ^ _u64(_XXH3_SECRET, so), _u64(d, do + 8) ^ _u64(_XXH3_SECRET, so + 8))


def xxh3_64bits(data: bytes) -> int:
    """Compute official Cyan4973 XXH3_64bits (seed=0) matching PCSX2's GSXXH3_64."""
    n = len(data)
    if n <= 16:
        if n > 8:
            il = _u64(data, 0) ^ _u64(_XXH3_SECRET, 24) ^ _u64(_XXH3_SECRET, 32)
            ih = _u64(data, n - 8) ^ _u64(_XXH3_SECRET, 40) ^ _u64(_XXH3_SECRET, 48)
            return _aval((n + int.from_bytes(il.to_bytes(8, "little"), "big") + ih + _mul128(il, ih)) & _M64)
        if n >= 4:
            h = ((_u32(data, n - 4) + (_u32(data, 0) << 32)) ^ _u64(_XXH3_SECRET, 8) ^ _u64(_XXH3_SECRET, 16)) & _M64
            h = (((h ^ (((h << 49) | (h >> 15)) & _M64) ^ (((h << 24) | (h >> 40)) & _M64)) * _P_MX2) & _M64)
            h = (((h ^ ((h >> 35) + n)) & _M64) * _P_MX2) & _M64
            return h ^ (h >> 28)
        if n > 0:
            comb = (data[0] << 16) | (data[n >> 1] << 24) | data[n - 1] | (n << 8)
            return _aval64((comb ^ ((_u32(_XXH3_SECRET, 0) ^ _u32(_XXH3_SECRET, 4)) & _M32)) & _M64)
        return _aval64(_u64(_XXH3_SECRET, 56) ^ _u64(_XXH3_SECRET, 64))
    if n <= 128:
        acc = (n * _P64_1) & _M64
        if n > 32:
            if n > 64:
                if n > 96:
                    acc = (acc + _mix16(data, 48, 96) + _mix16(data, n - 64, 112)) & _M64
                acc = (acc + _mix16(data, 32, 64) + _mix16(data, n - 48, 80)) & _M64
            acc = (acc + _mix16(data, 16, 32) + _mix16(data, n - 32, 48)) & _M64
        return _aval((acc + _mix16(data, 0, 0) + _mix16(data, n - 16, 16)) & _M64)
    if n <= 240:
        acc = _aval(((n * _P64_1) + sum(_mix16(data, 16 * i, 16 * i) for i in range(8))) & _M64)
        for i in range(8, n // 16):
            acc = (acc + _mix16(data, 16 * i, 16 * (i - 8) + 3)) & _M64
        return _aval((acc + _mix16(data, n - 16, 119)) & _M64)

    acc = np.array([_P32_3, _P64_1, _P64_2, _P64_3, _P64_4, _P32_2, _P64_5, _P32_1], dtype=np.uint64)
    nb, m32, s32, s47, p32 = (n - 1) // 1024, np.uint64(_M32), np.uint64(32), np.uint64(47), np.uint64(_P32_1)
    if nb > 0:
        blks = np.frombuffer(data, dtype="<u8", count=nb * 128).reshape(nb, 16, 8)
        dk = blks ^ _S_STRIPES[None, :, :]
        delta = (blks[:, :, _SWAP] + (dk & m32) * (dk >> s32)).sum(axis=1, dtype=np.uint64)
        for b in range(nb):
            acc = ((acc + delta[b]) ^ ((acc + delta[b]) >> s47) ^ _S_SCRAMBLE) * p32
    ns = ((n - 1) - 1024 * nb) // 64
    if ns > 0:
        rem = np.frombuffer(data, dtype="<u8", count=ns * 8, offset=nb * 1024).reshape(ns, 8)
        dk = rem ^ _S_STRIPES[:ns]
        acc = acc + (rem[:, _SWAP] + (dk & m32) * (dk >> s32)).sum(axis=0, dtype=np.uint64)
    last = np.frombuffer(data, dtype="<u8", count=8, offset=n - 64)
    dk = last ^ _S_LAST
    ai = [int(v) for v in (acc + last[_SWAP] + (dk & m32) * (dk >> s32))]
    res = (n * _P64_1) & _M64
    for i in range(4):
        res = (res + _mul128(ai[2 * i] ^ _u64(_XXH3_SECRET, 11 + 16 * i), ai[2 * i + 1] ^ _u64(_XXH3_SECRET, 19 + 16 * i))) & _M64
    return _aval(res)


def _build_gs_luts() -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    x32, y32 = np.arange(8, dtype=np.int32)[None, :], np.arange(8, dtype=np.int32)[:, None]
    b32 = (((y32 >> 1) & 3) * 16 + (y32 & 1) * 2) + (((x32 & 6) * 2) + (x32 & 1))
    x8, y8 = np.arange(16, dtype=np.int32)[None, :], np.arange(16, dtype=np.int32)[:, None]
    b8 = (((y8 >> 2) & 3) * 64 + ((((x8 >> 2) & 1) ^ ((y8 >> 1) & 1) ^ ((y8 >> 2) & 1)) * 32)
          + (((x8 >> 1) & 1) * 16) + ((y8 & 1) * 8) + ((x8 & 1) * 4) + (((x8 >> 3) & 1) * 2) + ((y8 >> 1) & 1))
    x4, y4 = np.arange(32, dtype=np.int32)[None, :], np.arange(16, dtype=np.int32)[:, None]
    nib4 = (((y4 >> 2) & 3) * 128 + ((((x4 >> 2) & 1) ^ ((y4 >> 1) & 1) ^ ((y4 >> 2) & 1)) * 64)
            + (((x4 >> 1) & 1) * 32) + ((y4 & 1) * 16) + ((x4 & 1) * 8)
            + (((x4 >> 4) & 1) * 4) + (((x4 >> 3) & 1) * 2) + ((y4 >> 1) & 1))
    return b32, b8, (nib4 >> 1), ((nib4 & 1) * 4).astype(np.uint8)


_LUT_B32, _LUT_B8, _LUT_B4, _LUT_N4 = _build_gs_luts()
_N4_LO, _N4_HI = (_LUT_N4 == 0), (_LUT_N4 != 0)


def gs_block_address(bx: int, by: int, psm: int, tbw_pages: int = 1) -> int:
    """Compute 256-byte GS VRAM block index for block (bx, by) via blockTable4/8/32."""
    if psm == PSM_T4:
        return ((by >> 3) * tbw_pages + (bx >> 2)) * 32 + BLOCK_TABLE_4[by & 7][bx & 3]
    if psm in (PSM_T8, PSM_CT32):
        return ((by >> 2) * tbw_pages + (bx >> 3)) * 32 + (BLOCK_TABLE_8 if psm == PSM_T8 else BLOCK_TABLE_32)[by & 3][bx & 7]
    raise ValueError(f"Unsupported PSM: 0x{psm:02x}")


def gs_swizzle_level_blocks(pixels: np.ndarray, psm: int) -> bytes:
    """Pack a linear mip level into exact PCSX2 HashTextureLevel VRAM block order."""
    if psm == PSM_T4:
        h, w = pixels.shape
        if w < 32 or h < 16:
            return np.ascontiguousarray(pixels, dtype=np.uint8).tobytes()
        tiles = pixels.reshape(h // 16, 16, w // 32, 32).transpose(0, 2, 1, 3)
        out = np.zeros((h // 16, w // 32, 256), dtype=np.uint8)
        out[:, :, _LUT_B4[_N4_LO]] |= tiles[:, :, _N4_LO] & 0x0F
        out[:, :, _LUT_B4[_N4_HI]] |= (tiles[:, :, _N4_HI] & 0x0F) << 4
        return out.tobytes()
    if psm == PSM_T8:
        h, w = pixels.shape
        if w < 16 or h < 16:
            return np.ascontiguousarray(pixels, dtype=np.uint8).tobytes()
        out = np.empty((h // 16, w // 16, 256), dtype=np.uint8)
        out[:, :, _LUT_B8] = pixels.reshape(h // 16, 16, w // 16, 16).transpose(0, 2, 1, 3)
        return out.tobytes()
    if psm == PSM_CT32:
        h, w = pixels.shape[:2]
        if w < 8 or h < 8:
            return np.ascontiguousarray(pixels, dtype=np.uint8).tobytes()
        u32 = np.ascontiguousarray(pixels, dtype=np.uint8).view("<u4").reshape(h, w)
        out = np.empty((h // 8, w // 8, 64), dtype="<u4")
        out[:, :, _LUT_B32] = u32.reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3)
        return out.tobytes()
    raise ValueError(f"Unsupported PSM: 0x{psm:02x}")


def pcsx2_flags(psm: int, width: int, height: int) -> int:
    """Compute PCSX2 v2.6.3 32-bit texture flags: ``psm | (log2(w) << 6) | (log2(h) << 10)``."""
    return (psm & 0x3F) | ((int(round(math.log2(max(1, width)))) & 0xF) << 6) | ((int(round(math.log2(max(1, height)))) & 0xF) << 10)


def format_pcsx2_stem(tex_hash: int, clut_hash: Optional[int], flags: int) -> str:
    """Format PCSX2 v2.6.3 lowercase stem (no leading zeros on hashes, 8-hex flags)."""
    return f"{tex_hash:x}-{clut_hash:x}-{flags:08x}" if clut_hash is not None else f"{tex_hash:x}-{flags:08x}"


def hash_clut_ps2(palette_bytes: bytes, entries: int, clut_psm: int = PSM_CT32) -> int:
    """Compute PCSX2 ClutHash using raw PS2 [0..128] alpha (scale_alpha=False)."""
    return xxh3_64bits(te.unswizzle_clut_csm1(palette_bytes, entries=entries, clut_psm=clut_psm, scale_alpha=False).tobytes())


@dataclass
class RawTPKTexture:
    archive: str; section: str; name: str; name_hash: int
    width: int; height: int; psm: int; clut_psm: int; palette_entries: int
    plat_bytes: bytes; image_bytes: bytes; palette_bytes: bytes


def parse_plat_mips(plat_bytes: bytes, base_w: int, base_h: int) -> List[Tuple[int, int, int]]:
    """Extract active ``(w, h, is_swizzled)`` mip descriptors from TexturePlatInfo[16..56]."""
    mips: List[Tuple[int, int, int]] = []
    if len(plat_bytes) >= 56:
        for m in range(5):
            mw, mh, _tbp, _tbw, swiz = struct.unpack_from("<hhHBB", plat_bytes, 16 + m * 8)
            if mw <= 0 or mh <= 0:
                break
            mips.append((int(mw), int(mh), int(swiz)))
    return mips or ([(base_w, base_h, int(plat_bytes[23]) if len(plat_bytes) >= 24 else 1)] if base_w > 0 and base_h > 0 else [])


def decode_mip_linear(raw: bytes, w: int, h: int, psm: int, swiz: int) -> np.ndarray:
    """Decode one raw EA TPK mip level into linear indices (PSMT4/PSMT8) or RGBA (PSMCT32)."""
    if psm == PSM_T4:
        if swiz and w >= 32 and h >= 8 and (w % 2) == 0:
            lut, nsh = te._lut_psmt4(w, h)
            arr = np.frombuffer(raw, dtype=np.uint8)
            need = max((w * h + 1) // 2, int(lut.max()) + 1)
            arr = np.pad(arr, (0, need - len(arr))) if len(arr) < need else arr
            return ((arr[lut] >> nsh) & 0x0F).astype(np.uint8).reshape(h, w)
        return np.frombuffer(te.decode_linear_4bit(raw, w, h), dtype=np.uint8).reshape(h, w)
    if psm == PSM_T8:
        if swiz and w >= 16 and h >= 8:
            lut = te._lut_psmt8(w, h)
            arr = np.frombuffer(raw, dtype=np.uint8)
            need = max(w * h, int(lut.max()) + 1)
            return (np.pad(arr, (0, need - len(arr))) if len(arr) < need else arr)[lut].reshape(h, w)
        return np.frombuffer(raw[: w * h].ljust(w * h, b"\x00"), dtype=np.uint8).reshape(h, w)
    if psm == PSM_CT32:
        n = w * h * 4
        return np.frombuffer(raw[:n].ljust(n, b"\x00"), dtype=np.uint8).reshape(h, w, 4)
    raise ValueError(f"Unsupported PSM: 0x{psm:02x}")


def decode_raw_tpk_rgba(tex: RawTPKTexture, scale_alpha: bool = False) -> np.ndarray:
    """Decode mip 0 of a RawTPKTexture to an ``(H, W, 4)`` uint8 RGBA array."""
    mips = parse_plat_mips(tex.plat_bytes, tex.width, tex.height)
    w, h, swiz = mips[0] if mips else (tex.width, tex.height, 1)
    nb = (w * h) // 2 if tex.psm == PSM_T4 else (w * h if tex.psm == PSM_T8 else w * h * 4)
    lin = decode_mip_linear(tex.image_bytes[:nb], w, h, tex.psm, swiz)
    if tex.psm == PSM_CT32:
        rgba = lin.copy()
        if scale_alpha:
            rgba[:, :, 3] = np.clip(rgba[:, :, 3].astype(np.uint16) * 2, 0, 255).astype(np.uint8)
        return rgba
    pal = te.unswizzle_clut_csm1(tex.palette_bytes, entries=tex.palette_entries or (16 if tex.psm == PSM_T4 else 256), clut_psm=tex.clut_psm, scale_alpha=scale_alpha)
    return pal[np.minimum(lin, len(pal) - 1)]


def compute_pcsx2_stems(tex: RawTPKTexture, include_sub_mips: bool = True) -> List[str]:
    """Compute exact PCSX2 replacement stem(s) across a texture's full active mip chain."""
    if tex.psm not in (PSM_T4, PSM_T8, PSM_CT32):
        return []
    mips = parse_plat_mips(tex.plat_bytes, tex.width, tex.height)
    if not mips:
        return []
    clut_h = hash_clut_ps2(tex.palette_bytes, tex.palette_entries or (16 if tex.psm == PSM_T4 else 256), tex.clut_psm) if tex.psm in (PSM_T4, PSM_T8) else None
    packed, cur = [], 0
    for mw, mh, swiz in mips:
        nb = (mw * mh) // 2 if tex.psm == PSM_T4 else (mw * mh if tex.psm == PSM_T8 else mw * mh * 4)
        if cur + nb > len(tex.image_bytes) and cur != 0:
            break
        raw = tex.image_bytes[cur : cur + nb] if cur + nb <= len(tex.image_bytes) else tex.image_bytes[:nb]
        cur += nb
        packed.append((mw, mh, gs_swizzle_level_blocks(decode_mip_linear(raw, mw, mh, tex.psm, swiz), tex.psm)))
    if not packed:
        return []
    w0, h0, _ = packed[0]
    stems = [format_pcsx2_stem(xxh3_64bits(b"".join(p[2] for p in packed)), clut_h, pcsx2_flags(tex.psm, w0, h0))]
    if include_sub_mips and len(packed) > 1:
        for k, (wk, hk, blk) in enumerate(packed):
            fl = pcsx2_flags(tex.psm, wk, hk)
            for hv in ([xxh3_64bits(b"".join(p[2] for p in packed[k:]))] if k > 0 else []) + [xxh3_64bits(blk)]:
                st = format_pcsx2_stem(hv, clut_h, fl)
                if st not in stems:
                    stems.append(st)
    return stems


def iter_tpk_raw_textures(data: bytes, archive: str, section: str = "") -> Iterator[RawTPKTexture]:
    """Yield every raw texture from all 0xB3300000 TPK chunks inside ``data`` (auto-JDLZ)."""
    if is_jdlz(data):
        data = lz_decompress(data)
    roots: List[Tuple[int, int]] = []
    te._find_tpk_roots(data, 0, len(data), roots)
    for rpos, rsz in roots:
        tis, pis, streams, sp_start, sp_size = [], [], [], None, 0
        for cid, pos, csz in te._iter_linear_chunks(data, rpos + 8, rpos + 8 + rsz):
            if cid == ti.CHUNK_TPK_INFO:
                for scid, spos, scsz in te._iter_linear_chunks(data, pos + 8, pos + 8 + csz):
                    s0 = spos + 8
                    if scid == ti.CHUNK_TPK_STREAM_TABLE:
                        streams = [struct.unpack_from("<IIII", data, s0 + i * 24) for i in range(scsz // 24)]
                    elif scid == ti.CHUNK_TPK_TEX_INFO:
                        tis = [data[s0 + i * 124 : s0 + (i + 1) * 124] for i in range(scsz // 124)]
                    elif scid == ti.CHUNK_TPK_TEX_PLAT:
                        pis = [data[s0 + i * 84 : s0 + (i + 1) * 84] for i in range(scsz // 84)]
            elif cid == ti.CHUNK_TPK_DATA:
                for dcid, dpos, dcsz in te._iter_linear_chunks(data, pos + 8, pos + 8 + csz):
                    if dcid == ti.CHUNK_TPK_DATA_PAYLOAD:
                        sp_start, sp_size = dpos + 8, dcsz
        if streams:
            for hsh, off, csz_e, _ in streams:
                if off + csz_e > len(data):
                    continue
                dec, _ = ti.decompress_ea_block(data[off : off + csz_e])
                if len(dec) < 208:
                    continue
                pi_b = dec[-84:]
                m = te._decode_texture_pair(dec[-208:-84], pi_b)
                isz, psz = m["image_size"], m["palette_size"]
                yield RawTPKTexture(archive, section, m["name"], m["name_hash"] or hsh, m["width"], m["height"], m["psm"], m["clut_psm"], m["num_palette_entries"], pi_b, dec[:isz], dec[isz : isz + psz] if psz > 0 else b"")
            continue
        if sp_start is None or not tis:
            continue
        pad = (((sp_start - rpos) + 127) & ~127) - (sp_start - rpos)
        pbase = sp_start + pad if pad > 0 and data[sp_start : sp_start + pad] == b"\x11" * pad else sp_start
        pend = sp_start + sp_size
        for i, ti_b in enumerate(tis):
            pi_b = pis[i] if i < len(pis) else b"\x00" * 84
            m = te._decode_texture_pair(ti_b, pi_b)
            ioff, poff = pbase + m["image_offset"], pbase + m["palette_offset"]
            if ioff + m["image_size"] > pend:
                continue
            pal = data[poff : poff + m["palette_size"]] if m["palette_size"] > 0 and poff + m["palette_size"] <= pend else b""
            yield RawTPKTexture(archive, section, m["name"], m["name_hash"], m["width"], m["height"], m["psm"], m["clut_psm"], m["num_palette_entries"], pi_b, data[ioff : ioff + m["image_size"]], pal)


def iter_streaml2ra_sections(l2ra_bytes: bytes, stream_bytes: bytes) -> Iterator[Tuple[str, int, bytes]]:
    """Yield ``(section_name, section_num, section_bytes)`` from L2RA.BUN + STREAML2RA.BUN."""
    pos = 0
    while pos + 8 <= len(l2ra_bytes):
        cid, csz = struct.unpack_from("<II", l2ra_bytes, pos)
        if cid == 0x00034110:
            base = pos + 8
            for i in range(csz // 92):
                rec = l2ra_bytes[base + i * 92 : base + (i + 1) * 92]
                foff, fsz = struct.unpack_from("<II", rec, 20)
                if fsz > 0 and foff + fsz <= len(stream_bytes):
                    yield rec[:8].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip(), int(struct.unpack_from("<h", rec, 8)[0]), stream_bytes[foff : foff + fsz]
            return
        pos += 8 + csz


def verify_against_dumps(iso_path: Path, dumps_dir: Path, include_all_cars: bool = False) -> Dict[str, object]:
    """Empirically verify offline hashes against real PCSX2 texture dumps."""
    arc = PS2NFSMWArchive(iso_path)
    dumps = {p.stem.lower() for p in dumps_dir.glob("*.png")}
    dumps_3part = {s for s in dumps if len(s.split("-")) == 3}
    computed_stems, computed_cluts = {}, set()

    def _scan(tex: RawTPKTexture) -> None:
        for st in compute_pcsx2_stems(tex, include_sub_mips=True):
            computed_stems.setdefault(st, (tex.archive, tex.name))
            parts = st.split("-")
            if len(parts) == 3:
                computed_cluts.add(parts[1])

    for sname, _, sbytes in iter_streaml2ra_sections(arc.read_entry(arc.find(r"TRACKS\L2RA.BUN")), arc.read_entry(arc.find(r"TRACKS\STREAML2RA.BUN"))):
        for tex in iter_tpk_raw_textures(sbytes, r"TRACKS\STREAML2RA.BUN", section=sname):
            _scan(tex)
    targets = [
        r"GLOBAL\GLOBALA.BUN", r"GLOBAL\GLOBALB.LZC", r"GLOBAL\INGAMEA.BUN", r"GLOBAL\INGAMEB.LZC",
        r"GLOBAL\DYNTEX.BIN", r"GLOBAL\HUDTEXRACE.BIN", r"TRACKS\L2RA\TRACKMAPS.BIN",
        r"CARS\BMWM3GTRE46\TEXTURES.BIN", r"CARS\TEXTURES.BIN", r"CARS\WHEELS\TEXTURES.BIN",
    ]
    if include_all_cars:
        targets.extend(sorted({e.resolved_path for e in arc.entries if e.resolved_path and e.resolved_path.startswith("CARS\\") and e.resolved_path.endswith("\\TEXTURES.BIN")}))
    for t in dict.fromkeys(targets):
        try:
            for tex in iter_tpk_raw_textures(arc.read_entry(arc.find(t)), t):
                _scan(tex)
        except KeyError:
            continue
    clut_cov = sorted(s for s in dumps_3part if s.split("-")[1] in computed_cluts)
    clut_miss = [s for s in clut_cov if s not in computed_stems]
    return {
        "total_dumps": len(dumps), "total_3part_dumps": len(dumps_3part),
        "matched_total": sum(1 for s in dumps if s in computed_stems),
        "clut_covered": len(clut_cov), "clut_matched": len(clut_cov) - len(clut_miss),
        "clut_mismatched": len(clut_miss), "mismatched_samples": clut_miss[:10],
        "unique_computed_stems": len(computed_stems),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline pure-math PCSX2 v2.6.3 texture hasher.")
    ap.add_argument("--iso", type=Path, required=True)
    ap.add_argument("--verify-dumps", type=Path, required=True)
    ap.add_argument("--all-cars", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = verify_against_dumps(args.iso.expanduser().resolve(), args.verify_dumps.expanduser().resolve(), args.all_cars)
    print(json.dumps(rep, indent=2) if args.json else f"Matched {rep['matched_total']}/{rep['total_dumps']} | CLUT-covered: {rep['clut_matched']}/{rep['clut_covered']} (mismatches={rep['clut_mismatched']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
