#!/usr/bin/env python3
"""extract_rt_car.py — On-Demand 40-Car Playable Garage Extractor for RockportMetalRT.

Extracts any of the 40 playable Need for Speed: Most Wanted - Black Edition
(PS2, SLUS-21351) vehicles directly from an untouched source ISO (strictly
read-only 'rb') into `build/car_cache/<CAR_NAME>/` in ~1-2 seconds on demand:

  - Resolves all 40 playable CARS\\<CAR>\\{GEOMETRY.BIN, TEXTURES.BIN} entries
    from ZDIR.BIN (excluding traffic, police, trailers, and shared part packs).
  - Solves EA's 23-char TextureInfo.name truncation bug on long car names
    (BMWM3GTRE46, CORVETTEC6R, IMPREZAWRX, LANCEREVO8, MURCIELAGO, RX7, etc.)
    via `bstringhash(f"{CAR}_KIT00_HEADLIGHT_ON")` / `BRAKELIGHT_ON` + fallbacks.
  - Decodes authentic KIT00 body, base, hood, spoiler, side mirrors, interior,
    windows, headlights, headlight glass, brakelights, OEM stock wheels, and
    front/rear brake rotors & calipers into Y-up right-handed meters (`Y=0` tires).
  - Applies seam-locked adaptive PN-triangle subdivision (`subdivide_car_mesh.py`),
    high-angle mirror normal smoothing, cross-paint-panel normal welding, and
    filters EA's bogus PS2 inner-wheel square alpha-billboard quads.
  - Auto-fits matte inner wheel-well liners + tucked underbody floor pan to each
    car's exact wheel-arch bounds and records `wheel_bounds` in `scene.json`.

Usage:
  python3 tools/extract_rt_car.py --list
  python3 tools/extract_rt_car.py --car FORDGT
  python3 tools/extract_rt_car.py --cars BMWM3GTRE46 CARRERAGT GALLARDO CORVETTEC6R FORDGT RX7 MURCIELAGO
  python3 tools/extract_rt_car.py --verify-all-manifest
  python3 tools/extract_rt_car.py --synthetic --car FORDGT --out-dir /tmp/synth_fordgt
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageFilter

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_pbr_scene as eps  # noqa: E402
import geometry_compiler as gc  # noqa: E402
import geometry_inspect as gi  # noqa: E402
import subdivide_car_mesh as scm  # noqa: E402
import tpk_extract as te  # noqa: E402
from jdlz import jdlz_decompress  # noqa: E402
from lighting_ps2 import iter_top_level_bchunks, parse_light_materials  # noqa: E402
from zdir_tool import PS2NFSMWArchive, bstringhash  # noqa: E402

DEFAULT_CACHE_ROOT = REPO_ROOT / "build" / "car_cache"
DEFAULT_VLT_PATH = REPO_ROOT / "build" / "gameplay_pack" / "vlt_vehicles.json"

# ---------------------------------------------------------------------------
# Authentic 40-Car Playable NFSMW (2005) Garage Catalog + OEM Factory Paints
# ---------------------------------------------------------------------------
# Each entry maps canonical ISO CARS\<CAR> directory name -> metadata & linear-RGB PBR paint.

PLAYABLE_CAR_CATALOG: Dict[str, Dict[str, Any]] = {
    "BMWM3GTRE46": {
        "display_name": "BMW M3 GTR (E46 ALMS)",
        "alias": "m3_gtr",
        "base_color": [0.56, 0.60, 0.66],
        "secondary_color": [0.025, 0.14, 0.50],
        "metallic": 0.74,
        "roughness": 0.18,
        "livery_enabled": True,
        "include_base_mesh": False,
    },
    "CARRERAGT": {
        "display_name": "Porsche Carrera GT",
        "alias": "carrera_gt",
        "base_color": [0.64, 0.67, 0.72],
        "secondary_color": [0.64, 0.67, 0.72],
        "metallic": 0.68,
        "roughness": 0.20,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "GALLARDO": {
        "display_name": "Lamborghini Gallardo",
        "alias": "gallardo",
        "base_color": [0.96, 0.68, 0.028],
        "secondary_color": [0.88, 0.52, 0.015],
        "metallic": 0.54,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "CORVETTEC6R": {
        "display_name": "Chevrolet Corvette C6.R",
        "alias": "corvette_c6r",
        "base_color": [0.94, 0.72, 0.03],
        "secondary_color": [0.10, 0.11, 0.12],
        "metallic": 0.42,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": False,
    },
    "FORDGT": {
        "display_name": "Ford GT",
        "alias": "ford_gt",
        "base_color": [0.024, 0.13, 0.46],
        "secondary_color": [0.88, 0.90, 0.94],
        "metallic": 0.62,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "MURCIELAGO": {
        "display_name": "Lamborghini Murcielago",
        "alias": "murcielago",
        "base_color": [0.88, 0.24, 0.02],
        "secondary_color": [0.95, 0.36, 0.04],
        "metallic": 0.58,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "RX7": {
        "display_name": "Mazda RX-7 (FD3S)",
        "alias": "rx7",
        "base_color": [0.68, 0.032, 0.030],
        "secondary_color": [0.68, 0.032, 0.030],
        "metallic": 0.48,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "SLR": {
        "display_name": "Mercedes-Benz SLR McLaren",
        "alias": "slr",
        "base_color": [0.62, 0.65, 0.70],
        "secondary_color": [0.62, 0.65, 0.70],
        "metallic": 0.74,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "VIPER": {
        "display_name": "Dodge Viper SRT-10",
        "alias": "viper",
        "base_color": [0.74, 0.030, 0.025],
        "secondary_color": [0.88, 0.90, 0.92],
        "metallic": 0.45,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "DB9": {
        "display_name": "Aston Martin DB9",
        "alias": "db9",
        "base_color": [0.06, 0.16, 0.11],
        "secondary_color": [0.06, 0.16, 0.11],
        "metallic": 0.66,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "CORVETTE": {
        "display_name": "Chevrolet Corvette C6",
        "alias": "corvette",
        "base_color": [0.76, 0.035, 0.028],
        "secondary_color": [0.76, 0.035, 0.028],
        "metallic": 0.56,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "911GT2": {
        "display_name": "Porsche 911 GT2 (996)",
        "alias": "911_gt2",
        "base_color": [0.64, 0.66, 0.70],
        "secondary_color": [0.64, 0.66, 0.70],
        "metallic": 0.70,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "911TURBO": {
        "display_name": "Porsche 911 Turbo S (996)",
        "alias": "911_turbo",
        "base_color": [0.04, 0.09, 0.22],
        "secondary_color": [0.04, 0.09, 0.22],
        "metallic": 0.68,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "997S": {
        "display_name": "Porsche 911 Carrera S (997)",
        "alias": "997s",
        "base_color": [0.60, 0.63, 0.68],
        "secondary_color": [0.60, 0.63, 0.68],
        "metallic": 0.68,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "CAYMANS": {
        "display_name": "Porsche Cayman S",
        "alias": "cayman_s",
        "base_color": [0.76, 0.04, 0.03],
        "secondary_color": [0.76, 0.04, 0.03],
        "metallic": 0.52,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "MUSTANGGT": {
        "display_name": "Ford Mustang GT",
        "alias": "mustang_gt",
        "base_color": [0.05, 0.06, 0.08],
        "secondary_color": [0.70, 0.74, 0.80],
        "metallic": 0.62,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "LANCEREVO8": {
        "display_name": "Mitsubishi Lancer Evolution VIII",
        "alias": "evo8",
        "base_color": [0.76, 0.04, 0.03],
        "secondary_color": [0.76, 0.04, 0.03],
        "metallic": 0.44,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "IMPREZAWRX": {
        "display_name": "Subaru Impreza WRX STi",
        "alias": "wrx_sti",
        "base_color": [0.025, 0.14, 0.58],
        "secondary_color": [0.025, 0.14, 0.58],
        "metallic": 0.60,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "SUPRA": {
        "display_name": "Toyota Supra (MKIV)",
        "alias": "supra",
        "base_color": [0.78, 0.78, 0.80],
        "secondary_color": [0.78, 0.78, 0.80],
        "metallic": 0.48,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "CLK500": {
        "display_name": "Mercedes-Benz CLK 500",
        "alias": "clk500",
        "base_color": [0.58, 0.61, 0.66],
        "secondary_color": [0.58, 0.61, 0.66],
        "metallic": 0.70,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "SL500": {
        "display_name": "Mercedes-Benz SL 500",
        "alias": "sl500",
        "base_color": [0.54, 0.57, 0.62],
        "secondary_color": [0.54, 0.57, 0.62],
        "metallic": 0.72,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "SL65": {
        "display_name": "Mercedes-Benz SL65 AMG",
        "alias": "sl65",
        "base_color": [0.16, 0.18, 0.21],
        "secondary_color": [0.16, 0.18, 0.21],
        "metallic": 0.72,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "CAMARO": {
        "display_name": "Chevrolet Camaro SS (1967)",
        "alias": "camaro",
        "base_color": [0.80, 0.26, 0.03],
        "secondary_color": [0.88, 0.90, 0.92],
        "metallic": 0.45,
        "roughness": 0.19,
        "livery_enabled": False,
        "include_base_mesh": False,
    },
    "GTO": {
        "display_name": "Pontiac GTO",
        "alias": "gto",
        "base_color": [0.05, 0.20, 0.48],
        "secondary_color": [0.05, 0.20, 0.48],
        "metallic": 0.60,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "MONARO": {
        "display_name": "Vauxhall Monaro VXR",
        "alias": "monaro",
        "base_color": [0.76, 0.05, 0.04],
        "secondary_color": [0.76, 0.05, 0.04],
        "metallic": 0.55,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "ELISE": {
        "display_name": "Lotus Elise 111R",
        "alias": "elise",
        "base_color": [0.92, 0.66, 0.03],
        "secondary_color": [0.92, 0.66, 0.03],
        "metallic": 0.46,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "RX8": {
        "display_name": "Mazda RX-8",
        "alias": "rx8",
        "base_color": [0.04, 0.22, 0.58],
        "secondary_color": [0.04, 0.22, 0.58],
        "metallic": 0.60,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "RX8SPEEDT": {
        "display_name": "Mazda RX-8 Mazdaspeed",
        "alias": "rx8_speedt",
        "base_color": [0.72, 0.04, 0.04],
        "secondary_color": [0.72, 0.04, 0.04],
        "metallic": 0.54,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "ECLIPSEGT": {
        "display_name": "Mitsubishi Eclipse GT",
        "alias": "eclipse_gt",
        "base_color": [0.88, 0.28, 0.02],
        "secondary_color": [0.88, 0.28, 0.02],
        "metallic": 0.58,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "IS300": {
        "display_name": "Lexus IS 300",
        "alias": "is300",
        "base_color": [0.56, 0.60, 0.66],
        "secondary_color": [0.56, 0.60, 0.66],
        "metallic": 0.68,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "CTS": {
        "display_name": "Cadillac CTS",
        "alias": "cts",
        "base_color": [0.16, 0.17, 0.20],
        "secondary_color": [0.16, 0.17, 0.20],
        "metallic": 0.70,
        "roughness": 0.19,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "A3": {
        "display_name": "Audi A3 3.2 Quattro",
        "alias": "a3",
        "base_color": [0.56, 0.59, 0.64],
        "secondary_color": [0.56, 0.59, 0.64],
        "metallic": 0.68,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "A4": {
        "display_name": "Audi A4 3.2 FSI Quattro",
        "alias": "a4",
        "base_color": [0.12, 0.16, 0.24],
        "secondary_color": [0.12, 0.16, 0.24],
        "metallic": 0.68,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "TT": {
        "display_name": "Audi TT 3.2 Quattro",
        "alias": "tt",
        "base_color": [0.62, 0.65, 0.70],
        "secondary_color": [0.62, 0.65, 0.70],
        "metallic": 0.70,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "GTI": {
        "display_name": "Volkswagen Golf GTI (MkV)",
        "alias": "gti",
        "base_color": [0.76, 0.04, 0.03],
        "secondary_color": [0.76, 0.04, 0.03],
        "metallic": 0.44,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "COBALTSS": {
        "display_name": "Chevrolet Cobalt SS Supercharged",
        "alias": "cobalt_ss",
        "base_color": [0.92, 0.72, 0.03],
        "secondary_color": [0.92, 0.72, 0.03],
        "metallic": 0.42,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "CLIO": {
        "display_name": "Renault Clio V6",
        "alias": "clio",
        "base_color": [0.035, 0.16, 0.56],
        "secondary_color": [0.035, 0.16, 0.56],
        "metallic": 0.64,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "PUNTO": {
        "display_name": "Fiat Grande Punto",
        "alias": "punto",
        "base_color": [0.84, 0.30, 0.02],
        "secondary_color": [0.84, 0.30, 0.02],
        "metallic": 0.52,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": True,
    },
    "BMWM3": {
        "display_name": "BMW M3 GTR Street (E46)",
        "alias": "bmw_m3",
        "base_color": [0.58, 0.62, 0.68],
        "secondary_color": [0.58, 0.62, 0.68],
        "metallic": 0.74,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": False,
    },
    "BMWM3GTR": {
        "display_name": "BMW M3 GTR Race Spec",
        "alias": "bmw_m3_gtr_race",
        "base_color": [0.58, 0.62, 0.68],
        "secondary_color": [0.025, 0.14, 0.50],
        "metallic": 0.74,
        "roughness": 0.18,
        "livery_enabled": False,
        "include_base_mesh": False,
    },
}

assert len(PLAYABLE_CAR_CATALOG) == 40, f"Expected 40 playable cars, got {len(PLAYABLE_CAR_CATALOG)}"

# Top-15 Iconic Flagship & Blacklist Roster (verified in tests/test_top15_cars.py)
TOP15_FLAGSHIP_CARS: Tuple[str, ...] = (
    "BMWM3GTRE46",
    "CARRERAGT",
    "GALLARDO",
    "CORVETTEC6R",
    "FORDGT",
    "RX7",
    "SLR",
    "DB9",
    "VIPER",
    "CORVETTE",
    "CLK500",
    "MUSTANGGT",
    "LANCEREVO8",
    "CAYMANS",
    "SUPRA",
)

# Vehicles with authentic stock flush pop-up headlights (zero KIT00_HEADLIGHT_B parts in PS2 ISO)
POPUP_HEADLIGHT_CARS = frozenset({"RX7"})

# Lookup alias -> canonical ISO car name
CAR_ALIAS_MAP: Dict[str, str] = {}
for _canon, _meta in PLAYABLE_CAR_CATALOG.items():
    CAR_ALIAS_MAP[_canon.lower()] = _canon
    CAR_ALIAS_MAP[_meta["alias"].lower()] = _canon
CAR_ALIAS_MAP["m3"] = "BMWM3GTRE46"
CAR_ALIAS_MAP["bmw"] = "BMWM3GTRE46"
CAR_ALIAS_MAP["cgt"] = "CARRERAGT"
CAR_ALIAS_MAP["porsche"] = "CARRERAGT"
CAR_ALIAS_MAP["lambo"] = "GALLARDO"
CAR_ALIAS_MAP["c6r"] = "CORVETTEC6R"
CAR_ALIAS_MAP["gt"] = "FORDGT"
CAR_ALIAS_MAP["murci"] = "MURCIELAGO"
CAR_ALIAS_MAP["evo"] = "LANCEREVO8"
CAR_ALIAS_MAP["wrx"] = "IMPREZAWRX"
CAR_ALIAS_MAP["mustang"] = "MUSTANGGT"


def canonicalize_car_name(raw: str) -> str:
    """Resolve user/CLI alias or canonical directory name to one of the 40 playable cars."""
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if key in CAR_ALIAS_MAP:
        return CAR_ALIAS_MAP[key]
    compact = key.replace("_", "")
    if compact in CAR_ALIAS_MAP:
        return CAR_ALIAS_MAP[compact]
    upper = raw.strip().upper()
    if upper in PLAYABLE_CAR_CATALOG:
        return upper
    raise KeyError(f"Unknown playable car '{raw}'. Use --list to view all 40 playable cars.")


def resolve_iso_path(cli_iso: Optional[Path] = None) -> Optional[Path]:
    """Resolve untouched PS2 ISO path strictly read-only."""
    if cli_iso is not None:
        p = cli_iso.expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"ISO not found: {p}")
        return p
    if "NFSMW_ISO" in os.environ:
        env_val = os.environ["NFSMW_ISO"].strip()
        if not env_val:
            return None
        p = Path(env_val).expanduser()
        return p if p.is_file() else None
    default_iso = eps.DEFAULT_SOURCE_ISO
    return default_iso if default_iso.is_file() else None


def _load_vlt_table() -> Dict[str, Any]:
    if DEFAULT_VLT_PATH.is_file():
        try:
            doc = json.loads(DEFAULT_VLT_PATH.read_text(encoding="utf-8"))
            return doc.get("vehicles", {})
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Texture Slot Matching (Solves EA 23-Char TextureInfo.name Truncation!)
# ---------------------------------------------------------------------------


def match_car_textures(
    car_name: str,
    extracted: Sequence[te.ExtractedTexture],
) -> Dict[str, te.ExtractedTexture]:
    """Match decoded TPK textures to RTSceneBuilder texture slots via bstringhash + prefix fallback."""
    car_u = car_name.upper()
    by_hash: Dict[int, te.ExtractedTexture] = {t.name_hash: t for t in extracted}

    slot_candidates: Dict[str, List[str]] = {
        "car_misc": [f"{car_u}_MISC", f"{car_u}_KIT00_MISC"],
        "car_badging": [f"{car_u}_BADGING", f"{car_u}_BADGE", f"{car_u}_KIT00_BADGING"],
        "car_headlight": [
            f"{car_u}_KIT00_HEADLIGHT_ON",
            f"{car_u}_HEADLIGHT_ON",
            f"{car_u}_KIT00_HEADLIGHT_OFF",
            f"{car_u}_KIT00_HEADLIGHT_GLASS_ON",
            f"{car_u}_KIT00_HEADLIGHT_GLASS_OFF",
        ],
        "car_taillight": [
            f"{car_u}_KIT00_BRAKELIGHT_ON",
            f"{car_u}_BRAKELIGHT_ON",
            f"{car_u}_KIT00_BRAKELIGHT_GLASS_ON",
            f"{car_u}_KIT00_BRAKELIGHT_OFF",
            f"{car_u}_KIT00_TAILLIGHT_ON",
        ],
        "car_tire": [f"{car_u}_TIRE", f"{car_u}_WHEEL", f"{car_u}_KIT00_TIRE"],
        "car_interior": [f"{car_u}_INTERIOR", f"{car_u}_KIT00_INTERIOR"],
    }

    matched: Dict[str, te.ExtractedTexture] = {}
    for slot, names in slot_candidates.items():
        for cand in names:
            h = bstringhash(cand)
            if h in by_hash:
                matched[slot] = by_hash[h]
                break

    # Substring fallback if any car uses non-standard naming
    fallback_keys = {
        "car_misc": ("_MISC",),
        "car_badging": ("_BADG",),
        "car_headlight": ("HEADL",),
        "car_taillight": ("BRAKEL", "TAILL"),
        "car_tire": ("_TIRE", "_WHEEL"),
        "car_interior": ("_INTERIOR",),
    }
    for slot, keys in fallback_keys.items():
        if slot in matched:
            continue
        for tex in extracted:
            tu = tex.name.upper()
            if any(k in tu for k in keys):
                matched[slot] = tex
                break
    return matched


# ---------------------------------------------------------------------------
# Material Mapping, Wheel-Quad Filtering & Cross-Panel Normal Smoothing
# ---------------------------------------------------------------------------


def map_car_material_name(part_name: str, ps2_mat_name: str, tex_hash_name: str = "") -> str:
    """Map PS2 part + LightMaterial into RockportMetalRT's rich UV-mapped PBR material names."""
    pn = part_name.upper()
    mn = ps2_mat_name.upper()
    tn = tex_hash_name.upper()

    if "HEADLIGHT_GLASS" in pn or mn == "HEADLIGHTGLASS":
        return "headlight_glass"
    if "BRAKELIGHT_GLASS" in pn:
        return "taillight_emissive"
    if "HEADLIGHT" in pn:
        if mn == "CHROME":
            return "car_misc_chrome"
        if mn in ("MOLDINGS", "DULLPLASTIC", "PLAINNOTHING"):
            return "matte_trim"
        return "headlight_emissive"
    if "BRAKELIGHT" in pn or mn == "BRAKELIGHT":
        if mn in ("MOLDINGS", "DULLPLASTIC"):
            return "matte_trim"
        return "taillight_emissive"
    if mn == "WINDSHIELD" or ("WINDOW" in pn and mn != "WINDOWMASK"):
        return "smoked_glass"
    if mn == "WINDOWMASK":
        return "matte_trim"
    if mn == "INTERIOR" or "INTERIOR" in pn:
        return "interior_dark"
    if mn == "BRAKEDISC":
        return "brake_rotor"
    if mn == "CALIPERDECAL":
        return "brake_caliper"
    if "BADG" in tn or mn == "DECAL":
        return "car_badge"
    if mn == "DIABLOHP":
        # On BMW M3 GTR E46, DIABLOHP separates silver trim/trunk cove from blue livery panels;
        # on single-tone OEM exotics (Carrera GT, Ford GT, etc.) unifying with car_paint_hero removes fender-door seams!
        return "car_paint_silver" if pn.startswith("BMWM3") else "car_paint_hero"
    if mn == "CARBODY":
        return "car_paint_hero"
    if mn == "CHROME":
        return "car_misc_chrome" if "MISC" in tn or "BODY" in pn else "chrome_trim"
    if mn == "ALUMINUM":
        return "car_misc_metal" if "BODY" in pn else "brushed_aluminum"
    if mn == "MAGSILVER":
        return "alloy_rim"
    if mn in ("CARBONFIBER", "CARBONFIBER2") or "_CF_" in pn:
        return "carbon_fiber"
    if mn == "RUBBER":
        return "rubber_tire"
    if mn == "LICENSEPLATE":
        return "license_plate"
    if "TIRE" in pn or "WHEEL" in pn:
        if mn in ("MOLDINGS", "DULLPLASTIC", "BOTTOM", "PLAINNOTHING"):
            return "matte_trim"
        return "alloy_rim"
    if mn in ("MOLDINGS", "DULLPLASTIC", "BOTTOM", "PLAINNOTHING"):
        return "car_misc_trim" if "MISC" in tn else "matte_trim"
    if "SPOILER" in pn:
        return "car_paint_hero"
    return "car_paint_hero"


