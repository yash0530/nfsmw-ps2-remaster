#!/usr/bin/env python3
"""
test_elf_patches.py — Automated validation suite for SLUS_213.51 ELF patches,
PCSX2 v2 .pnach suite, and the pure-Python MIPS R5900 disassembler.

Runs 100% offline in CI (JSON schema, alignment, overlap, MIPS decoder,
synthetic ELF patching, and .pnach consistency), and automatically runs full
read-only byte-for-byte ELF verification against the unmodified retail ISO
when present locally (via NFSMW_ISO env var or default PS2Games directory).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_modded_iso  # noqa: E402
import elf_disasm  # noqa: E402
import lint_pnach  # noqa: E402
import setup_pcsx2_macos  # noqa: E402

PATCHES_PATH = REPO_ROOT / "patches" / "elf_patches.json"
PNACH_PATH = REPO_ROOT / "patches" / "pnach" / "SLUS-21351_0518D274.pnach"

EXPECTED_ELF_NAME = "SLUS_213.51"
EXPECTED_ELF_SIZE = 4918740
EXPECTED_ELF_SHA1 = "60289f3f8b643822a302720f9649ddc4ba55034b"
EXPECTED_ELF_CRC = "0518D274"
EXPECTED_PT_VADDR = 0x00100000
EXPECTED_PT_OFFSET = 0x00001000
EXPECTED_PT_FILESZ = 0x004ABBDC

HEX32_RE = re.compile(r"^0x[0-9A-Fa-f]{8}$")
PNACH_PATCH_RE = re.compile(
    r"^patch=(?P<mode>[012]),(?P<cpu>EE|IOP),(?P<addr>[0-9A-Fa-f]{8}),"
    r"(?P<size>byte|short|word|double|extended|lefloat),(?P<val>[0-9A-Fa-f]+)$"
)


def _find_local_iso() -> Optional[Path]:
    """Locate the local retail ISO strictly for read-only verification (no hardcoded user paths)."""
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso:
        p = Path(env_iso).expanduser()
        return p if p.is_file() else None
    default_iso = (
        Path.home()
        / "Documents"
        / "PS2Games"
        / "Need for Speed - Most Wanted - Black Edition.iso"
    )
    return default_iso if default_iso.is_file() else None


def _make_synthetic_elf(
    pt_vaddr: int = EXPECTED_PT_VADDR,
    pt_offset: int = EXPECTED_PT_OFFSET,
    code_words: Optional[Dict[int, int]] = None,
    filesz: int = 0x4000,
) -> bytes:
    """Construct a minimal valid 32-bit LE MIPS ELF with one PT_LOAD segment for offline tests."""
    buf = bytearray(pt_offset + filesz)
    # ELF32 header (52 bytes)
    # e_ident: \x7fELF, ELFCLASS32=1, ELFDATA2LSB=1, EV_CURRENT=1
    buf[0:16] = b"\x7fELF\x01\x01\x01" + b"\x00" * 9
    # e_type=2 (ET_EXEC), e_machine=8 (EM_MIPS), e_version=1, e_entry, e_phoff=52, e_shoff=0,
    # e_flags=0x20924001 (R5900), e_ehsize=52, e_phentsize=32, e_phnum=1, e_shentsize=0, e_shnum=0, e_shstrndx=0
    struct.pack_into(
        "<HHIIIIIHHHHHH",
        buf,
        16,
        2,
        8,
        1,
        pt_vaddr,
        52,
        0,
        0x20924001,
        52,
        32,
        1,
        0,
        0,
        0,
    )
    # Single PT_LOAD program header at offset 52 (32 bytes)
    # p_type=1, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags=7, p_align=0x1000
    struct.pack_into(
        "<IIIIIIII",
        buf,
        52,
        1,
        pt_offset,
        pt_vaddr,
        pt_vaddr,
        filesz,
        filesz,
        7,
        0x1000,
    )
    if code_words:
        for vaddr, word in code_words.items():
            off = vaddr - pt_vaddr + pt_offset
            struct.pack_into("<I", buf, off, word)
    return bytes(buf)


class TestElfPatchJsonSchema(unittest.TestCase):
    """Validate JSON schema, alignment, uniqueness, and conflict safety for all patch specs."""

    def _validate_patch_file(self, path: Path) -> dict:
        self.assertTrue(path.is_file(), f"Missing patch spec: {path}")
        spec = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(spec, dict)
        self.assertEqual(spec.get("schema_version"), 1)

        target = spec.get("target")
        self.assertIsInstance(target, dict)
        self.assertEqual(target.get("serial"), "SLUS-21351")
        self.assertEqual(target.get("elf_name"), EXPECTED_ELF_NAME)
        self.assertEqual(target.get("elf_size"), EXPECTED_ELF_SIZE)
        self.assertEqual(target.get("elf_pcsx2_crc"), EXPECTED_ELF_CRC)
        self.assertRegex(target.get("pt_load_vaddr", ""), HEX32_RE)
        self.assertRegex(target.get("pt_load_file_offset", ""), HEX32_RE)

        pt_vaddr = int(target["pt_load_vaddr"], 16)
        pt_off = int(target["pt_load_file_offset"], 16)
        self.assertEqual(pt_vaddr, EXPECTED_PT_VADDR)
        self.assertEqual(pt_off, EXPECTED_PT_OFFSET)

        patches = spec.get("patches")
        self.assertIsInstance(patches, list)
        self.assertGreater(len(patches), 0)

        seen_ids: Set[str] = set()
        default_vaddrs: Dict[int, str] = {}
        default_ids: Set[str] = set()

        for patch in patches:
            pid = patch.get("id")
            self.assertIsInstance(pid, str)
            self.assertRegex(pid, r"^[a-z0-9_]+$")
            self.assertNotIn(pid, seen_ids, f"Duplicate patch ID: {pid}")
            seen_ids.add(pid)

            for field in ("name", "description", "author"):
                self.assertIsInstance(patch.get(field), str)
                self.assertTrue(patch[field].strip(), f"{pid}: empty {field}")

            enabled = patch.get("enabled_by_default")
            self.assertIsInstance(enabled, bool, f"{pid}: enabled_by_default must be bool")
            if enabled:
                default_ids.add(pid)

            writes = patch.get("writes")
            self.assertIsInstance(writes, list)
            self.assertGreater(len(writes), 0, f"{pid}: must have at least one write")

            patch_vaddrs: Set[int] = set()
            for w in writes:
                for hex_field in ("vaddr", "expected_orig", "patched"):
                    self.assertRegex(
                        w.get(hex_field, ""),
                        HEX32_RE,
                        f"{pid}: invalid {hex_field} {w.get(hex_field)!r}",
                    )
                vaddr = int(w["vaddr"], 16)
                orig = int(w["expected_orig"], 16)
                patched = int(w["patched"], 16)

                # Must be 4-byte aligned and inside SLUS_213.51 PT_LOAD file range
                self.assertEqual(vaddr % 4, 0, f"{pid}: unaligned vaddr 0x{vaddr:08X}")
                self.assertGreaterEqual(vaddr, pt_vaddr, f"{pid}: vaddr below PT_LOAD")
                self.assertLessEqual(
                    vaddr + 4,
                    pt_vaddr + EXPECTED_PT_FILESZ,
                    f"{pid}: vaddr 0x{vaddr:08X} exceeds PT_LOAD filesz",
                )
                self.assertNotEqual(orig, patched, f"{pid}: no-op write at 0x{vaddr:08X}")
                self.assertNotIn(vaddr, patch_vaddrs, f"{pid}: duplicate vaddr 0x{vaddr:08X}")
                patch_vaddrs.add(vaddr)

                if enabled:
                    self.assertNotIn(
                        vaddr,
                        default_vaddrs,
                        f"Default patch conflict at 0x{vaddr:08X}: {default_vaddrs.get(vaddr)} vs {pid}",
                    )
                    default_vaddrs[vaddr] = pid

        # Validate mutually_exclusive_with declarations
        for patch in patches:
            excl = patch.get("mutually_exclusive_with", [])
            self.assertIsInstance(excl, list)
            for other_id in excl:
                self.assertIn(
                    other_id,
                    seen_ids,
                    f"{patch['id']}: unknown mutually_exclusive_with target {other_id!r}",
                )
                self.assertFalse(
                    patch["enabled_by_default"] and other_id in default_ids,
                    f"Mutually exclusive patches both enabled by default: {patch['id']} and {other_id}",
                )

        return spec

    def test_elf_patches_schema_and_inventory(self) -> None:
        spec = self._validate_patch_file(PATCHES_PATH)
        self.assertEqual(spec["target"].get("elf_sha1"), EXPECTED_ELF_SHA1)
        patches = spec["patches"]
        self.assertEqual(len(patches), 20)

        default_ids = [p["id"] for p in patches if p["enabled_by_default"]]
        self.assertEqual(
            default_ids,
            [
                "remove_brown_filter",
                "remove_motion_blur",
                "widescreen_16_9_native",
                "high_lod_cars",
                "full_rate_car_reflections",
                "extended_scenery_draw_distance",
            ],
        )

        total_writes = sum(len(p["writes"]) for p in patches)
        self.assertEqual(total_writes, 42)


class TestElfDisasmOffline(unittest.TestCase):
    """Offline unit tests for tools/elf_disasm.py (address math, symbols, MIPS R5900 decode, xrefs)."""

    def test_address_translation_roundtrip(self) -> None:
        cases = [
            (0x00100000, 0x00001000),
            (0x0020F5B0, 0x001105B0),
            (0x0026D9F4, 0x0016E9F4),
            (0x00550D58, 0x00451D58),
            (0x0059FF28, 0x004A0F28),
        ]
        for vaddr, expected_off in cases:
            self.assertEqual(elf_disasm.vaddr_to_elf_offset(vaddr), expected_off)
            self.assertEqual(elf_disasm.elf_offset_to_vaddr(expected_off), vaddr)

    def test_symbol_table_parsing_and_queries(self) -> None:
        sample = """
