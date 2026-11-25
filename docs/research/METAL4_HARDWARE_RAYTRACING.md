# Apple Silicon Metal 4 Hardware Ray Tracing Engine (`native_metal_rt/`)

**Target Platform:** Apple Silicon (verified on **Apple M5 Pro 20-core GPU**, Metal 4, macOS)  
**Toolchain:** Pure Apple Swift (`/usr/bin/swiftc`) + Apple frameworks (`Metal`, `MetalKit`, `AppKit`, `CoreGraphics`, `ImageIO`, `UniformTypeIdentifiers`, `simd`) — zero external packages.

---

## 1. Architecture Overview

`native_metal_rt/` implements a standalone, hardware-accelerated ray tracing renderer (`RockportMetalRT`) built on Metal's hardware ray-intersection pipeline (`MTLPrimitiveAccelerationStructure`, `MTLInstanceAccelerationStructure`, and `metal::raytracing::intersector<triangle_data, instancing>`).

| File | Responsibility |
| :--- | :--- |
| `native_metal_rt/Makefile` | Compiles `RockportMetalRT` with `swiftc -O -whole-module-optimization`; builds `.metallib` when `xcrun metal` is installed or stages `RayTracing.metal` for runtime compilation. |
| `native_metal_rt/RTSceneBuilder.swift` | Multi-source scene loader (`scene.json`, direct `build/obj_preview/*.obj` PS2 Z-up → Y-up converter, and offline `--synthetic` procedural coupe) + Rockport wet asphalt & urban architecture BLAS geometry + CC0/procedural textures. |
| `native_metal_rt/RTRenderer.swift` | Runtime `device.supportsRaytracing` guard, BLAS/TLAS construction via `MTLAccelerationStructureCommandEncoder`, `useResources` residency tracking, progressive `RGBA32Float` accumulation, bloom/ACES compute post-process, and PNG output. |
| `native_metal_rt/RayTracing.metal` | Compute kernels (`raytraceKernel`, `bloomThresholdKernel`, `bloomBlurKernel`, `compositeKernel`) using `metal::raytracing::intersector<triangle_data, instancing>`. |
| `native_metal_rt/main.swift` | Interactive 120Hz `MTKView` showcase window + headless CLI (`--headless`, `--scene`, `--synthetic`, `--preset`, `--spp`, `--width`, `--height`, `--out`, `--compare-out`). |

---

## 2. Acceleration Structure Pipeline (BLAS + TLAS)

1. **Runtime Hardware Verification**: `RTRenderer.init` verifies `device.supportsRaytracing` and exits with a clear diagnostic if run on hardware without ray tracing support.
2. **Global Geometry Pools**: All vertices are packed into `RTVertex` (48-byte stride: `position`, smooth `normal`, `uv`, `modelPos`, `materialID`), paired with `UInt32` index triplets and per-triangle material IDs (`triMaterialBuffer`).
3. **Per-Mesh BLAS (`MTLPrimitiveAccelerationStructure`)**:
   - Each car sub-part (`BMWM3GTRE46_KIT00_BODY_B`, interior, windows, headlights, brakelights, 4× positioned `ENKEI_STYLE01_17_25_B` wheels, 4× brake assemblies), the wet asphalt ground plane, and the Rockport urban bridge/tower architecture are built into dedicated `MTLPrimitiveAccelerationStructure` instances via `MTLAccelerationStructureCommandEncoder`.
4. **Scene TLAS (`MTLInstanceAccelerationStructure`)**:
   - `MTLAccelerationStructureInstanceDescriptor` records `MTLPackedFloat4x3` model transforms, `accelerationStructureIndex`, and visibility masks:
     - `0x01`: opaque geometry (car body, wheels, calipers, rotors, asphalt, concrete/neon architecture)
     - `0x02`: transparent car glass (`smoked_glass`, `headlight_glass`)
   - Shadow rays query with mask `0x01` so sunlight passes naturally through headlight lenses and cabin glass onto the rollcage/seats and ground, while primary and reflection rays query `0xFF`.
   - `computeEncoder.useResources(blasList, usage: .read)` is called before dispatching `raytraceKernel` so Metal guarantees residency for all BLASes referenced indirectly by the TLAS.

### Shader Compilation Portability

When Xcode's optional `MetalToolchain` component is not installed (`xcrun metal` unavailable), `RTRenderer` seamlessly falls back to `device.makeLibrary(source:options:)` against `RayTracing.metal` at runtime with `fastMathEnabled = true`. Both `.metallib` and runtime source paths execute the identical Metal Shading Language ray-tracing kernels.

