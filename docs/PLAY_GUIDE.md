# NFSMW: Rockport 2026 — Player's Guide

Drive the entire **6.66 × 6.27 km Rockport City** island natively on Apple Silicon with **Metal 4 Hardware Ray Tracing**, **120 FPS MetalFX Frame Generation**, **40 playable NFSMW cars**, **authentic VLT handling**, **decoded Ginsu PSOLA engine audio**, and **20 live AI traffic & Heat 1–5 police pursuits**.

> Looking for milestone status? See the live table in [PROGRESS.md](PROGRESS.md).

---

## 1. Requirements & Legal BYO-ISO Setup

- **Hardware / OS**: Apple Silicon Mac with Metal hardware ray tracing (M3/M4/M5; Metal 4 + `MTLFXTemporalDenoisedScaler` + `MTLFXFrameInterpolator` supported on macOS 15+; benchmarked at `103–128 FPS @ 1080p` on Apple M5 Pro).
- **Toolchain**: Xcode Command Line Tools (`swiftc`), Python 3.9+ (`Pillow`, `numpy`), and Rust (`cargo`) for the multi-threaded BC7 texture batch encoder.
- **Legal Game ISO**: Your own legal copy of *Need for Speed: Most Wanted — Black Edition* (PS2, `SLUS-21351`). Point `NFSMW_ISO` at it — all tools open the ISO strictly read-only (`'rb'`) and write extracted assets exclusively into gitignored `build/`:

```bash
export NFSMW_ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
```

*(Zero copyrighted Electronic Arts or Sony assets are stored in this repository. See [ASSETS_LICENSE.md](../ASSETS_LICENSE.md).)*

---

## 2. Two-Command Quickstart

```bash
# 1. Export all 718 Rockport sections (.rkpt v2 BC7, ~209 MiB) + VLT physics, Ginsu audio & AI pack (~8.5 MiB)
make export-rockport-full

# 2. Build & launch native Metal 4 RT Rockport at 100–128 FPS (MetalFX + 2x Frame Generation)
make play-rockport
```

### Handy Make Shortcuts

*(Pass `ISO="/path/to/iso"` or `export NFSMW_ISO=...`, and pass any extra engine flags via `ROCKPORT_ARGS="..."` — e.g. `make play-rockport ROCKPORT_ARGS="--car GALLARDO --preset night_neon --rain 0.88"`.)*

| Command | What It Does |
| :--- | :--- |
| `make export-rockport-full` | Exports full-island `build/rockport_world/rockport_full.rkpt` (`718` secs, `.rkpt` v2 BC7) + `build/gameplay_pack/` (VLT stats, Ginsu audio, 7 AI vehicle archetypes) |
| `make build-rockport` | Compiles `native_metal_rt/` into `build/metal_rt_v06/RockportMetalRT` |
| `make play-rockport` | Ensures build & exports exist and launches full-island Rockport with `--frame-gen on --hud $(ROCKPORT_ARGS)` |
| `make play-pursuit` | Launches straight into an active **Heat 3 Rosewood Blvd police pursuit** (`--landmark "Rosewood Blvd" --pursuit 3 --frame-gen on --hud $(ROCKPORT_ARGS)`) |
| `make bench-rockport` | Builds & runs the automated 6-district full-island Frame-Gen & VRAM benchmark (`tests/test_multi_district_bench.py`) |

### Popular CLI Launch Flags (`build/metal_rt_v06/RockportMetalRT`)

```bash
# Night rain Heat 4 pursuit in a Lamborghini Gallardo at Downtown East Park
build/metal_rt_v06/RockportMetalRT \
  --world build/rockport_world/rockport_full.rkpt \
  --car GALLARDO --landmark "Downtown East Park" \
  --preset night_neon --rain 0.88 --pursuit 4 --frame-gen on --hud

# Golden hour Ford GT coastal cruise at Camden Beach
build/metal_rt_v06/RockportMetalRT \
  --world build/rockport_world/rockport_full.rkpt \
  --car FORDGT --landmark 2 --time-of-day 17.5 --frame-gen on --hud
```

