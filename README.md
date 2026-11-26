# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Native Metal 4 RT Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) for modern 4K/5K hardware on PCSX2 & Apple Silicon — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** ships only visual toggles.

**NFSMW: Rockport 2026 (`v0.6.0`)** is a dual-track open-source remaster & modding suite combining:
- **Track 1 (`v0.5.0`, `8.9 / 10` Live Video QA PASS)**: Full-stack PS2 ISO + PCSX2 v2.6.3 4K/60fps Metal remastering (84-car VU1 fleet, planar wet road reflections, 293-edit Forza Ultra lighting, 4× detail-preserving world BC7 textures, PINE RAM autopilot, and 4-panel MP4 video QA).
- **Track 2 (`v0.6.0`, `8.7 / 10` Visual QA PASS)**: **Real Hardware-Ray-Traced Rockport City** on native Apple Silicon **Metal 4 / MSL 4.0** — streaming authentic Rosewood & Downtown PS2 city geometry (`TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN`) into hardware BVH + **`MTLFXTemporalDenoisedScaler` MXU neural denoising** at **`52.9–57.0 FPS` (`~17.7–18.9 ms/frame`) @ `1920×1080` live driving**!

---

## Verified `v0.6.0` Track 2 — Real Hardware-Ray-Traced Rockport City (`8.7 / 10`, `0 P0`)

- **Streaming Binary World Extractor ([`tools/export_rockport_world.py`](tools/export_rockport_world.py))**:
  - Reads `TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN` strictly read-only (`'rb'`) from the user's PS2 ISO and packs compact memory-mapped `.rkpt` v1 city archives into gitignored `build/rockport_world/`:
    - **`rosewood.rkpt`**: `96.59 MiB` | `25` streamed sections | `2,575` world instances | `339/339` authentic world textures resolved
    - **`downtown.rkpt`**: `109.59 MiB` | `28` streamed sections | `3,498` world instances | `345/345` authentic world textures resolved
  - Automatically filters **`855` Free-Roam-invisible neon chevron race barriers**, applies **`4×`/`8×` detail-preserving world texture enhancement**, and solves verified bidirectional boulevard spawns (`120m` forward / `40m` rear sightlines).
- **Native Apple Silicon Metal 4 / MSL 4.0 + Hardware MXU MetalFX Neural Denoiser ([`native_metal_rt/`](native_metal_rt/))**:
  - **`MTLFXTemporalDenoisedScaler`** (`[MetalFX] UBFNet/dBFNet fused-MXU path ACTIVE`) sustains **`52.9–57.0 FPS` (`~17.7–18.9 ms/frame`) at `1920×1080`** live Rosewood driving on Apple M5 Pro.
  - **8-ray GPU BVH road/wall driving physics**, **bindless `MTLHeap` + `MTLResidencySet`** texture residency, grazing-angle 4-tap anisotropic world sampling, perceptual sRGB ACES filmic grading, night-rain glowing storefronts/streetlamps/twin projector headlights/raindrop ripples, plus pure vulcanized tires and crystal-clear glass headlights on authentic UV-mapped PS2 hero cars (`m3_gtr`, `carrera_gt`, `gallardo`).
  - **Honest Visual QA Progression (`renders_v5`)**: **`5.9 → 6.9 → 8.2 → 8.7 / 10` (`0 P0`)** — clears the `>= 8.5 / 10` `v0.6.0` release gate (noting the minor M3 trunk plate cove surround & P2 polish backlog tracked for `v0.6.1`).

---

## Verified `v0.5.0` Track 1 — PS2 ISO + PCSX2 v2.6.3 Remaster (`8.9 / 10`, `0 P0`, `0 P1`)

Every PS2 release candidate is captured end-to-end as a 60 FPS H.264 MP4 + 4K F8 snapshots (`2992×1683`) driven by our **20 Hz PINE IPC RAM-telemetry autopilot**, then audited independently across 4 specialist QA axes ([`docs/research/VIDEO_QA_RUBRIC.md`](docs/research/VIDEO_QA_RUBRIC.md)):

