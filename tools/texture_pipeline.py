#!/usr/bin/env python3
"""NFSMW: Rockport 2026 — Apple Silicon Texture Audit, Upscale, DDS & Procedural HUD CLI.

Production batch pipeline for Need for Speed: Most Wanted - Black Edition
(PS2 NTSC-U, SLUS-21351) runtime texture replacement in PCSX2 on macOS Apple Silicon:

  1. audit           Scan PCSX2 dumped textures, analyze resolution / aspect / alpha
                     usage / multi-CLUT groups, classify into remaster categories, and
                     output a structured JSON inventory.
  2. upscale         Run tiled local AI upscaling on Apple Silicon MPS via PyTorch +
                     spandrel (with PYTORCH_ENABLE_MPS_FALLBACK=1, MPS cache flushing,
                     and RGB alpha-bleed dilation to prevent dark halos on foliage/HUD).
  3. encode-dds      Normalize textures to Power-of-Two dimensions via Pillow and batch-
                     compile into BC7_UNORM DDS with full internal mipmap chains using
                     native macOS arm64 encoders (dds_tool, kram, or cuttlefish),
                     preserving exact PCSX2 <TexHash>-<ClutHash>.dds filenames.
  4. render-hud-demo Procedurally generate a 2048x2048 supersampled Forza/GT7-inspired
                     modern racing tachometer/speedometer HUD prototype using Pillow.
  5. check-env       Preflight audit of Apple Silicon MPS acceleration, installed Python
                     packages, native macOS arm64 BC7 DDS encoders (dds_tool, kram,
                     cuttlefish), and PCSX2 texture directories with exact install hints.

Legal Notice:
  Copyrighted game dumps and AI-upscaled derivatives must remain strictly local
  (never commit dumps/, upscaled/, replacements/, or *.dds to public Git repos).
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PCSX2_SERIAL = "SLUS-21351"
DEFAULT_PCSX2_BASE = Path.home() / "Library" / "Application Support" / "PCSX2" / "textures" / PCSX2_SERIAL
DEFAULT_PCSX2_DUMPS = DEFAULT_PCSX2_BASE / "dumps"
DEFAULT_PCSX2_REPLACEMENTS = DEFAULT_PCSX2_BASE / "replacements"

SUPPORTED_IMAGE_EXTS = {".png", ".tga", ".bmp", ".webp", ".tif", ".tiff"}
AUDIT_IMAGE_EXTS = SUPPORTED_IMAGE_EXTS | {".dds"}
PCSX2_HASH_RE = re.compile(
    r"^(?P<tex>[0-9a-fA-F]{8,16})(?:-(?P<part2>[0-9a-fA-F]{8,16}))?(?:-(?P<flags>[0-9a-fA-F]{8}))?$"
)


class PipelineError(RuntimeError):
    """Raised on recoverable pipeline or CLI configuration errors."""


def _require_pillow() -> Tuple[Any, Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont  # type: ignore

        return Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as exc:
        raise PipelineError(
            "Pillow (PIL) is required for this command.\n"
            "Install it with: python3 -m pip install 'pillow>=10.0.0'"
        ) from exc


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def compute_pot_dimension(value: int, mode: str = "nearest", min_dim: int = 4, max_dim: int = 4096) -> int:
    """Snap a pixel dimension to a Power-of-Two value within [min_dim, max_dim]."""
    clamped = max(min_dim, min(value, max_dim))
    if is_power_of_two(clamped):
        return clamped

    log2_val = math.log2(clamped)
    lower = max(min_dim, 1 << int(math.floor(log2_val)))
    upper = min(max_dim, 1 << int(math.ceil(log2_val)))

    if mode == "ceil" or mode == "pad":
        return upper
    if mode == "floor":
        return lower
    # Default: nearest in log space (equivalently geometric midpoint)
    return upper if (clamped - lower) >= (upper - clamped) else lower


def parse_pcsx2_filename(stem: str) -> Tuple[Optional[str], Optional[str], bool]:
    """Parse PCSX2 ``<TexHash>-<ClutHash>-<Flags>``, ``<TexHash>-<Flags>``, ``<TexHash>-<ClutHash>``, or ``<TexHash>`` stem."""
    match = PCSX2_HASH_RE.match(stem)
    if not match:
        return None, None, False
    tex = match.group("tex").lower()
    part2 = match.group("part2").lower() if match.group("part2") else None
    flags = match.group("flags").lower() if match.group("flags") else None
    if flags is not None:
        return tex, part2, True
    if part2 is not None and len(tex) >= 12 and len(part2) == 8:
        return tex, None, True
    return tex, part2, True


def iter_image_files(root: Path, extensions: set) -> List[Path]:
    if not root.exists():
        return []
    files = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in extensions and not p.name.startswith(".")
    ]
    files.sort()
    return files


# ---------------------------------------------------------------------------
# Alpha-bleed RGB dilation (prevents dark halos on foliage, fences, HUD)
# ---------------------------------------------------------------------------


def alpha_bleed_rgba_numpy(rgba: Any, alpha_threshold: int = 2, max_passes: int = 32) -> Any:
    """Dilate RGB channels outward into transparent pixels using SciPy EDT or NumPy passes.

    Args:
        rgba: HxWx4 uint8 NumPy array.
        alpha_threshold: Pixels with alpha <= threshold receive dilated RGB colors.
        max_passes: Iteration limit for pure-NumPy fallback when SciPy is unavailable.

    Returns:
        New HxWx4 uint8 NumPy array with identical alpha channel and bled RGB channels.
    """
    import numpy as np  # type: ignore

    if rgba.ndim != 3 or rgba.shape[2] != 4:
        return rgba

    alpha = rgba[:, :, 3]
    transparent_mask = alpha <= alpha_threshold
    if not np.any(transparent_mask) or np.all(transparent_mask):
        return rgba

    out = rgba.copy()

    # Fast, exact infinite-radius Voronoi nearest-opaque RGB bleed via SciPy when available.
    try:
        from scipy import ndimage  # type: ignore

        _, nearest_indices = ndimage.distance_transform_edt(transparent_mask, return_indices=True)
        out[:, :, :3] = rgba[nearest_indices[0], nearest_indices[1], :3]
        out[:, :, 3] = alpha
        return out
    except ImportError:
        pass

    # Pure-NumPy 8-neighbor iterative RGB dilation fallback (zero SciPy dependency).
    valid = ~transparent_mask
    rgb = out[:, :, :3].astype(np.float32)
    h, w = valid.shape

    neighbor_offsets = (
        (-1, 0),
        (1, 0),
        (0, -1),
        (0, 1),
        (-1, -1),
        (-1, 1),
        (1, -1),
        (1, 1),
    )

    for _ in range(max(1, max_passes)):
        unknown = ~valid
        if not np.any(unknown):
            break

        accum = np.zeros((h, w, 3), dtype=np.float32)
        count = np.zeros((h, w), dtype=np.float32)

        for dy, dx in neighbor_offsets:
            src_y0 = max(0, -dy)
            src_y1 = min(h, h - dy)
            src_x0 = max(0, -dx)
            src_x1 = min(w, w - dx)

            dst_y0 = max(0, dy)
            dst_y1 = min(h, h + dy)
            dst_x0 = max(0, dx)
            dst_x1 = min(w, w + dx)

            v_slice = valid[src_y0:src_y1, src_x0:src_x1]
            if not np.any(v_slice):
                continue

            weight = v_slice.astype(np.float32)
            accum[dst_y0:dst_y1, dst_x0:dst_x1] += rgb[src_y0:src_y1, src_x0:src_x1] * weight[:, :, None]
            count[dst_y0:dst_y1, dst_x0:dst_x1] += weight

        fill_mask = unknown & (count > 0.0)
        if not np.any(fill_mask):
            break

        rgb[fill_mask] = accum[fill_mask] / count[fill_mask, None]
        valid[fill_mask] = True

    out[:, :, :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    out[:, :, 3] = alpha
    return out


def alpha_bleed_pil_image(img: Any, alpha_threshold: int = 2, max_passes: int = 16) -> Any:
    """Apply RGB alpha-bleed dilation to a PIL Image (uses NumPy/SciPy if present, else PIL fallback)."""
    Image, _, ImageFilter, _ = _require_pillow()
    if "A" not in img.getbands():
        return img

    rgba_img = img.convert("RGBA")
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(rgba_img, dtype=np.uint8)
        bled = alpha_bleed_rgba_numpy(arr, alpha_threshold=alpha_threshold, max_passes=max_passes)
        return Image.fromarray(bled, mode="RGBA")
    except ImportError:
        pass

    # Pure-PIL fallback for environments without NumPy (used during encode-dds PoT staging)
    alpha_band = rgba_img.getchannel("A")
    extrema = alpha_band.getextrema()
    if extrema is None or extrema[0] > alpha_threshold or extrema[1] <= alpha_threshold:
        return rgba_img

    rgb_base = rgba_img.convert("RGB")
    valid_mask = alpha_band.point(lambda a: 255 if a > alpha_threshold else 0, mode="L")
    current_rgb = rgb_base.copy()
    current_valid = valid_mask.copy()

    # Iterative max-filter expansion over masked RGB without corrupting opaque pixels
    for _ in range(max(1, min(max_passes, 12))):
        if current_valid.getextrema() == (255, 255):
            break
        blurred = current_rgb.filter(ImageFilter.BoxBlur(1))
        # Preserve already-valid pixels, fill newly-reached border pixels
        expanded_valid = current_valid.filter(ImageFilter.MaxFilter(3))
        current_rgb = Image.composite(current_rgb, blurred, current_valid)
        current_valid = expanded_valid

    final_rgb = Image.composite(rgb_base, current_rgb, valid_mask)
    r, g, b = final_rgb.split()
    return Image.merge("RGBA", (r, g, b, alpha_band))


# ---------------------------------------------------------------------------
# Subcommand 1: audit
# ---------------------------------------------------------------------------


def _analyze_alpha_usage(img: Any) -> Dict[str, Any]:
    """Classify alpha usage quickly using Pillow's C-level channel histogram."""
    if "A" not in img.getbands():
        if img.mode == "P" and "transparency" in img.info:
            rgba = img.convert("RGBA")
            alpha = rgba.getchannel("A")
        else:
            return {
                "has_alpha_channel": False,
                "alpha_class": "opaque",
                "transparent_ratio": 0.0,
                "semi_transparent_ratio": 0.0,
                "min_alpha": 255,
                "max_alpha": 255,
            }
    else:
        alpha = img.getchannel("A")

    hist = alpha.histogram()
    total = sum(hist) or 1
    min_a = next((i for i, c in enumerate(hist) if c > 0), 255)
    max_a = next((i for i in range(255, -1, -1) if hist[i] > 0), 0)

    transparent_px = sum(hist[:16])  # alpha 0..15
    semi_px = sum(hist[16:240])  # alpha 16..239
    opaque_px = sum(hist[240:])  # alpha 240..255

    transparent_ratio = transparent_px / total
    semi_ratio = semi_px / total

    if max_a <= 2:
        alpha_class = "fully_transparent"
    elif min_a >= 253 or (transparent_px == 0 and semi_ratio < 0.002):
        alpha_class = "opaque"
    elif transparent_px > 0 and semi_ratio < 0.05 and opaque_px > 0:
        alpha_class = "binary_cutout"
    else:
        alpha_class = "smooth_alpha"

    return {
        "has_alpha_channel": True,
        "alpha_class": alpha_class,
        "transparent_ratio": round(transparent_ratio, 4),
        "semi_transparent_ratio": round(semi_ratio, 4),
        "min_alpha": min_a,
        "max_alpha": max_a,
    }


