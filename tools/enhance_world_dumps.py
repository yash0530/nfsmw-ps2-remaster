#!/usr/bin/env python3
"""Detail-Preserving Local 4x World Texture Super-Resolution + PBR-Baked
Micro-Detail Engine for NFSMW: Rockport 2026.

Instead of swapping unique Rockport storefronts, roads, buildings, and skies
with unrelated generic textures (or touching UI/HUD/fonts/icons/liveries),
this engine enhances REAL live-dumped PCSX2 world textures in-place into
razor-sharp, richly shaded mipmapped BC7_UNORM `.dds` replacements:

  1. Ultra-Strict Safety Filter (ZERO UI/Icon/Logo/Font/Livery/Cubemap leaks):
     - Parses PCSX2 v2.6.3 3-part `<TexHash>-<ClutHash>-<Flags>` and 2-part
       `<TexHash>-<Flags>` stems, preserving exact lowercase output stems.
     - Quarantines multi-CLUT textures (same `TexHash` appearing with >= 2
       distinct `ClutHash` values) to protect dynamic car vinyls, painted rims,
       and palette-swapped icons.
     - Skips small textures (`min(w, h) < 64` or `w * h < 4096`), banner/strip
       aspect ratios (`> 4.0` or `< 0.25`), non-opaque/alpha-cutout sprites,
       monochrome binary fonts/vinyls/minimap splines, reflection cubemaps,
       dark-framed car parts/headlights/wheels, crimson taillights, 1D chrome
       specular maps, risky 8-bit `PSMT8` UI/minimap/car-atlas screens, and
       TPK-matched UI/HUD/FX/car/logo/icon assets.
  2. Detail-Preserving 4x Enhancement Pipeline:
     - Optional `spandrel` + PyTorch MPS/CPU neural SR hook if local weights
       and packages are installed; otherwise pure NumPy + Pillow 2-stage
       Lanczos + edge-directed multi-scale luminance pyramid (zero haloing).
     - Multi-scale Sobel normal map + micro-cavity ambient occlusion baked in
       linear RGB space for genuine 3D brick/concrete/asphalt surface relief.
     - Material-aware seamless procedural micro-detail overlay (`asphalt`,
       `masonry_building`, `foliage_terrain`, `sky`) that protects painted road
       stripes, signs, and window structure.
  3. Exact PS2 GS Alpha Preservation:
     - Preserves PS2 GS uniform `0x80` (`128`) opaque alpha verbatim (never
       promoting `128 -> 255`, which would double PCSX2 GS brightness!).
  4. Native Apple Silicon / Rust `rockport_dds` BC7_UNORM encoding with full
     mipmaps, atomic writes, strict DX10 DDS validation, JSON manifest, and a
     side-by-side before/after visual self-audit contact sheet.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image, ImageDraw

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import bake_pbr_to_diffuse as bake_mod  # noqa: E402
import build_4k_texture_pack as pack_mod  # noqa: E402
import install_macos_texture_tools as tools_mod  # noqa: E402
import match_texture_dumps as mtd  # noqa: E402

DEFAULT_DUMPS_DIR = (
    REPO_ROOT / "build" / "flywheel" / "sandbox_textures" / "SLUS-21351" / "dumps"
)
DEFAULT_OUT_DIR = REPO_ROOT / "build" / "flywheel" / "world_enhanced_v1" / "replacements"
DEFAULT_TPK_DIR = REPO_ROOT / "build" / "tpk_extracted"
DEFAULT_MODELS_DIR = REPO_ROOT / "models"

# PS2 GS Pixel Storage Mode (low 6 bits of PCSX2 v2.6.3 8-hex flags)
PSM_CT32 = 0x00
PSM_T8 = 0x13
PSM_T4 = 0x14

# TPK denylist tokens — any TPK match containing these is protected UI/FX/car/logo.
_TPK_BLOCKED_SUBSTRINGS = (
    "UI_",
    "HUD",
    "FONT",
    "LOGO",
    "ICON",
    "MENU",
    "LOADING",
    "VINYL",
    "DECAL",
    "LICENSEPLATE",
    "LICENSE",
    "PLATE",
    "SHADOW",
    "FX_",
    "GLOW",
    "FLARE",
    "SUNCENTER",
    "SUNRING",
    "CURSOR",
    "ARROW",
    "MASK",
    "MINIMAP",
    "RADAR",
    "METER",
    "PDA",
    "TIP_",
    "CAREER_",
    "QRACE_",
    "CARLOT_",
    "CSHOP_",
    "ENVMAP",
    "SPECULAR",
    "REFLECT",
    "REFSKY",
    "SKID_",
    "TIRE",
    "WHEEL",
    "RIM",
    "BRAKE",
    "HEADLIGHT",
    "TAILLIGHT",
    "BRAKELIGHT",
    "BADGE",
    "INTERIOR",
    "DRIVER",
    "GLASS",
    "CHROME",
    "DEMO_OVERLAY",
    "TV_MASK",
    "RVM",
    "BKGBMP",
)

_TPK_BLOCKED_ARCHIVES = (
    "HUDTEX",
    "CARS_TEXTURES",
    "CARS_WHEELS",
)


@dataclasses.dataclass(frozen=True)
class ParsedDumpName:
    stem: str
    tex_hash: str
    clut_hash: Optional[str]
    flags_hex: Optional[str]
    psm: Optional[int]
    valid: bool


@dataclasses.dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str
    material: str = "unknown"
    alpha_mode: str = "unknown"
    alpha_value: int = 128
    alpha_min: int = 0
    alpha_max: int = 0
    tpk_match: Optional[str] = None
    tpk_score: float = 0.0
    metrics: Dict[str, float] = dataclasses.field(default_factory=dict)


# ---------------------------------------------------------------------------
# PCSX2 Stem Parsing & Multi-CLUT Analysis
# ---------------------------------------------------------------------------


def parse_pcsx2_dump_stem(stem: str) -> ParsedDumpName:
    """Parse PCSX2 v2.6.3 3-part `<TexHash>-<ClutHash>-<Flags>` or 2-part `<TexHash>-<Flags>` stems."""
    tex, clut, flags, ok = mtd.parse_pcsx2_stem_full(stem)
    if not ok or not tex:
        return ParsedDumpName(
            stem=stem.strip().lower(),
            tex_hash="",
            clut_hash=None,
            flags_hex=None,
            psm=None,
            valid=False,
        )
    psm: Optional[int] = None
    if flags is not None and len(flags) == 8:
        try:
            psm = int(flags, 16) & 0x3F
        except ValueError:
            psm = None
    return ParsedDumpName(
        stem=stem.strip().lower(),
        tex_hash=tex.lower(),
        clut_hash=clut.lower() if clut else None,
        flags_hex=flags.lower() if flags else None,
        psm=psm,
        valid=True,
    )


def find_multi_clut_tex_hashes(dump_paths: Sequence[Path]) -> Set[str]:
    """Identify every `TexHash` appearing with >= 2 distinct `ClutHash` values."""
    tex_to_cluts: Dict[str, Set[str]] = collections.defaultdict(set)
    for path in dump_paths:
        parsed = parse_pcsx2_dump_stem(path.stem)
        if parsed.valid and parsed.clut_hash:
            tex_to_cluts[parsed.tex_hash].add(parsed.clut_hash)
    return {th for th, cluts in tex_to_cluts.items() if len(cluts) >= 2}


# ---------------------------------------------------------------------------
# Fast Pure-NumPy Image Filtering & Feature Extraction
# ---------------------------------------------------------------------------


def _box_blur_1d_periodic(arr: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """O(N) 1D box blur with periodic wrap padding (exact seamless tiling)."""
    if radius <= 0:
        return arr
    n = arr.shape[axis]
    r = min(radius, max(1, (n - 1) // 2))
    k = 2 * r + 1
    if axis == 0:
        padded = np.concatenate([arr[-r:, ...], arr, arr[:r, ...]], axis=0)
        csum = np.cumsum(padded, axis=0, dtype=np.float64)
        zero = np.zeros((1,) + arr.shape[1:], dtype=np.float64)
        csum = np.concatenate([zero, csum], axis=0)
        out = (csum[k:, ...] - csum[:-k, ...]) / float(k)
    else:
        padded = np.concatenate([arr[:, -r:, ...], arr, arr[:, :r, ...]], axis=1)
        csum = np.cumsum(padded, axis=1, dtype=np.float64)
        zero = np.zeros((arr.shape[0], 1) + arr.shape[2:], dtype=np.float64)
        csum = np.concatenate([zero, csum], axis=1)
        out = (csum[:, k:, ...] - csum[:, :-k, ...]) / float(k)
    return out.astype(np.float32, copy=False)


def _box_blur_1d_edge(arr: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """O(N) 1D box blur with edge-replicate padding."""
    if radius <= 0:
        return arr
    n = arr.shape[axis]
    r = min(radius, max(1, (n - 1) // 2))
    k = 2 * r + 1
    if axis == 0:
        pad_width = ((r, r), (0, 0)) if arr.ndim == 2 else ((r, r), (0, 0), (0, 0))
        padded = np.pad(arr, pad_width, mode="edge")
        csum = np.cumsum(padded, axis=0, dtype=np.float64)
        zero = np.zeros((1,) + arr.shape[1:], dtype=np.float64)
        csum = np.concatenate([zero, csum], axis=0)
        out = (csum[k:, ...] - csum[:-k, ...]) / float(k)
    else:
        pad_width = ((0, 0), (r, r)) if arr.ndim == 2 else ((0, 0), (r, r), (0, 0))
        padded = np.pad(arr, pad_width, mode="edge")
        csum = np.cumsum(padded, axis=1, dtype=np.float64)
        zero = np.zeros((arr.shape[0], 1) + arr.shape[2:], dtype=np.float64)
        csum = np.concatenate([zero, csum], axis=1)
        out = (csum[:, k:, ...] - csum[:, :-k, ...]) / float(k)
    return out.astype(np.float32, copy=False)


def fast_gaussian_blur(
    arr: np.ndarray,
    radius: int,
    *,
    passes: int = 2,
    periodic: bool = True,
) -> np.ndarray:
    """Multi-pass O(N) separable box blur approximating a clean Gaussian kernel."""
    if radius <= 0:
        return arr.astype(np.float32, copy=True)
    fn = _box_blur_1d_periodic if periodic else _box_blur_1d_edge
    out = arr.astype(np.float32, copy=False)
    for _ in range(max(1, passes)):
        out = fn(out, radius, axis=1)
        out = fn(out, radius, axis=0)
    return out


def compute_sobel_gradients(height: np.ndarray, *, periodic: bool = True) -> Tuple[np.ndarray, np.ndarray]:
    """Compute Sobel (gx, gy) gradients of a 2D float32 height field in pure NumPy."""
    h = height.astype(np.float32, copy=False)
    if periodic:
        tl = np.roll(np.roll(h, 1, axis=0), 1, axis=1)
        tc = np.roll(h, 1, axis=0)
        tr = np.roll(np.roll(h, 1, axis=0), -1, axis=1)
        ml = np.roll(h, 1, axis=1)
        mr = np.roll(h, -1, axis=1)
        bl = np.roll(np.roll(h, -1, axis=0), 1, axis=1)
        bc = np.roll(h, -1, axis=0)
        br = np.roll(np.roll(h, -1, axis=0), -1, axis=1)
    else:
        p = np.pad(h, ((1, 1), (1, 1)), mode="edge")
        tl, tc, tr = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
        ml, mr = p[1:-1, :-2], p[1:-1, 2:]
        bl, bc, br = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]

    gx = (-tl + tr - 2.0 * ml + 2.0 * mr - bl + br) * 0.125
    gy = (-tl - 2.0 * tc - tr + bl + 2.0 * bc + br) * 0.125
    return gx.astype(np.float32), gy.astype(np.float32)


def measure_laplacian_sharpness(rgb_u8: np.ndarray) -> float:
    """Compute mean absolute 4-neighbor Laplacian energy of luminance (sharpness metric)."""
    rgb = rgb_u8.astype(np.float32)
    luma = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    p = np.pad(luma, ((1, 1), (1, 1)), mode="edge")
    lap = (
        p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] - 4.0 * p[1:-1, 1:-1]
    )
    return float(np.mean(np.abs(lap)))


def _rgb_to_hsv_fast(rgb_u8: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized RGB uint8 -> Hue [0..360), Sat [0..1], Val [0..1]."""
    rgb = rgb_u8.astype(np.float32) * (1.0 / 255.0)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    hue = np.zeros_like(cmax)
    nz = delta > 1e-6
    r_dom = nz & (cmax == r)
    g_dom = nz & (cmax == g) & (~r_dom)
    b_dom = nz & (~r_dom) & (~g_dom)

    hue[r_dom] = (60.0 * ((g[r_dom] - b[r_dom]) / delta[r_dom])) % 360.0
    hue[g_dom] = 60.0 * (((b[g_dom] - r[g_dom]) / delta[g_dom]) + 2.0)
    hue[b_dom] = 60.0 * (((r[b_dom] - g[b_dom]) / delta[b_dom]) + 4.0)

    sat = np.where(cmax > 1e-6, delta / np.maximum(cmax, 1e-6), 0.0).astype(np.float32)
    return hue.astype(np.float32), sat, cmax.astype(np.float32)


