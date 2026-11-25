# Open-Source Assets, Apple Silicon Pipeline & BYO-ISO Legal Model

> **Project:** NFSMW: Rockport 2026  
> **Scope:** Legally clean CC0/OFL/MIT asset sourcing, local PyTorch MPS AI upscaling & PBR-to-diffuse baking on Apple Silicon, native `arm64` BC7 DDS encoding, and our DMCA-safe distribution architecture.

---

## 1. Legally Clean Open-Source & CC0 Asset Sources

Every asset committed to the **NFSMW: Rockport 2026** repository must be either **100% original contributor work** or sourced from verified permissive/public-domain libraries tracked in [`ASSETS_LICENSE.md`](../../ASSETS_LICENSE.md).

### CC0 1.0 PBR Textures & HDRIs

| Source | License | Programmatic API / Workflow | Best Use Cases in Rockport 2026 |
| :--- | :--- | :--- | :--- |
| **Poly Haven** | `CC0 1.0` | REST API: `api.polyhaven.com` | Skybox HDRIs, realistic concrete, asphalt, weathered plaster, foliage, metal trims |
| **ambientCG** | `CC0 1.0` | REST API: `ambientcg.com/api/v3/assets` | Tileable road asphalt, sidewalks, brick walls, industrial siding, gravel, painted curb markings |
| **TextureCan** | `CC0 1.0` | Direct catalog fetch | Urban facades, metal grates, wood fences, roof surfaces |
| **CGBookcase** | `CC0 1.0` | Direct catalog fetch | High-res stone, masonry, road grime, concrete slabs |
| **3DTextures.me** | `CC0 1.0` | Direct catalog fetch | Supplemental architectural materials and ground textures |
| **OpenGameArt** *(CC0 filter)* | `CC0 1.0` | Strict `CC0` license filter only | Decals, particle sprites, skies, UI accents |

Using REST APIs (`api.polyhaven.com` and `ambientcg.com/api/v3/assets`) allows our asset pipeline to stay lightweight and reproducible via declarative manifest files rather than bloating Git history with gigabytes of raw texture archives.

### Procedural Automotive Detail Baking in Blender (`bpy`)

For iconic automotive details that benefit from razor-sharp geometric precision rather than photographs, headless **Blender (`bpy`)** scripts can procedurally generate and bake 100% original CC0 textures:

- **Metallic Paint Flake:** Tileable micro-flake albedo/specular sparkle maps for modern car finishes.
- **Drilled & Slotted Brake Rotors:** Anisotropic brushed-steel rotors with crisp cross-drilled ventilation patterns and heat-blued outer edges.
- **Tire Treads & Sidewalls:** High-resolution performance tread blocks with clean AO recessed grooves.
- **Fresnel Headlight & Taillight Lenses:** Optical refraction grids, reflector bowls, and LED-inspired light-bar masks.
- **Wet-Puddle Asphalt Masks:** Procedural macro/micro puddle roughness masks used during PBR-to-diffuse baking to create convincing damp-road sheen variation across Rockport.

### Crisp Vector HUD, Typography & UI Icons

Never upscale blurry 480i HUD textures with photo AI models. Instead, author modern HUD components natively in SVG/vector form and rasterize cleanly at target resolution:

| Category | Approved Libraries / Families | License | Notes |
| :--- | :--- | :--- | :--- |
| **Typography** | **Google Fonts:** `Rajdhani`, `Chakra Petch`, `Exo 2`, `Orbitron` | `SIL OFL 1.1` | Motorsport-inspired geometric display & telemetry typefaces; always bundle each font's `OFL.txt` |
| **UI Icons** | **Tabler Icons**, **Lucide**, **Phosphor** | `MIT` / `ISC` | Razor-sharp vector icons for speedo indicators, pursuit heat badges, NOS, minimap markers, and menu prompts |
| **Game UI Primitives** | **Kenney** | `CC0 1.0` | Clean public-domain gauges, bars, arrows, and racing decals |

### Audio Assets & Explicit Licensing Guardrail

