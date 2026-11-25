# Rockport 2026 Post-FX Shaders (`shaders/`)

Original clean-room shaders authored for **NFSMW: Rockport 2026** (`v0.4.0`).

---

## Files

| File | Format / Target | Purpose |
| :--- | :--- | :--- |
| [`rockport2026_ssgi.slang`](rockport2026_ssgi.slang) | RetroArch / `librashader` `#version 450` `.slang` | Screen-Space Global Illumination (8-direction golden-angle color bounce), perspective wet-road SSR with Schlick Fresnel & ripple roughness, and 3×3 bilateral stabilization. |
| [`rockport2026_aces_grade.glsl`](rockport2026_aces_grade.glsl) | `#version 450` GLSL (Vulkan / `librashader` compatible) | Dark-channel atmospheric dehaze, edge-preserving micro-contrast, 8-tap Karis-weighted HDR bloom, and Stephen Hill fitted ACES RRT+ODT filmic tone mapping. |

---

## Execution Matrix: PCSX2 v2.6.3 vs `librashader` vs Native Metal 4

| Feature / Shader | PCSX2 v2.6.3 (macOS Metal Today) | PCSX2 + `librashader` (PR `#14433` / Vulkan) | Native Metal 4 (`native_metal/`) |
| :--- | :--- | :--- | :--- |
| **AMD FidelityFX CAS (`CASMode`, `CASSharpness`)** | **Native** (`CASFloat` / `CASHalf` in `Metal23.metallib`) | Available | Optional post-pass |
| **FXAA (`fxaa = true`)** | **Native** (`ps_fxaa` in `Metal23.metallib`) | Available | Optional / MSAA |
| **ShadeBoost (`ShadeBoost_*`)** | **Native** (`ps_shadeboost` in `Metal23.metallib`) | Available | Replaced by HDR ACES |
| **TV Shaders (`TVShader = 0..5`)** | **Native** (`ps_filter_*` in `Metal23.metallib`) | Available | N/A |
| **`rockport2026_ssgi.slang`** | **Not supported** (`librashader` absent in v2.6.3 binary) | **Target runtime** (screen-space color-buffer GI/SSR) | **Superseded** by hardware BVH RT |
| **`rockport2026_aces_grade.glsl`** | **Not supported** (standalone `.fx`/`.glsl` removed in 2.0) | **Target runtime** (wrap or include in `.slangp` chain) | **Native** (`native_metal/Shaders.metal`) |
| **True BVH Ray-Traced Reflections & GI** | Not possible in post-FX | Not possible (no world BVH) | **Native** (`MTLAccelerationStructure`) |

> **Important Scope Note:** Because `librashader` post-FX shaders operate on the final presented RGBA frame without PS2 GS world-space G-buffers or acceleration structures, `rockport2026_ssgi.slang` reconstructs screen-space relief normals and horizon perspective masks from color/luminance gradients. True world-space ray-traced GI, clearcoat reflections, and wet-road ray hits live in `native_metal/`.

See [`docs/research/PCSX2_LIVE_VERIFICATION_AND_POSTFX.md`](../docs/research/PCSX2_LIVE_VERIFICATION_AND_POSTFX.md) for the complete binary audit of PCSX2 v2.6.3 Metal post-FX and live boot telemetry.