def _decoded_to_compiled(mesh: gi.DecodedMesh) -> gc.CompiledMesh:
    return gc.CompiledMesh(
        vertices=list(mesh.vertices),
        normals=list(mesh.normals),
        uvs=list(mesh.uvs),
        triangles=list(mesh.triangles),
    )


def _compiled_to_decoded(mesh: gc.CompiledMesh) -> gi.DecodedMesh:
    return gi.DecodedMesh(
        vertices=list(mesh.vertices),
        normals=list(mesh.normals),
        uvs=list(mesh.uvs),
        triangles=list(mesh.triangles),
    )


def filter_wheel_billboard_quads(
    mesh: gi.DecodedMesh,
    part_info: gi.SolidPartInfo,
    mat_lookup: Dict[str, str],
) -> Tuple[gi.DecodedMesh, int]:
    """Strip EA PS2 over-radius square inner-wheel alpha-billboard quads (r > 1.07 * tire_r)."""
    if not mesh.triangles:
        return mesh, 0
    mat_hashes = part_info.light_material_hashes
    # Determine tire/rim radius in PS2 (x, z) plane around origin
    max_r = 0.0
    for i0, i1, i2, _tx, lm in mesh.triangles:
        mh = mat_hashes[lm].lower() if 0 <= lm < len(mat_hashes) else ""
        mn = mat_lookup.get(mh, mh).upper()
        if mn in ("RUBBER", "MAGSILVER", "CHROME", "ALUMINUM"):
            for idx in (i0, i1, i2):
                vx, _vy, vz = mesh.vertices[idx]
                r = math.hypot(vx, vz)
                if r > max_r:
                    max_r = r
    if max_r < 0.15:
        return mesh, 0

    limit_r = max_r * 1.07
    kept: List[Tuple[int, int, int, int, int]] = []
    removed = 0
    for tri in mesh.triangles:
        i0, i1, i2, _tx, lm = tri
        mh = mat_hashes[lm].lower() if 0 <= lm < len(mat_hashes) else ""
        mn = mat_lookup.get(mh, mh).upper()
        if mn in ("MOLDINGS", "DULLPLASTIC", "BOTTOM", "PLAINNOTHING"):
            r_tri = max(
                math.hypot(mesh.vertices[idx][0], mesh.vertices[idx][2])
                for idx in (i0, i1, i2)
            )
            if r_tri > limit_r:
                removed += 1
                continue
        kept.append(tri)
    if removed == 0:
        return mesh, 0
    return gi.DecodedMesh(
        vertices=list(mesh.vertices),
        normals=list(mesh.normals),
        uvs=list(mesh.uvs),
        triangles=kept,
    ), removed