| Adversarial QA Axis | Verified `v0.5.0` Score | Key Verified Proof (`iter_17`, 68.05s @ 59.95 fps) |
| :--- | :---: | :--- |
| **QA-1: UI, Logos, Loading Icons & HUD** | **`9.2 / 10`** (`0 P0`, `0 P1`) | `0` texture leaks across all 170 BC7 DDS; true circular 16:9 gauges; verified live NOS depletion, `▲ R` reverse gear, and `104 MPH` 3-digit readout |
| **QA-2: 3D Car Geometry, VU1 & Liveries** | **`9.1 / 10`** (`0 P0`, `0 P1`) | Wheel-safe 84-car 2.83M-tri VU1 fleet; intact Golf GTI aftermarket 15-spoke wheels, red calipers, white roof scoop, blue NOS flame, BMW M3 GTR & 8+ live traffic classes |
| **QA-3: World Lighting, Wet Reflections & 4× World** | **`8.6 / 10`** (`0 P0`, `0 P1`) | `293`-edit `forza_ultra` lighting + planar wet road reflections + `2.67×` reflection scenery; `+20%` corridor luma lift (`70.88`), `9.5×` lower shadow crush (`0.48%`), `0` highlight-clip spikes, `170` 4× world BC7 DDS |
| **QA-4: PINE Autopilot, Reverse, NOS & Video Health** | **`8.6 / 10`** (`0 P0`, `0 P1`) | `808.11 m` open-boulevard drive, `105.1 mph` peak, `647/647` live RPM/Gear/NOS ticks (`0.389 ms` read), `64` NOS-engaged ticks, clean `17.8 mph` `R` reverse, `4` unique traffic dodges, `0` health flags |
| **Overall Release Gate (`>= 8.5 / 10`, `0 P0`, `0 P1`)** | **`8.9 / 10` — `RELEASE PASS`** | All 4 independent axes clear `>= 8.5 / 10` with `0 P0` blockers and `0 P1` majors |

### Track 1 Core Capabilities
1. **One-Command Modded ISO Builder & 9 Curated Profiles (`tools/rockport_cli.py`, `tools/build_modded_iso.py`)**: APFS copy-on-write clone of untouched `SLUS-21351` ISO with SHA-256 verification before/after, dual-synced `GLOBALMEMORYFILE.BIN` (`ATTRIBUTES.BIN` + `GLOBALB.LZC` JDLZ v2), `ZDIR.BIN` duplicate sync + `bCalculateCrc32` (`1,294 / 1,294` verified), and `ZZDATA3.BIN` append + ISO9660/PVD sizing. Flagship profile `rockport2026_v0.5` bundles all 9 flagship ELF patches, 5 VLT recipes, `293`-edit `forza_ultra` lighting, and 88 optional Wave-5 overrides (`84` wheel-safe cars + `4` `Gnsu` synths).
2. **Wheel-Safe 84-Car VU1 `.vutext` Geometry Compiler (`tools/geometry_compiler.py`, `tools/upgrade_car_fleet.py`)**: GF(2) VU1 `RINIT`/`RNEXT` 23-bit LFSR seed solver, `151,729` verified strips (`0` bad tags/overshoots), `708K → 2.83M` triangles across 84 cars / 968 solids while excluding `GLOBALMEMORYFILE`-cached `BRAKES`, `WHEELS`, `SPOILER*`, and `ROOF`.
3. **21 Binary-Verified MIPS Engine Patches (`patches/elf_patches.json`)**: Native 16:9 / 21:9, Brown Filter & smear removal, High-LOD cars, 5-face reflections, ~3× scenery draw distance, `UpdateAllScreenEFX` 60 FPS pacing, plus **`always_wet_road_reflections`** & **`high_detail_reflection_scenery`** (`2.67×`).
4. **Detail-Preserving 4× World Texture Enhancer & Native `arm64` Rust BC7 Encoder (`tools/enhance_world_dumps.py`, `tools/dds_encoder_rs/`)**: Preserves exact PS2 GS `0x80` alpha, `0` UI/icon/sign/livery leaks, `DumpReplaceableTextures = false` by default in generated per-game INIs.
5. **PINE IPC Autopilot, Native AVFoundation MP4 Video QA & Decomp Bridge (`tools/pcsx2_pine_driver.py`, `tools/video_qa_pipeline.py`, `tools/decomp_bridge.py`)**: Live RAM telemetry (`0.389 ms`), automated MP4 QA sheets/metrics, plus C++20/Metal header codegen targeting `dbalatoni13/nfsmw` (CC0) & `SpeedBreakerProject/speedbreaker`.

