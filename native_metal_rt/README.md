# NFSMW: Rockport 2026 — Native Apple Silicon Metal 4 Hardware Ray Tracing & Real Rockport City Engine (`v0.6.0`)

Pure Apple Swift + **Metal 4 / MSL 4.0** hardware ray tracing engine (`RockportMetalRT`) targeting Apple Silicon GPUs (verified on **Apple M5 Pro 20-core GPU**, macOS). Zero external Swift packages or third-party dependencies.

## Features (`v0.6.0` Real Rockport City + Hardware MXU MetalFX Denoiser)

- **Real Streamed Rockport City (`.rkpt` v1 Binary Loader)**:
  - Loads authentic extracted NFSMW PS2 city districts (`TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN`, exported via `tools/export_rockport_world.py`) through compact memory-mapped `.rkpt` archives (`rosewood.rkpt`: `96.59 MiB` / `25` sections / `2,575` instances / `339/339` textures; `downtown.rkpt`: `109.59 MiB` / `28` sections / `3,498` instances / `345/345` textures) with `855` Free-Roam-invisible neon chevron barriers filtered out and `4×`/`8×` detail-preserving world textures.
  - Builds bindless **`MTLHeap` + `MTLResidencySet`** texture heaps and per-section hardware BVH BLAS/TLAS acceleration structures (`MTLPrimitiveAccelerationStructure` / `MTLInstanceAccelerationStructure`) with verified bidirectional boulevard spawns (`120m` forward / `40m` rear sightlines).
- **Hardware MXU Neural Denoising (`MTLFXTemporalDenoisedScaler`) + SVGF Fallback (`--denoise metalfx|svgf|off`)**:
  - Activates Apple Silicon's **`[MetalFX] UBFNet/dBFNet fused-MXU path ACTIVE`** temporal neural denoiser with motion vectors, depth, normals, diffuse/specular albedo, and roughness guides — sustaining **`52.9–57.0 FPS` (`~17.7–18.9 ms/frame`) at `1920×1080`** live driving across real Rosewood.
  - Also supports real-time GPU **SVGF** (`--denoise svgf`) and offline progressive multi-SPP accumulation (`--spp 64`) with Heitz GGX VNDF importance sampling, grazing-angle 4-tap anisotropic world texture filtering, and perceptual sRGB ACES filmic grading.
- **8-Ray GPU BVH Road & Wall Driving Physics**:
  - Live interactive & benchmark driving (`--drive-bench N`, `--drive-demo`) across real Rockport elevation, ramps, curbs, and buildings using 8-ray hardware BVH road-height/surface-normal queries and wall collision detection, with chase/hero/street/overhead cameras (`--camera-mode`) and optional motorsport HUD (`--hud`).
- **Authentic UV-Mapped 3-Car PS2 Garage (`--car m3_gtr|carrera_gt|gallardo`)**:
  - Loads crease-locked PN-subdivided PS2 hero cars with authentic UV-mapped liveries, badges, crystal-clear glass projector headlights, LED taillights, pure vulcanized matte-rubber tires, and deep-dish alloy wheels + Brembo calipers, falling back cleanly to `--synthetic` offline.
- **Dynamic Day, Golden Hour & Night-Rain Lighting (`--preset golden_hour|night_neon|daylight`)**:
  - Night-rain mode features glowing Rockport storefronts, warm sodium streetlamps, crisp twin headlight projectors, pooled wet-asphalt Fresnel reflections, and animated raindrop ripples.
- **Verified `v0.6.0` Visual QA**: Progressed **`5.9 → 6.9 → 8.2 → 8.7 / 10` (`0 P0`)** across `renders_v5` (minor M3 trunk plate cove surround & P2 polish tracked for `v0.6.1`).

## Build & Run

```bash
# 1. Export real Rosewood & Downtown Rockport City .rkpt archives read-only from your clean PS2 ISO
python3 tools/rockport_cli.py export-world --district all \
  --iso "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --out-dir build/rockport_world

# 2. Build native Metal 4 / MSL 4.0 engine
make -C native_metal_rt OUT_DIR=../build/metal_rt_v06

# 3. Interactive real-time 1080p MetalFX driving in Rosewood
./build/metal_rt_v06/RockportMetalRT \
  --world build/rockport_world/rosewood.rkpt \
  --car m3_gtr --preset golden_hour --denoise metalfx --camera-mode chase --hud

# 4. Headless real-time driving benchmark (30 frames @ 1080p with MetalFX MXU neural denoiser)
./build/metal_rt_v06/RockportMetalRT --headless \
  --world build/rockport_world/rosewood.rkpt \
  --car m3_gtr --preset golden_hour --denoise metalfx --camera-mode chase \
  --drive-bench 30 --hud --width 1920 --height 1080 \
  --out build/rockport_world/rosewood_drive_bench.png

# 5. Headless 64-SPP high-detail hero render (golden_hour / night_neon / daylight)
./build/metal_rt_v06/RockportMetalRT --headless \
  --world build/rockport_world/rosewood.rkpt \
  --car m3_gtr --preset night_neon --denoise metalfx --camera-mode hero \
  --spp 64 --width 1920 --height 1080 \
  --out build/rockport_world/rosewood_night_rain.png
```

## Interactive Controls

| Key / Input | Action |
|---|---|
| `W` / `S` or Up / Down | Throttle / Brake (`0..280 km/h`) |
| `A` / `D` or Left / Right | Steer front wheels + chassis roll |
| `C` | Cycle Camera (`hero` / `chase` / `street` / `overhead`) |
| `G` | Cycle Garage Car (`m3_gtr` / `carrera_gt` / `gallardo`) |
| `H` | Toggle Motorsport Digital HUD |
| Mouse Drag / Scroll | Orbit camera / Zoom |
| `1` / `2` / `3` | Switch preset (`golden_hour` / `night_neon` / `daylight`) |
| `T` / `N` / `L` | Toggle Hardware RT / Cycle Denoiser (`metalfx` / `svgf` / `off`) / Toggle Livery |
| `R` | Reset camera & driving state |
| `P` | Render & save 1920×1080 64-SPP PNG |
| `Q` / `Esc` | Quit |
