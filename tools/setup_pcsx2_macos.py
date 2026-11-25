#!/usr/bin/env python3
"""
setup_pcsx2_macos.py — Non-destructive PCSX2 v2.6.3 configurator for macOS Apple Silicon.

NEVER modifies your global ~/Library/Application Support/PCSX2/inis/PCSX2.ini or saves!
Instead, it installs:
  1. Verified per-CRC `.pnach` patches into ~/Library/Application Support/PCSX2/patches/
  2. Per-game Apple Silicon 4K/Metal profiles into ~/Library/Application Support/PCSX2/gamesettings/
  3. Empty texture dump & replacement folders at ~/Library/Application Support/PCSX2/textures/SLUS-21351/

All INI section names, key names, and enum values below have been directly audited
against PCSX2 v2.6.3 (Pcsx2Config::LoadSaveCore, OpenGSDevice, Patch::UpdateActivePatches,
and GameIndex.yaml for SLUS-21351).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Verified directly from PCSX2 v2.6.3 OpenGSDevice / FullscreenUI::s_renderer_values
RENDERER_IDS: Dict[str, int] = {
    "auto": -1,      # Automatic (Default -> resolves to Metal 17 on macOS)
    "null": 11,      # Null
    "software": 13,  # Software Renderer
    "vulkan": 14,    # Vulkan (MoltenVK on macOS)
    "metal": 17,     # Metal (native macOS GPU backend)
}

VALID_ANISOTROPY = (0, 2, 4, 8, 16)
VALID_ASPECT_RATIOS = ("Stretch", "Auto 4:3/3:2", "4:3", "16:9", "10:7")
VALID_FMV_ASPECT_RATIOS = ("Off", "Auto 4:3/3:2", "4:3", "16:9", "10:7")

# Exact case-sensitive schema verified against PCSX2 v2.6.3 Pcsx2Config::LoadSaveCore
# and Patch::PATCHES_CONFIG_SECTION / PATCH_ENABLE_CONFIG_KEY.
PCSX2_V263_SCHEMA: Dict[str, Dict[str, str]] = {
    "EmuCore": {
        "EnablePatches": "bool",
        "EnableWideScreenPatches": "bool",
        "EnableNoInterlacingPatches": "bool",
        "EnableGameFixes": "bool",
    },
    "EmuCore/Speedhacks": {
        "EECycleRate": "int:-3:3",
    },
    "EmuCore/GS": {
        "Renderer": "enum_int:-1,11,13,14,17",
        "AspectRatio": "enum_str:Stretch,Auto 4:3/3:2,4:3,16:9,10:7",
        "FMVAspectRatioSwitch": "enum_str:Off,Auto 4:3/3:2,4:3,16:9,10:7",
        "upscale_multiplier": "float:1:25",
        "MaxAnisotropy": "enum_int:0,2,4,8,16",
        "mipmap": "bool",
        "hw_mipmap": "bool",
        "dithering_ps2": "int:0:3",
        "accurate_blending_unit": "int:0:5",
        "fxaa": "bool",
        "CASMode": "int:0:2",
        "CASSharpness": "int:0:100",
        "ShadeBoost": "bool",
        "ShadeBoost_Brightness": "int:0:100",
        "ShadeBoost_Contrast": "int:0:100",
        "ShadeBoost_Saturation": "int:0:100",
        "ShadeBoost_Gamma": "int:0:100",
        "LoadTextureReplacements": "bool",
        "LoadTextureReplacementsAsync": "bool",
        "PrecacheTextureReplacements": "bool",
        "DumpReplaceableTextures": "bool",
        "DumpReplaceableMipmaps": "bool",
        "DumpTexturesWithFMVActive": "bool",
        "DumpDirectTextures": "bool",
        "DumpPaletteTextures": "bool",
        "UserHacks": "bool",
        "UserHacks_BilinearHack": "int:0:2",
        "UserHacks_HalfPixelOffset": "int:0:5",
        "UserHacks_native_scaling": "int:0:4",
        "UserHacks_CPUCLUTRender": "int:0:2",
        "UserHacks_GPUTargetCLUTMode": "int:0:2",
    },
    "Patches": {
        "Enable": "str",
        "Disable": "str",
    },
}

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_SKIP_AUTO_ENABLE_TOKENS = ("optional", "experimental", "21:9", "ultrawide")


def extract_default_pnach_sections(pnach_path: Path) -> List[str]:
    """Return non-optional, non-experimental [Section] names from a PCSX2 v2 .pnach file.

    In PCSX2 v2.6.3 (Patch::UpdateActivePatches), EnableWideScreenPatches=true ONLY
    auto-enables a section named literally '[Widescreen 16:9]'. Every other named
    section (including '[Remove Brown Filter]', '[Remove Motion Blur]', and modded-ISO
    '[Widescreen 16:9 (Runtime Flag)]') must be listed under [Patches] via `Enable = ...`.
    """
    sections: List[str] = []
    if not pnach_path.is_file():
        return sections

    for raw_line in pnach_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        m = _SECTION_RE.match(stripped)
        if not m:
            continue
        name = m.group(1).strip()
        low = name.lower()
        if any(tok in low for tok in _SKIP_AUTO_ENABLE_TOKENS):
            continue
        if name not in sections:
            sections.append(name)
    return sections


def _validate_value_spec(spec: str, value: str) -> Optional[str]:
    if spec == "bool":
        if value not in ("true", "false"):
            return f"expected 'true' or 'false', got {value!r}"
        return None
    if spec == "str":
        if not value:
            return "expected non-empty string"
        return None
    if spec.startswith("int:"):
        _, lo_s, hi_s = spec.split(":")
        try:
            iv = int(value)
        except ValueError:
            return f"expected integer, got {value!r}"
        if not (int(lo_s) <= iv <= int(hi_s)):
            return f"integer {iv} out of PCSX2 v2.6.3 range [{lo_s}, {hi_s}]"
        return None
    if spec.startswith("float:"):
        _, lo_s, hi_s = spec.split(":")
        try:
            fv = float(value)
        except ValueError:
            return f"expected float, got {value!r}"
        if not (float(lo_s) <= fv <= float(hi_s)):
            return f"float {fv} out of PCSX2 v2.6.3 range [{lo_s}, {hi_s}]"
        return None
    if spec.startswith("enum_int:"):
        allowed = {int(x) for x in spec.split(":", 1)[1].split(",")}
        try:
            iv = int(value)
        except ValueError:
            return f"expected integer enum, got {value!r}"
        if iv not in allowed:
            return f"integer {iv} not in allowed PCSX2 v2.6.3 enum values {sorted(allowed)}"
        return None
    if spec.startswith("enum_str:"):
        allowed_s = spec.split(":", 1)[1].split(",")
        if value not in allowed_s:
            return f"string {value!r} not in allowed PCSX2 v2.6.3 values {allowed_s}"
        return None
    return f"unknown schema spec {spec!r}"


def validate_gamesettings_ini(ini_text: str) -> List[str]:
    """Validate every section, key name, and enum/range value against PCSX2 v2.6.3."""
    errors: List[str] = []
    current_section: Optional[str] = None
    seen_single_keys: Dict[Tuple[str, str], int] = {}

    for lineno, raw in enumerate(ini_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        sec_m = _SECTION_RE.match(line)
        if sec_m:
            current_section = sec_m.group(1).strip()
            if current_section not in PCSX2_V263_SCHEMA:
                errors.append(f"line {lineno}: unknown PCSX2 v2.6.3 section [{current_section}]")
            continue
        if "=" not in line:
            errors.append(f"line {lineno}: malformed INI line (missing '='): {line!r}")
            continue
        if current_section is None:
            errors.append(f"line {lineno}: key outside any [Section]: {line!r}")
            continue

        key, val = [part.strip() for part in line.split("=", 1)]
        sec_schema = PCSX2_V263_SCHEMA.get(current_section, {})
        if key not in sec_schema:
            errors.append(
                f"line {lineno}: unknown/misspelled PCSX2 v2.6.3 key '{key}' in [{current_section}]"
            )
            continue

        # Multi-key lists are used by PCSX2 CSimpleIni under [Patches] (Enable / Disable).
        if current_section != "Patches":
            pair = (current_section, key)
            if pair in seen_single_keys:
                errors.append(
                    f"line {lineno}: duplicate key '{key}' in [{current_section}] "
                    f"(first defined on line {seen_single_keys[pair]})"
                )
            else:
                seen_single_keys[pair] = lineno

        err = _validate_value_spec(sec_schema[key], val)
        if err:
            errors.append(f"line {lineno}: [{current_section}] {key} = {val}: {err}")

    return errors


def build_gamesettings_ini(
    upscale: int,
    anisotropy: int,
    ee_cycle_rate: int,
    enable_dump: bool,
    precache: bool,
    *,
    renderer: int = RENDERER_IDS["metal"],
    enabled_patches: Optional[Sequence[str]] = None,
    dithering: int = 0,
    blending: int = 2,
    cas_mode: int = 2,
    cas_sharpness: int = 45,
) -> str:
    """Build a PCSX2 v2.6.3-verified per-game INI profile for SLUS-21351 on macOS."""
    if not (1 <= upscale <= 25):
        raise ValueError(f"upscale must be between 1 and 25, got {upscale}")
    if anisotropy not in VALID_ANISOTROPY:
        raise ValueError(f"anisotropy must be one of {VALID_ANISOTROPY}, got {anisotropy}")
    if not (-3 <= ee_cycle_rate <= 3):
        raise ValueError(f"ee_cycle_rate must be between -3 and 3, got {ee_cycle_rate}")
    if renderer not in RENDERER_IDS.values():
        raise ValueError(
            f"renderer must be a verified PCSX2 v2.6.3 macOS ID {sorted(RENDERER_IDS.values())}, got {renderer}"
        )
    if not (0 <= dithering <= 3):
        raise ValueError(f"dithering must be in 0..3, got {dithering}")
    if not (0 <= blending <= 5):
        raise ValueError(f"blending must be in 0..5, got {blending}")
    if not (0 <= cas_mode <= 2):
        raise ValueError(f"cas_mode must be in 0..2, got {cas_mode}")
    if not (0 <= cas_sharpness <= 100):
        raise ValueError(f"cas_sharpness must be in 0..100, got {cas_sharpness}")

    renderer_label = {
        -1: "Automatic (Metal on macOS)",
        11: "Null",
        13: "Software Renderer",
        14: "Vulkan (MoltenVK)",
        17: "Metal (native macOS GPU backend)",
    }[renderer]

    sections = [
        f"""# Generated by NFSMW: Rockport 2026 (tools/setup_pcsx2_macos.py)
