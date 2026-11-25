#!/usr/bin/env python3
"""
build_4k_texture_pack.py — Procedural CC0 4K Motorsport Texture Generator &
Native Apple Silicon arm64 BC7 DDS Pack Builder for NFSMW: Rockport 2026.

Workflow:
  1. Generates original CC0 high-resolution (2048px / 4096px) motorsport & car
     detail textures inside `build/dds_wave3/generated_textures/` according to
     `assets_cc0/car_livery_rules.json`:
       - Drilled carbon-ceramic brake rotor + caliper accent
       - Performance semi-slick tire tread + original Rockport sidewall stencil
       - 2x2 twill carbon fiber weave
       - Silver / royal-blue metallic clearcoat flake motorsport livery base
       - Radial brushed forged alloy wheel finish
       - Track asphalt detail & concrete pit-wall detail
  2. Optionally ingests existing CC0 baked world surfaces (`build/cc0_baked/`)
     and Motorsport 2026 HUD assets (`build/hud_pack/motorsport2026/`).
  3. Encodes all assets into real `DXGI_FORMAT_BC7_UNORM` (98) `.dds` files with
     full mipmap chains inside `build/dds_wave3/pcsx2_dds_pack/` using the
     compiled native `rockport_dds` encoder (`build/dds_wave3/target/release/rockport_dds`).
  4. Validates every produced `.dds` file (`DDS ` magic, DX10 `dxgiFormat == 98`,
     `mipMapCount > 1`, and exact expected BC7 mipmap payload byte size).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"Pillow (PIL) is required to run build_4k_texture_pack.py: {exc}")

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from install_macos_texture_tools import build_rockport_dds, find_rockport_dds  # noqa: E402

DEFAULT_RULES_PATH = REPO_ROOT / "assets_cc0" / "car_livery_rules.json"
DEFAULT_GENERATED_DIR = REPO_ROOT / "build" / "dds_wave3" / "generated_textures"
DEFAULT_DDS_OUT_DIR = REPO_ROOT / "build" / "dds_wave3" / "pcsx2_dds_pack"
DEFAULT_MANIFEST_PATH = REPO_ROOT / "build" / "dds_wave3" / "pack_manifest.json"
DEFAULT_CC0_BAKED_DIR = REPO_ROOT / "build" / "cc0_baked"
DEFAULT_HUD_PACK_DIR = REPO_ROOT / "build" / "hud_pack" / "motorsport2026"
DEFAULT_STAGED_MANIFEST = REPO_ROOT / "build" / "staged_replacements" / "deploy_manifest.json"

DXGI_FORMAT_BC7_UNORM = 98
DXGI_FORMAT_BC7_UNORM_SRGB = 99


@dataclasses.dataclass(frozen=True)
class DdsValidationResult:
    path: str
    width: int
    height: int
    mip_count: int
    dxgi_format: int
    actual_bytes: int
    expected_bytes: int
    valid: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def full_mip_count(width: int, height: int) -> int:
    m = max(1, width, height)
    count = 1
    while m > 1:
        m >>= 1
        count += 1
    return count


def expected_bc7_payload_bytes(width: int, height: int, mip_count: int) -> int:
    total = 0
    w = max(1, width)
    h = max(1, height)
    for _ in range(mip_count):
        bx = max(1, (w + 3) // 4)
        by = max(1, (h + 3) // 4)
        total += bx * by * 16
        w = max(1, w // 2)
        h = max(1, h // 2)
    return total


def validate_bc7_dds(dds_path: Path, require_srgb: Optional[bool] = None) -> DdsValidationResult:
    """Strictly validate a DX10 BC7 DDS file header, mip count, and exact byte size."""
    if not dds_path.is_file():
        return DdsValidationResult(
            path=str(dds_path),
            width=0,
            height=0,
            mip_count=0,
            dxgi_format=0,
            actual_bytes=0,
            expected_bytes=0,
            valid=False,
            error="File does not exist",
        )

    actual_bytes = dds_path.stat().st_size
    if actual_bytes < 148:
        return DdsValidationResult(
            path=str(dds_path),
            width=0,
            height=0,
            mip_count=0,
            dxgi_format=0,
            actual_bytes=actual_bytes,
            expected_bytes=148,
            valid=False,
            error=f"File smaller than 148-byte DX10 DDS header ({actual_bytes} bytes)",
        )

    with dds_path.open("rb") as fh:
        hdr = fh.read(148)

    if hdr[0:4] != b"DDS ":
        return DdsValidationResult(
            path=str(dds_path),
            width=0,
            height=0,
            mip_count=0,
            dxgi_format=0,
            actual_bytes=actual_bytes,
            expected_bytes=0,
            valid=False,
            error=f"Invalid DDS magic: {hdr[0:4]!r}",
        )

    dw_size = struct.unpack_from("<I", hdr, 4)[0]
    height = struct.unpack_from("<I", hdr, 12)[0]
    width = struct.unpack_from("<I", hdr, 16)[0]
    mip_count = struct.unpack_from("<I", hdr, 28)[0]
    pf_size = struct.unpack_from("<I", hdr, 76)[0]
    fourcc = hdr[84:88]
    dxgi_format, resource_dim, _misc_flag, array_size, _misc2 = struct.unpack_from("<5I", hdr, 128)

    expected_bytes = 148 + expected_bc7_payload_bytes(width, height, mip_count)
    allowed_formats = (
        {DXGI_FORMAT_BC7_UNORM_SRGB}
        if require_srgb is True
        else ({DXGI_FORMAT_BC7_UNORM} if require_srgb is False else {DXGI_FORMAT_BC7_UNORM, DXGI_FORMAT_BC7_UNORM_SRGB})
    )

    err: Optional[str] = None
    if dw_size != 124:
        err = f"Invalid DDS_HEADER dwSize={dw_size} (expected 124)"
    elif pf_size != 32 or fourcc != b"DX10":
        err = f"Expected DX10 pixel format (pf_size={pf_size}, fourcc={fourcc!r})"
    elif dxgi_format not in allowed_formats:
        err = f"Unexpected dxgiFormat={dxgi_format} (expected {sorted(allowed_formats)})"
    elif resource_dim != 3 or array_size != 1:
        err = f"Unexpected DX10 resourceDimension={resource_dim}, arraySize={array_size}"
    elif (width > 1 or height > 1) and mip_count <= 1:
        err = f"Expected mipMapCount > 1 for {width}x{height}, got {mip_count}"
    elif actual_bytes != expected_bytes:
        err = f"Byte size mismatch: actual={actual_bytes}, expected={expected_bytes}"

    return DdsValidationResult(
        path=str(dds_path),
        width=width,
        height=height,
        mip_count=mip_count,
        dxgi_format=dxgi_format,
        actual_bytes=actual_bytes,
        expected_bytes=expected_bytes,
        valid=err is None,
        error=err,
    )


# ---------------------------------------------------------------------------
# Procedural CC0 4K / 2K Motorsport Texture Generators
# ---------------------------------------------------------------------------


def _next_pot_clamped(val: int, min_dim: int = 4, max_dim: int = 4096) -> int:
    val = max(min_dim, min(max_dim, val))
    p = 1
    while p * 2 <= val:
        p *= 2
    if abs(val - p) <= abs(p * 2 - val):
        return max(min_dim, p)
    return min(max_dim, p * 2)


def _hash_noise_2d(h: int, w: int, seed: int) -> Any:
    rng = np.random.default_rng(seed)
    return rng.random((h, w), dtype=np.float32)


def generate_brake_rotor(size: int) -> Image.Image:
    """Drilled & slotted carbon-ceramic brake rotor with anodized hat + caliper accent."""
    if np is None:
        img = Image.new("RGBA", (size, size), (42, 44, 48, 255))
        draw = ImageDraw.Draw(img)
        c = size // 2
        draw.ellipse((int(size * 0.06), int(size * 0.06), int(size * 0.94), int(size * 0.94)), fill=(120, 124, 130, 255))
        draw.ellipse((int(size * 0.28), int(size * 0.28), int(size * 0.72), int(size * 0.72)), fill=(32, 36, 44, 255))
        return img

    coords = np.linspace(-1.0, 1.0, size, dtype=np.float32)
    yy, xx = np.meshgrid(coords, coords, indexing="ij")
    r = np.sqrt(xx * xx + yy * yy)
    theta = np.arctan2(yy, xx)

    rgba = np.zeros((size, size, 4), dtype=np.float32)
    # Dark outer background (transparent around rotor outer edge for clean wheel cutout)
    rgba[..., 0] = 24.0
    rgba[..., 1] = 26.0
    rgba[..., 2] = 30.0
    rgba[..., 3] = 255.0

    # Carbon-ceramic friction ring (r in 0.40 .. 0.92)
    in_ring = (r >= 0.40) & (r <= 0.92)
    radial_grooves = 0.5 + 0.5 * np.sin(r * 420.0)
    micro_grain = _hash_noise_2d(size, size, 20261) * 14.0
    ceramic_base = 88.0 + 18.0 * radial_grooves + micro_grain + 12.0 * np.cos(theta * 2.0 + 0.4)
    # Subtle bronze/ceramic warmth
    rgba[in_ring, 0] = ceramic_base[in_ring] * 1.04
    rgba[in_ring, 1] = ceramic_base[in_ring] * 1.00
    rgba[in_ring, 2] = ceramic_base[in_ring] * 0.94

    # Cross-drilled cooling holes (16 radial spokes x 4 staggered rings)
    hole_mask = np.zeros((size, size), dtype=bool)
    for ring_idx, r_hole in enumerate((0.50, 0.61, 0.72, 0.83)):
        n_holes = 16
        ang_offset = ring_idx * (math.pi / n_holes) * 0.55
        for k in range(n_holes):
            ang = (2.0 * math.pi * k) / n_holes + ang_offset
            hx = r_hole * math.cos(ang)
            hy = r_hole * math.sin(ang)
            dist2 = (xx - hx) ** 2 + (yy - hy) ** 2
            hole_mask |= dist2 <= (0.022 ** 2)

    # Directional curved cooling slots (8 slots)
    slot_mask = np.zeros((size, size), dtype=bool)
    for k in range(8):
        ang0 = (2.0 * math.pi * k) / 8.0
        d_ang = ((theta - ang0 - (r - 0.45) * 0.65 + math.pi) % (2.0 * math.pi)) - math.pi
        slot_mask |= (np.abs(d_ang) * np.maximum(r, 0.2) < 0.011) & (r >= 0.45) & (r <= 0.88)

    vent_mask = in_ring & (hole_mask | slot_mask)
    rgba[vent_mask, 0] = 16.0
    rgba[vent_mask, 1] = 18.0
    rgba[vent_mask, 2] = 22.0

    # Anodized dark-gunmetal center hat (r < 0.40) + 5-lug hardware
    in_hat = r < 0.40
    hat_bevel = 48.0 + 22.0 * np.clip((0.40 - r) / 0.05, 0.0, 1.0)
    rgba[in_hat, 0] = hat_bevel[in_hat] * 0.92
    rgba[in_hat, 1] = hat_bevel[in_hat] * 0.96
    rgba[in_hat, 2] = hat_bevel[in_hat] * 1.08

    # Outer rotor rim cooling fin edge (0.92..0.95) + brembo-style motorsport caliper accent arc (top-right quadrant)
    in_rim = (r > 0.92) & (r <= 0.95)
    rgba[in_rim, 0:3] = 56.0
    caliper_arc = (r >= 0.64) & (r <= 0.97) & (theta >= -0.95) & (theta <= 0.25)
    caliper_highlight = 0.85 + 0.15 * np.cos((theta + 0.35) * 3.0)
    rgba[caliper_arc, 0] = 208.0 * caliper_highlight[caliper_arc]
    rgba[caliper_arc, 1] = 36.0 * caliper_highlight[caliper_arc]
    rgba[caliper_arc, 2] = 32.0 * caliper_highlight[caliper_arc]

    # 5-lug bolts on hat
    for k in range(5):
        ang = (2.0 * math.pi * k) / 5.0
        lx = 0.24 * math.cos(ang)
        ly = 0.24 * math.sin(ang)
        lug = ((xx - lx) ** 2 + (yy - ly) ** 2) <= (0.036 ** 2)
        rgba[lug, 0:3] = 192.0

    # Center bore
    bore = r <= 0.11
    rgba[bore, 0:3] = 14.0

    # Alpha mask: crisp circle alpha around outer edge
    rgba[r > 0.965, 3] = 0.0
    return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8))


def generate_tire_tread_sidewall(size: int) -> Image.Image:
    """Performance semi-slick tire tread (top/left) + original motorsport sidewall stencil."""
    if np is None:
        return Image.new("RGBA", (size, size), (28, 30, 34, 255))

    u = np.linspace(0.0, 1.0, size, dtype=np.float32)
    v = np.linspace(0.0, 1.0, size, dtype=np.float32)
    vv, uu = np.meshgrid(v, u, indexing="ij")

    grain = _hash_noise_2d(size, size, 20262) * 10.0
    rgba = np.zeros((size, size, 4), dtype=np.float32)
    rgba[..., 0] = 30.0 + grain
    rgba[..., 1] = 32.0 + grain
    rgba[..., 2] = 35.0 + grain
    rgba[..., 3] = 255.0

    # Top 68% = asymmetric semi-slick tread blocks; Bottom 32% = sidewall strip
    is_tread = vv < 0.68
    # Two longitudinal water-evacuation channels
    long_groove = is_tread & (
        (np.abs(uu - 0.34) < 0.028) | (np.abs(uu - 0.66) < 0.028)
    )
    # Angled semi-slick sipes repeating 24 times vertically
    phase = (vv * 24.0) % 1.0
    sipe_left = is_tread & (uu < 0.32) & (np.abs(phase - (0.35 + uu * 0.8)) < 0.09)
    sipe_right = is_tread & (uu > 0.68) & (np.abs(phase - (0.35 + (1.0 - uu) * 0.8)) < 0.09)
    center_rib_sipe = (
        is_tread
        & (uu >= 0.37)
        & (uu <= 0.63)
        & (np.abs(((vv * 12.0) % 1.0) - 0.5) < 0.05)
        & (np.abs(uu - 0.50) > 0.04)
    )
    grooves = long_groove | sipe_left | sipe_right | center_rib_sipe
    rgba[grooves, 0:3] = 14.0 + grain[grooves, None] * 0.4

    # Raised tread block highlight
    blocks = is_tread & (~grooves)
    rgba[blocks, 0:3] += 8.0

    # Sidewall seam & subtle rim-protector rib
    rib = (vv >= 0.68) & (vv <= 0.72)
    rgba[rib, 0:3] = 44.0
    sidewall_ring = np.abs(vv - 0.84) < 0.008
    rgba[sidewall_ring, 0:3] = 68.0

    img = Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    label = "ROCKPORT MOTORSPORT 2026  //  SEMI-SLICK 265/35ZR19  //  TREADWEAR 180"
    y_txt = int(size * 0.78)
    for x_frac in (0.06, 0.54):
        draw.text((int(size * x_frac), y_txt), label, fill=(212, 218, 228, 255), font=font)
    return img


def generate_carbon_fiber_twill(size: int) -> Image.Image:
    """High-resolution 2x2 twill aerospace carbon fiber weave."""
    if np is None:
        return Image.new("RGBA", (size, size), (26, 28, 32, 255))

    cell = max(4, size // 64)
    coords = np.arange(size, dtype=np.int32)
    yy, xx = np.meshgrid(coords, coords, indexing="ij")
    tx = (xx // cell) % 4
    ty = (yy // cell) % 4
    # 2x2 twill pattern: diagonal shift by 1 every row
    warp = ((tx + ty) % 4) < 2

    # Fine filament micro-shading within each tow
    fx = (xx % cell).astype(np.float32) / float(cell)
    fy = (yy % cell).astype(np.float32) / float(cell)
    warp_shade = np.sin(fx * math.pi) * (0.85 + 0.15 * np.sin(fy * math.pi * 6.0))
    weft_shade = np.sin(fy * math.pi) * (0.85 + 0.15 * np.sin(fx * math.pi * 6.0))

    lum = np.where(warp, 18.0 + 38.0 * warp_shade, 12.0 + 24.0 * weft_shade)
    lum += _hash_noise_2d(size, size, 20263) * 3.0

    rgba = np.zeros((size, size, 4), dtype=np.float32)
    rgba[..., 0] = lum * 0.96
    rgba[..., 1] = lum * 0.99
    rgba[..., 2] = lum * 1.06
    rgba[..., 3] = 255.0
    return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8))


def generate_metallic_livery(size: int) -> Image.Image:
    """Original silver + royal-blue metallic clearcoat flake motorsport livery base."""
    if np is None:
        return Image.new("RGBA", (size, size), (195, 204, 218, 255))

    u = np.linspace(0.0, 1.0, size, dtype=np.float32)
    v = np.linspace(0.0, 1.0, size, dtype=np.float32)
    vv, uu = np.meshgrid(v, u, indexing="ij")

    # Fine metallic aluminum flake sparkle
    flake = (_hash_noise_2d(size, size, 20264) - 0.5) * 20.0
    sheen = 198.0 + 24.0 * np.sin(vv * math.pi) + flake

    rgba = np.zeros((size, size, 4), dtype=np.float32)
    rgba[..., 0] = sheen * 0.95
    rgba[..., 1] = sheen * 0.98
    rgba[..., 2] = sheen * 1.03
    rgba[..., 3] = 255.0

    # Diagonal royal-blue & midnight-navy geometric motorsport livery bands
    diag = uu * 0.78 + vv * 0.55
    royal_band = ((diag >= 0.42) & (diag <= 0.62)) | ((uu >= 0.12) & (uu <= 0.19) & (vv > 0.25))
    accent_pin = (np.abs(diag - 0.412) < 0.006) | (np.abs(diag - 0.628) < 0.006)
    dark_slash = (diag >= 0.64) & (diag <= 0.69)

    rgba[royal_band, 0] = 24.0 + flake[royal_band] * 0.55
    rgba[royal_band, 1] = 74.0 + flake[royal_band] * 0.65
    rgba[royal_band, 2] = 178.0 + flake[royal_band] * 0.85

    rgba[dark_slash, 0] = 16.0 + flake[dark_slash] * 0.35
    rgba[dark_slash, 1] = 32.0 + flake[dark_slash] * 0.45
    rgba[dark_slash, 2] = 72.0 + flake[dark_slash] * 0.65

    rgba[accent_pin, 0] = 236.0
    rgba[accent_pin, 1] = 242.0
    rgba[accent_pin, 2] = 250.0

    return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8))


def generate_alloy_wheel_brushed(size: int) -> Image.Image:
    """Radial lathe-brushed forged motorsport alloy wheel finish."""
    if np is None:
        return Image.new("RGBA", (size, size), (176, 182, 192, 255))

    coords = np.linspace(-1.0, 1.0, size, dtype=np.float32)
    yy, xx = np.meshgrid(coords, coords, indexing="ij")
    r = np.sqrt(xx * xx + yy * yy)
    theta = np.arctan2(yy, xx)

    brushed = 164.0 + 20.0 * np.sin(r * 520.0) + 26.0 * np.cos(theta * 2.0 + 0.7)
    brushed += _hash_noise_2d(size, size, 20265) * 10.0

    rgba = np.zeros((size, size, 4), dtype=np.float32)
    rgba[..., 0] = brushed * 0.96
    rgba[..., 1] = brushed * 0.98
    rgba[..., 2] = brushed * 1.03
    rgba[..., 3] = 255.0

    # Machined outer lip highlight & inner barrel shadow
    lip = (r >= 0.86) & (r <= 0.96)
    rgba[lip, 0:3] += 28.0
    barrel = r < 0.24
    rgba[barrel, 0:3] *= 0.45

    return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8))


def generate_track_asphalt_detail(size: int) -> Image.Image:
    """Multi-scale aggregate asphalt texture with subtle racing-line rubber wear."""
    if np is None:
        return Image.new("RGBA", (size, size), (54, 56, 60, 255))

    u = np.linspace(0.0, 1.0, size, dtype=np.float32)
    v = np.linspace(0.0, 1.0, size, dtype=np.float32)
    _vv, uu = np.meshgrid(v, u, indexing="ij")

    fine = _hash_noise_2d(size, size, 20266)
    # Coarse speckle via block-averaged noise
    coarse_n = max(16, size // 8)
    coarse_small = _hash_noise_2d(coarse_n, coarse_n, 20267)
    coarse = np.repeat(np.repeat(coarse_small, size // coarse_n, axis=0), size // coarse_n, axis=1)

    racing_line = 1.0 - 0.16 * np.exp(-((uu - 0.5) ** 2) / 0.04)
    base = (44.0 + fine * 28.0 + coarse * 16.0) * racing_line

    rgba = np.zeros((size, size, 4), dtype=np.float32)
    rgba[..., 0] = base * 0.99
    rgba[..., 1] = base * 1.00
    rgba[..., 2] = base * 1.03
    rgba[..., 3] = 255.0
    return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8))


def generate_concrete_pitwall(size: int) -> Image.Image:
    """Poured pit-wall concrete with expansion seams and subtle apex curb trim."""
    if np is None:
        return Image.new("RGBA", (size, size), (146, 148, 144, 255))

    u = np.linspace(0.0, 1.0, size, dtype=np.float32)
    v = np.linspace(0.0, 1.0, size, dtype=np.float32)
    vv, uu = np.meshgrid(v, u, indexing="ij")

    fine = _hash_noise_2d(size, size, 20268)
    streak = np.sin(uu * math.pi * 12.0) * 4.0 + np.cos(vv * math.pi * 6.0) * 5.0
    base = 138.0 + fine * 22.0 + streak

    rgba = np.zeros((size, size, 4), dtype=np.float32)
    rgba[..., 0] = base * 1.01
    rgba[..., 1] = base * 1.00
    rgba[..., 2] = base * 0.97
    rgba[..., 3] = 255.0

    # Vertical expansion joints
    seam = (np.abs(((uu * 4.0) % 1.0) - 0.02) < 0.008) | (np.abs(vv - 0.5) < 0.006)
    rgba[seam, 0:3] *= 0.55

    # Bottom motorsport red/white apex curb strip (bottom 8%)
    curb = vv >= 0.92
    red_block = curb & (((uu * 8.0).astype(np.int32) % 2) == 0)
    white_block = curb & (~red_block)
    rgba[red_block, 0] = 198.0
    rgba[red_block, 1] = 36.0
    rgba[red_block, 2] = 32.0
    rgba[white_block, 0:3] = 224.0

    return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8))


PROCEDURAL_GENERATORS = {
    "brake_rotor_4k.png": generate_brake_rotor,
    "tire_tread_sidewall_4k.png": generate_tire_tread_sidewall,
    "carbon_fiber_twill_4k.png": generate_carbon_fiber_twill,
    "m3_gtr_metallic_livery_4k.png": generate_metallic_livery,
    "alloy_wheel_brushed_4k.png": generate_alloy_wheel_brushed,
    "track_asphalt_detail_4k.png": generate_track_asphalt_detail,
    "concrete_pitwall_4k.png": generate_concrete_pitwall,
}


def generate_procedural_textures(out_dir: Path, size: int = 2048) -> List[Path]:
    """Generate all procedural CC0 motorsport & car detail PNGs inside `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    generated: List[Path] = []
    for filename, fn in PROCEDURAL_GENERATORS.items():
        img = fn(size)
        dst = out_dir / filename
        img.save(dst, format="PNG")
        generated.append(dst)
    return generated


# ---------------------------------------------------------------------------
# BC7 DDS Encoding via Native `rockport_dds`
# ---------------------------------------------------------------------------


def _write_pam_rgba(img: Image.Image, pam_path: Path) -> Tuple[int, int]:
    """Normalize image to Power-of-Two RGBA and write fast uncompressed P7 PAM."""
    orig_w, orig_h = img.size
    pot_w = _next_pot_clamped(orig_w)
    pot_h = _next_pot_clamped(orig_h)
    work = img.convert("RGBA")
    if (orig_w, orig_h) != (pot_w, pot_h):
        work = work.resize((pot_w, pot_h), Image.Resampling.LANCZOS)

    header = (
        f"P7\nWIDTH {pot_w}\nHEIGHT {pot_h}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
    ).encode("ascii")
    with pam_path.open("wb") as fh:
        fh.write(header)
        fh.write(work.tobytes())
    return pot_w, pot_h


def encode_png_to_bc7_dds(
    encoder_bin: Path,
    src_png: Path,
    dst_dds: Path,
    *,
    srgb: bool = False,
    threads: Optional[int] = None,
) -> DdsValidationResult:
    """Encode a PNG to a BC7_UNORM (DXGI 98) DDS with full mipmaps via `rockport_dds`."""
    dst_dds.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rockport_pam_") as tmp:
        pam_path = Path(tmp) / f"{src_png.stem}.pam"
        with Image.open(src_png) as img:
            _write_pam_rgba(img, pam_path)

        cmd = [
            str(encoder_bin),
            "--input",
            str(pam_path),
            "--output",
            str(dst_dds),
            "--format",
            "bc7",
            "--mipmaps",
        ]
        if srgb:
            cmd.append("--srgb")
        if threads is not None and threads > 0:
            cmd.extend(["--threads", str(threads)])

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"rockport_dds failed for {src_png}: {proc.stderr.strip() or proc.stdout.strip()}"
            )

    res = validate_bc7_dds(dst_dds, require_srgb=srgb)
    if not res.valid:
        raise RuntimeError(f"DDS validation failed for {dst_dds}: {res.error}")
    return res


def collect_input_sources(
    generated_dir: Path,
    cc0_baked_dir: Optional[Path],
    hud_pack_dir: Optional[Path],
    *,
    include_baked: bool = True,
    include_hud: bool = True,
) -> List[Tuple[str, Path]]:
    """Collect `(group, png_path)` pairs for encoding into `pcsx2_dds_pack`."""
    items: List[Tuple[str, Path]] = []

    if generated_dir.is_dir():
        for p in sorted(generated_dir.glob("*.png")):
            items.append(("generated_car_livery", p))

    if include_baked and cc0_baked_dir and cc0_baked_dir.is_dir():
        for p in sorted(cc0_baked_dir.glob("*.png")):
            items.append(("cc0_baked", p))

    if include_hud and hud_pack_dir and hud_pack_dir.is_dir():
        for p in sorted(hud_pack_dir.glob("*.png")):
            if p.name == "hud_contact_sheet.png":
                continue
            items.append(("hud_motorsport2026", p))

    return items


def build_pack(
    *,
    size: int = 2048,
    generated_dir: Path = DEFAULT_GENERATED_DIR,
    dds_out_dir: Path = DEFAULT_DDS_OUT_DIR,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    rules_path: Path = DEFAULT_RULES_PATH,
    cc0_baked_dir: Optional[Path] = DEFAULT_CC0_BAKED_DIR,
    hud_pack_dir: Optional[Path] = DEFAULT_HUD_PACK_DIR,
    staged_manifest_path: Optional[Path] = DEFAULT_STAGED_MANIFEST,
    include_baked: bool = True,
    include_hud: bool = True,
    srgb: bool = False,
    threads: Optional[int] = None,
    build_encoder_if_missing: bool = True,
) -> Dict[str, Any]:
    """Generate procedural 2K/4K CC0 assets, ingest baked/HUD packs, encode BC7 DDS, and validate all."""
    t0 = time.monotonic()

    rules_data: Dict[str, Any] = {}
    if rules_path.is_file():
        rules_data = json.loads(rules_path.read_text(encoding="utf-8"))

    encoder_bin = find_rockport_dds()
    if encoder_bin is None:
        if not build_encoder_if_missing:
            raise RuntimeError("rockport_dds encoder not found; run install_macos_texture_tools.py --build-encoder")
        encoder_bin = build_rockport_dds()

    generated_pngs = generate_procedural_textures(generated_dir, size=size)
    sources = collect_input_sources(
        generated_dir=generated_dir,
        cc0_baked_dir=cc0_baked_dir,
        hud_pack_dir=hud_pack_dir,
        include_baked=include_baked,
        include_hud=include_hud,
    )

    encoded_entries: List[Dict[str, Any]] = []
    total_dds_bytes = 0

    for group, src_png in sources:
        dst_dds = dds_out_dir / group / f"{src_png.stem}.dds"
        t_item = time.monotonic()
        val = encode_png_to_bc7_dds(
            encoder_bin,
            src_png,
            dst_dds,
            srgb=srgb,
            threads=threads,
        )
        elapsed_ms = round((time.monotonic() - t_item) * 1000.0, 2)
        total_dds_bytes += val.actual_bytes
        encoded_entries.append(
            {
                "group": group,
                "source_png": str(src_png.relative_to(REPO_ROOT))
                if src_png.is_relative_to(REPO_ROOT)
                else str(src_png),
                "output_dds": str(dst_dds.relative_to(REPO_ROOT))
                if dst_dds.is_relative_to(REPO_ROOT)
                else str(dst_dds),
                "width": val.width,
                "height": val.height,
                "mip_count": val.mip_count,
                "dxgi_format": val.dxgi_format,
                "bytes": val.actual_bytes,
                "valid": val.valid,
                "encode_ms": elapsed_ms,
            }
        )

    # Optional: Also mirror matched PCSX2 <TexHash>-<ClutHash>.dds files if deploy_manifest.json exists
    pcsx2_ready_entries: List[Dict[str, Any]] = []
    if staged_manifest_path and staged_manifest_path.is_file():
        staged_data = json.loads(staged_manifest_path.read_text(encoding="utf-8"))
        ready_dir = dds_out_dir / "pcsx2_ready"
        for item in staged_data.get("staged", []):
            stem = item.get("pcsx2_stem")
            src_asset_rel = item.get("source_asset")
            staged_png_rel = item.get("staged_png")
            src_candidate = None
            if staged_png_rel and (REPO_ROOT / staged_png_rel).is_file():
                src_candidate = REPO_ROOT / staged_png_rel
            elif src_asset_rel and (REPO_ROOT / src_asset_rel).is_file():
                src_candidate = REPO_ROOT / src_asset_rel
            if stem and src_candidate is not None:
                dst_ready = ready_dir / f"{stem}.dds"
                val = encode_png_to_bc7_dds(
                    encoder_bin,
                    src_candidate,
                    dst_ready,
                    srgb=srgb,
                    threads=threads,
                )
                total_dds_bytes += val.actual_bytes
                pcsx2_ready_entries.append(
                    {
                        "pcsx2_stem": stem,
                        "matched_tpk_name": item.get("matched_tpk_name"),
                        "output_dds": str(dst_ready.relative_to(REPO_ROOT))
                        if dst_ready.is_relative_to(REPO_ROOT)
                        else str(dst_ready),
                        "width": val.width,
                        "height": val.height,
                        "mip_count": val.mip_count,
                        "dxgi_format": val.dxgi_format,
                        "bytes": val.actual_bytes,
                        "valid": val.valid,
                    }
                )

    report = {
        "schema_version": "1.0",
        "project": rules_data.get("project", "NFSMW: Rockport 2026"),
        "encoder_bin": str(encoder_bin.relative_to(REPO_ROOT))
        if encoder_bin.is_relative_to(REPO_ROOT)
        else str(encoder_bin),
        "procedural_resolution": size,
        "generated_count": len(generated_pngs),
        "encoded_count": len(encoded_entries),
        "pcsx2_ready_count": len(pcsx2_ready_entries),
        "all_valid": all(e["valid"] for e in encoded_entries)
        and all(e["valid"] for e in pcsx2_ready_entries),
        "total_dds_bytes": total_dds_bytes,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        "rules_count": len(rules_data.get("rules", [])),
        "encoded": encoded_entries,
        "pcsx2_ready": pcsx2_ready_entries,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate 2K/4K CC0 car/livery textures and encode BC7 DDS pack via rockport_dds."
    )
    parser.add_argument(
        "--size",
        type=int,
        default=2048,
        choices=(512, 1024, 2048, 4096),
        help="Procedural car/livery texture resolution (default: 2048).",
    )
    parser.add_argument(
        "--generated-dir",
        type=Path,
        default=DEFAULT_GENERATED_DIR,
        help="Output directory for procedural CC0 PNGs.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_DDS_OUT_DIR,
        help="Output directory for BC7 DDS pack.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Output JSON pack manifest path.",
    )
    parser.add_argument(
        "--no-baked",
        action="store_true",
        help="Skip ingesting build/cc0_baked/.",
    )
    parser.add_argument(
        "--no-hud",
        action="store_true",
        help="Skip ingesting build/hud_pack/motorsport2026/.",
    )
    parser.add_argument(
        "--srgb",
        action="store_true",
        help="Encode as BC7_UNORM_SRGB (DXGI 99) instead of BC7_UNORM (DXGI 98).",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Override worker thread count passed to rockport_dds.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args(argv)

    report = build_pack(
        size=args.size,
        generated_dir=args.generated_dir.resolve(),
        dds_out_dir=args.out_dir.resolve(),
        manifest_path=args.manifest.resolve(),
        include_baked=not args.no_baked,
        include_hud=not args.no_hud,
        srgb=args.srgb,
        threads=args.threads,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"NFSMW: Rockport 2026 BC7 Pack Built — "
            f"{report['generated_count']} procedural ({report['procedural_resolution']}px), "
            f"{report['encoded_count']} pack DDS + {report['pcsx2_ready_count']} PCSX2-ready DDS, "
            f"total={report['total_dds_bytes'] / (1024 * 1024):.1f} MiB, "
            f"all_valid={report['all_valid']} in {report['elapsed_seconds']}s"
        )
        print(f"Manifest: {args.manifest}")
    return 0 if report["all_valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
