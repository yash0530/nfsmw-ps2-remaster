#!/usr/bin/env python3
"""
make_release_patch.py — Clean `xdelta3` binary-diff release patcher with
cryptographic SHA-256 verification for NFSMW: Rockport 2026.

Generates legally distributable, compact `.xdelta` binary patches between a
user's untouched original `SLUS-21351` ISO and a locally built modded ISO,
alongside a JSON SHA-256 verification manifest (`<patch>.json`).

NEVER distributes copyrighted game data — only compact binary deltas and hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from iso_inspect import sha256_file

INSTALL_HINT = (
    "xdelta3 executable not found on PATH.\n"
    "Install it via:\n"
    "  macOS (Homebrew): brew install xdelta\n"
    "  Ubuntu / Debian : sudo apt-get install xdelta3\n"
    "  Windows (winget): winget install xdelta3  (or download xdelta3.exe from GitHub releases)"
)


def find_xdelta3(explicit_bin: Optional[str] = None) -> str:
    """Locate the `xdelta3` binary or raise FileNotFoundError with install instructions."""
    if explicit_bin:
        p = Path(explicit_bin)
        if p.is_file():
            return str(p)
        resolved = shutil.which(explicit_bin)
        if resolved:
            return resolved
        raise FileNotFoundError(f"Specified xdelta3 binary not found: {explicit_bin}")

    found = shutil.which("xdelta3")
    if not found:
        raise FileNotFoundError(INSTALL_HINT)
    return found


def create_patch(
    source: Path,
    modified: Path,
    output: Path,
    manifest_path: Optional[Path] = None,
    compression_level: int = 9,
    force: bool = False,
    verify: bool = False,
    xdelta3_bin: Optional[str] = None,
) -> dict:
    """Create a verified `.xdelta` patch from `source` -> `modified` and emit its SHA-256 manifest."""
    src = source.expanduser().resolve()
    mod = modified.expanduser().resolve()
    out = output.expanduser().resolve()

    if not src.is_file():
        raise FileNotFoundError(f"Source file not found: {src}")
    if not mod.is_file():
        raise FileNotFoundError(f"Modified file not found: {mod}")
    if src == mod:
        raise ValueError("Source and modified files must NOT be the same file.")
    if out in (src, mod):
        raise ValueError("Output patch path must NOT overwrite source or modified file.")
    if out.exists() and not force:
        raise FileExistsError(f"Output patch already exists (pass --force to overwrite): {out}")
    if not (0 <= compression_level <= 9):
        raise ValueError(f"Compression level must be 0..9, got {compression_level}")

    xd_bin = find_xdelta3(xdelta3_bin)

    src_size = src.stat().st_size
    mod_size = mod.stat().st_size
    src_sha = sha256_file(src)
    mod_sha = sha256_file(mod)

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [xd_bin, "-e", f"-{compression_level}"]
    if force:
        cmd.append("-f")
    cmd.extend(["-s", str(src), str(mod), str(out)])

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"xdelta3 encode failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}"
        )
    if not out.is_file():
        raise RuntimeError(f"xdelta3 exited 0 but output file was not created: {out}")

    patch_size = out.stat().st_size
    patch_sha = sha256_file(out)

    manifest = {
        "schema_version": 1,
        "format": "xdelta3-vcdiff",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "compression_level": compression_level,
        "source": {
            "filename": src.name,
            "size_bytes": src_size,
            "sha256": src_sha,
        },
        "modified": {
            "filename": mod.name,
            "size_bytes": mod_size,
            "sha256": mod_sha,
        },
        "patch": {
            "filename": out.name,
            "size_bytes": patch_size,
            "sha256": patch_sha,
        },
    }

    mpath = manifest_path.expanduser().resolve() if manifest_path else out.with_suffix(out.suffix + ".json")
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if verify:
        with tempfile.TemporaryDirectory(prefix="rockport_xdelta_verify_") as td:
            rebuilt = Path(td) / mod.name
            apply_patch(
                source=src,
                patch=out,
                output=rebuilt,
                manifest_path=mpath,
                force=True,
                skip_sha256=False,
                xdelta3_bin=xd_bin,
            )

    return manifest


def apply_patch(
    source: Path,
    patch: Path,
    output: Path,
    manifest_path: Optional[Path] = None,
    force: bool = False,
    skip_sha256: bool = False,
    xdelta3_bin: Optional[str] = None,
) -> dict:
    """Apply an `.xdelta` patch non-destructively to `source`, verifying SHA-256 hashes."""
    src = source.expanduser().resolve()
    pat = patch.expanduser().resolve()
    out = output.expanduser().resolve()

    if not src.is_file():
        raise FileNotFoundError(f"Source file not found: {src}")
    if not pat.is_file():
        raise FileNotFoundError(f"Patch file not found: {pat}")
    if out in (src, pat):
        raise ValueError("Output path must NOT overwrite source or patch file.")
    if out.exists() and not force:
        raise FileExistsError(f"Output file already exists (pass --force to overwrite): {out}")

    xd_bin = find_xdelta3(xdelta3_bin)

    # Auto-discover companion manifest `<patch>.json` if not explicitly passed
    mpath = manifest_path.expanduser().resolve() if manifest_path else pat.with_suffix(pat.suffix + ".json")
    manifest: Optional[dict] = None
    if mpath.is_file():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    elif manifest_path is not None:
        raise FileNotFoundError(f"Manifest file not found: {mpath}")

    src_sha = None
    patch_sha = None
    if manifest is not None and not skip_sha256:
        expected_src_sha = manifest["source"]["sha256"].lower()
        src_sha = sha256_file(src).lower()
        if src_sha != expected_src_sha:
            raise ValueError(
                f"Source SHA-256 mismatch!\n"
                f"  Expected: {expected_src_sha}\n"
                f"  Actual  : {src_sha}\n"
                f"Ensure you are patching the exact clean source image."
            )

        expected_patch_sha = manifest["patch"]["sha256"].lower()
        patch_sha = sha256_file(pat).lower()
        if patch_sha != expected_patch_sha:
            raise ValueError(
                f"Patch SHA-256 mismatch (corrupt patch file)!\n"
                f"  Expected: {expected_patch_sha}\n"
                f"  Actual  : {patch_sha}"
            )

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [xd_bin, "-d"]
    if force:
        cmd.append("-f")
    cmd.extend(["-s", str(src), str(pat), str(out)])

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        if out.exists():
            out.unlink()
        raise RuntimeError(
            f"xdelta3 decode failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}"
        )

    out_sha = sha256_file(out).lower()
    if manifest is not None and not skip_sha256:
        expected_out_sha = manifest["modified"]["sha256"].lower()
        if out_sha != expected_out_sha:
            out.unlink()
            raise ValueError(
                f"Output SHA-256 mismatch after applying patch (deleted corrupt output)!\n"
                f"  Expected: {expected_out_sha}\n"
                f"  Actual  : {out_sha}"
            )

    return {
        "output_filename": out.name,
        "output_size_bytes": out.stat().st_size,
        "output_sha256": out_sha,
        "source_sha256_verified": bool(manifest is not None and not skip_sha256),
        "patch_sha256_verified": bool(manifest is not None and not skip_sha256),
        "output_sha256_verified": bool(manifest is not None and not skip_sha256),
        "source_sha256": src_sha,
        "patch_sha256": patch_sha,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create, verify, and apply compact xdelta3 binary release patches with SHA-256 checks."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Generate a compact .xdelta patch + SHA-256 manifest")
    p_create.add_argument("--source", type=Path, required=True, help="Clean original ISO (strictly read-only)")
    p_create.add_argument("--modified", type=Path, required=True, help="Locally built modded ISO (read-only)")
    p_create.add_argument("--output", type=Path, required=True, help="Destination .xdelta patch file")
    p_create.add_argument("--manifest", type=Path, default=None, help="Optional JSON manifest path (default: <output>.json)")
    p_create.add_argument("-c", "--compression", type=int, default=9, help="xdelta3 compression level 0..9 (default: 9)")
    p_create.add_argument("--force", action="store_true", help="Overwrite existing output patch")
    p_create.add_argument("--verify", action="store_true", help="Round-trip verify patch against temp output")
    p_create.add_argument("--xdelta3-bin", type=str, default=None, help="Explicit path to xdelta3 executable")

    p_apply = sub.add_parser("apply", help="Apply a .xdelta patch to a clean source ISO with SHA-256 verification")
    p_apply.add_argument("--source", type=Path, required=True, help="Clean original ISO (strictly read-only)")
    p_apply.add_argument("--patch", type=Path, required=True, help="Path to .xdelta patch file")
    p_apply.add_argument("--output", type=Path, required=True, help="Destination path for reconstructed modded ISO")
    p_apply.add_argument("--manifest", type=Path, default=None, help="Optional SHA-256 manifest JSON (auto-detects <patch>.json)")
    p_apply.add_argument("--skip-sha256", action="store_true", help="Skip SHA-256 checks (not recommended)")
    p_apply.add_argument("--force", action="store_true", help="Overwrite existing output file")
    p_apply.add_argument("--xdelta3-bin", type=str, default=None, help="Explicit path to xdelta3 executable")

    p_info = sub.add_parser("info", help="Display a release patch SHA-256 manifest")
    p_info.add_argument("manifest", type=Path)

    args = parser.parse_args()

    try:
        if args.cmd == "create":
            manifest = create_patch(
                source=args.source,
                modified=args.modified,
                output=args.output,
                manifest_path=args.manifest,
                compression_level=args.compression,
                force=args.force,
                verify=args.verify,
                xdelta3_bin=args.xdelta3_bin,
            )
            print(f"Created patch : {args.output} ({manifest['patch']['size_bytes']:,} bytes)")
            print(f"Patch SHA-256 : {manifest['patch']['sha256']}")
            print(f"Source SHA-256: {manifest['source']['sha256']}")
            print(f"Modded SHA-256: {manifest['modified']['sha256']}")
            if args.verify:
                print("Round-trip verification: PASSED (reconstructed SHA-256 matched 100%)")
            return 0

        if args.cmd == "apply":
            report = apply_patch(
                source=args.source,
                patch=args.patch,
                output=args.output,
                manifest_path=args.manifest,
                force=args.force,
                skip_sha256=args.skip_sha256,
                xdelta3_bin=args.xdelta3_bin,
            )
            print(f"Patched output: {args.output} ({report['output_size_bytes']:,} bytes)")
            print(f"Output SHA-256: {report['output_sha256']}")
            print(f"Verified      : {report['output_sha256_verified']}")
            return 0

        if args.cmd == "info":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            print(f"Format        : {manifest.get('format')}")
            print(f"Created (UTC) : {manifest.get('created_at_utc')}")
            print(f"Source        : {manifest['source']['filename']} ({manifest['source']['size_bytes']:,} B) sha256={manifest['source']['sha256']}")
            print(f"Modified      : {manifest['modified']['filename']} ({manifest['modified']['size_bytes']:,} B) sha256={manifest['modified']['sha256']}")
            print(f"Patch         : {manifest['patch']['filename']} ({manifest['patch']['size_bytes']:,} B) sha256={manifest['patch']['sha256']}")
            return 0
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