- **Approved Open Audio Sources:**
  - **Freesound** — strictly filtered to **`CC0`** only (exclude CC-BY-NC, Sampling+, and restrictive licenses).
  - **OpenGameArt** — strictly filtered to **`CC0`**.
  - **Kenney** — **`CC0`** UI clicks, chimes, and audio stingers.
- **EXPLICIT WARNING — Sonniss GDC Audio:**
  > **Do NOT redistribute Sonniss GDC raw audio files.** While Sonniss GDC packs are royalty-free when compiled into a commercial/indie game, redistributing raw or standalone Sonniss audio files inside an open-source Git repository or standalone mod asset pack violates Sonniss's redistribution terms. Exclude Sonniss raw assets entirely from Rockport 2026.

---

## 2. Apple Silicon AI Upscaling & PBR-to-Diffuse Pipeline

For textures dumped locally by the user from their own legally owned `SLUS-21351` disc, Rockport 2026 provides an automated **local-only** processing pipeline optimized for Apple Silicon (M5 Pro, 64 GB unified memory).

```text
PCSX2 Local Dumps (user's own disc only — NEVER committed to Git)
       │
       ▼
[1. Classifier] ──► Filter out unstable CLUT vinyl/rim palette explosions
       │            Route HUD/text to Vector SVG pipeline (NO photo AI!)
       ▼
[2. Pre-Process] ──► Alpha-bleed RGB + split Alpha channel separately
       │
       ▼
[3. PyTorch MPS + spandrel] ──► Tiled inference + torch.mps.empty_cache()
       │                        (PBRify / 4x-NMKD-Siax / 4x-Nomos8k)
       ▼
[4. Depth Bake (DeepBump)] ──► Subtle AO / cavity bake into diffuse RGB
       │                       (compensates for PCSX2 diffuse-only slot)
       ▼
[5. Native arm64 DDS Encoder] ──► BC7_UNORM + full internal mipmap chain
       │                          (kram / dds_tool / Cuttlefish / image_dds)
       ▼
~/Library/Application Support/PCSX2/textures/SLUS-21351/replacements/
```

### PyTorch MPS + `chaiNNer-org/spandrel` Best Practices

- **Model Loader:** Use **`chaiNNer-org/spandrel`** to load standard OpenModelDB architectures cleanly inside Python without pulling in heavy GUI dependencies.
- **MPS Environment Guard:** Run with `PYTORCH_ENABLE_MPS_FALLBACK=1` so any neural op lacking a native Metal kernel falls back cleanly to CPU rather than aborting batch runs.
- **Tiled Inference:** Slice larger textures into overlapping tiles (e.g., 256×256 or 512×512 with overlap blending) to keep memory predictable and prevent visible tile boundaries.
- **Allocator Hygiene:** Call `torch.mps.empty_cache()` periodically between images/batches during unattended runs to prevent Apple Silicon MPS heap fragmentation.
- **Alpha Channel Discipline:**
  1. Perform **RGB alpha-bleed** (dilating opaque edge colors into transparent pixels) *before* upscaling so foliage, chain-link fences, and decals never develop dark halo fringes.
  2. **Upscale the alpha channel separately** (using clean edge-preserving upscaling) and recombine it with the upscaled RGB channels prior to mipmap generation and BC7 compression.

### OpenModelDB Model Policy

| Model | License | Role | Policy Status |
| :--- | :--- | :--- | :--- |
| **`PBRify`** | `CC0-1.0` | Material detail & surface realism enhancement | **Approved** (public domain) |
| **`4x-NMKD-Siax`** | `WTFPL` | Versatile general-purpose texture upscaling | **Approved** (ultra-permissive) |
| **`4x-Nomos8k`** | OpenModelDB *(verify exact release license in ledger)* | High-resolution photorealistic world surfaces | **Approved** for local user execution |
| **`4x-UltraSharp`** | **Non-Commercial (NC)** | — | **DO NOT USE** — NC license conflicts with clean open-source tooling policy |

