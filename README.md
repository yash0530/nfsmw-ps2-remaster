# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Modding Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) for modern 4K/5K hardware on PCSX2 & Apple Silicon — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** ships only visual toggles.

**NFSMW: Rockport 2026 (`v0.5.0`)** is a dual-track open-source remaster & modding suite combining **live-QA-verified PS2 / PCSX2 v2.6.3 hardware remastering (`8.9 / 10`, `0 P0`, `0 P1`)** with a **native Apple Silicon Metal PBR & Hardware Ray Tracing renderer**:

### Verified `v0.5.0` Live 4-Panel Adversarial Video QA Scorecard (`iter_17`)

Every `v0.5.0` release candidate is captured end-to-end as a 60 FPS H.264 MP4 + 4K F8 snapshots (`2992×1683`) driven by our **20 Hz PINE IPC RAM-telemetry autopilot**, then audited independently across 4 specialist QA axes ([`docs/research/VIDEO_QA_RUBRIC.md`](docs/research/VIDEO_QA_RUBRIC.md)):

| Adversarial QA Axis | Verified `v0.5.0` Score | Key Verified Proof (`iter_17`, 68.05s @ 59.95 fps) |
| :--- | :---: | :--- |
| **QA-1: UI, Logos, Loading Icons & HUD** | **`9.2 / 10`** (`0 P0`, `0 P1`) | `0` texture leaks across all 170 BC7 DDS; true circular 16:9 gauges; verified live NOS depletion, `▲ R` reverse gear, and `104 MPH` 3-digit readout |
| **QA-2: 3D Car Geometry, VU1 & Liveries** | **`9.1 / 10`** (`0 P0`, `0 P1`) | Wheel-safe 84-car 2.83M-tri VU1 fleet; intact Golf GTI aftermarket 15-spoke wheels, red calipers, white roof scoop, blue NOS flame, BMW M3 GTR & 8+ live traffic classes |
| **QA-3: World Lighting, Wet Reflections & 4× World** | **`8.6 / 10`** (`0 P0`, `0 P1`) | `293`-edit `forza_ultra` lighting + planar wet road reflections + `2.67×` reflection scenery; `+20%` corridor luma lift (`70.88`), `9.5×` lower shadow crush (`0.48%`), `0` highlight-clip spikes, `170` 4× world BC7 DDS |
| **QA-4: PINE Autopilot, Reverse, NOS & Video Health** | **`8.6 / 10`** (`0 P0`, `0 P1`) | `808.11 m` open-boulevard drive, `105.1 mph` peak, `647/647` live RPM/Gear/NOS ticks (`0.389 ms` read), `64` NOS-engaged ticks, clean `17.8 mph` `R` reverse, `4` unique traffic dodges, `0` health flags |
| **Overall Release Gate (`>= 8.5 / 10`, `0 P0`, `0 P1`)** | **`8.9 / 10` — `RELEASE PASS`** | All 4 independent axes clear `>= 8.5 / 10` with `0 P0` blockers and `0 P1` majors |

---

### Track 1 — PS2 ISO + PCSX2 v2.6.3 Max-Ceiling Remaster Toolchain

1. **One-Command Modded ISO Builder & 9 Curated Profiles (`tools/rockport_cli.py`, `tools/build_modded_iso.py`)**
   - Reads your clean `SLUS-21351` ISO strictly read-only (`'rb'`) and clones a separate bootable ISO via APFS copy-on-write with SHA-256 verification before & after.
   - Dual-syncs `GLOBAL\GLOBALMEMORYFILE.BIN` Entry `[1]` (`ATTRIBUTES.BIN`) **and** Entry `[2]` (`GLOBALB.LZC` JDLZ v2), updates duplicated `ZDIR.BIN` rows, recomputes EA `bCalculateCrc32` checksums (`1,294 / 1,294` retail entries verified), and appends to `ZZDATA3.BIN` with ISO9660/PVD sizing.
   - **Flagship & Preset Build Profiles (`patches/profiles/`):**
     - `rockport2026_v0.5` *(Flagship — `8.9/10` Verified)* — All **9** flagship ELF patches (including `always_wet_road_reflections` & `high_detail_reflection_scenery`), 5 VLT recipes (`forza_hero_camera`, `modern_handling_tuning`, `full_roster_handling`, `pursuit_and_traffic`, `forza_ultra_look`), **`293`-edit `rockport2026_forza_ultra` lighting**, plus optional Wave-5 ZZDATA overrides for the wheel-safe **84-car VU1 fleet** (`2.83M` tris) & **4 tuned `Gnsu` v20 engine synths**.
     - `rockport2026_v0.5_forza_autumn` — Alternate warm golden-hour autumn preset (`91` lighting edits).
     - `rockport2026_v0.4`, `rockport2026_v0.3`, `rockport2026_golden_hour`, `rockport2026_purist_hd`, `rockport2026_ultrawide_21_9`, `rockport2026_sandbox`, `rockport2026_v0.2`.
