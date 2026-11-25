# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Modding Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) for modern 4K/5K hardware on PCSX2 & Apple Silicon — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** ships only visual toggles.

**NFSMW: Rockport 2026 (`v0.4.0`)** is a dual-track open-source remaster & modding suite spanning BOTH **maximum-ceiling PS2 / PCSX2 hardware mods** AND **native Apple Silicon Metal PBR + Metal 4 Hardware Ray Tracing**:

### Track 1 — PS2 ISO + PCSX2 v2.6.3 Max-Ceiling Toolchain

1. **One-Command Modded ISO Builder & 7 Curated Presets (`tools/rockport_cli.py`, `tools/build_modded_iso.py`)**
   - Strictly reads your clean `SLUS-21351` ISO read-only (`'rb'`) and clones a separate bootable ISO via APFS copy-on-write with SHA-256 verification before & after.
   - Dual-syncs `GLOBAL\GLOBALMEMORYFILE.BIN` Entry `[1]` (`ATTRIBUTES.BIN`) **and** Entry `[2]` (`GLOBALB.LZC` JDLZ v2), updates duplicated `ZDIR.BIN` rows, recomputes EA `bCalculateCrc32` checksums (`1,294 / 1,294` retail entries verified), and supports `ZZDATA3.BIN` append + ISO9660/PVD sizing.
   - **7 Validated Build Profiles (`patches/profiles/`):**
     - `rockport2026_v0.4` *(Flagship)* — Everything in `v0.3` plus optional local Wave-3 ZZDATA upgrades (14,560-poly PN-subdivided BMW M3 GTR `GEOMETRY.BIN` & tuned P60B40 V8 `Gnsu` synth when generated in `build/`)
     - `rockport2026_v0.3` — 7 ELF fixes + 5 VLT recipes (`360` edits across 39 cars, cameras, traffic, pursuits, `visuallook`) + `rockport2026_daylight` (`37` `SunInfos` & `LightMaterials` edits)
     - `rockport2026_golden_hour` — Full overhaul with Forza-inspired Golden Hour sunset lighting (`52` edits)
     - `rockport2026_purist_hd` — Crisp 16:9, visual/LOD/reflection/60Hz EFX fixes preserving 100% stock handling & lighting
     - `rockport2026_ultrawide_21_9` — True 21:9 cinema aspect ratio + widened frustum culling + PCSX2 `AspectRatio = Stretch`
     - `rockport2026_sandbox` — Instant unlocks + skip intros + no AI rubber-banding + 4 VLT recipes
     - `rockport2026_v0.2` — Baseline v0.2 profile
2. **PS2 Car Geometry Compiler & PN/Loop Mesh Smoother (`tools/geometry_compiler.py`, `tools/subdivide_car_mesh.py`)**
   - Exact inverse of `tools/geometry_inspect.py` with **GF(2) VU1 `RINIT`/`RNEXT` 23-bit LFSR seed solver**, `0x00134A00`–`0x00134A02` VIF batch packing, and crease-aware PN-triangle subdivision (upgrades `BMWM3GTRE46_KIT00_BODY_B` from `3,640` → `14,560` smooth tris with `0` inspect errors).
3. **SPU-ADPCM / `Gnsu` / `ABKC` Audio Modder & CC0 Synth (`tools/audio_patcher.py`, `assets_cc0/audio_manifest.json`)**
   - Pure-Python Sony PS2 SPU-ADPCM encoder/decoder, `Gnsu` v20 hero engine curve tuning (`bmw_m3_gtr`, `carrera_gt`, `gallardo`, `camaro_ss`), `ABKC`/`BNKl` sample slot replacer, and 100% original CC0 UI/turbo/gearbox WAV synthesizer.
4. **MIPS R5900 Code Caves & `ps2sdk` C Plugin Template (`tools/mips_cave_builder.py`, `patches/elf_caves.json`, `src/ps2_mod/`)**
   - Clean-room two-pass MIPS R5900 mini-assembler, verified zero-overlap cave trampolines (`SLUS_213.51`), `.pnach`/ELF emitter, and reproducible `ps2dev/ps2sdk` C framework.
5. **19 Binary-Verified MIPS Engine Patches (`patches/elf_patches.json` + `tools/elf_disasm.py`)**
   - Native 16:9 / 21:9 / FOV, Brown Filter removal, bloom-preserving Motion Blur fix, High-LOD Cars & Wheels, 5-Face Car Reflections, ~3× Scenery Draw Distance, and `UpdateAllScreenEFX` (`1/30f → 1/60f`) true 60 FPS pacing (`180%+` EE Overclock).
6. **PS2 Lighting (`GLOBALB.LZC`), VLT Engine (360 Edits), & Native `arm64` Rust BC7 DDS Pack**
   - `tools/lighting_ps2.py`, `tools/vlt_ps2.py`, `tools/tpk_extract.py` (**916** decoded textures), `tools/bake_pbr_to_diffuse.py`, `tools/generate_hud_pack.py`, `tools/match_texture_dumps.py`, and zero-dep native Rust BC7 DDS + mipmap compiler (`tools/dds_encoder_rs/` + `tools/build_4k_texture_pack.py`).
7. **Live Sandboxed PCSX2 Boot Verifier & Post-FX Shaders (`tools/pcsx2_live_verify.py`, `shaders/`)**
   - Boots your modded ISO inside PCSX2 v2.6.3 Metal, verifies ELF CRC, per-game `.ini` (`CASMode`, `fxaa`, `ShadeBoost`), active `.pnach`, and BIOS/FMV boot frames, captures live screenshots cleanly, plus ships `shaders/rockport2026_ssgi.slang` & `rockport2026_aces_grade.glsl` for upcoming `librashader` Metal support.

