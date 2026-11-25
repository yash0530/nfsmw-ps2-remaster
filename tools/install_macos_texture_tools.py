#!/usr/bin/env python3
"""
install_macos_texture_tools.py — Apple Silicon (arm64) & Linux CI native texture
toolchain inspector and `rockport_dds` builder for NFSMW: Rockport 2026.

Checks available native binaries (rockport_dds, cargo, rustc, dds_tool, kram,
cuttlefish, magick, ffmpeg, xdelta3, xcrun) and Python imaging/GPU packages
(PIL/Pillow, numpy, torch + MPS), and optionally compiles `tools/dds_encoder_rs`
into gitignored `build/dds_wave3/target/`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CARGO_MANIFEST = REPO_ROOT / "tools" / "dds_encoder_rs" / "Cargo.toml"
DEFAULT_TARGET_DIR = REPO_ROOT / "build" / "dds_wave3" / "target"


def find_rockport_dds(target_dir: Path = DEFAULT_TARGET_DIR) -> Optional[Path]:
    """Locate compiled `rockport_dds` binary in build/dds_wave3/target or PATH."""
    candidates = [
        target_dir / "release" / "rockport_dds",
        target_dir / "debug" / "rockport_dds",
    ]
    if os.name == "nt":
        candidates = [p.with_suffix(".exe") for p in candidates]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    on_path = shutil.which("rockport_dds")
    return Path(on_path) if on_path else None


def _probe_binary(name: str, explicit_path: Optional[Path] = None) -> Dict[str, Any]:
    path = str(explicit_path) if explicit_path else shutil.which(name)
    info: Dict[str, Any] = {
        "name": name,
        "available": path is not None,
        "path": path,
        "version": None,
    }
    if path is None:
        return info

    version_args = {
        "rockport_dds": ["--version"],
        "cargo": ["--version"],
        "rustc": ["--version"],
        "magick": ["--version"],
        "ffmpeg": ["-version"],
        "xdelta3": ["-V"],
        "xcrun": ["--version"],
    }.get(name)

    if version_args:
        try:
            proc = subprocess.run(
                [path, *version_args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            out = (proc.stdout or proc.stderr or "").strip().splitlines()
            if out:
                info["version"] = out[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return info


def _probe_python_packages() -> Dict[str, Any]:
    pkgs: Dict[str, Any] = {}
    for mod_name in ("PIL", "numpy", "torch"):
        entry: Dict[str, Any] = {"available": False, "version": None}
        try:
            mod = importlib.import_module(mod_name)
            entry["available"] = True
            entry["version"] = getattr(mod, "__version__", None)
            if mod_name == "torch":
                mps_mod = getattr(getattr(mod, "backends", None), "mps", None)
                entry["mps_available"] = bool(mps_mod and mps_mod.is_available())
        except Exception:
            pass
        pkgs[mod_name] = entry
    return pkgs


def inspect_toolchain(target_dir: Path = DEFAULT_TARGET_DIR) -> Dict[str, Any]:
    """Inspect all native CLI tools and Python imaging packages."""
    rockport_bin = find_rockport_dds(target_dir=target_dir)
    binaries: Dict[str, Any] = {
        "rockport_dds": _probe_binary("rockport_dds", explicit_path=rockport_bin),
    }
    for tool in ("cargo", "rustc", "dds_tool", "kram", "cuttlefish", "magick", "ffmpeg", "xdelta3", "xcrun"):
        binaries[tool] = _probe_binary(tool)

    py_pkgs = _probe_python_packages()

    # Check that no accidental Cargo target/ exists inside tools/dds_encoder_rs/
    rogue_target = REPO_ROOT / "tools" / "dds_encoder_rs" / "target"

    return {
        "schema_version": "1.0",
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "target_dir": str(target_dir.relative_to(REPO_ROOT))
        if target_dir.is_relative_to(REPO_ROOT)
        else str(target_dir),
        "rogue_crate_target_present": rogue_target.exists(),
        "binaries": binaries,
        "python_packages": py_pkgs,
        "bc7_encoder_ready": bool(
            binaries["rockport_dds"]["available"]
            or binaries["dds_tool"]["available"]
            or binaries["kram"]["available"]
            or binaries["cuttlefish"]["available"]
        ),
    }


def build_rockport_dds(
    manifest_path: Path = DEFAULT_CARGO_MANIFEST,
    target_dir: Path = DEFAULT_TARGET_DIR,
    release: bool = True,
) -> Path:
    """Compile `tools/dds_encoder_rs` strictly into `build/dds_wave3/target/`."""
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError(
            "cargo not found on PATH. Install Rust via `brew install rust` or https://rustup.rs"
        )
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Cargo manifest not found: {manifest_path}")

    target_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(target_dir)

    cmd: List[str] = [cargo, "build", "--manifest-path", str(manifest_path)]
    if release:
        cmd.append("--release")

    proc = subprocess.run(cmd, env=env, cwd=str(REPO_ROOT), check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"cargo build failed with exit code {proc.returncode}")

    profile = "release" if release else "debug"
    built = target_dir / profile / ("rockport_dds.exe" if os.name == "nt" else "rockport_dds")
    if not built.is_file():
        raise FileNotFoundError(f"Expected compiled binary not found at {built}")
    return built


def _print_human_report(report: Dict[str, Any]) -> None:
    plat = report["platform"]
    print(f"NFSMW: Rockport 2026 — Native Texture Toolchain ({plat['system']} / {plat['machine']})")
    print(f"Python: {plat['python']} | Target dir: {report['target_dir']}")
    print("\nNative Binaries:")
    for name, info in report["binaries"].items():
        status = "OK" if info["available"] else "missing"
        ver = f" ({info['version']})" if info.get("version") else ""
        loc = f" -> {info['path']}" if info.get("path") else ""
        print(f"  - {name:14s}: {status:7s}{ver}{loc}")
    print("\nPython Packages:")
    for name, info in report["python_packages"].items():
        status = "OK" if info["available"] else "missing"
        ver = f" {info['version']}" if info.get("version") else ""
        extra = ""
        if name == "torch" and info["available"]:
            extra = f" (mps={info.get('mps_available', False)})"
        print(f"  - {name:14s}: {status:7s}{ver}{extra}")
    if report["rogue_crate_target_present"]:
        print("\n[WARNING] Unexpected tools/dds_encoder_rs/target/ directory found!")
    if not report["binaries"]["rockport_dds"]["available"]:
        print("\nTip: run `python3 tools/install_macos_texture_tools.py --build-encoder` to build rockport_dds.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect macOS/Linux native texture tools and build pure-Rust rockport_dds encoder."
    )
    parser.add_argument(
        "--build-encoder",
        action="store_true",
        help="Compile tools/dds_encoder_rs in release mode into build/dds_wave3/target.",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=DEFAULT_TARGET_DIR,
        help="Cargo target directory (default: build/dds_wave3/target).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if rockport_dds (or fallback BC7 encoder) is unavailable.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    args = parser.parse_args(argv)

    built_path: Optional[Path] = None
    if args.build_encoder:
        built_path = build_rockport_dds(target_dir=args.target_dir.resolve())

    report = inspect_toolchain(target_dir=args.target_dir.resolve())
    if built_path is not None:
        report["built_encoder"] = str(built_path)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human_report(report)
        if built_path is not None:
            print(f"\nBuilt rockport_dds: {built_path}")

    if args.check and not report["bc7_encoder_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
