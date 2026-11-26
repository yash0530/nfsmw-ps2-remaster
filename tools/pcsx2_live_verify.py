#!/usr/bin/env python3
"""
pcsx2_live_verify.py — Live PCSX2 v2.6.3 macOS Metal boot & frame verifier.

Non-destructive verification tool for NFSMW: Rockport 2026:
  1. Performs read-only pre-flight checks on the target ISO, PCSX2.app binary,
     PCSX2.ini ([Folders] Bios), BIOS directory, and per-CRC .ini / .pnach files.
     NEVER modifies the source ISO or global ~/Library/Application Support/PCSX2/inis/PCSX2.ini.
  2. Launches PCSX2 v2.6.3 with `-batch -fastboot -logfile <out_dir>/pcsx2_emulog.txt`
     against the target ISO, capturing stdout/stderr to `<out_dir>/pcsx2_boot.log`.
  3. Polls process health and parses log markers (ISO mount, SLUS-21351 ELF CRC match,
     per-game .ini load, .pnach patches applied, Metal GS device creation, errors).
  4. Attempts visual frame capture via both macOS Quartz window discovery + `screencapture`
     and PCSX2 `snaps/` inspection (plus optional F8 hotkey via AppleScript if permitted),
     reporting permission blocks honestly in JSON.
  5. Cleanly terminates PCSX2 in a `try/finally` block (SIGTERM -> wait -> SIGKILL) and
     emits `<out_dir>/verify_report.json`.
"""

from __future__ import annotations

import argparse
import configparser
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import iso_inspect  # noqa: E402

DEFAULT_ISO = REPO_ROOT / "build" / "NFSMW_BlackEdition_Rockport2026_v0.3.iso"
DEFAULT_PCSX2_APP = Path("/Applications/PCSX2-v2.6.3.app")
DEFAULT_OUT_DIR = REPO_ROOT / "build" / "pcsx2_verify_wave3"


def default_pcsx2_data_dir() -> Path:
    """Return the standard macOS PCSX2 user data directory."""
    return Path.home() / "Library" / "Application Support" / "PCSX2"


def derive_serial_from_elf(elf_name: Optional[str]) -> Optional[str]:
    """Convert PS2 boot ELF name like 'SLUS_213.51' into 'SLUS-21351'."""
    if not elf_name:
        return None
    m = re.match(r"^([A-Z]{4})_(\d{3})\.(\d{2})$", elf_name.strip().upper())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}{m.group(3)}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_ini_preserving_multikey(path: Path) -> Dict[str, Dict[str, List[str]]]:
    """Read an INI file read-only while preserving duplicate keys (e.g. [Patches] Enable)."""
    sections: Dict[str, Dict[str, List[str]]] = {}
    current = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if "=" in line:
            key, val = [part.strip() for part in line.split("=", 1)]
            sections.setdefault(current, {}).setdefault(key, []).append(val)
    return sections


def _resolve_pcsx2_folder(base_dir: Path, raw_value: str) -> Path:
    """Resolve a PCSX2 [Folders] value relative to the PCSX2 data directory."""
    p = Path(raw_value).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (base_dir / p).resolve()