def _classify_texture(
    width: int,
    height: int,
    alpha_info: Dict[str, Any],
) -> Tuple[str, str]:
    """Categorize a dumped PS2 texture and recommend its remaster pipeline track."""
    aspect = width / float(max(1, height))
    min_dim = min(width, height)
    max_dim = max(width, height)
    alpha_class = alpha_info["alpha_class"]
    trans_ratio = alpha_info["transparent_ratio"]

    if alpha_class == "fully_transparent":
        return "misc_small", "skip_empty_alpha"

    if min_dim <= 16 or max_dim <= 32:
        return "misc_small", "skip_lut_or_small"

    # Wide panoramic opaque textures (e.g. 512x128, 512x256, 256x64 horizon bands)
    if alpha_class == "opaque" and aspect >= 2.0 and max_dim >= 256:
        return "skybox", "cc0_skybox_or_ai_upscale"

    # Cutout foliage, trees, bushes, chain-link fences, grilles
    if alpha_class == "binary_cutout":
        if aspect >= 2.5 and height <= 128:
            return "ui_hud", "procedural_or_vector_hud"
        return "foliage_alpha", "alpha_bleed_ai_upscale"

    # Smooth alpha: HUD elements, gauges, font atlases, decals, glass, skidmarks
    if alpha_class == "smooth_alpha":
        if aspect >= 2.0 or max_dim >= 256 or trans_ratio >= 0.35:
            return "ui_hud", "procedural_or_alpha_bleed_upscale"
        return "foliage_alpha", "alpha_bleed_ai_upscale"

    # Default opaque square/rectangular textures: roads, asphalt, buildings, vehicles
    return "world_diffuse", "ai_upscale_or_cc0_pbr"


def cmd_audit(args: argparse.Namespace) -> int:
    Image, _, _, _ = _require_pillow()
    dumps_dir = Path(args.dumps).expanduser().resolve()

    if not dumps_dir.is_dir():
        print(
            f"[info] PCSX2 dumps directory does not exist yet: {dumps_dir}\n"
            "To capture textures from NFS Most Wanted (SLUS-21351):\n"
            "  1. Open PCSX2 -> Game Properties -> Graphics -> Texture Replacement\n"
            "  2. Enable 'Dump Textures' and drive across Rockport (downtown, highway, menus)\n"
            "  3. Re-run: python3 tools/texture_pipeline.py audit --dumps "
            f"\"{dumps_dir}\" --out build/texture_audit.json",
            file=sys.stderr,
        )
        return 1

    image_paths = iter_image_files(dumps_dir, AUDIT_IMAGE_EXTS)
    if args.limit and args.limit > 0:
        image_paths = image_paths[: args.limit]

    records: List[Dict[str, Any]] = []
    category_counts: collections.Counter = collections.Counter()
    alpha_counts: collections.Counter = collections.Counter()
    resolution_counts: collections.Counter = collections.Counter()
    tex_hash_cluts: Dict[str, List[str]] = collections.defaultdict(list)
    non_pot_count = 0
    unreadable_count = 0

    for path in image_paths:
        rel_path = str(path.relative_to(dumps_dir))
        tex_hash, clut_hash, is_pcsx2_name = parse_pcsx2_filename(path.stem)

        try:
            with Image.open(path) as img:
                width, height = img.size
                mode = img.mode
                alpha_info = _analyze_alpha_usage(img)
        except Exception as exc:
            unreadable_count += 1
            records.append(
                {
                    "relative_path": rel_path,
                    "error": str(exc),
                    "category": "unreadable",
                }
            )
            continue

        pot = is_power_of_two(width) and is_power_of_two(height)
        if not pot:
            non_pot_count += 1

        category, recommended_action = _classify_texture(width, height, alpha_info)
        res_key = f"{width}x{height}"
        category_counts[category] += 1
        alpha_counts[alpha_info["alpha_class"]] += 1
        resolution_counts[res_key] += 1

        if tex_hash:
            tex_hash_cluts[tex_hash].append(clut_hash or "no_clut")

        records.append(
            {
                "relative_path": rel_path,
                "filename": path.name,
                "pcsx2_named": is_pcsx2_name,
                "tex_hash": tex_hash,
                "clut_hash": clut_hash,
                "width": width,
                "height": height,
                "aspect_ratio": round(width / float(max(1, height)), 3),
                "is_pot": pot,
                "mode": mode,
                "size_bytes": path.stat().st_size,
                **alpha_info,
                "category": category,
                "recommended_action": recommended_action,
            }
        )

    # Annotate CLUT variant group sizes (important for PS2 paletted car/HUD variants)
    multi_clut_groups = {h: sorted(set(cluts)) for h, cluts in tex_hash_cluts.items() if len(set(cluts)) > 1}
    for rec in records:
        th = rec.get("tex_hash")
        if th:
            rec["clut_variant_count"] = len(set(tex_hash_cluts.get(th, [])))

    inventory = {
        "schema_version": "1.0",
        "project": "NFSMW: Rockport 2026",
        "pcsx2_serial": PCSX2_SERIAL,
        "dumps_directory": str(dumps_dir),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "total_files": len(image_paths),
            "parsed_textures": len(records) - unreadable_count,
            "unreadable_files": unreadable_count,
            "non_pot_textures": non_pot_count,
            "unique_tex_hashes": len(tex_hash_cluts),
            "multi_clut_tex_hashes": len(multi_clut_groups),
            "categories": dict(category_counts.most_common()),
            "alpha_classes": dict(alpha_counts.most_common()),
            "top_resolutions": dict(resolution_counts.most_common(20)),
        },
        "multi_clut_groups": multi_clut_groups,
        "textures": records,
    }

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
        print(f"Texture audit complete: {len(image_paths)} file(s) scanned.")
        print(f"  Categories   : {dict(category_counts.most_common())}")
        print(f"  Alpha usage  : {dict(alpha_counts.most_common())}")
        print(f"  Non-PoT      : {non_pot_count}")
        print(f"  Multi-CLUTs  : {len(multi_clut_groups)} shared TexHash group(s)")
        print(f"  Inventory    : {out_path}")
    else:
        print(json.dumps(inventory, indent=2))

    return 0


