# RockportMetalPBR — Native Apple Silicon Metal 2026 PBR Renderer

Pure Apple Swift + Metal (`Metal`, `MetalKit`, `AppKit`, `CoreGraphics`, `ImageIO`, `simd`) showcase renderer for **NFSMW: Rockport 2026**. Zero third-party packages.

## Features
- **Cook-Torrance GGX PBR**: Trowbridge-Reitz NDF, height-correlated Smith-GGX Geometry, Schlick Fresnel.
- **Dual-Lobe Automotive Clearcoat Paint**: Metallic flake normal perturbation, 1.5 IOR glossy clearcoat lobe, energy-conserving base coat attenuation, and an analytical BMW M3 GTR silver-and-blue racing livery option.
- **Procedural Nishita/Atmospheric Sky-Dome + Analytic IBL**: Horizon haze, sun disc + corona (driven by `patches/lighting/*.json`), and Rockport skyline silhouette reflections in clearcoat, chrome, glass, and wet asphalt.
- **Wet Asphalt Ground Plane**: Procedural periodic puddle-mask roughness modulation, CC0 4K wet asphalt texture (`build/cc0_baked/asphalt_02_wet_4k.png`) support, planar/analytic car & headlight reflections, and soft contact AO + directional shadow.
- **HDR Post-Processing**: `RGBA16Float` HDR render target, bright-pass bloom threshold, separable 9-tap Gaussian HDR bloom, and ACES Filmic tone mapping + sRGB gamma.

## Build
```bash
make -C native_metal OUT_DIR=../build/metal_wave3
```

## Run
**Headless 1080p M5 Pro Render:**
```bash
./build/metal_wave3/RockportMetalPBR \
  --headless \
  --scene build/metal_wave3/scene/scene.json \
  --out build/metal_wave3/render_golden_hour.png \
  --width 1920 --height 1080 --preset golden_hour
```

**Interactive 120Hz Window:**
```bash
./build/metal_wave3/RockportMetalPBR --scene build/metal_wave3/scene/scene.json
```
- **Mouse Drag**: Orbit camera | **Scroll**: Zoom
- **`1` / `2` / `3`**: Switch `golden_hour` / `daylight` / `night_rain`
- **`L`**: Toggle BMW M3 GTR racing stripes | **`R`**: Reset camera | **`P`**: Save PNG | **`Q`/`Esc`**: Quit