---

## Legal & "Bring Your Own ISO" Policy

**This repository contains ZERO copyrighted Electronic Arts or Sony files.**
- No `.iso`, `.bin`, `.bun`, `.elf`, `.rkpt`, BIOS, audio banks, `.obj` meshes, or original game textures are hosted here.
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

### 2. Real Hardware-Ray-Traced Rockport City (`v0.6.0` — Native Metal 4 + MetalFX)
```bash
# Export authentic Rosewood & Downtown Rockport City (.rkpt v1) read-only from your clean PS2 ISO
python3 tools/rockport_cli.py export-world --district all \
  --iso "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --out build/rockport_world

# Launch interactive 1080p real-time Metal 4 RT + MetalFX MXU neural driving in Rosewood
python3 tools/rockport_cli.py metal-rt \
  --world build/rockport_world/rosewood.rkpt \
  --car m3_gtr --preset golden_hour --denoise metalfx --camera-mode chase --hud

# Or run a headless 30-frame 1080p driving benchmark
python3 tools/rockport_cli.py metal-rt --headless \
  --world build/rockport_world/rosewood.rkpt \
  --car m3_gtr --preset golden_hour --denoise metalfx --camera-mode chase \
  --drive-bench 30 --hud --out build/rockport_world/rosewood_drive_bench.png
```
*(Or simply `make export-world ISO="/path/to/iso"` followed by `make metal-rt-world`.)*

### 3. One-Command Flagship PS2 ISO Build + PCSX2 4K Metal 60 FPS Install (`v0.5.0`)
```bash
python3 tools/rockport_cli.py build \
  --profile rockport2026_v0.5 \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --install-pcsx2 \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```
*(Or `make build-v0.5 ISO="/path/to/iso"`. Optional Wave-5 extras: `python3 tools/rockport_cli.py upgrade-fleet --iso "/path/to/iso"` and `python3 tools/rockport_cli.py enhance-world --scale 4 --max-size 2048`.)*

### 4. PINE Telemetry Autopilot, Video QA & Decomp Bridge
```bash
# Probe live PCSX2 PINE RAM telemetry (slot 28011)
python3 tools/rockport_cli.py pine-drive probe --slot 28011 -- --json

# Extract Pillow-validated QA frames, timeline contact sheets & telemetry from a capture MP4
python3 tools/rockport_cli.py video-qa extract-qa -- path/to/session_video.mp4 --out-dir build/qa_out

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
│   ├── ROADMAP.md                                 # Completed v0.1–v0.6.0 milestones & v0.6.1/v0.7+ plan
│   └── research/                                  # Clean-room engineering specs & VIDEO_QA_RUBRIC.md
├── native_metal/                                  # Native Apple Silicon Swift + Metal PBR showcase
├── native_metal_rt/                               # Native Metal 4 / MSL 4.0 RT + MetalFX Rockport City engine
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
├── tools/                                         # Pure-Python CLI suite (incl. export_rockport_world.py) + Rust BC7
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