# ---------------------------------------------------------------------------
# Subcommand 2: upscale (Apple Silicon MPS + spandrel + alpha-bleed)
# ---------------------------------------------------------------------------


def _import_upscale_stack() -> Tuple[Any, Any, Any, Any]:
    """Set PYTORCH_ENABLE_MPS_FALLBACK=1 and import torch, spandrel, numpy, tqdm."""
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    missing: List[str] = []

    try:
        import numpy as np  # type: ignore
    except ImportError:
        np = None
        missing.append("numpy")

    try:
        import torch  # type: ignore
    except ImportError:
        torch = None
        missing.append("torch")

    try:
        import spandrel  # type: ignore
    except ImportError:
        spandrel = None
        missing.append("spandrel")

    if missing:
        req_path = Path(__file__).resolve().parent / "requirements-textures.txt"
        raise PipelineError(
            f"Missing optional AI upscaling dependencies: {', '.join(missing)}\n\n"
            "Set up an isolated Apple Silicon (Python 3.12) upscaling venv:\n"
            "  uv venv --python 3.12 .venv-textures\n"
            "  source .venv-textures/bin/activate\n"
            f"  pip install -r {req_path}\n\n"
            "Then re-run with MPS fallback enabled (also set automatically by this script):\n"
            "  PYTORCH_ENABLE_MPS_FALLBACK=1 python3 tools/texture_pipeline.py upscale \\\n"
            "    --input build/dumps_curated --output build/upscaled \\\n"
            "    --model models/4x-PBRify_UpscalerDAT2.pth --tile 256 --pad 16\n\n"
            "Recommended CC0 / permissive OpenModelDB models:\n"
            "  - 4x-PBRify_UpscalerDAT2 (world asphalt, concrete, brick, vehicle surfaces)\n"
            "  - 4x-NMKD-Siax           (clean general-purpose PS2 diffuse & signage)"
        )

    try:
        from tqdm import tqdm  # type: ignore
    except ImportError:
        tqdm = None

    return torch, spandrel, np, tqdm


def _select_torch_device(torch: Any, requested: str) -> Any:
    if requested == "mps":
        if not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
            raise PipelineError("Requested device 'mps' is not available on this machine.")
        return torch.device("mps")
    if requested == "cuda":
        if not torch.cuda.is_available():
            raise PipelineError("Requested device 'cuda' is not available on this machine.")
        return torch.device("cuda")
    if requested == "cpu":
        return torch.device("cpu")

    # Auto-detect: prefer Apple Silicon MPS, then CUDA, then CPU
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def _flush_device_cache(torch: Any, device: Any) -> None:
    if device.type == "mps" and hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
        torch.mps.empty_cache()
    elif device.type == "cuda" and torch.cuda.is_available():
        torch.cuda.empty_cache()


def _infer_rgb_tiled(
    model: Any,
    rgb_uint8: Any,
    scale: int,
    tile_size: int,
    tile_pad: int,
    device: Any,
    use_fp16: bool,
    torch: Any,
    np: Any,
) -> Any:
    """Run overlap-padded tiled inference on an HxWx3 uint8 NumPy array."""
    import torch.nn.functional as F  # type: ignore

    h, w, _ = rgb_uint8.shape
    tensor = torch.from_numpy(rgb_uint8.astype(np.float32) / 255.0).permute(2, 0, 1).unsqueeze(0)

    # Fast single-pass path when image fits inside one tile
    if h <= tile_size and w <= tile_size:
        inp = tensor.to(device=device, dtype=torch.float16 if use_fp16 else torch.float32)
        with torch.inference_mode():
            out = model(inp)
        out_cpu = out.detach().float().clamp(0.0, 1.0).cpu().squeeze(0).permute(1, 2, 0).numpy()
        return np.rint(out_cpu * 255.0).astype(np.uint8)

    out_h, out_w = h * scale, w * scale
    output = torch.zeros((1, 3, out_h, out_w), dtype=torch.float32, device="cpu")

    tiles_y = math.ceil(h / float(tile_size))
    tiles_x = math.ceil(w / float(tile_size))
    dtype = torch.float16 if use_fp16 else torch.float32

    with torch.inference_mode():
        for ty in range(tiles_y):
            for tx in range(tiles_x):
                y0 = ty * tile_size
                y1 = min(y0 + tile_size, h)
                x0 = tx * tile_size
                x1 = min(x0 + tile_size, w)

                # Extract context window with reflection padding at image boundaries
                py0 = y0 - tile_pad
                py1 = y1 + tile_pad
                px0 = x0 - tile_pad
                px1 = x1 + tile_pad

                src_y0 = max(0, py0)
                src_y1 = min(h, py1)
                src_x0 = max(0, px0)
                src_x1 = min(w, px1)

                patch = tensor[:, :, src_y0:src_y1, src_x0:src_x1]
                pad_left = src_x0 - px0
                pad_right = px1 - src_x1
                pad_top = src_y0 - py0
                pad_bottom = py1 - src_y1

                if pad_left or pad_right or pad_top or pad_bottom:
                    pad_mode = (
                        "reflect"
                        if (patch.shape[2] > max(pad_top, pad_bottom) and patch.shape[3] > max(pad_left, pad_right))
                        else "replicate"
                    )
                    patch = F.pad(patch, (pad_left, pad_right, pad_top, pad_bottom), mode=pad_mode)

                patch_dev = patch.to(device=device, dtype=dtype)
                pred = model(patch_dev).detach().float().clamp(0.0, 1.0).cpu()

                # Crop exact non-overlapping tile interior
                crop_y0 = tile_pad * scale
                crop_x0 = tile_pad * scale
                crop_y1 = crop_y0 + (y1 - y0) * scale
                crop_x1 = crop_x0 + (x1 - x0) * scale

                output[:, :, y0 * scale : y1 * scale, x0 * scale : x1 * scale] = pred[
                    :, :, crop_y0:crop_y1, crop_x0:crop_x1
                ]
                del patch_dev, pred

            if device.type == "mps":
                _flush_device_cache(torch, device)

    out_np = output.squeeze(0).permute(1, 2, 0).numpy()
    return np.rint(out_np * 255.0).astype(np.uint8)


