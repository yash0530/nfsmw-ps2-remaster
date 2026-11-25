#!/usr/bin/env python3
"""bake_pbr_to_diffuse.py — Photorealistic CC0 PBR-to-PS2-Diffuse Material Baker.

PCSX2's PlayStation 2 Graphics Synthesizer replacement pipeline renders diffuse RGBA
textures only (no runtime tangent-space normal/roughness shader maps). This tool ingests
CC0 PBR texture sets (Poly Haven or ambientCG naming conventions) and bakes physically
grounded micro-detail directly into crisp, seamless Power-of-Two 2K/4K diffuse PNGs:

  1. Linear-space directional sunlight micro-normal shading (configurable elevation/azimuth)
  2. Ambient occlusion + high-frequency normal-divergence & displacement cavity contrast
  3. Procedural seamless wet-road puddle generator (--wetness 0.0..1.0):
       - Multi-octave domain-warped periodic noise (100% tile-seamless!)
       - Settles organically into displacement/cavity troughs
       - Darkens wet asphalt albedo ~35% (water-film internal absorption)
       - Flattens standing-water micro-normals + boosts sky reflection & specular sheen
  4. Clean Power-of-Two resize (Lanczos) & quantitative seamless tile-edge verification.

Dependencies: NumPy + Pillow only (zero extra packages).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".tga"}


# ---------------------------------------------------------------------------
# Color Space & Math Helpers (Linear-Light PBR)
# ---------------------------------------------------------------------------


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """Convert sRGB float32 [0, 1] to linear RGB float32 [0, 1]."""
    x = np.clip(x, 0.0, 1.0).astype(np.float32, copy=False)
    return np.where(x <= 0.04045, x / 12.92, np.power((x + 0.055) / 1.055, 2.4)).astype(np.float32)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    """Convert linear RGB float32 [0, 1] to sRGB float32 [0, 1]."""
    x = np.clip(x, 0.0, 1.0).astype(np.float32, copy=False)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1.0 / 2.4) - 0.055).astype(np.float32)


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def snap_pot(value: int, mode: str = "nearest", min_dim: int = 4, max_dim: int = 4096) -> int:
    clamped = max(min_dim, min(int(value), max_dim))
    if mode == "exact" or is_power_of_two(clamped):
        return clamped
    log2_v = math.log2(clamped)
    lower = max(min_dim, 1 << int(math.floor(log2_v)))
    upper = min(max_dim, 1 << int(math.ceil(log2_v)))
    if mode == "ceil":
        return upper
    if mode == "floor":
        return lower
    return upper if (clamped - lower) >= (upper - clamped) else lower


def _quintic_fade(t: np.ndarray) -> np.ndarray:
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def periodic_value_noise(
    height: int,
    width: int,
    freq_y: int,
    freq_x: int,
    rng: np.random.Generator,
    phase_y: float = 0.0,
    phase_x: float = 0.0,
) -> np.ndarray:
    """Generate a 100% tile-seamless 2D periodic value noise field in [0, 1]."""
    grid = rng.random((freq_y, freq_x), dtype=np.float32)

    xs = (np.arange(width, dtype=np.float32) / float(width) + phase_x) * float(freq_x)
    ys = (np.arange(height, dtype=np.float32) / float(height) + phase_y) * float(freq_y)

    x0 = np.floor(xs).astype(np.int32) % freq_x
    x1 = (x0 + 1) % freq_x
    tx = _quintic_fade(xs - np.floor(xs))

    y0 = np.floor(ys).astype(np.int32) % freq_y
    y1 = (y0 + 1) % freq_y
    ty = _quintic_fade(ys - np.floor(ys))

    # Separable 1D interpolation: X first (freq_y x width), then Y (height x width)
    row_interp = grid[:, x0] * (1.0 - tx[None, :]) + grid[:, x1] * tx[None, :]
    out = row_interp[y0, :] * (1.0 - ty[:, None]) + row_interp[y1, :] * ty[:, None]
    return out.astype(np.float32, copy=False)


def periodic_fbm(
    height: int,
    width: int,
    base_freq: int = 4,
    octaves: int = 5,
    persistence: float = 0.52,
    seed: int = 2026,
    domain_warp: float = 0.25,
) -> np.ndarray:
    """Generate 100% tile-seamless multi-octave domain-warped fBm in [0, 1]."""
    rng = np.random.default_rng(seed)
    base_freq = max(2, int(base_freq))

    # Optional periodic domain warp for realistic ragged puddle shorelines
    if domain_warp > 0.0:
        wx = periodic_value_noise(height, width, base_freq, base_freq, rng) - 0.5
        wy = periodic_value_noise(height, width, base_freq, base_freq, rng) - 0.5
    else:
        wx = wy = None

    total = np.zeros((height, width), dtype=np.float32)
    amp = 1.0
    amp_sum = 0.0

    for oct_idx in range(octaves):
        f = base_freq * (1 << oct_idx)
        # Cap frequency below Nyquist so we never alias
        fx = min(f, max(2, width // 2))
        fy = min(f, max(2, height // 2))
        layer = periodic_value_noise(height, width, fy, fx, rng)
        total += layer * amp
        amp_sum += amp
        amp *= persistence

    fbm = total / max(1e-6, amp_sum)

    if wx is not None and wy is not None:
        # Apply seamless periodic integer-wrapped coordinate warp with bilinear sampling
        shift_x = wx * (width * domain_warp * 0.14)
        shift_y = wy * (height * domain_warp * 0.14)
        gx = (np.arange(width, dtype=np.float32)[None, :] + shift_x) % float(width)
        gy = (np.arange(height, dtype=np.float32)[:, None] + shift_y) % float(height)

        x0 = np.floor(gx).astype(np.int32) % width
        y0 = np.floor(gy).astype(np.int32) % height
        x1 = (x0 + 1) % width
        y1 = (y0 + 1) % height
        fx_frac = gx - np.floor(gx)
        fy_frac = gy - np.floor(gy)

        c00 = fbm[y0, x0]
        c10 = fbm[y0, x1]
        c01 = fbm[y1, x0]
        c11 = fbm[y1, x1]
        fbm = (
            c00 * (1.0 - fx_frac) * (1.0 - fy_frac)
            + c10 * fx_frac * (1.0 - fy_frac)
            + c01 * (1.0 - fx_frac) * fy_frac
            + c11 * fx_frac * fy_frac
        )

    lo = float(np.percentile(fbm, 2.0))
    hi = float(np.percentile(fbm, 98.0))
    if hi - lo > 1e-5:
        fbm = np.clip((fbm - lo) / (hi - lo), 0.0, 1.0)
    return fbm.astype(np.float32, copy=False)


def _box_blur_periodic(arr: np.ndarray, radius: int) -> np.ndarray:
    """Fast separable periodic box blur (wrap-around edges preserve seamlessness)."""
    if radius <= 0:
        return arr
    k = 2 * radius + 1
    # Horizontal periodic prefix sum
    pad_x = np.concatenate([arr[:, -radius:], arr, arr[:, :radius]], axis=1)
    cs_x = np.cumsum(np.pad(pad_x, ((0, 0), (1, 0)), mode="constant"), axis=1, dtype=np.float64)
    blur_x = (cs_x[:, k:] - cs_x[:, :-k]) / float(k)

    # Vertical periodic prefix sum
    pad_y = np.concatenate([blur_x[-radius:, :], blur_x, blur_x[:radius, :]], axis=0)
    cs_y = np.cumsum(np.pad(pad_y, ((1, 0), (0, 0)), mode="constant"), axis=0, dtype=np.float64)
    blur_xy = (cs_y[k:, :] - cs_y[:-k, :]) / float(k)
    return blur_xy.astype(np.float32)


# ---------------------------------------------------------------------------
# PBR Map Auto-Discovery (Poly Haven & ambientCG)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PBRMapSet:
    diffuse: Path
    normal: Optional[Path] = None
    normal_is_dx: bool = False
    roughness: Optional[Path] = None
    ao: Optional[Path] = None
    arm: Optional[Path] = None  # Poly Haven packed R=AO, G=Roughness, B=Metallic
    displacement: Optional[Path] = None
    opacity: Optional[Path] = None


def discover_pbr_maps(material_dir: Path) -> PBRMapSet:
    """Auto-discover PBR texture maps inside a Poly Haven or ambientCG material folder."""
    if not material_dir.is_dir():
        raise FileNotFoundError(f"Material directory not found: {material_dir}")

    files = sorted(
        p
        for p in material_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
    )
    if not files:
        raise FileNotFoundError(f"No image files found in {material_dir}")

    def pick(patterns: Sequence[str]) -> Optional[Path]:
        for pat in patterns:
            rx = re.compile(pat, re.IGNORECASE)
            for fp in files:
                if rx.search(fp.stem):
                    return fp
        return None

    diffuse = pick(
        [
            r"(?:^|[_\-])(diff|diffuse|color|albedo|basecolor|col)(?:$|[_\-])",
            r"(diff|color|albedo)",
        ]
    )
    if diffuse is None:
        raise FileNotFoundError(f"Could not find diffuse/Color map in {material_dir}")

    normal_gl = pick([r"(?:^|[_\-])(nor_gl|normalgl|normal_gl)(?:$|[_\-])"])
    normal_dx = pick([r"(?:^|[_\-])(nor_dx|normaldx|normal_dx)(?:$|[_\-])"])
    normal_generic = pick([r"(?:^|[_\-])(nor|normal|nrm)(?:$|[_\-])"])

    if normal_gl is not None:
        normal, normal_is_dx = normal_gl, False
    elif normal_generic is not None:
        normal, normal_is_dx = normal_generic, False
    elif normal_dx is not None:
        normal, normal_is_dx = normal_dx, True
    else:
        normal, normal_is_dx = None, False

    roughness = pick([r"(?:^|[_\-])(rough|roughness)(?:$|[_\-])"])
    ao = pick([r"(?:^|[_\-])(ao|ambientocclusion|ambient_occlusion|occ)(?:$|[_\-])"])
    arm = pick([r"(?:^|[_\-])arm(?:$|[_\-])"])
    displacement = pick([r"(?:^|[_\-])(disp|displacement|height|bump)(?:$|[_\-])"])
    opacity = pick([r"(?:^|[_\-])(opacity|alpha|mask)(?:$|[_\-])"])

    return PBRMapSet(
        diffuse=diffuse,
        normal=normal,
        normal_is_dx=normal_is_dx,
        roughness=roughness,
        ao=ao,
        arm=arm,
        displacement=displacement,
        opacity=opacity,
    )


# ---------------------------------------------------------------------------
# Seamless Edge Verification
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SeamlessReport:
    width: int
    height: int
    lr_border_mad: float
    tb_border_mad: float
    interior_x_mad: float
    interior_y_mad: float
    lr_seam_ratio: float
    tb_seam_ratio: float
    is_seamless: bool

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def check_seamless_edges(rgb_uint8: np.ndarray, threshold_ratio: float = 2.25) -> SeamlessReport:
    """Measure opposite-edge tile discontinuity relative to interior pixel-to-pixel variance.

    A truly seamless texture has left-vs-right and top-vs-bottom edge differences comparable
    to typical adjacent interior pixel differences (seam_ratio ~ 1.0). Non-seamless crops
    typically score > 3.0.
    """
    arr = rgb_uint8[:, :, :3].astype(np.float32)
    h, w, _ = arr.shape

    lr_mad = float(np.mean(np.abs(arr[:, 0, :] - arr[:, -1, :])))
    tb_mad = float(np.mean(np.abs(arr[0, :, :] - arr[-1, :, :])))

    int_x_mad = float(np.mean(np.abs(arr[:, 1:, :] - arr[:, :-1, :])))
    int_y_mad = float(np.mean(np.abs(arr[1:, :, :] - arr[:-1, :, :])))

    # Guard against perfectly solid-color test images where interior MAD == 0
    lr_ratio = (lr_mad / int_x_mad) if int_x_mad > 0.25 else (1.0 if lr_mad <= 1.0 else lr_mad)
    tb_ratio = (tb_mad / int_y_mad) if int_y_mad > 0.25 else (1.0 if tb_mad <= 1.0 else tb_mad)

    is_seamless = (lr_ratio <= threshold_ratio and tb_ratio <= threshold_ratio) or (
        lr_mad <= 2.0 and tb_mad <= 2.0
    )
    return SeamlessReport(
        width=w,
        height=h,
        lr_border_mad=round(lr_mad, 3),
        tb_border_mad=round(tb_mad, 3),
        interior_x_mad=round(int_x_mad, 3),
        interior_y_mad=round(int_y_mad, 3),
        lr_seam_ratio=round(lr_ratio, 3),
        tb_seam_ratio=round(tb_ratio, 3),
        is_seamless=bool(is_seamless),
    )


# ---------------------------------------------------------------------------
# Core PBR-to-PS2-Diffuse Baker
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BakeConfig:
    sun_elevation_deg: float = 42.0
    sun_azimuth_deg: float = 315.0
    sun_intensity: float = 0.72
    ambient: float = 0.36
    normal_strength: float = 1.25
    normal_convention: str = "auto"  # "auto", "opengl", "directx"
    ao_strength: float = 0.80
    cavity_strength: float = 0.42
    specular_strength: float = 0.22
    wetness: float = 0.0
    wet_albedo_darken: float = 0.35
    puddle_scale: int = 4
    puddle_seed: int = 2026
    sky_tint: Tuple[float, float, float] = (0.64, 0.74, 0.88)
    target_width: Optional[int] = None
    target_height: Optional[int] = None
    pot_mode: str = "nearest"
    max_dim: int = 4096


def _load_resized_array(path: Path, size: Tuple[int, int], mode: str) -> np.ndarray:
    with Image.open(path) as img:
        if img.size != size:
            img = img.resize(size, Image.Resampling.LANCZOS)
        return np.asarray(img.convert(mode), dtype=np.uint8)


def bake_pbr_arrays(
    diffuse_srgb: np.ndarray,
    normal_rgb: Optional[np.ndarray] = None,
    roughness_gray: Optional[np.ndarray] = None,
    ao_gray: Optional[np.ndarray] = None,
    displacement_gray: Optional[np.ndarray] = None,
    config: Optional[BakeConfig] = None,
    normal_is_dx: bool = False,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Bake PBR channel arrays into a photorealistic PS2 diffuse RGBA/RGB uint8 array.

    Args:
        diffuse_srgb: HxWx3 or HxWx4 uint8 sRGB albedo.
        normal_rgb: Optional HxWx3 uint8 tangent-space normal map.
        roughness_gray: Optional HxW uint8 roughness map (0=mirror, 255=matte).
        ao_gray: Optional HxW uint8 ambient occlusion map (0=occluded, 255=open).
        displacement_gray: Optional HxW uint8 height/displacement map.
        config: BakeConfig lighting, cavity, and wetness parameters.
        normal_is_dx: True if input normal map uses DirectX (-Y green) convention.

    Returns:
        (baked_uint8, stats_dict)
    """
    cfg = config or BakeConfig()
    has_alpha = diffuse_srgb.ndim == 3 and diffuse_srgb.shape[2] == 4
    h, w = diffuse_srgb.shape[:2]

    albedo_lin = srgb_to_linear(diffuse_srgb[:, :, :3].astype(np.float32) / 255.0)
    alpha_chan = diffuse_srgb[:, :, 3] if has_alpha else None

    # 1. Tangent-space normal vector field (OpenGL +X right, +Y up, +Z out)
    if normal_rgb is not None:
        nx = (normal_rgb[:, :, 0].astype(np.float32) / 127.5) - 1.0
        ny = (normal_rgb[:, :, 1].astype(np.float32) / 127.5) - 1.0
        nz = (normal_rgb[:, :, 2].astype(np.float32) / 127.5) - 1.0

        flip_y = (cfg.normal_convention == "directx") or (
            cfg.normal_convention == "auto" and normal_is_dx
        )
        if flip_y:
            ny = -ny

        nx *= float(cfg.normal_strength)
        ny *= float(cfg.normal_strength)
        nz = np.maximum(nz, 0.08)
        n_len = np.sqrt(nx * nx + ny * ny + nz * nz)
        nx /= np.maximum(n_len, 1e-6)
        ny /= np.maximum(n_len, 1e-6)
        nz /= np.maximum(n_len, 1e-6)
    else:
        nx = np.zeros((h, w), dtype=np.float32)
        ny = np.zeros((h, w), dtype=np.float32)
        nz = np.ones((h, w), dtype=np.float32)

    # 2. Roughness field [0.05..1.0]
    if roughness_gray is not None:
        rough = np.clip(roughness_gray.astype(np.float32) / 255.0, 0.05, 1.0)
    else:
        rough = np.full((h, w), 0.72, dtype=np.float32)

    # 3. Ambient occlusion + micro-cavity map (keeps asphalt aggregate & cracks razor-sharp)
    if ao_gray is not None:
        ao = np.clip(ao_gray.astype(np.float32) / 255.0, 0.0, 1.0)
    else:
        ao = np.ones((h, w), dtype=np.float32)

    cavity = np.ones((h, w), dtype=np.float32)
    if normal_rgb is not None:
        # Periodic central differences of tangent normal X/Y detect micro-crevices cleanly
        dnx_dx = 0.5 * (np.roll(nx, -1, axis=1) - np.roll(nx, 1, axis=1))
        dny_dy = 0.5 * (np.roll(ny, -1, axis=0) - np.roll(ny, 1, axis=0))
        div = dnx_dx + dny_dy
        # Negative divergence corresponds to convex peaks; positive divergence to concave pits/cracks
        normal_cav = np.clip(1.0 - div * 0.95, 0.45, 1.25)
        cavity *= normal_cav

    height_norm: Optional[np.ndarray] = None
    if displacement_gray is not None:
        height_norm = displacement_gray.astype(np.float32) / 255.0
        blur_r = max(2, min(16, min(h, w) // 96))
        local_mean = _box_blur_periodic(height_norm, blur_r)
        height_detail = height_norm - local_mean
        disp_cav = np.clip(1.0 + height_detail * 1.65, 0.55, 1.25)
        cavity *= disp_cav

    ao_factor = np.clip(1.0 - float(cfg.ao_strength) * (1.0 - ao), 0.15, 1.0)
    cavity_factor = np.clip(1.0 + float(cfg.cavity_strength) * (cavity - 1.0), 0.35, 1.28)

    # 4. Wet-road puddle generator (--wetness 0.0..1.0)
    wetness = float(np.clip(cfg.wetness, 0.0, 1.0))
    puddle_coverage = 0.0
    damp_coverage = 0.0
    puddle_mask = np.zeros((h, w), dtype=np.float32)
    damp_mask = np.zeros((h, w), dtype=np.float32)

    if wetness > 0.001:
        noise = periodic_fbm(
            height=h,
            width=w,
            base_freq=max(2, int(cfg.puddle_scale)),
            octaves=5,
            persistence=0.52,
            seed=int(cfg.puddle_seed),
            domain_warp=0.30,
        )
        # Pool water preferentially inside low displacement troughs & crevices
        if height_norm is not None:
            smooth_h = _box_blur_periodic(height_norm, max(2, min(h, w) // 128))
            basin = 0.72 * (1.0 - noise) + 0.28 * (1.0 - smooth_h)
        else:
            basin = 1.0 - noise

        # Standing water puddles: crisp meniscus shoreline transition
        puddle_thresh = 1.0 - (0.12 + 0.56 * wetness)
        puddle_soft = 0.055
        puddle_mask = np.clip((basin - puddle_thresh) / puddle_soft, 0.0, 1.0)
        puddle_mask = puddle_mask * puddle_mask * (3.0 - 2.0 * puddle_mask)

        # Broad rain-damp asphalt halo surrounding puddles (so whole road feels damp at high wetness)
        damp_thresh = puddle_thresh - (0.18 + 0.32 * wetness)
        damp_soft = 0.22
        damp_raw = np.clip((basin - damp_thresh) / damp_soft, 0.0, 1.0)
        damp_mask = np.maximum(puddle_mask, np.clip(damp_raw * (0.55 + 0.45 * wetness), 0.0, 1.0))

        # Physical water-film albedo darkening (~35% darker in sRGB -> deeper linear absorption)
        absorb = np.clip(float(cfg.wet_albedo_darken), 0.0, 0.65)
        # Convert sRGB darkening fraction cleanly into linear-light attenuation (~(1-a)^2.2)
        lin_wet_mult = math.pow(max(0.15, 1.0 - absorb), 2.15)
        damp_atten = 1.0 - (1.0 - lin_wet_mult) * (0.76 * damp_mask + 0.24 * puddle_mask)
        # Subtle capillary dark ring right along puddle meniscus shoreline
        shore_ring = np.clip(4.0 * puddle_mask * (1.0 - puddle_mask), 0.0, 1.0)
        darken_factor = damp_atten * (1.0 - 0.14 * shore_ring)
        albedo_lin = albedo_lin * darken_factor[:, :, None]

        # Subtle wet mineral color richness (wet tarmac looks deeper & slightly richer)
        luma = (
            0.2126 * albedo_lin[:, :, 0]
            + 0.7152 * albedo_lin[:, :, 1]
            + 0.0722 * albedo_lin[:, :, 2]
        )[:, :, None]
        sat_boost = 1.0 + 0.18 * damp_mask[:, :, None]
        albedo_lin = np.clip(luma + (albedo_lin - luma) * sat_boost, 0.0, 1.0)

        # Standing water fills asphalt pores -> smooths normals toward flat (0, 0, 1)
        # Keep slight ~14% micro-ripple so subtle submerged grain peeks realistically through shallow water
        flat_blend = 0.86 * puddle_mask
        nx = nx * (1.0 - flat_blend)
        ny = ny * (1.0 - flat_blend)
        nz = nz * (1.0 - flat_blend) + 1.0 * flat_blend
        n_len = np.sqrt(nx * nx + ny * ny + nz * nz)
        nx /= np.maximum(n_len, 1e-6)
        ny /= np.maximum(n_len, 1e-6)
        nz /= np.maximum(n_len, 1e-6)

        # Wet asphalt drops roughness; standing water becomes glossy mirror-like
        rough = rough * (1.0 - 0.52 * damp_mask)
        rough = rough * (1.0 - puddle_mask) + 0.05 * puddle_mask

        puddle_coverage = float(np.mean(puddle_mask > 0.5))
        damp_coverage = float(np.mean(damp_mask > 0.25))

    # 5. Directional sunlight micro-normal lighting (exposure-calibrated around flat N=(0,0,1))
    elev = math.radians(float(np.clip(cfg.sun_elevation_deg, 8.0, 88.0)))
    azim = math.radians(float(cfg.sun_azimuth_deg))
    lx = math.cos(elev) * math.cos(azim)
    ly = math.cos(elev) * math.sin(azim)
    lz = math.sin(elev)

    ndotl = np.clip(nx * lx + ny * ly + nz * lz, 0.0, 1.0)
    # Normalize by flat-surface lz so changing sun elevation doesn't crush overall brightness
    ndotl_norm = ndotl / max(0.30, lz)
    direct = float(cfg.sun_intensity) * ndotl_norm
    hemi = float(cfg.ambient) * (0.72 + 0.28 * nz)

    # Normalize illumination baseline so a flat surface (ndotl_norm=1, nz=1) has gain ~ 1.0
    baseline = max(0.1, float(cfg.sun_intensity) + float(cfg.ambient))
    diffuse_shade = (direct + hemi) / baseline

    # Combine with AO & micro-cavity
    lit_lin = albedo_lin * (diffuse_shade * ao_factor * cavity_factor)[:, :, None]

    # 6. Micro-specular & wet-puddle sky reflection sheen
    # Use a driving-perspective tilted view vector so road puddles catch glancing sky & sun glint
    vx, vy, vz = 0.0, -0.45, 0.89302855  # normalized ~27 deg forward view angle
    hx, hy, hz = lx + vx, ly + vy, lz + vz
    h_len = math.sqrt(hx * hx + hy * hy + hz * hz)
    hx, hy, hz = hx / h_len, hy / h_len, hz / h_len

    ndoth = np.clip(nx * hx + ny * hy + nz * hz, 0.0, 1.0)
    smoothness = np.clip(1.0 - rough, 0.0, 0.96)
    shininess = 8.0 + 180.0 * (smoothness * smoothness * smoothness)
    spec_lobe = np.power(ndoth, shininess)

    # Dry aggregate sparkle + wet sheen
    spec_gain = float(cfg.specular_strength) * (0.28 + 0.72 * smoothness * smoothness)
    if wetness > 0.001:
        spec_gain = spec_gain * (1.0 + 1.85 * damp_mask + 3.2 * puddle_mask)

    # Slight warm sunlight tint on specular peaks
    sun_spec = (spec_lobe * spec_gain * ao_factor)[:, :, None] * np.array(
        [1.04, 1.00, 0.94], dtype=np.float32
    )[None, None, :]
    lit_lin += sun_spec

    # Standing-water sky reflection + glossy dark-puddle sheen (iconic Rockport rain-soaked road)
    if wetness > 0.001:
        sky_lin = srgb_to_linear(np.array(cfg.sky_tint, dtype=np.float32))
        sky_cloud = periodic_fbm(
            height=h,
            width=w,
            base_freq=2,
            octaves=4,
            persistence=0.52,
            seed=int(cfg.puddle_seed) + 7919,
            domain_warp=0.18,
        )
        # Dramatic cloud-contrast sky sheen (dark reflections with subtle sky-gap streaks)
        sky_streak = np.power(np.clip(sky_cloud, 0.0, 1.0), 1.65)
        fresnel = (0.032 + 0.125 * sky_streak) * puddle_mask
        damp_sheen = (
            0.038 * (damp_mask * (1.0 - puddle_mask)) * smoothness * np.power(ndoth, 14.0)
        )
        reflect_mix = np.clip(fresnel + damp_sheen, 0.0, 0.24)[:, :, None]
        lit_lin = lit_lin * (1.0 - 0.42 * puddle_mask[:, :, None]) + sky_lin[None, None, :] * reflect_mix

    # Gentle filmic highlight roll-off (Reinhard soft-shoulder above 0.85 so highlights never clip harshly)
    lit_lin = np.where(lit_lin > 0.85, 0.85 + 0.15 * np.tanh((lit_lin - 0.85) / 0.25), lit_lin)
    out_srgb = linear_to_srgb(np.clip(lit_lin, 0.0, 1.0))
    out_rgb_u8 = np.clip(np.rint(out_srgb * 255.0), 0, 255).astype(np.uint8)

    if alpha_chan is not None:
        out_u8 = np.concatenate([out_rgb_u8, alpha_chan[:, :, None]], axis=2)
    else:
        out_u8 = out_rgb_u8

    in_luma = float(np.mean(diffuse_srgb[:, :, :3]))
    out_luma = float(np.mean(out_rgb_u8))
    stats = {
        "input_mean_luma": round(in_luma, 2),
        "output_mean_luma": round(out_luma, 2),
        "luma_ratio": round(out_luma / max(1.0, in_luma), 3),
        "wetness": round(wetness, 3),
        "puddle_coverage": round(puddle_coverage, 4),
        "damp_coverage": round(damp_coverage, 4),
        "has_normal": normal_rgb is not None,
        "has_roughness": roughness_gray is not None,
        "has_ao": ao_gray is not None,
        "has_displacement": displacement_gray is not None,
    }
    return out_u8, stats


def bake_pbr_set(
    maps: PBRMapSet,
    out_path: Path,
    config: Optional[BakeConfig] = None,
    compare_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load PBR maps from disk, bake to PS2 diffuse, PoT-resize, verify seamless edges, and save."""
    cfg = config or BakeConfig()

    with Image.open(maps.diffuse) as diff_img:
        orig_w, orig_h = diff_img.size
        has_alpha = "A" in diff_img.getbands() or maps.opacity is not None
        diff_mode = "RGBA" if has_alpha else "RGB"
        diff_arr = np.array(diff_img.convert(diff_mode), dtype=np.uint8, copy=True)

    work_size = (orig_w, orig_h)
    if maps.opacity is not None:
        op_arr = _load_resized_array(maps.opacity, work_size, "L")
        if diff_arr.shape[2] == 3:
            diff_arr = np.concatenate([diff_arr, op_arr[:, :, None]], axis=2)
        else:
            diff_arr[:, :, 3] = op_arr

    normal_arr = _load_resized_array(maps.normal, work_size, "RGB") if maps.normal else None

    rough_arr: Optional[np.ndarray] = None
    ao_arr: Optional[np.ndarray] = None
    if maps.arm is not None:
        arm_rgb = _load_resized_array(maps.arm, work_size, "RGB")
        ao_arr = arm_rgb[:, :, 0]
        rough_arr = arm_rgb[:, :, 1]
    if maps.roughness is not None:
        rough_arr = _load_resized_array(maps.roughness, work_size, "L")
    if maps.ao is not None:
        ao_arr = _load_resized_array(maps.ao, work_size, "L")

    disp_arr = _load_resized_array(maps.displacement, work_size, "L") if maps.displacement else None

    baked_arr, bake_stats = bake_pbr_arrays(
        diffuse_srgb=diff_arr,
        normal_rgb=normal_arr,
        roughness_gray=rough_arr,
        ao_gray=ao_arr,
        displacement_gray=disp_arr,
        config=cfg,
        normal_is_dx=maps.normal_is_dx,
    )

    out_mode = "RGBA" if baked_arr.shape[2] == 4 else "RGB"
    out_img = Image.fromarray(baked_arr)

    # Power-of-Two resize
    req_w = cfg.target_width if cfg.target_width else orig_w
    req_h = cfg.target_height if cfg.target_height else orig_h
    final_w = snap_pot(req_w, mode=cfg.pot_mode, max_dim=cfg.max_dim)
    final_h = snap_pot(req_h, mode=cfg.pot_mode, max_dim=cfg.max_dim)

    if (final_w, final_h) != (orig_w, orig_h):
        out_img = out_img.resize((final_w, final_h), Image.Resampling.LANCZOS)

    final_arr = np.asarray(out_img, dtype=np.uint8)
    seamless = check_seamless_edges(final_arr)

    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_img.save(out_path, format="PNG")

    if compare_path is not None:
        cmp_p = compare_path.expanduser().resolve()
        cmp_p.parent.mkdir(parents=True, exist_ok=True)
        raw_img = Image.fromarray(diff_arr[:, :, :3]).resize(
            (final_w, final_h), Image.Resampling.LANCZOS
        )
        baked_rgb = out_img.convert("RGB")
        # Split-diagonal or side-by-side comparison
        sheet = Image.new("RGB", (final_w * 2, final_h), (12, 14, 18))
        sheet.paste(raw_img, (0, 0))
        sheet.paste(baked_rgb, (final_w, 0))
        sheet.save(cmp_p, format="PNG")

    return {
        "output_path": str(out_path),
        "input_size": [orig_w, orig_h],
        "output_size": [final_w, final_h],
        "mode": out_mode,
        "maps_used": {
            "diffuse": str(maps.diffuse.name),
            "normal": maps.normal.name if maps.normal else None,
            "normal_is_dx": maps.normal_is_dx,
            "roughness": maps.roughness.name if maps.roughness else (f"{maps.arm.name}[G]" if maps.arm else None),
            "ao": maps.ao.name if maps.ao else (f"{maps.arm.name}[R]" if maps.arm else None),
            "displacement": maps.displacement.name if maps.displacement else None,
            "opacity": maps.opacity.name if maps.opacity else None,
        },
        "bake_stats": bake_stats,
        "seamless_check": seamless.to_dict(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bake_pbr_to_diffuse.py",
        description=(
            "Bake CC0 PBR texture sets (Diffuse + Normal + Roughness + AO/Displacement) into "
            "photorealistic PS2 diffuse RGBA/RGB PNGs with optional seamless wet-road puddles."
        ),
    )
    parser.add_argument(
        "--dir",
        type=Path,
        help="Material directory to auto-discover Poly Haven / ambientCG PBR maps.",
    )
    parser.add_argument("--diffuse", type=Path, help="Explicit Diffuse / Color map path.")
    parser.add_argument("--normal", type=Path, help="Explicit Normal map path (GL or DX).")
    parser.add_argument("--roughness", type=Path, help="Explicit Roughness map path.")
    parser.add_argument("--ao", type=Path, help="Explicit Ambient Occlusion map path.")
    parser.add_argument("--arm", type=Path, help="Explicit Poly Haven packed ARM map (R=AO, G=Rough).")
    parser.add_argument("--displacement", type=Path, help="Explicit Displacement / Height map path.")
    parser.add_argument("--opacity", type=Path, help="Explicit Opacity / Alpha mask path.")
    parser.add_argument("-o", "--out", type=Path, required=True, help="Output baked PNG path.")
    parser.add_argument(
        "--compare-out",
        type=Path,
        help="Optional side-by-side raw-vs-baked comparison PNG path.",
    )
    parser.add_argument(
        "--size",
        type=int,
        help="Optional square target size (e.g. 2048 or 4096); snapped via --pot-mode.",
    )
    parser.add_argument("--width", type=int, help="Optional target width in pixels.")
    parser.add_argument("--height", type=int, help="Optional target height in pixels.")
    parser.add_argument(
        "--pot-mode",
        default="nearest",
        choices=["nearest", "ceil", "floor", "exact"],
        help="Power-of-Two dimension snapping mode (default: nearest).",
    )
    parser.add_argument(
        "--sun-elevation",
        type=float,
        default=42.0,
        help="Directional sunlight elevation angle in degrees (default: 42.0).",
    )
    parser.add_argument(
        "--sun-azimuth",
        type=float,
        default=315.0,
        help="Directional sunlight azimuth angle in degrees (default: 315.0).",
    )
    parser.add_argument(
        "--sun-intensity",
        type=float,
        default=0.72,
        help="Direct sunlight contribution weight (default: 0.72).",
    )
    parser.add_argument(
        "--ambient",
        type=float,
        default=0.36,
        help="Hemisphere ambient fill contribution weight (default: 0.36).",
    )
    parser.add_argument(
        "--normal-strength",
        type=float,
        default=1.25,
        help="Tangent-space normal relief multiplier (default: 1.25).",
    )
    parser.add_argument(
        "--normal-convention",
        default="auto",
        choices=["auto", "opengl", "directx"],
        help="Normal map Y-channel handedness (default: auto from filename).",
    )
    parser.add_argument(
        "--ao-strength",
        type=float,
        default=0.80,
        help="Ambient occlusion darkening strength 0..1 (default: 0.80).",
    )
    parser.add_argument(
        "--cavity-strength",
        type=float,
        default=0.42,
        help="Normal/displacement micro-cavity contrast strength (default: 0.42).",
    )
    parser.add_argument(
        "--specular-strength",
        type=float,
        default=0.22,
        help="Micro-specular grazing sheen strength (default: 0.22).",
    )
    parser.add_argument(
        "--wetness",
        type=float,
        default=0.0,
        help="Wet-road puddle coverage & rain-dampness 0.0..1.0 (default: 0.0 = dry).",
    )
    parser.add_argument(
        "--wet-darken",
        type=float,
        default=0.35,
        help="Albedo darkening fraction on wet asphalt (default: 0.35 = 35%% darker).",
    )
    parser.add_argument(
        "--puddle-scale",
        type=int,
        default=4,
        help="Periodic puddle base frequency across one tile (default: 4).",
    )
    parser.add_argument(
        "--puddle-seed",
        type=int,
        default=2026,
        help="Deterministic PRNG seed for procedural puddle placement (default: 2026).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full bake + seamless-check report as JSON.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dir is not None:
        maps = discover_pbr_maps(args.dir.expanduser().resolve())
        if args.diffuse:
            maps.diffuse = args.diffuse.expanduser().resolve()
        if args.normal:
            maps.normal = args.normal.expanduser().resolve()
            maps.normal_is_dx = "dx" in maps.normal.stem.lower()
        if args.roughness:
            maps.roughness = args.roughness.expanduser().resolve()
        if args.ao:
            maps.ao = args.ao.expanduser().resolve()
        if args.arm:
            maps.arm = args.arm.expanduser().resolve()
        if args.displacement:
            maps.displacement = args.displacement.expanduser().resolve()
        if args.opacity:
            maps.opacity = args.opacity.expanduser().resolve()
    elif args.diffuse is not None:
        norm = args.normal.expanduser().resolve() if args.normal else None
        maps = PBRMapSet(
            diffuse=args.diffuse.expanduser().resolve(),
            normal=norm,
            normal_is_dx=(bool(norm and "dx" in norm.stem.lower())),
            roughness=args.roughness.expanduser().resolve() if args.roughness else None,
            ao=args.ao.expanduser().resolve() if args.ao else None,
            arm=args.arm.expanduser().resolve() if args.arm else None,
            displacement=args.displacement.expanduser().resolve() if args.displacement else None,
            opacity=args.opacity.expanduser().resolve() if args.opacity else None,
        )
    else:
        parser.error("Either --dir or --diffuse must be provided.")

    target_w = args.width if args.width else args.size
    target_h = args.height if args.height else args.size
    cfg = BakeConfig(
        sun_elevation_deg=args.sun_elevation,
        sun_azimuth_deg=args.sun_azimuth,
        sun_intensity=args.sun_intensity,
        ambient=args.ambient,
        normal_strength=args.normal_strength,
        normal_convention=args.normal_convention,
        ao_strength=args.ao_strength,
        cavity_strength=args.cavity_strength,
        specular_strength=args.specular_strength,
        wetness=args.wetness,
        wet_albedo_darken=args.wet_darken,
        puddle_scale=args.puddle_scale,
        puddle_seed=args.puddle_seed,
        target_width=target_w,
        target_height=target_h,
        pot_mode=args.pot_mode,
    )

    report = bake_pbr_set(maps, out_path=args.out, config=cfg, compare_path=args.compare_out)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        sc = report["seamless_check"]
        st = report["bake_stats"]
        status = "PASS" if sc["is_seamless"] else "WARN"
        print(f"Baked PS2 diffuse  : {report['output_path']} ({report['output_size'][0]}x{report['output_size'][1]} {report['mode']})")
        print(f"Maps used          : {report['maps_used']}")
        print(
            f"Lighting / Wetness : luma {st['input_mean_luma']} -> {st['output_mean_luma']} "
            f"(ratio {st['luma_ratio']}), wetness={st['wetness']}, "
            f"puddles={st['puddle_coverage']*100:.1f}%, damp={st['damp_coverage']*100:.1f}%"
        )
        print(
            f"Seamless edge check: {status} "
            f"(L/R ratio={sc['lr_seam_ratio']}, T/B ratio={sc['tb_seam_ratio']}, "
            f"L/R MAD={sc['lr_border_mad']}, T/B MAD={sc['tb_border_mad']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