# Audited against PCSX2 v2.6.3 for Apple Silicon (M-series, 64 GB Unified Memory)

[EmuCore]
EnablePatches = true
EnableWideScreenPatches = true
EnableNoInterlacingPatches = true
EnableGameFixes = true

[EmuCore/Speedhacks]
# -3=50%, -2=60%, -1=75%, 0=100%, 1=130%, 2=180%, 3=300%
EECycleRate = {ee_cycle_rate}

[EmuCore/GS]
# PCSX2 v2.6.3 GSRendererType: -1=Auto, 14=Vulkan, 17=Metal ({renderer_label})
Renderer = {renderer}
AspectRatio = 16:9
FMVAspectRatioSwitch = Off
upscale_multiplier = {upscale}
MaxAnisotropy = {anisotropy}
mipmap = true
hw_mipmap = true
# 0=Off, 1=Scaled, 2=Unscaled (Default), 3=Force 32bit
dithering_ps2 = {dithering}
# 0=Minimum, 1=Basic, 2=Medium, 3=High, 4=Full, 5=Maximum
accurate_blending_unit = {blending}

# Modern crispness & color grading (Apple Silicon Metal-native post-processing)
fxaa = true
# 0=None, 1=Sharpen Only (Internal Res), 2=Sharpen and Resize (Display Res)
CASMode = {cas_mode}
CASSharpness = {cas_sharpness}
ShadeBoost = true
ShadeBoost_Brightness = 50
ShadeBoost_Contrast = 54
ShadeBoost_Saturation = 56
ShadeBoost_Gamma = 48

