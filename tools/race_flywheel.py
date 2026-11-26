#!/usr/bin/env python3
"""
race_flywheel.py — Sandboxed live PCSX2 v2.6.3 driving & visual lookdev flywheel.

Non-destructive visual verification & gameplay automation tool for NFSMW: Rockport 2026:
  1. Never modifies the source ISO, global ~/Library/Application Support/PCSX2/inis/PCSX2.ini,
     or the user's real memory cards (Mcd001.ps2 / Mcd002.ps2).
  2. Creates/refreshes an isolated sandbox under build/flywheel/:
       - sandbox_memcards/  (copied once from user memcards, or on --reset-memcards)
       - sandbox_gamesettings/  (per-CRC INI overrides for dump mode, ShadeBoost, CAS, replacements)
       - sandbox_textures/  (synced .dds/.png replacements into SLUS-21351/replacements/)
       - sandbox_home/Library/Application Support/PCSX2/inis/PCSX2.ini
         with isolated [Folders], clean keyboard [Pad1] bindings, and non-conflicting [Hotkeys]/[UI].
  3. Subcommands:
       - run-race: boots PCSX2 inside the sandbox, drives NFSMW menus & race inputs via
         build/flywheel/key_driver, collects F8 snapshots, computes image metrics, builds
         a labeled montage, writes race_report.json, and appends to build/flywheel/history.json.
       - analyze: computes objective image quality metrics for PNG screenshots.
       - montage: generates a clean labeled comparison grid PNG from input screenshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import pcsx2_live_verify  # noqa: E402
import setup_pcsx2_macos  # noqa: E402
import video_qa_pipeline  # noqa: E402

DEFAULT_FLYWHEEL_DIR = REPO_ROOT / "build" / "flywheel"
DEFAULT_ISO_V04 = REPO_ROOT / "build" / "NFSMW_BlackEdition_Rockport2026_v0.4.iso"
DEFAULT_ISO_V03 = pcsx2_live_verify.DEFAULT_ISO
DEFAULT_KEY_DRIVER_SRC = TOOLS_DIR / "macos_key_driver.swift"
DEFAULT_KEY_DRIVER_BIN = DEFAULT_FLYWHEEL_DIR / "key_driver"
DEFAULT_PINE_DRIVER_SCRIPT = TOOLS_DIR / "pcsx2_pine_driver.py"

DEFAULT_RACE_SCRIPT = ",".join(
    [
        "wait:8500",
        "tap:k:200",
        "wait:1500",
        "tap:g:200",
        "wait:1500",
        "tap:k:200",
        "wait:4500",
        "f8",
        "wait:1200",
        "tap:down:160",
        "wait:450",
        "tap:k:200",
        "wait:1400",
        "tap:k:200",
        "wait:1400",
        "tap:k:200",
        "wait:1400",
        "tap:k:200",
        "wait:6000",
        "f8",
        "wait:1200",
        "hold:k:2500",
        "f8",
        "wait:1200",
        "hold:k+q+a:1200",
        "f8",
        "wait:1200",
        "tap:i:160",
        "wait:350",
        "hold:k+d:1500",
        "f8",
        "wait:1200",
    ]
)

DEFAULT_CHECKPOINT_LABELS = [
    "menu",
    "grid",
    "straightaway",
    "nos_corner",
    "cam_steer",
]

SANDBOX_PAD1_BINDINGS: Dict[str, str] = {
    "Type": "DualShock2",
    "Up": "Keyboard/Up",
    "LUp": "Keyboard/W",
    "Down": "Keyboard/Down",
    "LDown": "Keyboard/S",
    "Left": "Keyboard/Left",
    "LLeft": "Keyboard/A",
    "Right": "Keyboard/Right",
    "LRight": "Keyboard/D",
    "Cross": "Keyboard/K",
    "R2": "Keyboard/2",
    "Square": "Keyboard/J",
    "L2": "Keyboard/1",
    "Circle": "Keyboard/L",
    "Triangle": "Keyboard/I",
    "Start": "Keyboard/G",
    "Select": "Keyboard/H",
    "L1": "Keyboard/Q",
    "R1": "Keyboard/E",
    "L3": "",
    "R3": "",
    "RUp": "",
    "RDown": "",
    "RLeft": "",
    "RRight": "",
}

SANDBOX_UI_OVERRIDES: Dict[str, str] = {
    "ConfirmShutdown": "false",
    "PauseOnFocusLoss": "false",
}

SANDBOX_GS_OVERRIDES: Dict[str, str] = {
    "PauseOnFocusLoss": "false",
}

SANDBOX_HOTKEY_OVERRIDES: Dict[str, str] = {
    "Screenshot": "Keyboard/F8",
    "TogglePause": "",
    "ToggleTurbo": "",
    "HoldTurbo": "",
}

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def resolve_default_iso() -> Path:
    """Resolve the default target ISO (env NFSMW_ISO -> v0.4 -> v0.3)."""
    env_iso = os.environ.get("NFSMW_ISO", "").strip()
    if env_iso:
        return Path(env_iso).expanduser()
    if DEFAULT_ISO_V04.is_file():
        return DEFAULT_ISO_V04
    return DEFAULT_ISO_V03


def _sha256_or_none(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return pcsx2_live_verify._sha256_file(path)


def _slugify_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "snap"


def parse_shadeboost(raw: str) -> Tuple[int, int, int, int]:
    """Parse 'B,C,S,G' integers in [0, 100] for PCSX2 ShadeBoost."""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(f"--shadeboost must have 4 comma-separated ints B,C,S,G, got {raw!r}")
    vals: List[int] = []
    for p in parts:
        iv = int(p)
        if not (0 <= iv <= 100):
            raise ValueError(f"ShadeBoost value {iv} out of [0, 100]")
        vals.append(iv)
    return (vals[0], vals[1], vals[2], vals[3])


def upsert_ini_sections(
    ini_text: str,
    section_updates: Dict[str, Dict[str, str]],
) -> str:
    """Update INI sections/keys cleanly while preserving comments and duplicate keys (e.g. [Patches])."""
    out_lines: List[str] = []
    current_section: Optional[str] = None
    seen_sections: set[str] = set()
    written_keys: Dict[str, set[str]] = {sec: set() for sec in section_updates}

    def _flush_missing_keys(sec: Optional[str]) -> None:
        if sec is None or sec not in section_updates:
            return
        for k, v in section_updates[sec].items():
            if k not in written_keys[sec]:
                out_lines.append(f"{k} = {v}" if v != "" else f"{k} = ")
                written_keys[sec].add(k)

    for raw_line in ini_text.splitlines():
        stripped = raw_line.strip()
        sec_m = _SECTION_RE.match(stripped)
        if sec_m:
            _flush_missing_keys(current_section)
            current_section = sec_m.group(1).strip()
            seen_sections.add(current_section)
            out_lines.append(raw_line)
            continue

        if (
            current_section is not None
            and current_section in section_updates
            and not stripped.startswith("#")
            and not stripped.startswith(";")
            and "=" in stripped
        ):
            key = stripped.split("=", 1)[0].strip()
            updates = section_updates[current_section]
            if key in updates:
                val = updates[key]
                if key not in written_keys[current_section]:
                    out_lines.append(f"{key} = {val}" if val != "" else f"{key} = ")
                    written_keys[current_section].add(key)
                continue

        out_lines.append(raw_line)

    _flush_missing_keys(current_section)

    for sec, updates in section_updates.items():
        if sec not in seen_sections:
            if out_lines and out_lines[-1].strip() != "":
                out_lines.append("")
            out_lines.append(f"[{sec}]")
            for k, v in updates.items():
                out_lines.append(f"{k} = {v}" if v != "" else f"{k} = ")

    return "\n".join(out_lines) + "\n"


def ensure_key_driver_binary(
    src_path: Path = DEFAULT_KEY_DRIVER_SRC,
    bin_path: Path = DEFAULT_KEY_DRIVER_BIN,
    *,
    force_rebuild: bool = False,
) -> Path:
    """Compile tools/macos_key_driver.swift to build/flywheel/key_driver if needed."""
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    needs_build = force_rebuild or not bin_path.is_file() or not os.access(bin_path, os.X_OK)
    if not needs_build and src_path.is_file():
        needs_build = src_path.stat().st_mtime > bin_path.stat().st_mtime

    if needs_build:
        if not src_path.is_file():
            raise FileNotFoundError(f"key_driver Swift source not found: {src_path}")
        res = subprocess.run(
            ["swiftc", "-O", str(src_path), "-o", str(bin_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to compile {src_path} -> {bin_path} (exit {res.returncode}):\n{res.stderr.strip()}"
            )
    return bin_path


def sync_replacement_textures(
    replacements_dir: Path,
    sandbox_textures_dir: Path,
    serial: str = "SLUS-21351",
) -> Dict[str, Any]:
    """Sync .dds/.png replacement textures into sandbox_textures/<serial>/replacements/."""
    if not replacements_dir.is_dir():
        raise FileNotFoundError(f"Replacements directory not found: {replacements_dir}")

    target_dir = (sandbox_textures_dir / serial / "replacements").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    copied_files: List[str] = []
    for src in sorted(replacements_dir.rglob("*")):
        if not src.is_file() or src.name.startswith("."):
            continue
        if src.suffix.lower() not in (".dds", ".png"):
            continue
        rel = src.relative_to(replacements_dir)
        dst = target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied_files.append(str(rel))

    return {
        "source_dir": str(replacements_dir.resolve()),
        "target_dir": str(target_dir),
        "synced_count": len(copied_files),
        "synced_files": copied_files,
    }


def prepare_flywheel_sandbox(
    flywheel_dir: Path,
    iter_dir: Path,
    data_dir: Path,
    preflight: Dict[str, Any],
    *,
    reset_memcards: bool = False,
    dump_textures: bool = False,
    replacements_dir: Optional[Path] = None,
    patches_dir: Optional[Path] = None,
    shadeboost: Optional[Tuple[int, int, int, int]] = None,
    cas_sharpness: Optional[int] = None,
    upscale: Optional[int] = None,
    screenshot_key: str = "f8",
    record_video: bool = False,
    video_mode: str = "auto",
    video_hotkey: str = "f12",
    video_width: int = 1280,
    video_height: int = 720,
    video_bitrate: int = 8000,
    enable_pine: bool = False,
    pine_slot: int = 28011,
) -> Dict[str, Any]:
    """Create/refresh isolated PCSX2 sandbox inside `flywheel_dir` and `iter_dir`.

    NEVER modifies `data_dir/inis/PCSX2.ini` or `data_dir/memcards/*`.
    """
    flywheel_dir = flywheel_dir.resolve()
    iter_dir = iter_dir.resolve()
    data_dir = data_dir.resolve()

    sandbox_home = flywheel_dir / "sandbox_home"
    sandbox_pcsx2 = sandbox_home / "Library" / "Application Support" / "PCSX2"
    sandbox_inis = sandbox_pcsx2 / "inis"
    sandbox_memcards = flywheel_dir / "sandbox_memcards"
    sandbox_gamesettings = flywheel_dir / "sandbox_gamesettings"
    sandbox_textures = flywheel_dir / "sandbox_textures"
    sandbox_tmp = flywheel_dir / "sandbox_tmp"

    snaps_dir = iter_dir / "snaps"
    logs_dir = iter_dir / "logs"
    frames_dir = iter_dir / "frames"
    effective_record_video = bool(record_video and video_mode.strip().lower() != "off")
    videos_dir = (iter_dir / "videos") if effective_record_video else (sandbox_tmp / "videos")

    for d in (
        sandbox_inis,
        sandbox_memcards,
        sandbox_gamesettings,
        sandbox_textures,
        sandbox_tmp / "cache",
        sandbox_tmp / "sstates",
        sandbox_tmp / "videos",
        sandbox_tmp / "cheats",
        sandbox_tmp / "inputprofiles",
        sandbox_tmp / "debuggerlayouts",
        sandbox_tmp / "debuggersettings",
        iter_dir,
        snaps_dir,
        logs_dir,
        frames_dir,
        videos_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # 1. Copy user memory cards into sandbox_memcards ONLY once (or on --reset-memcards).
    src_memcards_dir = data_dir / "memcards"
    memcard_status: Dict[str, Any] = {}
    for mcd_name in ("Mcd001.ps2", "Mcd002.ps2"):
        src_mcd = src_memcards_dir / mcd_name
        dst_mcd = sandbox_memcards / mcd_name
        copied = False
        if src_mcd.is_file() and (reset_memcards or not dst_mcd.exists()):
            shutil.copy2(src_mcd, dst_mcd)
            copied = True
        memcard_status[mcd_name] = {
            "source_exists": src_mcd.is_file(),
            "sandbox_path": str(dst_mcd),
            "sandbox_exists": dst_mcd.is_file(),
            "copied_this_run": copied,
        }

    # 2. Populate sandbox_gamesettings (copy existing INIs read-only, then apply overrides for target CRC).
    src_gs_dir = data_dir / "gamesettings"
    if src_gs_dir.is_dir():
        for ini_file in src_gs_dir.glob("*.ini"):
            dst_ini = sandbox_gamesettings / ini_file.name
            if not dst_ini.exists():
                shutil.copy2(ini_file, dst_ini)

    serial = (preflight.get("iso") or {}).get("serial") or "SLUS-21351"
    crc = (preflight.get("iso") or {}).get("elf_crc") or "2DAD2310"

    # Sync replacement textures if requested.
    replacements_info: Optional[Dict[str, Any]] = None
    if replacements_dir is not None:
        replacements_info = sync_replacement_textures(
            replacements_dir=replacements_dir,
            sandbox_textures_dir=sandbox_textures,
            serial=serial,
        )

    target_gs_ini = sandbox_gamesettings / f"{serial}_{crc}.ini"
    if not target_gs_ini.is_file():
        src_target = src_gs_dir / target_gs_ini.name
        if src_target.is_file():
            shutil.copy2(src_target, target_gs_ini)
        else:
            pnach_path = (patches_dir or (data_dir / "patches")) / f"{serial}_{crc}.pnach"
            enabled_patches = setup_pcsx2_macos.extract_default_pnach_sections(pnach_path)
            target_gs_ini.write_text(
                setup_pcsx2_macos.build_gamesettings_ini(
                    upscale=upscale if upscale is not None else 6,
                    anisotropy=16,
                    ee_cycle_rate=2,
                    enable_dump=dump_textures,
                    precache=True,
                    enabled_patches=enabled_patches,
                ),
                encoding="utf-8",
            )

    gs_key_updates: Dict[str, str] = {
        "DumpReplaceableTextures": "true" if dump_textures else "false",
    }
    if replacements_info is not None:
        gs_key_updates["LoadTextureReplacements"] = "true"
        gs_key_updates["LoadTextureReplacementsAsync"] = "true"
        gs_key_updates["PrecacheTextureReplacements"] = "true"
    if shadeboost is not None:
        b, c, s, g = shadeboost
        gs_key_updates.update(
            {
                "ShadeBoost": "true",
                "ShadeBoost_Brightness": str(b),
                "ShadeBoost_Contrast": str(c),
                "ShadeBoost_Saturation": str(s),
                "ShadeBoost_Gamma": str(g),
            }
        )
    if cas_sharpness is not None:
        if not (0 <= cas_sharpness <= 100):
            raise ValueError(f"--cas-sharpness must be in [0, 100], got {cas_sharpness}")
        gs_key_updates["CASMode"] = "2" if cas_sharpness > 0 else "0"
        gs_key_updates["CASSharpness"] = str(cas_sharpness)
    if upscale is not None:
        if not (1 <= upscale <= 25):
            raise ValueError(f"--upscale must be in [1, 25], got {upscale}")
        gs_key_updates["upscale_multiplier"] = str(upscale)

    gs_text = target_gs_ini.read_text(encoding="utf-8", errors="replace")
    target_gs_ini.write_text(
        upsert_ini_sections(gs_text, {"EmuCore/GS": gs_key_updates}),
        encoding="utf-8",
    )

    # 3. Build isolated sandbox_home/.../PCSX2.ini from user's global PCSX2.ini (read-only).
    global_ini_path = data_dir / "inis" / "PCSX2.ini"
    bios_dir = (preflight.get("global_pcsx2_ini") or {}).get("bios_dir") or str(
        (data_dir / "bios").resolve()
    )
    effective_patches_dir = (patches_dir or (data_dir / "patches")).resolve()

    folder_overrides: Dict[str, str] = {
        "Bios": str(bios_dir),
        "MemoryCards": str(sandbox_memcards),
        "Snapshots": str(snaps_dir),
        "Logs": str(logs_dir),
        "GameSettings": str(sandbox_gamesettings),
        "Patches": str(effective_patches_dir),
        "Textures": str(sandbox_textures),
        "Cache": str(sandbox_tmp / "cache"),
        "Savestates": str(sandbox_tmp / "sstates"),
        "Videos": str(videos_dir),
        "Cheats": str(sandbox_tmp / "cheats"),
        "InputProfiles": str(sandbox_tmp / "inputprofiles"),
        "UserResources": str((data_dir / "resources").resolve()),
        "DebuggerLayouts": str(sandbox_tmp / "debuggerlayouts"),
        "DebuggerSettings": str(sandbox_tmp / "debuggersettings"),
    }

    hotkey_overrides = dict(SANDBOX_HOTKEY_OVERRIDES)
    if screenshot_key.lower() == "p":
        hotkey_overrides["Screenshot"] = "Keyboard/P"
    elif screenshot_key.lower() != "f8":
        raise ValueError(f"Unsupported screenshot_key {screenshot_key!r}; expected 'f8' or 'p'")

    gs_overrides = dict(SANDBOX_GS_OVERRIDES)
    resolved_video_hotkey: Optional[str] = None
    if effective_record_video and video_mode.strip().lower() in ("auto", "pcsx2"):
        hk_key, hk_binding = video_qa_pipeline.resolve_pcsx2_video_hotkey(video_hotkey)
        resolved_video_hotkey = hk_key
        hotkey_overrides["ToggleVideoCapture"] = hk_binding
        gs_overrides.update(
            video_qa_pipeline.pcsx2_video_capture_gs_settings(
                width=video_width,
                height=video_height,
                bitrate_kbps=video_bitrate,
            )
        )

    global_ini_text = global_ini_path.read_text(encoding="utf-8", errors="replace")
    sandbox_sections: Dict[str, Dict[str, str]] = {
        "Folders": folder_overrides,
        "UI": SANDBOX_UI_OVERRIDES,
        "EmuCore/GS": gs_overrides,
        "Hotkeys": hotkey_overrides,
        "Pad1": SANDBOX_PAD1_BINDINGS,
        "GameList": {"RecursivePaths": ""},
    }
    if enable_pine:
        # PCSX2 v2.6.3 PINE IPC settings live in [EmuCore] (sandbox PCSX2.ini ONLY — never global).
        sandbox_sections["EmuCore"] = {
            "EnablePINE": "true",
            "PINESlot": str(int(pine_slot)),
        }
    sandbox_ini_text = upsert_ini_sections(global_ini_text, sandbox_sections)
    sandbox_ini_path = sandbox_inis / "PCSX2.ini"
    sandbox_ini_path.write_text(sandbox_ini_text, encoding="utf-8")

    return {
        "flywheel_dir": str(flywheel_dir),
        "iter_dir": str(iter_dir),
        "sandbox_home": str(sandbox_home),
        "sandbox_ini": str(sandbox_ini_path),
        "sandbox_memcards_dir": str(sandbox_memcards),
        "memcards": memcard_status,
        "sandbox_gamesettings_dir": str(sandbox_gamesettings),
        "target_gamesettings_ini": str(target_gs_ini),
        "sandbox_textures_dir": str(sandbox_textures),
        "patches_dir": str(effective_patches_dir),
        "snaps_dir": str(snaps_dir),
        "logs_dir": str(logs_dir),
        "frames_dir": str(frames_dir),
        "videos_dir": str(videos_dir),
        "record_video": effective_record_video,
        "video_mode": video_mode.strip().lower(),
        "video_hotkey": resolved_video_hotkey,
        "enable_pine": bool(enable_pine),
        "pine_slot": int(pine_slot) if enable_pine else None,
        "replacements": replacements_info,
        "dump_textures": dump_textures,
        "shadeboost": list(shadeboost) if shadeboost is not None else None,
        "cas_sharpness": cas_sharpness,
        "upscale": upscale,
    }


def analyze_frame_metrics(image_path: Path) -> Dict[str, Any]:
    """Compute objective exposure, contrast, color cast, and sharpness metrics on a PNG."""
    info: Dict[str, Any] = {
        "path": str(image_path),
        "exists": image_path.is_file(),
        "size_bytes": image_path.stat().st_size if image_path.is_file() else 0,
        "width": 0,
        "height": 0,
        "mean_luma": 0.0,
        "std_luma": 0.0,
        "p5": 0.0,
        "p99": 0.0,
        "highlight_clip_pct": 0.0,
        "shadow_crush_pct": 0.0,
        "amber_rb_ratio": 0.0,
        "laplacian_sharpness": 0.0,
        "non_black_coverage": 0.0,
        "is_valid_render": False,
    }
    if not image_path.is_file() or info["size_bytes"] == 0:
        return info

    try:
        with Image.open(image_path) as img:
            rgb_img = img.convert("RGB")
            w, h = rgb_img.size
            info["width"] = w
            info["height"] = h
            if w == 0 or h == 0:
                return info

            arr = np.asarray(rgb_img, dtype=np.float32)
    except Exception as exc:
        info["error"] = f"failed to decode image: {exc}"
        return info

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]

    # BT.709 luminance in [0, 255]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b

    mean_luma = float(np.mean(luma))
    std_luma = float(np.std(luma))
    p5 = float(np.percentile(luma, 5.0))
    p99 = float(np.percentile(luma, 99.0))
    highlight_clip_pct = float(np.mean(luma >= 250.0) * 100.0)
    shadow_crush_pct = float(np.mean(luma <= 5.0) * 100.0)

    mean_r = float(np.mean(r))
    mean_b = float(np.mean(b))
    amber_rb_ratio = float(mean_r / max(mean_b, 1e-6))

    max_rgb = np.max(arr, axis=2)
    non_black_coverage = float(np.mean(max_rgb > 8.0))

    if w >= 3 and h >= 3:
        lap = (
            luma[:-2, 1:-1]
            + luma[2:, 1:-1]
            + luma[1:-1, :-2]
            + luma[1:-1, 2:]
            - 4.0 * luma[1:-1, 1:-1]
        )
        laplacian_sharpness = float(np.var(lap))
    else:
        laplacian_sharpness = 0.0

    info.update(
        {
            "mean_luma": round(mean_luma, 2),
            "std_luma": round(std_luma, 2),
            "p5": round(p5, 2),
            "p99": round(p99, 2),
            "highlight_clip_pct": round(highlight_clip_pct, 3),
            "shadow_crush_pct": round(shadow_crush_pct, 3),
            "amber_rb_ratio": round(amber_rb_ratio, 4),
            "laplacian_sharpness": round(laplacian_sharpness, 2),
            "non_black_coverage": round(non_black_coverage, 4),
            "is_valid_render": bool(non_black_coverage > 0.05 and std_luma > 2.0),
        }
    )
    return info


def _load_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_montage(
    image_paths: Sequence[Path],
    out_path: Path,
    *,
    labels: Optional[Sequence[str]] = None,
    cols: Optional[int] = None,
    tile_width: int = 800,
    metrics_by_path: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Generate a clean labeled comparison grid PNG using Pillow."""
    existing = [p for p in image_paths if p.is_file()]
    if not existing:
        raise ValueError("No valid input PNG files provided for montage")

    n = len(existing)
    if cols is None:
        cols = 1 if n == 1 else 2
    cols = max(1, min(cols, n))
    rows = math.ceil(n / cols)

    aspect_ratios: List[float] = []
    for p in existing:
        try:
            with Image.open(p) as im:
                iw, ih = im.size
                aspect_ratios.append((ih / iw) if iw > 0 else (9.0 / 16.0))
        except Exception:
            aspect_ratios.append(9.0 / 16.0)
    median_aspect = sorted(aspect_ratios)[len(aspect_ratios) // 2]
    tile_height = max(120, int(round(tile_width * median_aspect)))

    header_h = 44
    pad = 16
    canvas_w = pad + cols * (tile_width + pad)
    canvas_h = pad + rows * (header_h + tile_height + pad)

    canvas = Image.new("RGB", (canvas_w, canvas_h), (14, 18, 26))
    draw = ImageDraw.Draw(canvas)
    title_font = _load_font(18)
    meta_font = _load_font(14)
    unreadable_count = 0

    for idx, img_path in enumerate(existing):
        row = idx // cols
        col = idx % cols
        x0 = pad + col * (tile_width + pad)
        y0 = pad + row * (header_h + tile_height + pad)

        label = (
            labels[idx]
            if labels and idx < len(labels) and labels[idx].strip()
            else img_path.stem
        )

        # Header bar
        draw.rectangle(
            [x0, y0, x0 + tile_width, y0 + header_h],
            fill=(26, 33, 46),
            outline=(56, 72, 98),
            width=1,
        )
        draw.text(
            (x0 + 12, y0 + 11),
            f"{idx + 1:02d}  {label}",
            fill=(240, 244, 248),
            font=title_font,
        )

        m = (metrics_by_path or {}).get(str(img_path))
        if m:
            badge = (
                f"luma={m.get('mean_luma', 0):.1f}  "
                f"R/B={m.get('amber_rb_ratio', 0):.2f}  "
                f"sharp={m.get('laplacian_sharpness', 0):.0f}"
            )
            bbox = draw.textbbox((0, 0), badge, font=meta_font)
            bw = bbox[2] - bbox[0]
            draw.text(
                (x0 + tile_width - bw - 12, y0 + 14),
                badge,
                fill=(158, 186, 224),
                font=meta_font,
            )

        try:
            with Image.open(img_path) as im:
                rgb = im.convert("RGB").resize((tile_width, tile_height), Image.Resampling.LANCZOS)
                canvas.paste(rgb, (x0, y0 + header_h))
        except Exception as exc:
            unreadable_count += 1
            draw.rectangle(
                [x0, y0 + header_h, x0 + tile_width, y0 + header_h + tile_height],
                fill=(42, 20, 24),
            )
            draw.text(
                (x0 + 20, y0 + header_h + tile_height // 2 - 14),
                f"[UNREADABLE / TRUNCATED PNG: {img_path.name}]",
                fill=(255, 168, 168),
                font=title_font,
            )
            draw.text(
                (x0 + 20, y0 + header_h + tile_height // 2 + 12),
                str(exc)[:90],
                fill=(210, 140, 140),
                font=meta_font,
            )
        draw.rectangle(
            [x0, y0 + header_h, x0 + tile_width, y0 + header_h + tile_height],
            outline=(56, 72, 98),
            width=1,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")
    return {
        "path": str(out_path.resolve()),
        "width": canvas_w,
        "height": canvas_h,
        "tile_count": n,
        "cols": cols,
        "rows": rows,
        "unreadable_count": unreadable_count,
    }


def _estimate_script_duration_sec(script: str) -> float:
    """Estimate total runtime in seconds of a comma-separated key_driver script."""
    total_ms = 0
    for raw in script.split(","):
        tok = raw.strip().lower()
        if not tok:
            continue
        if tok == "f8":
            total_ms += 320
            continue
        parts = tok.split(":")
        if parts[0] in ("wait", "tap", "hold"):
            if len(parts) >= 2 and parts[-1].isdigit():
                total_ms += int(parts[-1])
            else:
                total_ms += 150
    return total_ms / 1000.0


def _wait_for_gs_window_ready(
    emu_log_path: Path,
    proc: subprocess.Popen[bytes],
    *,
    timeout_sec: float = 4.0,
    poll_sec: float = 0.10,
) -> Dict[str, Any]:
    """Wait briefly until PCSX2 initializes its Metal GS/VM subsystems (typically ~0.6s)
    so video capture starts right at boot before logos and loading screens render."""
    start = time.monotonic()
    reason = "timeout_fallback"
    early_exit_code: Optional[int] = None

    while True:
        rc = proc.poll()
        if rc is not None:
            early_exit_code = rc
            reason = "process_exited"
            break

        if emu_log_path.is_file():
            low = emu_log_path.read_text(encoding="utf-8", errors="replace").lower()
            if (
                "vm subsystems initialized" in low
                or "metal graphics driver info" in low
                or ("elf " in low and "is executing" in low)
            ):
                reason = "gs_vm_ready"
                break

        if (time.monotonic() - start) >= timeout_sec:
            break
        time.sleep(poll_sec)

    return {
        "ready": early_exit_code is None,
        "reason": reason,
        "waited_sec": round(time.monotonic() - start, 2),
        "early_exit_code": early_exit_code,
    }


def _wait_for_pine_socket(
    pine_slot: int,
    proc: subprocess.Popen[bytes],
    *,
    timeout_sec: float = 6.0,
    poll_sec: float = 0.15,
) -> Optional[Path]:
    """Wait briefly for PCSX2's PINE Unix domain socket to appear before launching autopilot."""
    import pcsx2_pine_driver

    deadline = time.monotonic() + max(0.5, timeout_sec)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        for cand in pcsx2_pine_driver.candidate_pine_socket_paths(slot=pine_slot):
            if cand.exists():
                return cand
        time.sleep(poll_sec)
    return None


def _wait_for_emulation_ready(
    emu_log_path: Path,
    proc: subprocess.Popen[bytes],
    *,
    timeout_sec: float = 14.0,
    poll_sec: float = 0.25,
) -> Dict[str, Any]:
    """Wait until PCSX2 log confirms active emulation (CRTC/VSync/DS2/ELF) or fallback timeout."""
    start = time.monotonic()
    reason = "timeout_fallback"
    early_exit_code: Optional[int] = None

    while True:
        rc = proc.poll()
        if rc is not None:
            early_exit_code = rc
            reason = "process_exited"
            break

        if emu_log_path.is_file():
            text = emu_log_path.read_text(encoding="utf-8", errors="replace")
            low = text.lower()
            has_elf = ("elf " in low and "is executing" in low) or "elf loading:" in low
            has_pad = "ds2 config finished" in low
            if has_elf and has_pad:
                reason = "log_ready_marker"
                break

        if (time.monotonic() - start) >= timeout_sec:
            break
        time.sleep(poll_sec)

    return {
        "ready": early_exit_code is None,
        "reason": reason,
        "waited_sec": round(time.monotonic() - start, 2),
        "early_exit_code": early_exit_code,
    }


def _wait_for_snaps_flush(
    snaps_dir: Path,
    before_snaps: set[str],
    emu_log_path: Path,
    *,
    expected_count: int,
    timeout_sec: float,
    proc: Optional[subprocess.Popen[bytes]] = None,
) -> Optional[int]:
    """Wait until pending PCSX2 screenshot writes complete and PNG sizes stabilize."""
    deadline = time.monotonic() + max(1.2, timeout_sec)
    last_sizes: Dict[str, int] = {}
    stable_since: Dict[str, float] = {}

    while time.monotonic() < deadline:
        if proc is not None:
            rc = proc.poll()
            if rc is not None:
                return rc

        pending_in_log = False
        if emu_log_path.is_file():
            log_text = emu_log_path.read_text(encoding="utf-8", errors="replace")
            saving = re.findall(r"Saving screenshot to '([^']+)'", log_text, re.I)
            saved = set(re.findall(r"Saved screenshot to '([^']+)'", log_text, re.I))
            if any(name not in saved for name in saving):
                pending_in_log = True

        new_files: List[Path] = []
        if snaps_dir.is_dir():
            new_files = sorted(
                (
                    p
                    for p in snaps_dir.iterdir()
                    if p.is_file() and p.suffix.lower() == ".png" and p.name not in before_snaps
                ),
                key=lambda p: p.stat().st_mtime,
            )

        now = time.monotonic()
        all_stable_and_valid = bool(new_files)
        for p in new_files:
            sz = p.stat().st_size
            if sz <= 0 or last_sizes.get(p.name) != sz:
                last_sizes[p.name] = sz
                stable_since[p.name] = now
                all_stable_and_valid = False
            elif (now - stable_since.get(p.name, now)) < 0.30:
                all_stable_and_valid = False
            else:
                metrics = analyze_frame_metrics(p)
                if metrics.get("error") or metrics.get("width", 0) <= 0:
                    all_stable_and_valid = False

        if (
            not pending_in_log
            and all_stable_and_valid
            and (expected_count <= 0 or len(new_files) >= expected_count)
        ):
            return None

        time.sleep(0.20)
    return None


def _next_iter_dir(flywheel_dir: Path, iter_name: Optional[str]) -> Tuple[str, Path]:
    runs_dir = flywheel_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    if iter_name:
        return iter_name, (runs_dir / iter_name).resolve()

    existing = sorted(p.name for p in runs_dir.iterdir() if p.is_dir() and re.match(r"^iter_\d+$", p.name))
    next_idx = (int(existing[-1].split("_")[1]) + 1) if existing else 1
    name = f"iter_{next_idx:03d}"
    return name, (runs_dir / name).resolve()


def _append_history(flywheel_dir: Path, entry: Dict[str, Any]) -> Path:
    history_path = flywheel_dir / "history.json"
    history: List[Dict[str, Any]] = []
    if history_path.is_file():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = loaded
        except Exception:
            history = []
    history.append(entry)
    history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return history_path


def run_race(
    *,
    iso_path: Path,
    flywheel_dir: Path = DEFAULT_FLYWHEEL_DIR,
    iter_name: Optional[str] = None,
    iter_dir: Optional[Path] = None,
    pcsx2_app: Path = pcsx2_live_verify.DEFAULT_PCSX2_APP,
    pcsx2_data_dir: Optional[Path] = None,
    key_driver_bin: Optional[Path] = None,
    script: str = DEFAULT_RACE_SCRIPT,
    labels: Optional[Sequence[str]] = None,
    boot_ready_timeout: float = 14.0,
    post_script_wait: float = 4.0,
    reset_memcards: bool = False,
    dump_textures: bool = False,
    replacements_dir: Optional[Path] = None,
    patches_dir: Optional[Path] = None,
    shadeboost: Optional[Tuple[int, int, int, int]] = None,
    cas_sharpness: Optional[int] = None,
    upscale: Optional[int] = None,
    tap_mode: str = "auto",
    screenshot_key: str = "f8",
    record_video: bool = False,
    video_mode: str = "auto",
    video_interval: float = 1.5,
    video_hotkey: str = "f12",
    video_width: int = 1280,
    video_height: int = 720,
    video_bitrate: int = 8000,
    video_tools_bin: Optional[Path] = None,
    enable_pine: bool = False,
    pine_slot: int = 28011,
    pine_autopilot: bool = False,
    autopilot_duration: float = 28.0,
    autopilot_f8_interval: float = 5.0,
    autopilot_menu_mode: str = "freeroam",
    autopilot_wait_racing_timeout: float = 45.0,
    pine_driver_script: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run one sandboxed PCSX2 live race iteration and return its full JSON report."""
    flywheel_dir = flywheel_dir.resolve()
    flywheel_dir.mkdir(parents=True, exist_ok=True)
    data_dir = (pcsx2_data_dir or pcsx2_live_verify.default_pcsx2_data_dir()).expanduser().resolve()

    if autopilot_menu_mode not in ("freeroam", "challenge"):
        raise ValueError(
            f"Unsupported autopilot_menu_mode {autopilot_menu_mode!r}; expected 'freeroam' or 'challenge'"
        )

    if iter_dir is None:
        resolved_name, resolved_iter_dir = _next_iter_dir(flywheel_dir, iter_name)
    else:
        resolved_iter_dir = iter_dir.resolve()
        resolved_name = iter_name or resolved_iter_dir.name

    preflight = pcsx2_live_verify.run_preflight(
        iso_path,
        pcsx2_app=pcsx2_app,
        pcsx2_data_dir=data_dir,
    )

    effective_record_video = bool(record_video and video_mode.strip().lower() != "off")
    effective_enable_pine = bool(enable_pine or pine_autopilot)
    resolved_pine_driver = (
        pine_driver_script.resolve() if pine_driver_script else DEFAULT_PINE_DRIVER_SCRIPT.resolve()
    )

    sandbox_info = prepare_flywheel_sandbox(
        flywheel_dir=flywheel_dir,
        iter_dir=resolved_iter_dir,
        data_dir=data_dir,
        preflight=preflight,
        reset_memcards=reset_memcards,
        dump_textures=dump_textures,
        replacements_dir=replacements_dir,
        patches_dir=patches_dir,
        shadeboost=shadeboost,
        cas_sharpness=cas_sharpness,
        upscale=upscale,
        screenshot_key=screenshot_key,
        record_video=effective_record_video,
        video_mode=video_mode,
        video_hotkey=video_hotkey,
        video_width=video_width,
        video_height=video_height,
        video_bitrate=video_bitrate,
        enable_pine=effective_enable_pine,
        pine_slot=pine_slot,
    )

    driver_path = key_driver_bin.resolve() if key_driver_bin else ensure_key_driver_binary(
        bin_path=flywheel_dir / "key_driver"
    )
    resolved_video_tools: Optional[Path] = None
    if effective_record_video:
        resolved_video_tools = (
            video_tools_bin.resolve()
            if video_tools_bin
            else video_qa_pipeline.ensure_video_tools_binary(bin_path=flywheel_dir / "video_tools")
        )

    global_ini_path = data_dir / "inis" / "PCSX2.ini"
    global_ini_sha_before = preflight["global_pcsx2_ini"].get("sha256")
    mcd1_real = data_dir / "memcards" / "Mcd001.ps2"
    mcd2_real = data_dir / "memcards" / "Mcd002.ps2"
    mcd_sha_before = {
        "Mcd001.ps2": _sha256_or_none(mcd1_real),
        "Mcd002.ps2": _sha256_or_none(mcd2_real),
    }

    report_path = resolved_iter_dir / "race_report.json"
    boot_log_path = resolved_iter_dir / "pcsx2_boot.log"
    emu_log_path = resolved_iter_dir / "pcsx2_emulog.txt"
    snaps_dir = Path(sandbox_info["snaps_dir"])
    frames_dir = Path(sandbox_info["frames_dir"])

    report: Dict[str, Any] = {
        "tool": "race_flywheel.py",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "iter_name": resolved_name,
        "iter_dir": str(resolved_iter_dir),
        "dry_run": dry_run,
        "iso": str(iso_path),
        "script": script,
        "sandbox": sandbox_info,
        "preflight": preflight,
        "boot_ready": None,
        "launch": None,
        "key_driver": None,
        "pine_autopilot": None,
        "pine_summary": None,
        "parsed_log": None,
        "frames": [],
        "montage": None,
        "video": None,
        "video_qa": None,
        "summary_metrics": {},
        "shutdown": None,
        "global_pcsx2_ini_untouched": None,
        "real_memcards_untouched": None,
        "passed": False,
    }

    if dry_run or not preflight["ok"]:
        report["global_pcsx2_ini_untouched"] = (
            _sha256_or_none(global_ini_path) == global_ini_sha_before
            if global_ini_sha_before is not None
            else None
        )
        report["real_memcards_untouched"] = (
            _sha256_or_none(mcd1_real) == mcd_sha_before["Mcd001.ps2"]
            and _sha256_or_none(mcd2_real) == mcd_sha_before["Mcd002.ps2"]
        )
        report["passed"] = bool(dry_run and preflight["ok"])
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    before_snaps = {p.name for p in snaps_dir.iterdir() if p.is_file()} if snaps_dir.is_dir() else set()
    for lp in (boot_log_path, emu_log_path):
        if lp.exists():
            lp.unlink()

    pcsx2_bin = Path(preflight["pcsx2_app"]["binary_path"])
    cmd = [
        str(pcsx2_bin),
        "-batch",
        "-fastboot",
        "-logfile",
        str(emu_log_path.resolve()),
        "--",
        str(iso_path.resolve()),
    ]
    child_env = dict(os.environ)
    child_env["HOME"] = sandbox_info["sandbox_home"]

    proc: Optional[subprocess.Popen[bytes]] = None
    early_exit_code: Optional[int] = None
    start_ts = time.monotonic()

    recorder: Optional[video_qa_pipeline.SessionVideoRecorder] = None
    if effective_record_video:
        active_drive_est = (
            (autopilot_wait_racing_timeout + autopilot_duration)
            if pine_autopilot
            else _estimate_script_duration_sec(script)
        )
        est_total_sec = int(math.ceil(boot_ready_timeout + active_drive_est + post_script_wait + 45.0))
        recorder = video_qa_pipeline.SessionVideoRecorder(
            mode=video_mode,
            iter_dir=resolved_iter_dir,
            videos_dir=Path(sandbox_info["videos_dir"]),
            key_driver_bin=driver_path,
            video_tools_bin=resolved_video_tools,
            tap_mode=tap_mode,
            video_hotkey=video_hotkey,
            max_duration_sec=est_total_sec,
        )

    with boot_log_path.open("wb") as boot_log_fp:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=boot_log_fp,
                stderr=subprocess.STDOUT,
                cwd=str(resolved_iter_dir),
                env=child_env,
            )
            pid = proc.pid

            if recorder is not None:
                gs_ready = _wait_for_gs_window_ready(
                    emu_log_path,
                    proc,
                    timeout_sec=min(4.0, boot_ready_timeout),
                )
                if gs_ready["early_exit_code"] is None:
                    recorder.start(pid)

            ready_info = _wait_for_emulation_ready(
                emu_log_path,
                proc,
                timeout_sec=boot_ready_timeout,
            )
            report["boot_ready"] = ready_info
            if ready_info["early_exit_code"] is not None:
                early_exit_code = ready_info["early_exit_code"]
            else:
                if recorder is not None:
                    recorder.ensure_started_retry(pid)

                if pine_autopilot:
                    pine_telemetry_path = resolved_iter_dir / "pine_telemetry.jsonl"
                    pine_summary_path = resolved_iter_dir / "pine_summary.json"
                    if pine_driver_script is None:
                        _wait_for_pine_socket(pine_slot, proc, timeout_sec=6.0)

                    pine_cmd = [
                        sys.executable,
                        str(resolved_pine_driver),
                        "--slot",
                        str(int(pine_slot)),
                        "autopilot",
                        "--pid",
                        str(pid),
                        "--key-driver",
                        str(driver_path),
                        "--tap-mode",
                        tap_mode,
                        "--duration",
                        f"{autopilot_duration:.2f}",
                        "--f8-interval",
                        f"{autopilot_f8_interval:.2f}",
                        "--wait-racing-timeout",
                        f"{autopilot_wait_racing_timeout:.2f}",
                        "--menu-auto-advance",
                        "--menu-mode",
                        autopilot_menu_mode,
                        "--telemetry-out",
                        str(pine_telemetry_path),
                        "--summary-out",
                        str(pine_summary_path),
                        "--json",
                    ]
                    pine_timeout = max(90.0, autopilot_wait_racing_timeout + autopilot_duration + 30.0)
                    pine_res = subprocess.run(
                        pine_cmd,
                        capture_output=True,
                        text=True,
                        timeout=pine_timeout,
                        check=False,
                    )
                    pine_summary_data: Optional[Dict[str, Any]] = None
                    if pine_summary_path.is_file():
                        try:
                            pine_summary_data = json.loads(
                                pine_summary_path.read_text(encoding="utf-8")
                            )
                        except json.JSONDecodeError:
                            pine_summary_data = None
                    if pine_summary_data is None and pine_res.stdout.strip():
                        try:
                            parsed_stdout = json.loads(pine_res.stdout.strip())
                            if isinstance(parsed_stdout, dict):
                                pine_summary_data = parsed_stdout
                        except json.JSONDecodeError:
                            pine_summary_data = None

                    report["pine_autopilot"] = {
                        "enabled": True,
                        "returncode": pine_res.returncode,
                        "command": pine_cmd,
                        "slot": int(pine_slot),
                        "menu_mode": autopilot_menu_mode,
                        "duration_sec": float(autopilot_duration),
                        "f8_interval_sec": float(autopilot_f8_interval),
                        "wait_racing_timeout_sec": float(autopilot_wait_racing_timeout),
                        "telemetry_path": str(pine_telemetry_path),
                        "summary_path": str(pine_summary_path),
                        "stderr": pine_res.stderr.strip(),
                    }
                    report["pine_summary"] = pine_summary_data
                    expected_f8_count = 0
                else:
                    effective_script = script
                    if screenshot_key.lower() == "p":
                        effective_script = ",".join(
                            "tap:p:100,wait:200" if tok.strip().lower() == "f8" else tok.strip()
                            for tok in script.split(",")
                            if tok.strip()
                        )
                    expected_f8_count = sum(
                        1
                        for tok in script.split(",")
                        if tok.strip().lower() == "f8" or tok.strip().lower().startswith("tap:p:")
                    )
                    est_sec = _estimate_script_duration_sec(effective_script)
                    driver_timeout = max(60.0, est_sec + 30.0)
                    drv_res = subprocess.run(
                        [
                            str(driver_path),
                            "--pid",
                            str(pid),
                            "--tap-mode",
                            tap_mode,
                            "--json",
                            "--actions",
                            effective_script,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=driver_timeout,
                        check=False,
                    )
                    drv_payload: Dict[str, Any] = {
                        "returncode": drv_res.returncode,
                        "stderr": drv_res.stderr.strip(),
                    }
                    if drv_res.stdout.strip():
                        try:
                            drv_payload.update(json.loads(drv_res.stdout.strip()))
                        except json.JSONDecodeError:
                            drv_payload["raw_stdout"] = drv_res.stdout.strip()
                    report["key_driver"] = drv_payload

                # Stop PCSX2/screencapture video recording immediately after driving finishes
                # (BEFORE _wait_for_snaps_flush) so parked dead air is never appended while PCSX2
                # flushes async 4K F8 PNGs; PCSX2 still gets the flush window below to finalize
                # both the MP4 moov atom and any queued F8 PNGs cleanly before SIGTERM.
                if recorder is not None:
                    recorder.stop_before_shutdown(pid, proc_alive=(proc.poll() is None))

                # Allow final F8 4K PNGs (and MP4 moov atom) to finish flushing to disk before SIGTERM.
                flush_rc = _wait_for_snaps_flush(
                    snaps_dir,
                    before_snaps,
                    emu_log_path,
                    expected_count=expected_f8_count,
                    timeout_sec=max(8.0, post_script_wait),
                    proc=proc,
                )
                if flush_rc is not None:
                    early_exit_code = flush_rc

            if early_exit_code is None and proc.poll() is not None:
                early_exit_code = proc.returncode

            report["launch"] = {
                "command": cmd,
                "pid": pid,
                "uptime_sec": round(time.monotonic() - start_ts, 2),
                "early_exit": early_exit_code is not None,
                "early_exit_code": early_exit_code,
                "boot_log": str(boot_log_path),
                "emu_log": str(emu_log_path),
            }
        finally:
            if recorder is not None and not recorder.stopped and proc is not None:
                try:
                    recorder.stop_before_shutdown(proc.pid, proc_alive=(proc.poll() is None))
                except Exception:
                    pass
            if proc is not None:
                report["shutdown"] = pcsx2_live_verify.terminate_process_cleanly(proc)

    # Parse combined PCSX2 logs
    combined_chunks: List[str] = []
    if emu_log_path.is_file():
        combined_chunks.append(emu_log_path.read_text(encoding="utf-8", errors="replace"))
    if boot_log_path.is_file():
        combined_chunks.append(boot_log_path.read_text(encoding="utf-8", errors="replace"))
    parsed = pcsx2_live_verify.parse_pcsx2_log(
        "\n".join(combined_chunks),
        expected_serial=preflight["iso"].get("serial"),
        expected_crc=preflight["iso"].get("elf_crc"),
    )
    report["parsed_log"] = parsed

    # Collect & analyze new snaps
    checkpoint_labels = list(labels) if labels else list(DEFAULT_CHECKPOINT_LABELS)
    new_snaps: List[Path] = []
    if snaps_dir.is_dir():
        new_names = {
            p.name
            for p in snaps_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".png"
        } - before_snaps
        new_snaps = sorted((snaps_dir / n for n in new_names), key=lambda p: p.name)

    frames: List[Dict[str, Any]] = []
    frame_paths: List[Path] = []
    frame_labels: List[str] = []
    metrics_by_path: Dict[str, Dict[str, Any]] = {}

    def _quarantine_corrupt_snap(snap_path: Path, metrics: Dict[str, Any], label: str) -> Dict[str, Any]:
        """Rename a truncated/unreadable PNG so no agent or tool reads it as .png."""
        if not metrics.get("error") and (not metrics.get("is_valid_render") or metrics.get("width", 0) <= 0):
            metrics["error"] = "truncated or unreadable PNG"
        candidate = snap_path.with_name(snap_path.name + ".corrupt")
        suffix_idx = 1
        while candidate.exists():
            candidate = snap_path.with_name(f"{snap_path.name}.corrupt.{suffix_idx}")
            suffix_idx += 1
        snap_path.rename(candidate)
        metrics["path"] = str(candidate)
        metrics["label"] = label
        metrics["source_snap"] = str(candidate)
        metrics["quarantined_corrupt"] = True
        metrics["is_valid_render"] = False
        return metrics

    for idx, src_snap in enumerate(new_snaps, start=1):
        lbl = checkpoint_labels[idx - 1] if (idx - 1) < len(checkpoint_labels) else f"snap_{idx:02d}"
        if not pcsx2_live_verify._wait_for_complete_png(src_snap, timeout_sec=4.0):
            frames.append(_quarantine_corrupt_snap(src_snap, analyze_frame_metrics(src_snap), lbl))
            continue

        slug = _slugify_label(lbl)
        dst_frame = frames_dir / f"frame_{idx:02d}_{slug}.png"
        shutil.copy2(src_snap, dst_frame)
        metrics = analyze_frame_metrics(dst_frame)
        if metrics.get("error") or metrics.get("width", 0) <= 0:
            if dst_frame.exists():
                dst_frame.unlink()
            frames.append(_quarantine_corrupt_snap(src_snap, metrics, lbl))
            continue

        metrics["label"] = lbl
        metrics["source_snap"] = str(src_snap)
        frames.append(metrics)
        frame_paths.append(dst_frame)
        frame_labels.append(lbl)
        metrics_by_path[str(dst_frame)] = metrics

    report["frames"] = frames

    if frame_paths:
        montage_out = resolved_iter_dir / "montage.png"
        report["montage"] = build_montage(
            frame_paths,
            montage_out,
            labels=frame_labels,
            metrics_by_path=metrics_by_path,
        )

    if recorder is not None:
        video_res = recorder.finalize_and_extract_qa(
            interval_sec=video_interval,
            fallback_frames_dir=frames_dir,
        )
        qa_data = video_res.pop("qa", None)
        report["video"] = video_res
        if qa_data is not None:
            report["video_qa"] = {
                "ok": qa_data.get("ok"),
                "qa_frames_dir": qa_data.get("qa_frames_dir"),
                "qa_sheets": qa_data.get("qa_sheets"),
                "telemetry_path": qa_data.get("telemetry_path"),
                "summary": qa_data.get("summary"),
            }

    valid_frames = [f for f in frames if f.get("is_valid_render")]
    if frames:
        report["summary_metrics"] = {
            "total_frames": len(frames),
            "valid_frames": len(valid_frames),
            "avg_mean_luma": round(sum(f["mean_luma"] for f in frames) / len(frames), 2),
            "avg_std_luma": round(sum(f["std_luma"] for f in frames) / len(frames), 2),
            "avg_amber_rb_ratio": round(sum(f["amber_rb_ratio"] for f in frames) / len(frames), 4),
            "avg_laplacian_sharpness": round(
                sum(f["laplacian_sharpness"] for f in frames) / len(frames), 2
            ),
            "avg_non_black_coverage": round(
                sum(f["non_black_coverage"] for f in frames) / len(frames), 4
            ),
        }

    # Verify global PCSX2.ini and real memcards remain untouched
    report["global_pcsx2_ini_untouched"] = (
        _sha256_or_none(global_ini_path) == global_ini_sha_before
        if global_ini_sha_before is not None
        else None
    )
    report["real_memcards_untouched"] = (
        _sha256_or_none(mcd1_real) == mcd_sha_before["Mcd001.ps2"]
        and _sha256_or_none(mcd2_real) == mcd_sha_before["Mcd002.ps2"]
    )

    no_orphan = not (report["shutdown"] or {}).get("orphan_remains", False)
    if pine_autopilot:
        driver_ok = bool((report["pine_autopilot"] or {}).get("returncode", 1) == 0)
    else:
        driver_ok = bool((report["key_driver"] or {}).get("returncode", 1) == 0)
    boot_ok = (
        early_exit_code is None
        and parsed["metal_gs_created"]
        and parsed["iso_mounted"]
        and not parsed["fatal_errors"]
        and no_orphan
        and driver_ok
        and report["global_pcsx2_ini_untouched"] is not False
        and report["real_memcards_untouched"] is True
    )
    report["passed"] = bool(boot_ok)

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    _append_history(
        flywheel_dir,
        {
            "timestamp_utc": report["timestamp_utc"],
            "iter_name": resolved_name,
            "iter_dir": str(resolved_iter_dir),
            "iso": str(iso_path),
            "passed": report["passed"],
            "pine_autopilot": bool(pine_autopilot),
            "pine_summary": report["pine_summary"],
            "accessibility_permitted": (report["key_driver"] or {}).get("accessibility_permitted"),
            "frame_count": len(frames),
            "valid_frame_count": len(valid_frames),
            "summary_metrics": report["summary_metrics"],
            "montage_path": (report["montage"] or {}).get("path"),
            "session_video_path": (report["video"] or {}).get("session_video_path"),
            "qa_sheets": [
                sh.get("path") for sh in ((report["video_qa"] or {}).get("qa_sheets") or [])
            ],
            "qa_telemetry_path": (report["video_qa"] or {}).get("telemetry_path"),
            "report_path": str(report_path),
        },
    )

    return report


def _parse_labels_arg(raw_labels: Optional[Sequence[str]]) -> Optional[List[str]]:
    if not raw_labels:
        return None
    out: List[str] = []
    for item in raw_labels:
        for part in item.split(","):
            s = part.strip()
            if s:
                out.append(s)
    return out or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sandboxed live PCSX2 v2.6.3 driving & visual lookdev flywheel"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run-race
    p_run = sub.add_parser("run-race", help="Boot PCSX2 in sandbox, drive race inputs, capture & analyze")
    p_run.add_argument("--iso", type=Path, default=None, help="Target PS2 .iso path")
    p_run.add_argument("--flywheel-dir", type=Path, default=DEFAULT_FLYWHEEL_DIR)
    p_run.add_argument("--iter-name", type=str, default=None, help="Iteration name under runs/")
    p_run.add_argument("--iter-dir", type=Path, default=None, help="Explicit iteration directory")
    p_run.add_argument("--pcsx2-app", type=Path, default=pcsx2_live_verify.DEFAULT_PCSX2_APP)
    p_run.add_argument("--pcsx2-data-dir", type=Path, default=None)
    p_run.add_argument("--key-driver", type=Path, default=None)
    p_run.add_argument("--script", type=str, default=DEFAULT_RACE_SCRIPT)
    p_run.add_argument("--labels", action="append", default=None, help="Comma-separated frame labels")
    p_run.add_argument("--boot-ready-timeout", type=float, default=14.0)
    p_run.add_argument("--post-script-wait", type=float, default=4.0)
    p_run.add_argument("--reset-memcards", action="store_true")
    p_run.add_argument("--dump-textures", action="store_true")
    p_run.add_argument("--replacements-dir", type=Path, default=None)
    p_run.add_argument("--patches-dir", type=Path, default=None)
    p_run.add_argument("--shadeboost", type=str, default=None, help="B,C,S,G ints in 0..100")
    p_run.add_argument("--cas-sharpness", type=int, default=None)
    p_run.add_argument("--upscale", type=int, default=None)
    p_run.add_argument("--tap-mode", choices=["auto", "hid", "session", "pid"], default="auto")
    p_run.add_argument("--screenshot-key", choices=["f8", "p"], default="f8")
    p_run.add_argument(
        "--record-video",
        action="store_true",
        help="Record full-session video from boot (t=0s) and automatically run extract-qa",
    )
    p_run.add_argument(
        "--video-mode",
        choices=["auto", "pcsx2", "screencapture", "burst", "off"],
        default="auto",
        help="Video capture backend when --record-video is enabled (default: auto -> pcsx2 native MP4)",
    )
    p_run.add_argument(
        "--video-interval",
        type=float,
        default=1.5,
        help="Keyframe extraction interval in seconds for QA contact sheets (default: 1.5)",
    )
    p_run.add_argument(
        "--video-hotkey",
        choices=sorted(video_qa_pipeline.SUPPORTED_VIDEO_HOTKEYS),
        default="f12",
        help="PCSX2 [Hotkeys] ToggleVideoCapture key (default: f12)",
    )
    p_run.add_argument("--video-width", type=int, default=1280)
    p_run.add_argument("--video-height", type=int, default=720)
    p_run.add_argument("--video-bitrate", type=int, default=8000)
    p_run.add_argument("--video-tools", type=Path, default=None)
    p_run.add_argument(
        "--enable-pine",
        action="store_true",
        help="Enable PCSX2 [EmuCore] PINE IPC Unix socket inside the isolated sandbox PCSX2.ini",
    )
    p_run.add_argument(
        "--pine-slot",
        type=int,
        default=28011,
        help="PCSX2 [EmuCore] PINESlot value when --enable-pine is set (default: 28011)",
    )
    p_run.add_argument(
        "--pine-autopilot",
        action="store_true",
        help="Drive via 20 Hz PINE telemetry closed-loop autopilot (implies --enable-pine)",
    )
    p_run.add_argument(
        "--autopilot-duration",
        type=float,
        default=28.0,
        help="Closed-loop driving duration in seconds once in RACING state (default: 28.0)",
    )
    p_run.add_argument(
        "--autopilot-f8-interval",
        type=float,
        default=5.0,
        help="Interval in seconds between 4K F8 snapshots during autopilot driving (default: 5.0)",
    )
    p_run.add_argument(
        "--autopilot-menu-mode",
        choices=["freeroam", "challenge"],
        default="freeroam",
        help="GameFlowState-gated menu entry target for --pine-autopilot (default: freeroam)",
    )
    p_run.add_argument(
        "--autopilot-wait-racing-timeout",
        type=float,
        default=45.0,
        help="Max seconds to wait/advance menus to reach RACING before driving (default: 45.0)",
    )
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--json", action="store_true")

    # analyze
    p_ana = sub.add_parser("analyze", help="Compute objective image metrics on PNG screenshot(s)")
    p_ana.add_argument("pngs", nargs="+", type=Path, help="Input PNG file(s)")
    p_ana.add_argument("--json", action="store_true")

    # montage
    p_mon = sub.add_parser("montage", help="Build labeled comparison montage PNG")
    p_mon.add_argument("--out", type=Path, required=True, help="Output montage PNG path")
    p_mon.add_argument("--labels", action="append", default=None, help="Comma-separated tile labels")
    p_mon.add_argument("--cols", type=int, default=None, help="Grid columns (default: 2 for 2+ images)")
    p_mon.add_argument("--tile-width", type=int, default=800)
    p_mon.add_argument("pngs", nargs="+", type=Path, help="Input PNG file(s)")

    args = parser.parse_args(argv)

    if args.command == "analyze":
        results = [analyze_frame_metrics(p) for p in args.pngs]
        if args.json:
            print(json.dumps({"frames": results}, indent=2))
        else:
            for m in results:
                print(
                    f"{m['path']}: {m['width']}x{m['height']} luma={m['mean_luma']}±{m['std_luma']} "
                    f"R/B={m['amber_rb_ratio']} sharp={m['laplacian_sharpness']} cov={m['non_black_coverage']}"
                )
        return 0 if all(m.get("exists") and not m.get("error") for m in results) else 1

    if args.command == "montage":
        labels = _parse_labels_arg(args.labels)
        metrics_map = {str(p): analyze_frame_metrics(p) for p in args.pngs if p.is_file()}
        res = build_montage(
            args.pngs,
            args.out,
            labels=labels,
            cols=args.cols,
            tile_width=args.tile_width,
            metrics_by_path=metrics_map,
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.command == "run-race":
        sb = parse_shadeboost(args.shadeboost) if args.shadeboost else None
        iso_path = args.iso or resolve_default_iso()
        report = run_race(
            iso_path=iso_path,
            flywheel_dir=args.flywheel_dir,
            iter_name=args.iter_name,
            iter_dir=args.iter_dir,
            pcsx2_app=args.pcsx2_app,
            pcsx2_data_dir=args.pcsx2_data_dir,
            key_driver_bin=args.key_driver,
            script=args.script,
            labels=_parse_labels_arg(args.labels),
            boot_ready_timeout=args.boot_ready_timeout,
            post_script_wait=args.post_script_wait,
            reset_memcards=args.reset_memcards,
            dump_textures=args.dump_textures,
            replacements_dir=args.replacements_dir,
            patches_dir=args.patches_dir,
            shadeboost=sb,
            cas_sharpness=args.cas_sharpness,
            upscale=args.upscale,
            tap_mode=args.tap_mode,
            screenshot_key=args.screenshot_key,
            record_video=args.record_video,
            video_mode=args.video_mode,
            video_interval=args.video_interval,
            video_hotkey=args.video_hotkey,
            video_width=args.video_width,
            video_height=args.video_height,
            video_bitrate=args.video_bitrate,
            video_tools_bin=args.video_tools,
            enable_pine=args.enable_pine,
            pine_slot=args.pine_slot,
            pine_autopilot=args.pine_autopilot,
            autopilot_duration=args.autopilot_duration,
            autopilot_f8_interval=args.autopilot_f8_interval,
            autopilot_menu_mode=args.autopilot_menu_mode,
            autopilot_wait_racing_timeout=args.autopilot_wait_racing_timeout,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"Flywheel {report['iter_name']}: passed={report['passed']} "
                f"frames={len(report['frames'])} ini_untouched={report['global_pcsx2_ini_untouched']} "
                f"mcd_untouched={report['real_memcards_untouched']}"
            )
            if report.get("pine_summary"):
                ps = report["pine_summary"]
                print(
                    f"PINE Autopilot: dist={ps.get('distance_traveled_m', 0)}m "
                    f"max={ps.get('max_speed_mph', 0)}mph avg={ps.get('avg_speed_mph', 0)}mph "
                    f"recoveries={ps.get('stuck_recoveries', 0)} reverse_ok={ps.get('reverse_verified')}"
                )
            if report.get("montage"):
                print(f"Montage: {report['montage']['path']}")
            if (report.get("video") or {}).get("session_video_path"):
                print(f"Session Video: {report['video']['session_video_path']}")
            for sh in (report.get("video_qa") or {}).get("qa_sheets") or []:
                print(f"QA Sheet {sh['sheet_index']:02d}: {sh['path']}")
            if (report.get("video_qa") or {}).get("telemetry_path"):
                print(f"QA Telemetry: {report['video_qa']['telemetry_path']}")
            print(f"Report: {Path(report['iter_dir']) / 'race_report.json'}")
        return 0 if report["passed"] else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
