#!/usr/bin/env python3
"""
rockport_cli.py — Unified zero-dependency CLI launcher for NFSMW: Rockport 2026 (SLUS-21351).

Subcommands:
  status     Inspect untouched source ISO (read-only), built modded ISO & CRC,
             installed PCSX2 per-game .ini/.pnach profiles, texture dump/replacement
             counts, and optional AI/DDS tool readiness.
  profiles   Scan and validate declarative build profiles in patches/profiles/*.json
             against patches/elf_patches.json (checking mutually exclusive pairs)
             and VLT recipe paths, printing a comparison table.
  build      One-click reproducible modded ISO build via build_modded_iso.py
             (--verify-source-sha256 enabled by default) with optional PCSX2 v2.6.3
             per-game INI/AspectRatio & companion .pnach installation.
  dump-mode  Toggle DumpReplaceableTextures cleanly inside per-game
             gamesettings/SLUS-21351_<CRC>.ini ONLY (never touches global PCSX2.ini).
  verify     Run py_compile, audit_repo_clean.py, lint_pnach.py, and unittest in one shot.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import py_compile
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402
import iso_inspect  # noqa: E402
import lint_pnach  # noqa: E402
import setup_pcsx2_macos  # noqa: E402

SERIAL = "SLUS-21351"
STOCK_ELF_NAME = "SLUS_213.51"
STOCK_ELF_CRC = "0518D274"
DEFAULT_SOURCE_ISO = (
    Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
)
DEFAULT_PCSX2_DIR = Path.home() / "Library" / "Application Support" / "PCSX2"

_CRC_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_RENDERER_NAMES = {
    "-1": "Automatic (Metal on macOS)",
    "11": "Null",
    "12": "OpenGL",
    "13": "Software",
    "14": "Vulkan",
    "17": "Metal",
}


# ---------------------------------------------------------------------------
# Read-only ISO & INI inspection helpers
# ---------------------------------------------------------------------------


def inspect_iso_readonly(iso_path: Path, verify_sha256: bool = False) -> Dict[str, Any]:
    """Strictly read-only ('rb') metadata & ELF CRC inspection of a PS2 ISO."""
    info: Dict[str, Any] = {
        "path": str(iso_path),
        "exists": iso_path.is_file(),
    }
    if not iso_path.is_file():
        return info

    info["size_bytes"] = iso_path.stat().st_size
    try:
        with iso_path.open("rb") as f:
            vol_id, vol_sectors, entries = iso_inspect.parse_iso9660(f)
            by_path = {e["path"].upper(): e for e in entries}
            info["volume_id"] = vol_id
            info["volume_sectors"] = vol_sectors
            elf_entry = by_path.get(STOCK_ELF_NAME)
            if elf_entry is not None:
                f.seek(elf_entry["lba"] * iso_inspect.SECTOR_SIZE)
                elf_bytes = f.read(elf_entry["size"])
                elf_meta = iso_inspect.parse_elf_header(elf_bytes)
                info["elf_name"] = STOCK_ELF_NAME
                info["elf_size"] = elf_meta["size"]
                info["elf_crc"] = elf_meta["pcsx2_crc"]
                info["is_stock_crc"] = elf_meta["pcsx2_crc"] == STOCK_ELF_CRC
            else:
                info["elf_error"] = f"{STOCK_ELF_NAME} not found in ISO9660 root"
    except Exception as exc:
        info["error"] = str(exc)

    if verify_sha256:
        info["sha256"] = iso_inspect.sha256_file(iso_path)
    return info


def parse_pcsx2_ini(text: str) -> Dict[str, Dict[str, List[str]]]:
    """Parse a PCSX2 INI preserving multi-value keys (e.g. [Patches] Enable)."""
    sections: Dict[str, Dict[str, List[str]]] = {}
    current: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        sec_m = _SECTION_RE.match(line)
        if sec_m:
            current = sec_m.group(1).strip()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        k, v = [p.strip() for p in line.split("=", 1)]
        sections[current].setdefault(k, []).append(v)
    return sections


def _ini_get_last(parsed: Dict[str, Dict[str, List[str]]], section: str, key: str, default: Optional[str] = None) -> Optional[str]:
    vals = parsed.get(section, {}).get(key)
    return vals[-1] if vals else default


def count_texture_dir(directory: Path) -> Dict[str, Any]:
    if not directory.is_dir():
        return {"path": str(directory), "exists": False, "total_files": 0, "dds": 0, "png": 0, "other": 0, "total_bytes": 0}
    total = dds = png = other = total_bytes = 0
    for p in directory.rglob("*"):
        if not p.is_file() or p.name.startswith("."):
            continue
        total += 1
        total_bytes += p.stat().st_size
        ext = p.suffix.lower()
        if ext == ".dds":
            dds += 1
        elif ext == ".png":
            png += 1
        else:
            other += 1
    return {
        "path": str(directory),
        "exists": True,
        "total_files": total,
        "dds": dds,
        "png": png,
        "other": other,
        "total_bytes": total_bytes,
    }


def collect_tool_readiness(repo_root: Path) -> Dict[str, Any]:
    py_modules = {}
    for mod in ("PIL", "numpy", "scipy", "torch", "spandrel", "tqdm"):
        py_modules[mod] = importlib.util.find_spec(mod) is not None

    cli_bins = {}
    for binary in ("dds_tool", "kram", "cuttlefish", "texconv", "compressonatorcli", "nvcompress", "magick", "xdelta3", "cargo", "swiftc"):
        found = shutil.which(binary)
        cli_bins[binary] = found is not None

    rockport_dds_bin = repo_root / "build" / "dds_wave3" / "target" / "release" / "rockport_dds"
    metal_pbr_bin = repo_root / "build" / "metal_wave3" / "RockportMetalPBR"
    metal_rt_bin = repo_root / "build" / "metal_rt_wave3" / "RockportMetalRT"

    models_dir = repo_root / "models"
    model_files = []
    if models_dir.is_dir():
        for ext in (".pth", ".safetensors", ".onnx"):
            model_files.extend(p.name for p in sorted(models_dir.glob(f"*{ext}")))

    pcsx2_apps = sorted(str(p) for p in Path("/Applications").glob("PCSX2*.app")) if Path("/Applications").is_dir() else []

    return {
        "python_version": sys.version.split()[0],
        "python_modules": py_modules,
        "dds_and_media_clis": cli_bins,
        "rockport_dds_built": rockport_dds_bin.is_file(),
        "metal_pbr_built": metal_pbr_bin.is_file(),
        "metal_rt_built": metal_rt_bin.is_file(),
        "dds_encoder_ready": rockport_dds_bin.is_file() or cli_bins.get("cargo", False) or any(
            cli_bins[k] for k in ("dds_tool", "kram", "cuttlefish", "texconv", "compressonatorcli", "nvcompress")
        ),
        "ai_upscaler_ready": py_modules["torch"] and py_modules["spandrel"] and py_modules["PIL"],
        "models_found": model_files,
        "pcsx2_apps": pcsx2_apps,
    }


def collect_status(
    *,
    repo_root: Path,
    source_iso: Path,
    build_dir: Path,
    pcsx2_dir: Path,
    verify_sha256: bool = False,
) -> Dict[str, Any]:
    source_info = inspect_iso_readonly(source_iso, verify_sha256=verify_sha256)

    # Built modded ISOs & manifests
    built_isos: List[Dict[str, Any]] = []
    manifests_by_iso: Dict[str, Dict[str, Any]] = {}
    if build_dir.is_dir():
        for mf in sorted(build_dir.glob("*.build-manifest.json")):
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                out_name = Path(data.get("output_iso", "")).name
                manifests_by_iso[out_name] = data
            except Exception:
                pass
        for iso in sorted(build_dir.glob("*.iso")):
            iso_info = inspect_iso_readonly(iso, verify_sha256=False)
            mf_data = manifests_by_iso.get(iso.name)
            if mf_data:
                iso_info["manifest_output_crc"] = mf_data.get("output_elf_crc")
                iso_info["built_at_utc"] = mf_data.get("built_at_utc")
                iso_info["applied_elf_patches"] = [p.get("id") for p in mf_data.get("applied_elf_patches", [])]
                comp = mf_data.get("companion_pnach")
                if comp:
                    iso_info["companion_pnach_exists"] = Path(comp).is_file()
            built_isos.append(iso_info)

    # PCSX2 per-game .ini & .pnach status (read-only!)
    gamesettings_dir = pcsx2_dir / "gamesettings"
    patches_dir = pcsx2_dir / "patches"
    global_ini = pcsx2_dir / "inis" / "PCSX2.ini"

    crc_set = set()
    if gamesettings_dir.is_dir():
        for p in gamesettings_dir.glob(f"{SERIAL}_*.ini"):
            crc = p.stem.split("_", 1)[1].upper()
            crc_set.add(crc)
    if patches_dir.is_dir():
        for p in patches_dir.glob(f"{SERIAL}_*.pnach"):
            crc = p.stem.split("_", 1)[1].upper()
            crc_set.add(crc)

    profiles: List[Dict[str, Any]] = []
    for crc in sorted(crc_set):
        ini_path = gamesettings_dir / f"{SERIAL}_{crc}.ini"
        pnach_path = patches_dir / f"{SERIAL}_{crc}.pnach"
        entry: Dict[str, Any] = {
            "crc": crc,
            "ini_exists": ini_path.is_file(),
            "ini_path": str(ini_path),
            "pnach_exists": pnach_path.is_file(),
            "pnach_path": str(pnach_path),
        }
        if ini_path.is_file():
            raw_ini = ini_path.read_text(encoding="utf-8", errors="replace")
            parsed = parse_pcsx2_ini(raw_ini)
            renderer_val = _ini_get_last(parsed, "EmuCore/GS", "Renderer")
            entry["renderer"] = renderer_val
            entry["renderer_name"] = _RENDERER_NAMES.get(renderer_val or "", "unset (inherits global)")
            entry["upscale_multiplier"] = _ini_get_last(parsed, "EmuCore/GS", "upscale_multiplier")
            entry["load_textures"] = _ini_get_last(parsed, "EmuCore/GS", "LoadTextureReplacements")
            entry["precache_textures"] = _ini_get_last(parsed, "EmuCore/GS", "PrecacheTextureReplacements")
            entry["dump_mode"] = _ini_get_last(parsed, "EmuCore/GS", "DumpReplaceableTextures")
            entry["enabled_patches"] = parsed.get("Patches", {}).get("Enable", [])
            entry["schema_warnings"] = setup_pcsx2_macos.validate_gamesettings_ini(raw_ini)
        profiles.append(entry)

    tex_base = pcsx2_dir / "textures" / SERIAL
    textures_info = {
        "pcsx2_dumps": count_texture_dir(tex_base / "dumps"),
        "pcsx2_replacements": count_texture_dir(tex_base / "replacements"),
    }

    return {
        "serial": SERIAL,
        "source_iso": source_info,
        "built_isos": built_isos,
        "pcsx2": {
            "data_dir": str(pcsx2_dir),
            "global_ini_exists": global_ini.is_file(),
            "profiles": profiles,
        },
        "textures": textures_info,
        "tools": collect_tool_readiness(repo_root),
    }


def _fmt_size(nbytes: int) -> str:
    if nbytes >= 1024 ** 3:
        return f"{nbytes / (1024 ** 3):.2f} GiB"
    if nbytes >= 1024 ** 2:
        return f"{nbytes / (1024 ** 2):.2f} MiB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.1f} KiB"
    return f"{nbytes} B"


def print_status_human(data: Dict[str, Any]) -> None:
    print("NFSMW: Rockport 2026 (SLUS-21351) — Toolchain Status")
    print("=" * 64)

    src = data["source_iso"]
    print("\n[1] Source PS2 ISO (read-only)")
    if src["exists"]:
        crc = src.get("elf_crc", "unknown")
        tag = "OK (untouched stock CRC)" if src.get("is_stock_crc") else f"UNEXPECTED CRC (expected {STOCK_ELF_CRC})"
        print(f"  Path   : {src['path']}")
        print(f"  Size   : {_fmt_size(src['size_bytes'])}")
        print(f"  ELF CRC: {crc} — {tag}")
        if "sha256" in src:
            print(f"  SHA-256: {src['sha256']}")
    else:
        print(f"  NOT FOUND: {src['path']}")

    print("\n[2] Built Modded ISO(s)")
    if not data["built_isos"]:
        print("  None found in build/. Run `make build-iso` to build one.")
    else:
        for bi in data["built_isos"]:
            crc = bi.get("elf_crc", bi.get("manifest_output_crc", "unknown"))
            patches = ", ".join(bi.get("applied_elf_patches", [])) or "(manifest not found)"
            print(f"  ISO    : {bi['path']} ({_fmt_size(bi['size_bytes'])})")
            print(f"  ELF CRC: {crc} | Patches: {patches}")

    print("\n[3] PCSX2 Per-Game Profiles (global PCSX2.ini untouched)")
    pcsx2 = data["pcsx2"]
    print(f"  Data Dir  : {pcsx2['data_dir']}")
    print(f"  Global INI: {'present (untouched)' if pcsx2['global_ini_exists'] else 'not found'}")
    if not pcsx2["profiles"]:
        print("  No SLUS-21351 per-game profiles installed yet. Run `make setup-pcsx2`.")
    else:
        for prof in pcsx2["profiles"]:
            ini_s = "OK" if prof["ini_exists"] else "MISSING"
            pn_s = "OK" if prof["pnach_exists"] else "MISSING"
            print(f"  CRC {prof['crc']}: gamesettings={ini_s}, pnach={pn_s}")
            if prof["ini_exists"]:
                print(
                    f"    Renderer={prof['renderer']} ({prof['renderer_name']}), "
                    f"Upscale={prof['upscale_multiplier']}x, "
                    f"LoadTex={prof['load_textures']}, Precache={prof['precache_textures']}, "
                    f"DumpMode={prof['dump_mode']}"
                )
                ep = prof.get("enabled_patches") or []
                print(f"    [Patches] Enable ({len(ep)}): {', '.join(ep) if ep else '(none explicit)'}")
                warns = prof.get("schema_warnings") or []
                if warns:
                    print(f"    [!] Schema warnings ({len(warns)}): {warns[0]}")

    print("\n[4] PCSX2 Texture Pipeline")
    dumps = data["textures"]["pcsx2_dumps"]
    repl = data["textures"]["pcsx2_replacements"]
    print(
        f"  Dumps       : {dumps['total_files']} files "
        f"(png={dumps['png']}, dds={dumps['dds']}, {_fmt_size(dumps['total_bytes'])})"
    )
    print(
        f"  Replacements: {repl['total_files']} files "
        f"(dds={repl['dds']}, png={repl['png']}, {_fmt_size(repl['total_bytes'])})"
    )

    print("\n[5] Optional AI / DDS Tool Readiness")
    tools = data["tools"]
    mods = ", ".join(f"{k}={'OK' if v else 'no'}" for k, v in tools["python_modules"].items())
    clis = ", ".join(f"{k}={'OK' if v else 'no'}" for k, v in tools["dds_and_media_clis"].items())
    print(f"  Python {tools['python_version']} : {mods}")
    print(f"  CLI Tools     : {clis}")
    print(
        f"  Readiness     : BC7 DDS Encoder={'READY' if tools['dds_encoder_ready'] else 'not installed'} "
        f"(rockport_dds={'built' if tools.get('rockport_dds_built') else 'unbuilt'}), "
        f"Metal PBR={'READY' if tools.get('metal_pbr_built') else 'unbuilt'}, "
        f"Metal 4 RT={'READY' if tools.get('metal_rt_built') else 'unbuilt'}, "
        f"AI Upscaler={'READY' if tools['ai_upscaler_ready'] else 'not installed'}, "
        f"Models={len(tools['models_found'])}"
    )


# ---------------------------------------------------------------------------
# dump-mode (strictly per-game gamesettings/SLUS-21351_<CRC>.ini ONLY!)
# ---------------------------------------------------------------------------


def toggle_ini_key_in_section(ini_text: str, section: str, key: str, value: str) -> str:
    """Update or insert `key = value` inside `[section]` while preserving comments & multi-keys."""
    lines = ini_text.splitlines()
    out: List[str] = []
    in_target = False
    found_section = False
    replaced = False
    target_key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=")

    for line in lines:
        sec_m = _SECTION_RE.match(line.strip())
        if sec_m:
            if in_target and not replaced:
                # Insert before leaving target section (before trailing blank lines if any)
                trailing_blanks: List[str] = []
                while out and out[-1].strip() == "":
                    trailing_blanks.append(out.pop())
                out.append(f"{key} = {value}")
                out.extend(reversed(trailing_blanks))
                replaced = True
            sec_name = sec_m.group(1).strip()
            in_target = sec_name == section
            if in_target:
                found_section = True
            out.append(line)
            continue

        if in_target and not replaced and target_key_re.match(line):
            out.append(f"{key} = {value}")
            replaced = True
            continue

        out.append(line)

    if in_target and not replaced:
        out.append(f"{key} = {value}")
        replaced = True

    if not found_section:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"[{section}]")
        out.append(f"{key} = {value}")

    return "\n".join(out) + "\n"


def set_dump_mode(
    *,
    pcsx2_dir: Path,
    enabled: bool,
    crcs: Optional[Sequence[str]] = None,
    create_if_missing: bool = False,
) -> List[Path]:
    """Toggle DumpReplaceableTextures strictly inside per-game gamesettings INI(s)."""
    gamesettings_dir = pcsx2_dir / "gamesettings"
    global_ini = (pcsx2_dir / "inis" / "PCSX2.ini").resolve()
    val_str = "true" if enabled else "false"

    targets: List[Path] = []
    if crcs:
        for crc in crcs:
            crc_u = crc.upper()
            if not _CRC_RE.match(crc_u):
                raise ValueError(f"Invalid 8-hex CRC: {crc!r}")
            targets.append(gamesettings_dir / f"{SERIAL}_{crc_u}.ini")
    else:
        if gamesettings_dir.is_dir():
            targets = sorted(gamesettings_dir.glob(f"{SERIAL}_*.ini"))
        if not targets:
            if create_if_missing:
                targets = [gamesettings_dir / f"{SERIAL}_{STOCK_ELF_CRC}.ini"]
            else:
                raise FileNotFoundError(
                    f"No per-game {SERIAL}_*.ini profiles found in {gamesettings_dir}. "
                    "Run `tools/setup_pcsx2_macos.py` first or pass `--create-if-missing`."
                )

    updated: List[Path] = []
    for ini_path in targets:
        resolved = ini_path.resolve()
        if resolved == global_ini or resolved.name.lower() == "pcsx2.ini" or "inis" in resolved.parts[:-1] and resolved.parent.name == "inis":
            raise RuntimeError(f"SAFETY ABORT: Refusing to modify global PCSX2 config: {ini_path}")

        if not ini_path.is_file():
            if not create_if_missing:
                raise FileNotFoundError(
                    f"Per-game INI does not exist: {ini_path}. Pass --create-if-missing to generate it."
                )
            gamesettings_dir.mkdir(parents=True, exist_ok=True)
            ini_text = setup_pcsx2_macos.build_gamesettings_ini(
                upscale=6,
                anisotropy=16,
                ee_cycle_rate=1,
                enable_dump=enabled,
                precache=True,
            )
            ini_path.write_text(ini_text, encoding="utf-8")
        else:
            orig = ini_path.read_text(encoding="utf-8")
            patched = toggle_ini_key_in_section(orig, "EmuCore/GS", "DumpReplaceableTextures", val_str)
            ini_path.write_text(patched, encoding="utf-8")

        updated.append(ini_path)

    return updated


# ---------------------------------------------------------------------------
# verify (py_compile + audit_repo_clean + lint_pnach + unittest)
# ---------------------------------------------------------------------------


def run_verify(
    *,
    repo_root: Path,
    skip_tests: bool = False,
    include_untracked: bool = False,
) -> Tuple[bool, List[Dict[str, Any]]]:
    results: List[Dict[str, Any]] = []

    # 1. py_compile all tools/*.py (and tests/*.py)
    py_files: List[Path] = []
    for sub in ("tools", "tests"):
        d = repo_root / sub
        if d.is_dir():
            py_files.extend(sorted(d.glob("*.py")))
    compile_errors: List[str] = []
    for pyf in py_files:
        try:
            py_compile.compile(str(pyf), doraise=True)
        except py_compile.PyCompileError as exc:
            compile_errors.append(f"{pyf.relative_to(repo_root)}: {exc.msg}")
    results.append(
        {
            "check": "py_compile",
            "ok": not compile_errors and bool(py_files),
            "summary": f"{len(py_files)} Python file(s) compiled" if not compile_errors else f"{len(compile_errors)} syntax error(s)",
            "details": compile_errors,
        }
    )

    # 2. audit_repo_clean
    rel_paths, audit_issues = audit_repo_clean.audit_repository(
        repo_root,
        include_untracked=include_untracked,
    )
    results.append(
        {
            "check": "audit_repo_clean",
            "ok": not audit_issues,
            "summary": f"{len(rel_paths)} file(s) scanned, {len(audit_issues)} issue(s)",
            "details": [i.format_human() for i in audit_issues],
        }
    )

    # 3. lint_pnach
    pnach_files = lint_pnach.collect_pnach_targets([], repo_root)
    pnach_errors: List[lint_pnach.LintError] = []
    for pf in pnach_files:
        pnach_errors.extend(lint_pnach.lint_pnach_file(pf))
    pnach_ok = bool(pnach_files) and not pnach_errors
    results.append(
        {
            "check": "lint_pnach",
            "ok": pnach_ok,
            "summary": (
                f"{len(pnach_files)} .pnach file(s) checked, {len(pnach_errors)} error(s)"
                if pnach_files
                else "No .pnach files found in patches/pnach/"
            ),
            "details": [e.format_human() for e in pnach_errors],
        }
    )

    # 3b. Validate build profiles, elf_caves.json & CC0 asset manifests (when present in repo_root)
    profiles_dir = repo_root / "patches" / "profiles"
    caves_path = repo_root / "patches" / "elf_caves.json"
    elf_spec_path = repo_root / "patches" / "elf_patches.json"
    manifest_paths = [
        repo_root / "assets_cc0" / "audio_manifest.json",
        repo_root / "assets_cc0" / "car_livery_rules.json",
    ]
    has_profiles = profiles_dir.is_dir() and any(profiles_dir.glob("*.json"))
    has_any_manifests = has_profiles or caves_path.is_file() or any(mp.is_file() for mp in manifest_paths)

    if has_any_manifests:
        manifest_errors: List[str] = []
        validated_profiles = 0
        total_profiles = 0
        if has_profiles:
            profile_report = collect_profiles(repo_root=repo_root)
            validated_profiles = profile_report["valid_count"]
            total_profiles = profile_report["profile_count"]
            if profile_report["status"] != "pass":
                for e in profile_report["profiles"]:
                    if not e["valid"]:
                        manifest_errors.extend(f"{e['profile_id']}: {err}" for err in e["errors"])

        if caves_path.is_file() and elf_spec_path.is_file():
            try:
                caves_data = json.loads(caves_path.read_text(encoding="utf-8"))
                elf_data = json.loads(elf_spec_path.read_text(encoding="utf-8"))
                elf_addrs = {
                    int(w["vaddr"], 16)
                    for p in elf_data.get("patches", [])
                    for w in p.get("writes", [])
                    if "vaddr" in w
                }
                for cave in caves_data.get("patches", []):
                    for w in cave.get("writes", []):
                        if "vaddr" in w and int(w["vaddr"], 16) in elf_addrs:
                            manifest_errors.append(
                                f"elf_caves.json ({cave.get('id')}) overlaps elf_patches.json at {w['vaddr']}"
                            )
            except Exception as exc:
                manifest_errors.append(f"elf_caves.json error: {exc}")

        for mp in manifest_paths:
            if mp.is_file():
                try:
                    json.loads(mp.read_text(encoding="utf-8"))
                except Exception as exc:
                    manifest_errors.append(f"Invalid JSON in {mp.relative_to(repo_root)}: {exc}")

        results.append(
            {
                "check": "profiles_manifests",
                "ok": not manifest_errors,
                "summary": (
                    f"{validated_profiles}/{total_profiles} profiles + caves/manifests valid"
                    if not manifest_errors
                    else f"{len(manifest_errors)} profile/manifest error(s)"
                ),
                "details": manifest_errors,
            }
        )

    # 4. unittest discover
    if not skip_tests:
        tests_dir = repo_root / "tests"
        if tests_dir.is_dir():
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir), "-v"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            combined = (proc.stdout + "\n" + proc.stderr).strip()
            last_lines = [ln for ln in combined.splitlines() if ln.strip()][-3:]
            results.append(
                {
                    "check": "unittest",
                    "ok": proc.returncode == 0,
                    "summary": " | ".join(last_lines) if last_lines else f"exit code {proc.returncode}",
                    "details": [] if proc.returncode == 0 else combined.splitlines(),
                }
            )
        else:
            results.append(
                {
                    "check": "unittest",
                    "ok": False,
                    "summary": "tests/ directory not found",
                    "details": [],
                }
            )

    all_ok = all(r["ok"] for r in results)
    return all_ok, results


# ---------------------------------------------------------------------------
# profiles (scan, validate mutually exclusive ELF IDs & VLT recipes, compare)
# ---------------------------------------------------------------------------


def _normalize_vpath(vpath: str) -> str:
    return vpath.strip().replace("/", "\\").upper()


def _resolve_repo_path(raw: str, repo_root: Path) -> Path:
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return (repo_root / p).resolve()


def infer_profile_aspect(include_ids: Sequence[str]) -> Tuple[str, str]:
    """Return (display_aspect, pcsx2_v263_aspect_ratio) for a profile's ELF IDs."""
    id_set = set(include_ids)
    if "ultrawide_21_9_native" in id_set:
        # PCSX2 v2.6.3 [EmuCore/GS] AspectRatio enum uses "Stretch" for 21:9 ultrawide.
        return "21:9", "Stretch"
    if "widescreen_16_9_native" in id_set or "wider_camera_fov_12pct_16_9" in id_set:
        return "16:9", "16:9"
    return "4:3", "Auto 4:3/3:2"


