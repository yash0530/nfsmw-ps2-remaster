# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Modding Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) for modern 4K/5K hardware on PCSX2 & Apple Silicon — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** ships only visual toggles.

**NFSMW: Rockport 2026 (`v0.3.0`)** delivers a complete, pure-Python clean-room PS2 modding, lighting, texture, audio/geometry, and ISO mastering suite:

1. **One-Command Modded ISO Builder & 6 Curated Presets (`tools/rockport_cli.py`, `tools/build_modded_iso.py`)**
   - Strictly reads your clean `SLUS-21351` ISO read-only (`'rb'`) and clones a separate bootable ISO via APFS copy-on-write with SHA-256 verification before & after.
   - Dual-syncs `GLOBAL\GLOBALMEMORYFILE.BIN` Entry `[1]` (`ATTRIBUTES.BIN`) **and** Entry `[2]` (`GLOBALB.LZC` JDLZ v2), updates duplicated `ZDIR.BIN` rows, recomputes EA `bCalculateCrc32` checksums (`1,294 / 1,294` retail entries verified), and supports `ZZDATA3.BIN` append + ISO9660/PVD sizing.
   - **6 Validated Build Profiles (`patches/profiles/`):**
     - `rockport2026_v0.3` *(Flagship)* — 7 ELF fixes + 5 VLT recipes (`360` edits across 39 cars, cameras, traffic, pursuits, `visuallook`) + `rockport2026_daylight` (`37` `SunInfos` & `LightMaterials` edits)
     - `rockport2026_golden_hour` — Same full overhaul with Forza-inspired Golden Hour sunset lighting (`52` edits)
     - `rockport2026_purist_hd` — Crisp 16:9, visual/LOD/reflection/60Hz EFX fixes preserving 100% stock handling & lighting
     - `rockport2026_ultrawide_21_9` — True 21:9 cinema aspect ratio + widened frustum culling + PCSX2 `AspectRatio = Stretch`
     - `rockport2026_sandbox` — Instant unlocks + skip intros + no AI rubber-banding + 4 VLT recipes
     - `rockport2026_v0.2` — Baseline v0.2 profile
2. **19 Binary-Verified MIPS Engine Patches (`patches/elf_patches.json` + `tools/elf_disasm.py`)**
   - Pure-Python MIPS R5900 disassembler + CC0 `dbalatoni13/nfsmw` symbol integration:
     - **True Native 16:9 Widescreen** (`VideoSettings::Default` static bool `1` + `0.75f` projection + BE flag `0x00AAFE54`), optional **21:9 Ultrawide**, and **+12% FOV**
     - **Clean Visual Overhaul** (Brown Filter removal, smear-only Motion Blur fix preserving bloom, optional Bloom/Fog/Brume/Raindrop/Radial-Blur toggles)
     - **High-LOD Cars & Wheels**, **Full-Rate 5-Face Car Reflections** every frame, **~3× Extended Scenery Draw Distance & High-LOD World**
     - **Native 60 FPS Proof & `ScreenEFX` Fix:** Disassembly proved retail `SLUS_213.51` runs `Scheduler`/`Sim` at native 60 Hz (`g_FrameLimiter = 0`); PCSX2 `180%+` EE Overclock + our `UpdateAllScreenEFX` (`1/30f → 1/60f`) patch unlocks smooth 60 FPS without speedup!
3. **PS2 Lighting, `SunInfos` & `LightMaterials` Pipeline (`tools/lighting_ps2.py` + `patches/lighting/`)**
   - Unpacks `GLOBAL\GLOBALB.LZC` (`JDLZ` v2), parses all 5 `SunInfos` (`0x00034202`) profiles (`MIDDAY`/`SUNSET`/`PARKLAND`) and 156 `LightMaterials` (`0x00135200`), fixes EA's amber `MIDDAY` sun layers and crushed `ROAD` diffuse `[0.90, 0.57, 0.56]`, and re-compresses compactly in-place into `GLOBALMEMORYFILE.BIN` Entry `[2]`.
4. **Full PS2 `VPAK`/`VLT` Engine & 39-Car Roster Overhaul (`tools/vlt_ps2.py` + `patches/vlt/`)**
   - Resolves **56 / 56 VLT classes** and **1,156 / 1,156 fields** (`2,301` collections in `ATTRIBUTES.BIN`, `411` in `FE_ATTRIB.BIN`, `6,287` in `GAMEPLAY.BIN`).
   - Ships **5 zero-overlap verified VLT recipes (360 edits total)**: `modern_camera.json`, `modern_handling_tuning.json`, `full_roster_handling.json` (39 career & Black Edition cars), `pursuit_and_traffic.json`, and `modern_visual_look.json`.
5. **PS2 GS TPK Pixel Extractor, Perceptual Dump Matcher & 4K CC0 Baker**
   - `tools/tpk_extract.py`: Pure-Python PS2 GS decoder (`PSMT4`/`PSMT8`/`PSMCT32` + CSM1 CLUT unswizzling + EA `HUFF`/`JDLZ`), extracting **916** clean RGBA textures read-only across 8 ISO archives.
   - `tools/bake_pbr_to_diffuse.py` & `tools/generate_hud_pack.py`: Bakes CC0 Poly Haven/ambientCG PBR sets into 4K tile-seamless dry/wet-puddle diffuse surfaces + renders 2× supersampled (`4096→2048`) dual-theme HUD kits (`motorsport2026` & `rockport_neon`).
   - `tools/match_texture_dumps.py` & `assets_cc0/material_pack_rules.json`: Alpha-aware dHash/pHash + histogram matcher linking PCSX2 dumps (`<TexHash>-<ClutHash>.png`) to 4K replacements ready for `texture_pipeline.py encode-dds`.