---

## 3. Ray-Traced Lighting & Shading Stages (`RayTracing.metal`)

1. **Roberts R2 Quasi-Monte Carlo Sampling**:
   - Uses a per-pixel Cranley-Patterson shifted R2 low-discrepancy sequence (`alpha = (0.7548776662, 0.5698402910)`) across sub-pixel jitter, sun cone shadows, GGX microfacet reflection rays, clearcoat reflection rays, and cosine-weighted GI rays for fast convergence at 32 SPP.
2. **Ray-Traced Soft Area Sun Shadows**:
   - `shadowRT.accept_any_intersection(true)` traces cone-jittered rays (`sampleCone`) toward the sun disc, producing true contact-hardening shadows under the tires and widebody skirts with soft penumbras at distance.
3. **Ray-Traced Specular Reflections (RTR)**:
   - **Wet Asphalt Puddles**: Domain-warped `fbm2D` puddle masks vary surface roughness from `0.018` (mirror puddles) to `0.54` (damp rough asphalt). GGX importance-sampled reflection rays intersect actual 3D car triangles, glowing headlights/taillights, alloy rims, and Rockport neon/concrete towers.
   - **Dual-Lobe Metallic Clearcoat Car Paint**: Combines analytical BMW M3 GTR silver (`#C7CFD9`) + cobalt blue racing livery (`evaluateBMWLivery`) + subtle micro-flake perturbation with a secondary low-roughness (`0.03`) IOR 1.5 clearcoat reflection ray capturing car self-reflections and environment highlights.
4. **RTAO & 1-Bounce RTGI**:
   - Cosine-weighted hemisphere bounce rays (`max_distance = 24.0`) compute distance-attenuated ambient occlusion and inject real secondary bounce radiance from sunlit asphalt, glowing xenon/taillight emitters, and neon architectural strips.
5. **Glass Fresnel Reflection + Transmission**:
   - Glass hits evaluate Schlick Fresnel (`IOR = 1.52`), trace a secondary reflection ray off the windshield/headlight cover, and trace a continuation transmission ray through the glass onto the interior rollcage/bucket seat or xenon projector housing.
6. **HDR Post-Processing & Split Comparison**:
   - `RGBA32Float` progressive accumulation → half-resolution bright-pass bloom threshold (`bloomThresholdKernel`) → 2-pass separable 9-tap Gaussian blur (`bloomBlurKernel`) → ACES Filmic tone mapping + subtle vignette + sRGB encode (`compositeKernel`).
   - `--compare-out` renders a split comparison frame with an RT-Off raster-style baseline on the left half (`x < width/2`) and full M5 Pro Hardware Ray Tracing on the right half (`x > width/2`).

---

## 4. Measured Apple M5 Pro Benchmarks (1920×1080 @ 32 SPP)

Observed directly on **Apple M5 Pro (20-core GPU, Metal 4)** against `build/metal_wave3/scene/scene.json` (**24 BLASes**, **7,996 triangles**):

| Output | Resolution & SPP | Measured GPU Render Time |
| :--- | :--- | :--- |
| `build/metal_rt_wave3/rt_bmw_m3_gtr_golden_hour.png` | 1920×1080 @ 32 SPP | **0.34 s** |
| `build/metal_rt_wave3/rt_comparison_split.png` | 1920×1080 @ 32 SPP (split) | **0.25 s** |
| `build/metal_rt_wave3/rt_bmw_m3_gtr_night_neon.png` | 1920×1080 @ 32 SPP | **0.36 s** |

---

## 5. Offline CI & Self-Contained Scene Modes

- **Primary (`scene.json`)**: Loads all visible extracted BMW M3 GTR E46 + Enkei wheel meshes from `build/metal_wave3/scene/scene.json` and CC0 4K wet asphalt / carbon fiber textures if present.
- **Direct OBJ (`--scene build/obj_preview`)**: Directly parses raw PS2 Z-up OBJs from `build/obj_preview/`, converts `(-y_ps2, z_ps2 + 0.131551, x_ps2)` into Y-up meters, computes smooth area-weighted vertex normals, and places all 4 wheels at the wheel-arch coordinates.
- **Offline CI (`--synthetic`)**: Generates a clean procedural widebody coupe + rear wing + 4 wheels + headlights/taillights + Rockport plaza without requiring an ISO or extracted `build/` assets.
