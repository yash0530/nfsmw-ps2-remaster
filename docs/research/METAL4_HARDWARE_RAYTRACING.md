# Apple Silicon Metal 4 Hardware Ray Tracing Engine (`native_metal_rt/`)

**Target Platform:** Apple Silicon (verified on **Apple M5 Pro 20-core GPU**, Metal 4, macOS)  
**Toolchain:** Pure Apple Swift (`/usr/bin/swiftc`) + Apple frameworks (`Metal`, `MetalKit`, `AppKit`, `CoreGraphics`, `ImageIO`, `UniformTypeIdentifiers`, `simd`) — zero external packages.

---

## 1. Architecture Overview (Wave 4 GT7-Grade Overhaul)

`native_metal_rt/` implements a standalone, hardware-accelerated ray tracing renderer (`RockportMetalRT`) built on Metal's hardware ray-intersection pipeline (`MTLPrimitiveAccelerationStructure`, `MTLInstanceAccelerationStructure`, and `metal::raytracing::intersector<triangle_data, instancing>`).

| File | Responsibility |
| :--- | :--- |
| `native_metal_rt/Makefile` | Compiles `RockportMetalRT` with `swiftc -O -whole-module-optimization`; builds `.metallib` when `xcrun metal` is installed or stages `RayTracing.metal` for runtime compilation. |
| `native_metal_rt/RTSceneBuilder.swift` | Multi-source scene loader (`build/flywheel/hq_m3_gtr/scene.json` 58K-poly hero priority, `build/metal_wave3/scene/scene.json`, direct `build/obj_preview/*.obj` PS2 Z-up → Y-up converter, and offline `--synthetic`), Rockport Autumn Coastal Highway geometry (wet highway, concrete Jersey barrier, coastal bay water, sculpted hillside, instanced pine/autumn trees, instanced streetlamps), 4K/2K CC0 PBR texture loader, and `RTLookdevConfig` (`--config <json>`) parser. |
| `native_metal_rt/RTRenderer.swift` | Runtime `device.supportsRaytracing` guard, BLAS/TLAS construction & dynamic per-frame TLAS updates (steering, spinning wheels, chassis roll/yaw, scrolling highway & streetlamps), demodulated G-buffer + progressive accumulation targets, 4-pass À-Trous denoiser dispatch, bloom/ACES compute post-process, real-time driving physics, and PNG output. |
| `native_metal_rt/RayTracing.metal` | Compute kernels (`raytraceKernel`, `denoiseAtrousKernel`, `remodulateKernel`, `bloomThresholdKernel`, `bloomBlurKernel`, `compositeKernel`) using `metal::raytracing::intersector<triangle_data, instancing>`, Heitz GGX VNDF sampling, Nishita atmospheric sky + 3D fBM volumetric clouds, twin BMW E46 Angel-Eye projectors, and procedural `"MW 2026"` rear plate. |
| `native_metal_rt/main.swift` | Interactive 120Hz driveable `MTKView` showcase window (`W/S` throttle/brake, `A/D` steer, `C` chase cam) + headless CLI (`--headless`, `--config`, `--drive-demo`, `--speed-kmh`, `--steer-deg`, `--spp`, `--out`, `--compare-out`). |

---

## 2. Acceleration Structure Pipeline (BLAS + Dynamic TLAS)

1. **Runtime Hardware Verification**: `RTRenderer.init` verifies `device.supportsRaytracing` and exits cleanly if run on non-RT hardware.
2. **Global Geometry Pools**: All vertices are packed into `RTVertex` (48-byte stride: `position`, smooth `normal`, `uv`, `modelPos`, `materialID`), paired with `UInt32` index triplets and per-triangle material IDs (`triMaterialBuffer`).
3. **Per-Mesh BLAS & Shared Instance BLASes**:
   - Loads `build/flywheel/hq_m3_gtr/scene.json` (crease-locked `18°` L1 PN-subdivided 14,560-tri `BMWM3GTRE46_KIT00_BODY_B`, 4× L1-subdivided 2,456-tri authentic stock `BMWM3GTRE46` BBS deep-dish race wheels, interior, windows, headlights, taillights, and Brembo brakes) alongside the gritty Rockport 2005 Industrial Overpass & Safehouse Dockyard structures (wet asphalt, concrete viaduct piers & elevated deck, corrugated steel warehouse loading dock with hanging sodium pendant worklamps, W-beam steel guardrail, and instanced cobra-head sodium streetlamps — **36 TLAS instances**, **29,516 triangles** total).
4. **Dynamic TLAS Rebuilds**:
   - Each wheel/brake mesh records its exact bounding-box `pivotCenter`, enabling per-frame steering (`steerDeg`), spinning (`wheelSpinRad`), chassis body roll/yaw, and infinite scrolling streetlamp poles via `MTLAccelerationStructureCommandEncoder.build(accelerationStructure: tlas, ...)`.
   - Visibility masks (`0x01` opaque vs `0x02` glass) allow sunlight to pass through clear headlight covers and smoked cabin windows onto the interior rollcage, bucket seat, and recessed Angel-Eye projectors.