### Track 2 — Native Apple Silicon Metal 2026 PBR, Metal 4 Hardware Ray Tracing & Decomp Bridge

8. **Native Apple Silicon Metal 2026 PBR Renderer (`native_metal/`, `tools/export_pbr_scene.py`)**
   - Pure Swift + Metal (`RockportMetalPBR`): Cook-Torrance GGX, dual-lobe metallic clearcoat automotive paint + micro-flake sparkle, procedural sky-dome IBL + Rockport skyline reflections, CC0 wet-asphalt puddle Fresnel + soft contact shadows, `RGBA16Float` HDR bloom + ACES Filmic tone mapping, 120Hz interactive orbit window & headless 1080p PNG mode.
9. **Apple M5 Pro Metal 4 Hardware Ray Tracing Engine (`native_metal_rt/`)**
   - Pure Swift + Metal RT (`RockportMetalRT`) building GPU BLAS (`MTLPrimitiveAccelerationStructure`) + TLAS (`MTLInstanceAccelerationStructure`) and tracing rays via `metal::raytracing::intersector<triangle_data, instancing>`: soft area sun shadows, ray-traced wet-asphalt & clearcoat specular reflections (RTR), RTAO + 1-bounce RTGI, progressive Roberts R2 accumulation, ACES bloom, interactive orbit/presets/RT toggle (`T`), and headless RT-Off vs RT-On split comparisons.
10. **Decomp & Native Recomp Bridge (`tools/decomp_bridge.py`, `docs/research/DECOMP_AND_RECOMP_ROADMAP.md`)**
    - Maps our `SLUS-21351` symbols, 360 VLT deltas, and 156 `LightMaterials` / `SunInfos` presets into C++20 & Metal headers (`Rockport2026Config.hpp`, `Rockport2026Lighting.h`, `bridge_manifest.json`) targeting `dbalatoni13/nfsmw` (CC0) and `SpeedBreakerProject/speedbreaker`.

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

### 2. One-Command Build + PCSX2 4K Metal 60 FPS Install (`v0.4`)
```bash
python3 tools/rockport_cli.py build \
  --profile rockport2026_v0.4 \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --install-pcsx2 \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```
Or try `--profile rockport2026_v0.3`, `--profile rockport2026_golden_hour` for Forza Horizon-inspired sunset lighting, `--profile rockport2026_ultrawide_21_9`, `--profile rockport2026_purist_hd`, or `--profile rockport2026_sandbox`.

### 3. Native Apple Silicon Metal PBR, Metal 4 Hardware Ray Tracing & Live Verification
```bash
# Launch native M5 Pro Metal 4 Hardware Ray Tracing showcase (extracted BMW M3 GTR)
python3 tools/rockport_cli.py metal-rt --preset golden_hour

# Or launch the interactive Metal 2026 PBR showcase / render headless 1080p PNG
python3 tools/rockport_cli.py metal-showcase --preset golden_hour

# Boot-verify your built ISO live in PCSX2 v2.6.3 Metal
python3 tools/rockport_cli.py live-verify --iso build/NFSMW_BlackEdition_Rockport2026_v0.4.iso

# Compile native arm64 Rust BC7 encoder + build mipmapped 2K/4K DDS pack
python3 tools/rockport_cli.py build-dds --size 2048

# Export C++20 & Metal headers for dbalatoni13/nfsmw decomp & SpeedBreaker
python3 tools/rockport_cli.py decomp-bridge --json
```

### 4. Explore Lighting, VLT, Audio, Geometry & 4K Textures
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
│   ├── audio_manifest.json                        # 100% CC0 procedural WAV cues & synth presets
│   ├── car_livery_rules.json                      # Procedural 4K motorsport & livery texture rules
│   ├── manifest.example.json                      # Verified CC0 1.0 Poly Haven & ambientCG manifest
│   └── material_pack_rules.json                   # Perceptual matching rules for 4K CC0 & HUD packs
├── docs/
│   ├── ROADMAP.md                                 # Multi-phase vision & PS2 vs 2026 engineering matrix
│   └── research/                                  # 15 clean-room engineering specs (PS2, Metal, RT, Decomp)
├── native_metal/                                  # Native Apple Silicon Swift + Metal 2026 PBR showcase
├── native_metal_rt/                               # M5 Pro Metal 4 Hardware Ray Tracing engine (BLAS/TLAS)
├── patches/
│   ├── elf_caves.json                             # Verified SLUS_213.51 MIPS R5900 code caves
│   ├── elf_patches.json                           # 19 binary-verified MIPS patches (38 writes)
│   ├── lighting/                                  # rockport2026_daylight & golden_hour recipes
│   ├── pnach/SLUS-21351_0518D274.pnach            # Complete PCSX2 v2 patch suite for SLUS-21351
│   ├── profiles/                                  # 7 validated preset build profiles (v0.2 - v0.4)
│   └── vlt/                                       # 5 verified zero-overlap VLT recipes (360 edits)
├── shaders/                                       # rockport2026_ssgi.slang & rockport2026_aces_grade.glsl
├── src/ps2_mod/                                   # Reproducible ps2dev/ps2sdk C plugin blueprint
├── tests/                                         # Automated unit & live read-only ISO tests
├── tools/                                         # Pure-Python CLI suite + tools/dds_encoder_rs (Rust BC7)
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