2. **Wheel-Safe 84-Car VU1 `.vutext` Geometry Compiler (`tools/geometry_compiler.py`, `tools/subdivide_car_mesh.py`, `tools/upgrade_car_fleet.py`)**
   - Exact inverse of `tools/geometry_inspect.py` with **GF(2) VU1 `RINIT`/`RNEXT` 23-bit LFSR seed solver**, exact VU1 AABB/strip verification (`151,729` strips, `0` bad tags/overshoots), and crease-aware PN-triangle subdivision across **84 cars / 968 solids (`708K → 2.83M` triangles)** while safely excluding `GLOBALMEMORYFILE`-cached `BRAKES`, `WHEELS`, `SPOILER*`, and `ROOF` parts.
3. **21 Binary-Verified MIPS Engine Patches (`patches/elf_patches.json`, `tools/elf_disasm.py`)**
   - Native 16:9 / 21:9 / FOV, Brown Filter removal, bloom-preserving Motion Blur fix, High-LOD Cars & Wheels, 5-Face Car Reflections, ~3× Scenery Draw Distance, `UpdateAllScreenEFX` (`1/30f → 1/60f`) true 60 FPS pacing, plus Wave-5 **`always_wet_road_reflections`** (planar wet asphalt reflections) and **`high_detail_reflection_scenery`** (`2.67×` reflection draw distance).
4. **Detail-Preserving 4× World Texture Enhancer & Native `arm64` Rust BC7 Encoder (`tools/enhance_world_dumps.py`, `tools/dds_encoder_rs/`)**
   - Enhances dumped PCSX2 world textures (`170` BC7 `DXGI_FORMAT_BC7_UNORM` DDS in `world_enhanced_v2`: Rosewood road strips up to `512×2048`/`2048×2048`, masonry, sidewalks, foliage) while **preserving exact PS2 GS `0x80` alpha**, protecting UI/HUD/fonts/signage/car liveries with `0` leaks, and keeping `DumpReplaceableTextures = false` by default in generated per-game INIs.
5. **PINE IPC Live RAM-Telemetry Autopilot & Native AVFoundation Video QA (`tools/pcsx2_pine_driver.py`, `tools/video_qa_pipeline.py`, `tools/race_flywheel.py`)**
   - Closed-loop 20 Hz PINE IPC reader/driver (`0.389 ms` mean latency, disassembly-verified `PVehicle` `IEngine 0x1B4` / `ITransmission 0x1BC` / `NOS_ENGAGED 0x180`, `WRoadNav` lane tracking, obstacle avoidance, NOS bursts, and open-road reverse) paired with native macOS AVFoundation H.264 MP4 extraction, Pillow media validation, contact sheets, and exposure/motion telemetry.
6. **SPU-ADPCM / `Gnsu` / `ABKC` Audio Modder, MIPS Code Caves & `ps2sdk` Template (`tools/audio_patcher.py`, `tools/mips_cave_builder.py`, `src/ps2_mod/`)**
   - Pure-Python Sony SPU-ADPCM codec, `Gnsu` v20 hero engine synth tuning, verified zero-overlap MIPS R5900 code caves, and `ps2dev/ps2sdk` C plugin scaffold.

### Track 2 — Native Apple Silicon Metal PBR, Hardware Ray Tracing & Decomp Bridge

7. **Native Apple Silicon Metal 3.1 Hardware Ray Tracing Garage (`native_metal_rt/`)**
   - Pure Swift + Metal 3.1 (`RockportMetalRT`) building GPU BLAS/TLAS acceleration structures (`metal::raytracing::intersector<triangle_data, instancing>`) with authentic UV-mapped 3-car extracted PS2 meshes (BMW M3 GTR, Carrera GT, Gallardo), clearcoat metallic paint, soft area shadows, RTR wet-asphalt reflections, and 1-bounce RTGI.
   - **Honest Current State (`v0.5.0`)**: Builds cleanly on **Metal 3.1** using **offline progressive 64-SPP accumulation** on a **synthetic Rockport viaduct** scene (not yet real streamed city geometry or real-time 60 FPS driving denoising). See **`v0.6.0` Frontier Pivot** in [`docs/ROADMAP.md`](docs/ROADMAP.md) for the next leap to real `STREAML2RA.BUN` Rockport City geometry + real-time temporal/SVGF or MetalFX denoising.