// src/Ecstasy/eView.cpp
eDisplayFrame__Fv = 0x20DA90; // type:func
g_FrameLimiter = 0x54B5D8; // type:data size:0x4

// src/Game/VideoSettings.cpp
Default__13VideoSettings = 0x26D9F0; // type:func
"""
        tbl = elf_disasm.SymbolTable.from_text(sample)
        self.assertEqual(len(tbl), 3)

        sym = tbl.get_by_name("Default__13VideoSettings")
        self.assertIsNotNone(sym)
        assert sym is not None
        self.assertEqual(sym.vaddr, 0x0026D9F0)
        self.assertEqual(sym.sym_type, "func")
        self.assertEqual(sym.source_unit, "src/Game/VideoSettings.cpp")

        self.assertEqual(tbl.format_addr(0x0020DA90), "eDisplayFrame__Fv")
        self.assertEqual(tbl.format_addr(0x0020DA9C), "eDisplayFrame__Fv+0xc")
        self.assertEqual(tbl.next_symbol_addr(0x0020DA90), 0x0026D9F0)

        funcs = tbl.search("VideoSettings", sym_type="func")
        self.assertEqual([s.name for s in funcs], ["Default__13VideoSettings"])
        datas = tbl.search("Limiter", sym_type="data")
        self.assertEqual([s.name for s in datas], ["g_FrameLimiter"])

    def test_mips_r5900_instruction_decoding(self) -> None:
        tbl = elf_disasm.SymbolTable.from_text("TargetFn = 0x0020F684; // type:func\n")
        vectors: List[Tuple[int, int, str]] = [
            (0x00000000, 0x00100000, "nop"),
            (0x03E00008, 0x00219A68, "jr      $ra"),
            (0x0000102D, 0x002B595C, "move    $v0, $zero"),
            (0x24010001, 0x0026D9F8, "li      $at, 1"),
            (0x3C014040, 0x003DB178, "lui     $at, 0x4040"),
            (0x34210000, 0x003DB17C, "ori     $at, $at, 0x0"),
            (0x10400034, 0x0020F5B0, "beq     $v0, $zero, 0x0020F684"),
            (0x10000034, 0x0020F5B0, "b       0x0020F684"),
            (0x2A020012, 0x003DB110, "slti    $v0, $s0, 0x12"),
            (0xAC81000C, 0x0026D9FC, "sw      $at, 0xc($a0)"),
            (0x44810000, 0x0026D9F4, "mtc1    $at, $f0"),
            (0xE4800000, 0x0026D9FC, "swc1    $f0, 0x0($a0)"),
            (0x46020800, 0x00100000, "add.s   $f0, $f1, $f2"),
            (0x706210C8, 0x003EC7BC, "pmaxw   $v0, $v1, $v0"),
            (0x706210E8, 0x003EC7C0, "pminw   $v0, $v1, $v0"),
            (0x0C083DA1, 0x00100000, "jal     0x0020F684  # TargetFn"),
        ]
        for word, pc, expected in vectors:
            got = elf_disasm.disassemble_instruction(word, pc, symbols=tbl)
            self.assertEqual(got, expected, f"word=0x{word:08X} @ 0x{pc:08X}: got {got!r}")

    def test_synthetic_elf_disasm_xrefs_and_patch_application(self) -> None:
        # Build synthetic code referencing 0x00101000 via lui+lw and jal 0x00100020
        target_data_vaddr = 0x00101000
        callee_vaddr = 0x00100020
        code_words = {
            0x00100000: 0x3C020010,  # lui   $v0, 0x0010
            0x00100004: 0x8C421000,  # lw    $v0, 0x1000($v0) -> 0x00101000
            0x00100008: 0x0C040008,  # jal   0x00100020
            0x0010000C: 0x00000000,  # nop
            0x00100020: 0x03E00008,  # jr    $ra
            0x00100024: 0x00000000,  # nop
            target_data_vaddr: 0x3F35C28F,  # 0.71f
        }
        raw = _make_synthetic_elf(code_words=code_words)
        elf = elf_disasm.ElfImage(raw, name="SYNTH.ELF")
        tbl = elf_disasm.SymbolTable.from_text(
            "MainFn = 0x100000; // type:func\n"
            "CalleeFn = 0x100020; // type:func\n"
            "AspectFloat = 0x101000; // type:data size:0x4\n"
        )

        lines = elf_disasm.disassemble_range(elf, 0x00100000, 4, symbols=tbl)
        joined = "\n".join(lines)
        self.assertIn("<MainFn> @ 0x00100000:", joined)
        self.assertIn("AspectFloat", joined)
        self.assertIn("0.71", joined)
        self.assertIn("jal     0x00100020  # CalleeFn", joined)

        data_xrefs = elf_disasm.find_xrefs(elf, target_data_vaddr, symbols=tbl, text_end_vaddr=0x00100100)
        self.assertEqual([pc for pc, _, _ in data_xrefs], [0x00100004])

        call_xrefs = elf_disasm.find_xrefs(elf, callee_vaddr, symbols=tbl, text_end_vaddr=0x00100100)
        self.assertEqual([pc for pc, _, _ in call_xrefs], [0x00100008])

        # Verify build_modded_iso.apply_elf_patches works and enforces expected_orig strictly
        mini_spec = {
            "target": {
                "pt_load_vaddr": "0x00100000",
                "pt_load_file_offset": "0x00001000",
            },
            "patches": [
                {
                    "id": "aspect_fix",
                    "name": "Aspect Fix",
                    "enabled_by_default": True,
                    "writes": [
                        {
                            "vaddr": "0x00101000",
                            "expected_orig": "0x3F35C28F",
                            "patched": "0x3F400000",
                        }
                    ],
                }
            ],
        }
        buf = bytearray(raw)
        applied = build_modded_iso.apply_elf_patches(buf, mini_spec, None, include_optional=False)
        self.assertEqual(len(applied), 1)
        off = elf_disasm.vaddr_to_elf_offset(target_data_vaddr)
        self.assertEqual(struct.unpack_from("<I", buf, off)[0], 0x3F400000)

        # Re-applying to already patched buffer must abort cleanly due to expected_orig mismatch
        with self.assertRaises(ValueError):
            build_modded_iso.apply_elf_patches(buf, mini_spec, None, include_optional=False)


class TestPnachConsistency(unittest.TestCase):
    """Verify SLUS-21351_0518D274.pnach lint cleanliness, default auto-enable set, and JSON parity."""

    def test_pnach_lints_cleanly(self) -> None:
        self.assertTrue(PNACH_PATH.is_file(), f"Missing .pnach: {PNACH_PATH}")
        errs = lint_pnach.lint_pnach_file(PNACH_PATH)
        self.assertEqual(errs, [], f".pnach lint errors: {errs}")

    def test_pnach_widescreen_uses_black_edition_flag_address(self) -> None:
        text = PNACH_PATH.read_text(encoding="utf-8")
        self.assertIn("[Widescreen 16:9]", text)
        self.assertIn("gsaspectratio=16:9", text)
        self.assertIn("patch=1,EE,00AAFE54,word,00000001", text)
        # Ensure stale standard-edition SLUS-21267 flag address and unlicensed third-party blocks are absent
        self.assertNotIn("00AAFDD4", text)
        self.assertNotIn("PeterDelta", text)
        self.assertNotIn("005500F0", text)

    def test_pnach_default_auto_enable_matches_six_safe_defaults(self) -> None:
        defaults = setup_pcsx2_macos.extract_default_pnach_sections(PNACH_PATH)
        self.assertEqual(
            defaults,
            [
                "Widescreen 16:9",
                "Remove Brown Filter",
                "Disable Motion Blur Effect",
                "Force High-LOD Cars and Wheels",
                "Full-Rate Car Reflections (All 5 Faces Every Frame)",
                "Extended Scenery Draw Distance and High-LOD World",
            ],
        )

    def test_every_elf_write_is_represented_in_pnach(self) -> None:
        spec = json.loads(PATCHES_PATH.read_text(encoding="utf-8"))
        pnach_writes: Set[Tuple[int, int]] = set()
        for raw_line in PNACH_PATH.read_text(encoding="utf-8").splitlines():
            m = PNACH_PATCH_RE.match(raw_line.strip())
            if m and m.group("cpu") == "EE" and m.group("size") == "word":
                pnach_writes.add((int(m.group("addr"), 16), int(m.group("val"), 16)))

        for patch in spec["patches"]:
            for w in patch["writes"]:
                pair = (int(w["vaddr"], 16), int(w["patched"], 16))
                self.assertIn(
                    pair,
                    pnach_writes,
                    f"Write from {patch['id']} ({w['vaddr']} -> {w['patched']}) missing in {PNACH_PATH.name}",
                )


class TestLiveRetailElfVerification(unittest.TestCase):
    """Read-only byte-for-byte verification against the unmodified retail SLUS_213.51 inside the ISO."""

    _iso_path: Optional[Path] = None
    _elf: Optional[elf_disasm.ElfImage] = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._iso_path = _find_local_iso()
        if cls._iso_path is None:
            raise unittest.SkipTest(
                "Local retail SLUS-21351 ISO not present (set NFSMW_ISO to enable live ELF tests)"
            )
        cls._elf = elf_disasm.ElfImage.from_iso(cls._iso_path, elf_name=EXPECTED_ELF_NAME)

    def test_elf_identity_hashes_and_segments(self) -> None:
        assert self._elf is not None
        self.assertEqual(len(self._elf.data), EXPECTED_ELF_SIZE)
        self.assertEqual(self._elf.header["pcsx2_crc"], EXPECTED_ELF_CRC)
        self.assertEqual(hashlib.sha1(self._elf.data).hexdigest(), EXPECTED_ELF_SHA1)
        self.assertEqual(self._elf.pt_vaddr, EXPECTED_PT_VADDR)
        self.assertEqual(self._elf.pt_offset, EXPECTED_PT_OFFSET)
        self.assertEqual(self._elf.pt_filesz, EXPECTED_PT_FILESZ)

    def test_all_expected_orig_words_match_byte_for_byte(self) -> None:
        assert self._elf is not None
        spec = json.loads(PATCHES_PATH.read_text(encoding="utf-8"))
        checked = 0
        for patch in spec["patches"]:
            for w in patch["writes"]:
                vaddr = int(w["vaddr"], 16)
                exp = int(w["expected_orig"], 16)
                pat = int(w["patched"], 16)
                actual = self._elf.read_u32(vaddr)
                self.assertEqual(
                    actual,
                    exp,
                    f"{patch['id']} @ 0x{vaddr:08X}: expected 0x{exp:08X}, got 0x{actual:08X}",
                )
                # Every patched code word in .text must disassemble to a valid MIPS instruction
                if vaddr < elf_disasm.DEFAULT_TEXT_END_VADDR:
                    dis = elf_disasm.disassemble_instruction(pat, vaddr)
                    self.assertFalse(
                        dis.startswith(".word"),
                        f"{patch['id']} @ 0x{vaddr:08X}: patched instruction 0x{pat:08X} failed decode",
                    )
                checked += 1
        self.assertEqual(checked, 42)

    def test_apply_elf_patches_defaults_and_each_individual_patch(self) -> None:
        assert self._elf is not None
        spec = json.loads(PATCHES_PATH.read_text(encoding="utf-8"))

        # 1. All 6 default-enabled patches apply cleanly together
        default_buf = bytearray(self._elf.data)
        applied_defaults = build_modded_iso.apply_elf_patches(
            default_buf, spec, selected_ids=None, include_optional=False
        )
        self.assertEqual(len(applied_defaults), 6)
        self.assertNotEqual(bytes(default_buf), self._elf.data)

        # 2. Every single patch applies cleanly on a fresh copy of the ELF
        for patch in spec["patches"]:
            buf = bytearray(self._elf.data)
            applied = build_modded_iso.apply_elf_patches(
                buf, spec, selected_ids=[patch["id"]], include_optional=True
            )
            self.assertEqual(len(applied), 1)
            self.assertEqual(applied[0]["id"], patch["id"])
            for w in patch["writes"]:
                off = elf_disasm.vaddr_to_elf_offset(int(w["vaddr"], 16))
                self.assertEqual(struct.unpack_from("<I", buf, off)[0], int(w["patched"], 16))

    def test_disassembly_invariants_for_widescreen_reflections_and_60fps(self) -> None:
        assert self._elf is not None
        # VideoSettings::Default (0x0026D9F0..0x0026D9FC)
        self.assertEqual(
            elf_disasm.disassemble_instruction(self._elf.read_u32(0x0026D9F0), 0x0026D9F0),
            "lui     $at, 0x3f80",
        )
        self.assertEqual(
            elf_disasm.disassemble_instruction(self._elf.read_u32(0x0026D9F8), 0x0026D9F8),
            "sw      $zero, 0xc($a0)",
        )
        # IsFaceEnabled envmap throttle branch
        self.assertEqual(
            elf_disasm.disassemble_instruction(self._elf.read_u32(0x0020F5B0), 0x0020F5B0),
            "beq     $v0, $zero, 0x0020F684",
        )
        # Retail 30 FPS limiter flag in eDisplayFrame is disabled (0) by default -> native unlocked 60 FPS
        self.assertEqual(self._elf.read_u32(0x0054B5D8), 0)
        # UpdateAllScreenEFX hardcoded 1/30f upper half (0x3D088889)
        self.assertEqual(
            elf_disasm.disassemble_instruction(self._elf.read_u32(0x003DE778), 0x003DE778),
            "lui     $at, 0x3d08",
        )
        self.assertEqual(
            elf_disasm.disassemble_instruction(self._elf.read_u32(0x003DE77C), 0x003DE77C),
            "ori     $at, $at, 0x8889",
        )
        # UpdateAndRender__4Rain dry-road dampness branch + g_MaxRoadDampness (1.0f) + DrawAScenery reflection cull
        self.assertEqual(
            elf_disasm.disassemble_instruction(self._elf.read_u32(0x00407560), 0x00407560),
            "bc1t    0x00407570",
        )
        self.assertEqual(self._elf.read_u32(0x005A2C80), 0x3F800000)
        self.assertEqual(
            elf_disasm.disassemble_instruction(self._elf.read_u32(0x003DB0D4), 0x003DB0D4),
            "slti    $v0, $s0, 0x20",
        )


if __name__ == "__main__":
    unittest.main()
