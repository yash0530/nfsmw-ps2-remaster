# Native Apple Silicon Metal 2026 PBR Renderer Architecture

> **Status:** Implemented & verified on Apple M5 Pro (`RockportMetalPBR`) — Wave 3 (`v0.4.0` Path 2)  
> **Toolchain:** Pure Apple `swiftc` + Apple system frameworks (`Metal`, `MetalKit`, `AppKit`, `CoreGraphics`, `ImageIO`, `UniformTypeIdentifiers`, `simd`) — zero third-party packages.

---

## 1. Overview & Motivation

While **Path 1** upgrades the retail PS2 `SLUS-21351` disc experience inside PCSX2's Metal backend (6x internal resolution, 4K CC0 baked asphalt, patched `SunInfos` & `LightMaterials`, and 60fps EE overclock), the PS2 Graphics Synthesizer fixed-function pipeline cannot natively execute multi-lobe GGX microfacet BRDFs, dual-lobe automotive clearcoat, screen-space/planar wet-puddle reflections, or `RGBA16Float` HDR bloom + ACES Filmic tone mapping.

**Path 2 (`native_metal/`)** proves the target **2026 Rockport visual benchmark** running natively on Apple Silicon Metal by rendering extracted `SLUS-21351` geometry (`BMWM3GTRE46`, `ENKEI_STYLE01_17_25_B`, `SPOILER_STYLE01_B`) and our `patches/lighting/*.json` lighting profiles directly on the Apple GPU.

---

## 2. Coordinate Space & Scene Assembly (`tools/export_pbr_scene.py`)

PS2 NFSMW `eSolid` (`0x80134010`) meshes store vertices in right-handed **Z-up meters**:
- $+X_{\text{PS2}}$ = forward (front bumper $\approx +2.44\text{ m}$, rear $\approx -2.12\text{ m}$)
- $+Y_{\text{PS2}}$ = left (width $\pm 0.936\text{ m}$)
- $+Z_{\text{PS2}}$ = up (roof $\approx +1.235\text{ m}$)

`export_pbr_scene.py` converts all meshes cleanly into **Y-up right-handed meters** with a proper rotation ($\det = +1$), preserving outward vertex normals and counter-clockwise triangle winding without index reversal:

$$
(x, y, z)_{\text{Metal}} = (-y_{\text{PS2}},\; z_{\text{PS2}} - z_{\text{ground}},\; x_{\text{PS2}})
$$

### Wheel-Arch & Grounding Anchors
1. **Body Wheel-Arch Extraction**: Outer fender lip vertices ($|y_{\text{PS2}}| \ge 0.88 \cdot W_{\text{half}}$, $z_{\text{PS2}} \in [0.22, 0.59]$) determine the front ($x \approx +1.51\text{ m}$) and rear ($x \approx -1.13\text{ m}$) axle centers and top arch height ($z_{\text{arch}} \approx 0.60\text{ m}$), matching `chassis/bmwm3gtre46` VLT `WHEEL_BASE = 2.73 m`.
2. **Wheel Orientation**: `ENKEI_STYLE01_17_25_B` models the outer spokes at $-Y_{\text{PS2}}$ and inner tire barrel toward $+Y_{\text{PS2}}$. Right-side wheels keep default orientation; left-side wheels rotate $180^\circ$ around $Z_{\text{PS2}}$ so spokes face outward on both sides, paired with `FRONT_BRAKE_B` / `REAR_BRAKE_B` rotors & red calipers inside each hub.
3. **Ground Contact**: $z_{\text{ground}} = z_{\text{wheel}} + \min(z_{\text{wheel\_local}})$ shifts the assembled vehicle so all four tires rest cleanly at $Y = 0.000\text{ m}$.
4. **Wing Auto-Detection**: `BMWM3GTRE46_KIT00_BODY_B` integrates the ALMS GT wing directly in the body mesh; `SPOILER_STYLE01_B` is always extracted and registered in `scene.json` (`visible: false` in `auto` mode when the body already includes an integrated wing, or `visible: true` via `--spoiler force` and `--synthetic`).

---

## 3. Material Translation (`eLightMaterial` $\to$ 2026 PBR)

Per-triangle `light_material_index` entries inside `0x00134A01` strip tables resolve against `GLOBAL\GLOBALB.LZC` (`0x00135200` `eLightMaterial` chunks) and map into physically-based surface classes:

| PS2 `eLightMaterial` / Part | PBR Material | Kind | Metallic | Roughness | Clearcoat (IOR 1.5) | Notes |
|---|---|---|---|---|---|---|
| `CARBODY` / `DIABLOHP` | `car_paint_hero` | `car_paint` | `0.85` (silver) / `0.42` (blue) | `0.20` | `1.0` (`0.03`) | Micro-flake jitter + BMW M3 GTR livery |
| `CHROME` | `chrome_trim` | `metal` | `1.00` | `0.055` | `0.0` | Kidney grilles, exhausts, trim |
| `MAGSILVER` | `alloy_rim` | `metal` | `0.96` | `0.14` | `0.45` | Machined Enkei wheel spokes |
| `ALUMINUM` | `brushed_aluminum` | `metal` | `0.94` | `0.22` | `0.15` | Wing struts & ducts |
| `CARBONFIBER2` / `_CF_` | `carbon_fiber` | `carbon_fiber` | `0.08` | `0.28` | `1.0` (`0.045`) | Samples CC0 `carbon_fiber_4k.png` |
| `RUBBER` / `TIRE` | `rubber_tire` | `rubber` | `0.00` | `0.82` | `0.0` | Matte sidewall & tread |
| `WINDSHIELD` | `smoked_glass` | `glass` | `0.02` | `0.03` | `1.0` (`0.02`) | Alpha-blended Fresnel (`0.34` base) |
| `HEADLIGHTREFLECTOR` | `headlight_emissive` | `emissive_headlight` | `0.85` | `0.08` | `1.0` | HDR `[5.5, 6.2, 7.6]` 6000K Xenon |
| `BRAKELIGHT` | `taillight_emissive` | `emissive_taillight` | `0.05` | `0.12` | `1.0` | HDR `[5.2, 0.45, 0.12]` ruby LED |
| `BRAKEDISC` / `CALIPERDECAL` | `brake_rotor` / `brake_caliper` | `metal` / `car_paint` | `0.90` / `0.55` | `0.28` / `0.24` | `0.0` / `0.9` | Steel disc + anodized red caliper |

---

## 4. Metal Render Pipeline & Shading Architecture (`native_metal/Shaders.metal`)

```
[1] Sky Dome Pass ──► [2] Wet Asphalt Ground ──► [3] Opaque Car PBR ──► [4] Blended Glass
        │                                                                      │
        └───────────────────────► RGBA16Float HDR + Depth32Float ◄─────────────┘
                                                │
                         ┌──────────────────────┴──────────────────────┐
                         ▼                                             ▼
            [5] Bloom Threshold (Half-Res)                     Full-Res HDR Scene
                         │                                             │
            [6] 2x Separable 9-Tap Gaussian Blur                       │
                         │                                             │
                         └──────────────────────► [7] ACES Filmic + sRGB Composite
```

### Key Shader Passes
1. **Cook-Torrance GGX + Dual-Lobe Automotive Clearcoat**:
   - Base coat uses Trowbridge-Reitz $D_{\text{GGX}}$, height-correlated Smith-GGX $G$, and Schlick Fresnel $F_0 = \text{mix}(0.04, \text{albedo}, \text{metallic})$.
   - Clearcoat lobe evaluates an independent $\text{IOR} = 1.5$ ($F_0 = 0.04$) specular & IBL reflection layer over smooth geometric normal $N$, attenuating the base coat by $(1 - F_{\text{cc,env}})$ for strict energy conservation.
   - `evaluateBMWLivery` synthesizes anti-aliased twin center stripes and sweeping flank slashes in Y-up model space, toggleable interactively (`L`) or via `--no-livery`.
2. **Procedural Atmospheric Sky & Rockport Skyline IBL**:
   - Driven by sun vectors & layer colors imported from `patches/lighting/rockport2026_golden_hour.json` (`SUNSET`), `rockport2026_daylight.json` (`MIDDAY`), plus a `night_rain` preset.
   - Includes Rayleigh/Mie horizon haze, solar disc + corona, and a mist-blended Rockport downtown waterfront silhouette reflected directly in clearcoat paint, chrome, glass, and puddles.
3. **Wet Asphalt Ground Plane**:
   - Dual-scale sampling of CC0 4K `build/cc0_baked/asphalt_02_wet_4k.png` combined with periodic domain-warped fBm puddle masks ($r_{\text{puddle}} \approx 0.055$, $r_{\text{damp}} \approx 0.38$).
   - Combines 4-wheel + chassis contact AO, directional sun-projected shadow, analytic car reflection occlusion, and wet-road headlight/taillight reflection pools.
4. **Shader Compilation Portability**:
   - `Makefile` compiles `RockportMetalPBR.metallib` via `xcrun metal` when the Metal Toolchain component is installed; otherwise `PBRRenderer.swift` seamlessly compiles `Shaders.metal` at runtime via `MTLDevice.makeLibrary(source:options:)` so the binary executes cleanly on any Apple Silicon macOS machine without extra downloads.