6. **PS2 Audio & Car Geometry Inspectors (`tools/audio_inspect.py`, `tools/geometry_inspect.py`)**
   - Inspects all **301 `ABKC` banks** (2,350 samples), **160 `Gnsu` v20 engine synths**, **41 `SCHl` containers / 1,980 streams**, and **97 `0x80134000` geometry packs** (10,751 solids / 2.07M polys) with exact VU1 LFSR ADC `.obj` preview export.
7. **PCSX2 v2.6.3 Audited Configurator, Hygiene Guardrails & CI (`tools/setup_pcsx2_macos.py`, `.github/workflows/ci.yml`)**
   - Verified against PCSX2 v2.6.3 (`Renderer = 17` Metal, explicit `[Patches] Enable = ...`, `6x` 4K, `16x` AF, CAS, ShadeBoost, `180%` EE Overclock, full RAM texture precache, global `PCSX2.ini` never touched), `lint_pnach.py`, `audit_repo_clean.py`, and **124 automated tests**.

---

## Legal & "Bring Your Own ISO" Policy

**This repository contains ZERO copyrighted Electronic Arts or Sony files.**
- No `.iso`, `.bin`, `.bun`, `.elf`, BIOS, audio banks, `.obj` meshes, or original game textures are hosted here.
- Automated CI (`tools/audit_repo_clean.py`) checks every commit so zero prohibited binaries, oversized files, or hardcoded personal paths enter Git.
- See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for our strict CC0 / SIL OFL / MIT policy.

---

## Quick Start (macOS Apple Silicon + PCSX2 2.6.3+)

### 1. Check Status, Profiles & Run Verification
```bash
python3 tools/rockport_cli.py status
python3 tools/rockport_cli.py profiles
python3 tools/rockport_cli.py verify
```

### 2. One-Command Build + PCSX2 4K Metal 60 FPS Install (`v0.3`)
```bash
python3 tools/rockport_cli.py build \
  --profile rockport2026_v0.3 \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --install-pcsx2 \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```
Or try `--profile rockport2026_golden_hour` for Forza Horizon-inspired sunset lighting, `--profile rockport2026_ultrawide_21_9`, `--profile rockport2026_purist_hd`, or `--profile rockport2026_sandbox`.

### 3. Explore Lighting, VLT, Audio, Geometry & 4K Textures
```bash
# Inspect PS2 SunInfos, LightMaterials, and GLOBALMEMORYFILE.BIN
python3 tools/lighting_ps2.py inspect "/path/to/iso" --sun MIDDAY --material ROAD

# Extract all 916 PS2 TPK textures read-only into build/tpk_extracted/
python3 tools/tpk_extract.py extract "/path/to/iso" --all --out build/tpk_extracted

# Inspect PS2 ABKC/Gnsu/SCHl audio and 0x80134000 car geometry
python3 tools/audio_inspect.py summary "/path/to/iso"
python3 tools/geometry_inspect.py inspect "/path/to/iso" 'CARS\BMWM3GTRE46\GEOMETRY.BIN'

# Dump textures in PCSX2, auto-match to 4K CC0 + HUD pack, and encode BC7 DDS
python3 tools/rockport_cli.py dump-mode on
python3 tools/match_texture_dumps.py match --dumps ~/Library/Application\ Support/PCSX2/textures/SLUS-21351/dumps
```

---

## Repository Structure

```text
nfsmw-ps2-remaster/
├── .github/workflows/ci.yml                       # Automated Python 3.11/3.12 CI & hygiene checks
├── assets_cc0/
│   ├── manifest.example.json                      # Verified CC0 1.0 Poly Haven & ambientCG manifest
│   └── material_pack_rules.json                   # Perceptual matching rules for 4K CC0 & HUD packs
├── docs/
│   ├── ROADMAP.md                                 # Multi-phase vision & PS2 vs 2026 engineering matrix
│   └── research/
│       ├── PS2_ARCHITECTURE.md                    # SLUS-21351 ISO, ZDIR, VPAK & ELF ground truth
│       ├── PCSX2_MACOS_REMASTER.md                # PCSX2 2.6.3 Metal, BC7 DDS & shader pipeline
│       ├── OPEN_SOURCE_ASSETS_AND_PIPELINE.md     # CC0 sources, Apple Silicon MPS AI, BYO-ISO model
│       ├── MIPS_REVERSE_ENGINEERING.md            # SLUS_213.51 disassembly proofs & 60 FPS analysis
│       ├── VLT_PS2_SCHEMA.md                      # AttribSys VPAK layout, MemoryFile sync & curves
│       ├── PS2_LIGHTING_SUNINFOS.md               # GLOBALB.LZC SunInfos, LightMaterials & MemoryFile
│       └── PS2_AUDIO_AND_GEOMETRY.md              # ABKC/Gnsu/SCHl audio & 0x80134000 VU1 geometry
├── patches/
│   ├── elf_patches.json                           # 19 binary-verified MIPS patches (38 writes)
│   ├── lighting/                                  # rockport2026_daylight & golden_hour recipes
│   ├── pnach/SLUS-21351_0518D274.pnach            # Complete PCSX2 v2 patch suite for SLUS-21351
│   ├── profiles/                                  # 6 validated preset build profiles
│   └── vlt/                                       # 5 verified zero-overlap VLT recipes (360 edits)
├── tests/                                         # 124 automated unit & live read-only ISO tests
├── tools/                                         # Full clean-room CLI suite + rockport_cli.py
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
