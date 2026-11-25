# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Modding Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) for modern 4K/5K hardware on PCSX2 & Apple Silicon — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** ships only visual toggles.

**NFSMW: Rockport 2026 (`v0.2.0`)** delivers an end-to-end, pure-Python clean-room PS2 modding & remaster suite:

1. **Reproducible Non-Destructive Modded ISO Builder (`tools/build_modded_iso.py` + `patches/profiles/rockport2026_v0.2.json`)**
   - Strictly opens your clean `SLUS-21351` ISO read-only (`'rb'`) and creates a brand-new bootable ISO via APFS copy-on-write cloning (with SHA-256 verification before & after).
   - Automatically updates duplicated `ZDIR.BIN` entries, recomputes EA `bCalculateCrc32` archive checksums (`1,294 / 1,294` retail entries verified), supports `ZZDATA3.BIN` append expansion + ISO9660/PVD sizing, and syncs `GLOBAL\GLOBALMEMORYFILE.BIN` when patching `GLOBAL\ATTRIBUTES.BIN`.
2. **19 Binary-Verified MIPS Engine Patches (`patches/elf_patches.json` + `tools/elf_disasm.py`)**
   - Built with our pure-Python MIPS R5900 disassembler + CC0 `dbalatoni13/nfsmw` symbol map:
     - **True Native 16:9 Widescreen** (`VideoSettings::Default` static bool `1` + `0.75f` projection + BE runtime `0x00AAFE54`), plus optional **21:9 Ultrawide** and **+12% FOV** modes
     - **Clean Visual Overhaul** (Brown Filter removal, smear-only Motion Blur removal that preserves bloom, plus optional Bloom/Fog/Brume/Raindrop/Radial-Blur toggles)
     - **High-LOD Cars & Wheels** (`g_ForceCarBodyLOD`/`g_ForceCarTireLOD` clamped safely via R5900 `pmaxw`/`pminw`), **Full-Rate 5-Face Car Reflections** every frame, and **~3× Extended Scenery Draw Distance & High-LOD World**
     - **Native 60 FPS Proof & `ScreenEFX` Fix:** Disassembly proved retail `SLUS_213.51` already runs `Scheduler`/`Sim` at 60 Hz natively (`g_FrameLimiter = 0`), so **PCSX2 180%+ EE Overclock** unlocks smooth 60 FPS without physics speedup — paired with our `UpdateAllScreenEFX` (`1/30f` → `1/60f`) fix!
     - **Gameplay / QoL Toggles:** Disable AI Catch-Up (`GRaceParameters::GetCatchUp`), Skip Intro Movies, Unlock All Cars & Parts.
3. **Full PS2 `VPAK`/`VLT` Gameplay, Camera & Visual Editor (`tools/vlt_ps2.py` + `patches/vlt/`)**
   - Clean-room 32-bit `VPAK` parser resolving **56 / 56 VLT classes** and **1,156 / 1,156 fields** (`2,301` collections in `ATTRIBUTES.BIN`, `411` in `FE_ATTRIB.BIN`, `6,287` in `GAMEPLAY.BIN`).
   - Ships **4 verified starter recipes (75 field edits)** baked cleanly into `v0.2`: `modern_camera.json`, `modern_handling_tuning.json`, `pursuit_and_traffic.json`, and `modern_visual_look.json`.
4. **PS2 Archive, `JDLZ` Codec & 721-Texture TPK Inspector (`tools/zdir_tool.py`, `tools/jdlz.py`, `tools/tpk_inspect.py`)**
   - `1,363 / 1,447` `ZDIR.BIN` rows (`94.2%`) mapped by path in `tools/zdir_names.txt`.
   - Pure-Python EA `JDLZ` v2 compressor/decompressor (`GAMEPLAY.LZC` decompresses byte-identically to `GAMEPLAY.BIN`) and EA `HUFF 0x30FB` + TPK `0xB3300000` reader cataloging `721` embedded textures.
5. **4K CC0 PBR Material Baker, Dual-Theme HUD Kit & PCSX2 Metal Configurator**
   - `tools/bake_pbr_to_diffuse.py`: Bakes CC0 Poly Haven / ambientCG PBR sets into tile-seamless PS2 diffuse + procedural wet-puddle roads.
   - `tools/generate_hud_pack.py`: Generates 2× supersampled (`4096→2048`) original `motorsport2026` & `rockport_neon` HUD kits.
   - `tools/setup_pcsx2_macos.py` & `tools/rockport_cli.py`: Audited against PCSX2 v2.6.3 (`Renderer = 17` Metal, explicit `[Patches] Enable = ...`, `6x` 4K internal res, `16x` AF, CAS, ShadeBoost, `180%` EE Overclock, full RAM texture precache) + `lint_pnach.py`, `audit_repo_clean.py`, and GitHub Actions CI.

