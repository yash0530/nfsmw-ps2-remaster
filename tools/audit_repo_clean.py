#!/usr/bin/env python3
"""
audit_repo_clean.py — Legal & repository hygiene guardrail for NFSMW: Rockport 2026.

Scans tracked Git files (or a target directory via --dir) and exits non-zero if any
violation is detected:
  1. Prohibited copyrighted/binary/model extensions tracked in Git
     (*.iso, *.bin, *.bun, *.elf, *.tpk, *.abk, *.gin, *.dds, *.pth, *.xdelta,
      *.chd, *.tm2, *.img, *.irx, etc.)
  2. Any single file exceeding the size limit (default: 2 MB)
  3. Any hardcoded local personal path (/Users/<name>, /home/<name>, C:\\Users\\<name>)
  4. Any invalid JSON file inside patches/ or assets_cc0/
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB

PROHIBITED_EXTENSIONS = frozenset(
    {
        ".iso",
        ".bin",
        ".bun",
        ".elf",
        ".tpk",
        ".abk",
        ".gin",
        ".dds",
        ".pth",
        ".xdelta",
        ".vcdiff",
        ".chd",
        ".tm2",
        ".img",
        ".irx",
        ".vp6",
        ".mpf",
        ".mus",
        ".lzc",
        ".viv",
        ".big",
        ".safetensors",
        ".onnx",
        ".ckpt",
        ".ps2",
        ".p2s",
        ".rkpt",
        ".wav",
        ".pcm",
        ".m4a",
        ".caf",
        ".aac",
        ".mp3",
        ".obj",
    }
)

JSON_VALIDATED_DIRS = ("patches", "assets_cc0")

# Matches real local personal home paths while ignoring placeholder documentation
# tokens such as /Users/<name>, /home/<user>, C:\Users\<name>, or $USER.
PERSONAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.:-])"
    r"(?:/Users/|/home/|[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/))"
    r"([A-Za-z][A-Za-z0-9._-]{0,63})"
    r"(?=[\\/\"'\s:,;)\]}]|$)"
)


@dataclasses.dataclass(frozen=True)
class AuditIssue:
    category: str
    path: str
    message: str
    line: Optional[int] = None

    def format_human(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"[{self.category}] {loc}: {self.message}"

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "category": self.category,
            "path": self.path,
            "message": self.message,
        }
        if self.line is not None:
            out["line"] = self.line
        return out


def list_git_tracked_files(repo_root: Path, include_untracked: bool = False) -> List[Path]:
    """Return relative Paths of files tracked by Git (and optionally untracked non-ignored)."""
    cmd = ["git", "-C", str(repo_root), "ls-files", "-z"]
    out = subprocess.check_output(cmd)
    rels = [Path(p.decode("utf-8", errors="surrogateescape")) for p in out.split(b"\x00") if p]

    if include_untracked:
        extra_cmd = [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ]
        extra = subprocess.check_output(extra_cmd)
        seen = set(rels)
        for raw in extra.split(b"\x00"):
            if not raw:
                continue
            rp = Path(raw.decode("utf-8", errors="surrogateescape"))
            if rp not in seen:
                seen.add(rp)
                rels.append(rp)

    rels.sort()
    return rels


def list_directory_files(target_dir: Path) -> List[Path]:
    """Recursively list relative Paths inside target_dir, skipping .git metadata."""
    rels: List[Path] = []
    for path in sorted(target_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(target_dir)
        if ".git" in rel.parts:
            continue
        rels.append(rel)
    return rels


def _is_in_json_validated_dir(rel: Path) -> bool:
    return bool(rel.parts) and rel.parts[0] in JSON_VALIDATED_DIRS and rel.suffix.lower() == ".json"


def audit_files(
    root: Path,
    rel_paths: Iterable[Path],
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> List[AuditIssue]:
    """Run all legal, size, personal-path, and JSON hygiene checks on `rel_paths` under `root`."""
    issues: List[AuditIssue] = []

    for rel in rel_paths:
        full = root / rel
        rel_posix = rel.as_posix()

        if not full.exists() or full.is_symlink():
            continue

        # 1. Prohibited binary / copyrighted game asset extensions
        ext = rel.suffix.lower()
        if ext in PROHIBITED_EXTENSIONS:
            issues.append(
                AuditIssue(
                    category="prohibited-extension",
                    path=rel_posix,
                    message=f"Prohibited binary/game extension '{ext}' must never be committed to Git",
                )
            )

        # 2. Max single file size (default 2 MiB)
        size = full.stat().st_size
        if size > max_file_bytes:
            issues.append(
                AuditIssue(
                    category="file-too-large",
                    path=rel_posix,
                    message=f"File size {size:,} bytes exceeds limit of {max_file_bytes:,} bytes",
                )
            )

        # Avoid loading huge files into RAM for text scanning
        if size > max_file_bytes * 4:
            if _is_in_json_validated_dir(rel):
                issues.append(
                    AuditIssue(
                        category="invalid-json",
                        path=rel_posix,
                        message="JSON file too large to validate safely",
                    )
                )
            continue

        raw = full.read_bytes()
        is_binary = b"\x00" in raw

        # 3. Hardcoded local personal paths in text files
        if not is_binary:
            text = raw.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for match in PERSONAL_PATH_RE.finditer(line):
                    issues.append(
                        AuditIssue(
                            category="personal-path",
                            path=rel_posix,
                            line=lineno,
                            message=f"Hardcoded local personal path detected: {match.group(0)!r}",
                        )
                    )

            # 4. Strict JSON validation in patches/ and assets_cc0/
            if _is_in_json_validated_dir(rel):
                try:
                    json.loads(text)
                except json.JSONDecodeError as exc:
                    issues.append(
                        AuditIssue(
                            category="invalid-json",
                            path=rel_posix,
                            line=exc.lineno,
                            message=f"JSON parse error at col {exc.colno}: {exc.msg}",
                        )
                    )
        elif _is_in_json_validated_dir(rel):
            issues.append(
                AuditIssue(
                    category="invalid-json",
                    path=rel_posix,
                    message="JSON file contains NUL bytes (binary/corrupt)",
                )
            )

    return issues


def audit_repository(
    repo_root: Path,
    *,
    target_dir: Optional[Path] = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    include_untracked: bool = False,
) -> Tuple[List[Path], List[AuditIssue]]:
    """Audit either a Git repository's tracked files or an explicit target directory."""
    if target_dir is not None:
        base = target_dir.resolve()
        rel_paths = list_directory_files(base)
    else:
        base = repo_root.resolve()
        if (base / ".git").exists():
            rel_paths = list_git_tracked_files(base, include_untracked=include_untracked)
        else:
            rel_paths = list_directory_files(base)

    issues = audit_files(base, rel_paths, max_file_bytes=max_file_bytes)
    return rel_paths, issues