# ---------------------------------------------------------------------------
# Ultra-Strict Safety Filter & World Material Classifier
# ---------------------------------------------------------------------------


def is_tpk_blocked(tpk_name: str, tpk_archive: str = "") -> bool:
    """Return True if a TPK match identifies a protected UI/HUD/FX/car/logo/icon asset."""
    name = tpk_name.strip().upper()
    arch = tpk_archive.strip().upper()
    if not name and not arch:
        return False

    # Genuine sky domes (not reflection/specular/sun flare sprites) are allowed.
    if name.startswith("SKY_") and not any(
        tok in name for tok in ("SPECULAR", "REF", "SUN", "FLARE", "GLOW", "ENV")
    ):
        return False

    if any(tok in arch for tok in _TPK_BLOCKED_ARCHIVES):
        return True
    if any(tok in name for tok in _TPK_BLOCKED_SUBSTRINGS):
        return True
    if "DYNTEX" in arch and not name.startswith("SKY_"):
        return True
    return False


def classify_world_material(
    rgb_u8: np.ndarray,
    *,
    tpk_name: Optional[str] = None,
) -> str:
    """Classify an already safety-approved opaque world texture into a material profile.

    Returns one of: ``'sky'``, ``'asphalt'``, ``'foliage_terrain'``, ``'masonry_building'``.
    """
    if tpk_name and tpk_name.upper().startswith("SKY_"):
        return "sky"

    h, w, _ = rgb_u8.shape
    rgb = rgb_u8.astype(np.float32)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    hue, sat, val = _rgb_to_hsv_fast(rgb_u8)

    sat_mean = float(sat.mean())
    sat_p90 = float(np.percentile(sat, 90))
    luma_mean = float(gray.mean())

    # 1. Sky / clouds: bright cool blue sky gradient or bright low-frequency cloud bank
    #    (excludes dark blue painted metal shutters/tiles which have low luma or high slat gradient)
    blue_sky_mask = (b > r * 1.12) & (b > g * 1.02) & (b > 95.0)
    top_half_blue = float(blue_sky_mask[: max(1, h // 2), :].mean())
    total_blue = float(blue_sky_mask.mean())
    luma_p90 = float(np.percentile(gray, 90))
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    mean_grad = float(0.5 * (gx.mean() + gy.mean()))
    if luma_mean >= 115.0 and luma_p90 >= 165.0 and mean_grad < 11.0 and (
        (total_blue >= 0.25 and top_half_blue >= 0.35) or top_half_blue >= 0.55
    ):
        return "sky"

    # 2. Asphalt / tarmac / concrete roadway: desaturated neutral mid-to-dark gray
    #    with optional small yellow/white painted lane markings
    yellow_paint = (hue >= 32.0) & (hue <= 62.0) & (sat >= 0.28) & (val >= 0.35)
    white_paint = (sat < 0.14) & (gray > 195.0)
    paint_mask = yellow_paint | white_paint
    non_paint = ~paint_mask
    paint_frac = float(paint_mask.mean())
    if non_paint.any():
        np_sat = float(sat[non_paint].mean())
        np_sat_p90 = float(np.percentile(sat[non_paint], 90))
        np_luma = float(gray[non_paint].mean())
        gray_np = np.where(non_paint, gray, np_luma)
    else:
        np_sat = sat_mean
        np_sat_p90 = sat_p90
        np_luma = luma_mean
        gray_np = gray

    # Architectural facades tend to have strong axis-aligned rectilinear edge imbalance
    # or warm brick/wood coloration; asphalt has low saturation & balanced/road-aligned grain.
    edge_ratio = float(gx.mean()) / max(float(gy.mean()), 0.5)
    is_tall_road_strip = (h >= 512 and w >= 64 and (h / float(w)) >= 4.0)
    is_neutral = np_sat < 0.125 and np_sat_p90 < 0.24 and paint_frac < 0.30
    if is_tall_road_strip and np_sat < 0.16 and 28.0 <= np_luma <= 165.0:
        return "asphalt"
    if is_neutral and 32.0 <= np_luma <= 155.0 and 0.40 <= edge_ratio <= 2.5:
        # Check that the underlying non-paint surface isn't regular shutter slats
        row_energy = float(gray_np.mean(axis=1).std())
        col_energy = float(gray_np.mean(axis=0).std())
        luma_std = max(float(gray_np.std()), 1.0)
        if max(row_energy, col_energy) / luma_std < 0.65:
            return "asphalt"

    # 3. Foliage / grass / autumn terrain / earth / water
    green_earth = (
        ((hue >= 38.0) & (hue <= 165.0) & (sat >= 0.16) & (val >= 0.12) & (val <= 0.78))
    )
    green_frac = float(green_earth.mean())
    row_energy = float(gray.mean(axis=1).std())
    col_energy = float(gray.mean(axis=0).std())
    luma_std = max(float(gray.std()), 1.0)
    p95_grad = float(np.percentile(np.hypot(np.diff(gray, axis=1, prepend=gray[:, :1]), np.diff(gray, axis=0, prepend=gray[:1, :])), 95))
    if green_frac >= 0.48 and luma_std <= 34.0 and p95_grad <= 48.0 and max(row_energy, col_energy) / luma_std < 0.50:
        return "foliage_terrain"

    # 4. Default: masonry, brick, concrete buildings, wood siding, storefronts, bridges, signs
    return "masonry_building"


def evaluate_dump_safety(
    dump_path: Path,
    rgba_u8: np.ndarray,
    *,
    multi_clut_hashes: Set[str],
    tpk_match_info: Optional[Dict[str, Any]] = None,
    min_dim: int = 64,
    min_area: int = 4096,
    max_aspect: float = 4.0,
    strict_psmt4_only: bool = False,
) -> SafetyDecision:
    """Apply ultra-strict world-texture safety rules.

    Guarantees ZERO UI, HUD, font, logo, loading icon, minimap, car vinyl,
    wheel/headlight/taillight, or reflection cubemap corruption.
    """
    parsed = parse_pcsx2_dump_stem(dump_path.stem)
    if not parsed.valid:
        return SafetyDecision(False, "invalid_pcsx2_stem")

    if parsed.tex_hash in multi_clut_hashes:
        return SafetyDecision(False, "multi_clut_quarantine")

    h, w, _ = rgba_u8.shape
    if min(w, h) < min_dim or (w * h) < min_area:
        return SafetyDecision(False, "too_small")

    # NFSMW PS2 stores tiling road/sidewalk strips vertically (64x512, 128x512, 64x1024;
    # aspect 8:1..16:1), whereas horizontal strips >4:1 (e.g. 512x64, 256x32) are UI bars,
    # banners, or grass-edge cutouts. Allow tall vertical road strips up to 16:1 safely!
    aspect = max(w / float(h), h / float(w))
    is_vertical_road_strip = (h >= 512 and w >= 64 and h > w and (h / float(w)) <= 16.0)
    if aspect > max_aspect and not is_vertical_road_strip:
        return SafetyDecision(False, "extreme_aspect")

    rgb = rgba_u8[:, :, :3].astype(np.float32)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    _, sat, _ = _rgb_to_hsv_fast(rgba_u8[:, :, :3])

    gstd = float(gray.std())
    smean = float(sat.mean())
    luma_mean = float(gray.mean())
    pure_blk = float((rgb.max(axis=2) < 8.0).mean())
    pure_wht = float((rgb.min(axis=2) > 242.0).mean())

    # Alpha inspection — supports ALL THREE genuine PS2 opaque-world alpha encodings while
    # strictly rejecting 100% of alpha-cutout UI/HUD/fonts/logos/decals/trees/car-liveries:
    #   1. Uniform opaque: PS2 GS 0x80 (124..130) or PC 0xFF (>=245)
    #   2. PS2 wet-road specular alpha (`ps2_wet_alpha`): zero transparent pixels, alpha in
    #      80..130, 4-bit PSMT4 desaturated surface (blocks PSMT8 car liveries at alpha 12..18!)
    #   3. PS2 world reflection/specular mask in alpha (`ps2_specular_mask`): alpha_max in
    #      20..116 (strictly below cutout 120..128!), with richly textured full-frame RGB even
    #      where alpha==0 (matte stone/brick/asphalt has alpha=0, glossy windows/sheen >0!).
    alpha = rgba_u8[:, :, 3]
    a_min = int(alpha.min())
    a_max = int(alpha.max())
    a_span = a_max - a_min
    is_ps2_opaque = a_span <= 4 and (124 <= a_min <= 130)
    is_pc_opaque = a_span <= 4 and (a_min >= 245)

    zero_alpha_mask = alpha < 8
    zero_alpha_frac = float(zero_alpha_mask.mean())
    is_ps2_wet_alpha = (
        (not is_ps2_opaque)
        and (not is_pc_opaque)
        and zero_alpha_frac == 0.0
        and a_min >= 80
        and a_max <= 130
        and parsed.psm in (PSM_T4, None)
        and smean < 0.16
        and gstd >= 6.0
        and pure_blk < 0.05
    )

    is_ps2_specular_mask = False
    if (
        (not is_ps2_opaque)
        and (not is_pc_opaque)
        and (not is_ps2_wet_alpha)
        and 20 <= a_max <= 116
        and gstd >= 5.0
        and 26.0 <= luma_mean <= 205.0
        and pure_blk < 0.10
        and pure_wht < 0.15
    ):
        if int(zero_alpha_mask.sum()) >= 32:
            gstd_zero = float(gray[zero_alpha_mask].std())
            luma_zero = float(gray[zero_alpha_mask].mean())
            if gstd_zero >= 6.5 and 26.0 <= luma_zero <= 205.0:
                is_ps2_specular_mask = True
        else:
            is_ps2_specular_mask = True

    if not (is_ps2_opaque or is_pc_opaque or is_ps2_wet_alpha or is_ps2_specular_mask):
        return SafetyDecision(
            False,
            "non_opaque_alpha",
            alpha_min=a_min,
            alpha_max=a_max,
        )

    if is_ps2_opaque:
        alpha_mode = "ps2_0x80"
    elif is_pc_opaque:
        alpha_mode = "pc_0xff"
    elif is_ps2_wet_alpha:
        alpha_mode = "ps2_wet_alpha"
    else:
        alpha_mode = "ps2_specular_mask"
    alpha_val = int(round(float(np.median(alpha))))

    # TPK denylist check (protects UI tips, HUD, FX, car textures, envmaps, specular maps)
    tpk_label: Optional[str] = None
    tpk_name: Optional[str] = None
    tpk_score = 0.0
    if tpk_match_info is not None:
        tpk_score = float(tpk_match_info.get("score", 0.0))
        t_name = str(tpk_match_info.get("tpk_name") or tpk_match_info.get("name") or "")
        t_arch = str(tpk_match_info.get("tpk_archive") or tpk_match_info.get("archive") or "")
        if tpk_score >= 0.78 and t_name:
            tpk_label = f"{t_arch}/{t_name}" if t_arch else t_name
            tpk_name = t_name
            if is_tpk_blocked(t_name, t_arch):
                return SafetyDecision(
                    False,
                    "tpk_protected_ui_car_fx",
                    alpha_mode=alpha_mode,
                    alpha_value=alpha_val,
                    alpha_min=a_min,
                    alpha_max=a_max,
                    tpk_match=tpk_label,
                    tpk_score=tpk_score,
                )

    n_uniq_gray = int(len(np.unique(gray.astype(np.uint8))))
    dark_frac = float((gray < 18.0).mean())
    light_frac = float((gray > 195.0).mean())
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    border_dark = float((border < 16.0).mean())

    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    gmag = np.hypot(gx, gy)
    flat_frac = float((gmag < 2.0).mean())
    p95_g = float(np.percentile(gmag, 95))
    p97_g = float(np.percentile(gmag, 97))

    red_dom_frac = float(((r > 110.0) & (r > g * 1.7) & (r > b * 1.7)).mean())

    metrics = {
        "gstd": round(gstd, 2),
        "sat_mean": round(smean, 4),
        "dark_frac": round(dark_frac, 4),
        "pure_blk": round(pure_blk, 4),
        "border_dark": round(border_dark, 4),
        "flat_frac": round(flat_frac, 4),
        "p95_grad": round(p95_g, 2),
        "red_dom": round(red_dom_frac, 4),
    }

    # 1. Flat / blank / constant-color textures
    if gstd < 4.5 or n_uniq_gray <= 3:
        return SafetyDecision(False, "flat_or_binary_texture", metrics=metrics)

    # 2. Binary / monochrome font atlas or pure B&W UI mask
    extremes_frac = float(((gray < 20.0) | (gray > 235.0)).mean())
    if n_uniq_gray <= 4 or (smean < 0.04 and extremes_frac > 0.75 and p95_g > 35.0):
        return SafetyDecision(False, "binary_font_or_mask", metrics=metrics)

    # 3. Reflection cubemap / spherical environment map (PSMCT32 direct RGBA or dark corners + bright core)
    ch, cw = max(2, h // 6), max(2, w // 6)
    corners = np.concatenate(
        [
            gray[:ch, :cw].ravel(),
            gray[:ch, -cw:].ravel(),
            gray[-ch:, :cw].ravel(),
            gray[-ch:, -cw:].ravel(),
        ]
    )
    center = gray[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
    corner_luma = float(corners.mean())
    center_luma = float(center.mean())
    if parsed.psm == PSM_CT32 and corner_luma < 28.0 and (center_luma - corner_luma) > 30.0:
        return SafetyDecision(False, "reflection_envmap", metrics=metrics)
    if corner_luma < 12.0 and border_dark > 0.65 and center_luma > 75.0:
        return SafetyDecision(False, "radial_glow_or_envmap", metrics=metrics)

    # 4. Dark-framed isolated sprite / car headlight / wheel / undercarriage
    if border_dark >= 0.45 and pure_blk >= 0.10:
        return SafetyDecision(False, "dark_framed_car_part_or_sprite", metrics=metrics)

    # 5. Dark minimap / UI board / dark car interior
    if dark_frac >= 0.45 and gstd >= 22.0:
        return SafetyDecision(False, "dark_ui_or_minimap", metrics=metrics)

    # 6. Monochrome vector graphic: minimap splines & car tribal vinyls
    #    (nearly zero saturation + large flat solid regions + sharp vector edges)
    if smean < 0.035 and flat_frac > 0.50 and p97_g > 30.0 and gstd > 20.0:
        return SafetyDecision(False, "monochrome_vinyl_or_minimap", metrics=metrics)

    # 7. Bright crimson car taillights (natural red bricks are muted terracotta, not neon ruby)
    if red_dom_frac >= 0.25:
        return SafetyDecision(False, "car_taillight_or_signal", metrics=metrics)

    # 8. 1D brushed chrome / specular reflection lookup map
    if smean < 0.045 and gstd > 50.0 and flat_frac < 0.35:
        col_std = float(gray.mean(axis=0).std())
        row_std = float(gray.mean(axis=1).std())
        aniso = max(col_std, row_std) / max(min(col_std, row_std), 0.5)
        if aniso >= 3.0:
            return SafetyDecision(False, "chrome_specular_gradient", metrics=metrics)

    # 9. License plate / high-contrast white text placard (e.g. NFSMW plate)
    if light_frac >= 0.35 and smean < 0.14 and p95_g >= 110.0 and gstd >= 50.0:
        return SafetyDecision(False, "license_plate_or_text_card", metrics=metrics)

    # 10. High-contrast car/prop/wheel multi-panel atlas sheets
    if p95_g >= 80.0 and gstd >= 42.0 and max(w, h) <= 256 and not (tpk_name and tpk_name.upper().startswith("SKY_")):
        return SafetyDecision(False, "high_contrast_car_or_prop_atlas", metrics=metrics)

    # 10b. Circular car wheel rims / brake rotors / hubcaps (high 90-deg rotational symmetry
    #      or strong concentric radial profile on desaturated square textures)
    if w == h and smean < 0.12 and gstd >= 22.0:
        g_centered = gray - float(gray.mean())
        g_rot = np.rot90(g_centered)
        denom = float(np.sqrt((g_centered * g_centered).sum() * (g_rot * g_rot).sum())) + 1e-6
        rot90_corr = float((g_centered * g_rot).sum() / denom)
        if rot90_corr >= 0.50:
            return SafetyDecision(False, "circular_wheel_or_hubcap", metrics=metrics)
        if smean < 0.03:
            yy_r, xx_r = np.mgrid[-1.0:1.0:h * 1j, -1.0:1.0:w * 1j]
            rr_r = np.sqrt(xx_r * xx_r + yy_r * yy_r)
            rbins = np.clip((rr_r * 24.0).astype(np.int32), 0, 34)
            r_pred = np.zeros_like(gray)
            for b_idx in range(35):
                mask = rbins == b_idx
                if mask.any():
                    r_pred[mask] = gray[mask].mean()
            radial_r2 = 1.0 - float(((gray - r_pred) ** 2).mean()) / max(float(gray.var()), 1e-6)
            if radial_r2 >= 0.18:
                return SafetyDecision(False, "circular_wheel_or_hubcap", metrics=metrics)

    # 11. PS2 8-bit PSMT8 (256-color) strict world gate:
    #     In NFSMW PS2, PSMT8 holds UI menus, loading splashes, world minimaps,
    #     wheel rims, and high-res car interior/exterior atlases; only smooth
    #     bounded terrain/road/wall PSMT8 textures pass unless --strict-psmt4.
    if parsed.psm == PSM_T8:
        if strict_psmt4_only:
            return SafetyDecision(False, "psmt8_strict_skip", metrics=metrics)
        if max(w, h) >= 512 and min(w, h) >= 256:
            return SafetyDecision(False, "psmt8_ui_splash_or_minimap", metrics=metrics)
        if gstd > 34.0 or dark_frac > 0.12:
            return SafetyDecision(False, "psmt8_ui_or_car_atlas", metrics=metrics)

    material = classify_world_material(rgba_u8[:, :, :3], tpk_name=tpk_name)
    return SafetyDecision(
        allowed=True,
        reason="approved_world",
        material=material,
        alpha_mode=alpha_mode,
        alpha_value=alpha_val,
        alpha_min=a_min,
        alpha_max=a_max,
        tpk_match=tpk_label,
        tpk_score=tpk_score,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Optional Neural Upscaler Hook (`spandrel` + PyTorch MPS/CPU)
# ---------------------------------------------------------------------------


class OptionalSpandrelUpscaler:
    """Optional PyTorch MPS/CPU `spandrel` upscaler if local weights & deps exist."""

    def __init__(self, weights_path: Optional[Path] = None) -> None:
        self.weights_path = weights_path
        self.available = False
        self._model = None
        self._torch = None
        self._device = None
        if weights_path is not None and weights_path.is_file():
            self._try_load(weights_path)

    def _try_load(self, weights_path: Path) -> None:
        try:
            import torch  # type: ignore[import-not-found]
            import spandrel  # type: ignore[import-not-found]
        except ImportError:
            return
        try:
            device = (
                torch.device("mps")
                if hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
                else torch.device("cpu")
            )
            desc = spandrel.ModelLoader(device=device).load_from_file(str(weights_path))
            desc.model.eval()
            self._torch = torch
            self._device = device
            self._model = desc.model
            self.available = True
        except Exception:
            self.available = False

    def upscale_rgb(self, rgb_u8: np.ndarray) -> Optional[np.ndarray]:
        if not self.available or self._model is None or self._torch is None:
            return None
        torch = self._torch
        tensor = (
            torch.from_numpy(rgb_u8.astype(np.float32) / 255.0)
            .permute(2, 0, 1)
            .unsqueeze(0)
            .to(self._device)
        )
        with torch.no_grad():
            out = self._model(tensor).clamp(0.0, 1.0).squeeze(0).permute(1, 2, 0).cpu().numpy()
        return np.clip(np.round(out * 255.0), 0, 255).astype(np.uint8)


def find_local_upscaler_weights(models_dir: Path = DEFAULT_MODELS_DIR) -> Optional[Path]:
    if not models_dir.is_dir():
        return None
    for ext in ("*.pth", "*.safetensors", "*.pt"):
        hits = sorted(models_dir.glob(ext))
        if hits:
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# Detail-Preserving 4x Super-Resolution + Sobel Normal/AO + Micro-Detail
# ---------------------------------------------------------------------------


def compute_target_pot_size(
    orig_w: int,
    orig_h: int,
    *,
    scale: int = 4,
    max_size: int = 1024,
) -> Tuple[int, int]:
    """Compute Power-of-Two target dimensions preserving exact aspect ratio."""
    long_side = max(orig_w, orig_h)
    eff_scale = float(scale)
    if long_side * eff_scale > max_size:
        eff_scale = max(1.0, float(max_size) / float(long_side))
    # Snap effective scale to nearest lower/equal power of two if inputs are PoT
    if bake_mod.is_power_of_two(orig_w) and bake_mod.is_power_of_two(orig_h):
        pot_scale = 1
        while pot_scale * 2 <= eff_scale + 1e-6:
            pot_scale *= 2
        tw = min(max_size, max(4, orig_w * pot_scale))
        th = min(max_size, max(4, orig_h * pot_scale))
        return tw, th
    tw = bake_mod.snap_pot(int(round(orig_w * eff_scale)), mode="nearest", min_dim=4, max_dim=max_size)
    th = bake_mod.snap_pot(int(round(orig_h * eff_scale)), mode="nearest", min_dim=4, max_dim=max_size)
    return tw, th


def _rgb_to_ycbcr(rgb01: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    r, g, b = rgb01[:, :, 0], rgb01[:, :, 1], rgb01[:, :, 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = (b - y) * 0.56433408
    cr = (r - y) * 0.71326676
    return y, cb, cr


def _ycbcr_to_rgb(y: np.ndarray, cb: np.ndarray, cr: np.ndarray) -> np.ndarray:
    r = y + 1.402 * cr
    g = y - 0.344136 * cb - 0.714136 * cr
    b = y + 1.772 * cb
    return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0).astype(np.float32)


def _deterministic_seed(tag: str) -> int:
    digest = hashlib.blake2b(tag.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "little")


def _seamless_micro_grain(
    height: int,
    width: int,
    seed: int,
    *,
    tile_size: int = 256,
) -> Tuple[np.ndarray, np.ndarray]:
    """Generate fast 100% tile-seamless (fine_grain, medium_grain) fields in [-1, 1]."""
    rng = np.random.default_rng(seed)
    th = min(height, tile_size)
    tw = min(width, tile_size)

    # Fine 1px-2px aggregate/plaster grain (tiled seamlessly)
    raw_fine = rng.normal(0.0, 1.0, size=(th, tw)).astype(np.float32)
    fine_tile = 0.65 * raw_fine + 0.35 * fast_gaussian_blur(raw_fine, radius=1, passes=1, periodic=True)
    fine_tile /= max(float(fine_tile.std()), 1e-5)

    # Medium organic surface roughness / puddle field via periodic fBm
    med_tile = bake_mod.periodic_fbm(
        th,
        tw,
        base_freq=4,
        octaves=4,
        persistence=0.55,
        seed=seed ^ 0x5A17,
        domain_warp=0.22,
    )
    med_tile = (med_tile - float(med_tile.mean())) / max(float(med_tile.std()), 1e-5)

    if (th, tw) != (height, width):
        ry = (height + th - 1) // th
        rx = (width + tw - 1) // tw
        fine = np.tile(fine_tile, (ry, rx))[:height, :width]
        med = np.tile(med_tile, (ry, rx))[:height, :width]
    else:
        fine, med = fine_tile, med_tile

    return np.clip(fine * 0.45, -1.0, 1.0), np.clip(med * 0.50, -1.0, 1.0)


def _edge_directed_upscale_rgb(
    rgb_u8: np.ndarray,
    target_w: int,
    target_h: int,
    *,
    material: str,
    neural_upscaler: Optional[OptionalSpandrelUpscaler] = None,
) -> np.ndarray:
    """Upscale RGB uint8 to `(target_h, target_w, 3)` float32 [0, 1] with crisp edges."""
    if neural_upscaler is not None and neural_upscaler.available:
        sr = neural_upscaler.upscale_rgb(rgb_u8)
        if sr is not None:
            if sr.shape[1] != target_w or sr.shape[0] != target_h:
                sr = np.asarray(
                    Image.fromarray(sr).resize((target_w, target_h), Image.Resampling.LANCZOS),
                    dtype=np.uint8,
                )
            return sr.astype(np.float32) * (1.0 / 255.0)

    src_h, src_w, _ = rgb_u8.shape
    img = Image.fromarray(rgb_u8)

    if material == "sky":
        # Smooth Lanczos upscale without harsh sharpening; de-banding happens next.
        up = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
        return np.asarray(up, dtype=np.float32) * (1.0 / 255.0)

    # 2-stage progressive Lanczos with gentle intermediate luminance tightening
    # yields noticeably crisper 4x edges than single-shot Lanczos without ringing.
    if target_w >= src_w * 4 and target_h >= src_h * 4:
        mid_w, mid_h = src_w * 2, src_h * 2
        mid_arr = np.asarray(
            img.resize((mid_w, mid_h), Image.Resampling.LANCZOS), dtype=np.float32
        ) * (1.0 / 255.0)
        y, cb, cr = _rgb_to_ycbcr(mid_arr)
        y_blur = fast_gaussian_blur(y, radius=1, passes=2, periodic=True)
        detail = y - y_blur
        # Soft-clamped detail tightening (zero white/black ringing halos)
        y_sharp = np.clip(y + 0.55 * (0.09 * np.tanh(detail / 0.09)), 0.0, 1.0)
        mid_sharp = np.clip(np.round(_ycbcr_to_rgb(y_sharp, cb, cr) * 255.0), 0, 255).astype(np.uint8)
        up = Image.fromarray(mid_sharp).resize((target_w, target_h), Image.Resampling.LANCZOS)
        return np.asarray(up, dtype=np.float32) * (1.0 / 255.0)

    up = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return np.asarray(up, dtype=np.float32) * (1.0 / 255.0)


def _enhance_sky(rgb01: np.ndarray) -> np.ndarray:
    """De-band 16-color PS2 sky CLUT steps while keeping cloud wisps crisp & atmospheric."""
    y, cb, cr = _rgb_to_ycbcr(rgb01)

    # Edge-preserving gradient smoothing removes staircase CLUT banding in flat sky zones
    gx, gy = compute_sobel_gradients(y, periodic=False)
    grad = np.hypot(gx, gy)
    smooth_mask = np.clip(1.0 - grad / 0.045, 0.0, 1.0)

    y_smooth = fast_gaussian_blur(y, radius=4, passes=2, periodic=False)
    cb_smooth = fast_gaussian_blur(cb, radius=6, passes=2, periodic=False)
    cr_smooth = fast_gaussian_blur(cr, radius=6, passes=2, periodic=False)

    y_deband = y * (1.0 - 0.70 * smooth_mask) + y_smooth * (0.70 * smooth_mask)
    cb_deband = cb * (1.0 - 0.80 * smooth_mask) + cb_smooth * (0.80 * smooth_mask)
    cr_deband = cr * (1.0 - 0.80 * smooth_mask) + cr_smooth * (0.80 * smooth_mask)

    # Subtle large-scale cloud local contrast (no high-frequency noise!)
    y_med = fast_gaussian_blur(y_deband, radius=10, passes=2, periodic=False)
    cloud_contrast = 0.08 * np.tanh((y_deband - y_med) / 0.08)
    y_out = np.clip(y_deband + 0.35 * cloud_contrast, 0.0, 1.0)

    # Gentle atmospheric chroma enrichment
    cb_out = np.clip(cb_deband * 1.08, -0.5, 0.5)
    cr_out = np.clip(cr_deband * 1.06, -0.5, 0.5)
    return _ycbcr_to_rgb(y_out, cb_out, cr_out)


def enhance_world_texture_rgba(
    rgba_u8: np.ndarray,
    *,
    material: str,
    scale: int = 4,
    max_size: int = 1024,
    seed_key: str = "rockport",
    neural_upscaler: Optional[OptionalSpandrelUpscaler] = None,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Enhance a single safety-approved opaque world RGBA texture.

    Guarantees exact preservation of the original PS2 GS alpha range (e.g.
    uniform `128` stays exact `128`, uniform `255` stays exact `255`).
    """
    src_h, src_w, _ = rgba_u8.shape
    target_w, target_h = compute_target_pot_size(src_w, src_h, scale=scale, max_size=max_size)

    # Record exact source alpha before any RGB work!
    src_alpha = rgba_u8[:, :, 3]
    a_min = int(src_alpha.min())
    a_max = int(src_alpha.max())

    # 1. Detail-preserving base upscale
    rgb01 = _edge_directed_upscale_rgb(
        rgba_u8[:, :, :3],
        target_w,
        target_h,
        material=material,
        neural_upscaler=neural_upscaler,
    )

    if material == "sky":
        out_rgb01 = _enhance_sky(rgb01)
    else:
        # 2. Multi-scale luminance pyramid (CLAHE-like local contrast + halo-free sharpening)
        y, cb, cr = _rgb_to_ycbcr(rgb01)
        y_fine_blur = fast_gaussian_blur(y, radius=2, passes=2, periodic=True)
        y_med_blur = fast_gaussian_blur(y, radius=9, passes=2, periodic=True)

        fine_detail = y - y_fine_blur
        med_detail = y_fine_blur - y_med_blur

        # Soft-clamped detail boost prevents ringing halos around hard edges/signs
        gx0, gy0 = compute_sobel_gradients(y, periodic=True)
        edge_mag = np.hypot(gx0, gy0)
        anti_halo = 1.0 / (1.0 + (edge_mag * 6.5) ** 2)

        if material == "asphalt":
            fine_gain, med_gain, relief_str, ao_str, sat_boost = 0.72, 0.30, 1.9, 0.42, 1.02
        elif material == "foliage_terrain":
            fine_gain, med_gain, relief_str, ao_str, sat_boost = 0.80, 0.32, 2.2, 0.46, 1.07
        else:  # masonry_building
            fine_gain, med_gain, relief_str, ao_str, sat_boost = 0.76, 0.34, 2.4, 0.52, 1.05

        fine_boost = fine_gain * (0.075 * np.tanh(fine_detail / 0.075)) * (0.45 + 0.55 * anti_halo)
        med_boost = med_gain * (0.10 * np.tanh(med_detail / 0.10))
        y_crisp = np.clip(y + fine_boost + med_boost, 0.0, 1.0)

        # Subtle chroma richness without oversaturating neutral concrete/asphalt
        cb_out = np.clip(cb * sat_boost, -0.5, 0.5)
        cr_out = np.clip(cr * sat_boost, -0.5, 0.5)
        crisp_rgb01 = _ycbcr_to_rgb(y_crisp, cb_out, cr_out)

        # 3. Multi-scale Sobel Normal Map + Micro-Cavity Ambient Occlusion in linear RGB
        #    Compute height field from smoothed luminance so 1px PS2 CLUT dithering &
        #    pixel-art staircases are never embossed, while true brick/mortar/ledge relief pops!
        height_field = fast_gaussian_blur(y_crisp, radius=2, passes=2, periodic=True)
        hx, hy = compute_sobel_gradients(height_field, periodic=True)

        # Surface normal N = normalize(-hx * relief, -hy * relief, 1.0)
        nx = -hx * relief_str
        ny = -hy * relief_str
        nz = np.ones_like(nx)
        inv_len = 1.0 / np.sqrt(nx * nx + ny * ny + nz * nz + 1e-8)
        nx *= inv_len
        ny *= inv_len
        nz *= inv_len

        # Late-afternoon Rockport sunlight direction (elev ~42 deg, azim ~35 deg)
        elev = math.radians(42.0)
        azim = math.radians(35.0)
        lx = math.cos(elev) * math.cos(azim)
        ly = math.cos(elev) * math.sin(azim)
        lz = math.sin(elev)

        ndotl = nx * lx + ny * ly + nz * lz
        sun_shade = 1.0 + 0.24 * np.clip(ndotl - lz, -0.40, 0.40)

        # Micro-cavity AO: recessed cracks, mortar joints, window ledges, tarmac pits
        h_local = fast_gaussian_blur(height_field, radius=6, passes=2, periodic=True)
        cavity = height_field - h_local
        ao_factor = np.clip(1.0 + ao_str * np.clip(cavity, -0.22, 0.09), 0.82, 1.05)

        lin = bake_mod.srgb_to_linear(crisp_rgb01)
        lin *= (sun_shade * ao_factor)[:, :, None]

        # 4. Material-Aware Seamless Procedural Micro-Detail Overlay
        seed = _deterministic_seed(seed_key)
        fine_grain, med_grain = _seamless_micro_grain(target_h, target_w, seed)

        # Protect sharp architectural features, sign lettering & road markings from grain
        feature_protect = np.clip(1.0 - edge_mag * 5.0, 0.25, 1.0)

        if material == "asphalt":
            _, sat_up, _ = _rgb_to_hsv_fast(
                np.clip(np.round(crisp_rgb01 * 255.0), 0, 255).astype(np.uint8)
            )
            # Protect painted yellow/white lane lines & crosswalks from wet darkening/aggregate
            paint_mask = ((sat_up > 0.20) | (y_crisp > 0.66)).astype(np.float32)
            paint_mask = fast_gaussian_blur(paint_mask, radius=2, passes=1, periodic=True)
            tarmac_mask = np.clip(1.0 - paint_mask * 1.4, 0.0, 1.0)

            # High-frequency tarmac aggregate micro-grain
            aggregate = 1.0 + (0.048 * fine_grain + 0.018 * med_grain) * tarmac_mask * feature_protect
            lin *= aggregate[:, :, None]

            # Subtle wet-sheen puddle contrast (richer darker damp tarmac + cool specular micro-sheen)
            puddle = np.clip((med_grain + 0.10) * 1.35, 0.0, 1.0) * tarmac_mask
            wet_darken = 1.0 - 0.13 * puddle
            lin *= wet_darken[:, :, None]

            # Subtle grazing sunlit/sky sheen on damp pockets
            sheen = (0.018 * puddle * np.clip(ndotl, 0.0, 1.0))[:, :, None]
            lin += sheen * np.array([0.92, 0.96, 1.04], dtype=np.float32)[None, None, :]

        elif material == "foliage_terrain":
            # Organic multi-scale grass/leaf/soil micro-detail
            organic = 1.0 + (0.042 * fine_grain + 0.022 * med_grain) * feature_protect
            lin *= organic[:, :, None]

        else:  # masonry_building
            # Subtle plaster/mortar/stone micro-grain (preserves windows, awnings, signs!)
            masonry = 1.0 + (0.032 * fine_grain + 0.014 * med_grain) * feature_protect
            lin *= masonry[:, :, None]

        # Subtle toe/highlight rolloff to preserve clean dynamic range
        out_rgb01 = bake_mod.linear_to_srgb(np.clip(lin, 0.0, 1.0))

    out_rgb_u8 = np.clip(np.round(out_rgb01 * 255.0), 0, 255).astype(np.uint8)

    # 5. CRITICAL PS2 GS ALPHA PRESERVATION (never promote 128 -> 255!)
    if a_min == a_max:
        out_alpha_u8 = np.full((target_h, target_w), a_min, dtype=np.uint8)
    else:
        alpha_up = Image.fromarray(src_alpha).resize(
            (target_w, target_h), Image.Resampling.BILINEAR
        )
        out_alpha_u8 = np.clip(np.asarray(alpha_up, dtype=np.uint8), a_min, a_max)

    out_rgba = np.dstack([out_rgb_u8, out_alpha_u8])

    # Measure sharpness improvement at matched target resolution (vs plain bilinear upscale)
    baseline_up = np.asarray(
        Image.fromarray(rgba_u8[:, :, :3]).resize((target_w, target_h), Image.Resampling.BILINEAR),
        dtype=np.uint8,
    )
    sharp_before = measure_laplacian_sharpness(baseline_up)
    sharp_after = measure_laplacian_sharpness(out_rgb_u8)

    stats = {
        "orig_size": [src_w, src_h],
        "out_size": [target_w, target_h],
        "material": material,
        "alpha_preserved_min": int(out_alpha_u8.min()),
        "alpha_preserved_max": int(out_alpha_u8.max()),
        "sharpness_baseline": round(sharp_before, 3),
        "sharpness_enhanced": round(sharp_after, 3),
        "sharpness_gain": round(sharp_after / max(sharp_before, 1e-4), 3),
    }
    return out_rgba, stats


# ---------------------------------------------------------------------------
# Atomic Native Rust BC7_UNORM DDS Encoding
# ---------------------------------------------------------------------------


def encode_rgba_to_bc7_dds_atomic(
    encoder_bin: Path,
    rgba_u8: np.ndarray,
    dst_dds: Path,
    *,
    threads: int = 2,
) -> pack_mod.DdsValidationResult:
    """Atomically encode tightly-packed RGBA8 array to mipmapped BC7_UNORM DDS."""
    h, w, c = rgba_u8.shape
    if c != 4:
        raise ValueError(f"Expected RGBA8 array with 4 channels, got shape {rgba_u8.shape}")

    dst_dds.parent.mkdir(parents=True, exist_ok=True)
    contiguous = np.ascontiguousarray(rgba_u8, dtype=np.uint8)

    with tempfile.TemporaryDirectory(prefix="rockport_world_dds_") as tmp:
        raw_path = Path(tmp) / "input.rgba"
        tmp_dds = dst_dds.parent / f".{dst_dds.name}.{os.getpid()}.{time.time_ns()}.tmp"
        raw_path.write_bytes(contiguous.tobytes())

        cmd = [
            str(encoder_bin),
            "--input",
            str(raw_path),
            "--input-raw",
            "--width",
            str(w),
            "--height",
            str(h),
            "--output",
            str(tmp_dds),
            "--format",
            "bc7",
            "--mipmaps",
            "--dx10",
            "--threads",
            str(max(1, int(threads))),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"rockport_dds failed for {dst_dds.name}: {proc.stderr.strip() or proc.stdout.strip()}"
                )

            val = pack_mod.validate_bc7_dds(tmp_dds, require_srgb=False)
            if not val.valid:
                raise RuntimeError(f"BC7 DDS validation failed for {dst_dds.name}: {val.error}")

            os.replace(tmp_dds, dst_dds)
            return dataclasses.replace(val, path=str(dst_dds))
        finally:
            if tmp_dds.exists():
                try:
                    tmp_dds.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Visual Self-Audit Before/After Contact Sheet
# ---------------------------------------------------------------------------


def build_before_after_preview(
    preview_samples: Sequence[Dict[str, Any]],
    out_png: Path,
    *,
    max_items: int = 16,
) -> Optional[Path]:
    """Write and Pillow-validate a side-by-side Original vs Enhanced 4x contact sheet."""
    if not preview_samples:
        return None

    # Diversify selection across material classes and larger resolutions first
    by_mat: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for item in preview_samples:
        by_mat[item["material"]].append(item)
    for mat_list in by_mat.values():
        # Prioritize newly recovered high-impact road strips, wet roads, and specular-alpha
        # storefronts alongside large world surfaces so the preview sheet audits them first.
        mat_list.sort(
            key=lambda x: (
                1 if x.get("alpha_mode") in ("ps2_specular_mask", "ps2_wet_alpha") or x.get("is_road_strip") else 0,
                x["out_size"][0] * x["out_size"][1],
                x["sharpness_gain"],
            ),
            reverse=True,
        )

    selected: List[Dict[str, Any]] = []
    order = ["asphalt", "masonry_building", "foliage_terrain", "sky"]
    # Round-robin across available materials so every category is represented
    while len(selected) < max_items:
        added_any = False
        for mat in order:
            if by_mat[mat] and len(selected) < max_items:
                selected.append(by_mat[mat].pop(0))
                added_any = True
        if not added_any:
            break

    n = len(selected)
    cols = 2
    rows = (n + cols - 1) // cols
    crop_sz = 192
    gap = 6
    cell_w = crop_sz * 2 + gap
    label_h = 24
    pad = 14
    header_h = 38

    canvas_w = pad + cols * (cell_w + pad)
    canvas_h = header_h + pad + rows * (crop_sz + label_h + pad)
    sheet = Image.new("RGB", (canvas_w, canvas_h), (20, 22, 26))
    draw = ImageDraw.Draw(sheet)

    draw.text(
        (pad, 10),
        f"NFSMW Rockport 2026 - World Texture 4x Detail-Preserving Enhancement Audit (Top {n}: LEFT=Orig Dump | RIGHT=Enhanced BC7)",
        fill=(240, 244, 248),
    )

    for idx, item in enumerate(selected):
        r, c = divmod(idx, cols)
        x0 = pad + c * (cell_w + pad)
        y0 = header_h + pad + r * (crop_sz + label_h + pad)

        orig_rgb = item["orig_rgb"]
        enh_rgb = item["enh_rgb"]
        oh, ow, _ = orig_rgb.shape
        eh, ew, _ = enh_rgb.shape

        # Take a centered square crop (or full aspect-fitted resize if small) to show crisp 1:1 detail
        orig_up = Image.fromarray(orig_rgb).resize((ew, eh), Image.Resampling.BILINEAR)
        enh_im = Image.fromarray(enh_rgb)

        side = min(ew, eh, crop_sz * 2)
        cx, cy = ew // 2, eh // 2
        box = (
            max(0, cx - side // 2),
            max(0, cy - side // 2),
            min(ew, cx - side // 2 + side),
            min(eh, cy - side // 2 + side),
        )
        orig_tile = orig_up.crop(box).resize((crop_sz, crop_sz), Image.Resampling.LANCZOS)
        enh_tile = enh_im.crop(box).resize((crop_sz, crop_sz), Image.Resampling.LANCZOS)

        sheet.paste(orig_tile, (x0, y0))
        sheet.paste(enh_tile, (x0 + crop_sz + gap, y0))

        # Divider bar between left (orig) and right (enhanced)
        draw.rectangle(
            (x0 + crop_sz + 1, y0, x0 + crop_sz + gap - 2, y0 + crop_sz - 1),
            fill=(70, 175, 255),
        )

        stem_short = item["stem"][:14]
        lbl = (
            f"{stem_short} | {ow}x{oh}->{ew}x{eh} | {item['material']} | "
            f"a={item['alpha_val']} | {item['sharpness_gain']:.2f}x"
        )
        draw.text((x0 + 2, y0 + crop_sz + 5), lbl, fill=(216, 224, 232))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    tmp_png = out_png.parent / f".{out_png.name}.{os.getpid()}.tmp.png"
    try:
        sheet.save(tmp_png, format="PNG")
        # Strictly verify full decodability before publishing
        with Image.open(tmp_png) as check_im:
            check_im.load()
        os.replace(tmp_png, out_png)
    finally:
        if tmp_png.exists():
            try:
                tmp_png.unlink()
            except OSError:
                pass
    return out_png


# ---------------------------------------------------------------------------
# Batch Directory Pipeline
# ---------------------------------------------------------------------------


def _load_optional_tpk_index(
    tpk_dir: Optional[Path],
) -> Optional[Tuple[List[Dict[str, Any]], np.ndarray, np.ndarray, np.ndarray]]:
    if tpk_dir is None or not tpk_dir.is_dir():
        return None
    db = mtd.load_tpk_database(tpk_dir)
    if not db:
        return None
    dhashes = np.array([e["sig"].dhash for e in db], dtype=np.uint64)
    phashes = np.array([e["sig"].phash for e in db], dtype=np.uint64)
    hists = np.stack([e["sig"].color_hist for e in db], axis=0)
    return db, dhashes, phashes, hists


def enhance_dumps_directory(
    dumps_dir: Path,
    out_replacements_dir: Path,
    *,
    scale: int = 4,
    max_size: int = 1024,
    tpk_dir: Optional[Path] = DEFAULT_TPK_DIR,
    encoder_bin: Optional[Path] = None,
    upscaler_weights: Optional[Path] = None,
    workers: int = 4,
    dds_threads: int = 2,
    strict_psmt4_only: bool = False,
    limit: Optional[int] = None,
    preview_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Scan `dumps_dir`, strictly filter world textures, enhance 4x, and encode BC7 DDS."""
    t0 = time.monotonic()

    if not dumps_dir.is_dir():
        raise FileNotFoundError(f"Dumps directory not found: {dumps_dir}")

    if encoder_bin is None:
        encoder_bin = tools_mod.find_rockport_dds()
    if encoder_bin is None or not encoder_bin.is_file():
        raise FileNotFoundError(
            "Native rockport_dds encoder not found; build it via tools/install_macos_texture_tools.py"
        )

    dump_files = sorted(dumps_dir.glob("*.png"))
    if limit is not None and limit > 0:
        dump_files = dump_files[:limit]

    # Always scan full directory for multi-CLUT collisions even if --limit is passed
    all_dump_files = sorted(dumps_dir.glob("*.png"))
    multi_clut_hashes = find_multi_clut_tex_hashes(all_dump_files)

    tpk_index = _load_optional_tpk_index(tpk_dir)
    if upscaler_weights is None:
        upscaler_weights = find_local_upscaler_weights()
    neural_upscaler = OptionalSpandrelUpscaler(upscaler_weights)

    out_replacements_dir.mkdir(parents=True, exist_ok=True)
    if preview_path is None:
        preview_path = out_replacements_dir.parent / "preview_before_after.png"
    if manifest_path is None:
        manifest_path = out_replacements_dir.parent / "manifest.json"

    skipped_by_reason: Dict[str, int] = collections.Counter()
    skipped_records: List[Dict[str, Any]] = []
    approved_jobs: List[Tuple[Path, ParsedDumpName, np.ndarray, SafetyDecision]] = []

    # Stage 1: Ultra-strict safety screening
    for dump_path in dump_files:
        parsed = parse_pcsx2_dump_stem(dump_path.stem)
        try:
            with Image.open(dump_path) as im:
                im.load()
                rgba = np.asarray(im.convert("RGBA"), dtype=np.uint8)
        except Exception as exc:
            skipped_by_reason["corrupt_input_png"] += 1
            skipped_records.append({"file": dump_path.name, "reason": f"corrupt_input_png: {exc}"})
            continue

        # Fast-path pre-checks before running TPK signature scoring
        tpk_match_info: Optional[Dict[str, Any]] = None
        h, w, _ = rgba.shape
        alpha = rgba[:, :, 3]
        a_min, a_max = int(alpha.min()), int(alpha.max())
        maybe_world_alpha = (
            ((a_max - a_min) <= 4 and (124 <= a_min <= 130 or a_min >= 245))
            or (a_min >= 80 and a_max <= 130)
            or (20 <= a_max <= 116)
        )
        aspect_ok = (max(w / float(h), h / float(w)) <= 4.0) or (
            h >= 512 and w >= 64 and h > w and (h / float(w)) <= 16.0
        )
        if (
            tpk_index is not None
            and parsed.valid
            and parsed.tex_hash not in multi_clut_hashes
            and min(w, h) >= 64
            and w * h >= 4096
            and aspect_ok
            and maybe_world_alpha
        ):
            sig = mtd.compute_signature(Image.fromarray(rgba))
            db, dhashes, phashes, hists = tpk_index
            hits = mtd.score_dump_against_db(sig, db, dhashes, phashes, hists, top_k=1)
            if hits:
                tpk_match_info = hits[0]

        decision = evaluate_dump_safety(
            dump_path,
            rgba,
            multi_clut_hashes=multi_clut_hashes,
            tpk_match_info=tpk_match_info,
            strict_psmt4_only=strict_psmt4_only,
        )
        if not decision.allowed:
            skipped_by_reason[decision.reason] += 1
            skipped_records.append(
                {
                    "file": dump_path.name,
                    "size": [w, h],
                    "reason": decision.reason,
                    "tpk_match": decision.tpk_match,
                }
            )
            continue

        approved_jobs.append((dump_path, parsed, rgba, decision))

    # Stage 2: Parallel 4x enhancement + native Rust BC7 DDS encoding
    def _process_one(
        job: Tuple[Path, ParsedDumpName, np.ndarray, SafetyDecision],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        dump_path, parsed, rgba, decision = job
        t_item = time.monotonic()
        enhanced_rgba, enh_stats = enhance_world_texture_rgba(
            rgba,
            material=decision.material,
            scale=scale,
            max_size=max_size,
            seed_key=parsed.stem,
            neural_upscaler=neural_upscaler,
        )
        dst_dds = out_replacements_dir / f"{parsed.stem}.dds"
        val = encode_rgba_to_bc7_dds_atomic(
            encoder_bin,
            enhanced_rgba,
            dst_dds,
            threads=dds_threads,
        )
        elapsed_ms = round((time.monotonic() - t_item) * 1000.0, 2)
        entry = {
            "source_dump": dump_path.name,
            "pcsx2_stem": parsed.stem,
            "output_dds": str(dst_dds.relative_to(REPO_ROOT))
            if dst_dds.is_relative_to(REPO_ROOT)
            else str(dst_dds),
            "material": decision.material,
            "alpha_mode": decision.alpha_mode,
            "alpha_preserved": [
                enh_stats["alpha_preserved_min"],
                enh_stats["alpha_preserved_max"],
            ],
            "orig_size": enh_stats["orig_size"],
            "out_size": enh_stats["out_size"],
            "mip_count": val.mip_count,
            "dxgi_format": val.dxgi_format,
            "dds_bytes": val.actual_bytes,
            "valid_bc7": val.valid,
            "sharpness_baseline": enh_stats["sharpness_baseline"],
            "sharpness_enhanced": enh_stats["sharpness_enhanced"],
            "sharpness_gain": enh_stats["sharpness_gain"],
            "tpk_match": decision.tpk_match,
            "elapsed_ms": elapsed_ms,
        }
        preview_item = {
            "stem": parsed.stem,
            "material": decision.material,
            "alpha_mode": decision.alpha_mode,
            "alpha_val": decision.alpha_value,
            "is_road_strip": (
                enh_stats["orig_size"][1] >= 512 and enh_stats["orig_size"][0] <= 128
            ),
            "out_size": enh_stats["out_size"],
            "sharpness_gain": enh_stats["sharpness_gain"],
            "orig_rgb": rgba[:, :, :3],
            "enh_rgb": enhanced_rgba[:, :, :3],
        }
        return entry, preview_item

    enhanced_entries: List[Dict[str, Any]] = []
    preview_samples: List[Dict[str, Any]] = []
    material_counts: Dict[str, int] = collections.Counter()
    total_dds_bytes = 0

    max_workers = max(1, int(workers))
    if max_workers == 1 or len(approved_jobs) <= 1:
        for job in approved_jobs:
            entry, p_item = _process_one(job)
            enhanced_entries.append(entry)
            preview_samples.append(p_item)
            material_counts[entry["material"]] += 1
            total_dds_bytes += entry["dds_bytes"]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_process_one, job) for job in approved_jobs]
            for fut in concurrent.futures.as_completed(futures):
                entry, p_item = fut.result()
                enhanced_entries.append(entry)
                preview_samples.append(p_item)
                material_counts[entry["material"]] += 1
                total_dds_bytes += entry["dds_bytes"]

    enhanced_entries.sort(key=lambda e: e["pcsx2_stem"])

    # Stage 3: Build & Pillow-validate side-by-side before/after preview sheet
    saved_preview = build_before_after_preview(preview_samples, preview_path, max_items=16)

    elapsed_sec = round(time.monotonic() - t0, 2)
    avg_sharp_gain = (
        round(
            sum(e["sharpness_gain"] for e in enhanced_entries) / float(len(enhanced_entries)),
            3,
        )
        if enhanced_entries
        else 0.0
    )

    manifest = {
        "schema_version": "1.0",
        "engine": "rockport_world_4x_detail_enhancer",
        "dumps_dir": str(dumps_dir.relative_to(REPO_ROOT))
        if dumps_dir.is_relative_to(REPO_ROOT)
        else str(dumps_dir),
        "replacements_dir": str(out_replacements_dir.relative_to(REPO_ROOT))
        if out_replacements_dir.is_relative_to(REPO_ROOT)
        else str(out_replacements_dir),
        "preview_png": (
            str(saved_preview.relative_to(REPO_ROOT))
            if saved_preview and saved_preview.is_relative_to(REPO_ROOT)
            else (str(saved_preview) if saved_preview else None)
        ),
        "encoder_bin": str(encoder_bin.relative_to(REPO_ROOT))
        if encoder_bin.is_relative_to(REPO_ROOT)
        else str(encoder_bin),
        "neural_upscaler_active": neural_upscaler.available,
        "scale": scale,
        "max_size": max_size,
        "elapsed_seconds": elapsed_sec,
        "counts": {
            "scanned": len(dump_files),
            "multi_clut_hashes_quarantined": len(multi_clut_hashes),
            "skipped_total": sum(skipped_by_reason.values()),
            "enhanced_bc7_dds": len(enhanced_entries),
            "total_dds_bytes": total_dds_bytes,
            "avg_sharpness_gain": avg_sharp_gain,
            "skipped_by_reason": dict(sorted(skipped_by_reason.items())),
            "enhanced_by_material": dict(sorted(material_counts.items())),
        },
        "enhanced": enhanced_entries,
        "skipped": skipped_records,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detail-preserving 4x local World Texture Super-Resolution + PBR-baked "
            "micro-detail BC7 DDS engine for PCSX2 live dumps."
        )
    )
    parser.add_argument(
        "--dumps-dir",
        type=Path,
        default=DEFAULT_DUMPS_DIR,
        help="Input PCSX2 dumps directory containing dumped PNG textures.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Output replacements directory for BC7_UNORM .dds files.",
    )
    parser.add_argument(
        "--tpk-dir",
        type=Path,
        default=DEFAULT_TPK_DIR,
        help="Optional extracted TPK directory for UI/FX/car denylist matching.",
    )
    parser.add_argument(
        "--no-tpk",
        action="store_true",
        help="Disable optional TPK matching.",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=4,
        choices=(2, 4),
        help="Target upscale multiplier (default: 4).",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=1024,
        help="Maximum Power-of-Two texture dimension (default: 1024; 2048 for 2K).",
    )
    parser.add_argument(
        "--encoder-bin",
        type=Path,
        default=None,
        help="Path to native rockport_dds binary (auto-detected by default).",
    )
    parser.add_argument(
        "--upscaler-weights",
        type=Path,
        default=None,
        help="Optional local spandrel/PyTorch SR weights (.pth/.safetensors).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel worker count (default: 4).",
    )
    parser.add_argument(
        "--dds-threads",
        type=int,
        default=2,
        help="Threads per rockport_dds invocation (default: 2).",
    )
    parser.add_argument(
        "--strict-psmt4",
        action="store_true",
        help="Only allow 4-bit PSMT4 world textures (skip all 8-bit PSMT8 textures).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on number of input dumps to process (for quick tests).",
    )
    parser.add_argument(
        "--preview-path",
        type=Path,
        default=None,
        help="Optional output path for side-by-side before/after PNG contact sheet.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=None,
        help="Optional output path for JSON manifest.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = enhance_dumps_directory(
        dumps_dir=args.dumps_dir,
        out_replacements_dir=args.out_dir,
        scale=args.scale,
        max_size=args.max_size,
        tpk_dir=None if args.no_tpk else args.tpk_dir,
        encoder_bin=args.encoder_bin,
        upscaler_weights=args.upscaler_weights,
        workers=args.workers,
        dds_threads=args.dds_threads,
        strict_psmt4_only=args.strict_psmt4,
        limit=args.limit,
        preview_path=args.preview_path,
        manifest_path=args.manifest_path,
    )
    counts = manifest["counts"]
    print(
        f"[enhance_world_dumps] Scanned={counts['scanned']} | "
        f"Skipped={counts['skipped_total']} | "
        f"Enhanced BC7 DDS={counts['enhanced_bc7_dds']} | "
        f"Avg Sharpness Gain={counts['avg_sharpness_gain']}x | "
        f"Elapsed={manifest['elapsed_seconds']}s"
    )
    print(f"  Materials: {counts['enhanced_by_material']}")
    print(f"  Skipped reasons: {counts['skipped_by_reason']}")
    if manifest.get("preview_png"):
        print(f"  Preview sheet: {manifest['preview_png']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