---

## Legal & "Bring Your Own ISO" Policy

**This repository contains ZERO copyrighted Electronic Arts or Sony files.**
- No `.iso`, `.bin`, `.bun`, `.elf`, BIOS, audio banks, or original game textures are hosted here.
- Automated CI (`tools/audit_repo_clean.py`) verifies on every push that zero prohibited binaries, oversized files, or hardcoded personal paths enter Git.
- See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for our strict CC0 / SIL OFL / MIT policy.

---

## Quick Start (macOS Apple Silicon + PCSX2 2.6.3+)

### 1. Check Status & Run Offline Verification
```bash
python3 tools/rockport_cli.py status
python3 tools/rockport_cli.py verify
```

### 2. Build the `v0.2` Modded ISO (Never Touches Your Original!)
```bash
python3 tools/build_modded_iso.py \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --profile patches/profiles/rockport2026_v0.2.json \
  --verify-source-sha256
```
This verifies your source ISO (`SLUS_213.51`, CRC `0518D274`), builds `build/NFSMW_BlackEdition_Rockport2026_v0.2.iso` with all 7 MIPS ELF patches + 4 VLT recipes (syncing `GLOBALMEMORYFILE.BIN` and `ZDIR.BIN` checksums), emits a matching companion `.pnach`, and proves via SHA-256 that your original ISO was never touched.

### 3. Install PCSX2 4K Metal + 60 FPS Per-Game Profiles
```bash
python3 tools/setup_pcsx2_macos.py \
  --crcs 0518D274 <MODDED_CRC> \
  --extra-pnach build/SLUS-21351_<MODDED_CRC>.pnach \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```
*(Configures per-game `gamesettings/SLUS-21351_<CRC>.ini` and `patches/` only — never modifies your global `PCSX2.ini`!)*

### 4. Explore Modding & 4K Asset Tools
```bash
# Inspect VLT camera/handling/visual nodes inside the ISO
python3 tools/vlt_ps2.py dump-node "/path/to/iso" visuallook default

# Disassemble or verify ELF patches against SLUS_213.51
python3 tools/elf_disasm.py --iso "/path/to/iso" verify-patches patches/elf_patches.json

# Catalog embedded PS2 TPK textures (HUD, cars, wheels, dynamic)
python3 tools/tpk_inspect.py --iso "/path/to/iso" 'GLOBAL\HUDTEXRACE.BIN'

# Fetch CC0 PBR asphalt, bake dry & wet-puddle diffuse textures, and render 4K HUD kits
python3 tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10
python3 tools/generate_hud_pack.py --theme all --out build/hud_pack
```

---

## Repository Structure

```text
nfsmw-ps2-remaster/
├── .github/workflows/ci.yml                       # Automated Python 3.11/3.12 CI & hygiene checks
├── assets_cc0/
│   └── manifest.example.json                      # Verified CC0 1.0 Poly Haven & ambientCG manifest
├── docs/
│   ├── ROADMAP.md                                 # Multi-phase vision & PS2 vs 2026 engineering matrix
│   └── research/
│       ├── PS2_ARCHITECTURE.md                    # SLUS-21351 ISO, ZDIR, VPAK & ELF ground truth
│       ├── PCSX2_MACOS_REMASTER.md                # PCSX2 2.6.3 Metal, BC7 DDS & shader pipeline
│       ├── OPEN_SOURCE_ASSETS_AND_PIPELINE.md     # CC0 sources, Apple Silicon MPS AI, BYO-ISO model
│       ├── MIPS_REVERSE_ENGINEERING.md            # SLUS_213.51 disassembly proofs & 60 FPS analysis
│       └── VLT_PS2_SCHEMA.md                      # AttribSys VPAK layout, MemoryFile sync & lighting
├── patches/
│   ├── elf_patches.json                           # 19 binary-verified MIPS patches (38 writes)
│   ├── pnach/SLUS-21351_0518D274.pnach            # Complete PCSX2 v2 patch suite for SLUS-21351
│   ├── profiles/rockport2026_v0.2.json            # End-to-end v0.2 ISO build profile
│   └── vlt/                                       # 4 verified VLT recipes (75 writes total)
├── tests/                                         # 81 automated unit & live read-only ISO tests
├── tools/                                         # Full clean-room CLI toolchain + rockport_cli.py
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
