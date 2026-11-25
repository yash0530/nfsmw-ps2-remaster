#!/usr/bin/env python3
"""
rockport_cli.py — Unified zero-dependency CLI launcher for NFSMW: Rockport 2026 (SLUS-21351).

Subcommands:
  status     Inspect untouched source ISO (read-only), built modded ISO & CRC,
             installed PCSX2 per-game .ini/.pnach profiles, texture dump/replacement
             counts, and optional AI/DDS tool readiness.
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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    for binary in ("dds_tool", "kram", "cuttlefish", "texconv", "compressonatorcli", "nvcompress", "magick", "xdelta3"):
        found = shutil.which(binary)
        cli_bins[binary] = found is not None

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
        "dds_encoder_ready": any(cli_bins[k] for k in ("dds_tool", "kram", "cuttlefish", "texconv", "compressonatorcli", "nvcompress")),
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
        f"  Readiness     : BC7 DDS Encoder={'READY' if tools['dds_encoder_ready'] else 'not installed'}, "
        f"Local AI Upscaler={'READY' if tools['ai_upscaler_ready'] else 'not installed'}, "
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
    p_ver = sub.add_parser("verify", help="Run py_compile, audit_repo_clean, lint_pnach, and unittest")
    p_ver.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_ver.add_argument("--skip-tests", action="store_true", help="Skip unittest step")
    p_ver.add_argument(
        "--include-untracked",
        action="store_true",
        help="Also audit untracked non-ignored files in audit_repo_clean",
    )
    p_ver.add_argument("--json", action="store_true", help="Emit JSON output")

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

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