def run_preflight(
    iso_path: Path,
    pcsx2_app: Path = DEFAULT_PCSX2_APP,
    pcsx2_data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Perform read-only pre-flight validation of ISO, PCSX2 binary, BIOS, .ini, and .pnach."""
    data_dir = (pcsx2_data_dir or default_pcsx2_data_dir()).expanduser()
    pcsx2_bin = pcsx2_app / "Contents" / "MacOS" / "PCSX2"
    info_plist = pcsx2_app / "Contents" / "Info.plist"

    issues: List[str] = []
    warnings: List[str] = []

    # 1. Inspect PCSX2 bundle
    pcsx2_bin_exists = pcsx2_bin.is_file() and os.access(pcsx2_bin, os.X_OK)
    if not pcsx2_bin_exists:
        issues.append(f"PCSX2 executable not found or not executable: {pcsx2_bin}")

    app_version: Optional[str] = None
    if info_plist.is_file():
        plist_text = info_plist.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>",
            plist_text,
        )
        if m:
            app_version = m.group(1).strip()

    # 2. Inspect target ISO (strictly read-only)
    iso_info: Dict[str, Any] = {
        "path": str(iso_path),
        "exists": iso_path.is_file(),
        "size_bytes": iso_path.stat().st_size if iso_path.is_file() else 0,
        "boot_elf_name": None,
        "serial": None,
        "elf_crc": None,
        "volume_id": None,
    }
    if not iso_path.is_file():
        issues.append(f"Target ISO not found: {iso_path}")
    else:
        try:
            rep = iso_inspect.inspect_iso(iso_path, compute_sha256=False)
            boot_elf = rep.get("boot_elf") or {}
            elf_name = boot_elf.get("name")
            serial = derive_serial_from_elf(elf_name)
            iso_info.update(
                {
                    "volume_id": rep.get("volume_id"),
                    "boot_elf_name": elf_name,
                    "serial": serial,
                    "elf_crc": boot_elf.get("pcsx2_crc"),
                    "entry_count": rep.get("entry_count"),
                }
            )
            if not iso_info["elf_crc"]:
                issues.append("Failed to parse boot ELF CRC from ISO")
        except Exception as exc:
            issues.append(f"Failed to inspect ISO {iso_path}: {exc}")

    # 3. Inspect global PCSX2.ini (read-only)
    global_ini = data_dir / "inis" / "PCSX2.ini"
    ini_info: Dict[str, Any] = {
        "path": str(global_ini),
        "exists": global_ini.is_file(),
        "sha256": None,
        "bios_folder_setting": None,
        "bios_dir": None,
        "bios_dir_exists": False,
        "bios_files_count": 0,
        "configured_bios_file": None,
        "configured_bios_exists": False,
        "snaps_dir": str((data_dir / "snaps").resolve()),
        "logs_dir": str((data_dir / "logs").resolve()),
        "global_renderer": None,
    }

    if not global_ini.is_file():
        issues.append(f"Global PCSX2.ini not found: {global_ini}")
    else:
        ini_info["sha256"] = _sha256_file(global_ini)
        parsed_global = _parse_ini_preserving_multikey(global_ini)
        folders = parsed_global.get("Folders", {})
        filenames = parsed_global.get("Filenames", {})
        gs_sec = parsed_global.get("EmuCore/GS", {})

        bios_raw = (folders.get("Bios") or ["bios"])[0]
        snaps_raw = (folders.get("Snapshots") or ["snaps"])[0]
        logs_raw = (folders.get("Logs") or ["logs"])[0]
        configured_bios = (filenames.get("BIOS") or [""])[0]
        global_renderer = (gs_sec.get("Renderer") or [None])[0]

        bios_dir = _resolve_pcsx2_folder(data_dir, bios_raw)
        snaps_dir = _resolve_pcsx2_folder(data_dir, snaps_raw)
        logs_dir = _resolve_pcsx2_folder(data_dir, logs_raw)

        bios_files: List[str] = []
        if bios_dir.is_dir():
            bios_files = sorted(
                p.name
                for p in bios_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".bin", ".rom1", ".rom2", ".erom", ".mec", ".nvm")
            )
        else:
            issues.append(f"BIOS directory referenced by PCSX2.ini does not exist: {bios_dir}")

        rom_bins = [name for name in bios_files if name.lower().endswith(".bin")]
        if bios_dir.is_dir() and not rom_bins:
            issues.append(f"No PS2 BIOS .bin images found in {bios_dir}")

        configured_bios_exists = bool(configured_bios and (bios_dir / configured_bios).is_file())
        if configured_bios and not configured_bios_exists:
            warnings.append(f"Configured [Filenames] BIOS '{configured_bios}' missing in {bios_dir}")

        ini_info.update(
            {
                "bios_folder_setting": bios_raw,
                "bios_dir": str(bios_dir),
                "bios_dir_exists": bios_dir.is_dir(),
                "bios_files_count": len(bios_files),
                "bios_rom_bins": rom_bins,
                "configured_bios_file": configured_bios or None,
                "configured_bios_exists": configured_bios_exists,
                "snaps_dir": str(snaps_dir),
                "logs_dir": str(logs_dir),
                "global_renderer": global_renderer,
            }
        )

    # 4. Inspect per-game .ini & .pnach for ISO serial + CRC
    serial = iso_info.get("serial")
    crc = iso_info.get("elf_crc")
    per_game_info: Dict[str, Any] = {
        "gamesettings_ini": None,
        "gamesettings_ini_exists": False,
        "gamesettings_renderer": None,
        "gamesettings_upscale_multiplier": None,
        "gamesettings_enabled_patches": [],
        "pnach_path": None,
        "pnach_exists": False,
        "pnach_active_lines": 0,
    }
    if serial and crc:
        gs_ini_path = data_dir / "gamesettings" / f"{serial}_{crc}.ini"
        pnach_path = data_dir / "patches" / f"{serial}_{crc}.pnach"
        per_game_info["gamesettings_ini"] = str(gs_ini_path)
        per_game_info["pnach_path"] = str(pnach_path)

        if gs_ini_path.is_file():
            per_game_info["gamesettings_ini_exists"] = True
            parsed_game = _parse_ini_preserving_multikey(gs_ini_path)
            gs_sec = parsed_game.get("EmuCore/GS", {})
            patches_sec = parsed_game.get("Patches", {})
            per_game_info["gamesettings_renderer"] = (gs_sec.get("Renderer") or [None])[0]
            per_game_info["gamesettings_upscale_multiplier"] = (
                gs_sec.get("upscale_multiplier") or [None]
            )[0]
            per_game_info["gamesettings_cas_mode"] = (gs_sec.get("CASMode") or [None])[0]
            per_game_info["gamesettings_fxaa"] = (gs_sec.get("fxaa") or [None])[0]
            per_game_info["gamesettings_shadeboost"] = (gs_sec.get("ShadeBoost") or [None])[0]
            per_game_info["gamesettings_enabled_patches"] = list(patches_sec.get("Enable", []))
        else:
            warnings.append(f"Per-game settings INI not found: {gs_ini_path}")

        if pnach_path.is_file():
            per_game_info["pnach_exists"] = True
            active_lines = [
                ln.strip()
                for ln in pnach_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip().startswith("patch=1,")
            ]
            per_game_info["pnach_active_lines"] = len(active_lines)
        else:
            warnings.append(f"Per-CRC .pnach file not found: {pnach_path}")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "pcsx2_app": {
            "app_path": str(pcsx2_app),
            "binary_path": str(pcsx2_bin),
            "executable_exists": pcsx2_bin_exists,
            "version": app_version,
        },
        "iso": iso_info,
        "global_pcsx2_ini": ini_info,
        "per_game": per_game_info,
    }


def parse_pcsx2_log(
    log_text: str,
    *,
    expected_serial: Optional[str] = None,
    expected_crc: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse PCSX2 v2.6.3 boot/runtime log text into structured verification markers."""
    version: Optional[str] = None
    host_os: Optional[str] = None
    host_cpu: Optional[str] = None
    iso_mounted = False
    iso_logged_path: Optional[str] = None
    detected_serial: Optional[str] = None
    detected_crc: Optional[str] = None
    gamesettings_loaded = False
    gamesettings_paths: List[str] = []
    pnach_loaded_files: List[str] = []
    pnach_patches_found_count = 0
    pnach_patches_applied_count = 0
    pnach_enabled_names: List[str] = []
    metal_gs_created = False
    gs_renderer: Optional[str] = None
    metal_device_name: Optional[str] = None
    metal_shader_version: Optional[str] = None
    bios_loaded = False
    bios_info: Optional[str] = None
    elf_booted = False
    boot_elf_path: Optional[str] = None
    fmv_started = False
    crtc_config: Optional[str] = None
    screenshots_saved: List[str] = []
    fatal_errors: List[str] = []
    warnings: List[str] = []

    serial_crc_re = re.compile(r"\b([A-Z]{4}-\d{5})\b.*?\b([0-9A-Fa-f]{8})\b")
    crc_re = re.compile(
        r"(?:Game\s+CRC|ELF\s+CRC|Disc\s+CRC|active\s+CRC|CRC)\s*(?:[:=]|is)?\s*(?:0x)?([0-9A-Fa-f]{8})",
        re.I,
    )
    serial_only_re = re.compile(r"\b(SL[A-Z]{2}-\d{5}|SC[A-Z]{2}-\d{5})\b")
    in_metal_block = False

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip leading PCSX2 timestamp like "[    1.4357] "
        clean_line = re.sub(r"^\[\s*\d+\.\d+\]\s*", "", line)
        low = clean_line.lower()

        if version is None:
            vm = re.search(r"\bPCSX2\s+(v?\d+\.\d+\.\d+[^\s]*)", clean_line)
            if vm:
                version = vm.group(1)

        if host_os is None and low.startswith("operating system ="):
            host_os = clean_line.split("=", 1)[1].strip()
        if host_cpu is None and low.startswith("processor") and "=" in clean_line:
            host_cpu = clean_line.split("=", 1)[1].strip()

        # ISO mount / disc open markers across PCSX2 versions
        if (
            "isofile open ok" in low
            or ("cdvd" in low and ("disk open" in low or "mounted" in low or "iso" in low))
            or "opening iso" in low
            or "booting iso" in low
            or "disc changed to" in low
        ):
            iso_mounted = True
            if ".iso" in low:
                iso_logged_path = clean_line

        # ELF boot markers
        if ("elf " in low and "is executing" in low) or "elf loading:" in low or "initializing elf:" in low:
            elf_booted = True
            boot_elf_path = clean_line

        if "fmv started" in low:
            fmv_started = True
        if "set gs crtc configuration" in low:
            crtc_config = clean_line

        # Serial / CRC extraction
        m_sc = serial_crc_re.search(clean_line)
        if m_sc:
            detected_serial = m_sc.group(1).upper()
            detected_crc = m_sc.group(2).upper()
        else:
            m_ser = serial_only_re.search(clean_line)
            if m_ser and "gamesettings" not in low and "patches" not in low and "snaps" not in low:
                detected_serial = m_ser.group(1).upper()
            m_crc = crc_re.search(clean_line)
            if m_crc:
                detected_crc = m_crc.group(1).upper()

        # Per-game settings .ini loaded
        if "loading game settings from" in low:
            gamesettings_loaded = True
            gamesettings_paths.append(clean_line)
        elif ".ini" in low and "gamesettings" in low and "pcsx2.ini" not in low:
            gamesettings_loaded = True
            gamesettings_paths.append(clean_line)

        # Patches (.pnach)
        m_found = re.search(r"found\s+(\d+)\s+game\s+patch(?:es)?\s+in\s+(.+)", low)
        if m_found:
            pnach_patches_found_count = max(pnach_patches_found_count, int(m_found.group(1)))
            pnach_loaded_files.append(clean_line)
        m_active = re.search(r"(\d+)\s+game\s+patch(?:es)?\s+(?:is|are)\s+active", low)
        if m_active:
            pnach_patches_applied_count = max(pnach_patches_applied_count, int(m_active.group(1)))
        if low.startswith("enabled patch:"):
            pnach_enabled_names.append(clean_line.split(":", 1)[1].strip())
        elif ".pnach" in low and "loaded" in low and clean_line not in pnach_loaded_files:
            pnach_loaded_files.append(clean_line)

        # Metal GS device markers
        if "metal graphics driver info" in low:
            metal_gs_created = True
            gs_renderer = "Metal"
            in_metal_block = True
        elif in_metal_block:
            if low.startswith("name ="):
                metal_device_name = clean_line.split("=", 1)[1].strip()
            elif low.startswith("shader version:"):
                metal_shader_version = clean_line.split(":", 1)[1].strip()
            elif low.startswith("opening ") or low.startswith("max texture size"):
                if low.startswith("opening "):
                    in_metal_block = False
        elif "metal" in low and (
            "gsdevice" in low or "device" in low or "renderer" in low or "creating" in low
        ):
            metal_gs_created = True
            gs_renderer = "Metal"

        if ("vulkan" in low or "software" in low) and "renderer" in low and gs_renderer is None:
            gs_renderer = "Vulkan" if "vulkan" in low else "Software"

        # BIOS markers
        if "bios found:" in low:
            bios_loaded = True
            bios_info = clean_line

        # Screenshot markers
        if "saved screenshot to" in low:
            sm = re.search(r"Saved screenshot to '([^']+)'", clean_line, re.I)
            if sm:
                screenshots_saved.append(sm.group(1))

        # Fatal errors vs expected unsafe-settings warnings
        if "[unsafe settings]" in low or "osd [unsafe_settings_warning]" in low or "dithering is disabled" in low or "graphics api is not set to automatic" in low:
            warnings.append(clean_line)
            continue

        is_fatal = any(
            tok in low
            for tok in (
                "fatal",
                "segmentation fault",
                "bus error",
                "abort trap",
                "unhandled exception",
                "bios not found",
                "failed to open iso",
                "failed to create gs device",
                "failed to initialize",
                "out of memory",
                "panic",
                "assertion failed",
            )
        )
        if is_fatal:
            fatal_errors.append(clean_line)
        elif "(e)" in low or "[error]" in low or "error:" in low or "failed to" in low:
            if "metal: gpu supports framebuffer fetch" not in low:
                warnings.append(clean_line)

    crc_matched = False
    if detected_crc and expected_crc:
        crc_matched = detected_crc.upper() == expected_crc.upper()
    elif detected_crc and not expected_crc:
        crc_matched = True

    serial_matched = False
    if detected_serial and expected_serial:
        serial_matched = detected_serial.upper() == expected_serial.upper()
    elif detected_serial and not expected_serial:
        serial_matched = True

    if pnach_patches_applied_count == 0 and pnach_enabled_names:
        pnach_patches_applied_count = len(pnach_enabled_names)

    active_emulation = bool(
        metal_gs_created and (elf_booted or iso_mounted or crc_matched) and not fatal_errors
    )

    return {
        "pcsx2_version": version,
        "host_os": host_os,
        "host_cpu": host_cpu,
        "iso_mounted": iso_mounted,
        "iso_logged_path": iso_logged_path,
        "elf_booted": elf_booted,
        "boot_elf_path": boot_elf_path,
        "fmv_started": fmv_started,
        "crtc_config": crtc_config,
        "detected_serial": detected_serial,
        "detected_crc": detected_crc,
        "serial_matched": serial_matched,
        "elf_crc_matched": crc_matched,
        "gamesettings_ini_loaded": gamesettings_loaded,
        "gamesettings_paths": gamesettings_paths,
        "pnach_loaded_files": pnach_loaded_files,
        "pnach_patches_found_count": pnach_patches_found_count,
        "pnach_patches_applied_count": pnach_patches_applied_count,
        "pnach_enabled_names": pnach_enabled_names,
        "metal_gs_created": metal_gs_created,
        "gs_renderer": gs_renderer,
        "metal_device_name": metal_device_name,
        "metal_shader_version": metal_shader_version,
        "bios_loaded": bios_loaded,
        "bios_info": bios_info,
        "screenshots_saved": screenshots_saved,
        "active_emulation": active_emulation,
        "fatal_errors": fatal_errors,
        "warnings": warnings,
    }


def list_pid_windows(pid: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Query macOS Quartz CGWindowListCopyWindowInfo via Swift for windows owned by `pid`."""
    swift_code = (
        "import CoreGraphics\n"
        "import Foundation\n"
        f"let targetPid = {int(pid)}\n"
        "let list = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []\n"
        "var matches: [[String: Any]] = []\n"
        "for w in list {\n"
        "    if let ownerPid = w[kCGWindowOwnerPID as String] as? Int, ownerPid == targetPid {\n"
        "        let wid = w[kCGWindowNumber as String] as? Int ?? 0\n"
        "        let layer = w[kCGWindowLayer as String] as? Int ?? -1\n"
        "        let name = w[kCGWindowName as String] as? String ?? \"\"\n"
        "        let owner = w[kCGWindowOwnerName as String] as? String ?? \"\"\n"
        "        let bounds = w[kCGWindowBounds as String] as? [String: Any] ?? [:]\n"
        "        let bw = (bounds[\"Width\"] as? NSNumber)?.doubleValue ?? 0\n"
        "        let bh = (bounds[\"Height\"] as? NSNumber)?.doubleValue ?? 0\n"
        "        matches.append([\"window_id\": wid, \"layer\": layer, \"name\": name, \"owner\": owner, \"width\": bw, \"height\": bh])\n"
        "    }\n"
        "}\n"
        "let data = try JSONSerialization.data(withJSONObject: matches, options: [])\n"
        "print(String(data: data, encoding: .utf8) ?? \"[]\")\n"
    )
    try:
        res = subprocess.run(
            ["swift", "-e", swift_code],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return [], f"swift CGWindowListCopyWindowInfo failed: {exc}"
    if res.returncode != 0:
        return [], (res.stderr.strip() or f"swift exited {res.returncode}")
    try:
        windows = json.loads(res.stdout.strip() or "[]")
        return windows, None
    except Exception as exc:
        return [], f"failed to parse Quartz window JSON: {exc}"


def analyze_screenshot(image_path: Path) -> Dict[str, Any]:
    """Measure dimensions and non-black pixel coverage of a captured PNG/JPEG."""
    info: Dict[str, Any] = {
        "path": str(image_path),
        "exists": image_path.is_file(),
        "size_bytes": image_path.stat().st_size if image_path.is_file() else 0,
        "width": 0,
        "height": 0,
        "non_black_coverage": 0.0,
        "mean_luma": 0.0,
        "std_luma": 0.0,
        "is_valid_render": False,
    }
    if not image_path.is_file() or info["size_bytes"] == 0:
        return info

    try:
        from PIL import Image, ImageStat  # type: ignore
    except ImportError:
        info["error"] = "Pillow not installed; pixel stats unavailable"
        return info

    try:
        with Image.open(image_path) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            info["width"] = w
            info["height"] = h
            if w == 0 or h == 0:
                return info

            # Downsample if huge so analysis stays fast and deterministic
            sample = rgb
            if max(w, h) > 640:
                scale = 640.0 / float(max(w, h))
                sample = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))))

            pixels = list(sample.getdata())
            total = len(pixels)
            non_black = sum(1 for (r, g, b) in pixels if max(r, g, b) > 8)
            gray = sample.convert("L")
            stat = ImageStat.Stat(gray)

            non_black_cov = non_black / total if total else 0.0
            mean_luma = float(stat.mean[0]) if stat.mean else 0.0
            std_luma = float(stat.stddev[0]) if stat.stddev else 0.0

            info["non_black_coverage"] = round(non_black_cov, 4)
            info["mean_luma"] = round(mean_luma, 2)
            info["std_luma"] = round(std_luma, 2)
            info["is_valid_render"] = bool(non_black_cov > 0.05 and std_luma > 2.0)
            return info
    except Exception as exc:
        info["error"] = f"failed to decode image: {exc}"
        return info


def attempt_window_capture(pid: int, out_png: Path) -> Dict[str, Any]:
    """Attempt to capture the PCSX2 render window using Quartz + macOS `screencapture`."""
    windows, query_err = list_pid_windows(pid)
    result: Dict[str, Any] = {
        "method": "screencapture_window",
        "attempted": True,
        "windows_found": windows,
        "query_error": query_err,
        "captured": False,
        "permission_denied": False,
        "error": None,
        "image": None,
    }
    if query_err:
        result["error"] = query_err
        return result

    candidates = [
        w
        for w in windows
        if w.get("layer", -1) == 0 and w.get("width", 0) >= 200 and w.get("height", 0) >= 150
    ]
    if not candidates:
        result["error"] = "no visible PCSX2 render window >= 200x150 found"
        return result

    best = max(candidates, key=lambda w: float(w.get("width", 0)) * float(w.get("height", 0)))
    wid = int(best["window_id"])
    result["selected_window"] = best

    proc = subprocess.run(
        ["screencapture", "-x", "-l", str(wid), str(out_png)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    stderr = proc.stderr.strip()
    if proc.returncode != 0 or not out_png.is_file() or out_png.stat().st_size == 0:
        low = stderr.lower()
        if any(tok in low for tok in ("permission", "denied", "not authorized", "could not create image")):
            result["permission_denied"] = True
        result["error"] = stderr or f"screencapture exited with code {proc.returncode}"
        return result

    img_info = analyze_screenshot(out_png)
    result["captured"] = True
    result["image"] = img_info
    return result


def _wait_for_complete_png(path: Path, timeout_sec: float = 8.0) -> bool:
    """Wait until a PNG file's size stabilizes and Pillow can decode it completely."""
    deadline = time.monotonic() + timeout_sec
    last_size = -1
    while time.monotonic() < deadline:
        if path.is_file():
            sz = path.stat().st_size
            if sz > 0 and sz == last_size:
                info = analyze_screenshot(path)
                if info.get("width", 0) > 0 and not info.get("error"):
                    return True
            last_size = sz
        time.sleep(0.4)
    if path.is_file() and path.stat().st_size > 0:
        info = analyze_screenshot(path)
        return bool(info.get("width", 0) > 0 and not info.get("error"))
    return False


def attempt_pcsx2_f8_snapshot(
    pid: int,
    snaps_dir: Path,
    before_snaps: set[str],
    out_copy_path: Path,
) -> Dict[str, Any]:
    """Try triggering PCSX2's F8 internal screenshot hotkey via AppleScript and check `snaps/`."""
    result: Dict[str, Any] = {
        "method": "pcsx2_f8_snaps",
        "attempted": True,
        "snaps_dir": str(snaps_dir),
        "accessibility_permitted": None,
        "applescript_error": None,
        "new_snaps_detected": [],
        "captured": False,
        "image": None,
    }

    script = (
        f'tell application "System Events"\n'
        f'  set procs to every process whose unix id is {int(pid)}\n'
        f'  if (count of procs) > 0 then\n'
        f'    set frontmost of item 1 of procs to true\n'
        f'    delay 0.3\n'
        f'    key code 100\n'
        f'  end if\n'
        f'end tell\n'
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode == 0:
            result["accessibility_permitted"] = True
        else:
            err = res.stderr.strip()
            result["accessibility_permitted"] = (
                False if "not allowed" in err.lower() or "1002" in err else None
            )
            result["applescript_error"] = err or f"osascript exited {res.returncode}"
    except Exception as exc:
        result["applescript_error"] = str(exc)

    # Give PCSX2 a moment to enqueue and write the 4K PNG to snaps/
    deadline = time.monotonic() + 7.5
    new_files: List[str] = []
    while time.monotonic() < deadline:
        if snaps_dir.is_dir():
            after = {p.name for p in snaps_dir.iterdir() if p.is_file()}
            new_files = sorted(after - before_snaps)
            if new_files:
                newest = max(
                    (snaps_dir / name for name in new_files),
                    key=lambda p: p.stat().st_mtime,
                )
                if _wait_for_complete_png(newest, timeout_sec=max(1.0, deadline - time.monotonic())):
                    break
        time.sleep(0.4)

    result["new_snaps_detected"] = new_files
    if new_files:
        newest = max(
            (snaps_dir / name for name in new_files),
            key=lambda p: p.stat().st_mtime,
        )
        img_info = analyze_screenshot(newest)
        is_valid = bool(img_info.get("width", 0) > 0 and not img_info.get("error"))
        if is_valid:
            shutil.copy2(newest, out_copy_path)
            img_info = analyze_screenshot(out_copy_path)
            is_valid = bool(img_info.get("width", 0) > 0 and not img_info.get("error"))
        result["captured"] = is_valid
        result["source_snap"] = str(newest)
        result["image"] = img_info

    return result


def terminate_process_cleanly(proc: subprocess.Popen[bytes], term_timeout: float = 5.0) -> Dict[str, Any]:
    """Terminate a subprocess cleanly (SIGTERM -> wait -> SIGKILL if needed)."""
    if proc.poll() is not None:
        return {
            "already_exited": True,
            "signal_used": None,
            "returncode": proc.returncode,
            "orphan_remains": False,
        }

    signal_used = "SIGTERM"
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        signal_used = "SIGKILL"
        proc.kill()
        proc.wait(timeout=3.0)

    # Confirm process PID is gone
    orphan_remains = False
    try:
        os.kill(proc.pid, 0)
        orphan_remains = True
    except ProcessLookupError:
        orphan_remains = False
    except PermissionError:
        orphan_remains = True

    return {
        "already_exited": False,
        "signal_used": signal_used,
        "returncode": proc.returncode,
        "orphan_remains": orphan_remains,
    }


def prepare_sandbox_home(
    out_dir: Path,
    data_dir: Path,
    global_ini_path: Path,
    preflight: Dict[str, Any],
) -> Tuple[Path, Path]:
    """Create a sandboxed HOME inside `out_dir` so PCSX2 never mutates global PCSX2.ini.

    Reads the user's global `PCSX2.ini` read-only and writes a redirected copy inside
    `out_dir/sandbox_home/Library/Application Support/PCSX2/inis/PCSX2.ini` pointing
    BIOS/GameSettings/Patches/Textures to their existing directories (no BIOS copying!)
    and Snapshots/Logs/Cache/Savestates inside `out_dir/`.
    """
    sandbox_home = (out_dir / "sandbox_home").resolve()
    sandbox_pcsx2 = sandbox_home / "Library" / "Application Support" / "PCSX2"
    sandbox_inis = sandbox_pcsx2 / "inis"
    sandbox_inis.mkdir(parents=True, exist_ok=True)

    snaps_dir = (out_dir / "snaps").resolve()
    logs_dir = (out_dir / "logs").resolve()
    cache_dir = (out_dir / "cache").resolve()
    sstates_dir = (out_dir / "sstates").resolve()
    for d in (snaps_dir, logs_dir, cache_dir, sstates_dir):
        d.mkdir(parents=True, exist_ok=True)

    bios_dir = preflight["global_pcsx2_ini"].get("bios_dir") or str((data_dir / "bios").resolve())
    folder_overrides = {
        "Bios": str(bios_dir),
        "Snapshots": str(snaps_dir),
        "Savestates": str(sstates_dir),
        "MemoryCards": str((data_dir / "memcards").resolve()),
        "Logs": str(logs_dir),
        "Cheats": str((out_dir / "cheats").resolve()),
        "Patches": str((data_dir / "patches").resolve()),
        "GameSettings": str((data_dir / "gamesettings").resolve()),
        "UserResources": str((data_dir / "resources").resolve()),
        "Cache": str(cache_dir),
        "Textures": str((data_dir / "textures").resolve()),
        "InputProfiles": str((data_dir / "inputprofiles").resolve()),
        "Videos": str((out_dir / "videos").resolve()),
        "DebuggerLayouts": str((out_dir / "debuggerlayouts").resolve()),
        "DebuggerSettings": str((out_dir / "debuggersettings").resolve()),
    }

    raw_lines = global_ini_path.read_text(encoding="utf-8", errors="replace").splitlines()
    out_lines: List[str] = []
    current_section = ""
    written_folders: set[str] = set()

    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_section == "Folders":
                for k, v in folder_overrides.items():
                    if k not in written_folders:
                        out_lines.append(f"{k} = {v}")
                        written_folders.add(k)
            current_section = stripped[1:-1].strip()
            out_lines.append(line)
            continue

        if current_section == "Folders" and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in folder_overrides:
                out_lines.append(f"{k} = {folder_overrides[k]}")
                written_folders.add(k)
                continue

        if current_section == "GameList" and stripped.startswith("RecursivePaths"):
            out_lines.append("RecursivePaths = ")
            continue

        out_lines.append(line)

    if current_section == "Folders":
        for k, v in folder_overrides.items():
            if k not in written_folders:
                out_lines.append(f"{k} = {v}")

    (sandbox_inis / "PCSX2.ini").write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return sandbox_home, snaps_dir


def verify_live_pcsx2(
    iso_path: Path = DEFAULT_ISO,
    *,
    pcsx2_app: Path = DEFAULT_PCSX2_APP,
    pcsx2_data_dir: Optional[Path] = None,
    out_dir: Path = DEFAULT_OUT_DIR,
    boot_wait_sec: float = 24.0,
    poll_interval_sec: float = 0.5,
    skip_screenshot: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run full pre-flight, live PCSX2 v2.6.3 boot, visual capture, clean shutdown, and JSON report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = (pcsx2_data_dir or default_pcsx2_data_dir()).expanduser()

    preflight = run_preflight(iso_path, pcsx2_app=pcsx2_app, pcsx2_data_dir=data_dir)
    global_ini_path = data_dir / "inis" / "PCSX2.ini"
    global_ini_sha_before = preflight["global_pcsx2_ini"].get("sha256")

    report_path = out_dir / "verify_report.json"
    boot_log_path = out_dir / "pcsx2_boot.log"
    emu_log_path = out_dir / "pcsx2_emulog.txt"

    report: Dict[str, Any] = {
        "tool": "pcsx2_live_verify.py",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": dry_run,
        "preflight": preflight,
        "launch": None,
        "parsed_log": None,
        "captures": [],
        "best_screenshot": None,
        "shutdown": None,
        "global_pcsx2_ini_untouched": None,
        "passed": False,
    }

    if dry_run or not preflight["ok"]:
        report["passed"] = bool(dry_run and preflight["ok"])
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    sandbox_home, snaps_dir = prepare_sandbox_home(out_dir, data_dir, global_ini_path, preflight)
    before_snaps = {p.name for p in snaps_dir.iterdir() if p.is_file()} if snaps_dir.is_dir() else set()

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
    child_env["HOME"] = str(sandbox_home)

    # Clear prior log artifacts in out_dir
    for lp in (boot_log_path, emu_log_path):
        if lp.exists():
            lp.unlink()

    start_ts = time.monotonic()
    proc: Optional[subprocess.Popen[bytes]] = None
    early_exit_code: Optional[int] = None
    captures: List[Dict[str, Any]] = []
    did_first_capture = False
    did_second_capture = False

    with boot_log_path.open("wb") as boot_log_fp:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=boot_log_fp,
                stderr=subprocess.STDOUT,
                cwd=str(out_dir),
                env=child_env,
            )
            pid = proc.pid

            expected_serial = preflight["iso"].get("serial")
            expected_crc = preflight["iso"].get("elf_crc")

            while True:
                elapsed = time.monotonic() - start_ts
                rc = proc.poll()
                if rc is not None:
                    early_exit_code = rc
                    break

                if not skip_screenshot:
                    # Capture mid-boot (~16s) and post-intro (~boot_wait_sec - 2s)
                    if not did_first_capture and elapsed >= min(16.0, boot_wait_sec * 0.65):
                        did_first_capture = True
                        cap1 = attempt_window_capture(pid, out_dir / "capture_window_1.png")
                        cap1["elapsed_sec"] = round(elapsed, 2)
                        captures.append(cap1)

                    if not did_second_capture and elapsed >= max(4.0, boot_wait_sec - 2.5):
                        did_second_capture = True
                        cap2 = attempt_window_capture(pid, out_dir / "capture_window_2.png")
                        cap2["elapsed_sec"] = round(elapsed, 2)
                        captures.append(cap2)

                        f8_cap = attempt_pcsx2_f8_snapshot(
                            pid,
                            snaps_dir,
                            before_snaps,
                            out_dir / "capture_pcsx2_f8.png",
                        )
                        f8_cap["elapsed_sec"] = round(time.monotonic() - start_ts, 2)
                        captures.append(f8_cap)

                if elapsed >= boot_wait_sec:
                    break
                time.sleep(poll_interval_sec)

            uptime_sec = round(time.monotonic() - start_ts, 2)
            report["launch"] = {
                "command": cmd,
                "pid": pid,
                "uptime_sec": uptime_sec,
                "early_exit": early_exit_code is not None,
                "early_exit_code": early_exit_code,
                "boot_log": str(boot_log_path),
                "emu_log": str(emu_log_path),
            }
        finally:
            if proc is not None:
                report["shutdown"] = terminate_process_cleanly(proc)

    # Read and parse combined logs
    combined_chunks: List[str] = []
    if emu_log_path.is_file():
        combined_chunks.append(emu_log_path.read_text(encoding="utf-8", errors="replace"))
    if boot_log_path.is_file():
        combined_chunks.append(boot_log_path.read_text(encoding="utf-8", errors="replace"))
    combined_log = "\n".join(combined_chunks)

    parsed = parse_pcsx2_log(
        combined_log,
        expected_serial=preflight["iso"].get("serial"),
        expected_crc=preflight["iso"].get("elf_crc"),
    )
    report["parsed_log"] = parsed
    report["captures"] = captures

    # Check if any newly generated snaps exist after shutdown even if F8 fired late
    if snaps_dir.is_dir():
        after_snaps = {p.name for p in snaps_dir.iterdir() if p.is_file()}
        new_snaps = sorted(after_snaps - before_snaps)
        if new_snaps and not any(c.get("method") == "pcsx2_f8_snaps" and c.get("captured") for c in captures):
            newest = max((snaps_dir / n for n in new_snaps), key=lambda p: p.stat().st_mtime)
            if _wait_for_complete_png(newest, timeout_sec=5.0):
                dst = out_dir / "capture_pcsx2_f8_late.png"
                shutil.copy2(newest, dst)
                img_info = analyze_screenshot(dst)
                is_valid = bool(img_info.get("width", 0) > 0 and not img_info.get("error"))
            else:
                img_info = analyze_screenshot(newest)
                is_valid = False
            captures.append(
                {
                    "method": "pcsx2_f8_snaps_late",
                    "captured": is_valid,
                    "source_snap": str(newest),
                    "image": img_info,
                }
            )

    # Pick best valid screenshot (prefer native PCSX2 F8 snap, then highest non-black coverage & std_luma)
    valid_images: List[Tuple[int, float, Dict[str, Any]]] = []
    for cap in captures:
        img = cap.get("image")
        if not img or not img.get("exists"):
            continue
        is_f8 = 1 if "f8" in str(cap.get("method", "")) else 0
        score = float(img.get("non_black_coverage", 0.0)) * float(img.get("std_luma", 0.0))
        valid_images.append((is_f8, score, img))

    if valid_images:
        valid_images.sort(key=lambda item: (item[0], item[1]), reverse=True)
        report["best_screenshot"] = valid_images[0][2]

    # Verify global PCSX2.ini hash unchanged
    if global_ini_path.is_file() and global_ini_sha_before is not None:
        global_ini_sha_after = _sha256_file(global_ini_path)
        report["global_pcsx2_ini_untouched"] = (global_ini_sha_before == global_ini_sha_after)
    else:
        report["global_pcsx2_ini_untouched"] = None

    no_orphan = not (report["shutdown"] or {}).get("orphan_remains", False)
    boot_ok = (
        early_exit_code is None
        and parsed["metal_gs_created"]
        and parsed["iso_mounted"]
        and not parsed["fatal_errors"]
        and no_orphan
    )
    report["passed"] = bool(boot_ok)

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live PCSX2 v2.6.3 Metal boot & frame verifier for NFSMW: Rockport 2026"
    )
    parser.add_argument(
        "iso_pos",
        nargs="?",
        type=Path,
        default=None,
        help="Optional positional path to target PS2 .iso",
    )
    parser.add_argument(
        "--iso",
        type=Path,
        default=None,
        help=f"Target PS2 .iso path (default: {DEFAULT_ISO})",
    )
    parser.add_argument(
        "--pcsx2-app",
        type=Path,
        default=DEFAULT_PCSX2_APP,
        help=f"Path to PCSX2.app bundle (default: {DEFAULT_PCSX2_APP})",
    )
    parser.add_argument(
        "--pcsx2-data-dir",
        type=Path,
        default=None,
        help="PCSX2 user data dir (default: ~/Library/Application Support/PCSX2)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Directory for logs, screenshots, and JSON report (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--boot-wait",
        type=float,
        default=24.0,
        help="Seconds to run live emulation before clean termination (default: 24.0)",
    )
    parser.add_argument(
        "--skip-screenshot",
        action="store_true",
        help="Skip macOS Quartz/screencapture & F8 capture steps",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run read-only pre-flight checks only without launching PCSX2",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON verification report to stdout",
    )
    args = parser.parse_args(argv)

    iso_path = args.iso or args.iso_pos or DEFAULT_ISO
    report = verify_live_pcsx2(
        iso_path=iso_path,
        pcsx2_app=args.pcsx2_app,
        pcsx2_data_dir=args.pcsx2_data_dir,
        out_dir=args.out_dir,
        boot_wait_sec=args.boot_wait,
        skip_screenshot=args.skip_screenshot,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        pre = report["preflight"]
        print(
            f"Preflight: {'OK' if pre['ok'] else 'FAILED'} | "
            f"ISO={pre['iso'].get('serial')} CRC={pre['iso'].get('elf_crc')} | "
            f"BIOS={pre['global_pcsx2_ini'].get('bios_files_count')} files"
        )
        if report["launch"]:
            ln = report["launch"]
            pl = report["parsed_log"] or {}
            print(
                f"Live Boot: PID={ln['pid']} uptime={ln['uptime_sec']}s "
                f"MetalGS={pl.get('metal_gs_created')} CRC_Match={pl.get('elf_crc_matched')} "
                f"INI_Loaded={pl.get('gamesettings_ini_loaded')} Patches={pl.get('pnach_patches_applied_count')} "
                f"FatalErrors={len(pl.get('fatal_errors', []))}"
            )
        if report.get("best_screenshot"):
            bs = report["best_screenshot"]
            print(
                f"Screenshot: {bs['path']} ({bs['width']}x{bs['height']}, "
                f"non_black={bs['non_black_coverage']}, valid={bs['is_valid_render']})"
            )
        print(f"Report written: {args.out_dir / 'verify_report.json'}")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