# 4K Texture Replacement Pipeline (bypasses PS2 4 MB VRAM limit; precaches into 64 GB RAM)
LoadTextureReplacements = true
LoadTextureReplacementsAsync = true
PrecacheTextureReplacements = {"true" if precache else "false"}
DumpReplaceableTextures = {"true" if enable_dump else "false"}
DumpReplaceableMipmaps = false
DumpTexturesWithFMVActive = false
DumpDirectTextures = true
DumpPaletteTextures = true

# GameIndex.yaml-aligned HW fixes for SLUS-21351 (1:1 verified in PCSX2 v2.6.3)
# bilinearUpscale=2, halfPixelOffset=5, nativeScaling=2, cpuCLUTRender=1, gpuTargetCLUT=1
UserHacks = true
UserHacks_BilinearHack = 2
UserHacks_HalfPixelOffset = 5
UserHacks_native_scaling = 2
UserHacks_CPUCLUTRender = 1
UserHacks_GPUTargetCLUTMode = 1
"""
    ]

    if enabled_patches:
        patch_lines = [
            "",
            "[Patches]",
            "# PCSX2 v2.6.3 requires explicit Enable entries for named .pnach sections",
        ]
        seen = set()
        for patch_name in enabled_patches:
            cleaned = patch_name.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                patch_lines.append(f"Enable = {cleaned}")
        patch_lines.append("")
        sections.append("\n".join(patch_lines))

    ini_text = "".join(sections)
    schema_errors = validate_gamesettings_ini(ini_text)
    if schema_errors:
        raise RuntimeError("Generated INI failed PCSX2 v2.6.3 schema validation:\n" + "\n".join(schema_errors))
    return ini_text


def _find_pnach_for_crc(crc_upper: str, pnach_sources: Sequence[Path]) -> Optional[Path]:
    suffix = f"_{crc_upper}.PNACH"
    for src in pnach_sources:
        if src.name.upper().endswith(suffix):
            return src
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    pcsx2_dir = Path.home() / "Library" / "Application Support" / "PCSX2"

    parser = argparse.ArgumentParser(
        description="Install NFSMW: Rockport 2026 per-game PCSX2 v2.6.3 settings & patches on macOS"
    )
    parser.add_argument("--pcsx2-dir", type=Path, default=pcsx2_dir)
    parser.add_argument(
        "--crcs",
        nargs="+",
        default=["0518D274"],
        help="One or more ELF CRCs to configure (e.g. stock 0518D274 + your modded ISO CRC)",
    )
    parser.add_argument(
        "--extra-pnach",
        action="append",
        type=Path,
        default=[],
        help="Additional .pnach files to install into PCSX2/patches/ (e.g. build/SLUS-21351_<CRC>.pnach)",
    )
    parser.add_argument(
        "--renderer",
        choices=("metal", "vulkan", "auto"),
        default="metal",
        help="PCSX2 v2.6.3 GPU backend (metal=17, vulkan=14, auto=-1; default: metal)",
    )
    parser.add_argument("--upscale", type=int, default=6, help="Internal res multiplier (6 = 4K UHD, 8 = 5K)")
    parser.add_argument(
        "--anisotropy",
        type=int,
        choices=VALID_ANISOTROPY,
        default=16,
        help="Anisotropic filtering (0, 2, 4, 8, 16)",
    )
    parser.add_argument(
        "--ee-cycle-rate",
        type=int,
        default=1,
        help="EE Cycle Rate index (-3=50%%, -2=60%%, -1=75%%, 0=100%%, 1=130%%, 2=180%%, 3=300%%)",
    )
    parser.add_argument(
        "--enable-patch",
        action="append",
        default=[],
        help="Explicit .pnach [Section] name to enable under [Patches] (repeatable; overrides auto-discovery)",
    )
    parser.add_argument(
        "--no-auto-enable-patches",
        action="store_true",
        help="Do not auto-populate [Patches] Enable entries from matched .pnach files",
    )
    parser.add_argument("--enable-dump", action="store_true", help="Enable PCSX2 texture dumping for Phase 2 pack creation")
    parser.add_argument("--no-precache", action="store_true", help="Disable full-RAM texture precaching")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files")
    args = parser.parse_args(argv)

    patches_dir = args.pcsx2_dir / "patches"
    gamesettings_dir = args.pcsx2_dir / "gamesettings"
    tex_base = args.pcsx2_dir / "textures" / "SLUS-21351"
    global_ini = (args.pcsx2_dir / "inis" / "PCSX2.ini").resolve()

    stock_pnach = repo_root / "patches" / "pnach" / "SLUS-21351_0518D274.pnach"
    pnach_sources: List[Path] = []
    if stock_pnach.is_file():
        pnach_sources.append(stock_pnach)
    for ep in args.extra_pnach:
        if ep.is_file():
            pnach_sources.append(ep)
        else:
            print(f"WARNING: --extra-pnach not found, skipping: {ep}", file=sys.stderr)

    renderer_id = RENDERER_IDS[args.renderer]

    print(f"PCSX2 Data Dir : {args.pcsx2_dir}")
    print(f"Renderer       : {args.renderer} (Renderer = {renderer_id})")
    print(f"Dry Run        : {args.dry_run}")

    if not args.dry_run:
        patches_dir.mkdir(parents=True, exist_ok=True)
        gamesettings_dir.mkdir(parents=True, exist_ok=True)
        (tex_base / "dumps").mkdir(parents=True, exist_ok=True)
        (tex_base / "replacements").mkdir(parents=True, exist_ok=True)

    for src in pnach_sources:
        dst = patches_dir / src.name
        print(f"  [pnach]        {src} -> {dst}")
        if not args.dry_run:
            shutil.copy2(src, dst)

    for crc in args.crcs:
        crc_u = crc.upper()
        if not re.fullmatch(r"[0-9A-F]{8}", crc_u):
            print(f"ERROR: Invalid 8-hex CRC: {crc!r}", file=sys.stderr)
            return 2

        if args.enable_patch:
            enabled_patches: List[str] = list(args.enable_patch)
        elif args.no_auto_enable_patches:
            enabled_patches = []
        else:
            matched_pnach = _find_pnach_for_crc(crc_u, pnach_sources)
            if matched_pnach is None:
                installed_pnach = patches_dir / f"SLUS-21351_{crc_u}.pnach"
                if installed_pnach.is_file():
                    matched_pnach = installed_pnach
            enabled_patches = extract_default_pnach_sections(matched_pnach) if matched_pnach else []

        ini_text = build_gamesettings_ini(
            upscale=args.upscale,
            anisotropy=args.anisotropy,
            ee_cycle_rate=args.ee_cycle_rate,
            enable_dump=args.enable_dump,
            precache=not args.no_precache,
            renderer=renderer_id,
            enabled_patches=enabled_patches,
        )

        ini_path = gamesettings_dir / f"SLUS-21351_{crc_u}.ini"
        if ini_path.resolve() == global_ini:
            print("ERROR: Refusing to touch global PCSX2.ini!", file=sys.stderr)
            return 2

        patch_summary = f", {len(enabled_patches)} [Patches] enabled" if enabled_patches else ""
        print(
            f"  [gamesettings] Writing {ini_path} "
            f"(Renderer={renderer_id}, {args.upscale}x native, {args.anisotropy}x AF, "
            f"CAS+ShadeBoost, Texture Replacements{patch_summary})"
        )
        if not args.dry_run:
            ini_path.write_text(ini_text, encoding="utf-8")

    print(f"  [textures]     Ready: {tex_base / 'replacements'}")
    print("\nDone! Global PCSX2.ini was NOT modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
