# NFSMW: Rockport 2026 — Open-Source PS2 Remaster & Native Metal 4 RT Suite

> **Transforming *Need for Speed: Most Wanted – Black Edition* (2005, PS2 `SLUS-21351`) into a full-island 120 FPS Hardware-Ray-Traced Apple Silicon experience (`v1.4.0`, `9.5 / 10` Visual QA, `9.8 / 10` 4K Photo Bokeh) AND a 4K/60 FPS PCSX2 remaster (`v0.5.0`, `8.9 / 10`) — built 100% open-source, clean-room, and non-destructive.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

**Quick Links:** [Player's Guide (`docs/PLAY_GUIDE.md`)](docs/PLAY_GUIDE.md) · [Live Progress Table (`docs/PROGRESS.md`)](docs/PROGRESS.md) · [Engineering Roadmap (`docs/ROADMAP.md`)](docs/ROADMAP.md)

---

## Why This Exists

Almost all *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** version and fail on PlayStation 2 archives. Even PCSX2's official patch database for **Black Edition (`SLUS-21351`)** ships only visual toggles.

**NFSMW: Rockport 2026 (`v1.4.0` — All Wave 1 + Wave 2 Milestones DONE)** is a dual-track open-source remaster & native Apple Silicon ray-tracing suite combining:
- **Track 2 (`v1.4.0`, `9.5 / 10` Grand Finale PASS — `9.8 / 10` Peak 4K Photo Bokeh, `0 P0`, `0 P1` — Flagship)**: **Full-Island Native Apple Silicon Metal 4 Hardware-Ray-Traced Rockport City** — streaming all **718 Rockport sections** (`6.66 × 6.27 km`) with **EA PVS `2–4.8 km` vistas**, **`8,740` sculpted botanical trees/bushes**, **Gerstner animated harbor waves**, **`4,309` real streetlamps w/ 1-ray RIS shadows**, **40 on-demand playable cars**, **VLT handling**, **decoded Ginsu PSOLA engine & `MW_MUSIC.MUS` EA TRAX soundtrack + tunnel reverb**, **20 live AI traffic + Heat 1–5 police pursuits**, **15 Blacklist Boss Duels / Sprints / Speedtraps & O(1) `6,538`-road World Map**, **GT7 4K RT Photo Mode**, **drift sparks/smoke/skidmarks**, and **5 driving cameras incl. GT3 cockpit** at **`101–124 FPS` (`1080p`, 6-district Frame-Gen)** in **`306.0 MB` VRAM**!
- **Track 1 (`v0.5.0`, `8.9 / 10` Live Video QA PASS — Frozen)**: Full-stack PS2 ISO + PCSX2 v2.6.3 4K/60fps Metal remastering (84-car VU1 fleet, planar wet road reflections, 293-edit Forza Ultra lighting, 4× detail-preserving world BC7 textures, PINE RAM autopilot, and 4-panel MP4 video QA).

---

## Verified `v1.4.0` Track 2 — Full-Island Visual Ultra & Playable NFSMW Vertical Slice

- **Full-Island BC7 Streaming + EA PVS `4.8 km` Vistas + `8,740` Botanical Foliage ([`tools/export_rockport_world.py`](tools/export_rockport_world.py), [`native_metal_rt/RTWorldLoader.swift`](native_metal_rt/RTWorldLoader.swift), [`native_metal_rt/RTWorldStreamer.swift`](native_metal_rt/RTWorldStreamer.swift))**:
  - Reads `TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN` strictly read-only (`'rb'`) and encodes city-wide diffuse textures to **BC7 + sRGB mipmaps** via our multi-threaded pure-Rust batch encoder (`tools/dds_encoder_rs/`), emitting **`rockport_full.rkpt` (`.rkpt` v2 + PVS)**:
    - **`718` sections** (`638` spatial + `80` horizon panorama) | **`435` EA PVS visibility zones** (`0x80034150`) delivering **`2–4.8 km`** highway & cross-bay vistas (`J` toggle) | **`8,740`** all-district sculpted botanical trees, bushes & coastal beachgrass | **`9,520`** Free-Roam event barriers filtered
    - **`WRoadNetwork` (`0x0003B800`)**: `4,385` nodes / `6,538` segments (`229.25 km` road centerline) + **`6` verified district teleport landmarks** (`Rosewood Blvd`, `Downtown East Park`, `Camden Beach`, `Highway 99`, `Rosewood College`, `Rockport Waterfront`)
    - **`4,309` real `RTWorldLight` emitters** indexed in a **`128×128×8` XZ light grid** with **1-ray RIS real streetlamp shadows** + **6-wave directional Gerstner animated harbor waves** with shoreline foam & Beer-Lambert absorption
    - Runtime XZ AABB hysteresis + PVS union streaming bound via **`MTLResidencySet`** in **`306.0 MB` GPU world+AI VRAM**.
- **Metal 4 / MSL 4.0 Path Tracing, `101–124 FPS` Frame-Gen & `9.8 / 10` GT7 Photo Mode ([`native_metal_rt/`](native_metal_rt/))**:
  - **`MTLFXTemporalDenoisedScaler`** (`UBFNet/dBFNet` fused-MXU) + standalone post-ACES, pre-HUD **`MTLFXFrameInterpolator`** delivers **`101–124 FPS` Frame-Gen @ `1920×1080`** across all 6 districts with **100% unwarped post-FI HUD** (`RTHud.metal`).
  - **GT7 6-DoF Photo Mode (`O`)**: physically authentic 7-blade hardware-RT thin-lens DoF bokeh (`24–135mm`, `f/1.4–f/16`) and zero-extra-VRAM `2×2` tiled **4K 64-SPP** snapshots (`9.8 / 10`), **analytical RT drift skidmarks, ballistic sparks & tire smoke** (`RTVFXSystem.swift`), and a **5-camera rig (`C`)** including real-RT Hood Cam and post-FI GT3 carbon steering wheel + 10-LED shift-bar Cockpit Cam.
- **40-Car On-Demand Playable Garage & Top-15 Flagship QA ([`tools/extract_rt_car.py`](tools/extract_rt_car.py))**:
  - Extracts any of all **40 playable NFSMW 2005 cars** on demand (`G`/`Shift+G` live hot-swap, `--car <NAME>`, or `--top15` batch) with `bstringhash` 23-char headlight/taillight resolution, seam-locked adaptive PN subdivision, and `15/15` verified flagship icons (`9.5 / 10` 4K contact sheet).
- **VLT Physics, Ginsu Audio, `MW_MUSIC.MUS` EA TRAX, 20 Live AI/Cops & Blacklist Boss Duels ([`tools/export_gameplay_pack.py`](tools/export_gameplay_pack.py), [`tools/export_music_pack.py`](tools/export_music_pack.py), [`tools/export_events_pack.py`](tools/export_events_pack.py))**:
  - **48 VLT vehicle specs** (`ATTRIBUTES.BIN`), NOS dual Mach-diamond flames, `0.35×` burnt-amber Speedbreaker, native **PS5 DualSense / Xbox** `GameController`, lock-free **EA-XAS v0 Ginsu PSOLA** engine synths (incl. BMW M3 GTR straight-cut gearbox whine) + `20` SPU-ADPCM SFX + 3D Doppler sirens.
  - **EA TRAX Soundtrack (`v1.1.0`)**: all **26 licensed EA TRAX + 14 pursuit score tracks** decoded from `MW_MUSIC.MUS`/`MW_MUSIC.MPF` (`[`/`]`/`U`), sliding `[EA TRAX]` OLED banner, and live sky-ray **`AVAudioUnitReverb`** tunnel acoustics (`[REVERB]`).
  - **Events & World Map (`v1.2.0`, `9.5 / 10`)**: **15 Blacklist Boss Duels** (`Sonny` #15 → `Razor` #1), **18 Checkpoint Sprints**, **10 Speedtraps**, **20 live AI traffic + Heat 1–5 police** with flashing red/blue RT roof lightbars, and an O(1) pre-baked `1024×1024` **`6,538`-road interactive World Map (`M`)**.

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
- No `.iso`, `.bin`, `.bun`, `.elf`, `.rkpt`, `.m4a`, `.aac`, `.caf`, `.mp3`, `.wav`, `.pcm`, `.obj`, BIOS, audio banks, or original game textures are hosted here.
- Automated CI (`tools/audit_repo_clean.py`) checks every commit so zero prohibited binaries, oversized files, or hardcoded personal paths enter Git.
- See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for our strict CC0 / SIL OFL / MIT policy.

---

## Quick Start (macOS Apple Silicon)

### 1. Play Full-Island Native Metal 4 RT Rockport (`v1.4.0` — 2 Commands!)
```bash
export NFSMW_ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
make export-rockport-full && make play-rockport
```
- **Jump straight into a Heat 3 police pursuit**: `make play-pursuit`
- **Run the 6-district 100+ FPS benchmark**: `make bench-rockport`
- **Full controls (Photo Mode `O`, World Map `M`, Events `E`, EA TRAX `[`/`]`/`U`, PVS `J`, PS5/Xbox gamepad, 40 cars & CLI flags)**: see [**`docs/PLAY_GUIDE.md`**](docs/PLAY_GUIDE.md).

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
│   ├── PLAY_GUIDE.md                              # Complete v1.4.0 player guide: quickstart, controls, 40 cars
│   ├── PROGRESS.md                                # Live compact milestone progress table (v0.5 – v1.4 DONE)
│   ├── ROADMAP.md                                 # Completed v0.1–v1.4.0 engineering roadmap & QA scores
│   └── research/                                  # Clean-room engineering specs & VIDEO_QA_RUBRIC.md
├── native_metal_rt/                               # Native Metal 4 / MSL 4.0 RT Rockport City engine
│   ├── RayTracing.metal & RTHud.metal             # Hardware RT, RIS shadows, Gerstner waves, bokeh & post-FI HUD
│   ├── RTRenderer.swift & RTSceneBuilder.swift    # MetalFX TDS + 2x Frame-Gen + 40-car catalog
│   ├── RTWorldLoader.swift & RTWorldStreamer.swift# .rkpt v2 BC7 + EA PVS 4.8km vistas + XZ streaming
│   ├── RTPhysics.swift & RTAudioEngine.swift      # VLT physics, GameController, Ginsu PSOLA, EA TRAX & reverb
│   ├── RTGameWorld.swift & RTAIVehicleLoader.swift# 20 live AI traffic + Heat 1-5 pursuits + 7 AI RT BLASes
│   └── RTEventManager.swift & RTVFXSystem.swift   # 15 Blacklist Boss Duels, World Map, Photo Mode & Drift VFX
├── patches/                                       # ELF caves, 21 MIPS patches, lighting, .pnach, profiles, VLT
├── shaders/                                       # rockport2026_ssgi.slang & rockport2026_aces_grade.glsl
├── src/ps2_mod/                                   # Reproducible ps2dev/ps2sdk C plugin blueprint
├── tests/                                         # Automated offline & live unit/integration test suites
├── tools/                                         # Pure-Python CLI, world/car/gameplay/music/event exporters + Rust BC7
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