def _upscale_car_texture(img: Image.Image, slot: str) -> Image.Image:
    """Cleanly upscale low-res PS2 OEM car textures (128-256px) to crisp 512-1024px RT assets."""
    target_max = 1024 if slot == "car_misc" else 512
    w, h = img.size
    longest = max(w, h)
    if longest <= 0 or longest >= target_max:
        return img
    scale = min(4.0, float(target_max) / float(longest))
    nw = max(w, int(round(w * scale)))
    nh = max(h, int(round(h * scale)))
    if img.mode == "RGBA":
        rgb = img.convert("RGB").resize((nw, nh), Image.Resampling.LANCZOS)
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=1.0, percent=75, threshold=2))
        alpha = img.getchannel("A").resize((nw, nh), Image.Resampling.LANCZOS)
        out = rgb.convert("RGBA")
        out.putalpha(alpha)
        return out
    up = img.convert("RGB").resize((nw, nh), Image.Resampling.LANCZOS)
    return up.filter(ImageFilter.UnsharpMask(radius=1.0, percent=75, threshold=2))


def weld_smooth_normals_yup(
    mesh: gi.DecodedMesh,
    *,
    crease_deg: float = 48.0,
    part_info: Optional[gi.SolidPartInfo] = None,
    mat_lookup: Optional[Dict[str, str]] = None,
    only_paint_materials: bool = False,
    laplacian_iters: int = 0,
) -> gi.DecodedMesh:
    """Recompute outward-consistent crease-aware area-weighted smooth vertex normals in Y-up space.

    Orients every triangle's geometric face normal against its authored PS2 vertex normal first
    (preventing opposite-facing inner shells on thin mirrors or vents from corrupting outer paint),
    welds coincident / <=4.2mm panel-boundary vertices using signed dot >= cos(crease_deg), and
    optionally runs crease-locked Laplacian normal diffusion so flat low-poly automotive fender
    panels inherit authentic vertical body curvature without crossing hard trim/vent creases.
    """
    n_verts = len(mesh.vertices)
    if n_verts == 0 or not mesh.triangles:
        return mesh

    cos_thresh = math.cos(math.radians(crease_deg))
    mat_hashes = part_info.light_material_hashes if part_info else []
    mlook = mat_lookup or {}

    def _is_paint_lm(lm: int) -> bool:
        if not only_paint_materials:
            return True
        if 0 <= lm < len(mat_hashes):
            mn = mlook.get(mat_hashes[lm].lower(), "").upper()
            return mn in ("CARBODY", "DIABLOHP")
        return False

    cell_scale = 400.0
    max_weld_dist_sq = 0.0042 * 0.0042
    pos_groups: Dict[Tuple[int, int, int], List[int]] = {}
    vert_faces: List[List[Tuple[Tuple[float, float, float], float, bool, int]]] = [[] for _ in range(n_verts)]
    adj_edges: List[set[int]] = [set() for _ in range(n_verts)] if laplacian_iters > 0 else []

    for i0, i1, i2, _tx, lm in mesh.triangles:
        p0, p1, p2 = mesh.vertices[i0], mesh.vertices[i1], mesh.vertices[i2]
        e1 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
        e2 = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
        fn = (
            e1[1] * e2[2] - e1[2] * e2[1],
            e1[2] * e2[0] - e1[0] * e2[2],
            e1[0] * e2[1] - e1[1] * e2[0],
        )
        mag = math.sqrt(fn[0] * fn[0] + fn[1] * fn[1] + fn[2] * fn[2])
        if mag < 1e-12:
            continue
        # Orient face normal consistently outward using the triangle's authored normals
        onx = mesh.normals[i0][0] + mesh.normals[i1][0] + mesh.normals[i2][0]
        ony = mesh.normals[i0][1] + mesh.normals[i1][1] + mesh.normals[i2][1]
        onz = mesh.normals[i0][2] + mesh.normals[i1][2] + mesh.normals[i2][2]
        if (fn[0] * onx + fn[1] * ony + fn[2] * onz) < 0.0:
            fn = (-fn[0], -fn[1], -fn[2])

        is_paint = _is_paint_lm(lm)
        if laplacian_iters > 0 and is_paint:
            adj_edges[i0].add(i1); adj_edges[i0].add(i2)
            adj_edges[i1].add(i0); adj_edges[i1].add(i2)
            adj_edges[i2].add(i0); adj_edges[i2].add(i1)

        for idx in (i0, i1, i2):
            vert_faces[idx].append((fn, mag, is_paint, lm))
            vx, vy, vz = mesh.vertices[idx]
            qk = (int(math.floor(vx * cell_scale)), int(math.floor(vy * cell_scale)), int(math.floor(vz * cell_scale)))
            pos_groups.setdefault(qk, []).append(idx)

    for qk, lst in pos_groups.items():
        pos_groups[qk] = list(set(lst))

    # Precompute neighbor spatial clusters per vertex for fast welding & diffusion
    new_norms = list(mesh.normals)
    is_target_vert = [False] * n_verts
    for idx in range(n_verts):
        own_faces = vert_faces[idx]
        if not own_faces:
            continue
        if only_paint_materials and not any(ip for _, _, ip, _ in own_faces):
            continue
        is_target_vert[idx] = True

        # Reference outward normal from own incident faces (already outward-aligned!)
        rx, ry, rz = 0.0, 0.0, 0.0
        own_lms = set()
        for fn, _mag, ip, lm in own_faces:
            if only_paint_materials and not ip:
                continue
            own_lms.add(lm)
            rx += fn[0]
            ry += fn[1]
            rz += fn[2]
        rmag = math.sqrt(rx * rx + ry * ry + rz * rz)
        if rmag < 1e-10:
            continue
        rx, ry, rz = rx / rmag, ry / rmag, rz / rmag

        vx, vy, vz = mesh.vertices[idx]
        cx0 = int(math.floor(vx * cell_scale))
        cy0 = int(math.floor(vy * cell_scale))
        cz0 = int(math.floor(vz * cell_scale))
        ax, ay, az = 0.0, 0.0, 0.0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for nb_idx in pos_groups.get((cx0 + dx, cy0 + dy, cz0 + dz), ()):
                        nx_, ny_, nz_ = mesh.vertices[nb_idx]
                        ddx, ddy, ddz = nx_ - vx, ny_ - vy, nz_ - vz
                        if ddx * ddx + ddy * ddy + ddz * ddz > max_weld_dist_sq:
                            continue
                        for fn, fmag, ip, lm in vert_faces[nb_idx]:
                            if only_paint_materials:
                                if not ip:
                                    continue
                            elif lm not in own_lms:
                                # Keep non-paint sub-materials separated (e.g. mirror chrome glass vs painted cap)
                                continue
                            dot = (fn[0] * rx + fn[1] * ry + fn[2] * rz) / fmag
                            # Signed dot check (NEVER abs(dot)!) prevents inner mirror/vent backfaces from flipping paint!
                            if dot >= cos_thresh:
                                ax += fn[0]
                                ay += fn[1]
                                az += fn[2]
        amag = math.sqrt(ax * ax + ay * ay + az * az)
        if amag > 1e-10:
            new_norms[idx] = (ax / amag, ay / amag, az / amag)

    # Optional crease-locked Laplacian normal diffusion across connected smooth paint edges
    if laplacian_iters > 0:
        # Link coincident vertices AND bridge narrow surface-tangent trim slits (<=3.8cm wide, <=1.4cm out-of-plane,
        # e.g. Carrera GT's 2cm fender side-marker strake) so smooth body curvature flows naturally across flat panels
        # without ever jumping across deep vents or 90-deg body steps!
        slit_scale = 25.0  # 4cm spatial grid
        slit_grid: Dict[Tuple[int, int, int], List[int]] = {}
        for v_idx in range(n_verts):
            if not is_target_vert[v_idx]:
                continue
            vx, vy, vz = mesh.vertices[v_idx]
            sk = (int(math.floor(vx * slit_scale)), int(math.floor(vy * slit_scale)), int(math.floor(vz * slit_scale)))
            slit_grid.setdefault(sk, []).append(v_idx)

        slit_max_d2 = 0.040 * 0.040
        slit_cos = math.cos(math.radians(44.0))
        for (gx, gy, gz), group in slit_grid.items():
            neigh_verts: List[int] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        neigh_verts.extend(slit_grid.get((gx + dx, gy + dy, gz + dz), ()))
            for a in group:
                va = mesh.vertices[a]
                na = new_norms[a]
                for b in neigh_verts:
                    if b <= a or b in adj_edges[a]:
                        continue
                    vb = mesh.vertices[b]
                    ddx, ddy, ddz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
                    d2 = ddx * ddx + ddy * ddy + ddz * ddz
                    if d2 <= max_weld_dist_sq:
                        adj_edges[a].add(b)
                        adj_edges[b].add(a)
                    elif only_paint_materials and d2 <= slit_max_d2:
                        nb = new_norms[b]
                        if (na[0] * nb[0] + na[1] * nb[1] + na[2] * nb[2]) >= slit_cos:
                            # Ensure both vertices lie on the same outer body skin (minimal out-of-plane step)
                            amx = 0.5 * (na[0] + nb[0])
                            amy = 0.5 * (na[1] + nb[1])
                            amz = 0.5 * (na[2] + nb[2])
                            am = math.sqrt(amx * amx + amy * amy + amz * amz) + 1e-9
                            out_step = abs(ddx * amx + ddy * amy + ddz * amz) / am
                            if out_step <= 0.014:
                                adj_edges[a].add(b)
                                adj_edges[b].add(a)

        diff_cos = math.cos(math.radians(min(crease_deg, 48.0)))
        cur_norms = list(new_norms)
        for _ in range(laplacian_iters):
            nxt_norms = list(cur_norms)
            for idx in range(n_verts):
                if not is_target_vert[idx] or not adj_edges[idx]:
                    continue
                nx0, ny0, nz0 = cur_norms[idx]
                sx, sy, sz = nx0 * 0.75, ny0 * 0.75, nz0 * 0.75
                cnt = 0
                for nb in adj_edges[idx]:
                    if not is_target_vert[nb]:
                        continue
                    nbx, nby, nbz = cur_norms[nb]
                    if (nx0 * nbx + ny0 * nby + nz0 * nbz) >= diff_cos:
                        sx += nbx
                        sy += nby
                        sz += nbz
                        cnt += 1
                if cnt > 0:
                    sm = math.sqrt(sx * sx + sy * sy + sz * sz)
                    if sm > 1e-10:
                        nxt_norms[idx] = (sx / sm, sy / sm, sz / sm)
            cur_norms = nxt_norms
        new_norms = cur_norms

    return gi.DecodedMesh(
        vertices=list(mesh.vertices),
        normals=new_norms,
        uvs=list(mesh.uvs),
        triangles=list(mesh.triangles),
    )


