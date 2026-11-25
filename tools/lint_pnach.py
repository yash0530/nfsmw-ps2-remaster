#!/usr/bin/env python3
"""
lint_pnach.py — PCSX2 v2 `.pnach` syntax, bounds, alignment & conflict linter.

Validates all `patches/pnach/*.pnach` files (or explicitly passed files/directories):
  1. Required top-level headers: `gametitle=` and `comment=`
  2. Valid `[Section]` blocks (non-empty, unique within file, no orphan patches)
  3. Valid `patch=<mode>,<cpu>,<8-hex-addr>,<type>,<hex-val>` rules aligned with
     PCSX2 v2.6.3 (`Patch::s_place_to_string`, `s_cpu_to_string`, `s_type_to_string`)
  4. Strict 4-byte alignment for `word`/`beword` (2-byte for `short`, 8-byte for `double`)
  5. Valid EE RAM address bounds (`0x00000000..0x01FFFFFF`)
  6. No duplicate or conflicting writes to overlapping bytes within the same `[Section]`
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

EE_RAM_MIN = 0x00000000
EE_RAM_MAX = 0x01FFFFFF
IOP_RAM_MAX = 0x001FFFFF

VALID_MODES = frozenset({"0", "1", "2", "3"})
VALID_CPUS = frozenset({"EE", "IOP"})

# Width in bytes for standard typed writes (None for `extended` raw cheat codes)
TYPE_WIDTHS: Dict[str, Optional[int]] = {
    "byte": 1,
    "short": 2,
    "beshort": 2,
    "word": 4,
    "beword": 4,
    "double": 8,
    "bedouble": 8,
    "extended": None,
}

SECTION_METADATA_KEYS = frozenset({"author", "description", "comment", "gsaspectratio", "gsinterlacemode"})
TOP_LEVEL_KEYS = frozenset({"gametitle", "comment", "author", "description"})
VALID_ASPECT_ENUMS = frozenset({"stretch", "auto 4:3/3:2", "4:3", "16:9", "10:7"})
ASPECT_RATIO_RE = re.compile(r"^\d+(?:\.\d+)?:\d+(?:\.\d+)?$")
HEX8_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
HEX_ANY_RE = re.compile(r"^[0-9A-Fa-f]+$")
SECTION_RE = re.compile(r"^\[(.+)\]$")


@dataclasses.dataclass(frozen=True)
class LintError:
    path: str
    line: Optional[int]
    code: str
    message: str

    def format_human(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"[{self.code}] {loc}: {self.message}"

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "path": self.path,
            "code": self.code,
            "message": self.message,
        }
        if self.line is not None:
            out["line"] = self.line
        return out


def _strip_comment(raw_line: str) -> str:
    idx = raw_line.find("//")
    if idx != -1:
        raw_line = raw_line[:idx]
    return raw_line.strip()


def _value_to_bytes(ptype: str, val_int: int) -> List[int]:
    """Expand a typed write value into its ordered byte payload for conflict detection."""
    width = TYPE_WIDTHS[ptype]
    assert width is not None
    order = "big" if ptype.startswith("be") else "little"
    return list(val_int.to_bytes(width, byteorder=order, signed=False))


def lint_pnach_text(text: str, source_name: str = "<string>") -> List[LintError]:
    """Lint PCSX2 v2 .pnach text and return a list of LintErrors (empty means valid)."""
    errors: List[LintError] = []

    has_gametitle = False
    has_comment = False
    current_section: Optional[str] = None
    seen_sections: Dict[str, int] = {}
    section_patch_counts: Dict[str, int] = {}

    # Per-section byte write map: (cpu, byte_addr) -> (first_line, byte_val, orig_addr, orig_raw_val)
    section_writes: Dict[str, Dict[Tuple[str, int], Tuple[int, int, int, str]]] = {}
    section_has_conditional: Dict[str, bool] = {}

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line)
        if not line:
            continue

        # [Section Name] header
        if line.startswith("["):
            sec_m = SECTION_RE.match(line)
            if not sec_m or not sec_m.group(1).strip():
                errors.append(LintError(source_name, lineno, "bad-section", f"Malformed section header: {line!r}"))
                continue
            sec_name = sec_m.group(1).strip()
            if sec_name in seen_sections:
                errors.append(
                    LintError(
                        source_name,
                        lineno,
                        "duplicate-section",
                        f"Duplicate section [{sec_name}] (first defined on line {seen_sections[sec_name]})",
                    )
                )
            else:
                seen_sections[sec_name] = lineno
                section_patch_counts[sec_name] = 0
                section_writes[sec_name] = {}
                section_has_conditional[sec_name] = False
            current_section = sec_name
            continue

        if "=" not in line:
            errors.append(LintError(source_name, lineno, "syntax", f"Expected key=value directive, got: {line!r}"))
            continue

        key_raw, val_raw = line.split("=", 1)
        key = key_raw.strip().lower()
        val = val_raw.strip()

        # Top-level headers (before first [Section])
        if current_section is None:
            if key == "gametitle":
                if not val:
                    errors.append(LintError(source_name, lineno, "empty-header", "gametitle= must not be empty"))
                else:
                    has_gametitle = True
            elif key == "comment":
                if not val:
                    errors.append(LintError(source_name, lineno, "empty-header", "comment= must not be empty"))
                else:
                    has_comment = True
            elif key in TOP_LEVEL_KEYS:
                if not val:
                    errors.append(LintError(source_name, lineno, "empty-header", f"{key}= must not be empty"))
            elif key in ("patch", "dpatch"):
                errors.append(
                    LintError(
                        source_name,
                        lineno,
                        "orphan-patch",
                        f"'{key}=' rule appears outside any [Section] block (PCSX2 v2 requires named [Section] groups)",
                    )
                )
            else:
                errors.append(
                    LintError(source_name, lineno, "unknown-directive", f"Unknown top-level directive '{key}='")
                )
            continue

        # Inside a [Section]
        if key in SECTION_METADATA_KEYS:
            if not val:
                errors.append(
                    LintError(source_name, lineno, "empty-directive", f"'{key}=' inside [{current_section}] is empty")
                )
            elif key == "gsaspectratio":
                if val.lower() not in VALID_ASPECT_ENUMS and not ASPECT_RATIO_RE.match(val):
                    errors.append(
                        LintError(
                            source_name,
                            lineno,
                            "bad-aspect-ratio",
                            f"Invalid gsaspectratio value {val!r} (expected e.g. 16:9, 21:9, 4:3, Stretch)",
                        )
                    )
            continue

        if key == "dpatch":
            section_patch_counts[current_section] += 1
            continue

        if key != "patch":
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "unknown-directive",
                    f"Unknown directive '{key}=' inside [{current_section}]",
                )
            )
            continue

        # Validate `patch=<mode>,<cpu>,<addr>,<type>,<val>`
        section_patch_counts[current_section] += 1
        parts = [p.strip() for p in val.split(",")]
        if len(parts) != 5:
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "bad-patch-format",
                    f"Expected 5 comma-separated fields (mode,CPU,addr,type,val), got {len(parts)}: {val!r}",
                )
            )
            continue

        mode, cpu, addr_s, ptype_raw, val_s = parts
        cpu_u = cpu.upper()
        ptype = ptype_raw.lower()

        if mode not in VALID_MODES:
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "bad-mode",
                    f"Invalid patch mode {mode!r} (expected one of {sorted(VALID_MODES)})",
                )
            )

        if cpu_u not in VALID_CPUS:
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "bad-cpu",
                    f"Invalid CPU target {cpu!r} (expected 'EE' or 'IOP')",
                )
            )

        if not HEX8_RE.match(addr_s):
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "bad-address",
                    f"Address {addr_s!r} must be exactly 8 hexadecimal digits",
                )
            )
            continue

        if ptype not in TYPE_WIDTHS:
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "bad-type",
                    f"Invalid patch type {ptype_raw!r} (expected one of {sorted(TYPE_WIDTHS)})",
                )
            )
            continue

        addr_int = int(addr_s, 16)
        ram_max = EE_RAM_MAX if cpu_u == "EE" else IOP_RAM_MAX

        if ptype == "extended":
            if not HEX8_RE.match(val_s):
                errors.append(
                    LintError(
                        source_name,
                        lineno,
                        "bad-value",
                        f"Extended patch value {val_s!r} must be exactly 8 hex digits",
                    )
                )
                continue
            cmd = (addr_int >> 28) & 0xF
            eff = addr_int & 0x0FFFFFFF
            if cmd in (0xD, 0xE):
                section_has_conditional[current_section] = True
                if cmd == 0xD and eff > ram_max:
                    errors.append(
                        LintError(
                            source_name,
                            lineno,
                            "addr-out-of-bounds",
                            f"Conditional address 0x{eff:08X} exceeds {cpu_u} RAM limit 0x{ram_max:08X}",
                        )
                    )
                continue
            if cmd in (0x0, 0x1, 0x2):
                ext_width = 1 << cmd
                if eff < EE_RAM_MIN or eff + ext_width - 1 > ram_max:
                    errors.append(
                        LintError(
                            source_name,
                            lineno,
                            "addr-out-of-bounds",
                            f"Extended address 0x{eff:08X} outside {cpu_u} RAM 0x00000000..0x{ram_max:08X}",
                        )
                    )
                    continue
                if eff % ext_width != 0:
                    errors.append(
                        LintError(
                            source_name,
                            lineno,
                            "unaligned-address",
                            f"Extended {ext_width}-byte write address 0x{eff:08X} is not {ext_width}-byte aligned",
                        )
                    )
                    continue
                if not section_has_conditional[current_section]:
                    val_int = int(val_s, 16) & ((1 << (ext_width * 8)) - 1)
                    payload = list(val_int.to_bytes(ext_width, "little"))
                    _record_and_check_writes(
                        errors,
                        source_name,
                        lineno,
                        current_section,
                        section_writes[current_section],
                        cpu_u,
                        eff,
                        payload,
                        val_s,
                    )
            continue

        # Standard typed patch (byte, short, word, double, beshort, beword, bedouble)
        width = TYPE_WIDTHS[ptype]
        assert width is not None

        # Validate hex value length & range
        if not HEX_ANY_RE.match(val_s):
            errors.append(LintError(source_name, lineno, "bad-value", f"Non-hex patch value {val_s!r}"))
            continue

        if ptype in ("word", "beword"):
            if not HEX8_RE.match(val_s):
                errors.append(
                    LintError(
                        source_name,
                        lineno,
                        "bad-value",
                        f"'{ptype}' value {val_s!r} must be exactly 8 hex digits",
                    )
                )
                continue
        elif ptype in ("double", "bedouble"):
            if len(val_s) != 16:
                errors.append(
                    LintError(
                        source_name,
                        lineno,
                        "bad-value",
                        f"'{ptype}' value {val_s!r} must be exactly 16 hex digits",
                    )
                )
                continue
        elif ptype in ("short", "beshort"):
            if len(val_s) not in (4, 8) or int(val_s, 16) > 0xFFFF:
                errors.append(
                    LintError(
                        source_name,
                        lineno,
                        "bad-value",
                        f"'{ptype}' value {val_s!r} must be 4 or 8 hex digits <= 0xFFFF",
                    )
                )
                continue
        elif ptype == "byte":
            if len(val_s) not in (2, 8) or int(val_s, 16) > 0xFF:
                errors.append(
                    LintError(
                        source_name,
                        lineno,
                        "bad-value",
                        f"'byte' value {val_s!r} must be 2 or 8 hex digits <= 0xFF",
                    )
                )
                continue

        # Validate RAM bounds
        if addr_int < EE_RAM_MIN or addr_int + width - 1 > ram_max:
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "addr-out-of-bounds",
                    f"Address 0x{addr_int:08X} ({ptype}) outside {cpu_u} RAM 0x00000000..0x{ram_max:08X}",
                )
            )
            continue

        # Validate natural alignment (4-byte for word, 2-byte for short, 8-byte for double)
        if addr_int % width != 0:
            errors.append(
                LintError(
                    source_name,
                    lineno,
                    "unaligned-address",
                    f"Address 0x{addr_int:08X} for '{ptype}' must be {width}-byte aligned",
                )
            )
            continue

        if cpu_u in VALID_CPUS:
            val_int = int(val_s, 16)
            payload = _value_to_bytes(ptype, val_int)
            _record_and_check_writes(
                errors,
                source_name,
                lineno,
                current_section,
                section_writes[current_section],
                cpu_u,
                addr_int,
                payload,
                val_s,
            )

    # Required header checks
    if not has_gametitle:
        errors.append(LintError(source_name, None, "missing-header", "Missing required top-level 'gametitle=' header"))
    if not has_comment:
        errors.append(LintError(source_name, None, "missing-header", "Missing required top-level 'comment=' header"))
    if not seen_sections:
        errors.append(LintError(source_name, None, "no-sections", "No [Section] blocks found in .pnach file"))

    for sec_name, count in section_patch_counts.items():
        if count == 0:
            errors.append(
                LintError(
                    source_name,
                    seen_sections[sec_name],
                    "empty-section",
                    f"Section [{sec_name}] contains no patch= rules",
                )
            )

    return errors


def _record_and_check_writes(
    errors: List[LintError],
    source_name: str,
    lineno: int,
    section_name: str,
    write_map: Dict[Tuple[str, int], Tuple[int, int, int, str]],
    cpu: str,
    base_addr: int,
    payload: List[int],
    raw_val: str,
) -> None:
    reported = False
    for offset, byte_val in enumerate(payload):
        key = (cpu, base_addr + offset)
        if key in write_map:
            prev_line, prev_byte, prev_addr, prev_raw = write_map[key]
            if not reported:
                if prev_byte != byte_val:
                    errors.append(
                        LintError(
                            source_name,
                            lineno,
                            "conflicting-write",
                            f"Conflicting write in [{section_name}] at {cpu} 0x{base_addr:08X} "
                            f"(val {raw_val}) overlaps line {prev_line} (0x{prev_addr:08X} val {prev_raw})",
                        )
                    )
                else:
                    errors.append(
                        LintError(
                            source_name,
                            lineno,
                            "duplicate-write",
                            f"Duplicate write in [{section_name}] at {cpu} 0x{base_addr:08X} "
                            f"(already written on line {prev_line})",
                        )
                    )
                reported = True
        else:
            write_map[key] = (lineno, byte_val, base_addr, raw_val)


def lint_pnach_file(path: Path) -> List[LintError]:
    text = path.read_text(encoding="utf-8")
    return lint_pnach_text(text, source_name=str(path))


def collect_pnach_targets(paths: Sequence[Path], repo_root: Path) -> List[Path]:
    if not paths:
        default_dir = repo_root / "patches" / "pnach"
        if not default_dir.is_dir():
            return []
        return sorted(default_dir.glob("*.pnach"))

    out: List[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(p.rglob("*.pnach")))
        else:
            out.append(p)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Lint PCSX2 v2 .pnach patch files.")
    parser.add_argument(
        "targets",
        nargs="*",
        type=Path,
        help="Optional .pnach file(s) or directory(ies) to lint (default: patches/pnach/*.pnach)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    files = collect_pnach_targets(args.targets, repo_root)
    if not files:
        print("FAIL: No .pnach files found to lint.", file=sys.stderr)
        return 1

    all_errors: List[LintError] = []
    for path in files:
        if not path.is_file():
            all_errors.append(LintError(str(path), None, "not-found", "File does not exist"))
            continue
        all_errors.extend(lint_pnach_file(path))

    if args.json:
        payload = {
            "status": "pass" if not all_errors else "fail",
            "files_checked": [str(f) for f in files],
            "error_count": len(all_errors),
            "errors": [e.to_dict() for e in all_errors],
        }
        print(json.dumps(payload, indent=2))
    else:
        if not all_errors:
            print(f"PASS: All {len(files)} .pnach file(s) passed PCSX2 v2 lint checks.")
        else:
            print(
                f"FAIL: Found {len(all_errors)} lint error(s) across {len(files)} .pnach file(s):",
                file=sys.stderr,
            )
            for err in all_errors:
                print(f"  - {err.format_human()}", file=sys.stderr)

    return 0 if not all_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