def main(argv: Optional[Sequence[str]] = None) -> int:
    repo_default = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Audit tracked Git files (or a directory) for legal/hygiene violations."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_default,
        help="Git repository root to scan via `git ls-files` (default: repo root)",
    )
    parser.add_argument(
        "--dir",
        dest="target_dir",
        type=Path,
        default=None,
        help="Scan all files inside this directory instead of `git ls-files`",
    )
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="Also scan untracked non-ignored files in Git repo mode",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help=f"Maximum allowed file size in bytes (default: {DEFAULT_MAX_FILE_BYTES})",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args(argv)

    rel_paths, issues = audit_repository(
        args.repo_root,
        target_dir=args.target_dir,
        max_file_bytes=args.max_bytes,
        include_untracked=args.include_untracked,
    )

    if args.json:
        payload = {
            "status": "pass" if not issues else "fail",
            "files_scanned": len(rel_paths),
            "issue_count": len(issues),
            "issues": [i.to_dict() for i in issues],
        }
        print(json.dumps(payload, indent=2))
    else:
        if not issues:
            print(f"PASS: Repository hygiene audit clean ({len(rel_paths)} files scanned, 0 issues).")
        else:
            print(
                f"FAIL: Repository hygiene audit found {len(issues)} issue(s) "
                f"across {len(rel_paths)} file(s):",
                file=sys.stderr,
            )
            for issue in issues:
                print(f"  - {issue.format_human()}", file=sys.stderr)

    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