def _write_car_pbr_obj(
    out_path: Path,
    part_name: str,
    mesh: gi.DecodedMesh,
    part_info: Optional[gi.SolidPartInfo],
    mat_lookup: Dict[str, str],
    tex_hash_to_name: Dict[int, str],
    mtl_filename: str = "materials.mtl",
) -> Tuple[Path, Dict[str, int]]:
    """Write a Y-up DecodedMesh to OBJ with UV-aware PBR `usemtl` assignments."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        f"# NFSMW Rockport 2026 40-Car Garage Mesh (Y-up right-handed meters): {part_name}",
        f"# Vertices: {len(mesh.vertices)}, Triangles: {len(mesh.triangles)}",
        f"mtllib {mtl_filename}",
        f"o {part_name}",
    ]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    for u, v in mesh.uvs:
        lines.append(f"vt {u:.6f} {v:.6f}")
    for nx, ny, nz in mesh.normals:
        lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")

    mat_hashes = part_info.light_material_hashes if part_info else []
    tex_hashes = part_info.texture_hashes if part_info else []

    def _resolve_mtl(tx_idx: int, lm_idx: int) -> str:
        ps2_mat = ""
        if 0 <= lm_idx < len(mat_hashes):
            mh = mat_hashes[lm_idx].lower()
            ps2_mat = mat_lookup.get(mh, mh)
        tname = ""
        if 0 <= tx_idx < len(tex_hashes):
            th_str = tex_hashes[tx_idx]
            try:
                th_int = int(th_str, 16) if isinstance(th_str, str) else int(th_str)
                tname = tex_hash_to_name.get(th_int, "")
            except Exception:
                tname = ""
        return map_car_material_name(part_name, ps2_mat, tname)

    tri_with_mtl = [(_resolve_mtl(tx, lm), i0, i1, i2) for i0, i1, i2, tx, lm in mesh.triangles]
    tri_with_mtl.sort(key=lambda item: item[0])

    mat_counts: Dict[str, int] = {}
    cur_mtl: Optional[str] = None
    for mtl, i0, i1, i2 in tri_with_mtl:
        mat_counts[mtl] = mat_counts.get(mtl, 0) + 1
        if mtl != cur_mtl:
            cur_mtl = mtl
            lines.append(f"g {part_name}_{mtl}")
            lines.append(f"usemtl {mtl}")
        a, b, c = i0 + 1, i1 + 1, i2 + 1
        lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path, mat_counts


def _build_underbody_backing_mesh(
    front_z: float,
    rear_z: float,
    half_width: float,
    wheel_r: float,
    min_body_y: float,
) -> gi.DecodedMesh:
    """Build auto-fitted matte-black inner wheel-well liners + tucked underbody floor pan in Y-up."""
    verts: List[Tuple[float, float, float]] = []
    norms: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    tris: List[Tuple[int, int, int, int, int]] = []

    def _add_box(cx: float, cy: float, cz: float, hx: float, hy: float, hz: float) -> None:
        faces = [
            ((0, 0, 1), [(cx - hx, cy - hy, cz + hz), (cx + hx, cy - hy, cz + hz), (cx + hx, cy + hy, cz + hz), (cx - hx, cy + hy, cz + hz)]),
            ((0, 0, -1), [(cx + hx, cy - hy, cz - hz), (cx - hx, cy - hy, cz - hz), (cx - hx, cy + hy, cz - hz), (cx + hx, cy + hy, cz - hz)]),
            ((1, 0, 0), [(cx + hx, cy - hy, cz + hz), (cx + hx, cy - hy, cz - hz), (cx + hx, cy + hy, cz - hz), (cx + hx, cy + hy, cz + hz)]),
            ((-1, 0, 0), [(cx - hx, cy - hy, cz - hz), (cx - hx, cy - hy, cz + hz), (cx - hx, cy + hy, cz + hz), (cx - hx, cy + hy, cz - hz)]),
            ((0, 1, 0), [(cx - hx, cy + hy, cz + hz), (cx + hx, cy + hy, cz + hz), (cx + hx, cy + hy, cz - hz), (cx - hx, cy + hy, cz - hz)]),
            ((0, -1, 0), [(cx - hx, cy - hy, cz - hz), (cx + hx, cy - hy, cz - hz), (cx + hx, cy - hy, cz + hz), (cx - hx, cy - hy, cz + hz)]),
        ]
        for n, pts in faces:
            base = len(verts)
            for k, p in enumerate(pts):
                verts.append(p)
                norms.append((float(n[0]), float(n[1]), float(n[2])))
                uvs.append((float(k in (1, 2)), float(k >= 2)))
            tris.append((base, base + 1, base + 2, 0, 0))
            tris.append((base, base + 2, base + 3, 0, 0))

    floor_y = max(min_body_y + 0.055, 0.19)
    floor_hz = max((front_z - rear_z) * 0.5 + 0.22, 1.35)
    floor_cz = (front_z + rear_z) * 0.5
    floor_hx = max(half_width * 0.58, 0.48)
    _add_box(0.0, floor_y, floor_cz, floor_hx, 0.024, floor_hz)

    liner_x = max(half_width * 0.67, 0.56)
    liner_y = max(wheel_r * 1.28, 0.42)
    liner_hy = min(wheel_r * 0.46, 0.16)
    liner_hz = min(wheel_r * 0.72, 0.24)
    for sx in (-1.0, 1.0):
        _add_box(sx * liner_x, liner_y, front_z, 0.10, liner_hy, liner_hz)
        _add_box(sx * liner_x, liner_y, rear_z, 0.10, liner_hy, liner_hz)

    return gi.DecodedMesh(vertices=verts, normals=norms, uvs=uvs, triangles=tris)


# ---------------------------------------------------------------------------
# Wheel-Arch Anchor Derivation (Geometry + VLT Wheelbase Guard)
# ---------------------------------------------------------------------------


def compute_car_wheel_anchors(
    body_mesh: gi.DecodedMesh,
    body_part: gi.SolidPartInfo,
    tire_r: float,
    vlt_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """Compute millimeter-accurate PS2 wheel arch anchors via outer-fender cutout detection + VLT guard."""
    anchors = eps._compute_wheel_arch_anchors(body_mesh, body_part)
    bmin, bmax = body_part.aabb_min, body_part.aabb_max
    length = bmax[0] - bmin[0]
    half_w = anchors["half_width"]
    mid_x_body = 0.5 * (bmin[0] + bmax[0])

    # Primary geometric method: detect exact front/rear circular wheel-arch cutouts in lower outer fender skin
    # across multiple clean height/width slices (guards low race splitters like Corvette C6.R while matching VLT wheelbase).
    vlt_wb = 0.0
    if vlt_entry and isinstance(vlt_entry.get("chassis"), dict):
        vlt_wb = float(vlt_entry["chassis"].get("wheel_base", 0.0))

    cutout_candidates: List[Tuple[float, float, float]] = []  # (gap_sum, front_x, rear_x)
    slice_specs = (
        (0.80, 0.035, min(0.26, max(0.18, tire_r * 0.68))),
        (0.80, 0.045, min(0.28, max(0.22, tire_r * 0.76))),
        (0.76, 0.040, min(0.26, max(0.21, tire_r * 0.72))),
        (0.84, 0.035, min(0.26, max(0.20, tire_r * 0.70))),
    )
    for w_frac, dz_lo, dz_hi in slice_specs:
        z_lo = bmin[2] + dz_lo
        z_hi = bmin[2] + dz_hi
        outer_xs = sorted(
            x for x, y, z in body_mesh.vertices
            if abs(y) >= w_frac * half_w and z_lo <= z <= z_hi
        )
        front_cutouts: List[Tuple[float, float]] = []
        rear_cutouts: List[Tuple[float, float]] = []
        for i in range(len(outer_xs) - 1):
            x0, x1 = outer_xs[i], outer_xs[i + 1]
            gap = x1 - x0
            if 0.50 <= gap <= 1.05:
                cx = 0.5 * (x0 + x1)
                if cx > mid_x_body + 0.35:
                    front_cutouts.append((gap, cx))
                elif cx < mid_x_body - 0.35:
                    rear_cutouts.append((gap, cx))
        if front_cutouts and rear_cutouts:
            fg, fx = max(front_cutouts, key=lambda item: item[0])
            rg, rx = max(rear_cutouts, key=lambda item: item[0])
            est_wb = fx - rx
            if 2.05 <= est_wb <= 3.35:
                cutout_candidates.append((fg + rg, fx, rx))

    used_cutouts = False
    if cutout_candidates:
        if 2.1 <= vlt_wb <= 3.3:
            # Prefer a genuine geometric cutout pair closely matching the VLT chassis wheelbase
            best = min(cutout_candidates, key=lambda item: abs((item[1] - item[2]) - vlt_wb))
            if abs((best[1] - best[2]) - vlt_wb) <= 0.16:
                anchors["front_x"] = best[1]
                anchors["rear_x"] = best[2]
                used_cutouts = True
            else:
                best_gap = max(cutout_candidates, key=lambda item: item[0])
                anchors["front_x"] = best_gap[1]
                anchors["rear_x"] = best_gap[2]
                used_cutouts = True
        else:
            best_gap = max(cutout_candidates, key=lambda item: item[0])
            anchors["front_x"] = best_gap[1]
            anchors["rear_x"] = best_gap[2]
            used_cutouts = True

    # Re-measure arch_top_z directly above the detected front/rear axle centers
    arch_tops: List[float] = []
    for ax_x in (anchors["front_x"], anchors["rear_x"]):
        lip_zs = [
            z for x, y, z in body_mesh.vertices
            if abs(x - ax_x) <= 0.24 and abs(y) >= 0.85 * half_w and (bmin[2] + 0.20) <= z <= (bmin[2] + 0.72)
        ]
        if len(lip_zs) >= 3:
            arch_tops.append(max(lip_zs))
    if arch_tops:
        anchors["arch_top_z"] = min(arch_tops)

    # Sanity-check against VLT wheelbase when cutout detection wasn't conclusive
    if not used_cutouts and vlt_entry and isinstance(vlt_entry.get("chassis"), dict):
        vlt_wb = float(vlt_entry["chassis"].get("wheel_base", 0.0))
        if 2.1 <= vlt_wb <= 3.3:
            est_wb = anchors["front_x"] - anchors["rear_x"]
            if abs(est_wb - vlt_wb) > 0.28:
                mid_x = (anchors["front_x"] + anchors["rear_x"]) * 0.5
                mid_x = max(bmin[0] + 0.42 * length, min(bmax[0] - 0.42 * length, mid_x))
                anchors["front_x"] = mid_x + vlt_wb * 0.5
                anchors["rear_x"] = mid_x - vlt_wb * 0.5

    # Ensure arch_top_z respects the true wheel-arch apex lip and realistic sports/GT ride height [4.8cm .. 12.5cm]
    # (prevents fender-shoulder tuck on CLK500/Supra and monster-truck ride height on Lancer Evo VIII!)
    apex_zs: List[float] = []
    for ax_x in (anchors["front_x"], anchors["rear_x"]):
        cand = [
            z for x, y, z in body_mesh.vertices
            if abs(x - ax_x) <= 0.14 and abs(y) >= 0.86 * half_w and z > (bmin[2] + 0.34)
        ]
        if cand:
            apex_zs.append(min(cand))
    if apex_zs:
        avg_apex = sum(apex_zs) / len(apex_zs)
        anchors["arch_top_z"] = min(anchors["arch_top_z"], avg_apex + 0.095)

    # Bound resulting body ground clearance clr = bmin[2] - (arch_top_z - 2*tire_r - 0.040) to [0.048m, 0.120m]
    arch_for_min_clr = bmin[2] + 2.0 * tire_r + 0.040 - 0.048
    arch_for_max_clr = bmin[2] + 2.0 * tire_r + 0.040 - 0.120
    anchors["arch_top_z"] = max(arch_for_max_clr, min(arch_for_min_clr, anchors["arch_top_z"]))
    return anchors


def strip_occluding_light_decals(
    body_mesh: gi.DecodedMesh,
    body_part: gi.SolidPartInfo,
    light_parts: Sequence[gi.SolidPartInfo],
    mat_lookup: Dict[str, str],
) -> Tuple[gi.DecodedMesh, int]:
    """Remove EA empty/transparent DECAL cover quads floating directly in front of head/taillight lenses.

    On vehicles like the Ford GT, KIT00_BODY_B includes DECAL (BADGING) placeholder quads ~3cm
    outside the round taillight lens whose badging texture is nearly 100% transparent. In an
    opaque car-body BLAS, those quads would paint solid body color over the glowing taillights.
    """
    if not light_parts or not body_mesh.triangles:
        return body_mesh, 0
    mat_hashes = body_part.light_material_hashes
    light_boxes: List[Tuple[float, float, float, float, float, float]] = []
    for lp in light_parts:
        pu = lp.name.upper()
        if "BRAKELIGHT" in pu or "HEADLIGHT" in pu:
            a, b = lp.aabb_min, lp.aabb_max
            light_boxes.append((a[0] - 0.08, b[0] + 0.08, a[1] - 0.03, b[1] + 0.03, a[2] - 0.03, b[2] + 0.03))
    if not light_boxes:
        return body_mesh, 0

    kept: List[Tuple[int, int, int, int, int]] = []
    stripped = 0
    for tri in body_mesh.triangles:
        i0, i1, i2, tx, lm = tri
        ps2_mat = ""
        if 0 <= lm < len(mat_hashes):
            mh = mat_hashes[lm].lower()
            ps2_mat = mat_lookup.get(mh, mh).upper()
        if ps2_mat == "DECAL":
            v0, v1, v2 = body_mesh.vertices[i0], body_mesh.vertices[i1], body_mesh.vertices[i2]
            cx = (v0[0] + v1[0] + v2[0]) / 3.0
            cy = (v0[1] + v1[1] + v2[1]) / 3.0
            cz = (v0[2] + v1[2] + v2[2]) / 3.0
            if any(x0 <= cx <= x1 and y0 <= cy <= y1 and z0 <= cz <= z1 for x0, x1, y0, y1, z0, z1 in light_boxes):
                stripped += 1
                continue
        kept.append(tri)
    if stripped == 0:
        return body_mesh, 0
    return (
        gi.DecodedMesh(
            vertices=list(body_mesh.vertices),
            normals=list(body_mesh.normals),
            uvs=list(body_mesh.uvs),
            triangles=kept,
        ),
        stripped,
    )


# ---------------------------------------------------------------------------
# Main ISO Extraction Pipeline (Single Car in ~1-2 Seconds!)
# ---------------------------------------------------------------------------


def _select_kit00_parts(pack_info: gi.GeometryPackInfo, car_name: str, include_base: bool) -> List[gi.SolidPartInfo]:
    """Select all clean KIT00 LOD_B parts (body, base, hood, spoiler, mirrors, glass, lights, interior)."""
    car_u = car_name.upper()
    by_name = {p.name.upper(): p for p in pack_info.parts}
    selected: List[gi.SolidPartInfo] = []
    seen: set[int] = set()

    def _add_if(name: str) -> None:
        p = by_name.get(name.upper())
        if p is not None and p.index not in seen:
            seen.add(p.index)
            selected.append(p)

    # Primary body + optional base chassis (Carrera GT V10 deck, Gallardo vents, etc.)
    _add_if(f"{car_u}_KIT00_BODY_B")
    if not selected:
        # Fallback to highest-LOD body
        for p in pack_info.parts:
            if p.name.upper().endswith("_BODY_B") and "DAMAGE" not in p.name.upper():
                _add_if(p.name)
                break
    if include_base:
        _add_if(f"{car_u}_BASE_B")

    standard_suffixes = [
        "KIT00_HOOD_B",
        "KIT00_SPOILER_B",
        "KIT00_LEFT_SIDE_MIRROR_B",
        "KIT00_RIGHT_SIDE_MIRROR_B",
        "KIT00_INTERIOR_B",
        "KIT00_FRONT_WINDOW_B",
        "KIT00_REAR_WINDOW_B",
        "KIT00_FRONT_LEFT_WINDOW_B",
        "KIT00_FRONT_RIGHT_WINDOW_B",
        "KIT00_REAR_LEFT_WINDOW_B",
        "KIT00_REAR_RIGHT_WINDOW_B",
        "KIT00_LEFT_HEADLIGHT_B",
        "KIT00_RIGHT_HEADLIGHT_B",
        "KIT00_LEFT_HEADLIGHT_GLASS_B",
        "KIT00_RIGHT_HEADLIGHT_GLASS_B",
        "KIT00_LEFT_BRAKELIGHT_B",
        "KIT00_RIGHT_BRAKELIGHT_B",
        "KIT00_LEFT_BRAKELIGHT_GLASS_B",
        "KIT00_RIGHT_BRAKELIGHT_GLASS_B",
        "KIT00_EXHAUST_B",
        "KIT00_ROOF_B",
    ]
    for suf in standard_suffixes:
        _add_if(f"{car_u}_{suf}")
    return selected


def _load_light_material_lookup(arc: PS2NFSMWArchive) -> Dict[str, str]:
    mat_lookup: Dict[str, str] = dict(eps.BUILTIN_LIGHT_MATERIAL_NAMES)
    ent = arc.find("GLOBAL\\GLOBALB.LZC")
    if ent is not None:
        try:
            gb_raw = jdlz_decompress(arc.read_entry(ent))
            chunks = iter_top_level_bchunks(gb_raw)
            for lm in parse_light_materials(gb_raw, chunks):
                mat_lookup[f"0x{bstringhash(lm.name):08x}"] = lm.name
                mat_lookup[f"0x{lm.name_hash:08x}"] = lm.name
        except Exception:
            pass
    return mat_lookup


def _extract_m3_livery_mask(arc: PS2NFSMWArchive, out_tex_dir: Path) -> Optional[Path]:
    """Ensure authentic M3 GTR PSRTEST_MASK vinyl exists in `out_tex_dir`."""
    dst = out_tex_dir / "m3_livery_mask.png"
    existing = REPO_ROOT / "build" / "flywheel" / "hq_m3_gtr" / "textures" / "m3_livery_mask.png"
    if existing.is_file():
        dst.write_bytes(existing.read_bytes())
        return dst
    ent = arc.find("CARS\\BMWM3GTRE46\\VINYLS.BIN")
    if ent is None:
        return None
    try:
        raw = arc.read_entry(ent)
        texs = te.extract_tpk_bytes(raw, source="BMWM3GTRE46_VINYLS", out_dir=None, keep_images=True)
        for t in texs:
            if "MASK" in t.name.upper() and t.image is not None:
                t.image.convert("L").save(dst, format="PNG")
                return dst
    except Exception:
        return None
    return None


def extract_rt_car(
    car_name: str,
    *,
    iso_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    archive: Optional[PS2NFSMWArchive] = None,
    mat_lookup: Optional[Dict[str, str]] = None,
    subdivide: bool = True,
) -> Dict[str, Any]:
    """Extract a single playable NFSMW car from the PS2 ISO into `build/car_cache/<CAR>/`."""
    t0 = time.perf_counter()
    canon = canonicalize_car_name(car_name)
    spec = PLAYABLE_CAR_CATALOG[canon]
    dest = out_dir if out_dir is not None else (DEFAULT_CACHE_ROOT / canon)

    if archive is None:
        resolved_iso = resolve_iso_path(iso_path)
        if resolved_iso is None:
            raise FileNotFoundError(
                "PS2 ISO not found (set NFSMW_ISO, pass --iso, or use --synthetic)"
            )
        archive = PS2NFSMWArchive(resolved_iso)

    if mat_lookup is None:
        mat_lookup = _load_light_material_lookup(archive)

    geo_ent = archive.find(f"CARS\\{canon}\\GEOMETRY.BIN")
    tex_ent = archive.find(f"CARS\\{canon}\\TEXTURES.BIN")
    if geo_ent is None or tex_ent is None:
        raise FileNotFoundError(f"Missing GEOMETRY.BIN or TEXTURES.BIN for {canon} in ISO")

    geo_raw = archive.read_entry(geo_ent)
    tex_raw = archive.read_entry(tex_ent)

    pack_info, _ = gi.parse_geometry_pack(geo_raw, path=f"CARS\\{canon}\\GEOMETRY.BIN")
    by_name = {p.name.upper(): p for p in pack_info.parts}

    # 1. Extract & hash-match authentic OEM textures
    tex_dir = dest / "textures"
    tex_dir.mkdir(parents=True, exist_ok=True)
    decoded_texs = te.extract_tpk_bytes(tex_raw, source=canon, out_dir=None, keep_images=True)
    tex_hash_to_name = {t.name_hash: t.name for t in decoded_texs}
    matched_slots = match_car_textures(canon, decoded_texs)

    textures_manifest: Dict[str, str] = {
        "asphalt_wet": "build/cc0_baked/asphalt_02_wet_4k.png",
        "carbon_fiber": "build/cc0_baked/carbon_fiber_4k.png",
    }
    slug = spec["alias"]
    slot_file_suffix = {
        "car_misc": "misc.png",
        "car_badging": "badging.png",
        "car_headlight": "headlight.png",
        "car_taillight": "taillight.png",
        "car_tire": "tire.png",
        "car_interior": "interior.png",
    }
    for slot, tex in matched_slots.items():
        if tex.image is None:
            continue
        out_png = tex_dir / f"{slug}_{slot_file_suffix[slot]}"
        upscaled = _upscale_car_texture(tex.image, slot)
        upscaled.save(out_png, format="PNG")
        try:
            textures_manifest[slot] = str(out_png.resolve().relative_to(REPO_ROOT.resolve()))
        except ValueError:
            textures_manifest[slot] = str(out_png)

    if canon == "BMWM3GTRE46":
        mask_p = _extract_m3_livery_mask(archive, tex_dir)
        if mask_p is not None:
            try:
                textures_manifest["car_livery_mask"] = str(mask_p.resolve().relative_to(REPO_ROOT.resolve()))
            except ValueError:
                textures_manifest["car_livery_mask"] = str(mask_p)

    # 2. Decode selected car parts + OEM stock wheel + brakes
    selected_parts = _select_kit00_parts(pack_info, canon, include_base=bool(spec.get("include_base_mesh", True)))
    wheel_part = by_name.get(f"{canon}_KIT00_FRONT_TIRE_B") or by_name.get(f"{canon}_KIT00_WHEEL_B")
    rear_wheel_part = by_name.get(f"{canon}_KIT00_REAR_TIRE_B")
    front_brake_part = by_name.get(f"{canon}_KIT00_FRONT_BRAKE_B")
    rear_brake_part = by_name.get(f"{canon}_KIT00_REAR_BRAKE_B") or front_brake_part

    decode_indices = [p.index for p in selected_parts]
    for extra in (wheel_part, rear_wheel_part, front_brake_part, rear_brake_part):
        if extra is not None and extra.index not in decode_indices:
            decode_indices.append(extra.index)

    _, decoded_meshes = gi.parse_geometry_pack(geo_raw, decode_parts=decode_indices)

    # Fallback wheel from WHEELS\GEOMETRY.BIN if a car lacks its own stock tire solid
    external_wheel_mesh: Optional[gi.DecodedMesh] = None
    if wheel_part is None:
        w_ent = archive.find("CARS\\WHEELS\\GEOMETRY.BIN")
        if w_ent is not None:
            w_raw = archive.read_entry(w_ent)
            w_info, _ = gi.parse_geometry_pack(w_raw, path="CARS\\WHEELS\\GEOMETRY.BIN")
            wheel_part = next(
                (p for p in w_info.parts if p.name.upper() == "ENKEI_STYLE01_17_25_B"),
                w_info.parts[0],
            )
            _, w_meshes = gi.parse_geometry_pack(w_raw, decode_parts=[wheel_part.index])
            external_wheel_mesh = w_meshes[wheel_part.index]

    body_part = selected_parts[0]
    body_mesh_ps2 = decoded_meshes[body_part.index]

    def _maybe_subdivide(mesh_ps2: gi.DecodedMesh, pname: str) -> gi.DecodedMesh:
        if not subdivide or len(mesh_ps2.triangles) == 0:
            return mesh_ps2
        pu = pname.upper()
        if any(k in pu for k in ("WINDOW", "HEADLIGHT", "BRAKELIGHT", "INTERIOR", "BRAKE")):
            return mesh_ps2
        mode = "pn"
        if "MIRROR" in pu:
            # Loop subdivision relaxes coarse PS2 mirror shells into smooth aerodynamic teardrops (zero PN pinching!)
            mode = "loop"
            levels, crease = (2 if len(mesh_ps2.triangles) <= 500 else 1), 68.0
        elif "SPOILER" in pu or "HOOD" in pu:
            levels, crease = (2 if len(mesh_ps2.triangles) <= 500 else 1), 44.0
        elif "TIRE" in pu or "WHEEL" in pu:
            levels, crease = 1, 40.0
        elif "BODY" in pu or "BASE" in pu:
            levels, crease = 1, 42.0
        else:
            return mesh_ps2
        comp, _ = scm.subdivide_compiled_mesh(
            _decoded_to_compiled(mesh_ps2),
            crease_deg=crease,
            mode=mode,
            levels=levels,
            preserve_aabb=True,
            lock_material_seams=True,
        )
        return _compiled_to_decoded(comp)

    raw_wheel_mesh = external_wheel_mesh if external_wheel_mesh is not None else decoded_meshes[wheel_part.index]
    raw_wheel_mesh, wheel_quads_stripped = filter_wheel_billboard_quads(raw_wheel_mesh, wheel_part, mat_lookup)
    hq_wheel_ps2 = _maybe_subdivide(raw_wheel_mesh, wheel_part.name)
    rear_w_ps2 = hq_wheel_ps2
    rear_w_info = wheel_part
    if rear_wheel_part is not None and rear_wheel_part.index in decoded_meshes:
        rw_clean, _ = filter_wheel_billboard_quads(decoded_meshes[rear_wheel_part.index], rear_wheel_part, mat_lookup)
        rear_w_ps2 = _maybe_subdivide(rw_clean, rear_wheel_part.name)
        rear_w_info = rear_wheel_part

    wheel_r = max(abs(wheel_part.aabb_min[2]), abs(wheel_part.aabb_max[2]), 0.31)
    vlt_table = _load_vlt_table()
    vlt_entry = vlt_table.get(canon.lower())
    anchors = compute_car_wheel_anchors(body_mesh_ps2, body_part, wheel_r, vlt_entry=vlt_entry)

    # Compute exact wheel Z & ground offset from the SUBDIVIDED wheels so all 4 tires seat at Y=0.0000m!
    sub_wheel_zs = [v[2] for v in hq_wheel_ps2.vertices] + [v[2] for v in rear_w_ps2.vertices]
    wheel_min_z = min(sub_wheel_zs) if sub_wheel_zs else -wheel_r
    arch_clearance = 0.040
    wheel_z = anchors["arch_top_z"] - wheel_r - arch_clearance
    ground_z_ps2 = wheel_z + wheel_min_z

    outer_face_y = anchors["half_width"] * 0.972
    wheel_y_offset = outer_face_y + wheel_part.aabb_min[1]

    # 3. Subdivide, smooth normals, transform to Y-up, and write OBJs
    meshes_dir = dest / "meshes"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    eps.write_mtl_library(meshes_dir / "materials.mtl")

    scene_parts: List[Dict[str, Any]] = []
    total_verts = 0
    total_tris = 0

    def _emit_part(
        obj_name: str,
        ymesh: gi.DecodedMesh,
        pinfo: Optional[gi.SolidPartInfo],
        role: str,
        transparent: bool = False,
        visible: bool = True,
    ) -> None:
        nonlocal total_verts, total_tris
        obj_rel = f"meshes/{obj_name}.obj"
        _, mat_counts = _write_car_pbr_obj(
            meshes_dir / f"{obj_name}.obj", obj_name, ymesh, pinfo, mat_lookup, tex_hash_to_name
        )
        total_verts += len(ymesh.vertices)
        total_tris += len(ymesh.triangles)
        scene_parts.append(
            {
                "name": obj_name,
                "obj": obj_rel,
                "role": role,
                "transparent": transparent,
                "visible": visible,
                "vertex_count": len(ymesh.vertices),
                "triangle_count": len(ymesh.triangles),
                "materials": mat_counts,
            }
        )

    body_min_y = 0.14
    light_parts_all = [p for p in selected_parts if "BRAKELIGHT" in p.name.upper() or "HEADLIGHT" in p.name.upper()]
    for part in selected_parts:
        raw_ps2 = decoded_meshes.get(part.index)
        if raw_ps2 is None or not raw_ps2.triangles:
            continue
        pu = part.name.upper()
        if "BODY" in pu:
            raw_ps2, _ = strip_occluding_light_decals(raw_ps2, part, light_parts_all, mat_lookup)
        if any(k in pu for k in ("BODY", "BASE", "HOOD", "SPOILER")):
            # For non-M3 OEM cars (e.g. Carrera GT), unify BOTH (tx, lm) of DIABLOHP (DOORLINE) into CARBODY
            # BEFORE PN subdivision so lock_material_seams never creates an artificial fender-door seam!
            if not canon.startswith("BMWM3") and part.light_material_hashes:
                carbody_lm = next(
                    (i for i, h in enumerate(part.light_material_hashes) if mat_lookup.get(h.lower(), "").upper() == "CARBODY"),
                    None,
                )
                diablo_lms = {
                    i for i, h in enumerate(part.light_material_hashes) if mat_lookup.get(h.lower(), "").upper() == "DIABLOHP"
                }
                if carbody_lm is not None and diablo_lms:
                    carbody_tx = next((tx for _i0, _i1, _i2, tx, lm in raw_ps2.triangles if lm == carbody_lm), 0)
                    raw_ps2 = gi.DecodedMesh(
                        vertices=list(raw_ps2.vertices),
                        normals=list(raw_ps2.normals),
                        uvs=list(raw_ps2.uvs),
                        triangles=[
                            (i0, i1, i2, carbody_tx if lm in diablo_lms else tx, carbody_lm if lm in diablo_lms else lm)
                            for i0, i1, i2, tx, lm in raw_ps2.triangles
                        ],
                    )
        src_ps2 = _maybe_subdivide(raw_ps2, part.name)
        ymesh = eps.transform_ps2_mesh_to_yup(src_ps2, ground_z_ps2=ground_z_ps2)
        if "MIRROR" in pu:
            # Outward-consistent smooth welding on Loop-subdivided mirrors eliminates wrinkled-foil PN facets
            ymesh = weld_smooth_normals_yup(
                ymesh, crease_deg=68.0, part_info=part, mat_lookup=mat_lookup, only_paint_materials=False, laplacian_iters=2
            )
        elif any(k in pu for k in ("BODY", "BASE", "HOOD", "SPOILER")):
            # Outward-consistent cross-material normal weld + 12-pass crease-locked Laplacian diffusion
            # cures Carrera GT's flat fender-door luminance jump & A-pillar flatness while keeping vents/lips sharp!
            ymesh = weld_smooth_normals_yup(
                ymesh, crease_deg=50.0, part_info=part, mat_lookup=mat_lookup, only_paint_materials=True, laplacian_iters=12
            )
            if "BODY" in pu and ymesh.vertices:
                body_min_y = min(v[1] for v in ymesh.vertices)
        is_glass = ("WINDOW" in pu) or ("HEADLIGHT_GLASS" in pu)
        role = (
            "glass"
            if is_glass
            else ("lights" if ("HEADLIGHT" in pu or "BRAKELIGHT" in pu) else "body")
        )
        _emit_part(part.name, ymesh, part, role=role, transparent=is_glass, visible=True)

    # Emit 4x OEM wheels + front/rear brakes (already subdivided above for exact Y=0.0000m grounding)

    wheel_specs = [
        ("WHEEL_FL", (anchors["front_x"], +wheel_y_offset, wheel_z), True, hq_wheel_ps2, wheel_part, front_brake_part),
        ("WHEEL_FR", (anchors["front_x"], -wheel_y_offset, wheel_z), False, hq_wheel_ps2, wheel_part, front_brake_part),
        ("WHEEL_RL", (anchors["rear_x"], +wheel_y_offset, wheel_z), True, rear_w_ps2, rear_w_info, rear_brake_part),
        ("WHEEL_RR", (anchors["rear_x"], -wheel_y_offset, wheel_z), False, rear_w_ps2, rear_w_info, rear_brake_part),
    ]
    for wtag, pos_ps2, rot180, wmesh_src, winfo, bpart in wheel_specs:
        wmesh_yup = eps.transform_ps2_mesh_to_yup(
            wmesh_src, offset_ps2=pos_ps2, rotate_z_180_ps2=rot180, ground_z_ps2=ground_z_ps2
        )
        _emit_part(f"{canon}_STOCK_{wtag}", wmesh_yup, winfo, role="wheel", visible=True)
        if bpart is not None and bpart.index in decoded_meshes:
            inboard = -0.045 if pos_ps2[1] > 0 else +0.045
            bpos = (pos_ps2[0], pos_ps2[1] + inboard, pos_ps2[2])
            bmesh_yup = eps.transform_ps2_mesh_to_yup(
                decoded_meshes[bpart.index], offset_ps2=bpos, rotate_z_180_ps2=rot180, ground_z_ps2=ground_z_ps2
            )
            _emit_part(f"{bpart.name}_{wtag}", bmesh_yup, bpart, role="brake", visible=True)

    # Auto-fitted inner wheel-well liners + tucked underbody floor pan
    backing_yup = _build_underbody_backing_mesh(
        front_z=anchors["front_x"],
        rear_z=anchors["rear_x"],
        half_width=anchors["half_width"],
        wheel_r=wheel_r,
        min_body_y=body_min_y,
    )
    backing_info = gi.SolidPartInfo(
        index=999, name=f"{canon}_UNDERBODY_BACKING", name_hash="0x00000000", lod="B", version=1, flags=0,
        header_polys=0, num_bones=0, num_textures=0, num_light_materials=1, num_markers=0,
        aabb_min=[-1, -1, 0], aabb_max=[1, 1, 1], volume=1.0, density=1.0,
        texture_hashes=[], light_material_hashes=["0x52bf4c34"],
        plat_version=0, strip_flags=0, strip_count=0, strip_data_bytes=0,
        strip_vertex_total=len(backing_yup.vertices), strip_poly_total=len(backing_yup.triangles), chunk_offset=0,
    )
    _emit_part(f"{canon}_UNDERBODY_BACKING", backing_yup, backing_info, role="body", visible=True)

    wheel_bounds = {
        "front_axle_z": round(float(anchors["front_x"]), 4),
        "rear_axle_z": round(float(anchors["rear_x"]), 4),
        "wheel_radius_f": round(float(wheel_r), 4),
        "wheel_radius_r": round(float(wheel_r), 4),
        "half_track_f": round(float(abs(wheel_y_offset)), 4),
        "half_track_r": round(float(abs(wheel_y_offset)), 4),
        "body_half_width": round(float(anchors["half_width"]), 4),
        "body_length": round(float(anchors["length"]), 4),
        "ground_y": 0.0,
    }

    elapsed = time.perf_counter() - t0
    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "coordinate_system": "yup_right_handed_meters",
        "mode": f"rt_garage_{slug}",
        "car_id": canon,
        "car_alias": slug,
        "car_name": f"{spec['display_name']} — Authentic NFSMW 2005 (SLUS-21351)",
        "wheel_bounds": wheel_bounds,
        "summary": {
            "part_count": len(scene_parts),
            "total_vertices": total_verts,
            "total_triangles": total_tris,
            "wheel_billboard_tris_stripped": wheel_quads_stripped,
            "textures_matched": sorted(matched_slots.keys()),
            "elapsed_seconds": round(elapsed, 3),
        },
        "camera": {
            "target": [0.0, 0.50, round((anchors["front_x"] + anchors["rear_x"]) * 0.25, 3)],
            "distance": 4.75,
            "azimuth_deg": 36.0,
            "elevation_deg": 7.6,
            "fov_deg": 36.0,
        },
        "textures": textures_manifest,
        "materials": {
            "car_paint_hero": {
                "kind": "car_paint",
                "base_color": list(spec["base_color"]),
                "secondary_color": list(spec["secondary_color"]),
                "metallic": float(spec["metallic"]),
                "roughness": float(spec["roughness"]),
                "clearcoat": 1.0,
                "clearcoat_roughness": 0.014,
                "ior": 1.56,
                "livery_enabled": bool(spec["livery_enabled"]),
            }
        },
        "parts": scene_parts,
    }

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "scene.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def extract_synthetic_rt_car(car_name: str, out_dir: Path) -> Dict[str, Any]:
    """Offline synthetic car extractor for CI tests when no PS2 ISO is mounted."""
    canon = canonicalize_car_name(car_name)
    spec = PLAYABLE_CAR_CATALOG[canon]
    meshes_dir = out_dir / "meshes"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    eps.write_mtl_library(meshes_dir / "materials.mtl")

    body = eps._make_box_mesh(0.0, 0.55, 0.0, 1.84, 0.72, 4.30, mat_idx=0)
    body_info = gi.SolidPartInfo(
        index=0, name=f"{canon}_KIT00_BODY_B", name_hash="0x00000001", lod="B", version=1, flags=0,
        header_polys=0, num_bones=0, num_textures=0, num_light_materials=1, num_markers=0,
        aabb_min=[-2.15, -0.92, 0.14], aabb_max=[2.15, 0.92, 1.12], volume=1.0, density=1.0,
        texture_hashes=[], light_material_hashes=["0xd6d6080a"],
        plat_version=0, strip_flags=0, strip_count=0, strip_data_bytes=0,
        strip_vertex_total=len(body.vertices), strip_poly_total=len(body.triangles), chunk_offset=0,
    )
    parts: List[Dict[str, Any]] = []
    total_v = total_t = 0
    _, mc = _write_car_pbr_obj(meshes_dir / f"{canon}_KIT00_BODY_B.obj", f"{canon}_KIT00_BODY_B", body, body_info, eps.BUILTIN_LIGHT_MATERIAL_NAMES, {})
    total_v += len(body.vertices)
    total_t += len(body.triangles)
    parts.append({"name": f"{canon}_KIT00_BODY_B", "obj": f"meshes/{canon}_KIT00_BODY_B.obj", "role": "body", "transparent": False, "visible": True, "vertex_count": len(body.vertices), "triangle_count": len(body.triangles), "materials": mc})

    wheel_info = dataclasses.replace(body_info, name=f"{canon}_STOCK_WHEEL", light_material_hashes=["0x78743ca1"])
    for wtag, (wx, wz) in [("WHEEL_FL", (-0.82, 1.35)), ("WHEEL_FR", (0.82, 1.35)), ("WHEEL_RL", (-0.82, -1.35)), ("WHEEL_RR", (0.82, -1.35))]:
        wm = eps._make_cylinder_wheel(wx, 0.33, wz, 0.33, 0.24, segs=18)
        _, wmc = _write_car_pbr_obj(meshes_dir / f"{canon}_STOCK_{wtag}.obj", f"{canon}_STOCK_{wtag}", wm, wheel_info, eps.BUILTIN_LIGHT_MATERIAL_NAMES, {})
        total_v += len(wm.vertices)
        total_t += len(wm.triangles)
        parts.append({"name": f"{canon}_STOCK_{wtag}", "obj": f"meshes/{canon}_STOCK_{wtag}.obj", "role": "wheel", "transparent": False, "visible": True, "vertex_count": len(wm.vertices), "triangle_count": len(wm.triangles), "materials": wmc})

    manifest = {
        "schema_version": 1,
        "coordinate_system": "yup_right_handed_meters",
        "mode": f"synthetic_{spec['alias']}",
        "car_id": canon,
        "car_alias": spec["alias"],
        "car_name": f"{spec['display_name']} (Synthetic)",
        "wheel_bounds": {
            "front_axle_z": 1.35, "rear_axle_z": -1.35,
            "wheel_radius_f": 0.33, "wheel_radius_r": 0.33,
            "half_track_f": 0.82, "half_track_r": 0.82,
            "body_half_width": 0.92, "body_length": 4.30, "ground_y": 0.0,
        },
        "summary": {"part_count": len(parts), "total_vertices": total_v, "total_triangles": total_t, "wheel_billboard_tris_stripped": 0, "textures_matched": []},
        "camera": {"target": [0.0, 0.50, 0.0], "distance": 4.75, "azimuth_deg": 36.0, "elevation_deg": 7.6, "fov_deg": 36.0},
        "textures": {},
        "materials": {"car_paint_hero": {"kind": "car_paint", "base_color": list(spec["base_color"]), "secondary_color": list(spec["secondary_color"]), "metallic": float(spec["metallic"]), "roughness": float(spec["roughness"]), "clearcoat": 1.0, "clearcoat_roughness": 0.014, "ior": 1.56, "livery_enabled": bool(spec["livery_enabled"])}},
        "parts": parts,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scene.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_all_40_manifest(iso_path: Optional[Path] = None) -> Dict[str, Any]:
    """Verify all 40 playable cars exist in ISO with valid GEOMETRY.BIN + TEXTURES.BIN (or offline catalog check)."""
    resolved_iso = resolve_iso_path(iso_path)
    if resolved_iso is None:
        return {
            "mode": "offline_catalog",
            "total_catalog": len(PLAYABLE_CAR_CATALOG),
            "verified_in_iso": 0,
            "cars": sorted(PLAYABLE_CAR_CATALOG.keys()),
        }

    arc = PS2NFSMWArchive(resolved_iso)
    results: List[Dict[str, Any]] = []
    missing: List[str] = []
    for canon in sorted(PLAYABLE_CAR_CATALOG.keys()):
        g_ent = arc.find(f"CARS\\{canon}\\GEOMETRY.BIN")
        t_ent = arc.find(f"CARS\\{canon}\\TEXTURES.BIN")
        if g_ent is None or t_ent is None:
            missing.append(canon)
            continue
        g_raw = arc.read_entry(g_ent)
        t_raw = arc.read_entry(t_ent)
        g_info, _ = gi.parse_geometry_pack(g_raw, path=canon)
        texs = te.extract_tpk_bytes(t_raw, source=canon, out_dir=None, keep_images=False)
        slots = match_car_textures(canon, texs)
        has_body = any(p.name.upper().endswith("_BODY_B") for p in g_info.parts)
        results.append(
            {
                "car": canon,
                "display_name": PLAYABLE_CAR_CATALOG[canon]["display_name"],
                "parts": len(g_info.parts),
                "has_body_b": has_body,
                "textures": len(texs),
                "matched_slots": sorted(slots.keys()),
            }
        )
    return {
        "mode": "iso_verified",
        "total_catalog": len(PLAYABLE_CAR_CATALOG),
        "verified_in_iso": len(results),
        "missing": missing,
        "cars": results,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="On-demand 40-car playable garage extractor for RockportMetalRT.")
    p.add_argument("--iso", type=Path, default=None, help="Path to untouched PS2 ISO (read-only 'rb').")
    p.add_argument("--car", type=str, default=None, help="Single playable car name or alias to extract.")
    p.add_argument("--cars", nargs="+", default=None, help="Multiple playable car names/aliases to extract.")
    p.add_argument("--top15", action="store_true", help="Extract all Top-15 flagship & Blacklist cars into cache.")
    p.add_argument("--all", action="store_true", help="Extract all 40 playable cars into cache.")
    p.add_argument("--out-dir", type=Path, default=None, help="Override output directory (single car) or cache root.")
    p.add_argument("--list", action="store_true", help="List all 40 playable cars and aliases.")
    p.add_argument("--verify-all-manifest", action="store_true", help="Verify all 40 playable cars in ISO.")
    p.add_argument("--no-subdivide", action="store_true", help="Skip PN subdivision for ultra-fast preview.")
    p.add_argument("--synthetic", action="store_true", help="Generate offline synthetic car without ISO.")
    p.add_argument("--json", action="store_true", help="Emit JSON summary to stdout.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.list:
        entries = [
            {"car": c, "alias": m["alias"], "display_name": m["display_name"]}
            for c, m in PLAYABLE_CAR_CATALOG.items()
        ]
        if args.json:
            print(json.dumps({"count": len(entries), "cars": entries}, indent=2))
        else:
            print(f"NFSMW Playable Garage Catalog ({len(entries)} cars):")
            for i, e in enumerate(entries, 1):
                print(f"  {i:2d}. {e['car']:14s} ({e['alias']:14s}) — {e['display_name']}")
        return 0

    if args.verify_all_manifest:
        report = verify_all_40_manifest(args.iso)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"40-Car Garage Manifest ({report['mode']}): "
                f"{report['verified_in_iso']}/{report['total_catalog']} verified"
                + (f", missing={report['missing']}" if report.get("missing") else "")
            )
            if report["mode"] == "iso_verified":
                for r in report["cars"]:
                    print(
                        f"  [OK] {r['car']:14s} parts={r['parts']:3d} bodyB={r['has_body_b']} "
                        f"tex={r['textures']:2d} slots={','.join(r['matched_slots'])}"
                    )
        return 0 if not report.get("missing") else 1

    targets: List[str] = []
    if args.all:
        targets = list(PLAYABLE_CAR_CATALOG.keys())
    elif args.top15:
        targets = list(TOP15_FLAGSHIP_CARS)
    elif args.cars:
        targets = [canonicalize_car_name(c) for c in args.cars]
    elif args.car:
        targets = [canonicalize_car_name(args.car)]
    else:
        print("Specify --list, --verify-all-manifest, --car <NAME>, --cars ..., --top15, or --all.", file=sys.stderr)
        return 2

    if args.synthetic:
        manifests = []
        for canon in targets:
            dest = (
                args.out_dir
                if (args.out_dir is not None and len(targets) == 1)
                else ((args.out_dir or DEFAULT_CACHE_ROOT) / canon)
            )
            manifests.append(extract_synthetic_rt_car(canon, dest))
        if args.json:
            print(json.dumps(manifests if len(manifests) > 1 else manifests[0], indent=2))
        else:
            for m in manifests:
                print(f"[SYNTH] {m['car_id']:14s} tris={m['summary']['total_triangles']:,} -> {m['car_name']}")
        return 0

    resolved_iso = resolve_iso_path(args.iso)
    if resolved_iso is None:
        raise FileNotFoundError("PS2 ISO not found (set NFSMW_ISO, pass --iso, or use --synthetic)")
    arc = PS2NFSMWArchive(resolved_iso)
    mat_lookup = _load_light_material_lookup(arc)

    manifests = []
    for canon in targets:
        dest = (
            args.out_dir
            if (args.out_dir is not None and len(targets) == 1)
            else ((args.out_dir or DEFAULT_CACHE_ROOT) / canon)
        )
        m = extract_rt_car(
            canon,
            archive=arc,
            mat_lookup=mat_lookup,
            out_dir=dest,
            subdivide=not args.no_subdivide,
        )
        manifests.append(m)
        if not args.json:
            s = m["summary"]
            wb = m["wheel_bounds"]
            print(
                f"[OK] {canon:14s} parts={s['part_count']:2d} tris={s['total_triangles']:6,d} "
                f"tex={len(s['textures_matched'])}/6 axleZ=[{wb['rear_axle_z']:.2f},{wb['front_axle_z']:.2f}] "
                f"r={wb['wheel_radius_f']:.3f}m ({s['elapsed_seconds']:.2f}s) -> {dest}"
            )
    if args.json:
        print(json.dumps(manifests if len(manifests) > 1 else manifests[0], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