| Flag | Values / Default | Description |
| :--- | :--- | :--- |
| `--world <path>` | `build/rockport_world/rockport_full.rkpt` | Full-island `.rkpt` v2 (or pilot v1 `rosewood.rkpt` / `downtown.rkpt`) |
| `--car <NAME>` | `m3_gtr` (or any of 40 ISO car names) | Select starting car (auto-extracts on demand from `$NFSMW_ISO` into `build/car_cache/`) |
| `--landmark <0..5\|name>` | `0` (`Rosewood Blvd`) | Spawn directly at any of the 6 verified Rockport districts |
| `--preset <key>` | `golden_hour` \| `night_neon` \| `daylight` | Atmospheric lighting & wet-road preset |
| `--time-of-day <0..24>` | Preset default | Pin exact solar/lunar hour (`6.2` dawn, `13.5` noon, `17.5` golden hour, `22.5` night) |
| `--rain <0..1>` | Preset default | Override live rain intensity & concentric puddle ripple buildup |
| `--pursuit [1..5]` | Off (press `F` live) | Start immediately in an active Heat 1–5 Rockport PD chase (`--heat <1..5>` also accepted) |
| `--nos` / `--speedbreaker` | Off | Engage NOS boost / 0.35× amber bullet-time on launch (great for benchmarks & captures) |
| `--frame-gen <on\|off>` | `off` (`on` via `make play-rockport`) | Enable standalone post-ACES, pre-HUD 2× `MTLFXFrameInterpolator` (zero HUD warp) |
| `--denoise <mode>` | `metalfx` \| `svgf` \| `off` | Hardware MXU `MTLFXTemporalDenoisedScaler` (default), À-Trous SVGF, or raw RT |
| `--camera-mode <mode>` | `chase` \| `hero` \| `street` \| `overhead` | Starting camera preset |
| `--hud` / `--no-hud` | Off in static, On in drive bench | Toggle NFSMW tachometer, NOS/Speedbreaker bars, Pursuit Heat & rotating GPU minimap |
| `--no-ai` / `--no-audio` | Enabled by default | Disable AI traffic/cops or live `AVAudioEngine` audio for isolated graphics testing |

---

## 3. Controls (Keyboard, Mouse & Gamepad)

Both **PS5 DualSense** and **Xbox Wireless** controllers work out of the box via Apple `GameController` alongside keyboard & mouse.

| Action | Keyboard / Mouse | PS5 DualSense / Xbox Controller |
| :--- | :--- | :--- |
| **Accelerate** | `W` or `Up Arrow` | `R2` / `RT` (analog) |
| **Brake / Reverse** | `S` or `Down Arrow` | `L2` / `LT` (analog) |
| **Steer Left / Right** | `A` / `D` or `Left` / `Right Arrow` | `Left Stick` (cubic analog curve) |
| **Handbrake / Drift** | `Space` | `Circle` / `B` |
| **NOS Boost** | `Shift` or `Z` | `Cross` / `A` or `R1` / `RB` |
| **Speedbreaker (0.35× Slow-Mo)** | `X` or `Tab` | `Triangle` / `Y` or `L1` / `LB` |
| **Instant Police Pursuit (Heat 1→5)** | `F` | — |
| **Cycle Landmark Teleport (6 Districts)** | `V` | — |
| **Next / Previous Garage Car (40 Cars)** | `G` / `Shift+G` | — |
| **Cycle Camera (`chase`/`hero`/`street`/`overhead`)** | `C` | — |
| **Lighting Presets** | `1` Golden Hour · `2` Night Rain · `3` Daylight | — |
| **Toggle Live 24h Day/Night Cycle** | `4` | — |
| **Toggle Rain (`0.0` ↔ `0.88`)** | `B` | — |
| **Toggle 120 FPS MetalFX Frame-Gen** | `K` | — |
| **Toggle NFSMW HUD & Rotating Minimap** | `H` | — |
| **Toggle BMW M3 GTR ALMS Livery** | `L` | — |
| **Toggle Hardware Ray Tracing** | `T` | — |
| **Cycle Denoiser (`metalfx` / `svgf` / `off`)** | `N` | — |
| **Reset Car & Clear Pursuit** | `R` | — |
| **Save 64-SPP `1920×1080` PNG Screenshot** | `P` | — |
| **Orbit Camera / Zoom** | `Mouse Drag` / `Scroll Wheel` | — |
| **Quit** | `Esc` | — |

---

## 4. What's Included in `v1.0.0`

### Full-Island Rockport Streaming (6 Teleport Districts)
All **718 sections** (`638` spatial + `80` horizon panorama) stream seamlessly by XZ AABB distance (`350m` load / `430m` unload hysteresis) over Rockport's real `4,385`-node / `6,538`-segment (`229.25 km`) `WRoadNetwork`, illuminated by **`4,309` real extracted streetlamps, signals, and storefronts** in a `128×128×8` spatial light grid — using only **`~305 MB`** of GPU world+AI VRAM.

| Index | Landmark (`--landmark`) | Highlight |
| :---: | :--- | :--- |
| `0` | `Rosewood Blvd` | Iconic autumn boulevard with botanical Italian cypresses & golden-hour clearcoat reflections |
| `1` | `Downtown East Park` | Dense wet-asphalt city intersections, storefronts & real-streetlamp night-rain reflections |
| `2` | `Camden Beach` | Coastal hills, industrial skyline & Bayshore Blvd tunnel |
| `3` | `Highway 99` | High-speed multi-lane interstate ring built for `300+ km/h` NOS sprints |
| `4` | `Rosewood College` | Sweeping campus curves & elevation changes |
| `5` | `Rockport Waterfront` | Harbor-side avenues & sweeping bridge approaches |

