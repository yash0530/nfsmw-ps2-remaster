#!/usr/bin/env python3
"""Physically-Motivated 3D Surface Relief, Normal-Mapped Bevels & Baked AO (v2.5 + v2.9 polish).

Pure NumPy + Pillow deterministic offline texture shader for NFSMW: Rockport 2026.
Bakes multi-scale height-field normals, Rockport late-afternoon directional sun
shading, cool sky-fill crevice ambient occlusion, and material-aware micro-relief
into upscaled RGBA textures while preserving 2005 art, palette, and PS2 [0..128]
alpha channels. The v2.9 ``polished`` mode adds recessed reflective window glass,
gentle cornice-grit coring, a grounded car contact shadow (GLOBALB ``CARSHADOW``)
and PS2-aware coverage-preserving foliage mip chains.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image

RELIEF_MODES: Tuple[str, ...] = ("faithful", "sculpted", "polished")

# Rockport late-afternoon directional sun vector (upper-left key light in image XY, +Z out)
_SUN_RAW = np.array([-0.54, -0.68, 0.496], dtype=np.float32)
SUN_DIR: Tuple[float, float, float] = tuple((_SUN_RAW / float(np.linalg.norm(_SUN_RAW))).tolist())
_HALF_RAW = _SUN_RAW / float(np.linalg.norm(_SUN_RAW)) + np.array([0.0, 0.0, 1.0], dtype=np.float32)
HALF_DIR: Tuple[float, float, float] = tuple((_HALF_RAW / float(np.linalg.norm(_HALF_RAW))).tolist())


@dataclasses.dataclass(frozen=True)
class MaterialReliefProfile:
    """Physically-motivated relief & baked-AO tuning profile for one material class."""

    name: str
    ao_strength: float
    ao_gain: float
    bump_scale: float
    sun_strength: float
    spec_strength: float
    spec_exp: float
    clarity: float
    grain_bump: float
    wrap_edges: bool
    warm_tint: Tuple[float, float, float] = (1.10, 1.01, 0.87)
    cool_pull: Tuple[float, float, float] = (1.07, 1.00, 0.88)


MATERIAL_PROFILES: Dict[str, MaterialReliefProfile] = {
    "architecture": MaterialReliefProfile(
        name="architecture",
        ao_strength=0.66,
        ao_gain=4.4,
        bump_scale=3.6,
        sun_strength=0.78,
        spec_strength=0.14,
        spec_exp=18.0,
        clarity=0.42,
        grain_bump=0.07,
        wrap_edges=True,
    ),
    "road": MaterialReliefProfile(
        name="road",
        ao_strength=0.56,
        ao_gain=4.0,
        bump_scale=3.0,
        sun_strength=0.60,
        spec_strength=0.20,
        spec_exp=20.0,
        clarity=0.38,
        grain_bump=0.26,
        wrap_edges=True,
        warm_tint=(1.06, 1.00, 0.92),
        cool_pull=(1.05, 1.00, 0.91),
    ),
    "vehicle": MaterialReliefProfile(
        name="vehicle",
        ao_strength=0.60,
        ao_gain=4.2,
        bump_scale=3.1,
        sun_strength=0.54,
        spec_strength=0.24,
        spec_exp=26.0,
        clarity=0.32,
        grain_bump=0.0,
        wrap_edges=False,
        warm_tint=(1.05, 1.00, 0.94),
        cool_pull=(1.04, 1.00, 0.92),
    ),
    "foliage": MaterialReliefProfile(
        name="foliage",
        ao_strength=0.58,
        ao_gain=3.8,
        bump_scale=2.9,
        sun_strength=0.62,
        spec_strength=0.07,
        spec_exp=14.0,
        clarity=0.34,
        grain_bump=0.0,
        wrap_edges=False,
        warm_tint=(1.12, 1.03, 0.84),
        cool_pull=(1.06, 0.98, 0.90),
    ),
    "signage": MaterialReliefProfile(
        name="signage",
        ao_strength=0.0,
        ao_gain=1.0,
        bump_scale=0.0,
        sun_strength=0.0,
        spec_strength=0.0,
        spec_exp=16.0,
        clarity=0.28,
        grain_bump=0.0,
        wrap_edges=False,
    ),
    "sky": MaterialReliefProfile(
        name="sky",
        ao_strength=0.0,
        ao_gain=1.0,
        bump_scale=0.0,
        sun_strength=0.0,
        spec_strength=0.0,
        spec_exp=16.0,
        clarity=0.0,
        grain_bump=0.0,
        wrap_edges=False,
    ),
    "shadow_map": MaterialReliefProfile(
        name="shadow_map",
        ao_strength=0.0,
        ao_gain=1.0,
        bump_scale=0.0,
        sun_strength=0.0,
        spec_strength=0.0,
        spec_exp=16.0,
        clarity=0.0,
        grain_bump=0.0,
        wrap_edges=False,
    ),
}

_SIGNAGE_SUBSTRINGS = ("_SIGN", "_PLATE", "LICENSE", "BADGE", "BADGING", "LOGO")
_VEHICLE_PREFIXES = (
    "CAR_", "WHEEL", "TIRE", "BRAKE", "RIM", "TREAD", "ROTOR", "GRILL",
    "CARBON", "INTERCOOLER", "RADIATOR", "DOORLINE", "DAMAGE0", "XO_TIRE",
)
_FOLIAGE_PREFIXES = ("ORG_", "TREE", "BUSH", "LEAF", "HEDGE", "FOLIAGE", "MD_GRASS", "TRNS_GRASS")
_FOLIAGE_SUBSTRINGS = ("_TREE", "TREELINE", "FALLTREE", "_BUSH", "_LEAF", "LEAVES", "FLORA", "HEDGE", "BARK", "TRUNK")
_ROAD_PREFIXES = ("RO_", "TRNS_", "SDW_", "CRB_", "ASPH", "PAVE", "RDP_", "SKID_", "TIREPATCH", "TER_COBBLE")
_ARCH_PREFIXES = ("ARC_", "ARK_", "BLD_", "WAL_", "CON_", "BRK_", "WIN_", "OBJ_", "TUN_", "XW_", "XO_")
_ROAD_SUBSTRINGS = ("ASPH", "PAVE", "SIDEWALK", "CURB", "COBBLE", "_ROAD", "CONCRETE")


def classify_relief_material(name: str, archive: str = "") -> str:
    """Classify a PS2 Rockport texture into its tuned 3D relief material class."""
    nm = name.strip().upper()
    arc_u = archive.replace("/", "\\").upper()
    if nm.startswith("SHD_"):
        return "shadow_map"
    if nm.startswith(("SKY_", "PAN_", "ANM_WATER")):
        return "sky"
    if nm.startswith(("SGN_", "ADS_")) or any(t in nm for t in _SIGNAGE_SUBSTRINGS):
        return "signage"
    if arc_u.startswith(("CARS\\", "WHEELS\\")) or arc_u == r"GLOBAL\GLOBALB.LZC" or nm.startswith(_VEHICLE_PREFIXES):
        return "vehicle"
    if nm.startswith(_FOLIAGE_PREFIXES) or any(t in nm for t in _FOLIAGE_SUBSTRINGS):
        return "foliage"
    if nm.startswith(_ROAD_PREFIXES):
        return "road"
    if nm.startswith(_ARCH_PREFIXES):
        return "architecture"
    if any(t in nm for t in _ROAD_SUBSTRINGS):
        return "road"
    return "architecture"


def _effective_profile(mat: str, name: str) -> MaterialReliefProfile:
    """Return base profile, with gentle sub-surface specialization for vehicle parts."""
    prof = MATERIAL_PROFILES.get(mat, MATERIAL_PROFILES["architecture"])
    if mat != "vehicle":
        return prof
    nm = name.strip().upper()
    if any(k in nm for k in ("TIRE", "TREAD")):
        return dataclasses.replace(prof, ao_strength=0.68, bump_scale=3.8, sun_strength=0.64, spec_strength=0.08)
    if any(k in nm for k in ("ROTOR", "BRAKE", "WHEEL", "RIM", "INTERCOOLER", "RADIATOR")):
        return dataclasses.replace(prof, ao_strength=0.64, bump_scale=3.4, spec_strength=0.30, spec_exp=30.0)
    if any(k in nm for k in ("CARBON", "GRILL")):
        return dataclasses.replace(prof, ao_strength=0.70, bump_scale=3.6, sun_strength=0.62, spec_strength=0.24)
    if any(k in nm for k in ("HEADL", "BRAKEL")):
        return dataclasses.replace(prof, ao_strength=0.45, bump_scale=2.5, sun_strength=0.42, spec_strength=0.16)
    return prof


def _pyr_blur(arr: np.ndarray, factor: int, wrap: bool) -> np.ndarray:
    """Fast isotropic multi-scale blur via Pillow C bilinear pyramid (GIL-released)."""
    h, w = arr.shape
    if factor <= 1 or min(h, w) <= 4:
        return arr
    pad = min(factor * 2, h // 2, w // 2) if wrap else 0
    src = np.pad(arr, ((pad, pad), (pad, pad)), mode="wrap") if pad > 0 else arr
    sh, sw = src.shape
    dh, dw = max(2, sh // factor), max(2, sw // factor)
    im = Image.fromarray(src)
    up = np.asarray(im.resize((dw, dh), Image.Resampling.BILINEAR).resize((sw, sh), Image.Resampling.BILINEAR), dtype=np.float32)
    return up[pad : pad + h, pad : pad + w] if pad > 0 else up


def _blur3x3(arr: np.ndarray, wrap: bool) -> np.ndarray:
    p = np.pad(arr, ((1, 1), (1, 1)), mode="wrap" if wrap else "edge")
    return (
        p[:-2, :-2] + 2.0 * p[:-2, 1:-1] + p[:-2, 2:]
        + 2.0 * p[1:-1, :-2] + 4.0 * p[1:-1, 1:-1] + 2.0 * p[1:-1, 2:]
        + p[2:, :-2] + 2.0 * p[2:, 1:-1] + p[2:, 2:]
    ) * np.float32(0.0625)


def _strided_grad(field: np.ndarray, stride: int, wrap: bool) -> Tuple[np.ndarray, np.ndarray]:
    """Scale-invariant central difference across a bevel stride (immune to 4x upscaling attenuation)."""
    s = max(1, min(stride, field.shape[0] // 2, field.shape[1] // 2))
    p = np.pad(field, ((s, s), (s, s)), mode="wrap" if wrap else "edge")
    gx = (p[s:-s, 2 * s :] - p[s:-s, : -2 * s]) * np.float32(0.5)
    gy = (p[2 * s :, s:-s] - p[: -2 * s, s:-s]) * np.float32(0.5)
    return gx, gy


def _tileable_micro_grad(h: int, w: int, name: str) -> Tuple[np.ndarray, np.ndarray]:
    """Deterministic, 100% tile-seamless 2-octave micro-aggregate normal slopes (gx, gy)."""
    seed = int.from_bytes(hashlib.blake2b(f"v25:{name}".encode("utf-8"), digest_size=4).digest(), "little")
    rng = np.random.default_rng(seed)
    th, tw = min(h, 128), min(w, 128)
    g_hi = rng.normal(0.0, 1.0, size=(th, tw)).astype(np.float32)
    g_hi = 0.55 * g_hi + 0.45 * _blur3x3(g_hi, wrap=True)
    ch, cw = max(8, th // 3), max(8, tw // 3)
    g_lo_small = rng.normal(0.0, 1.0, size=(ch, cw)).astype(np.float32)
    g_lo = np.asarray(Image.fromarray(g_lo_small).resize((tw, th), Image.Resampling.BILINEAR), dtype=np.float32)
    tile = 0.58 * g_hi + 0.42 * g_lo
    tile = (tile - float(tile.mean())) / (float(tile.std()) + 1e-5)
    gx_t, gy_t = _strided_grad(tile, 1, wrap=True)
    reps = ((h + th - 1) // th, (w + tw - 1) // tw)
    return (
        np.tile(gx_t, reps)[:h, :w].astype(np.float32, copy=False),
        np.tile(gy_t, reps)[:h, :w].astype(np.float32, copy=False),
    )


def _dilate_cutout_rgb(rgb: np.ndarray, alpha: np.ndarray, threshold: int = 16, iters: int = 6) -> np.ndarray:
    """Outward alpha-edge RGB dilation to eliminate transparent cutout dark/white fringes."""
    out, valid = rgb.astype(np.float32, copy=True), alpha >= threshold
    if valid.all() or not valid.any():
        return rgb.copy()
    h, w = valid.shape
    for _ in range(iters):
        if valid.all():
            break
        acc, cnt = np.zeros_like(out), np.zeros((h, w), dtype=np.float32)
        prgb = np.pad(out, ((1, 1), (1, 1), (0, 0)), mode="edge")
        pv = np.pad(valid, ((1, 1), (1, 1)), mode="edge")
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    nv = pv[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
                    acc += prgb[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w] * nv[:, :, None]
                    cnt += nv.astype(np.float32)
        fill = (~valid) & (cnt > 0.0)
        if not fill.any():
            break
        out[fill] = acc[fill] / cnt[fill, None]
        valid |= fill
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def _normalize_ps2_alpha(src_alpha: np.ndarray, tw: int, th: int) -> Tuple[np.ndarray, str]:
    """Upscale and strictly enforce PS2 GS [0..128] alpha range."""
    if int(src_alpha.max()) > 132:
        src_gs = np.clip((src_alpha.astype(np.uint16) + 1) // 2, 0, 128).astype(np.uint8)
    else:
        src_gs = np.clip(src_alpha, 0, 128).astype(np.uint8)
    a_min, a_max = int(src_gs.min()), int(src_gs.max())
    if a_min >= 120:
        return np.full((th, tw), 128, dtype=np.uint8), "opaque_128"
    up_a = np.asarray(Image.fromarray(src_gs).resize((tw, th), Image.Resampling.BILINEAR), dtype=np.uint8)
    mode = "cutout_0_128" if (a_min <= 8 and a_max >= 96) else "mask_0_128"
    return np.clip(up_a, 0, min(128, max(a_max, 128))).astype(np.uint8), mode


def compute_relief_maps(
    rgb_f32: np.ndarray,
    profile: MaterialReliefProfile,
    *,
    name: str = "",
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Estimate multi-scale height field, baked AO [0..1], and directional shading delta [-1..1]."""
    h, w = rgb_f32.shape[:2]
    luma = (0.299 * rgb_f32[:, :, 0] + 0.587 * rgb_f32[:, :, 1] + 0.114 * rgb_f32[:, :, 2]).astype(np.float32)
    scale_px = max(32.0, float((h * w) ** 0.5))
    f_med = max(3, min(12, int(round(scale_px / 64.0))))
    f_wide = max(6, min(28, int(round(scale_px / 22.0))))
    s_fine = max(1, min(3, int(round(scale_px / 256.0))))
    s_macro = max(2, min(7, int(round(scale_px / 96.0))))

    l_fine = _blur3x3(luma, profile.wrap_edges)
    l_med = _pyr_blur(l_fine, f_med, profile.wrap_edges)
    l_wide = _pyr_blur(l_med, max(2, f_wide // max(2, f_med // 2)), profile.wrap_edges)

    # Multi-radius relative cavity AO (scale-adaptive for PS2 dark-to-bright diffuse range)
    denom = np.maximum(l_wide, np.float32(0.12))
    cav_micro = np.maximum(np.float32(0.0), l_fine - luma)
    cav_med = np.maximum(np.float32(0.0), l_med - l_fine)
    cav_wide = np.maximum(np.float32(0.0), l_wide - l_med)
    cavity = (0.25 * cav_micro + 0.62 * cav_med + 0.55 * cav_wide) / denom
    ao = np.clip(1.0 - profile.ao_strength * np.tanh(cavity * profile.ao_gain), 0.26, 1.0).astype(np.float32)

    # Multi-scale relative + macro luminance height field
    rel_h = (0.50 * (l_fine - l_med) + 0.38 * (l_med - l_wide) + 0.12 * (luma - l_fine)) / np.maximum(l_wide, np.float32(0.14))
    height = (np.tanh(rel_h * 1.65) * (1.0 / 1.65) + 0.26 * l_fine).astype(np.float32)

    gx_f, gy_f = _strided_grad(height, s_fine, profile.wrap_edges)
    gx_m, gy_m = _strided_grad(_blur3x3(height, profile.wrap_edges), s_macro, profile.wrap_edges)
    gx = 0.58 * gx_f + 0.42 * gx_m
    gy = 0.58 * gy_f + 0.42 * gy_m

    nx = -gx * np.float32(profile.bump_scale)
    ny = -gy * np.float32(profile.bump_scale)

    # Material-aware seamless micro-normal relief (asphalt aggregate / masonry grit)
    if profile.grain_bump > 0.0 and min(h, w) >= 32:
        ggx, ggy = _tileable_micro_grad(h, w, name)
        if profile.name == "road":
            yellow = np.clip((rgb_f32[:, :, 0] - rgb_f32[:, :, 2]) * 3.0, 0.0, 1.0)
            bright = np.clip((luma - np.maximum(l_wide, np.float32(0.22)) * 1.16) * 6.0, 0.0, 1.0)
            paint_mask = np.clip(np.maximum(bright, yellow * np.clip((luma - 0.20) * 5.0, 0.0, 1.0)), 0.0, 1.0)
            g_wt = np.float32(profile.grain_bump) * (np.float32(1.0) - np.float32(0.88) * paint_mask)
        else:
            edge_mag = np.abs(luma - l_med) / denom
            g_wt = np.float32(profile.grain_bump) * np.clip(1.0 - edge_mag * 2.2, 0.20, 1.0)
        nx = nx - ggx * g_wt
        ny = ny - ggy * g_wt

    inv_len = np.float32(1.0) / np.sqrt(nx * nx + ny * ny + np.float32(1.0))
    nx, ny, nz = nx * inv_len, ny * inv_len, inv_len

    lx, ly, lz = SUN_DIR
    ndotl = np.clip(nx * lx + ny * ly + nz * lz, 0.0, 1.0)
    lambert_delta = np.tanh((ndotl * np.float32(1.0 / lz) - np.float32(1.0)) * np.float32(1.35)).astype(np.float32)

    hx, hy, hz = HALF_DIR
    ndoth = np.clip(nx * hx + ny * hy + nz * hz, 0.0, 1.0)
    spec_flat = np.float32(hz ** profile.spec_exp)
    spec = (
        np.maximum(np.float32(0.0), np.power(ndoth, np.float32(profile.spec_exp)) - spec_flat)
        * np.float32(profile.spec_strength)
        * ao
    ).astype(np.float32)

    return ao, lambert_delta, spec


def sculpt_texture_relief_rgba(
    rgba_u8: np.ndarray,
    *,
    name: str,
    archive: str = "",
    scale: int = 4,
    small_scale: int = 8,
    max_size: int = 1024,
) -> Tuple[np.ndarray, str, str]:
    """Upscale and bake deterministic 3D relief, beveled normals & AO into RGBA uint8."""
    src_h, src_w = rgba_u8.shape[:2]
    eff = small_scale if (small_scale > scale and max(src_w, src_h) <= 32) else scale
    while max(src_w, src_h) * eff > max_size and eff > 1:
        eff //= 2
    tw, th = min(max_size, max(4, src_w * eff)), min(max_size, max(4, src_h * eff))

    mat = classify_relief_material(name, archive)
    prof = _effective_profile(mat, name)
    src_rgb, src_a = rgba_u8[:, :, :3], rgba_u8[:, :, 3]
    if int(src_a.min()) <= 8 and int(src_a.max()) >= 96:
        src_rgb = _dilate_cutout_rgb(src_rgb, src_a, threshold=16, iters=6)

    up_rgb = np.asarray(Image.fromarray(src_rgb).resize((tw, th), Image.Resampling.LANCZOS), dtype=np.float32) * np.float32(1.0 / 255.0)
    up_a, alpha_mode = _normalize_ps2_alpha(src_a, tw, th)
    luma = 0.299 * up_rgb[:, :, 0] + 0.587 * up_rgb[:, :, 1] + 0.114 * up_rgb[:, :, 2]

    # Constant-color / flat shadow overlays stay untouched to prevent synthetic noise
    if mat == "shadow_map" or float(luma.std()) < 0.004:
        out_rgb = np.clip(np.round(up_rgb * 255.0), 0, 255).astype(np.uint8)
        return np.dstack([out_rgb, up_a]), mat, alpha_mode

    if mat == "sky":
        blur = _blur3x3(_blur3x3(luma, wrap=False), wrap=False)
        grad = np.abs(np.diff(luma, axis=1, prepend=luma[:, :1])) + np.abs(np.diff(luma, axis=0, prepend=luma[:1, :]))
        delta = (blur - luma) * np.clip(1.0 - grad * 18.0, 0.0, 1.0) * np.float32(0.45)
        out_rgb = np.clip(np.round((up_rgb + delta[:, :, None]) * 255.0), 0, 255).astype(np.uint8)
        return np.dstack([out_rgb, up_a]), mat, alpha_mode

    if mat == "signage":
        blur = _blur3x3(luma, wrap=False)
        delta = np.clip(prof.clarity * np.float32(0.055) * np.tanh((luma - blur) * np.float32(18.0)), -0.032, 0.032)
        out_rgb = np.clip(np.round((up_rgb + delta[:, :, None]) * 255.0), 0, 255).astype(np.uint8)
        if alpha_mode == "cutout_0_128":
            out_rgb = _dilate_cutout_rgb(out_rgb, up_a, threshold=16, iters=4)
        return np.dstack([out_rgb, up_a]), mat, alpha_mode

    ao, lambert_delta, spec = compute_relief_maps(up_rgb, prof, name=name)

    # Pseudo-linear light shading (fast x^2 / sqrt) preserves 2005 hue & PS2 dynamic range
    lin = up_rgb * up_rgb
    lit_pos = np.maximum(np.float32(0.0), lambert_delta) * np.float32(prof.sun_strength)
    lit_neg = np.maximum(np.float32(0.0), -lambert_delta) * np.float32(prof.sun_strength * 0.72)
    shad_deficit = np.clip(np.float32(1.0) - ao * (np.float32(1.0) - lit_neg), 0.0, 0.80)

    warm = np.array(prof.warm_tint, dtype=np.float32)[None, None, :]
    cool = np.array(prof.cool_pull, dtype=np.float32)[None, None, :]
    mult = np.clip((np.float32(1.0) - shad_deficit[:, :, None] * cool) + lit_pos[:, :, None] * warm, 0.22, 2.05)

    # Subtle edge-guided local micro-clarity without haloing
    clar_l = _blur3x3(luma, prof.wrap_edges)
    clar_mult = np.float32(1.0) + np.float32(prof.clarity * 0.16) * np.tanh((luma - clar_l) * np.float32(12.0))

    lin_out = lin * mult * clar_mult[:, :, None] + spec[:, :, None] * warm * (np.float32(0.10) + np.float32(0.55) * lin)

    # Preserve mean luminance within tight bounds while keeping full sculpted local 3D contrast
    valid = up_a >= 16
    if valid.any():
        src_mean = float((0.299 * lin[:, :, 0] + 0.587 * lin[:, :, 1] + 0.114 * lin[:, :, 2])[valid].mean())
        out_mean = float((0.299 * lin_out[:, :, 0] + 0.587 * lin_out[:, :, 1] + 0.114 * lin_out[:, :, 2])[valid].mean())
        if src_mean > 1e-4 and out_mean > 1e-4:
            ratio = src_mean / out_mean
            if ratio > 1.07:
                lin_out = lin_out * np.float32(ratio / 1.07)
            elif ratio < 0.93:
                lin_out = lin_out * np.float32(ratio / 0.93)

    # Soft-knee highlight protection prevents clipping bright surfaces & headlights
    hi = np.maximum(np.float32(0.0), lin_out - np.float32(0.86))
    lin_out = np.where(lin_out > np.float32(0.86), np.float32(0.86) + np.float32(0.14) * np.tanh(hi * np.float32(7.0)), lin_out)

    out_rgb = np.clip(np.round(np.sqrt(np.clip(lin_out, 0.0, 1.0)) * 255.0), 0, 255).astype(np.uint8)
    if alpha_mode == "cutout_0_128":
        out_rgb = _dilate_cutout_rgb(out_rgb, up_a, threshold=16, iters=4)
    return np.dstack([out_rgb, up_a]), mat, alpha_mode


# ---------------------------------------------------------------------------
# v2.9 DDS surface-depth polish
# ---------------------------------------------------------------------------

# Exact (archive, name) of the retail under-car contact shadow (64x32 PSM_T4, black RGB, alpha 0..127).
CAR_CONTACT_SHADOWS: Dict[str, Tuple[str, ...]] = {r"GLOBAL\GLOBALB.LZC": ("CARSHADOW",)}
PS2_ALPHA_TEST_REF = 64  # half of PS2 GS opaque alpha 128
_SKY_REFLECT = np.array([0.62, 0.71, 0.82], dtype=np.float32) ** 2  # upper-sky blue (linear)
_WARM_REFLECT = np.array([0.88, 0.70, 0.50], dtype=np.float32) ** 2  # low warm street haze (linear)


def is_car_contact_shadow(name: str, archive: str = "") -> bool:
    """True only for the exact retail car contact shadow (never SHD_* world maps / UI / SHADOWRAMP)."""
    return name.strip().upper() in CAR_CONTACT_SHADOWS.get(archive.replace("/", "\\").upper(), ())


def is_window_texture(name: str) -> bool:
    nm = name.strip().upper()
    if "REFLECTION" in nm:
        return False
    return (nm.startswith(("ARC_", "BLD_", "WIN_")) and ("WIN" in nm or "GLASS" in nm)) or "_WIN" in nm


def _box_mean(arr: np.ndarray, k: int) -> np.ndarray:
    """Separable k x k box mean (edge-padded) via cumulative sums; O(1) per pixel."""
    out = arr.astype(np.float32)
    if k <= 1:
        return out
    for _ in range(2):
        p = np.pad(out, ((k // 2, k - 1 - k // 2), (0, 0)), mode="edge")
        c = np.concatenate([np.zeros((1, p.shape[1])), np.cumsum(p, axis=0, dtype=np.float64)])
        out = ((c[k:] - c[:-k]) / k).astype(np.float32).T
    return out


def _box_open(mask: np.ndarray, k: int) -> np.ndarray:
    """Morphological opening with a k x k square: keeps rectangles >= k, removes thin mortar/grit."""
    eroded = _box_mean(mask, k) > np.float32(1.0 - 1e-4)
    return _box_mean(eroded, k) > np.float32(1e-4)


def _run_len(mask: np.ndarray) -> np.ndarray:
    """Per-pixel count of consecutive True pixels ending here, walking down axis 0 (0 outside mask)."""
    idx = np.arange(mask.shape[0])[:, None]
    last_off = np.maximum.accumulate(np.where(mask, -1, idx), axis=0)
    return np.where(mask, idx - last_off, 0)


def detect_window_panes(rgb_f32: np.ndarray) -> np.ndarray:
    """Detect dark rectangular painted window panes (bool mask) on a lighter facade."""
    h, w = rgb_f32.shape[:2]
    luma = 0.299 * rgb_f32[:, :, 0] + 0.587 * rgb_f32[:, :, 1] + 0.114 * rgb_f32[:, :, 2]
    med = float(np.median(luma))
    if med < 0.08 or min(h, w) < 16:
        return np.zeros((h, w), dtype=bool)
    dark = (luma < np.float32(0.74 * med)) & (luma < np.float32(0.42))
    dark = _box_mean(dark, 3) > np.float32(0.5)
    k = max(3, int(round(min(h, w) * 0.045)))
    pane = _box_open(dark, k)
    kc = max(3, k // 3)  # close pinholes (painted interior specks) while keeping light mullions
    pane = _box_mean(_box_mean(pane, kc) > np.float32(1e-4), kc) > np.float32(1.0 - 1e-4)
    pane &= (_run_len(pane.T).T + _run_len(pane.T[::-1])[::-1].T) <= int(0.9 * w)  # drop full-width trim bands
    pane &= (_run_len(pane) + _run_len(pane[::-1])[::-1]) <= int(0.9 * h)
    pane = _box_open(pane, k)
    frac = float(pane.mean())
    return pane if 0.01 <= frac <= 0.70 else np.zeros((h, w), dtype=bool)


def polish_window_glass(rgb_u8: np.ndarray, strength: float = 1.0) -> Tuple[np.ndarray, float]:
    """Recess painted panes: inner-perimeter AO + upper-left frame drop shadow + sky/warm glass reflection."""
    rgb = rgb_u8.astype(np.float32) * np.float32(1.0 / 255.0)
    pane = detect_window_panes(rgb)
    if not pane.any():
        return rgb_u8.copy(), 0.0
    h, w = pane.shape
    up, down = _run_len(pane), _run_len(pane[::-1])[::-1]
    left, right = _run_len(pane.T).T, _run_len(pane.T[::-1])[::-1].T
    band = np.float32(max(2.0, round(min(h, w) * 0.012)))
    edge_d = np.minimum(np.minimum(up, down), np.minimum(left, right)).astype(np.float32)
    v = (up - 1).astype(np.float32) / np.maximum(up + down - 2, 1).astype(np.float32)  # 0 top .. 1 bottom
    recess_ao = np.float32(0.24) * np.exp(-(edge_d - 1.0) / band)
    sd = (np.minimum(up, left).astype(np.float32) - 1.0) / (np.float32(2.4) * band)  # sun from upper-left
    t = np.clip(1.0 - sd, 0.0, 1.0)
    drop = np.float32(0.30) * t * t * (3.0 - 2.0 * t)
    mult = np.where(pane, (1.0 - recess_ao) * (1.0 - drop), 1.0).astype(np.float32)
    warm_t = np.clip((v - 0.35) / 0.65, 0.0, 1.0)
    luma = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    glass = np.clip((0.42 - luma) * 4.0, 0.25, 1.0)  # reflections read on dark glass; lit interiors stay painted
    refl_w = np.where(pane, (0.12 * (1.0 - v) ** 1.4 + 0.035) * mult * glass, 0.0).astype(np.float32) * np.float32(strength)
    mult, refl_w = _blur3x3(mult, wrap=False), _blur3x3(refl_w, wrap=False)
    refl = _SKY_REFLECT[None, None, :] * (1.0 - warm_t[:, :, None]) + _WARM_REFLECT[None, None, :] * warm_t[:, :, None]
    lin = (rgb * rgb) * (1.0 - np.float32(strength) * (1.0 - mult[:, :, None]))
    lin = lin + refl_w[:, :, None] * refl * (1.0 - lin)  # screen blend keeps painted interior detail
    out = np.clip(np.round(np.sqrt(np.clip(lin, 0.0, 1.0)) * 255.0), 0, 255).astype(np.uint8)
    return out, float(pane.mean())


def reduce_cornice_grit(rgb_u8: np.ndarray, strength: float = 0.4) -> np.ndarray:
    """Gentle luma coring: damp low-amplitude grit while brick mortar / sill edges (> ~0.045) stay crisp."""
    rgb = rgb_u8.astype(np.float32) * np.float32(1.0 / 255.0)
    luma = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    detail = luma - _blur3x3(luma, wrap=True)
    att = np.float32(strength) * np.clip(1.0 - (np.abs(detail) - 0.012) / 0.033, 0.0, 1.0)
    return np.clip(np.round((rgb - (detail * att)[:, :, None]) * 255.0), 0, 255).astype(np.uint8)


def polish_car_contact_shadow(rgba_u8: np.ndarray) -> np.ndarray:
    """Deeper solid dark core + tighter soft penumbra, strictly inside the retail footprint, alpha <= 128."""
    a = rgba_u8[:, :, 3].astype(np.float32) * np.float32(1.0 / 128.0)
    if float(a.max()) <= 0.0:
        return rgba_u8.copy()
    k = max(3, min(a.shape) // 8)

    def _ss(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
        t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
        return t * t * (3.0 - 2.0 * t)

    core = _ss(_box_mean(a, k), 0.55, 0.95)
    tight = _ss(a, 0.04, 0.90)  # ~half-width penumbra, still soft (smoothstep)
    a_new = np.where(a > np.float32(0.02), np.maximum(tight, core), 0.0)
    out = rgba_u8.copy()
    out[:, :, :3] = np.round(out[:, :, :3].astype(np.float32) * (1.0 - core[:, :, None])).astype(np.uint8)
    out[:, :, 3] = np.clip(np.round(a_new * 128.0), 0, 128).astype(np.uint8)
    return out


def _alpha_cov(alpha: np.ndarray, ref: int) -> float:
    return float((alpha >= ref).mean())


def build_coverage_preserving_mips(rgba_u8: np.ndarray, alpha_ref: int = PS2_ALPHA_TEST_REF) -> List[np.ndarray]:
    """Full mip chain for PS2 [0..128] cutouts preserving alpha-test coverage + outward RGB dilation."""
    h0, w0 = rgba_u8.shape[:2]
    chain = [rgba_u8]
    target = _alpha_cov(rgba_u8[:, :, 3], alpha_ref)
    while len(chain) < int(max(w0, h0)).bit_length():  # same level count as rockport_dds
        prev = chain[-1].astype(np.float32)
        h, w = prev.shape[:2]
        nh, nw = max(1, h // 2), max(1, w // 2)
        ys0, xs0 = np.minimum(np.arange(nh) * 2, h - 1), np.minimum(np.arange(nw) * 2, w - 1)
        ys1, xs1 = np.minimum(ys0 + 1, h - 1), np.minimum(xs0 + 1, w - 1)
        quads = [prev[ys][:, xs] for ys in (ys0, ys1) for xs in (xs0, xs1)]
        a_sum = sum(q[:, :, 3] for q in quads)
        wsum = sum(q[:, :, :3] * q[:, :, 3:4] for q in quads)
        plain = sum(q[:, :, :3] for q in quads) * np.float32(0.25)
        rgb = np.where(a_sum[:, :, None] > 0, wsum / np.maximum(a_sum, 1e-6)[:, :, None], plain)
        alpha = a_sum * np.float32(0.25)
        if 0.0 < target < 1.0 and min(nh, nw) >= 2:
            part = alpha < np.float32(127.5)  # only partial texels scale; opaque stays locked at 128

            def _scaled(s: float) -> np.ndarray:
                return np.where(part, np.minimum(alpha * np.float32(s), np.float32(128.0)), alpha)

            lo, hi = 0.5, 4.0
            for _ in range(14):
                mid = 0.5 * (lo + hi)
                lo, hi = (mid, hi) if _alpha_cov(_scaled(mid), alpha_ref) < target else (lo, mid)
            alpha = min((_scaled(lo), _scaled(hi)), key=lambda a: abs(_alpha_cov(a, alpha_ref) - target))
        a_u8 = np.clip(np.round(alpha), 0, 128).astype(np.uint8)
        rgb_u8 = _dilate_cutout_rgb(np.clip(np.round(rgb), 0, 255).astype(np.uint8), a_u8, threshold=16, iters=2)
        chain.append(np.dstack([rgb_u8, a_u8]))
    return chain


def polish_texture_relief_rgba(
    rgba_u8: np.ndarray,
    *,
    name: str,
    archive: str = "",
    scale: int = 4,
    small_scale: int = 8,
    max_size: int = 1024,
) -> Tuple[np.ndarray, str, str]:
    """v2.9: v2.5 sculpted relief + car contact shadow / recessed window glass / cornice grit polish."""
    out, mat, amode = sculpt_texture_relief_rgba(
        rgba_u8, name=name, archive=archive, scale=scale, small_scale=small_scale, max_size=max_size)
    if is_car_contact_shadow(name, archive):
        return polish_car_contact_shadow(out), "car_contact_shadow", amode
    if mat != "architecture":
        return out, mat, amode
    rgb = reduce_cornice_grit(out[:, :, :3])
    if is_window_texture(name):
        rgb, frac = polish_window_glass(rgb)
        mat = "window_glass" if frac > 0.0 else mat
    return np.dstack([rgb, out[:, :, 3]]), mat, amode
