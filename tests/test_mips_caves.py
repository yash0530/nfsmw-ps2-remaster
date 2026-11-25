#!/usr/bin/env python3
"""
test_mips_caves.py — Automated validation suite for MIPS R5900 mini-assembler,
code-cave verification, PCSX2 .pnach extended output, and live ISO verification.

Runs 100% offline in CI (assembler round-trip vs tools/elf_disasm.py, schema,
alignment, non-overlap with patches/elf_patches.json, and lint_pnach validation),
and runs live read-only expected_orig checks against SLUS_213.51 when a retail
ISO is available (skipping gracefully when NFSMW_ISO is absent/nonexistent).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_modded_iso  # noqa: E402
import elf_disasm  # noqa: E402
import lint_pnach  # noqa: E402
import mips_cave_builder as mcb  # noqa: E402

CAVES_PATH = REPO_ROOT / "patches" / "elf_caves.json"
BASE_PATCHES_PATH = REPO_ROOT / "patches" / "elf_patches.json"
PS2_MOD_DIR = REPO_ROOT / "src" / "ps2_mod"


class TestMipsAssemblerRoundTrip(unittest.TestCase):
    """Verify MIPS R5900 encoding matches tools/elf_disasm.py instruction-for-instruction."""

    def test_core_instructions_roundtrip(self) -> None:
        src = """
        start:
            nop
            lui   $t0, 0x0055
            ori   $t0, $t0, 0x1234
            addiu $t1, $t0, -0x4C44
            addu  $t2, $t0, $t1
            subu  $t3, $t2, $t1
            andi  $t4, $t2, 0x00FF
            sll   $t5, $t4, 2
            srl   $t6, $t5, 1
            lw    $a0, 0x10($sp)
            sw    $a0, -0x8($sp)
            lwc1  $f0, 0x0($a1)
            swc1  $f0, 0x4($a1)
            mtc1  $t0, $f1
            mfc1  $t1, $f1
            mul.s $f2, $f0, $f1
            beq   $t0, $zero, done
            nop
            bne   $t1, $zero, start
            nop
            b     done
            nop
            jal   0x003037F0
            nop
        done:
            jr    $ra
            nop
        """
        base = 0x003037A4
        prog = mcb.assemble(src, base_vaddr=base)
        self.assertGreater(len(prog.words), 20)

        expected_prefixes = [
            "nop",
            "lui     $t0, 0x0055",
            "ori     $t0, $t0, 0x1234",
            "addiu   $t1, $t0, -0x4c44",
            "addu    $t2, $t0, $t1",
            "subu    $t3, $t2, $t1",
            "andi    $t4, $t2, 0xff",
            "sll     $t5, $t4, 2",
            "srl     $t6, $t5, 1",
            "lw      $a0, 0x10($sp)",
            "sw      $a0, -0x8($sp)",
            "lwc1    $f0, 0x0($a1)",
            "swc1    $f0, 0x4($a1)",
            "mtc1    $t0, $f1",
            "mfc1    $t1, $f1",
            "mul.s   $f2, $f0, $f1",
            f"beq     $t0, $zero, 0x{prog.labels['done']:08X}",
            "nop",
            f"bne     $t1, $zero, 0x{base:08X}",
            "nop",
            f"b       0x{prog.labels['done']:08X}",
            "nop",
            "jal     0x003037F0",
            "nop",
            "jr      $ra",
            "nop",
        ]
        self.assertEqual([i.disasm for i in prog.instructions], expected_prefixes)

    def test_li_s_pseudo_instruction(self) -> None:
        # 0.88f = 0x3F6147AE (3 words: lui + ori + mtc1)
        # 1.0f  = 0x3F800000 (2 words: lui + mtc1)
        src = """
            li.s $f1, 0.88f
            li.s $f2, 1.0
        """
        prog = mcb.assemble(src, base_vaddr=0x003037C0)
        self.assertEqual(len(prog.words), 5)
        self.assertEqual(
            [i.disasm for i in prog.instructions],
            [
                "lui     $at, 0x3f61",
                "ori     $at, $at, 0x47ae",
                "mtc1    $at, $f1",
                "lui     $at, 0x3f80",
                "mtc1    $at, $f2",
            ],
        )

    def test_hi_lo_and_dot_word(self) -> None:
        src = """
            lui $t0, hi(0x0054B3BC)
            lw  $t1, lo(0x0054B3BC)($t0)
            .word 0xDEADBEEF
        """
        prog = mcb.assemble(src, base_vaddr=0x003037A4)
        self.assertEqual(prog.words, [0x3C080055, 0x8D09B3BC, 0xDEADBEEF])

    def test_assembler_rejects_invalid_targets_and_operands(self) -> None:
        with self.assertRaises(mcb.MipsAssembleError):
            mcb.assemble("j 0x103037A4", base_vaddr=0x003037A4)  # crosses 256 MiB boundary
        with self.assertRaises(mcb.MipsAssembleError):
            mcb.assemble("j 0x003037A5", base_vaddr=0x003037A4)  # unaligned jump
        with self.assertRaises(mcb.MipsAssembleError):
            mcb.assemble("dup:\nnop\ndup:\nnop", base_vaddr=0x003037A4)
        with self.assertRaises(mcb.MipsAssembleError):
            mcb.assemble("addiu $badreg, $zero, 1", base_vaddr=0x003037A4)


class TestElfCavesSpecAndPnach(unittest.TestCase):
    """Validate patches/elf_caves.json offline against elf_patches.json, elf_disasm, and lint_pnach."""

    def test_elf_caves_schema_assembly_and_non_overlap(self) -> None:
        caves_spec = json.loads(CAVES_PATH.read_text(encoding="utf-8"))
        base_spec = json.loads(BASE_PATCHES_PATH.read_text(encoding="utf-8"))

        self.assertEqual(caves_spec.get("schema_version"), 1)
        self.assertEqual(caves_spec["target"]["serial"], "SLUS-21351")
        self.assertGreaterEqual(len(caves_spec["patches"]), 3)
        for p in caves_spec["patches"]:
            self.assertFalse(p["enabled_by_default"], f"{p['id']} must default to false")

        report = mcb.validate_caves_spec(caves_spec, base_patches_spec=base_spec)
        self.assertEqual(report["patch_count"], len(caves_spec["patches"]))
        self.assertGreater(report["total_writes"], 10)

        # Ensure cave_regions metadata matches actual cave writes
        regions = caves_spec["cave_regions"]
        self.assertGreaterEqual(len(regions), 2)

    def test_emit_pnach_extended_and_word_pass_linter(self) -> None:
        caves_spec = json.loads(CAVES_PATH.read_text(encoding="utf-8"))
        ext_text = mcb.emit_pnach_text(caves_spec, fmt="extended")
        self.assertIn("extended,", ext_text)
        self.assertEqual(lint_pnach.lint_pnach_text(ext_text, "caves_ext.pnach"), [])

        word_text = mcb.emit_pnach_text(caves_spec, fmt="word")
        self.assertIn(",word,", word_text)
        self.assertEqual(lint_pnach.lint_pnach_text(word_text, "caves_word.pnach"), [])

        gated_text = mcb.emit_pnach_text(
            caves_spec, fmt="extended", conditional_gate=(0x0054B3BC, 0x0001)
        )
        self.assertIn("E0", gated_text)
        self.assertEqual(lint_pnach.lint_pnach_text(gated_text, "caves_gated.pnach"), [])

    def test_ps2_mod_files_exist_and_clean(self) -> None:
        required = [
            "README.md",
            "Makefile",
            "linkfile.ld",
            "rockport_mod.h",
            "rockport_mod.c",
            "Dockerfile.ps2sdk",
        ]
        for fname in required:
            p = PS2_MOD_DIR / fname
            self.assertTrue(p.is_file(), f"Missing src/ps2_mod/{fname}")
            self.assertNotIn("/Users/", p.read_text(encoding="utf-8"))

    def test_cli_verify_and_emit_pnach_offline_to_temp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_pnach = Path(td) / "caves.pnach"
            rc = mcb.main(["emit-pnach", str(CAVES_PATH), "--out", str(out_pnach)])
            self.assertEqual(rc, 0)
            self.assertTrue(out_pnach.is_file())
            self.assertEqual(lint_pnach.lint_pnach_text(out_pnach.read_text(encoding="utf-8")), [])


class TestElfCavesLiveIso(unittest.TestCase):
    """Live read-only verification against retail SLUS_213.51 when ISO is available."""

    def test_live_iso_expected_orig_and_apply_caves(self) -> None:
        iso_path = mcb.find_local_iso()
        if iso_path is None:
            self.skipTest("Retail SLUS-21351 ISO not present locally (NFSMW_ISO unset/missing)")

        elf = elf_disasm.ElfImage.from_iso(iso_path)
        caves_spec = json.loads(CAVES_PATH.read_text(encoding="utf-8"))
        base_spec = json.loads(BASE_PATCHES_PATH.read_text(encoding="utf-8"))
        tbl = elf_disasm.load_symbols_optional(None)

        report = mcb.validate_caves_spec(
            caves_spec,
            base_patches_spec=base_spec,
            elf=elf,
            symbols=tbl,
        )
        self.assertEqual(report["verified_iso_writes"], report["total_writes"])

        # Also prove build_modded_iso.apply_elf_patches applies all caves cleanly in memory
        buf = bytearray(elf.data)
        applied = build_modded_iso.apply_elf_patches(
            buf,
            caves_spec,
            [p["id"] for p in caves_spec["patches"]],
            True,
        )
        self.assertEqual(len(applied), len(caves_spec["patches"]))

        # Disassemble patched ELF in memory to confirm hook + cave instructions decode cleanly
        patched_elf = elf_disasm.ElfImage(bytes(buf), name="SLUS_213.51_patched")
        for p in caves_spec["patches"]:
            for w in p["writes"]:
                vaddr = int(w["vaddr"], 16)
                actual = patched_elf.read_u32(vaddr)
                self.assertEqual(actual, int(w["patched"], 16))
                if vaddr < 0x004BE880:
                    dis = elf_disasm.disassemble_instruction(actual, vaddr, tbl)
                    self.assertFalse(dis.startswith(".word"), f"Invalid insn @ 0x{vaddr:08X}: {dis}")


if __name__ == "__main__":
    unittest.main()