Press **`V`** in-game anytime to teleport cleanly to the next district!

### 40-Car On-Demand Playable Garage
Press **`G`** / **`Shift+G`** in-game or launch with `--car <NAME>` to drive **any of all 40 playable NFSMW 2005 cars** — extracted on demand in `~1–2s` from your clean ISO (`build/car_cache/`) with seam-locked adaptive PN subdivision, exact `bstringhash` headlights/taillights, and auto-fitted wheel wells.

- **Flagship Hero Aliases**: `m3_gtr` (`BMWM3GTRE46`), `carrera_gt` (`CARRERAGT`), `gallardo` (`GALLARDO`), `corvette_c6r` (`CORVETTEC6R`), `ford_gt` (`FORDGT`), `murcielago` (`MURCIELAGO`), `rx7` (`RX7`), `slr` (`SLR`), `viper` (`VIPER`), `db9` (`DB9`), `supra` (`SUPRA`), `evo8` (`LANCEREVO8`), `wrx_sti` (`IMPREZAWRX`), `mustang_gt` (`MUSTANGGT`), `gti` (`GTI`), `cayman_s` (`CAYMANS`), `911_gt2` (`911GT2`), `911_turbo` (`911TURBO`).
- **List all 40 cars**: `python3 tools/extract_rt_car.py --list`

### Authentic VLT Physics, Ginsu PSOLA Audio & Heat 1–5 Police Pursuits
- **VLT Handling (`RTPhysics.swift`)**: `48` decoded `ATTRIBUTES.BIN` vehicle profiles (9-point torque curves, 6-speed auto gearbox, RWD vs AWD `TORQUE_SPLIT`, e-brake drift slip & `YAW_CONTROL`, spring-damper pitch/roll over 4-wheel GPU BVH ground probes, airborne gravity, `+65%` NOS boost with dual Mach-diamond exhaust flames & FOV kick, and `0.35×` amber Speedbreaker bullet-time).
- **Live PS2 Engine & Siren Audio (`RTAudioEngine.swift`)**: Lock-free `AVAudioEngine` granular PSOLA engine synth decoded from `9` real EA-XAS v0 `Gnsu v20` accel/decel banks (including the **BMW M3 GTR straight-cut gearbox whine**), plus `20` Sony SPU-ADPCM SFX (turbo spool/BOV flutter, drift tire screech, NOS roar/purge, shift backfire pops, Speedbreaker LPF, rain/wind bed, and 3D stereo Doppler police sirens).
- **20 Live AI Traffic & Heat 1–5 Police (`RTGameWorld.swift` + `RTAIVehicleLoader.swift`)**: Up to `16` live traffic cars + `4` pursuing Rockport PD units (`COPMIDSIZE` Heat 1–2 → `COPGTO` Heat 3–4 → `COPSPORT` Heat 5) riding real hills via `aiRoadProbeKernel`, casting blazing alternating **red/blue ray-traced roof lightbar sweeps** across wet streets and your car's clearcoat, tracked on a live post-FI rotating GPU minimap with Heat/Bounty/Evade/Busted HUD.

---

## 5. Performance & Troubleshooting Tips

- **Locked `103–128 FPS` @ `1080p`**: Keep `--frame-gen on` (or toggle **`K`** live). The NFSMW tachometer, NOS/Speedbreaker bars, Pursuit status, and rotating minimap run in `hudOverlayKernel` **after** `MTLFXFrameInterpolator` on both real and interpolated frames so text and digits never warp.
- **Native Zero-FG Mode**: Press **`K`** (or `--frame-gen off`) for `58–68 FPS` native `MTLFXTemporalDenoisedScaler` driving with 20 live AI/cops.
- **Need a 4K Beauty Shot?**: Press **`P`** anytime (or pass `--headless --world build/rockport_world/rockport_full.rkpt --landmark 1 --width 3840 --height 2160 --spp 48 --preset night_neon --rain 0.88 --out shot_4k.png`) to render a silky path-traced Downtown Rockport PNG straight to disk.
- **Missing ISO / Pack Error**: Confirm `echo "$NFSMW_ISO"` points to your `SLUS-21351` `.iso` file and re-run `make export-rockport-full`.
- **Track 1 (PS2 / PCSX2 4K Remaster)**: Frozen at `v0.5.0` (`8.9 / 10` Video QA PASS). See [README.md](../README.md) for the one-command PCSX2 ISO build workflow.
