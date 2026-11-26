# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Native Metal 4 RT Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) into a full-island 120 FPS Hardware-Ray-Traced Apple Silicon experience AND a 4K/60 FPS PCSX2 remaster — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

**Quick Links:** [Player's Guide (`docs/PLAY_GUIDE.md`)](docs/PLAY_GUIDE.md) · [Live Progress Table (`docs/PROGRESS.md`)](docs/PROGRESS.md) · [Engineering Roadmap (`docs/ROADMAP.md`)](docs/ROADMAP.md)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** ships only visual toggles.

**NFSMW: Rockport 2026 (`v1.0.0`)** is a dual-track open-source remaster & native Apple Silicon ray-tracing suite combining:
- **Track 2 (`v1.0.0`, `9.3 / 10` Grand Gate PASS — `4K` `9.5–9.6 / 10`, `0 P0`, `0 P1` — Flagship)**: **Full-Island Native Apple Silicon Metal 4 Hardware-Ray-Traced Rockport City** — streaming all **718 Rockport sections** (`6.66 × 6.27 km`), **`4,309` real streetlamps/signals/storefronts**, **40 on-demand playable NFSMW cars**, **authentic VLT handling**, **decoded Ginsu PSOLA engine & siren audio**, **20 live AI traffic + Heat 1–5 police pursuits with RT lightbars**, **NOS flames**, **Speedbreaker**, and a **post-FI NFSMW HUD & rotating minimap** at **`105–128 FPS` (`1080p`)** via **`MTLFXTemporalDenoisedScaler` + `MTLFXFrameInterpolator`** in **`305.3 MB` VRAM**!
- **Track 1 (`v0.5.0`, `8.9 / 10` Live Video QA PASS — Frozen)**: Full-stack PS2 ISO + PCSX2 v2.6.3 4K/60fps Metal remastering (84-car VU1 fleet, planar wet road reflections, 293-edit Forza Ultra lighting, 4× detail-preserving world BC7 textures, PINE RAM autopilot, and 4-panel MP4 video QA).

---

## Verified `v1.0.0` Track 2 — Full-Island Hardware-Ray-Traced Rockport City

- **Full-Island BC7 Streaming World ([`tools/export_rockport_world.py`](tools/export_rockport_world.py), [`native_metal_rt/RTWorldLoader.swift`](native_metal_rt/RTWorldLoader.swift), [`native_metal_rt/RTWorldStreamer.swift`](native_metal_rt/RTWorldStreamer.swift))**:
  - Reads `TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN` strictly read-only (`'rb'`) and encodes all **`1,497`** city-wide diffuse textures to **BC7 + sRGB mipmaps in `1.23s`** via our multi-threaded pure-Rust batch encoder (`tools/dds_encoder_rs/`), emitting **`rockport_full.rkpt` (`209.24 MiB`, `.rkpt` v2)**:
    - **`718` sections** (`638` spatial + `80` horizon panorama) | **`10,213`** unique meshes | **`86,033`** instances | **`9,520`** Free-Roam event barriers filtered
    - **`WRoadNetwork` (`0x0003B800`)**: `4,385` nodes / `6,538` segments (`229.25 km` road centerline) + **`6` verified district teleport landmarks** (`Rosewood Blvd`, `Downtown East Park`, `Camden Beach`, `Highway 99`, `Rosewood College`, `Rockport Waterfront`)
    - **`4,309` real `RTWorldLight` emitters** (streetlamps, traffic signals, iron lamps, elevated storefront clusters) indexed in a **`128×128×8` XZ light grid**
    - Runtime XZ AABB hysteresis streaming (`350m`/`430m` + dynamic `6,000`-instance clamp + `>250m` horizon panorama ring) bound via **`MTLResidencySet`** in **`~305 MB` GPU world+AI VRAM**.
- **Metal 4 / MSL 4.0 Path Tracing + `103–128 FPS` MetalFX 2× Frame Generation ([`native_metal_rt/`](native_metal_rt/))**:
  - **`MTLFXTemporalDenoisedScaler`** (`UBFNet/dBFNet` fused-MXU) + standalone post-ACES, pre-HUD **`MTLFXFrameInterpolator`** delivers **`58–68 FPS` native** and **`103–128 FPS` Frame-Gen @ `1920×1080`** with 20 live AI/cops and **100% unwarped post-FI HUD** (`RTHud.metal`).
  - Dynamic **24h Day/Night solar/lunar ephemeris**, live **rain & concentric puddle ripple buildup**, **analytical volumetric headlight cones**, **botanical Italian cypresses**, **poured-concrete beveled sidewalks**, and **4K (`3840×2160`) 48-SPP showcase rendering**.
- **40-Car On-Demand Playable Garage ([`tools/extract_rt_car.py`](tools/extract_rt_car.py))**:
  - Extracts any of all **40 playable NFSMW 2005 cars** on demand in `~1–2s` (`G`/`Shift+G` live hot-swap or `--car <NAME>`) with `bstringhash` 23-char headlight/taillight resolution, seam-locked adaptive PN subdivision, and auto-fitted wheel wells.
- **Authentic VLT Physics, Ginsu PSOLA Audio & Heat 1–5 Police Pursuits ([`tools/export_gameplay_pack.py`](tools/export_gameplay_pack.py), [`native_metal_rt/RTPhysics.swift`](native_metal_rt/RTPhysics.swift), [`native_metal_rt/RTAudioEngine.swift`](native_metal_rt/RTAudioEngine.swift), [`native_metal_rt/RTGameWorld.swift`](native_metal_rt/RTGameWorld.swift))**:
  - **48 VLT vehicle specs** (`ATTRIBUTES.BIN`), 6-speed auto gearbox, RWD/AWD `TORQUE_SPLIT`, e-brake drift, 4-wheel GPU BVH ground/wall probes, **NOS dual Mach-diamond exhaust flames**, **0.35× burnt-amber Speedbreaker**, and native **PS5 DualSense / Xbox** `GameController` support.
  - Lock-free **`AVAudioEngine` Ginsu PSOLA engine synth** (`9` EA-XAS v0 `Gnsu v20` accel/decel banks incl. BMW M3 GTR straight-cut gearbox whine) + `20` Sony SPU-ADPCM SFX + **3D stereo Doppler police sirens**.
  - **20 live AI traffic + Heat 1–5 Rockport PD cruisers** (`COPMIDSIZE` → `COPGTO` → `COPSPORT`) on 7 deduplicated RT BLASes (`RTAIVehicleLoader.swift`), hill-hugging `aiRoadProbeKernel`, flashing **red/blue RT roof lightbars**, and live rotating GPU minimap + Pursuit Heat/Bounty HUD.

---

## Verified `v0.5.0` Track 1 — PS2 ISO + PCSX2 v2.6.3 Remaster (`8.9 / 10`, Frozen)

Every PS2 release candidate was captured as a 60 FPS H.264 MP4 + 4K F8 snapshots (`2992×1683`) driven by our **20 Hz PINE IPC RAM-telemetry autopilot**, then audited independently across 4 specialist QA axes ([`docs/research/VIDEO_QA_RUBRIC.md`](docs/research/VIDEO_QA_RUBRIC.md)):

| Adversarial QA Axis | Verified `v0.5.0` Score | Key Verified Proof (`iter_17`, 68.05s @ 59.95 fps) |
| :--- | :---: | :--- |
| **QA-1: UI, Logos, Loading Icons & HUD** | **`9.2 / 10`** (`0 P0`, `0 P1`) | `0` texture leaks across all 170 BC7 DDS; true circular 16:9 gauges; verified live NOS depletion, `▲ R` reverse gear, and `104 MPH` 3-digit readout |
| **QA-2: 3D Car Geometry, VU1 & Liveries** | **`9.1 / 10`** (`0 P0`, `0 P1`) | Wheel-safe 84-car 2.83M-tri VU1 fleet; intact Golf GTI aftermarket 15-spoke wheels, red calipers, white roof scoop, blue NOS flame, BMW M3 GTR & 8+ live traffic classes |
| **QA-3: World Lighting, Wet Reflections & 4× World** | **`8.6 / 10`** (`0 P0`, `0 P1`) | `293`-edit `forza_ultra` lighting + planar wet road reflections + `2.67×` reflection scenery; `+20%` corridor luma lift (`70.88`), `9.5×` lower shadow crush (`0.48%`), `0` highlight-clip spikes, `170` 4× world BC7 DDS |
| **QA-4: PINE Autopilot, Reverse, NOS & Video Health** | **`8.6 / 10`** (`0 P0`, `0 P1`) | `808.11 m` open-boulevard drive, `105.1 mph` peak, `647/647` live RPM/Gear/NOS ticks (`0.389 ms` read), `64` NOS-engaged ticks, clean `17.8 mph` `R` reverse, `4` unique traffic dodges, `0` health flags |
| **Overall Release Gate (`>= 8.5 / 10`, `0 P0`, `0 P1`)** | **`8.9 / 10` — `RELEASE PASS`** | All 4 independent axes clear `>= 8.5 / 10` with `0 P0` blockers and `0 P1` majors |

---

## Legal & "Bring Your Own ISO" Policy

**This repository contains ZERO copyrighted Electronic Arts or Sony files.**
- No `.iso`, `.bin`, `.bun`, `.elf`, `.rkpt`, `.wav`, `.pcm`, `.obj`, BIOS, audio banks, or original game textures are hosted here.
- Automated CI (`tools/audit_repo_clean.py`) checks every commit so zero prohibited binaries, oversized files, or hardcoded personal paths enter Git.
- See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for our strict CC0 / SIL OFL / MIT policy.

---

## Quick Start (macOS Apple Silicon)

### 1. Play Full-Island Native Metal 4 RT Rockport (`v1.0.0` — 2 Commands!)
```bash
export NFSMW_ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
make export-rockport-full && make play-rockport
```
- **Jump straight into a Heat 3 police pursuit**: `make play-pursuit`
- **Run the 6-district 100+ FPS benchmark**: `make bench-rockport`
- **Full controls, PS5/Xbox gamepad map, 40-car list & CLI flags**: see [**`docs/PLAY_GUIDE.md`**](docs/PLAY_GUIDE.md).

### 2. Run Full Clean-Room Verification & Tests
```bash
python3 tools/rockport_cli.py verify --include-untracked
CARGO_TARGET_DIR=build/rust_target cargo test --manifest-path tools/dds_encoder_rs/Cargo.toml
```

### 3. One-Command PS2 ISO Build + PCSX2 4K Metal 60 FPS Install (`v0.5.0` Track 1)
```bash
python3 tools/rockport_cli.py build \
  --profile rockport2026_v0.5 \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --install-pcsx2 \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```
*(Or `make build-v0.5 ISO="/path/to/iso"`.)*

---

## Repository Structure

```text
nfsmw-ps2-remaster/
├── .github/workflows/ci.yml                       # Automated Python CI, Rust tests & hygiene audit
├── assets_cc0/                                    # 100% CC0/OFL audio, livery, & material rules
├── docs/
│   ├── PLAY_GUIDE.md                              # Complete player guide: quickstart, controls, 40 cars, tips
│   ├── PROGRESS.md                                # Live compact milestone progress table (v0.5 – v1.4)
│   ├── ROADMAP.md                                 # Engineering roadmap & completed v0.1–v1.0 milestones
│   └── research/                                  # Clean-room engineering specs & VIDEO_QA_RUBRIC.md
├── native_metal_rt/                               # Native Metal 4 / MSL 4.0 RT Rockport City engine
│   ├── RayTracing.metal & RTHud.metal             # Hardware RT, 4,309-light grid, NOS/SB VFX & post-FI HUD
│   ├── RTRenderer.swift & RTSceneBuilder.swift    # MetalFX TDS + 2x Frame-Gen + 40-car catalog
│   ├── RTWorldLoader.swift & RTWorldStreamer.swift# .rkpt v2 BC7 loader, MTLResidencySet & XZ streaming
│   ├── RTPhysics.swift & RTAudioEngine.swift      # VLT vehicle dynamics, GameController & Ginsu PSOLA synth
│   └── RTGameWorld.swift & RTAIVehicleLoader.swift# 20 live AI traffic + Heat 1-5 pursuits + 7 AI RT BLASes
├── patches/                                       # ELF caves, 21 MIPS patches, lighting, .pnach, profiles, VLT
├── shaders/                                       # rockport2026_ssgi.slang & rockport2026_aces_grade.glsl
├── src/ps2_mod/                                   # Reproducible ps2dev/ps2sdk C plugin blueprint
├── tests/                                         # Automated offline & live unit/integration test suites
├── tools/                                         # Pure-Python CLI, .rkpt/car/gameplay exporters + Rust BC7
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
