#!/usr/bin/env python3
"""Faithful 4x/8x BC7 HD HUD / font / menu / loading-icon pack builder (v2.3).

Builds deterministic PCSX2 replacement DDS files for the *real* SLUS-21351 UI textures
(``HUDTEX*``, ``HUDS_CUSTOM_*``, ``FRONTEND\\FRONTB.LZC``, ``LANGUAGES\\LANGUAGETEXTURES.BIN``
plus allow-listed UI names inside ``GLOBALA/GLOBALB/INGAMEA/DYNTEX``) straight from the
untouched ISO (opened read-only ``'rb'``). v2.2's world pack deny-lists exactly these.

Fidelity guards (every texture, hard-fail for fonts + loading icons):
  * Raw GS alpha is kept in ``[0..128]`` (128 = opaque) end-to-end; BC7 mode-6 alpha
    endpoints are clamped post-encode so no block decodes above 128 (or leaks 1 for 0).
  * Exact integer scale on both axes (POT in, POT out) so glyph/icon UVs stay texel-aligned.
  * Premultiplied bicubic upscale (no transparent-colour halos) + alpha-edge sharpening only
    on true cutout edges (crisp 2005 typography without thinning anti-aliased strokes).
  * Round-trip box-downsample must match the PS2 source, and must match strictly worse when
    shifted by one source texel (catches shifted/garbled loading icons), zero alpha bleed into
    texels >=2 texels from any glyph, and the swizzle decode must be the coherent one.
  * No mipmaps: forced trilinear on mipped atlases bleeds neighbouring glyphs/icons.

Outputs (gitignored ``build/``): ``replacements/<pcsx2-stem>.dds`` when the v2.2
``pcsx2_tex_hash`` module is importable, else ``textures/<archive>/<idx>_<name>_<hash>.dds``,
plus ``hud_ui_manifest.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import install_macos_texture_tools as imtt  # noqa: E402
import tpk_extract as te  # noqa: E402
import tpk_inspect as tpki  # noqa: E402
from jdlz import is_jdlz, lz_decompress  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

try:  # v2.2 offline PCSX2 hasher (optional; names files by real PCSX2 stems when present)
    import pcsx2_tex_hash as _pth  # type: ignore
except ImportError:  # pragma: no cover - depends on branch layout
    _pth = None

DEFAULT_OUT_DIR = _REPO_ROOT / "build" / "hud_ui_pack_v23"
DEFAULT_ISO = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
UI_ARCHIVES: Tuple[str, ...] = (
    r"GLOBAL\HUDTEXRACE.BIN", r"GLOBAL\HUDTEXDRAG.BIN", r"GLOBAL\HUDTEXSPLIT.BIN",
    r"GLOBAL\HUDTEXDRAGSPLIT.BIN", r"GLOBAL\HUDTEXTURESPHOTOFINISH.BIN",
    *(rf"GLOBAL\HUDS_CUSTOM_{i:02d}.BIN" for i in range(11)),
    r"FRONTEND\FRONTB.LZC", r"LANGUAGES\LANGUAGETEXTURES.BIN",
)
MIXED_ARCHIVES: Tuple[str, ...] = (r"GLOBAL\GLOBALA.BUN", r"GLOBAL\GLOBALB.LZC", r"GLOBAL\INGAMEA.BUN", r"GLOBAL\DYNTEX.BIN")
UI_NAME_RE = re.compile(
    r"^(FONT_|LOAD|TIP_|MW_LOGO|GENERIC_|(MAIN|INGAME|OPTIONS|MODE|MINIMAP|BOUNTY)_ICON_|MILESTONE_|OL_|"
    r"(BUSTED|EVADED|DEFEATED|IMPOUNDED)_|LOGITECH_WHEEL|DRAG_RPM_|HEAT_ICON|TAC_NUMBERS|DPAD|"
    r"(LEFT|RIGHT)_ANALOG|WORLD_|(TIME|POSITION|OUTRUN|MIRROR)_BACKING|INFO_FLASHER|TRAX_|MINIMAP_MASK|"
    r"CINGULAR_LOGO|[LR][123](_|$)|(CROSS|CIRCLE|SQUARE|TRIANGLE|START|SELECT)$)|_ICON$"
)
STRICT_CATEGORIES = ("font", "loading")
CRISP_GAIN = 2.5
MAX_ALPHA_MAE, MAX_PREMUL_MAE = 4.0, 6.0  # round-trip tolerances (alpha in /128, RGB in /255)
DXGI_FORMAT_BC7_UNORM = 98
_W4 = np.array([0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64], dtype=np.int32)

@dataclass
class RawTex:
    archive: str
    container: int
    index: int
    name: str
    name_hash: int
    width: int
    height: int
    psm: int
    clut_psm: int
    pal_entries: int
    swizzled: bool
    plat: bytes
    image: bytes
    palette: bytes

def classify(name: str, archive: str) -> Optional[str]:
    """Return ``font|loading|hud|menu`` for UI textures, ``None`` for world/FX textures."""
    n, arc = name.upper(), archive.upper()
    if arc in (a.upper() for a in MIXED_ARCHIVES) and not UI_NAME_RE.search(n):
        return None
    if n.startswith("FONT_") or "NUMBERS" in n:
        return "font"
    if n.startswith(("LOAD", "TIP_", "LOGITECH_WHEEL")) or n in ("GENERIC_AUTOSAVE", "GENERIC_ICON_LOAD", "GENERIC_ICON_SAVE"):
        return "loading"
    if "HUD" in arc or "INGAME" in arc or n.startswith(("MINIMAP", "HEAT_", "DRAG_RPM_")):
        return "hud"
    return "menu"

def iter_raw_textures(data: bytes, archive: str) -> Iterator[RawTex]:
    """Yield raw (still swizzled) TPK textures from every 0xB3300000 root in ``data``."""
    if is_jdlz(data):
        data = lz_decompress(data)
    roots: List[Tuple[int, int]] = []
    te._find_tpk_roots(data, 0, len(data), roots)
    for ci, (rpos, rsz) in enumerate(roots):
        tis: List[bytes] = []
        pis: List[bytes] = []
        streams: List[Tuple[int, ...]] = []
        sp_start, sp_size = None, 0
        for cid, pos, csz in te._iter_linear_chunks(data, rpos + 8, rpos + 8 + rsz):
            if cid == tpki.CHUNK_TPK_INFO:
                for scid, spos, scsz in te._iter_linear_chunks(data, pos + 8, pos + 8 + csz):
                    s0 = spos + 8
                    if scid == tpki.CHUNK_TPK_STREAM_TABLE:
                        streams = [struct.unpack_from("<IIII", data, s0 + i * 24) for i in range(scsz // 24)]
                    elif scid == tpki.CHUNK_TPK_TEX_INFO:
                        tis = [data[s0 + i * 124: s0 + (i + 1) * 124] for i in range(scsz // 124)]
                    elif scid == tpki.CHUNK_TPK_TEX_PLAT:
                        pis = [data[s0 + i * 84: s0 + (i + 1) * 84] for i in range(scsz // 84)]
            elif cid == tpki.CHUNK_TPK_DATA:
                for dcid, dpos, dcsz in te._iter_linear_chunks(data, pos + 8, pos + 8 + csz):
                    if dcid == tpki.CHUNK_TPK_DATA_PAYLOAD:
                        sp_start, sp_size = dpos + 8, dcsz
        records: List[Tuple[bytes, bytes, bytes, int, int]] = []  # (ti, pi, blob, base, end); base<0 = streamed
        if streams:
            for _hsh, off, csz_e, _ in streams:
                if off + csz_e <= len(data):
                    dec, _ = tpki.decompress_ea_block(data[off: off + csz_e])
                    if len(dec) >= 208:
                        records.append((dec[-208:-84], dec[-84:], dec, -1, len(dec)))
        elif sp_start is not None:
            pad = (((sp_start - rpos) + 127) & ~127) - (sp_start - rpos)
            base = sp_start + pad if pad and data[sp_start: sp_start + pad] == b"\x11" * pad else sp_start
            for i, ti_b in enumerate(tis):
                records.append((ti_b, pis[i] if i < len(pis) else b"\x00" * 84, data, base, sp_start + sp_size))
        for ti_i, (ti_b, pi_b, blob, base, end) in enumerate(records):
            m = te._decode_texture_pair(ti_b, pi_b)
            ioff = 0 if base < 0 else base + m["image_offset"]
            poff = m["image_size"] if base < 0 else base + m["palette_offset"]
            if ioff + m["image_size"] > end or m["width"] <= 0 or m["height"] <= 0:
                continue
            pal = blob[poff: poff + m["palette_size"]] if m["palette_size"] > 0 else b""
            yield RawTex(archive, ci, ti_i, m["name"], m["name_hash"], m["width"], m["height"], m["psm"],
                         m["clut_psm"], m["num_palette_entries"], m["is_swizzled"], pi_b,
                         blob[ioff: ioff + m["image_size"]], pal)

def decode_raw(rt: RawTex, swizzled: Optional[bool] = None) -> np.ndarray:
    """Decode mip 0 with raw PS2 GS alpha (0..128, never promoted to 255)."""
    img = te.decode_ps2_texture(rt.image, rt.palette, rt.width, rt.height, rt.psm, rt.clut_psm, rt.pal_entries,
                                rt.swizzled if swizzled is None else swizzled, scale_alpha=False)
    return np.asarray(img, dtype=np.uint8)

def swizzle_coherence(rt: RawTex) -> Dict[str, Any]:
    """Garble guard: the chosen GS swizzle decode must be at least as coherent as the flipped one."""
    def tv(a: np.ndarray) -> float:
        f = a.astype(np.float32)
        return float(np.abs(np.diff(f, axis=0)).mean() + np.abs(np.diff(f, axis=1)).mean())
    chosen, alt = tv(decode_raw(rt)), tv(decode_raw(rt, not rt.swizzled))
    return {"tv": round(chosen, 3), "tv_flipped": round(alt, 3), "ok": chosen <= alt * 1.02 + 1e-6}

def choose_scale(w: int, h: int, category: str, scale: int = 4, small_scale: int = 8, max_size: int = 4096) -> int:
    """8x for <=64 px icons and font sheets, else 4x; halve until it fits ``max_size``."""
    s = small_scale if (max(w, h) <= 64 or category == "font") else scale
    while s > 1 and max(w, h) * s > max_size:
        s //= 2
    return s

def _resize_f(ch: np.ndarray, w: int, h: int) -> np.ndarray:
    return np.asarray(Image.fromarray(ch.astype(np.float32)).resize((w, h), Image.Resampling.BICUBIC), dtype=np.float32)

def _local_extrema(a: np.ndarray, r: int) -> Tuple[np.ndarray, np.ndarray]:
    p = np.pad(a, r, mode="edge")
    win = [p[dy: dy + a.shape[0], dx: dx + a.shape[1]] for dy in range(2 * r + 1) for dx in range(2 * r + 1)]
    return np.min(win, axis=0), np.max(win, axis=0)

def upscale_ps2_rgba(src: np.ndarray, s: int, crisp: bool = True) -> np.ndarray:
    """Premultiplied bicubic integer upscale keeping GS alpha in [0..128] and texel alignment."""
    h, w = src.shape[:2]
    W, H = w * s, h * s
    a = np.clip(src[:, :, 3].astype(np.float32), 0, 128) / 128.0
    rgb = src[:, :, :3].astype(np.float32)
    if float(a.min()) >= 1.0:
        out_rgb = np.dstack([_resize_f(rgb[:, :, c], W, H) for c in range(3)])
        return np.dstack([np.clip(np.round(out_rgb), 0, 255), np.full((H, W), 128)]).astype(np.uint8)
    A = np.clip(_resize_f(a, W, H), 0.0, 1.0)
    P = np.dstack([_resize_f(rgb[:, :, c] * a, W, H) for c in range(3)])
    nearest = np.repeat(np.repeat(rgb, s, axis=0), s, axis=1)
    out_rgb = np.where(A[:, :, None] > 1.0 / 256, P / np.maximum(A, 1.0 / 256)[:, :, None], nearest)
    if crisp:  # sharpen only genuine 0<->opaque cutout edges; keep AA strokes and soft glows faithful
        lo, hi = _local_extrema(a, 1)
        edge = np.repeat(np.repeat((lo <= 0.1) & (hi >= 0.9), s, axis=0), s, axis=1)
        A = np.where(edge, np.clip((A - 0.5) * CRISP_GAIN + 0.5, 0.0, 1.0), A)
    out_a = np.round(A * 128.0)
    return np.dstack([np.clip(np.round(out_rgb), 0, 255), out_a]).astype(np.uint8)

def _premul_down(hd: np.ndarray, s: int) -> np.ndarray:
    H, W = hd.shape[:2]
    f = hd.astype(np.float32)
    pm = np.dstack([f[:, :, :3] * (f[:, :, 3:4] / 128.0), f[:, :, 3:4]])
    return pm.reshape(H // s, s, W // s, s, 4).mean(axis=(1, 3))

def validate_hd(src: np.ndarray, hd: np.ndarray, s: int) -> Dict[str, Any]:
    """Round-trip, alignment (+-1 texel shift), alpha-range and glyph-bleed checks."""
    h, w = src.shape[:2]
    res: Dict[str, Any] = {"scale": s, "size": [w * s, h * s]}
    if hd.shape != (h * s, w * s, 4):
        return {**res, "ok": False, "error": f"bad HD shape {hd.shape}"}
    f = src.astype(np.float32)
    ref = np.dstack([f[:, :, :3] * (np.minimum(f[:, :, 3:4], 128) / 128.0), np.minimum(f[:, :, 3:4], 128)])

    def err(img: np.ndarray) -> Tuple[float, float]:
        d = np.abs(_premul_down(img, s) - ref)
        return float(d[:, :, 3].mean()), float(d[:, :, :3].mean())

    a_mae, rgb_mae = err(hd)
    shifted = [sum(err(np.roll(hd, k * s, axis=ax))) for ax in (0, 1) for k in (-1, 1)]
    src_a = np.minimum(src[:, :, 3], 128)
    _, near = _local_extrema(src_a.astype(np.float32), 2)
    hd_blockmax = hd[:, :, 3].reshape(h, s, w, s).max(axis=(1, 3))
    bleed = int(np.count_nonzero((near == 0) & (hd_blockmax > 0)))
    res.update(alpha_mae=round(a_mae, 3), premul_rgb_mae=round(rgb_mae, 3), shift_margin=round(min(shifted) - (a_mae + rgb_mae), 3),
               hd_alpha_max=int(hd[:, :, 3].max()), src_alpha_over_128=int(np.count_nonzero(src[:, :, 3] > 128)), bleed_texels=bleed)
    flat = float(np.ptp(ref, axis=(0, 1)).max()) < 1e-6 or min(shifted) <= 1e-6
    res["ok"] = bool(a_mae <= MAX_ALPHA_MAE and rgb_mae <= MAX_PREMUL_MAE and res["hd_alpha_max"] <= 128
                     and bleed == 0 and (flat or res["shift_margin"] > 0))
    return res

def _bc7_split(blocks: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    b = np.ascontiguousarray(blocks, dtype=np.uint8)
    return b[:, :8].copy().view("<u8")[:, 0], b[:, 8:].copy().view("<u8")[:, 0]

def _bits(x: np.ndarray, off: int, n: int) -> np.ndarray:
    return ((x >> np.uint64(off)) & np.uint64((1 << n) - 1)).astype(np.int32)

def clamp_bc7_ps2_alpha(blocks: np.ndarray) -> Tuple[np.ndarray, int]:
    """Clamp BC7 mode-6 alpha endpoints to the PS2 GS range: >128 -> 128, 1 -> 0 (p-bit cleared)."""
    lo, hi = _bc7_split(blocks)
    if not bool(np.all((lo & np.uint64(0x7F)) == np.uint64(0x40))):
        raise ValueError("expected BC7 mode-6 blocks only")
    fixed = 0
    for e, (aoff, pbit_word, pbit) in enumerate(((49, "lo", 63), (56, "hi", 0))):
        a7 = _bits(lo, aoff, 7)
        p = _bits(lo if pbit_word == "lo" else hi, pbit, 1)
        over, one = ((a7 << 1) | p) > 128, (a7 == 0) & (p == 1)
        fix = over | one
        fixed += int(np.count_nonzero(fix))
        new_a7 = np.where(over, 64, a7).astype(np.uint64)
        lo = np.where(fix, (lo & ~np.uint64(0x7F << aoff)) | (new_a7 << np.uint64(aoff)), lo)
        mask = np.uint64(1 << pbit)
        if pbit_word == "lo":
            lo = np.where(fix, lo & ~mask, lo)
        else:
            hi = np.where(fix, hi & ~mask, hi)
    out = np.concatenate([lo.astype("<u8").view(np.uint8).reshape(-1, 8), hi.astype("<u8").view(np.uint8).reshape(-1, 8)], axis=1)
    return out, fixed

def decode_bc7_mode6(blocks: np.ndarray, w: int, h: int) -> np.ndarray:
    """Vectorised BC7 mode-6 decoder (validation only)."""
    lo, hi = _bc7_split(blocks)
    p0, p1 = _bits(lo, 63, 1), _bits(hi, 0, 1)
    e0 = np.stack([(_bits(lo, 7 + c * 14, 7) << 1) | p0 for c in range(4)], axis=1)
    e1 = np.stack([(_bits(lo, 14 + c * 14, 7) << 1) | p1 for c in range(4)], axis=1)
    idx = np.stack([_bits(hi, 1, 3)] + [_bits(hi, 4 + 4 * (i - 1), 4) for i in range(1, 16)], axis=1)
    wt = _W4[idx][:, :, None]
    px = (((64 - wt) * e0[:, None, :] + wt * e1[:, None, :] + 32) >> 6).astype(np.uint8)
    bw, bh = (w + 3) // 4, (h + 3) // 4
    return px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)[:h, :w]

def bc7_dds_header(w: int, h: int) -> bytes:
    """148-byte DX10 BC7_UNORM DDS header, single mip level."""
    top = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16
    return (b"DDS " + struct.pack("<7I", 124, 0x81007, h, w, top, 0, 1) + b"\x00" * 44
            + struct.pack("<II4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0) + struct.pack("<5I", 0x1000, 0, 0, 0, 0)
            + struct.pack("<5I", DXGI_FORMAT_BC7_UNORM, 3, 0, 1, 0))

def encode_bc7_batch(images: Sequence[np.ndarray], encoder: Path) -> List[bytes]:
    """Encode RGBA arrays to raw single-mip BC7 payloads via ``rockport_dds --batch``."""
    if not images:
        return []
    with tempfile.TemporaryDirectory(prefix="rp_v23_") as tmp:
        src, dst = Path(tmp) / "in.rkb1", Path(tmp) / "out.rko1"
        with src.open("wb") as fh:
            fh.write(b"RKB1" + struct.pack("<I", len(images)))
            for im in images:
                raw = np.ascontiguousarray(im, dtype=np.uint8).tobytes()
                fh.write(struct.pack("<4I", im.shape[1], im.shape[0], 0, len(raw)) + raw)
        proc = subprocess.run([str(encoder), "--batch", str(src), "--output", str(dst), "--format", "bc7"],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"rockport_dds batch failed: {proc.stderr.strip() or proc.stdout.strip()}")
        out = dst.read_bytes()
    if out[:4] != b"RKO1" or struct.unpack_from("<I", out, 4)[0] != len(images):
        raise RuntimeError("invalid RKO1 batch output")
    pos, payloads = 8, []
    for _ in images:
        _w, _h, _m, plen = struct.unpack_from("<4I", out, pos)
        payloads.append(out[pos + 16: pos + 16 + plen])
        pos += 16 + plen
    return payloads

def pcsx2_stem(rt: RawTex) -> Optional[str]:
    if _pth is None:
        return None
    tex = _pth.RawTPKTexture(archive=rt.archive, section="", name=rt.name, name_hash=rt.name_hash, width=rt.width,
                             height=rt.height, psm=rt.psm, clut_psm=rt.clut_psm, palette_entries=rt.pal_entries,
                             plat_bytes=rt.plat, image_bytes=rt.image, palette_bytes=rt.palette)
    stems = _pth.compute_pcsx2_stems(tex, include_sub_mips=False)
    return stems[0] if stems else None

def finish_texture(hd: np.ndarray, payload: bytes) -> Tuple[bytes, Dict[str, Any]]:
    """Clamp BC7 alpha to [0..128], decode-verify, and return the final DDS bytes + metrics."""
    H, W = hd.shape[:2]
    blocks, fixed = clamp_bc7_ps2_alpha(np.frombuffer(payload, dtype=np.uint8).reshape(-1, 16))
    dec = decode_bc7_mode6(blocks, W, H)
    da = dec[:, :, 3].astype(np.int32)
    zero = hd[:, :, 3] == 0
    m = {"bc7_alpha_max": int(da.max()), "bc7_alpha_fixed_endpoints": fixed,
         "bc7_alpha_mae": round(float(np.abs(da - hd[:, :, 3]).mean()), 3),
         "bc7_zero_alpha_kept": round(float((da[zero] == 0).mean()) if zero.any() else 1.0, 4)}
    m["bc7_ok"] = m["bc7_alpha_max"] <= 128 and m["bc7_alpha_mae"] <= 2.0
    return bc7_dds_header(W, H) + blocks.tobytes(), m

def build_pack(iso: Path, out_dir: Path, *, archives: Optional[Sequence[str]] = None, only: Sequence[str] = (),
               encoder: Optional[Path] = None, scale: int = 4, small_scale: int = 8, max_size: int = 4096,
               batch_bytes: int = 96 << 20) -> Dict[str, Any]:
    """Build the HD UI pack from an untouched ISO; returns the manifest dict."""
    encoder = encoder or imtt.find_rockport_dds() or imtt.build_rockport_dds(release=True)
    arc = PS2NFSMWArchive(iso)
    jobs: List[Dict[str, Any]] = []
    for vp in archives or (UI_ARCHIVES + MIXED_ARCHIVES):
        entry = arc.find(vp)
        if entry is None:
            continue
        for rt in iter_raw_textures(arc.read_entry(entry), vp):
            cat = classify(rt.name, vp)
            if cat is None or (only and cat not in only):
                continue
            src = decode_raw(rt)
            s = choose_scale(rt.width, rt.height, cat, scale, small_scale, max_size)
            hd = upscale_ps2_rgba(src, s)
            rec = {"archive": vp, "container": rt.container, "index": rt.index, "name": rt.name,
                   "name_hash": f"0x{rt.name_hash:08x}", "category": cat, "psm": rt.psm, "src_size": [rt.width, rt.height],
                   "swizzle": swizzle_coherence(rt), "hd": validate_hd(src, hd, s), "pcsx2_stem": pcsx2_stem(rt)}
            slug = vp.replace("\\", "_").rsplit(".", 1)[0]
            rec["file"] = (f"replacements/{rec['pcsx2_stem']}.dds" if rec["pcsx2_stem"]
                           else f"textures/{slug}/{rt.container}_{rt.index:03d}_{te._sanitize_name(rt.name, 'tex')}_{rt.name_hash:08x}.dds")
            jobs.append({"rec": rec, "hd": hd})
    jobs.sort(key=lambda j: (j["rec"]["archive"], j["rec"]["container"], j["rec"]["index"]))
    by_digest: Dict[str, Tuple[bytes, Dict[str, Any]]] = {}
    pending: List[Dict[str, Any]] = []

    def flush() -> None:
        uniq = {j["digest"]: j["hd"] for j in pending if j["digest"] not in by_digest}
        for (dg, hd), payload in zip(uniq.items(), encode_bc7_batch(list(uniq.values()), encoder)):
            by_digest[dg] = finish_texture(hd, payload)
        pending.clear()

    for j in jobs:
        ok_src = j["rec"]["swizzle"]["ok"] and j["rec"]["hd"]["ok"]
        j["digest"] = hashlib.sha256(j["hd"].tobytes() + str(j["hd"].shape).encode()).hexdigest()
        if ok_src:
            pending.append(j)
            if sum(p["hd"].nbytes for p in pending) >= batch_bytes:
                flush()
    flush()
    written: Dict[str, str] = {}
    failures: List[Dict[str, Any]] = []
    for j in jobs:
        rec = j["rec"]
        if j["digest"] in by_digest:
            dds, bc7 = by_digest[j["digest"]]
            rec["bc7"] = bc7
            rec["sha256"] = hashlib.sha256(dds).hexdigest()
        rec["ok"] = bool(rec["swizzle"]["ok"] and rec["hd"]["ok"] and rec.get("bc7", {}).get("bc7_ok", False))
        if rec["ok"] and written.get(rec["file"], rec["sha256"]) != rec["sha256"]:
            rec["ok"], rec["error"] = False, "pcsx2 stem collision with different content"
        if not rec["ok"]:
            failures.append(rec)
            continue
        if rec["file"] not in written:
            dest = out_dir / rec["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(dds)
            written[rec["file"]] = rec["sha256"]
    recs = [j["rec"] for j in jobs]
    cats = sorted({r["category"] for r in recs})
    manifest = {
        "schema": "rockport.hud_ui_pack.v1", "iso_name": iso.name, "naming": "pcsx2_stem" if _pth else "tpk_identity",
        "alpha_range": [0, 128], "mipmaps": False,
        "summary": {"textures": len(recs), "files_written": len(written), "failed": len(failures),
                    "strict_failed": sum(1 for r in failures if r["category"] in STRICT_CATEGORIES),
                    "by_category": {c: sum(1 for r in recs if r["category"] == c) for c in cats},
                    "pack_sha256": hashlib.sha256("".join(sorted(written.values())).encode()).hexdigest()},
        "textures": recs,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hud_ui_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest

def resolve_iso(cli: Optional[str]) -> Optional[Path]:
    raw = cli or os.environ.get("NFSMW_ISO", "").strip()
    p = Path(raw).expanduser() if raw else DEFAULT_ISO
    return p if p.is_file() else None

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iso", help="Untouched SLUS-21351 ISO (or NFSMW_ISO); opened read-only")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--archive", action="append", default=None, help="Restrict to virtual archive path(s)")
    ap.add_argument("--only", action="append", default=[], choices=["font", "loading", "hud", "menu"])
    ap.add_argument("--scale", type=int, default=4, choices=[2, 4, 8])
    ap.add_argument("--small-scale", type=int, default=8, choices=[4, 8])
    ap.add_argument("--max-size", type=int, default=4096)
    args = ap.parse_args(argv)
    iso = resolve_iso(args.iso)
    if iso is None:
        print("error: ISO not found (pass --iso or set NFSMW_ISO)", file=sys.stderr)
        return 2
    m = build_pack(iso, args.out, archives=args.archive, only=args.only, scale=args.scale,
                   small_scale=args.small_scale, max_size=args.max_size)
    s = m["summary"]
    print(f"[hud-ui] {s['textures']} UI textures -> {s['files_written']} BC7 DDS ({m['naming']}), "
          f"failed={s['failed']} strict_failed={s['strict_failed']} by_category={s['by_category']}")
    for r in m["textures"]:
        if not r["ok"]:
            print(f"  FAIL [{r['category']}] {r['archive']} #{r['index']} {r['name']}: swizzle={r['swizzle']} hd={r['hd']} bc7={r.get('bc7')}")
    return 1 if s["strict_failed"] else 0

if __name__ == "__main__":
    raise SystemExit(main())