---

## 3. Ray-Traced Lighting, Denoising & Shading Stages (`RayTracing.metal`)

1. **Physically-Based Nishita Atmosphere + Moody East Coast Autumn Storm Clouds**:
   - Single-scattering Rayleigh + Mie atmospheric raymarch with Henyey-Greenstein solar aureole, 6-layer 3D fBM dramatic East Coast late-autumn storm clouds, soft atmospheric horizon haze, and gritty Rockport industrial lighting.
2. **Heitz GGX VNDF Sampling & Firefly Clamping**:
   - Uses Cranley-Patterson shifted Roberts R2 quasi-Monte Carlo sequences (`alpha = (0.7548776662, 0.5698402910)`) paired with **GGX VNDF** importance sampling (`sampleGGXVNDF`), finite sun/moon disc clearcoat roughness flooring, single-counted analytical sun/moon speculars (`includeSunDisc = false` on secondary glossy rays), and secondary radiance clamping (`6.5` GI / `9.0–10.0` glossy reflection) to eliminate fireflies.
3. **GPU Edge-Preserving À-Trous Joint-Bilateral Denoiser (`denoiseAtrousKernel`)**:
   - `raytraceKernel` emits demodulated diffuse (`accumDiffTex`) and specular (`accumSpecTex`) accumulation buffers alongside progressive `gAlbedoTex`, `gEmissiveTex`, `gNormalRoughTex`, and `gDepthTex`.
   - `denoiseAtrousKernel` executes **4 passes** (step sizes `1, 2, 4, 8`) with a 5×5 B3-spline kernel weighted by surface normal dot (`pow(max(dot(nP, nQ), 0), 48)`), depth similarity, luminance difference, and roughness-aware specular radius scaling (preserving mirror-sharp clearcoat and wet puddle reflections).
   - `remodulateKernel` combines `denoisedDiff * gAlbedo + denoisedSpec + gEmissive`, feeding half-res bloom (`bloomThresholdKernel` + `bloomBlurKernel`) and ACES Filmic grading (`compositeKernel`).
4. **Authentic UV-Mapped NFSMW 2005 Hero Materials & Gritty Rockport Industrial Overpass**:
   - **Real PS2 UV-Mapped Hero Livery & Badges**: Loads local runtime-extracted `BMWM3GTRE46_PSRTEST_MASK` (edge-sharpened 2048×2048), `BMWM3GTRE46_BADGING` (BMW roundel emblem, M3 trunk badge, amber side markers, hood pins), `BMWM3GTRE46_MISC` (BMW kidney grille, honeycomb air dam, hood louver vents, M3 side fender gills, fuel cap, crimson "brembo" calipers), `KIT00_HEADL` (authentic BMW E46 twin projector headlights + fluted amber turn signals), `KIT00_BRAKE` (authentic E46 M3 LED taillights), `TIRE` (stock BMW M3 GTR deep-dish BBS race wheels), and `INTERIOR` (red racing bucket seat & rollcage), with clean offline procedural fallbacks for `--synthetic`.
   - **Gritty Rockport Overpass & Industrial Safehouse Dockyard**: Real ImageIO-loaded CC0 4K/2K wet asphalt & weathered concrete maps, formwork-seamed viaduct piers & overhead highway deck, corrugated weathered steel warehouse loading dock with hanging warm tungsten-sodium pendant worklamps, steel W-beam guardrail, and glowing raised amber/white road cat's-eyes.

---

## 4. Measured Apple M5 Pro Benchmarks (1920×1080 @ 64 SPP, À-Trous 4-Pass)

Observed directly on **Apple M5 Pro (20-core GPU, Metal 4)** against authentic UV-mapped `build/flywheel/hq_m3_gtr/scene.json` (**36 TLAS instances**, **29,516 triangles**):

| Output | Mode & Settings | Measured GPU Render Time |
| :--- | :--- | :--- |
| `build/metal_rt_wave4/rt_bmw_m3_gtr_golden_hour.png` | 1920×1080 @ 64 SPP (`atrous4`) | **1.47 s** |
| `build/metal_rt_wave4/rt_comparison_split.png` | 1920×1080 @ 64 SPP (split RT-Off / RT-On) | **1.01 s** |
| `build/metal_rt_wave4/rt_bmw_m3_gtr_night_rain.png` | 1920×1080 @ 64 SPP (`night_neon`, `atrous4`) | **1.45 s** |
| `build/metal_rt_wave4/rt_bmw_m3_gtr_drive_demo.png` | 1920×1080 @ 64 SPP (`--drive-demo --speed-kmh 165 --steer-deg 10`) | **1.62 s** |
| `build/metal_rt_wave4/rt_bmw_m3_gtr_daylight.png` | 1920×1080 @ 64 SPP (`daylight`, `atrous4`) | **1.46 s** |