> **Critical Rule — Never Run Photo AI on HUD Text:** Do **not** feed fonts, speedometer numerals, minimap icons, or menu text through photo-trained neural upscalers (`4x-NMKD-Siax`, `4x-Nomos8k`, etc.). Photo models hallucinate organic curves and corrupt legible typography. Always replace HUD and text textures via our vector SVG + `SIL OFL 1.1` font pipeline.

### Baking Perceived PBR Depth with `DeepBump`

Because PCSX2 replaces **diffuse RGBA textures only** (with no native normal/roughness shader channel), flat upscaled textures can still look papery under 2005 vertex lighting.

Running **`DeepBump`** (normal/height estimation from albedo, followed by ambient occlusion and cavity map extraction) — or combining height/AO maps from CC0 PBR materials — lets the pipeline subtly multiply **ambient occlusion and micro-cavity shading** into the diffuse RGB map. Used conservatively, this adds realistic mortar depth in brick walls, groove contrast in asphalt cracks, and sharper definition on vents, grilles, and rims without clashing with dynamic lighting.

---

## 3. Native macOS `arm64` DDS (`BC7_UNORM`) + Mipmap Encoders

All final replacement textures must be encoded to **`.dds` (`BC7_UNORM`) with complete internal mipmap chains** so PCSX2 can stream and precache them cleanly on Metal without mip-less shimmering. Native Apple Silicon (`arm64`) encoder candidates:

| Tool | Language / Acceleration | Strengths |
| :--- | :--- | :--- |
| **`alecazam/kram`** | C++ / Native macOS `arm64` | Fast, production-tested macOS BC7 + mipmap CLI built specifically with Apple platforms in mind |
| **`A-t-l-as/dds_tool`** (`dds_tool-macos-arm64`) | Native `arm64` binary | Lightweight standalone DDS conversion tool with ready-made macOS arm64 builds |
| **`akb825/Cuttlefish`** | C++ (`bc7enc_rdo` + `ispc`) | High-quality rate-distortion optimized (RDO) BC7 encoding accelerated with ISPC SIMD |
| **`image_dds`** | Rust library / CLI | Great option for programmatic integration, automated validation, and pure-Rust CI pipelines |

---

## 4. DMCA-Safe "Bring Your Own ISO" (BYO-ISO) Legal Model

**NFSMW: Rockport 2026** follows the proven clean-room open-source legal model established by projects such as **OpenGOAL**, **Shipwright** (*Ship of Harkinian*), **`PCSX2/pcsx2_patches`**, and **`Nenkai/GT4Hooks`**.

### Strictly Prohibited in Git & Public Releases

1. **Zero Copyrighted EA / Sony Files:** Never commit or distribute `.iso`/`.bin`/`.chd` images, PS2 BIOS files, `SLUS_213.51`, `ZDIR.BIN`, `ZZDATA*.BIN`, `.BUN` chunks, original models, or EA audio.
2. **Zero Derivative AI-Upscaled EA Textures:** An AI-upscaled version of a copyrighted EA texture remains a **legal derivative work**. AI upscaling MUST happen locally on the end user's machine from textures dumped from their own legally owned disc image. Never host or distribute packs of AI-upscaled EA game textures.

### Permitted & Distributed by Rockport 2026

| Artifact Type | Distribution Status | Notes |
| :--- | :---: | :--- |
| Python / Rust / Shell build & extraction scripts | **Committed to Git** (`MIT`) | Operates read-only on user-supplied ISO |
| `.pnach` code patches & PCSX2 `.ini` configs | **Committed to Git** | Clean-room memory/instruction patches & emulator presets |
| Clean-room `ps2sdk` C / MIPS source code | **Committed to Git** (`MIT`) | Compiled locally or shipped as clean original binaries |
| `xdelta3` binary diffs *(optional release path)* | **User-applied BYO-ISO only** | Suitable for multi-GB DVD images when patching cleanly against a user's verified retail ISO (never containing standalone copyrighted assets) |
| 100% original / `CC0 1.0` / `SIL OFL 1.1` / `MIT` assets | **Committed / Released** | Vector HUD, fonts, procedural Blender automotive textures, CC0 PBR replacements, CC0 audio — all logged in [`ASSETS_LICENSE.md`](../../ASSETS_LICENSE.md) |
