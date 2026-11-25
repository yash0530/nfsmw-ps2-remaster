# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Modding Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) for modern 4K/5K hardware on PCSX2 & Apple Silicon — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** currently ships only a single `[Remove Brown Filter]` patch.

**NFSMW: Rockport 2026** is a long-term, open-source engineering project to build:
1. **A Non-Destructive Modded PS2 ISO Builder** (`tools/build_modded_iso.py`) — reads your legally owned `SLUS-21351` ISO strictly read-only, creates a **brand-new bootable modded ISO** (using instant APFS copy-on-write cloning on macOS), bakes binary-verified ELF patches directly into `SLUS_213.51`, and supports clean virtual-file patching inside `ZDIR.BIN` / `ZZDATA0..3.BIN`.
2. **Pure-Python PS2 Reverse-Engineering Tools** — read-only ISO inspector (`iso_inspect.py`), `ZDIR.BIN`/`ZZDATA0..3.BIN` extractor with `bStringHash` resolution (`zdir_tool.py`), and a clean-room PS2 `VPAK`/`VLT` parser (`vlt_ps2.py`) for `GLOBAL\ATTRIBUTES.BIN` & `GLOBAL\GAMEPLAY.BIN`.
3. **Apple Silicon (Metal) PCSX2 4K Next-Gen Enhancement Profile** (`tools/setup_pcsx2_macos.py` + `patches/pnach/`) — 4K/5K internal resolution, 16x Anisotropic Filtering, 64 GB RAM texture precaching, AMD FidelityFX CAS + calibrated ShadeBoost color grading, true 16:9 widescreen, brown-filter removal, and motion-blur removal.
4. **4K Open-Source & Apple Silicon AI Texture Replacement Pipeline** (`tools/fetch_cc0_assets.py` + `tools/texture_pipeline.py`) — bypasses the PS2's 4 MB VRAM ceiling using CC0 PBR assets (Poly Haven / ambientCG), procedural 4K vector HUD generation, and local PyTorch MPS (`spandrel` + `PBRify` CC0) + native `arm64` `BC7_UNORM` mipmapped `.dds` compilation.

---

## Legal & "Bring Your Own ISO" Policy

**This repository contains ZERO copyrighted Electronic Arts or Sony files.**
- No `.iso`, `.bin`, `.bun`, `.elf`, BIOS, audio banks, or original game textures are hosted here.
- AI-upscaled textures derived from EA's original assets are generated **strictly locally** on your own machine from your own legally owned disc image.
- See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for our strict CC0 / SIL OFL / MIT asset policy.

---

## Quick Start (macOS Apple Silicon + PCSX2 2.6+)

### 1. Inspect Your Clean ISO (100% Read-Only)
```bash
python3 tools/iso_inspect.py "/path/to/Need for Speed - Most Wanted - Black Edition.iso"
```

### 2. Build a Brand-New Modded ISO (Never Touches Your Original!)
```bash
python3 tools/build_modded_iso.py \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --output build/NFSMW_BlackEdition_Rockport2026_v0.1.iso \
  --verify-source-sha256
```
This verifies your source ELF (`SLUS_213.51`, CRC `0518D274`), creates a separate modded ISO with verified visual/widescreen ELF patches baked in, computes the new modded ELF CRC, emits a companion `.pnach` for runtime RAM flags, and proves via SHA-256 that your original ISO was never touched.

### 3. Configure PCSX2 for 4K Metal + Texture Replacement
```bash
python3 tools/setup_pcsx2_macos.py \
  --crcs 0518D274 <NEW_MODDED_CRC> \
  --extra-pnach build/SLUS-21351_<NEW_MODDED_CRC>.pnach
```
*(Installs per-game `gamesettings/SLUS-21351_<CRC>.ini` and `patches/` only — never overwrites your global `PCSX2.ini`!)*

### 4. Explore PS2 Archives & VLT Databases
```bash
# List resolved virtual files inside ZZDATA0..3.BIN
python3 tools/zdir_tool.py list "/path/to/original.iso" --resolved-only

# Inspect PS2 GLOBAL\ATTRIBUTES.BIN (VPAK / VLT vaults)
python3 tools/vlt_ps2.py "/path/to/original.iso" --virtual-path 'GLOBAL\ATTRIBUTES.BIN'

# Search CC0 1.0 asphalt textures via Poly Haven REST API
python3 tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10

# Render a 2048x2048 procedural modern HUD tachometer prototype
python3 tools/texture_pipeline.py render-hud-demo --out build/hud_preview
```

---

## Repository Structure

```text
nfsmw-ps2-remaster/
├── docs/
│   ├── ROADMAP.md                                 # Multi-phase vision & PS2 vs 2026 engineering matrix
│   └── research/
│       ├── PS2_ARCHITECTURE.md                    # Verified SLUS-21351 ISO, ZDIR, VPAK & ELF specs
│       ├── PCSX2_MACOS_REMASTER.md                # PCSX2 2.6 Metal, BC7 DDS, glitches & librashader
│       └── OPEN_SOURCE_ASSETS_AND_PIPELINE.md     # CC0 sources, Apple Silicon MPS AI, legal BYO-ISO
├── patches/
│   ├── elf_patches.json                           # Declarative, byte-verified SLUS_213.51 ELF patches
│   └── pnach/
│       └── SLUS-21351_0518D274.pnach              # Verified PCSX2 v2 patch suite for Black Edition
├── tools/
│   ├── iso_inspect.py                             # Read-only ISO9660 / ELF CRC / ZDIR inspector
│   ├── zdir_tool.py                               # ZDIR.BIN + ZZDATA0..3.BIN reader & extractor
│   ├── vlt_ps2.py                                 # Clean-room PS2 VPAK/VLT parser (ATTRIBUTES.BIN)
│   ├── build_modded_iso.py                        # Non-destructive modded PS2 ISO builder
│   ├── setup_pcsx2_macos.py                       # Safe macOS Apple Silicon PCSX2 configurator
│   ├── fetch_cc0_assets.py                        # Poly Haven & ambientCG CC0 1.0 asset fetcher
│   ├── texture_pipeline.py                        # Apple Silicon MPS AI upscaler + BC7 DDS compiler
│   └── requirements-textures.txt                  # Optional PyTorch MPS + Spandrel dependencies
├── assets_cc0/
│   └── manifest.example.json                      # Starter CC0 1.0 material replacement manifest
├── Makefile                                       # One-command build & inspection shortcuts
├── ASSETS_LICENSE.md                              # Zero-copyright policy & attribution ledger
└── LICENSE                                        # MIT License
```

---

## Documentation & Roadmap

- [Multi-Phase Master Roadmap & Engineering Reality Check](docs/ROADMAP.md)
- [PS2 `SLUS-21351` Binary, `ZDIR.BIN`, & `VPAK` Architecture](docs/research/PS2_ARCHITECTURE.md)
- [PCSX2 macOS Metal, 4K BC7 Texture Replacement, & Shader Research](docs/research/PCSX2_MACOS_REMASTER.md)
- [Open-Source CC0 Assets & Apple Silicon Upscaling Pipeline](docs/research/OPEN_SOURCE_ASSETS_AND_PIPELINE.md)