def resolve_pcsx2_aspect(aspect_arg: str, include_ids: Sequence[str]) -> Tuple[str, str]:
    """Map CLI --aspect value + profile ELF IDs to (display_aspect, PCSX2 AspectRatio)."""
    cleaned = aspect_arg.strip()
    low = cleaned.lower()
    if low == "auto":
        return infer_profile_aspect(include_ids)
    if low in ("21:9", "stretch"):
        return "21:9", "Stretch"
    for valid in setup_pcsx2_macos.VALID_ASPECT_RATIOS:
        if low == valid.lower():
            return valid, valid
    allowed = ("auto", "21:9") + setup_pcsx2_macos.VALID_ASPECT_RATIOS
    raise ValueError(f"Unsupported aspect ratio {aspect_arg!r}; expected one of {allowed}")


def resolve_profile_path(profile_arg: str, repo_root: Path) -> Path:
    """Resolve a profile ID, filename, or path to an existing profile JSON file."""
    profiles_dir = repo_root / "patches" / "profiles"
    raw_path = Path(profile_arg).expanduser()
    candidates = [
        raw_path,
        repo_root / raw_path,
        profiles_dir / f"{profile_arg}.json",
        profiles_dir / profile_arg,
    ]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()

    if profiles_dir.is_dir():
        for p in sorted(profiles_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("profile_id") == profile_arg:
                return p.resolve()

    available = [p.stem for p in sorted(profiles_dir.glob("*.json"))] if profiles_dir.is_dir() else []
    avail_str = ", ".join(available) if available else "(none found)"
    raise FileNotFoundError(
        f"Build profile not found: {profile_arg!r}. Available profiles in {profiles_dir}: {avail_str}"
    )


def validate_build_profile(
    profile_path: Path,
    repo_root: Path,
    default_spec_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load and validate a build profile JSON against ELF patch spec & VLT recipes."""
    errors: List[str] = []
    warnings: List[str] = []

    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "profile_id": profile_path.stem,
            "file": str(profile_path),
            "name": "",
            "description": "",
            "output_filename": "",
            "aspect": "unknown",
            "pcsx2_aspect_ratio": "16:9",
            "elf_patch_count": 0,
            "elf_patch_ids": [],
            "vlt_recipe_count": 0,
            "vlt_recipes": [],
            "vlt_edit_count": 0,
            "zz_override_count": 0,
            "allow_zzdata3_append": True,
            "valid": False,
            "errors": [f"Failed to parse profile JSON: {exc}"],
            "warnings": [],
        }

    if not isinstance(profile, dict):
        errors.append("Profile root must be a JSON object")
        profile = {}

    if profile.get("schema_version") != 1:
        errors.append(f"Expected schema_version == 1, got {profile.get('schema_version')!r}")

    profile_id = str(profile.get("profile_id") or profile_path.stem)
    name = str(profile.get("name") or "")
    description = str(profile.get("description") or "")
    output_filename = str(profile.get("output_filename") or "")

    if not profile.get("profile_id"):
        errors.append("Missing required 'profile_id'")
    if not name.strip():
        errors.append("Missing required 'name'")
    if not output_filename.strip():
        errors.append("Missing required 'output_filename'")

    elf_cfg = profile.get("elf_patches")
    if elf_cfg is None:
        elf_cfg = {}
    elif not isinstance(elf_cfg, dict):
        errors.append("'elf_patches' must be a JSON object")
        elf_cfg = {}

    # Resolve ELF patches spec
    spec_rel = elf_cfg.get("spec")
    if spec_rel:
        spec_path = _resolve_repo_path(str(spec_rel), repo_root)
    elif default_spec_path is not None:
        spec_path = default_spec_path.resolve()
    else:
        spec_path = (repo_root / "patches" / "elf_patches.json").resolve()

    spec: Optional[Dict[str, Any]] = None
    if not spec_path.is_file():
        errors.append(f"Missing ELF patch spec: {spec_path}")
    else:
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Invalid ELF patch spec JSON ({spec_path}): {exc}")

    # Validate target metadata against ELF spec target
    target = profile.get("target")
    if target is not None and not isinstance(target, dict):
        errors.append("'target' must be a JSON object")
        target = {}
    if isinstance(target, dict) and isinstance(spec, dict):
        spec_target = spec.get("target", {})
        for key in ("serial", "elf_name", "elf_pcsx2_crc"):
            if key in target and key in spec_target and str(target[key]).upper() != str(spec_target[key]).upper():
                errors.append(
                    f"target.{key} mismatch: profile has {target[key]!r}, ELF spec has {spec_target[key]!r}"
                )

    # Validate ELF patch IDs, mutual exclusions, and write address collisions
    selected_ids: List[str] = []
    include_optional = bool(elf_cfg.get("include_optional", False))
    if isinstance(spec, dict):
        patches_list = spec.get("patches", [])
        patch_by_id: Dict[str, Dict[str, Any]] = {p["id"]: p for p in patches_list if isinstance(p, dict) and "id" in p}
        mutual_exclusions: Dict[str, Set[str]] = {pid: set() for pid in patch_by_id}
        vaddrs_by_id: Dict[str, Set[int]] = {}
        for pid, patch_obj in patch_by_id.items():
            for other in patch_obj.get("mutually_exclusive_with", []) or []:
                mutual_exclusions.setdefault(pid, set()).add(other)
                mutual_exclusions.setdefault(other, set()).add(pid)
            vaddrs_by_id[pid] = {
                int(w["vaddr"], 16)
                for w in patch_obj.get("writes", [])
                if isinstance(w, dict) and "vaddr" in w
            }

        if "include_ids" in elf_cfg:
            raw_ids = elf_cfg["include_ids"]
            if not isinstance(raw_ids, list) or not all(isinstance(x, str) for x in raw_ids):
                errors.append("'elf_patches.include_ids' must be a list of patch ID strings")
            else:
                selected_ids = list(raw_ids)
        else:
            selected_ids = [
                p["id"]
                for p in patches_list
                if isinstance(p, dict) and "id" in p and (p.get("enabled_by_default", False) or include_optional)
            ]

        seen_ids: List[str] = []
        claimed_vaddrs: Dict[int, str] = {}
        for pid in selected_ids:
            if pid not in patch_by_id:
                errors.append(f"Unknown ELF patch ID: {pid!r}")
                continue
            if pid in seen_ids:
                errors.append(f"Duplicate ELF patch ID in include_ids: {pid!r}")
                continue
            for prev_id in seen_ids:
                if prev_id in mutual_exclusions.get(pid, set()):
                    errors.append(f"Mutually exclusive ELF patches both enabled: {prev_id!r} and {pid!r}")
            for vaddr in sorted(vaddrs_by_id.get(pid, set())):
                if vaddr in claimed_vaddrs:
                    errors.append(
                        f"Overlapping ELF write at 0x{vaddr:08X} between {claimed_vaddrs[vaddr]!r} and {pid!r}"
                    )
                else:
                    claimed_vaddrs[vaddr] = pid
            seen_ids.append(pid)

    # Validate VLT recipes
    raw_vlt = profile.get("vlt_recipes", [])
    validated_vlt: List[Dict[str, Any]] = []
    total_vlt_edits = 0
    seen_vlt_offsets: Dict[Tuple[str, int], str] = {}
    if raw_vlt is not None and not isinstance(raw_vlt, list):
        errors.append("'vlt_recipes' must be a list")
        raw_vlt = []
    for idx, item in enumerate(raw_vlt or []):
        if not isinstance(item, dict):
            errors.append(f"vlt_recipes[{idx}] must be an object")
            continue
        vpath = item.get("virtual_path")
        recipe_rel = item.get("recipe")
        is_optional = bool(item.get("optional", False))
        if not isinstance(vpath, str) or not vpath.strip():
            errors.append(f"vlt_recipes[{idx}] missing 'virtual_path'")
            continue
        if not isinstance(recipe_rel, str) or not recipe_rel.strip():
            errors.append(f"vlt_recipes[{idx}] missing 'recipe'")
            continue

        norm_vpath = _normalize_vpath(vpath)
        recipe_path = _resolve_repo_path(recipe_rel, repo_root)
        entry: Dict[str, Any] = {
            "virtual_path": norm_vpath,
            "recipe": recipe_rel,
            "optional": is_optional,
            "exists": recipe_path.is_file(),
            "edit_count": 0,
        }

        if not recipe_path.is_file():
            if is_optional:
                warnings.append(f"Optional VLT recipe missing: {recipe_rel}")
            else:
                errors.append(f"Missing VLT recipe file: {recipe_rel} ({recipe_path})")
            validated_vlt.append(entry)
            continue

        try:
            recipe_data = json.loads(recipe_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Invalid VLT recipe JSON ({recipe_rel}): {exc}")
            validated_vlt.append(entry)
            continue

        if not isinstance(recipe_data, dict) or not isinstance(recipe_data.get("patches"), list):
            errors.append(f"VLT recipe {recipe_rel} must be a JSON object with a 'patches' list")
            validated_vlt.append(entry)
            continue

        recipe_vpath = recipe_data.get("target", {}).get("virtual_path")
        if recipe_vpath and _normalize_vpath(str(recipe_vpath)) != norm_vpath:
            errors.append(
                f"VLT recipe {recipe_rel} targets {recipe_vpath!r}, but profile assigns {vpath!r}"
            )

        recipe_id = str(recipe_data.get("id") or recipe_path.stem)
        patches_in_recipe = recipe_data["patches"]
        entry["id"] = recipe_id
        entry["edit_count"] = len(patches_in_recipe)
        total_vlt_edits += len(patches_in_recipe)

        for p_idx, p_edit in enumerate(patches_in_recipe):
            if not isinstance(p_edit, dict):
                errors.append(f"VLT recipe {recipe_rel} patches[{p_idx}] must be an object")
                continue
            off_raw = p_edit.get("expected_offset")
            if isinstance(off_raw, str):
                try:
                    off_int = int(off_raw, 16)
                except ValueError:
                    errors.append(f"VLT recipe {recipe_rel} patches[{p_idx}] invalid expected_offset {off_raw!r}")
                    continue
                key = (norm_vpath, off_int)
                if key in seen_vlt_offsets:
                    errors.append(
                        f"Overlapping VLT offset 0x{off_int:08X} in {norm_vpath} "
                        f"between {seen_vlt_offsets[key]} and {recipe_rel}"
                    )
                else:
                    seen_vlt_offsets[key] = recipe_rel

        validated_vlt.append(entry)

    # Validate optional lighting_recipes (GLOBALB.LZC SunInfos + LightMaterials)
    raw_lighting = profile.get("lighting_recipes", [])
    validated_lighting: List[Dict[str, Any]] = []
    total_lighting_edits = 0
    if raw_lighting is not None and not isinstance(raw_lighting, list):
        errors.append("'lighting_recipes' must be a list")
        raw_lighting = []
    for idx, item in enumerate(raw_lighting or []):
        if isinstance(item, str):
            recipe_rel = item
            is_optional = False
        elif isinstance(item, dict):
            recipe_rel = item.get("recipe", "")
            is_optional = bool(item.get("optional", False))
        else:
            errors.append(f"lighting_recipes[{idx}] must be a path string or object")
            continue
        if not isinstance(recipe_rel, str) or not recipe_rel.strip():
            errors.append(f"lighting_recipes[{idx}] missing 'recipe'")
            continue
        recipe_path = _resolve_repo_path(recipe_rel, repo_root)
        lentry: Dict[str, Any] = {
            "recipe": recipe_rel,
            "optional": is_optional,
            "exists": recipe_path.is_file(),
            "edit_count": 0,
        }
        if not recipe_path.is_file():
            if is_optional:
                warnings.append(f"Optional lighting recipe missing: {recipe_rel}")
            else:
                errors.append(f"Missing lighting recipe file: {recipe_rel} ({recipe_path})")
            validated_lighting.append(lentry)
            continue
        try:
            ldata = json.loads(recipe_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Invalid lighting recipe JSON ({recipe_rel}): {exc}")
            validated_lighting.append(lentry)
            continue
        if not isinstance(ldata, dict) or not isinstance(ldata.get("patches"), list):
            errors.append(f"Lighting recipe {recipe_rel} must be a JSON object with a 'patches' list")
            validated_lighting.append(lentry)
            continue
        lentry["id"] = str(ldata.get("id") or recipe_path.stem)
        lentry["edit_count"] = len(ldata["patches"])
        total_lighting_edits += len(ldata["patches"])
        validated_lighting.append(lentry)

    # Validate optional zz_overrides
    raw_zz = profile.get("zz_overrides", [])
    zz_items: List[Dict[str, Any]] = []
    if isinstance(raw_zz, dict):
        zz_items = [{"virtual_path": k, "file": v} for k, v in raw_zz.items()]
    elif isinstance(raw_zz, list):
        zz_items = [x for x in raw_zz if isinstance(x, dict)]
    elif raw_zz is not None:
        errors.append("'zz_overrides' must be a list or object")

    for idx, item in enumerate(zz_items):
        fp_raw = item.get("file")
        if not isinstance(fp_raw, str) or not fp_raw.strip():
            errors.append(f"zz_overrides[{idx}] missing 'file'")
            continue
        fp = _resolve_repo_path(fp_raw, repo_root)
        if not fp.is_file() and not item.get("optional", False):
            errors.append(f"Missing ZZ override file: {fp_raw}")

    display_aspect, pcsx2_aspect = infer_profile_aspect(selected_ids)
    return {
        "profile_id": profile_id,
        "file": str(profile_path),
        "name": name,
        "description": description,
        "output_filename": output_filename,
        "aspect": display_aspect,
        "pcsx2_aspect_ratio": pcsx2_aspect,
        "elf_spec": str(spec_path),
        "elf_patch_count": len(selected_ids),
        "elf_patch_ids": selected_ids,
        "vlt_recipe_count": len(validated_vlt),
        "vlt_recipes": validated_vlt,
        "vlt_edit_count": total_vlt_edits,
        "lighting_recipe_count": len(validated_lighting),
        "lighting_recipes": validated_lighting,
        "lighting_edit_count": total_lighting_edits,
        "zz_override_count": len(zz_items),
        "allow_zzdata3_append": bool(profile.get("allow_zzdata3_append", True)),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def collect_profiles(
    *,
    repo_root: Path,
    profiles_dir: Optional[Path] = None,
    patches_spec: Optional[Path] = None,
) -> Dict[str, Any]:
    """Scan and validate all *.json build profiles under patches/profiles/."""
    target_dir = (profiles_dir or (repo_root / "patches" / "profiles")).resolve()
    spec_path = (patches_spec or (repo_root / "patches" / "elf_patches.json")).resolve()

    profile_files = sorted(target_dir.glob("*.json")) if target_dir.is_dir() else []
    entries: List[Dict[str, Any]] = []
    seen_profile_ids: Dict[str, str] = {}

    for pf in profile_files:
        info = validate_build_profile(pf, repo_root=repo_root, default_spec_path=spec_path)
        pid = info["profile_id"]
        if pid in seen_profile_ids:
            info["errors"].append(f"Duplicate profile_id {pid!r} (already defined in {seen_profile_ids[pid]})")
            info["valid"] = False
        else:
            seen_profile_ids[pid] = pf.name
        entries.append(info)

    valid_count = sum(1 for e in entries if e["valid"])
    invalid_count = len(entries) - valid_count
    all_ok = bool(entries) and invalid_count == 0
    return {
        "status": "pass" if all_ok else "fail",
        "profiles_dir": str(target_dir),
        "elf_spec": str(spec_path),
        "profile_count": len(entries),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "profiles": entries,
    }


def print_profiles_human(report: Dict[str, Any]) -> None:
    print("NFSMW: Rockport 2026 (SLUS-21351) — Build Profiles")
    print("=" * 96)
    print(f"Profiles Dir : {report['profiles_dir']}")
    print(f"ELF Spec     : {report['elf_spec']}")

    entries = report["profiles"]
    if not entries:
        print("\nNo profile JSON files found.")
        return

    print("")
    id_w = max(len("Profile ID"), max(len(e["profile_id"]) for e in entries)) + 2
    header = (
        f"{'Profile ID':<{id_w}} {'Aspect':<7} {'PCSX2 AR':<12} {'ELF':>3}  "
        f"{'VLT (Edits)':<12} {'Status':<8} {'Output ISO'}"
    )
    print(header)
    print("-" * len(header))
    for e in entries:
        vlt_col = f"{e['vlt_recipe_count']} ({e['vlt_edit_count']})"
        status_col = "VALID" if e["valid"] else "INVALID"
        print(
            f"{e['profile_id']:<{id_w}} {e['aspect']:<7} {e['pcsx2_aspect_ratio']:<12} "
            f"{e['elf_patch_count']:>3}  {vlt_col:<12} {status_col:<8} {e['output_filename']}"
        )

    print("\nDetails:")
    for e in entries:
        badge = "VALID" if e["valid"] else "INVALID"
        print(f"  * {e['profile_id']} [{badge}] — {e['name']}")
        if e["elf_patch_ids"]:
            print(f"      ELF ({e['elf_patch_count']}): {', '.join(e['elf_patch_ids'])}")
        if e["vlt_recipes"]:
            vlt_names = [f"{Path(v['recipe']).name} ({v['edit_count']})" for v in e["vlt_recipes"]]
            print(f"      VLT ({e['vlt_recipe_count']}): {', '.join(vlt_names)}")
        else:
            print("      VLT (0): Stock retail GLOBAL\\ATTRIBUTES.BIN preserved")
        if e.get("lighting_recipes"):
            light_names = [f"{Path(v['recipe']).name} ({v['edit_count']})" for v in e["lighting_recipes"]]
            print(f"      Lighting ({e['lighting_recipe_count']}): {', '.join(light_names)}")
        for w in e["warnings"]:
            print(f"      WARN : {w}")
        for err in e["errors"]:
            print(f"      ERROR: {err}", file=sys.stderr)

    print(
        f"\nSummary: {report['valid_count']}/{report['profile_count']} profile(s) valid"
        + ("" if report["invalid_count"] == 0 else f", {report['invalid_count']} invalid")
    )


# ---------------------------------------------------------------------------
# build (one-click build_modded_iso.py + optional setup_pcsx2_macos.py)
# ---------------------------------------------------------------------------


def _extract_companion_pnach_patches(pnach_path: Path, is_ultrawide: bool) -> List[str]:
    """Return section names from a modded companion .pnach that require explicit Enable."""
    if not pnach_path.is_file():
        return []
    sections: List[str] = []
    for raw_line in pnach_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        m = _SECTION_RE.match(stripped)
        if not m:
            continue
        name = m.group(1).strip()
        low = name.lower()
        if "optional" in low or "experimental" in low:
            continue
        if ("21:9" in low or "ultrawide" in low) and not is_ultrawide:
            continue
        if name not in sections:
            sections.append(name)
    return sections


def _apply_aspect_to_gamesettings_ini(ini_path: Path, pcsx2_dir: Path, aspect_ratio: str) -> bool:
    """Set [EmuCore/GS] AspectRatio safely inside a per-game INI and validate against PCSX2 v2.6.3."""
    if aspect_ratio not in setup_pcsx2_macos.VALID_ASPECT_RATIOS:
        raise ValueError(
            f"AspectRatio {aspect_ratio!r} not in PCSX2 v2.6.3 enum {setup_pcsx2_macos.VALID_ASPECT_RATIOS}"
        )
    global_ini = (pcsx2_dir / "inis" / "PCSX2.ini").resolve()
    resolved = ini_path.resolve()
    if resolved == global_ini or resolved.name.lower() == "pcsx2.ini" or resolved.parent.name == "inis":
        raise RuntimeError(f"SAFETY ABORT: Refusing to modify global PCSX2 config: {ini_path}")
    if not ini_path.is_file():
        return False

    orig = ini_path.read_text(encoding="utf-8")
    updated = toggle_ini_key_in_section(orig, "EmuCore/GS", "AspectRatio", aspect_ratio)
    errs = setup_pcsx2_macos.validate_gamesettings_ini(updated)
    if errs:
        raise RuntimeError("Updated gamesettings INI failed PCSX2 v2.6.3 validation:\n" + "\n".join(errs))
    ini_path.write_text(updated, encoding="utf-8")
    return True


def run_build(
    *,
    repo_root: Path,
    profile_arg: str,
    source_iso: Path,
    output_iso: Optional[Path] = None,
    verify_sha256: bool = True,
    install_pcsx2: bool = False,
    pcsx2_dir: Path = DEFAULT_PCSX2_DIR,
    renderer: str = "metal",
    upscale: int = 6,
    anisotropy: int = 16,
    ee_cycle_rate: int = 2,
    aspect: str = "auto",
    enable_dump: bool = False,
    no_precache: bool = False,
    dry_run: bool = False,
) -> Tuple[int, Dict[str, Any]]:
    """Execute or dry-run the one-click modded ISO build + optional PCSX2 install workflow."""
    profile_path = resolve_profile_path(profile_arg, repo_root)
    profile_info = validate_build_profile(profile_path, repo_root=repo_root)
    if not profile_info["valid"]:
        return 1, {
            "status": "error",
            "stage": "validate-profile",
            "profile": profile_info,
            "errors": profile_info["errors"],
        }

    src = source_iso.expanduser().resolve()
    dst = (
        output_iso.expanduser().resolve()
        if output_iso is not None
        else (repo_root / "build" / profile_info["output_filename"]).resolve()
    )
    if src == dst:
        return 2, {
            "status": "error",
            "stage": "safety-check",
            "errors": [f"Refused: output ISO path matches source ISO path ({src})"],
        }

    display_aspect, pcsx2_aspect = resolve_pcsx2_aspect(aspect, profile_info["elf_patch_ids"])
    is_ultrawide = "ultrawide_21_9_native" in set(profile_info["elf_patch_ids"])

    build_script = (repo_root / "tools" / "build_modded_iso.py").resolve()
    setup_script = (repo_root / "tools" / "setup_pcsx2_macos.py").resolve()

    build_cmd: List[str] = [
        sys.executable,
        str(build_script),
        "--source",
        str(src),
        "--output",
        str(dst),
        "--profile",
        str(profile_path),
    ]
    if verify_sha256:
        build_cmd.append("--verify-source-sha256")

    planned_setup_cmd: Optional[List[str]] = None
    if install_pcsx2:
        planned_setup_cmd = [
            sys.executable,
            str(setup_script),
            "--pcsx2-dir",
            str(pcsx2_dir),
            "--crcs",
            "<MODDED_CRC>",
            "--extra-pnach",
            str(dst.parent / f"{SERIAL}_<MODDED_CRC>.pnach"),
            "--renderer",
            renderer,
            "--upscale",
            str(upscale),
            "--anisotropy",
            str(anisotropy),
            "--ee-cycle-rate",
            str(ee_cycle_rate),
        ]
        if is_ultrawide:
            planned_setup_cmd.extend(["--enable-patch", "Widescreen 21:9 (Runtime Confirm)"])
        if enable_dump:
            planned_setup_cmd.append("--enable-dump")
        if no_precache:
            planned_setup_cmd.append("--no-precache")

    if dry_run:
        src_info = inspect_iso_readonly(src, verify_sha256=False)
        return 0, {
            "status": "dry-run",
            "dry_run": True,
            "profile": profile_info,
            "source_iso": src_info,
            "output_iso": str(dst),
            "verify_source_sha256": verify_sha256,
            "display_aspect": display_aspect,
            "pcsx2_aspect_ratio": pcsx2_aspect,
            "install_pcsx2": install_pcsx2,
            "build_command": build_cmd,
            "setup_command": planned_setup_cmd,
            "pcsx2_settings": {
                "pcsx2_dir": str(pcsx2_dir),
                "renderer": renderer,
                "upscale": upscale,
                "anisotropy": anisotropy,
                "ee_cycle_rate": ee_cycle_rate,
                "aspect_ratio": pcsx2_aspect,
            },
        }

    if not src.is_file():
        return 1, {
            "status": "error",
            "stage": "source-check",
            "errors": [f"Source ISO not found: {src}"],
        }

    build_proc = subprocess.run(
        build_cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if build_proc.returncode != 0:
        return build_proc.returncode, {
            "status": "error",
            "stage": "build_modded_iso",
            "returncode": build_proc.returncode,
            "stdout": build_proc.stdout,
            "stderr": build_proc.stderr,
            "errors": [f"build_modded_iso.py exited with code {build_proc.returncode}"],
        }

    manifest_path = dst.with_suffix(".build-manifest.json")
    if not manifest_path.is_file():
        return 1, {
            "status": "error",
            "stage": "read-manifest",
            "stdout": build_proc.stdout,
            "stderr": build_proc.stderr,
            "errors": [f"Expected build manifest was not created: {manifest_path}"],
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    modded_crc = str(manifest["output_elf_crc"]).upper()
    pnach_name = manifest.get("companion_pnach") or f"{SERIAL}_{modded_crc}.pnach"
    companion_pnach = dst.parent / pnach_name

    pcsx2_result: Optional[Dict[str, Any]] = None
    if install_pcsx2:
        setup_cmd = [
            sys.executable,
            str(setup_script),
            "--pcsx2-dir",
            str(pcsx2_dir),
            "--crcs",
            modded_crc,
            "--renderer",
            renderer,
            "--upscale",
            str(upscale),
            "--anisotropy",
            str(anisotropy),
            "--ee-cycle-rate",
            str(ee_cycle_rate),
        ]
        if companion_pnach.is_file():
            setup_cmd.extend(["--extra-pnach", str(companion_pnach)])

        # setup_pcsx2_macos.extract_default_pnach_sections skips "21:9"/"ultrawide" tokens
        # (meant for optional sections in stock 0518D274.pnach); enable companion 21:9 explicitly.
        if is_ultrawide:
            explicit_sections = _extract_companion_pnach_patches(companion_pnach, is_ultrawide=True)
            if not explicit_sections:
                explicit_sections = ["Widescreen 21:9 (Runtime Confirm)"]
            for sec_name in explicit_sections:
                setup_cmd.extend(["--enable-patch", sec_name])

        if enable_dump:
            setup_cmd.append("--enable-dump")
        if no_precache:
            setup_cmd.append("--no-precache")

        setup_proc = subprocess.run(
            setup_cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if setup_proc.returncode != 0:
            return setup_proc.returncode, {
                "status": "error",
                "stage": "setup_pcsx2_macos",
                "returncode": setup_proc.returncode,
                "stdout": setup_proc.stdout,
                "stderr": setup_proc.stderr,
                "errors": [f"setup_pcsx2_macos.py exited with code {setup_proc.returncode}"],
            }

        ini_path = pcsx2_dir / "gamesettings" / f"{SERIAL}_{modded_crc}.ini"
        installed_pnach = pcsx2_dir / "patches" / companion_pnach.name
        aspect_updated = _apply_aspect_to_gamesettings_ini(ini_path, pcsx2_dir, pcsx2_aspect)
        enabled_patches: List[str] = []
        if ini_path.is_file():
            parsed_ini = parse_pcsx2_ini(ini_path.read_text(encoding="utf-8", errors="replace"))
            enabled_patches = parsed_ini.get("Patches", {}).get("Enable", [])

        pcsx2_result = {
            "pcsx2_dir": str(pcsx2_dir),
            "ini_path": str(ini_path),
            "ini_exists": ini_path.is_file(),
            "pnach_path": str(installed_pnach),
            "pnach_exists": installed_pnach.is_file(),
            "renderer": renderer,
            "upscale": upscale,
            "anisotropy": anisotropy,
            "ee_cycle_rate": ee_cycle_rate,
            "aspect_ratio": pcsx2_aspect,
            "ini_aspect_updated": aspect_updated,
            "enabled_patches": enabled_patches,
            "global_ini_untouched": True,
            "setup_command": setup_cmd,
            "setup_stdout": setup_proc.stdout,
        }

    return 0, {
        "status": "ok",
        "dry_run": False,
        "profile": profile_info,
        "source_iso": str(src),
        "source_elf_crc": manifest.get("source_elf_crc"),
        "source_sha256_before": manifest.get("source_sha256_before"),
        "source_sha256_after": manifest.get("source_sha256_after"),
        "verify_source_sha256": verify_sha256,
        "output_iso": str(dst),
        "output_elf_crc": modded_crc,
        "copy_mode": manifest.get("copy_mode"),
        "companion_pnach": str(companion_pnach),
        "manifest_path": str(manifest_path),
        "applied_elf_patches": [p.get("id") for p in manifest.get("applied_elf_patches", [])],
        "applied_zz_overrides": [z.get("virtual_path") for z in manifest.get("applied_zz_overrides", [])],
        "iso_layout_update": manifest.get("iso_layout_update", {}),
        "display_aspect": display_aspect,
        "pcsx2_aspect_ratio": pcsx2_aspect,
        "build_command": build_cmd,
        "build_stdout": build_proc.stdout,
        "pcsx2": pcsx2_result,
    }


def print_build_human(res: Dict[str, Any]) -> None:
    prof = res["profile"]
    if res.get("dry_run"):
        print("NFSMW: Rockport 2026 (SLUS-21351) — Build Plan (Dry Run)")
        print("=" * 68)
        src = res["source_iso"]
        if src.get("exists"):
            crc = src.get("elf_crc", "unknown")
            print(f"  Source ISO   : {src['path']} ({_fmt_size(src['size_bytes'])}, CRC {crc})")
        else:
            print(f"  Source ISO   : {src['path']} (not found locally; skipped read-only CRC check)")
        print(f"  Profile      : {prof['profile_id']} — {prof['name']}")
        print(f"  Output ISO   : {res['output_iso']}")
        print(f"  Verify SHA256: {res['verify_source_sha256']}")
        print(f"  ELF Patches  : {prof['elf_patch_count']} ({', '.join(prof['elf_patch_ids'])})")
        print(f"  VLT Recipes  : {prof['vlt_recipe_count']} ({prof['vlt_edit_count']} field edits)")
        print(f"  Aspect Ratio : {res['display_aspect']} (PCSX2 AspectRatio = {res['pcsx2_aspect_ratio']})")
        print(f"  Build Cmd    : {shlex.join(res['build_command'])}")
        if res.get("setup_command"):
            print(f"  PCSX2 Cmd    : {shlex.join(res['setup_command'])}")
        print("\nDry run only — no ISO, INI, or .pnach files were modified.")
        return

    print("NFSMW: Rockport 2026 (SLUS-21351) — Build & Launch Summary")
    print("=" * 68)
    print(f"  Profile         : {prof['profile_id']} — {prof['name']}")
    print(f"  Source ISO      : {res['source_iso']} (CRC {res.get('source_elf_crc')})")
    if res.get("verify_source_sha256"):
        sha_ok = res.get("source_sha256_before") == res.get("source_sha256_after")
        print(
            f"  Source SHA-256  : {'UNCHANGED' if sha_ok else 'MISMATCH'} "
            f"({res.get('source_sha256_after')})"
        )
    print(f"  Modded ISO      : {res['output_iso']} (via {res.get('copy_mode')})")
    print(f"  Modded ELF CRC  : {res['output_elf_crc']} (stock was {res.get('source_elf_crc')})")
    print(f"  Companion PNACH : {res['companion_pnach']}")
    print(f"  Build Manifest  : {res['manifest_path']}")
    elf_ids = res.get("applied_elf_patches") or []
    zz_ovs = res.get("applied_zz_overrides") or []
    print(f"  ELF Applied ({len(elf_ids):2d}): {', '.join(elf_ids) if elf_ids else '(none)'}")
    print(f"  ZZ Overrides ({len(zz_ovs)}): {', '.join(zz_ovs) if zz_ovs else '(none — stock ZZDATA)'}")
    print(f"  Aspect Ratio    : {res['display_aspect']} (PCSX2 AspectRatio = {res['pcsx2_aspect_ratio']})")

    pcsx2 = res.get("pcsx2")
    if pcsx2:
        print("\n[PCSX2 Per-Game Profile Installed]")
        print(f"  Gamesettings INI: {pcsx2['ini_path']}")
        print(f"  Installed PNACH : {pcsx2['pnach_path']}")
        print(
            f"  Settings        : Renderer={pcsx2['renderer']}, Upscale={pcsx2['upscale']}x, "
            f"AF={pcsx2['anisotropy']}x, EECycleRate={pcsx2['ee_cycle_rate']}, "
            f"AspectRatio={pcsx2['aspect_ratio']}"
        )
        ep = pcsx2.get("enabled_patches") or []
        print(f"  [Patches] Enable: {', '.join(ep) if ep else '(none)'}")
        print("  Safety          : Global PCSX2.ini was NOT modified.")
        print(f"\nReady to launch in PCSX2: open {res['output_iso']}")
    else:
        print("\nNext step — install PCSX2 per-game profile for this modded CRC:")
        print(
            f"  python3 tools/rockport_cli.py build --profile {prof['profile_id']} "
            f"--source \"{res['source_iso']}\" --install-pcsx2"
        )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rockport_cli.py",
        description="NFSMW: Rockport 2026 (SLUS-21351) unified CLI launcher & verifier",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="Inspect source ISO, modded builds, PCSX2 profiles, textures & tools")
    p_status.add_argument(
        "--source-iso",
        type=Path,
        default=Path(os.environ.get("ROCKPORT_SOURCE_ISO", str(DEFAULT_SOURCE_ISO))),
        help="Path to clean untouched SLUS-21351 ISO (opened strictly read-only)",
    )
    p_status.add_argument("--build-dir", type=Path, default=REPO_ROOT / "build")
    p_status.add_argument("--pcsx2-dir", type=Path, default=DEFAULT_PCSX2_DIR)
    p_status.add_argument("--verify-sha256", action="store_true", help="Also hash full source ISO (slower)")
    p_status.add_argument("--json", action="store_true", help="Emit JSON output")

    # profiles
    p_prof = sub.add_parser(
        "profiles",
        help="Scan patches/profiles/*.json, validate ELF IDs & VLT recipes, and show comparison table",
    )
    p_prof.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_prof.add_argument("--profiles-dir", type=Path, default=None, help="Override profiles directory")
    p_prof.add_argument("--patches-spec", type=Path, default=None, help="Override ELF patches JSON spec")
    p_prof.add_argument("--json", action="store_true", help="Emit JSON output")

    # build
    p_build = sub.add_parser(
        "build",
        help="Build a modded ISO from a preset profile and optionally install PCSX2 v2.6.3 per-game settings",
    )
    p_build.add_argument(
        "--profile",
        default="rockport2026_v0.2",
        help="Profile ID, filename, or path (default: rockport2026_v0.2)",
    )
    p_build.add_argument(
        "--source",
        "--source-iso",
        dest="source",
        type=Path,
        default=Path(os.environ.get("ROCKPORT_SOURCE_ISO", str(DEFAULT_SOURCE_ISO))),
        help="Path to clean untouched SLUS-21351 ISO (opened strictly read-only)",
    )
    p_build.add_argument("--output", type=Path, default=None, help="Optional custom output modded ISO path")
    p_build.add_argument(
        "--no-verify-sha256",
        action="store_true",
        help="Skip source ISO SHA-256 verification before/after build (verified by default)",
    )
    p_build.add_argument(
        "--install-pcsx2",
        action="store_true",
        help="Install per-game PCSX2 .ini + companion .pnach for the newly built modded CRC",
    )
    p_build.add_argument("--pcsx2-dir", type=Path, default=DEFAULT_PCSX2_DIR)
    p_build.add_argument(
        "--renderer",
        choices=("metal", "vulkan", "auto"),
        default="metal",
        help="PCSX2 v2.6.3 GPU backend (default: metal)",
    )
    p_build.add_argument("--upscale", type=int, default=6, help="Internal resolution multiplier (default: 6 = 4K)")
    p_build.add_argument(
        "--anisotropy",
        type=int,
        choices=setup_pcsx2_macos.VALID_ANISOTROPY,
        default=16,
        help="Anisotropic filtering (default: 16)",
    )
    p_build.add_argument(
        "--ee-cycle-rate",
        type=int,
        default=2,
        help="EE Cycle Rate index (default: 2 = 180%% for native 60 FPS)",
    )
    p_build.add_argument(
        "--aspect",
        default="auto",
        choices=("auto", "16:9", "21:9", "Stretch", "4:3", "Auto 4:3/3:2", "10:7"),
        help="PCSX2 AspectRatio override ('auto' maps 16:9->16:9 and 21:9->Stretch cleanly)",
    )
    p_build.add_argument("--enable-dump", action="store_true", help="Enable PCSX2 texture dumping on installed profile")
    p_build.add_argument("--no-precache", action="store_true", help="Disable full-RAM texture precaching")
    p_build.add_argument("--dry-run", action="store_true", help="Validate profile and print planned build/setup actions")
    p_build.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_build.add_argument("--json", action="store_true", help="Emit JSON output")

    # dump-mode
    p_dump = sub.add_parser(
        "dump-mode",
        help="Toggle DumpReplaceableTextures inside per-game gamesettings/SLUS-21351_<CRC>.ini ONLY",
    )
    p_dump.add_argument("state", choices=("on", "off"), help="Enable ('on') or disable ('off') texture dumping")
    p_dump.add_argument(
        "--crc",
        action="append",
        dest="crcs",
        default=None,
        help="Target specific 8-hex ELF CRC (repeatable; default updates all existing SLUS-21351_*.ini)",
    )
    p_dump.add_argument("--pcsx2-dir", type=Path, default=DEFAULT_PCSX2_DIR)
    p_dump.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create a verified per-game INI if the target CRC INI does not exist yet",
    )

    # verify
    p_ver = sub.add_parser("verify", help="Run py_compile, audit_repo_clean, lint_pnach, profiles, and unittest")
    p_ver.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_ver.add_argument("--skip-tests", action="store_true", help="Skip unittest step")
    p_ver.add_argument(
        "--include-untracked",
        action="store_true",
        help="Also audit untracked non-ignored files in audit_repo_clean",
    )
    p_ver.add_argument("--json", action="store_true", help="Emit JSON output")

    # metal-showcase (Native Apple Silicon Metal 2026 PBR renderer)
    p_metal = sub.add_parser(
        "metal-showcase",
        help="Export extracted SLUS-21351 BMW M3 GTR scene & launch/render native Metal 2026 PBR showcase",
    )
    p_metal.add_argument(
        "--iso",
        type=Path,
        default=Path(os.environ.get("ROCKPORT_SOURCE_ISO", str(DEFAULT_SOURCE_ISO))),
        help="Path to untouched SLUS-21351 ISO (strictly read-only)",
    )
    p_metal.add_argument("--synthetic", action="store_true", help="Use synthetic GT coupe geometry (no ISO needed)")
    p_metal.add_argument("--headless", action="store_true", help="Render offscreen PNG instead of interactive window")
    p_metal.add_argument("--preset", choices=("golden_hour", "daylight", "night_rain"), default="golden_hour")
    p_metal.add_argument("--out", type=Path, default=REPO_ROOT / "build" / "metal_wave3" / "showcase.png")
    p_metal.add_argument("--width", type=int, default=1920)
    p_metal.add_argument("--height", type=int, default=1080)

    # metal-rt (Native M5 Pro Metal 4 Hardware Ray Tracing renderer)
    p_rt = sub.add_parser(
        "metal-rt",
        help="Build & run native Apple Silicon Metal 4 Hardware Ray Tracing showcase (RTR, RTAO, RTGI, soft shadows)",
    )
    p_rt.add_argument(
        "--iso",
        type=Path,
        default=Path(os.environ.get("ROCKPORT_SOURCE_ISO", str(DEFAULT_SOURCE_ISO))),
        help="Path to untouched SLUS-21351 ISO if scene needs exporting",
    )
    p_rt.add_argument("--synthetic", action="store_true", help="Use synthetic GT coupe geometry (no ISO needed)")
    p_rt.add_argument("--headless", action="store_true", help="Render offscreen PNG instead of interactive window")
    p_rt.add_argument("--preset", choices=("golden_hour", "night_neon", "daylight"), default="golden_hour")
    p_rt.add_argument("--spp", type=int, default=32, help="Samples per pixel for headless RT accumulation (default: 32)")
    p_rt.add_argument("--out", type=Path, default=REPO_ROOT / "build" / "metal_rt_wave3" / "rt_showcase.png")
    p_rt.add_argument("--compare-out", type=Path, default=None, help="Optional split RT-Off vs RT-On PNG path")
    p_rt.add_argument("--width", type=int, default=1920)
    p_rt.add_argument("--height", type=int, default=1080)

    # build-dds (Native arm64 Rust BC7 DDS compiler & 4K pack)
    p_dds = sub.add_parser(
        "build-dds",
        help="Compile native Rust rockport_dds encoder & build 2K/4K mipmapped BC7 DDS texture pack",
    )
    p_dds.add_argument("--size", type=int, choices=(512, 1024, 2048, 4096), default=2048)
    p_dds.add_argument("--out-dir", type=Path, default=REPO_ROOT / "build" / "dds_wave3" / "pcsx2_dds_pack")
    p_dds.add_argument("--json", action="store_true", help="Emit JSON summary")

    # decomp-bridge (Export C++20/Metal bridge headers from VLT + lighting recipes)
    p_br = sub.add_parser(
        "decomp-bridge",
        help="Export C++20 Rockport2026Config.hpp, Metal Rockport2026Lighting.h & bridge_manifest.json",
    )
    p_br.add_argument("--out-dir", type=Path, default=REPO_ROOT / "build" / "decomp_bridge_wave3")
    p_br.add_argument("--no-symbols", action="store_true", help="Use built-in SLUS-21351 fallback symbol table")
    p_br.add_argument("--json", action="store_true", help="Emit JSON summary")

    # build-caves (Verify & emit MIPS R5900 code caves)
    p_cv = sub.add_parser(
        "build-caves",
        help="Verify MIPS R5900 code caves in patches/elf_caves.json and optionally emit companion .pnach",
    )
    p_cv.add_argument("--verify-only", action="store_true", help="Verify caves without writing .pnach")
    p_cv.add_argument(
        "--out-pnach",
        type=Path,
        default=REPO_ROOT / "build" / "caves_wave3" / "SLUS-21351_0518D274_caves.pnach",
    )

    # live-verify (Run live PCSX2 v2.6.3 boot & screenshot check)
    p_lv = sub.add_parser(
        "live-verify",
        help="Boot a built ISO live in PCSX2 v2.6.3 Metal, capture frame/log diagnostics, and cleanly shut down",
    )
    p_lv.add_argument(
        "--iso",
        type=Path,
        default=REPO_ROOT / "build" / "NFSMW_BlackEdition_Rockport2026_v0.4.iso",
    )
    p_lv.add_argument("--out-dir", type=Path, default=REPO_ROOT / "build" / "pcsx2_verify_wave3")
    p_lv.add_argument("--boot-wait", type=float, default=28.0)
    p_lv.add_argument("--skip-screenshot", action="store_true")
    p_lv.add_argument("--dry-run", action="store_true")
    p_lv.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        data = collect_status(
            repo_root=REPO_ROOT,
            source_iso=args.source_iso,
            build_dir=args.build_dir,
            pcsx2_dir=args.pcsx2_dir,
            verify_sha256=args.verify_sha256,
        )
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print_status_human(data)
        return 0

    if args.command == "profiles":
        report = collect_profiles(
            repo_root=args.repo_root,
            profiles_dir=args.profiles_dir,
            patches_spec=args.patches_spec,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_profiles_human(report)
        return 0 if report["status"] == "pass" else 1

    if args.command == "build":
        try:
            rc, result = run_build(
                repo_root=args.repo_root,
                profile_arg=args.profile,
                source_iso=args.source,
                output_iso=args.output,
                verify_sha256=not args.no_verify_sha256,
                install_pcsx2=args.install_pcsx2,
                pcsx2_dir=args.pcsx2_dir,
                renderer=args.renderer,
                upscale=args.upscale,
                anisotropy=args.anisotropy,
                ee_cycle_rate=args.ee_cycle_rate,
                aspect=args.aspect,
                enable_dump=args.enable_dump,
                no_precache=args.no_precache,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            if args.json:
                print(json.dumps({"status": "error", "errors": [str(exc)]}, indent=2))
            else:
                print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if args.json:
            print(json.dumps(result, indent=2))
        elif rc != 0:
            for err in result.get("errors", []):
                print(f"ERROR: {err}", file=sys.stderr)
            if result.get("stdout"):
                print(result["stdout"], file=sys.stderr)
            if result.get("stderr"):
                print(result["stderr"], file=sys.stderr)
        else:
            print_build_human(result)
        return rc

    if args.command == "dump-mode":
        enabled = args.state == "on"
        try:
            updated = set_dump_mode(
                pcsx2_dir=args.pcsx2_dir,
                enabled=enabled,
                crcs=args.crcs,
                create_if_missing=args.create_if_missing,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        val = "true" if enabled else "false"
        for p in updated:
            print(f"  [dump-mode] Set DumpReplaceableTextures = {val} in {p}")
        print("Done! Global PCSX2.ini was NOT modified.")
        return 0

    if args.command == "verify":
        all_ok, results = run_verify(
            repo_root=args.repo_root,
            skip_tests=args.skip_tests,
            include_untracked=args.include_untracked,
        )
        if args.json:
            print(json.dumps({"status": "pass" if all_ok else "fail", "checks": results}, indent=2))
        else:
            for r in results:
                badge = "PASS" if r["ok"] else "FAIL"
                print(f"[{badge}] {r['check']:18s}: {r['summary']}")
                for detail in r["details"]:
                    print(f"         {detail}", file=sys.stderr if not r["ok"] else sys.stdout)
            print("\nALL CHECKS PASSED." if all_ok else "\nVERIFICATION FAILED.")
        return 0 if all_ok else 1

    if args.command == "metal-showcase":
        out_dir = REPO_ROOT / "build" / "metal_wave3"
        scene_dir = out_dir / ("scene_synth" if args.synthetic else "scene")
        exp_cmd = [sys.executable, str(TOOLS_DIR / "export_pbr_scene.py"), "--out", str(scene_dir)]
        if args.synthetic:
            exp_cmd.append("--synthetic")
        elif args.iso.is_file():
            exp_cmd.extend(["--iso", str(args.iso)])
        else:
            exp_cmd.append("--synthetic")
        rc = subprocess.run(exp_cmd, cwd=str(REPO_ROOT)).returncode
        if rc != 0:
            return rc
        mk = subprocess.run(["make", "-C", str(REPO_ROOT / "native_metal"), f"OUT_DIR={out_dir}"], cwd=str(REPO_ROOT))
        if mk.returncode != 0:
            return mk.returncode
        run_cmd_list = [str(out_dir / "RockportMetalPBR"), "--scene", str(scene_dir / "scene.json"), "--preset", args.preset]
        if args.headless:
            run_cmd_list.extend(["--headless", "--width", str(args.width), "--height", str(args.height), "--out", str(args.out)])
        return subprocess.run(run_cmd_list, cwd=str(REPO_ROOT)).returncode

    if args.command == "metal-rt":
        out_dir = REPO_ROOT / "build" / "metal_rt_wave3"
        scene_dir = REPO_ROOT / "build" / "metal_wave3" / ("scene_synth" if args.synthetic else "scene")
        scene_json = scene_dir / "scene.json"
        if not scene_json.is_file():
            exp_cmd = [sys.executable, str(TOOLS_DIR / "export_pbr_scene.py"), "--out", str(scene_dir)]
            if args.synthetic or not args.iso.is_file():
                exp_cmd.append("--synthetic")
            else:
                exp_cmd.extend(["--iso", str(args.iso)])
            rc = subprocess.run(exp_cmd, cwd=str(REPO_ROOT)).returncode
            if rc != 0:
                return rc
        mk = subprocess.run(["make", "-C", str(REPO_ROOT / "native_metal_rt"), f"OUT_DIR={out_dir}"], cwd=str(REPO_ROOT))
        if mk.returncode != 0:
            return mk.returncode
        run_cmd_list = [str(out_dir / "RockportMetalRT"), "--preset", args.preset]
        if scene_json.is_file() and not args.synthetic:
            run_cmd_list.extend(["--scene", str(scene_json)])
        if args.synthetic:
            run_cmd_list.append("--synthetic")
        if args.headless:
            run_cmd_list.extend([
                "--headless", "--spp", str(args.spp),
                "--width", str(args.width), "--height", str(args.height),
                "--out", str(args.out),
            ])
            if args.compare_out is not None:
                run_cmd_list.extend(["--compare-out", str(args.compare_out)])
        return subprocess.run(run_cmd_list, cwd=str(REPO_ROOT)).returncode

    if args.command == "build-dds":
        rc1 = subprocess.run([sys.executable, str(TOOLS_DIR / "install_macos_texture_tools.py"), "--build-encoder", "--check"], cwd=str(REPO_ROOT)).returncode
        if rc1 != 0:
            return rc1
        cmd = [sys.executable, str(TOOLS_DIR / "build_4k_texture_pack.py"), "--size", str(args.size), "--out-dir", str(args.out_dir)]
        if args.json:
            cmd.append("--json")
        return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode

    if args.command == "decomp-bridge":
        cmd = [sys.executable, str(TOOLS_DIR / "decomp_bridge.py"), "--out-dir", str(args.out_dir)]
        if args.no_symbols:
            cmd.append("--no-symbols")
        if args.json:
            cmd.append("--json")
        return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode

    if args.command == "build-caves":
        rc1 = subprocess.run([sys.executable, str(TOOLS_DIR / "mips_cave_builder.py"), "verify", str(REPO_ROOT / "patches" / "elf_caves.json")], cwd=str(REPO_ROOT)).returncode
        if rc1 != 0 or args.verify_only:
            return rc1
        args.out_pnach.parent.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [sys.executable, str(TOOLS_DIR / "mips_cave_builder.py"), "emit-pnach", str(REPO_ROOT / "patches" / "elf_caves.json"), "--out", str(args.out_pnach)],
            cwd=str(REPO_ROOT),
        ).returncode

    if args.command == "live-verify":
        cmd = [
            sys.executable, str(TOOLS_DIR / "pcsx2_live_verify.py"),
            "--iso", str(args.iso),
            "--out-dir", str(args.out_dir),
            "--boot-wait", str(args.boot_wait),
        ]
        if args.skip_screenshot:
            cmd.append("--skip-screenshot")
        if args.dry_run:
            cmd.append("--dry-run")
        if args.json:
            cmd.append("--json")
        return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