8. **Native Metal 2026 PBR Showcase & Decomp Bridge (`native_metal/`, `tools/decomp_bridge.py`)**
   - Real-time 120Hz Cook-Torrance GGX + dual-lobe clearcoat showcase (`RockportMetalPBR`) plus C++20/Metal header codegen targeting `dbalatoni13/nfsmw` (CC0) and `SpeedBreakerProject/speedbreaker`.

---

## Legal & "Bring Your Own ISO" Policy

**This repository contains ZERO copyrighted Electronic Arts or Sony files.**
- No `.iso`, `.bin`, `.bun`, `.elf`, BIOS, audio banks, `.obj` meshes, or original game textures are hosted here.
- Automated CI (`tools/audit_repo_clean.py`) checks every commit so zero prohibited binaries, oversized files, or hardcoded personal paths enter Git.
- See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for our strict CC0 / SIL OFL / MIT policy.

---

## Quick Start (macOS Apple Silicon + PCSX2 2.6.3+)

### 1. Check Status, Profiles & Run Full Verification
```bash
python3 tools/rockport_cli.py status
python3 tools/rockport_cli.py profiles
python3 tools/rockport_cli.py verify
```

### 2. One-Command Flagship `v0.5.0` Build + PCSX2 4K Metal 60 FPS Install
```bash
python3 tools/rockport_cli.py build \
  --profile rockport2026_v0.5 \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --install-pcsx2 \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```
*(Or simply `make build-v0.5 ISO="/path/to/iso"`. Alternate presets: `rockport2026_v0.5_forza_autumn`, `rockport2026_purist_hd`, `rockport2026_ultrawide_21_9`, `rockport2026_sandbox`.)*

### 3. Wave-5 World Texture Enhancement, PINE Autopilot & Video QA
```bash
# Build optional 84-car wheel-safe VU1 fleet + 4 Gnsu audio synths locally into build/geom_wave5_full/
python3 tools/rockport_cli.py upgrade-fleet --iso "/path/to/iso"

# Enhance live PCSX2 world dumps to 4x BC7 DDS (preserving exact PS2 GS 0x80 alpha)
python3 tools/rockport_cli.py enhance-world --scale 4 --max-size 2048

# Probe live PCSX2 PINE RAM telemetry (slot 28011) or run closed-loop autopilot
python3 tools/rockport_cli.py pine-drive probe --slot 28011 -- --json

# Extract Pillow-validated QA frames, timeline contact sheets & telemetry from a capture MP4
python3 tools/rockport_cli.py video-qa extract-qa -- path/to/session_video.mp4 --out-dir build/qa_out
```

### 4. Native Apple Silicon Metal Showcase, RT Garage & Decomp Bridge
```bash
# Launch native Metal 3.1 RT 3-car garage (offline 64-SPP accumulation on synthetic viaduct)
python3 tools/rockport_cli.py metal-rt --preset golden_hour

# Launch interactive 120Hz Metal PBR showcase
python3 tools/rockport_cli.py metal-showcase --preset golden_hour

# Export C++20 & Metal headers for dbalatoni13/nfsmw decomp & SpeedBreaker
python3 tools/rockport_cli.py decomp-bridge --json
```

---

## Repository Structure

```text
nfsmw-ps2-remaster/
├── .github/workflows/ci.yml                       # Automated Python 3.11/3.12 CI & hygiene checks
├── assets_cc0/                                    # 100% CC0/OFL audio, livery, & material rules
├── docs/
│   ├── ROADMAP.md                                 # Completed v0.1–v0.5 milestones & v0.6.0 Frontier Pivot
│   └── research/                                  # Clean-room engineering specs & VIDEO_QA_RUBRIC.md
├── native_metal/                                  # Native Apple Silicon Swift + Metal PBR showcase
├── native_metal_rt/                               # Native Metal 3.1 RT 3-car garage (BLAS/TLAS, 64-SPP)
├── patches/
│   ├── elf_caves.json                             # Verified SLUS_213.51 MIPS R5900 code caves
│   ├── elf_patches.json                           # 21 binary-verified MIPS patches (incl. wet reflections)
│   ├── lighting/                                  # rockport2026_forza_ultra (293 edits), gt7_daylight, autumn
│   ├── pnach/SLUS-21351_0518D274.pnach            # Complete PCSX2 v2 patch suite for SLUS-21351
│   ├── profiles/                                  # 9 validated preset build profiles (v0.2 – v0.5.0)
│   └── vlt/                                       # Verified zero-overlap VLT recipes (cameras, handling, look)
├── shaders/                                       # rockport2026_ssgi.slang & rockport2026_aces_grade.glsl
├── src/ps2_mod/                                   # Reproducible ps2dev/ps2sdk C plugin blueprint
├── tests/                                         # Automated unit & integration test suites
├── tools/                                         # Pure-Python CLI suite + tools/dds_encoder_rs (Rust BC7)
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