def cmd_upscale(args: argparse.Namespace) -> int:
    Image, _, _, _ = _require_pillow()
    torch, spandrel, np, tqdm = _import_upscale_stack()

    input_dir = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    model_path = Path(args.model).expanduser().resolve()

    if not input_dir.is_dir():
        raise PipelineError(f"Input directory not found: {input_dir}")
    if not model_path.is_file():
        raise PipelineError(
            f"Upscaler model file not found: {model_path}\n"
            "Download a CC0 / permissive OpenModelDB model (e.g. 4x-PBRify_UpscalerDAT2.pth "
            "or 4x-NMKD-Siax.pth) into models/ (which is .gitignored)."
        )
    if args.tile < 32:
        raise PipelineError("--tile must be at least 32 pixels.")
    if args.pad < 0 or args.pad >= args.tile:
        raise PipelineError("--pad must be >= 0 and strictly less than --tile.")

    device = _select_torch_device(torch, args.device)
    use_fp16 = bool(args.fp16 and device.type in ("mps", "cuda"))

    print(
        f"Loading upscaler model: {model_path.name} "
        f"[device={device.type}, fp16={use_fp16}, PYTORCH_ENABLE_MPS_FALLBACK={os.environ.get('PYTORCH_ENABLE_MPS_FALLBACK')}]"
    )
    descriptor = spandrel.ModelLoader(device=device).load_from_file(model_path)
    if not isinstance(descriptor, spandrel.ImageModelDescriptor):
        raise PipelineError(f"Loaded model '{model_path.name}' is not a spandrel ImageModelDescriptor.")

    model = descriptor.model.eval()
    if use_fp16:
        model = model.half()
    scale = int(getattr(descriptor, "scale", 4))
    print(f"Model architecture: {descriptor.architecture.name} (scale={scale}x)")

    files = iter_image_files(input_dir, SUPPORTED_IMAGE_EXTS)
    if not files:
        print(f"No supported textures found in {input_dir}.")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    iterator = tqdm(files, desc="Upscaling", unit="tex") if tqdm is not None else files

    processed = 0
    skipped = 0
    bled_count = 0

    for idx, src_path in enumerate(iterator, start=1):
        rel_path = src_path.relative_to(input_dir)
        dst_path = (output_dir / rel_path).with_suffix(".png")
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        if dst_path.is_file() and not args.overwrite:
            skipped += 1
            continue

        with Image.open(src_path) as img:
            has_alpha = "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info)
            if has_alpha:
                rgba_np = np.asarray(img.convert("RGBA"), dtype=np.uint8)
                alpha_channel = rgba_np[:, :, 3]
                has_transparency = bool(np.any(alpha_channel < 254))
                if has_transparency and args.alpha_bleed_passes > 0:
                    rgba_np = alpha_bleed_rgba_numpy(
                        rgba_np,
                        alpha_threshold=2,
                        max_passes=args.alpha_bleed_passes,
                    )
                    bled_count += 1
                rgb_in = rgba_np[:, :, :3]
            else:
                rgba_np = None
                has_transparency = False
                rgb_in = np.asarray(img.convert("RGB"), dtype=np.uint8)

        rgb_up = _infer_rgb_tiled(
            model=model,
            rgb_uint8=rgb_in,
            scale=scale,
            tile_size=args.tile,
            tile_pad=args.pad,
            device=device,
            use_fp16=use_fp16,
            torch=torch,
            np=np,
        )

        if has_alpha and rgba_np is not None:
            out_h, out_w = rgb_up.shape[:2]
            if args.alpha_mode == "model" and has_transparency:
                alpha_rgb = np.repeat(rgba_np[:, :, 3:4], 3, axis=2)
                alpha_up_rgb = _infer_rgb_tiled(
                    model=model,
                    rgb_uint8=alpha_rgb,
                    scale=scale,
                    tile_size=args.tile,
                    tile_pad=args.pad,
                    device=device,
                    use_fp16=use_fp16,
                    torch=torch,
                    np=np,
                )
                alpha_up = alpha_up_rgb[:, :, 0]
            else:
                alpha_pil = Image.fromarray(rgba_np[:, :, 3], mode="L").resize(
                    (out_w, out_h), Image.Resampling.LANCZOS
                )
                alpha_up = np.asarray(alpha_pil, dtype=np.uint8)

            combined = np.dstack([rgb_up, alpha_up])
            Image.fromarray(combined, mode="RGBA").save(dst_path, format="PNG")
        else:
            Image.fromarray(rgb_up, mode="RGB").save(dst_path, format="PNG")

        _flush_device_cache(torch, device)
        processed += 1
        if tqdm is None:
            print(f"[{idx}/{len(files)}] {rel_path} -> {dst_path.name} ({rgb_up.shape[1]}x{rgb_up.shape[0]})")

    print(
        f"\nUpscaling finished: {processed} upscaled ({bled_count} with RGBA alpha-bleed dilation), "
        f"{skipped} skipped -> {output_dir}"
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand 3: encode-dds (native macOS arm64 BC7_UNORM + mipmaps)
# ---------------------------------------------------------------------------


def detect_native_dds_encoder(preferred: str = "auto") -> Tuple[Optional[str], Optional[str]]:
    """Detect native macOS arm64 DDS block-compression encoder binaries on PATH."""
    candidates = ["dds_tool", "kram", "cuttlefish"] if preferred == "auto" else [preferred]
    for name in candidates:
        binary = shutil.which(name)
        if binary:
            return name, binary
    return None, None


def build_encoder_command(
    encoder_name: str,
    encoder_bin: str,
    input_png: Path,
    output_dds: Path,
    dds_format: str,
) -> List[str]:
    """Build exact native CLI command for BCn DDS encoding with full mipmaps."""
    fmt_up = dds_format.upper()

    if encoder_name == "dds_tool":
        # dds_tool auto-generates a full mipmap chain on `create`
        return [encoder_bin, "create", str(input_png), "-f", fmt_up, "--output", str(output_dds)]

    if encoder_name == "kram":
        kram_fmt_map = {
            "BC7_UNORM": "bc7",
            "BC7_UNORM_SRGB": "bc7",
            "BC3_UNORM": "bc3",
            "BC1_UNORM": "bc1",
        }
        kram_fmt = kram_fmt_map.get(fmt_up, "bc7")
        cmd = [
            encoder_bin,
            "encode",
            "-f",
            kram_fmt,
            "-type",
            "2d",
            "-mipmin",
            "1",
            "-i",
            str(input_png),
            "-o",
            str(output_dds),
        ]
        if fmt_up.endswith("_SRGB"):
            cmd.insert(4, "-srgb")
        return cmd

    if encoder_name == "cuttlefish":
        cf_fmt_map = {
            "BC7_UNORM": ("bc7", "unorm"),
            "BC7_UNORM_SRGB": ("bc7", "srgb"),
            "BC3_UNORM": ("bc3", "unorm"),
            "BC1_UNORM": ("bc1", "unorm"),
        }
        cf_fmt, cf_type = cf_fmt_map.get(fmt_up, ("bc7", "unorm"))
        return [
            encoder_bin,
            "--format",
            cf_fmt,
            "--type",
            cf_type,
            "--mipmap",
            "-i",
            str(input_png),
            "-o",
            str(output_dds),
        ]

    raise PipelineError(f"Unsupported encoder: {encoder_name}")


def _normalize_to_pot_image(
    img: Any,
    pot_mode: str,
    max_dim: int,
    apply_alpha_bleed: bool,
) -> Tuple[Any, Tuple[int, int], Tuple[int, int]]:
    """Normalize PIL image to Power-of-Two dimensions and optional RGBA alpha-bleed."""
    Image, _, _, _ = _require_pillow()
    orig_w, orig_h = img.size
    target_w = compute_pot_dimension(orig_w, mode=pot_mode, min_dim=4, max_dim=max_dim)
    target_h = compute_pot_dimension(orig_h, mode=pot_mode, min_dim=4, max_dim=max_dim)

    has_alpha = "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info)
    work = img.convert("RGBA") if has_alpha else img.convert("RGB")

    if has_alpha and apply_alpha_bleed:
        work = alpha_bleed_pil_image(work)

    if (orig_w, orig_h) != (target_w, target_h):
        if pot_mode == "pad":
            bg = (0, 0, 0, 0) if has_alpha else (0, 0, 0)
            canvas = Image.new("RGBA" if has_alpha else "RGB", (target_w, target_h), bg)
            canvas.paste(work, (0, 0))
            work = canvas
        else:
            work = work.resize((target_w, target_h), Image.Resampling.LANCZOS)

    return work, (orig_w, orig_h), (target_w, target_h)


def _encoder_install_help() -> str:
    return (
        "No supported native macOS arm64 DDS encoder found on PATH (checked: dds_tool, kram, cuttlefish).\n"
        "Install one of the following Apple Silicon-native BC7 encoders:\n"
        "  1. dds_tool   : https://github.com/A-t-l-as/dds_tool (fast Go/C BC7 + auto mipmaps)\n"
        "  2. kram       : https://github.com/alecazam/kram (Apple Silicon Metal/ARM64 BC7 encoder)\n"
        "  3. cuttlefish : https://github.com/akb825/Cuttlefish (CMake build with ISPC BC7 support)\n"
        "Or run with --dry-run to preview PoT normalization and exact PCSX2 replacement paths."
    )


def cmd_encode_dds(args: argparse.Namespace) -> int:
    Image, _, _, _ = _require_pillow()

    input_dir = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()

    if not input_dir.is_dir():
        raise PipelineError(f"Input directory not found: {input_dir}")

    encoder_name, encoder_bin = detect_native_dds_encoder(args.encoder)
    if encoder_name is None or encoder_bin is None:
        if not args.dry_run:
            raise PipelineError(_encoder_install_help())
        print(f"[warn] {_encoder_install_help()}\n", file=sys.stderr)

    files = iter_image_files(input_dir, SUPPORTED_IMAGE_EXTS)
    if not files:
        print(f"No supported textures found in {input_dir}.")
        return 0

    plan: List[Dict[str, Any]] = []
    non_pcsx2_names = 0

    for src_path in files:
        rel_path = src_path.relative_to(input_dir)
        _, _, is_pcsx2 = parse_pcsx2_filename(src_path.stem)
        if not is_pcsx2:
            non_pcsx2_names += 1
            if args.strict_pcsx2_names:
                continue

        with Image.open(src_path) as img:
            orig_w, orig_h = img.size
            has_alpha = "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info)

        target_w = compute_pot_dimension(orig_w, mode=args.pot_mode, min_dim=4, max_dim=args.max_dim)
        target_h = compute_pot_dimension(orig_h, mode=args.pot_mode, min_dim=4, max_dim=args.max_dim)
        dst_path = (output_dir / rel_path).with_suffix(".dds")

        plan.append(
            {
                "src": src_path,
                "dst": dst_path,
                "rel": rel_path,
                "orig_size": (orig_w, orig_h),
                "target_size": (target_w, target_h),
                "has_alpha": has_alpha,
                "pcsx2_named": is_pcsx2,
            }
        )

    if non_pcsx2_names > 0:
        action = "skipped (--strict-pcsx2-names)" if args.strict_pcsx2_names else "included (stem preserved)"
        print(
            f"[warn] {non_pcsx2_names} file(s) do not match PCSX2 <TexHash>-<ClutHash> naming ({action}).",
            file=sys.stderr,
        )

    active_encoder = encoder_name or "dds_tool"
    active_bin = encoder_bin or "dds_tool"

    if args.dry_run:
        print(
            f"[dry-run] Planned {len(plan)} DDS texture(s) "
            f"[format={args.format}, encoder={encoder_name or 'NOT_INSTALLED (example: dds_tool)'}, "
            f"pot_mode={args.pot_mode}]:\n"
        )
        for idx, item in enumerate(plan, start=1):
            ow, oh = item["orig_size"]
            tw, th = item["target_size"]
            resize_note = f"{ow}x{oh} -> {tw}x{th} (PoT)" if (ow, oh) != (tw, th) else f"{tw}x{th} (PoT ok)"
            cmd = build_encoder_command(
                active_encoder,
                active_bin,
                Path("<staged_pot.png>"),
                item["dst"],
                args.format,
            )
            print(f"  [{idx:3d}/{len(plan):3d}] {item['rel']} [{resize_note}] -> {item['dst']}")
            print(f"             cmd: {' '.join(cmd)}")
        print("\n[dry-run] No files encoded or written.")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    jobs = max(1, int(args.jobs))
    encoded_count = 0
    skipped_count = 0
    errors: List[str] = []

    def _encode_one(item: Dict[str, Any], staging_root: Path) -> Tuple[str, str]:
        dst_path: Path = item["dst"]
        if dst_path.is_file() and not args.overwrite:
            return "skipped", str(item["rel"])

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        staged_png = staging_root / f"{item['src'].stem}_{abs(hash(str(item['src'])))}.png"

        with Image.open(item["src"]) as img:
            pot_img, _, _ = _normalize_to_pot_image(
                img=img,
                pot_mode=args.pot_mode,
                max_dim=args.max_dim,
                apply_alpha_bleed=not args.no_alpha_bleed,
            )
            pot_img.save(staged_png, format="PNG")

        cmd = build_encoder_command(active_encoder, active_bin, staged_png, dst_path, args.format)
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        staged_png.unlink(missing_ok=True)
        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout or "").strip()
            return "error", f"{item['rel']}: {err_msg or f'exit code {proc.returncode}'}"
        return "encoded", str(item["rel"])

    print(
        f"Encoding {len(plan)} texture(s) into {output_dir} "
        f"[encoder={active_encoder}, format={args.format}, jobs={jobs}]..."
    )
    with tempfile.TemporaryDirectory(prefix="nfsmw_dds_stage_") as tmp_dir:
        staging_root = Path(tmp_dir)
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(_encode_one, item, staging_root) for item in plan]
            for fut in concurrent.futures.as_completed(futures):
                status, detail = fut.result()
                if status == "encoded":
                    encoded_count += 1
                elif status == "skipped":
                    skipped_count += 1
                else:
                    errors.append(detail)

    print(f"DDS encode complete: {encoded_count} encoded, {skipped_count} skipped, {len(errors)} failed.")
    if errors:
        for msg in errors[:20]:
            print(f"  [error] {msg}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Subcommand 4: render-hud-demo (2048x2048 Forza/GT7 minimal tachometer)
# ---------------------------------------------------------------------------


def _load_hud_font(ImageFont: Any, pixel_size: int, style: str = "bold") -> Any:
    """Load crisp geometric motorsport typography on macOS with graceful cross-platform fallback."""
    candidates_bold = [
        ("/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf", 0),
        ("/System/Library/Fonts/Avenir Next.ttc", 0),
        ("/System/Library/Fonts/HelveticaNeue.ttc", 1),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ]
    candidates_regular = [
        ("/System/Library/Fonts/Avenir Next.ttc", 2),
        ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
        ("/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf", 0),
        ("/System/Library/Fonts/Menlo.ttc", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ]
    candidates = candidates_bold if style == "bold" else candidates_regular

    for font_path, index in candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size=pixel_size, index=index)
            except OSError:
                continue

    try:
        return ImageFont.load_default(size=pixel_size)
    except TypeError:
        return ImageFont.load_default()


def _polar_point(cx: float, cy: float, radius: float, angle_deg: float) -> Tuple[float, float]:
    rad = math.radians(angle_deg)
    return (cx + radius * math.cos(rad), cy + radius * math.sin(rad))


def _annular_wedge_poly(
    cx: float,
    cy: float,
    r_inner: float,
    r_outer: float,
    deg_start: float,
    deg_end: float,
    steps: int = 8,
) -> List[Tuple[float, float]]:
    """Build a smooth polygon for an annular arc wedge between r_inner and r_outer."""
    outer_pts = []
    inner_pts = []
    steps = max(2, steps)
    for i in range(steps + 1):
        t = i / float(steps)
        ang = deg_start + (deg_end - deg_start) * t
        outer_pts.append(_polar_point(cx, cy, r_outer, ang))
        inner_pts.append(_polar_point(cx, cy, r_inner, ang))
    inner_pts.reverse()
    return outer_pts + inner_pts


def _rpm_segment_color(rpm_val: float, redline: float, max_rpm: float) -> Tuple[int, int, int, int]:
    """Forza/GT7 minimal motorsport color ramp: crisp ice-white -> amber -> electric crimson."""
    amber_start = max(0.0, redline - 1200.0)
    if rpm_val >= redline:
        return (255, 42, 75, 255)
    if rpm_val >= amber_start:
        t = (rpm_val - amber_start) / max(1.0, (redline - amber_start))
        r = int(242 + (255 - 242) * t)
        g = int(246 + (178 - 246) * t)
        b = int(252 + (42 - 252) * t)
        return (r, g, b, 255)
    return (242, 246, 252, 255)


def render_modern_tachometer(
    size: int = 2048,
    supersample: int = 2,
    rpm: int = 6800,
    max_rpm: int = 9000,
    redline: int = 7500,
    speed: int = 248,
    unit: str = "KM/H",
    gear: str = "5",
    nos_level: float = 0.78,
) -> Any:
    """Procedurally render a supersampled modern minimal tachometer/speedometer RGBA HUD texture."""
    Image, ImageDraw, ImageFilter, ImageFont = _require_pillow()

    ss = max(1, min(supersample, 3))
    s = size * ss
    cx = s * 0.5
    cy = s * 0.5

    rpm_clamped = max(0.0, min(float(rpm), float(max_rpm)))
    nos_clamped = max(0.0, min(float(nos_level), 1.0))

    # Angle sweep: 252 deg symmetric around 270 deg (12 o'clock)
    start_deg = 144.0
    sweep_deg = 252.0
    end_deg = start_deg + sweep_deg  # 396 deg (= 36 deg)

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    # 1. Smoked-glass translucent dial backing + concentric depth vignette
    r_dial = s * 0.445
    draw.ellipse(
        (cx - r_dial, cy - r_dial, cx + r_dial, cy + r_dial),
        fill=(10, 14, 20, 212),
    )
    r_inner_glass = s * 0.418
    draw.ellipse(
        (cx - r_inner_glass, cy - r_inner_glass, cx + r_inner_glass, cy + r_inner_glass),
        fill=(14, 19, 28, 224),
    )
    r_core = s * 0.255
    draw.ellipse(
        (cx - r_core, cy - r_core, cx + r_core, cy + r_core),
        fill=(8, 11, 17, 170),
    )

    # 2. Outer brushed-titanium hairline bezel & subtle technical guide rings
    bezel_w = max(2, int(round(s * 0.0038)))
    draw.ellipse(
        (cx - r_dial, cy - r_dial, cx + r_dial, cy + r_dial),
        outline=(210, 222, 240, 145),
        width=bezel_w,
    )
    draw.ellipse(
        (cx - r_inner_glass, cy - r_inner_glass, cx + r_inner_glass, cy + r_inner_glass),
        outline=(70, 86, 110, 110),
        width=max(1, int(round(s * 0.0018))),
    )
    r_guide = s * 0.256
    draw.ellipse(
        (cx - r_guide, cy - r_guide, cx + r_guide, cy + r_guide),
        outline=(110, 130, 160, 55),
        width=max(1, int(round(s * 0.0015))),
    )

    # 3. Segmented tachometer track + illuminated active RPM arc (72 crisp radial blocks)
    num_segments = 72  # 8 crisp segments per 1000 RPM on a 9000 RPM dial
    seg_gap_deg = 0.72
    seg_step_deg = sweep_deg / float(num_segments)
    r_arc_in = s * 0.342
    r_arc_out = s * 0.396
    r_red_halo_in = s * 0.400
    r_red_halo_out = s * 0.409

    # Continuous outer redline danger band (redline -> max_rpm)
    redline_deg = start_deg + (float(redline) / float(max_rpm)) * sweep_deg
    red_band_poly = _annular_wedge_poly(cx, cy, r_red_halo_in, r_red_halo_out, redline_deg, end_deg, steps=28)
    draw.polygon(red_band_poly, fill=(255, 42, 75, 235))
    gdraw.polygon(red_band_poly, fill=(255, 42, 75, 195))

    for i in range(num_segments):
        seg_rpm_start = (i / float(num_segments)) * max_rpm
        seg_rpm_mid = ((i + 0.5) / float(num_segments)) * max_rpm
        a0 = start_deg + i * seg_step_deg + seg_gap_deg * 0.5
        a1 = start_deg + (i + 1) * seg_step_deg - seg_gap_deg * 0.5
        poly = _annular_wedge_poly(cx, cy, r_arc_in, r_arc_out, a0, a1, steps=4)

        if seg_rpm_mid <= rpm_clamped:
            color = _rpm_segment_color(seg_rpm_mid, float(redline), float(max_rpm))
            draw.polygon(poly, fill=color)
            glow_alpha = 130 if seg_rpm_mid >= redline else 65
            gdraw.polygon(poly, fill=(color[0], color[1], color[2], glow_alpha))
        else:
            if seg_rpm_start >= redline:
                bg_col = (72, 22, 34, 190)
            else:
                bg_col = (30, 39, 54, 195)
            draw.polygon(poly, fill=bg_col)

    # 4. Crisp leading-edge needle / cursor marker at exact RPM position
    needle_deg = start_deg + (rpm_clamped / float(max_rpm)) * sweep_deg
    needle_half_deg = 0.55
    needle_poly = _annular_wedge_poly(
        cx,
        cy,
        s * 0.326,
        s * 0.408,
        needle_deg - needle_half_deg,
        needle_deg + needle_half_deg,
        steps=2,
    )
    needle_col = (255, 70, 95, 255) if rpm_clamped >= redline else (0, 212, 255, 255)
    draw.polygon(needle_poly, fill=(255, 255, 255, 255))
    gdraw.polygon(needle_poly, fill=needle_col)

    # 5. Precision major & minor radial ticks + tabular numerals (0..9)
    num_major = max(1, int(max_rpm // 1000))
    minor_per_major = 4  # every 250 RPM
    total_ticks = num_major * minor_per_major

    for t_idx in range(total_ticks + 1):
        rpm_tick = (t_idx / float(total_ticks)) * max_rpm
        ang = start_deg + (t_idx / float(total_ticks)) * sweep_deg
        is_major = (t_idx % minor_per_major) == 0
        is_red = rpm_tick >= redline

        r0 = s * (0.314 if is_major else 0.326)
        r1 = s * 0.337
        p0 = _polar_point(cx, cy, r0, ang)
        p1 = _polar_point(cx, cy, r1, ang)
        width_px = max(2, int(round(s * (0.0042 if is_major else 0.0018))))
        if is_red:
            tick_col = (255, 72, 102, 240 if is_major else 175)
        elif rpm_tick <= rpm_clamped:
            tick_col = (242, 248, 255, 235 if is_major else 155)
        else:
            tick_col = (145, 162, 185, 185 if is_major else 105)
        draw.line([p0, p1], fill=tick_col, width=width_px)

    font_num = _load_hud_font(ImageFont, int(round(s * 0.036)), style="bold")
    r_label = s * 0.284
    for digit in range(num_major + 1):
        rpm_val = digit * 1000.0
        ang = start_deg + (digit / float(num_major)) * sweep_deg
        lx, ly = _polar_point(cx, cy, r_label, ang)
        if rpm_val >= redline:
            lbl_col = (255, 78, 106, 255)
        elif rpm_val <= rpm_clamped:
            lbl_col = (246, 250, 255, 255)
        else:
            lbl_col = (142, 158, 182, 215)
        draw.text((lx, ly), str(digit), fill=lbl_col, font=font_num, anchor="mm")

    # 6. Top-center shift-light LED array (7 minimal GT3/Forza shift pips)
    shift_leds = 7
    shift_ratio = rpm_clamped / float(max_rpm)
    led_span_deg = 24.0
    for idx in range(shift_leds):
        frac = idx / float(shift_leds - 1)
        ang = 270.0 - led_span_deg * 0.5 + frac * led_span_deg
        a0 = ang - 1.15
        a1 = ang + 1.15
        led_poly = _annular_wedge_poly(cx, cy, s * 0.422, s * 0.431, a0, a1, steps=3)
        threshold = 0.58 + frac * 0.34
        if shift_ratio >= threshold:
            if idx < 3:
                col = (0, 224, 150, 255)
            elif idx < 5:
                col = (255, 188, 38, 255)
            else:
                col = (255, 42, 75, 255)
            draw.polygon(led_poly, fill=col)
            gdraw.polygon(led_poly, fill=(col[0], col[1], col[2], 180))
        else:
            draw.polygon(led_poly, fill=(36, 46, 62, 190))

    # 7. Center telemetry stack: RPM caption, bold digital speed, unit, chamfered gear badge
    font_caption = _load_hud_font(ImageFont, int(round(s * 0.0175)), style="regular")
    font_speed = _load_hud_font(ImageFont, int(round(s * 0.145)), style="bold")
    font_unit = _load_hud_font(ImageFont, int(round(s * 0.024)), style="regular")
    font_gear = _load_hud_font(ImageFont, int(round(s * 0.056)), style="bold")
    font_micro = _load_hud_font(ImageFont, int(round(s * 0.0165)), style="bold")

    draw.text((cx, cy - s * 0.142), "RPM ×1000", fill=(138, 155, 178, 210), font=font_caption, anchor="mm")

    speed_str = f"{int(speed)}"
    # Subtle drop shadow behind speed digits for bright-sky contrast
    draw.text((cx, cy - s * 0.016 + s * 0.004), speed_str, fill=(0, 0, 0, 155), font=font_speed, anchor="mm")
    draw.text((cx, cy - s * 0.016), speed_str, fill=(248, 252, 255, 255), font=font_speed, anchor="mm")
    draw.text((cx, cy + s * 0.078), unit.upper(), fill=(150, 168, 194, 225), font=font_unit, anchor="mm")

    # Minimal chamfered gear pill
    gw = s * 0.088
    gh = s * 0.082
    gx0, gy0 = cx - gw * 0.5, cy + s * 0.125
    gx1, gy1 = cx + gw * 0.5, gy0 + gh
    chamfer = s * 0.014
    gear_poly = [
        (gx0 + chamfer, gy0),
        (gx1 - chamfer, gy0),
        (gx1, gy0 + chamfer),
        (gx1, gy1 - chamfer),
        (gx1 - chamfer, gy1),
        (gx0 + chamfer, gy1),
        (gx0, gy1 - chamfer),
        (gx0, gy0 + chamfer),
    ]
    draw.polygon(gear_poly, fill=(18, 26, 38, 235), outline=(92, 114, 146, 200))
    # Electric cyan top highlight strip on gear box
    accent_h = max(2, int(round(s * 0.004)))
    draw.line(
        [(gx0 + chamfer, gy0 + accent_h * 0.5), (gx1 - chamfer, gy0 + accent_h * 0.5)],
        fill=(0, 212, 255, 255),
        width=accent_h,
    )
    gdraw.line(
        [(gx0 + chamfer, gy0 + accent_h * 0.5), (gx1 - chamfer, gy0 + accent_h * 0.5)],
        fill=(0, 212, 255, 190),
        width=accent_h * 2,
    )
    draw.text((cx, (gy0 + gy1) * 0.5 + s * 0.002), str(gear).upper(), fill=(250, 253, 255, 255), font=font_gear, anchor="mm")

    # 8. Bottom bay: curved N2O (Nitrous) arc in iconic Rockport electric cyan + status pips
    nos_left_deg = 128.0
    nos_right_deg = 52.0
    nos_span = nos_left_deg - nos_right_deg  # 76 deg
    nos_segments = 24
    r_nos_in = s * 0.354
    r_nos_out = s * 0.382
    nos_step = nos_span / float(nos_segments)

    for i in range(nos_segments):
        frac_mid = (i + 0.5) / float(nos_segments)
        # Fill smoothly left (128 deg) -> right (52 deg)
        seg_a1 = nos_left_deg - i * nos_step - 0.35
        seg_a0 = nos_left_deg - (i + 1) * nos_step + 0.35
        poly = _annular_wedge_poly(cx, cy, r_nos_in, r_nos_out, seg_a0, seg_a1, steps=3)
        if frac_mid <= nos_clamped:
            draw.polygon(poly, fill=(0, 212, 255, 255))
            gdraw.polygon(poly, fill=(0, 212, 255, 145))
        else:
            draw.polygon(poly, fill=(24, 38, 52, 195))

    draw.text((cx, cy + s * 0.320), "N2O", fill=(0, 212, 255, 240), font=font_micro, anchor="mm")
    draw.text((cx - s * 0.140, cy + s * 0.255), "ABS", fill=(116, 134, 158, 175), font=font_micro, anchor="mm")
    draw.text((cx + s * 0.140, cy + s * 0.255), "TCS", fill=(116, 134, 158, 175), font=font_micro, anchor="mm")

    # 9. Composite soft luminous bloom layer beneath/over dial and Lanczos downsample to exact 2048x2048
    blurred_glow = glow.filter(ImageFilter.GaussianBlur(radius=s * 0.0085))
    composed = Image.alpha_composite(blurred_glow, base)

    if ss > 1:
        return composed.resize((size, size), Image.Resampling.LANCZOS)
    return composed


def cmd_render_hud_demo(args: argparse.Namespace) -> int:
    Image, ImageDraw, _, _ = _require_pillow()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    size = int(args.size)
    if size < 256 or size > 8192:
        raise PipelineError("--size must be between 256 and 8192.")

    hud_img = render_modern_tachometer(
        size=size,
        supersample=args.supersample,
        rpm=args.rpm,
        max_rpm=args.max_rpm,
        redline=args.redline,
        speed=args.speed,
        unit=args.unit,
        gear=args.gear,
        nos_level=args.nos,
    )

    tex_path = out_dir / f"tachometer_{size}.png"
    hud_img.save(tex_path, format="PNG")
    print(f"Generated transparent RGBA HUD texture : {tex_path} ({size}x{size})")

    if not args.no_backdrop_preview:
        # Compose over a clean dark studio-asphalt backdrop for easy visual review
        backdrop = Image.new("RGBA", (size, size), (12, 16, 24, 255))
        bdraw = ImageDraw.Draw(backdrop, "RGBA")
        # Subtle horizon ambient radial highlight
        for step in range(24):
            t = step / 23.0
            r = int(size * (0.62 - 0.48 * t))
            c = int(14 + 14 * t)
            bdraw.ellipse(
                (size // 2 - r, size // 2 - r, size // 2 + r, size // 2 + r),
                fill=(c, c + 4, c + 12, 255),
            )
        preview = Image.alpha_composite(backdrop, hud_img).convert("RGB")
        preview_path = out_dir / f"tachometer_{size}_preview.png"
        preview.save(preview_path, format="PNG")
        print(f"Generated dark-backdrop visual preview : {preview_path} ({size}x{size})")

    return 0


# ---------------------------------------------------------------------------
# Subcommand 5: check-env (Apple Silicon MPS, Python packages, BC7 DDS encoders)
# ---------------------------------------------------------------------------

DDS_ENCODER_HINTS: Dict[str, str] = {
    "dds_tool": "https://github.com/A-t-l-as/dds_tool  (Go/C fast BC7 + auto mipmaps; place binary on PATH)",
    "kram": "https://github.com/alecazam/kram  (Apple Silicon arm64 Metal/BC7 CLI encoder; build via Xcode/CMake)",
    "cuttlefish": "https://github.com/akb825/Cuttlefish  (CMake + ISPC BC7 encoder; brew install cmake && build)",
}


def check_environment() -> Dict[str, Any]:
    """Inspect Apple Silicon MPS, Python imaging/AI packages, and native BC7 DDS encoders."""
    import importlib
    import platform

    sys_info = {
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "is_apple_silicon": (platform.system() == "Darwin" and platform.machine() == "arm64"),
    }

    pkg_specs = [
        ("pillow", "PIL", "core", "python3 -m pip install 'pillow>=10.3.0'"),
        ("numpy", "numpy", "core", "python3 -m pip install 'numpy>=1.26.0'"),
        ("scipy", "scipy", "optional", "pip install -r tools/requirements-textures.txt"),
        ("torch", "torch", "ai_upscale", "pip install -r tools/requirements-textures.txt"),
        ("spandrel", "spandrel", "ai_upscale", "pip install -r tools/requirements-textures.txt"),
        ("tqdm", "tqdm", "optional", "pip install -r tools/requirements-textures.txt"),
    ]

    packages: Dict[str, Dict[str, Any]] = {}
    torch_mod: Any = None
    for pkg_name, import_name, tier, install_hint in pkg_specs:
        try:
            mod = importlib.import_module(import_name)
            ver = getattr(mod, "__version__", "installed")
            packages[pkg_name] = {
                "installed": True,
                "version": str(ver),
                "tier": tier,
                "install_hint": install_hint,
            }
            if import_name == "torch":
                torch_mod = mod
        except ImportError:
            packages[pkg_name] = {
                "installed": False,
                "version": None,
                "tier": tier,
                "install_hint": install_hint,
            }

    if torch_mod is not None:
        mps_backend = getattr(getattr(torch_mod, "backends", None), "mps", None)
        mps_built = bool(mps_backend.is_built()) if mps_backend is not None else False
        mps_avail = bool(mps_backend.is_available()) if mps_backend is not None else False
        cuda_avail = bool(torch_mod.cuda.is_available())
        recommended_device = "mps" if mps_avail else ("cuda" if cuda_avail else "cpu")
    else:
        mps_built = False
        mps_avail = False
        cuda_avail = False
        recommended_device = "unavailable (install torch)"

    acceleration = {
        "mps_built": mps_built,
        "mps_available": mps_avail,
        "cuda_available": cuda_avail,
        "recommended_device": recommended_device,
        "mps_fallback_env": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", ""),
        "install_hint": (
            "uv venv --python 3.12 .venv-textures && source .venv-textures/bin/activate && "
            "pip install -r tools/requirements-textures.txt"
        ),
    }

    dds_encoders: Dict[str, Dict[str, Any]] = {}
    available_encoders: List[str] = []
    for enc_name, hint in DDS_ENCODER_HINTS.items():
        binary = shutil.which(enc_name)
        found = binary is not None
        if found:
            available_encoders.append(enc_name)
        dds_encoders[enc_name] = {
            "installed": found,
            "path": binary,
            "install_hint": hint,
        }

    pcsx2_dirs = {
        "dumps_dir": str(DEFAULT_PCSX2_DUMPS),
        "dumps_exists": DEFAULT_PCSX2_DUMPS.is_dir(),
        "dumps_count": len(iter_image_files(DEFAULT_PCSX2_DUMPS, AUDIT_IMAGE_EXTS)) if DEFAULT_PCSX2_DUMPS.is_dir() else 0,
        "replacements_dir": str(DEFAULT_PCSX2_REPLACEMENTS),
        "replacements_exists": DEFAULT_PCSX2_REPLACEMENTS.is_dir(),
        "replacements_count": (
            len(iter_image_files(DEFAULT_PCSX2_REPLACEMENTS, AUDIT_IMAGE_EXTS))
            if DEFAULT_PCSX2_REPLACEMENTS.is_dir()
            else 0
        ),
    }

    readiness = {
        "pbr_and_hud_ready": packages["pillow"]["installed"] and packages["numpy"]["installed"],
        "ai_upscale_ready": (
            packages["torch"]["installed"]
            and packages["spandrel"]["installed"]
            and packages["pillow"]["installed"]
            and packages["numpy"]["installed"]
        ),
        "mps_accelerated": mps_avail,
        "dds_encode_ready": len(available_encoders) > 0,
        "preferred_dds_encoder": available_encoders[0] if available_encoders else None,
    }

    return {
        "schema_version": "1.0",
        "system": sys_info,
        "packages": packages,
        "acceleration": acceleration,
        "dds_encoders": dds_encoders,
        "pcsx2_textures": pcsx2_dirs,
        "readiness": readiness,
    }


def cmd_check_env(args: argparse.Namespace) -> int:
    report = check_environment()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    sys_info = report["system"]
    acc = report["acceleration"]
    rd = report["readiness"]
    print("=== NFSMW: Rockport 2026 — Texture Pipeline Environment Preflight ===")
    print(
        f"System        : {sys_info['os']} {sys_info['os_release']} ({sys_info['machine']}) "
        f"| Python {sys_info['python_version']}"
    )
    print(
        f"Apple Silicon : {'YES (arm64)' if sys_info['is_apple_silicon'] else 'NO'} "
        f"| MPS built={acc['mps_built']} available={acc['mps_available']} "
        f"| Device: {acc['recommended_device']}"
    )

    print("\nPython Packages:")
    for name, info in report["packages"].items():
        status = f"OK ({info['version']})" if info["installed"] else "MISSING"
        print(f"  {name:<10} [{info['tier']:<10}] : {status}")
        if not info["installed"]:
            print(f"      -> install: {info['install_hint']}")

    print("\nNative macOS arm64 BC7 DDS Encoders:")
    for enc_name, info in report["dds_encoders"].items():
        if info["installed"]:
            print(f"  {enc_name:<10} : OK ({info['path']})")
        else:
            print(f"  {enc_name:<10} : MISSING")
            print(f"      -> install: {info['install_hint']}")

    p2 = report["pcsx2_textures"]
    print("\nPCSX2 SLUS-21351 Texture Directories:")
    print(f"  dumps        : {'exists' if p2['dumps_exists'] else 'not created yet'} ({p2['dumps_count']} files)")
    print(f"  replacements : {'exists' if p2['replacements_exists'] else 'not created yet'} ({p2['replacements_count']} files)")

    print("\nStage Readiness Summary:")
    print(f"  CC0 PBR Bake & 4K HUD Gen : {'READY' if rd['pbr_and_hud_ready'] else 'MISSING DEPS'}")
    print(f"  Local AI Upscaling (MPS)  : {'READY (' + acc['recommended_device'] + ')' if rd['ai_upscale_ready'] else 'OPTIONAL (install requirements-textures.txt)'}")
    print(f"  Native BC7 DDS Encoding   : {'READY (' + str(rd['preferred_dds_encoder']) + ')' if rd['dds_encode_ready'] else 'MISSING ENCODER (use --dry-run or install dds_tool/kram/cuttlefish)'}")
    return 0


# ---------------------------------------------------------------------------
# CLI Argument Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="texture_pipeline.py",
        description=(
            "NFSMW: Rockport 2026 — Apple Silicon texture audit, local AI upscaling (MPS), "
            "native BC7_UNORM DDS mipmap compiler, and procedural 4K/2K HUD prototype generator."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 tools/texture_pipeline.py audit --out build/texture_audit.json\n"
            "  python3 tools/texture_pipeline.py upscale --input build/dumps --output build/upscaled "
            "--model models/4x-PBRify_UpscalerDAT2.pth --tile 256 --pad 16\n"
            "  python3 tools/texture_pipeline.py encode-dds --input build/upscaled --format BC7_UNORM --dry-run\n"
            "  python3 tools/texture_pipeline.py render-hud-demo --out build/hud_preview\n"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. audit
    p_audit = subparsers.add_parser(
        "audit",
        help="Scan PCSX2 texture dumps, classify categories/alpha/CLUTs, and emit JSON inventory.",
    )
    p_audit.add_argument(
        "--dumps",
        default=str(DEFAULT_PCSX2_DUMPS),
        help=f"PCSX2 texture dumps directory (default: {DEFAULT_PCSX2_DUMPS}).",
    )
    p_audit.add_argument(
        "--out",
        help="Output JSON inventory file path (if omitted, writes JSON to stdout).",
    )
    p_audit.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional cap on number of textures to scan (0 = scan all).",
    )
    p_audit.set_defaults(func=cmd_audit)

    # 2. upscale
    p_up = subparsers.add_parser(
        "upscale",
        help="Run tiled Apple Silicon (MPS) AI upscaling with spandrel + RGB alpha-bleed dilation.",
    )
    p_up.add_argument("--input", required=True, help="Directory containing source textures to upscale.")
    p_up.add_argument("--output", required=True, help="Directory for upscaled PNG textures.")
    p_up.add_argument(
        "--model",
        required=True,
        help="Path to OpenModelDB model weights (.pth / .safetensors), e.g. 4x-PBRify_UpscalerDAT2.pth.",
    )
    p_up.add_argument("--tile", type=int, default=256, help="Inference tile size in pixels (default: 256).")
    p_up.add_argument("--pad", type=int, default=16, help="Overlap padding per tile edge in pixels (default: 16).")
    p_up.add_argument(
        "--device",
        default="auto",
        choices=["auto", "mps", "cuda", "cpu"],
        help="PyTorch compute device (default: auto -> prefers Apple Silicon MPS).",
    )
    p_up.add_argument(
        "--fp16",
        action="store_true",
        help="Enable half-precision inference (opt-in; fp32 is default for maximum MPS op stability).",
    )
    p_up.add_argument(
        "--alpha-bleed-passes",
        type=int,
        default=32,
        help="RGB dilation passes into transparent pixels before upscaling (0 disables; default: 32).",
    )
    p_up.add_argument(
        "--alpha-mode",
        default="lanczos",
        choices=["lanczos", "model"],
        help="Method to upscale alpha channel on RGBA textures (default: lanczos).",
    )
    p_up.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-process textures that already exist in --output.",
    )
    p_up.set_defaults(func=cmd_upscale)

    # 3. encode-dds
    p_dds = subparsers.add_parser(
        "encode-dds",
        help="Normalize to PoT via Pillow and batch-encode BC7_UNORM + mipmaps into PCSX2 replacements.",
    )
    p_dds.add_argument("--input", required=True, help="Directory containing PNG/TGA textures to compile.")
    p_dds.add_argument(
        "--output",
        default=str(DEFAULT_PCSX2_REPLACEMENTS),
        help=f"PCSX2 replacements directory (default: {DEFAULT_PCSX2_REPLACEMENTS}).",
    )
    p_dds.add_argument(
        "--format",
        default="BC7_UNORM",
        choices=["BC7_UNORM", "BC7_UNORM_SRGB", "BC3_UNORM", "BC1_UNORM"],
        help="Target DDS block-compression format (default: BC7_UNORM).",
    )
    p_dds.add_argument(
        "--encoder",
        default="auto",
        choices=["auto", "dds_tool", "kram", "cuttlefish"],
        help="Native macOS arm64 DDS encoder binary (default: auto-detect).",
    )
    p_dds.add_argument(
        "--pot-mode",
        default="nearest",
        choices=["nearest", "ceil", "floor", "pad"],
        help="Power-of-Two dimension normalization strategy (default: nearest).",
    )
    p_dds.add_argument(
        "--max-dim",
        type=int,
        default=4096,
        help="Maximum PoT width/height clamp in pixels (default: 4096).",
    )
    p_dds.add_argument(
        "--no-alpha-bleed",
        action="store_true",
        help="Disable RGBA alpha-bleed dilation prior to mipmap generation.",
    )
    p_dds.add_argument(
        "--strict-pcsx2-names",
        action="store_true",
        help="Skip any texture whose filename does not match PCSX2 <TexHash>-<ClutHash> hex syntax.",
    )
    p_dds.add_argument(
        "--jobs",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Parallel encoder worker threads (default: min(4, CPU count)).",
    )
    p_dds.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .dds files in --output.",
    )
    p_dds.add_argument(
        "--dry-run",
        action="store_true",
        help="Print PoT sizing and exact encoder commands without writing .dds files.",
    )
    p_dds.set_defaults(func=cmd_encode_dds)

    # 4. render-hud-demo
    p_hud = subparsers.add_parser(
        "render-hud-demo",
        help="Procedurally generate a 2048x2048 modern minimal tachometer/speedometer HUD prototype.",
    )
    p_hud.add_argument(
        "--out",
        default="build/hud_preview",
        help="Output directory for generated HUD demo textures (default: build/hud_preview).",
    )
    p_hud.add_argument("--size", type=int, default=2048, help="Output square pixel size (default: 2048).")
    p_hud.add_argument(
        "--supersample",
        type=int,
        default=2,
        help="Internal supersampling factor before Lanczos downsample (default: 2 -> 4096px canvas).",
    )
    p_hud.add_argument("--rpm", type=int, default=6800, help="Demo RPM value displayed on gauge (default: 6800).")
    p_hud.add_argument("--max-rpm", type=int, default=9000, help="Gauge maximum RPM scale (default: 9000).")
    p_hud.add_argument("--redline", type=int, default=7500, help="Redline start RPM (default: 7500).")
    p_hud.add_argument("--speed", type=int, default=248, help="Demo digital speed readout (default: 248).")
    p_hud.add_argument("--unit", default="KM/H", choices=["KM/H", "MPH"], help="Speed unit label (default: KM/H).")
    p_hud.add_argument("--gear", default="5", help="Demo active gear indicator (default: 5).")
    p_hud.add_argument("--nos", type=float, default=0.78, help="Demo N2O nitrous fill fraction 0..1 (default: 0.78).")
    p_hud.add_argument(
        "--no-backdrop-preview",
        action="store_true",
        help="Only emit the transparent RGBA game texture (skip dark-backdrop preview PNG).",
    )
    p_hud.set_defaults(func=cmd_render_hud_demo)

    # 5. check-env
    p_env = subparsers.add_parser(
        "check-env",
        help="Inspect Apple Silicon MPS, Python packages, and native BC7 DDS encoders with install hints.",
    )
    p_env.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON preflight report.",
    )
    p_env.set_defaults(func=cmd_check_env)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PipelineError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[interrupted] Aborted by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
