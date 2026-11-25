# NFSMW: Rockport 2026 — Native Apple Silicon Metal 4 Hardware Ray Tracing Engine

Pure Apple Swift + Metal hardware ray tracing renderer (`RockportMetalRT`) targeting Apple Silicon GPUs (verified on **Apple M5 Pro 20-core GPU**, Metal 4, macOS). Zero external Swift packages or third-party dependencies.

## Features

- **Runtime GPU Verification**: Verifies `device.supportsRaytracing` before initializing ray-tracing pipelines.
- **Hardware Acceleration Structures**: Builds per-mesh `MTLPrimitiveAccelerationStructure` (BLAS) and scene `MTLInstanceAccelerationStructure` (TLAS) via `MTLAccelerationStructureCommandEncoder` with residency tracking (`useResources`).
- **Metal Ray Tracing Kernel (`RayTracing.metal`)**: Uses `metal::raytracing::intersector<triangle_data, instancing>`:
  - **Ray-Traced Soft Area Sun Shadows**: Jittered cone rays with instance-mask glass pass-through (`mask = 0x01` opaque vs `0x02` glass) for true contact-hardening shadows.
  - **Ray-Traced Specular Reflections (RTR)**: Secondary GGX bounce rays from wet asphalt puddles (reflecting real 3D car triangles, headlights, taillights, wheels, and skyline architecture) and from clearcoat car bodywork & alloy rims.
  - **RTAO & 1-Bounce RTGI**: Cosine-weighted hemisphere bounce rays with progressive multi-SPP accumulation (`--spp 32` headless, progressive 120Hz interactive).
  - **HDR Post-Processing**: `RGBA32Float` accumulation, half-res bright-pass bloom + separable Gaussian blur, ACES Filmic tone mapping, and sRGB PNG export.
  - **RT-Off vs Hardware-RT-On Split Compare (`--compare-out`)**: Renders raster-style direct/analytic shading on the left half vs full M5 Pro hardware RT on the right half.
- **Self-Contained Scene Loading**: Reads `build/metal_wave3/scene/scene.json`, falls back directly to `build/obj_preview/*.obj` (auto-converting PS2 Z-up to Y-up meters), or synthesizes clean procedural geometry with `--synthetic` for offline CI.

## Build & Run

```bash
make -C native_metal_rt OUT_DIR=../build/metal_rt_wave3

# Headless 1920x1080 32-SPP renders
./build/metal_rt_wave3/RockportMetalRT --headless --preset golden_hour --spp 32 --width 1920 --height 1080 \
  --out build/metal_rt_wave3/rt_bmw_m3_gtr_golden_hour.png \
  --compare-out build/metal_rt_wave3/rt_comparison_split.png

./build/metal_rt_wave3/RockportMetalRT --headless --preset night_neon --spp 32 --width 1920 --height 1080 \
  --out build/metal_rt_wave3/rt_bmw_m3_gtr_night_neon.png

# Interactive 120Hz Window
./build/metal_rt_wave3/RockportMetalRT --preset golden_hour
```

## Interactive Controls

| Key / Input | Action |
|---|---|
| Left Mouse Drag | Orbit camera |
| Scroll Wheel | Zoom in / out |
| `1` / `2` / `3` | Switch preset (`golden_hour` / `night_neon` / `daylight`) |
| `T` | Toggle Hardware Ray Tracing on/off |
| `L` | Toggle BMW M3 GTR racing livery |
| `R` | Reset camera |
| `P` | Render & save 1920×1080 32-SPP PNG |
| `Q` / `Esc` | Quit |
