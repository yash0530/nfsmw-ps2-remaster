# NFSMW: Rockport 2026 — Native Apple Silicon Metal 4 Hardware Ray Tracing Engine

Pure Apple Swift + Metal hardware ray tracing renderer (`RockportMetalRT`) targeting Apple Silicon GPUs (verified on **Apple M5 Pro 20-core GPU**, Metal 4, macOS). Zero external Swift packages or third-party dependencies.

## Features (Wave 4 Overhaul)

- **Runtime GPU Verification**: Verifies `device.supportsRaytracing` before initializing ray-tracing pipelines.
- **Hardware Acceleration Structures**: Builds per-mesh `MTLPrimitiveAccelerationStructure` (BLAS) and dynamic scene `MTLInstanceAccelerationStructure` (TLAS) via `MTLAccelerationStructureCommandEncoder` with residency tracking (`useResources`).
- **Authentic UV-Mapped NFSMW 2005 BMW M3 GTR E46**:
  - Automatically prioritizes `build/flywheel/hq_m3_gtr/scene.json` (crease-locked `18°` L1 PN-subdivided 14,560-tri BMW M3 GTR sheetmetal + 4× authentic stock `BMWM3GTRE46` deep-dish BBS race wheels, interior, windows, headlights, taillights, and Brembo brakes — 29.5K crisp tris), falling back cleanly to `build/metal_wave3/scene/scene.json`, `build/obj_preview/`, or `--synthetic`.
  - **Real PS2 UV-Mapped Hero Textures** (loaded locally from gitignored `build/` with offline procedural fallback for `--synthetic`):
    - Authentic NFSMW 2005 **hero blue-and-silver livery** (`BMWM3GTRE46_PSRTEST_MASK`, edge-sharpened 2048×2048) over BMW Titanium Silver Metallic & royal-cobalt blue under liquid-glass IOR `1.56` clearcoat.
    - Real **BMW Roundel emblem**, M3 trunk badge, amber side markers, hood pins (`BMWM3GTRE46_BADGING`), real **BMW kidney grille, honeycomb air dam, hood louver vents, M3 side fender gills, fuel filler cap, and red "brembo" calipers** (`BMWM3GTRE46_MISC`), real **BMW E46 twin projector headlights** (`KIT00_HEADL`), real **E46 M3 LED taillights** (`KIT00_BRAKE`), real **BBS race wheel & tire** (`TIRE`), and real **red racing bucket seat & rollcage interior** (`INTERIOR`).
- **Gritty Rockport 2005 Industrial Overpass & Safehouse Dockyard**:
  - Rain-slicked weathered 4K CC0 asphalt & concrete overpass deck with drier tire tracks, pooled puddles, organic micro-ripples, and glowing highway cat's-eyes.
  - Heavy weathered reinforced-concrete viaduct piers & overhead highway deck, corrugated steel industrial warehouse loading dock with hanging warm tungsten-sodium pendant worklamps, steel W-beam guardrail, instanced dark-steel sodium streetlamps, and moody late-autumn East Coast Nishita sky with dramatic 3D fBM storm clouds.
- **GPU Edge-Preserving À-Trous Joint-Bilateral Denoiser (`denoiseAtrousKernel`)**:
  - Emits demodulated diffuse/specular accumulation buffers alongside `gAlbedo`, `gNormalRough`, `gDepth`, and `gEmissive`.
  - Uses Heitz **GGX VNDF** importance sampling + firefly clamping + 4-pass À-Trous edge-stopping filter (step sizes `1, 2, 4, 8`) + `remodulateKernel` + Bloom + ACES Filmic color grading.
- **Real-Time Driveable Highway & JSON LookDev Config (`--config`, `--drive-demo`)**:
  - Interactive `W/S` throttle/brake (`0..280 km/h`), `A/D` steering + chassis roll/yaw, infinite scrolling highway & streetlamps, spinning wheels, `C` chase/showcase camera toggle, `1/2/3` presets, `T` toggle RT, `N` toggle denoiser, `P` save 64-SPP PNG.

## Build & Run

```bash
make -C native_metal_rt OUT_DIR=../build/metal_rt_wave4

# Headless 1920x1080 64-SPP denoised renders
./build/metal_rt_wave4/RockportMetalRT --headless --preset golden_hour --spp 64 --width 1920 --height 1080 \
  --out build/metal_rt_wave4/rt_bmw_m3_gtr_golden_hour.png \
  --compare-out build/metal_rt_wave4/rt_comparison_split.png

./build/metal_rt_wave4/RockportMetalRT --headless --preset night_neon --spp 64 --width 1920 --height 1080 \
  --out build/metal_rt_wave4/rt_bmw_m3_gtr_night_rain.png

./build/metal_rt_wave4/RockportMetalRT --headless --preset golden_hour --drive-demo --speed-kmh 145 --steer-deg 12 \
  --spp 64 --width 1920 --height 1080 --out build/metal_rt_wave4/rt_bmw_m3_gtr_drive_demo.png

# Interactive 120Hz Window
./build/metal_rt_wave4/RockportMetalRT --preset golden_hour
```

## Interactive Controls

| Key / Input | Action |
|---|---|
| `W` / `S` or Up / Down | Throttle / Brake (`0..280 km/h`) |
| `A` / `D` or Left / Right | Steer front wheels + chassis roll |
| `C` | Toggle Chase Cam vs Showcase Orbit Cam |
| Mouse Drag / Scroll | Orbit camera / Zoom |
| `1` / `2` / `3` | Switch preset (`golden_hour` / `night_neon` / `daylight`) |
| `T` / `N` / `L` | Toggle Hardware RT / À-Trous Denoiser / BMW Livery |
| `R` | Reset camera & driving state |
| `P` | Render & save 1920×1080 64-SPP PNG |
| `Q` / `Esc` | Quit |
