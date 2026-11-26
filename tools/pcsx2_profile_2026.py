#!/usr/bin/env python3
"""
pcsx2_profile_2026.py — PCSX2 v2.6.3 "Metal 2.3" per-game visual profile for SLUS-21351 (CRC 0518D274).

Builds two exact gamesettings INIs (validated against the audited PCSX2 v2.6.3 key schema):
  * v21_2026     — 4K/5K Metal, forced 32-bit colour, trilinear + 16x AF, ShadeBoost warm grade,
                   CAS or 4xRGSS (A/B), all 5 GameIndex.yaml SLUS-21351 HW fixes, EE 300% for 60 FPS.
  * original_ps2 — authentic untouched PS2 baseline (native 1x, 4:3, stock dithering, no patches).

Usage:
  python3 tools/pcsx2_profile_2026.py --profile v21_2026 --scale 8 --rgss --out build/SLUS-21351_0518D274.ini
  python3 tools/pcsx2_profile_2026.py --profile original_ps2 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_B = "bool"
_PCT = "int:0:100"

# Audited PCSX2 v2.6.3 per-game INI keys (section -> key -> type spec).
SCHEMA: Dict[str, Dict[str, str]] = {
    "EmuCore": {k: _B for k in ("EnablePatches", "EnableWideScreenPatches", "EnableNoInterlacingPatches", "EnableGameFixes")},
    "EmuCore/Speedhacks": {"EECycleRate": "int:-3:3"},
    "EmuCore/GS": {
        "Renderer": "enum_int:-1,11,13,14,17", "AspectRatio": "enum_str:Stretch,Auto 4:3/3:2,4:3,16:9,10:7",
        "FMVAspectRatioSwitch": "enum_str:Off,Auto 4:3/3:2,4:3,16:9,10:7", "upscale_multiplier": "float:1:25",
        "MaxAnisotropy": "enum_int:0,2,4,8,16", "TriFilter": "int:0:2", "mipmap": _B, "hw_mipmap": _B,
        "dithering_ps2": "int:0:3", "accurate_blending_unit": "int:0:5", "fxaa": _B, "TVShader": "int:0:7",
        "CASMode": "int:0:2", "CASSharpness": _PCT, "ShadeBoost": _B, "ShadeBoost_Brightness": _PCT,
        "ShadeBoost_Contrast": _PCT, "ShadeBoost_Saturation": _PCT, "ShadeBoost_Gamma": _PCT,
        "SoftwareRendererFMV": _B, "LoadTextureReplacements": _B, "LoadTextureReplacementsAsync": _B,
        "PrecacheTextureReplacements": _B, "DumpReplaceableTextures": _B, "DumpReplaceableMipmaps": _B,
        "DumpTexturesWithFMVActive": _B, "DumpDirectTextures": _B, "DumpPaletteTextures": _B,
        "UserHacks": _B, "UserHacks_BilinearHack": "int:0:2", "UserHacks_HalfPixelOffset": "int:0:5",
        "UserHacks_native_scaling": "int:0:4", "UserHacks_CPUCLUTRender": "int:0:2",
        "UserHacks_GPUTargetCLUTMode": "int:0:2",
    },
    "Patches": {"Enable": "str", "Disable": "str"},
}
MULTI_KEY_SECTIONS = {"Patches"}
RENDERERS: Dict[str, int] = {"metal": 17, "vulkan": 14, "auto": -1}

V21_DEFAULT_PATCHES: List[str] = [
    "Widescreen 16:9", "Remove Brown Filter", "Disable Motion Blur Effect",
    "Force High-LOD Cars and Wheels", "Full-Rate Car Reflections (All 5 Faces Every Frame)",
    "Extended Scenery Draw Distance and High-LOD World", "Warm Late-Autumn Lighting & Fog (Optional)",
    "60 FPS Companion: Fix ScreenEFX 30Hz Timestep (Experimental)",
    "High-Detail Car EnvMap and Road Reflection Scenery (Optional)",
]

# Official PCSX2 GameIndex.yaml SLUS-21351 GS hardware fixes.
SLUS21351_HW_FIXES: Dict[str, str] = {
    "UserHacks": "true", "UserHacks_BilinearHack": "2", "UserHacks_HalfPixelOffset": "5",
    "UserHacks_native_scaling": "2", "UserHacks_CPUCLUTRender": "1", "UserHacks_GPUTargetCLUTMode": "1",
}


def _check_value(spec: str, raw: str) -> Optional[str]:
    kind, _, arg = spec.partition(":")
    try:
        if kind == "bool":
            return None if raw in ("true", "false") else "expected true/false"
        if kind == "str":
            return None if raw.strip() else "empty value"
        if kind == "enum_str":
            return None if raw in arg.split(",") else f"expected one of {arg}"
        if kind == "enum_int":
            return None if int(raw) in {int(x) for x in arg.split(",")} else f"expected one of {arg}"
        lo, hi = arg.split(":")
        val = int(raw) if kind == "int" else float(raw)
        return None if float(lo) <= val <= float(hi) else f"out of range [{lo}, {hi}]"
    except ValueError:
        return f"unparsable {kind} {raw!r}"


def validate_profile_ini(ini_text: str) -> List[str]:
    """Return a list of schema errors (empty list == valid PCSX2 v2.6.3 per-game INI)."""
    errors: List[str] = []
    section: Optional[str] = None
    seen: Dict[str, set] = {}
    for lineno, raw_line in enumerate(ini_text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section not in SCHEMA:
                errors.append(f"line {lineno}: unknown section [{section}]")
            seen.setdefault(section, set())
            continue
        if "=" not in line:
            errors.append(f"line {lineno}: malformed line {line!r}")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if section is None:
            errors.append(f"line {lineno}: key {key!r} outside any section")
            continue
        if section not in SCHEMA:
            continue
        spec = SCHEMA[section].get(key)
        if spec is None:
            errors.append(f"line {lineno}: unknown key {key!r} in [{section}]")
            continue
        if key in seen[section] and section not in MULTI_KEY_SECTIONS:
            errors.append(f"line {lineno}: duplicate key {key!r} in [{section}]")
        seen[section].add(key)
        err = _check_value(spec, value)
        if err:
            errors.append(f"line {lineno}: [{section}] {key} = {value!r}: {err}")
    return errors


def _render(header: str, sections: Dict[str, Dict[str, object]], patches: Sequence[str]) -> str:
    lines = [f"; {header}", "; Generated by NFSMW: Rockport 2026 (tools/pcsx2_profile_2026.py) for PCSX2 v2.6.3", ""]
    for sec, kv in sections.items():
        lines.append(f"[{sec}]")
        for k, v in kv.items():
            lines.append(f"{k} = {str(v).lower() if isinstance(v, bool) else v}")
        lines.append("")
    lines.append("[Patches]")
    lines.extend(f"Enable = {name}" for name in patches)
    text = "\n".join(lines) + "\n"
    errors = validate_profile_ini(text)
    if errors:
        raise ValueError("Invalid PCSX2 profile: " + "; ".join(errors))
    return text


def build_v21_metal_profile(
    *,
    scale: int = 6,
    rgss: bool = False,
    ee_cycle_rate: int = 3,
    renderer: int = 17,
    enable_dump: bool = False,
    precache: bool = True,
    enabled_patches: Optional[Sequence[str]] = None,
    anisotropy: int = 16,
) -> str:
    """Build the v2.1 'Metal 2.3' 4K/5K per-game INI (raises ValueError on any invalid value)."""
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise ValueError(f"scale must be an integer, got {scale!r}")
    gs: Dict[str, object] = {
        "Renderer": renderer, "AspectRatio": "16:9", "FMVAspectRatioSwitch": "Off",
        "upscale_multiplier": scale, "MaxAnisotropy": anisotropy, "TriFilter": 2,
        "mipmap": True, "hw_mipmap": True, "dithering_ps2": 3, "accurate_blending_unit": 3,
        "ShadeBoost": True, "ShadeBoost_Brightness": 50, "ShadeBoost_Contrast": 52,
        "ShadeBoost_Saturation": 54, "ShadeBoost_Gamma": 50,
        "TVShader": 6 if rgss else 0, "CASMode": 0 if rgss else 2,
        "CASSharpness": 0 if rgss else 38, "fxaa": not rgss, "SoftwareRendererFMV": False,
        "LoadTextureReplacements": True, "LoadTextureReplacementsAsync": True,
        "PrecacheTextureReplacements": bool(precache), "DumpReplaceableTextures": bool(enable_dump),
        "DumpReplaceableMipmaps": bool(enable_dump), "DumpTexturesWithFMVActive": False,
        **SLUS21351_HW_FIXES,
    }
    sections = {
        "EmuCore": {k: True for k in SCHEMA["EmuCore"]},
        "EmuCore/Speedhacks": {"EECycleRate": ee_cycle_rate},
        "EmuCore/GS": gs,
    }
    patches = list(V21_DEFAULT_PATCHES if enabled_patches is None else enabled_patches)
    mode = "4xRGSS" if rgss else "CAS 38 + FXAA"
    return _render(f"v2.1 Metal 2.3 profile: {scale}x upscale, {mode}, EE cycle rate {ee_cycle_rate}", sections, patches)


def build_original_ps2_baseline_profile(*, renderer: int = 17) -> str:
    """Build the authentic untouched PS2 baseline INI (native res, 4:3, stock dithering, no patches)."""
    sections = {
        "EmuCore": {"EnablePatches": False, "EnableWideScreenPatches": False,
                    "EnableNoInterlacingPatches": False, "EnableGameFixes": True},
        "EmuCore/Speedhacks": {"EECycleRate": 0},
        "EmuCore/GS": {
            "Renderer": renderer, "AspectRatio": "4:3", "upscale_multiplier": 1, "MaxAnisotropy": 0,
            "TriFilter": 1, "mipmap": True, "hw_mipmap": True, "dithering_ps2": 2,
            "accurate_blending_unit": 1, "fxaa": False, "TVShader": 0, "CASMode": 0, "CASSharpness": 0,
            "ShadeBoost": False, "LoadTextureReplacements": False, "LoadTextureReplacementsAsync": False,
            "PrecacheTextureReplacements": False, "UserHacks": False,
        },
    }
    return _render("Original PS2 baseline profile (untouched native 1x, 4:3, stock EE)", sections, [])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PCSX2 v2.6.3 Metal 2.3 per-game profile builder (SLUS-21351)")
    parser.add_argument("--profile", choices=["v21_2026", "original_ps2"], default="v21_2026")
    parser.add_argument("--scale", type=int, default=6, help="Upscale multiplier (6 = 4K, 8 = 5K)")
    parser.add_argument("--rgss", action="store_true", help="Use 4xRGSS TV shader instead of CAS + FXAA")
    parser.add_argument("--ee-cycle-rate", type=int, default=3, help="EE cycle rate -3..3 (3 = 300%%)")
    parser.add_argument("--renderer", choices=sorted(RENDERERS), default="metal")
    parser.add_argument("--out", type=Path, default=None, help="Write INI to this path")
    parser.add_argument("--json", action="store_true", help="Print JSON metadata (+ INI when no --out)")
    args = parser.parse_args(argv)

    renderer = RENDERERS[args.renderer]
    try:
        if args.profile == "v21_2026":
            ini = build_v21_metal_profile(
                scale=args.scale, rgss=args.rgss, ee_cycle_rate=args.ee_cycle_rate, renderer=renderer
            )
        else:
            ini = build_original_ps2_baseline_profile(renderer=renderer)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(ini, encoding="utf-8")
    if args.json:
        meta = {"profile": args.profile, "renderer": renderer, "out": str(args.out) if args.out else None,
                "valid": not validate_profile_ini(ini)}
        if args.profile == "v21_2026":
            meta.update(scale=args.scale, rgss=args.rgss, ee_cycle_rate=args.ee_cycle_rate)
        if args.out is None:
            meta["ini"] = ini
        print(json.dumps(meta, indent=2))
    elif args.out is None:
        sys.stdout.write(ini)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
