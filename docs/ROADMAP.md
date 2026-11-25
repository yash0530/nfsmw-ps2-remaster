# NFSMW: Rockport 2026 — Engineering Roadmap

> **Mission:** Transform *Need for Speed: Most Wanted — Black Edition* (PS2, `SLUS-21351`) into a sharp, responsive, modern open-world racer on Apple Silicon macOS (and cross-platform PCSX2) via a 100% legally clean, BYO-ISO open-source modding toolchain.

---

## PS2 / PCSX2 Reality vs. 2026 Vision

Great remaster engineering starts with total honesty about hardware and emulator constraints. Every phase below is designed around **exact PlayStation 2 and PCSX2 v2.6.3 boundaries** — pushing past them where the architecture allows and refusing to chase gimmicks where it does not.

| Dimension | PS2 / PCSX2 Hard Reality | Rockport 2026 Strategy |
| :--- | :--- | :--- |
| **Resolution & Aspect Ratio** | 480i interlaced 4:3 output; upscaling exposes bloom, reflection, and HUD alignment artifacts | PCSX2 **Metal (`Renderer = 17`)** 6× (4K) internal resolution + 16× AF + GameDB fixes (`bilinearUpscale: 2`, `halfPixelOffset: 5`, `nativeScaling: 2`, `cpuCLUTRender: 1`, `gpuTargetCLUT: 1`) + native `VideoSettings::Default` 16:9 boot (`0x0054BA38 = 0.75f` + BE flag `0x00AAFE54`), plus optional 21:9 Ultrawide |
| **Color Grade & Post-FX** | Heavy 2005 amber/brown wash, smeared motion blur, dense brume haze; legacy `.fx`/GLSL removed in PCSX2 2.0 and no native ReShade on Metal | **v0.2 Done:** Binary-verified ELF switches (Brown Filter, bloom-preserving Motion Blur, optional Bloom/Fog/Brume/Raindrops) + native `visuallook` VLT color-curve tuning + CAS, ShadeBoost & FXAA. **Phase 5:** `librashader` Metal `.slangp` multi-pass shaders (PCSX2 PR `#14433`) |
| **Textures & Materials** | 4 MB GS VRAM; 4-bit/8-bit CLUT indexed textures; PCSX2 replaces **diffuse RGBA only** (no PBR normal/roughness shader channels); CLUT vinyls cause hash explosion | **4K `BC7_UNORM` mipmapped `.dds` replacements** precached into 64 GB Apple Silicon unified RAM; CC0 PBR-to-diffuse baker (`bake_pbr_to_diffuse.py` w/ wet puddles) + local PyTorch MPS `spandrel` upscaling (`PBRify`, `4x-NMKD-Siax`, `4x-Nomos8k`) + `tpk_inspect.py` cataloging `721` embedded textures |
| **HUD & Typography** | Blurry, stretched 480i raster menus and speedometers; photo AI mangles text | **Zero photo AI on HUD.** Procedural 2× supersampled (`4096→2048`) dual-theme HUD kits (`generate_hud_pack.py`) ready to pair with TPK/dump hash matching |
| **Frame Rate (60 FPS)** | Stock hardware runs 30 FPS due to VBlank lockstep when EE+GS exceeds 16.67 ms | **Myth Busted in v0.2:** Disassembly proved retail `SLUS_213.51` has `g_FrameLimiter = 0` and runs `Scheduler`/`Sim` at **native 60 Hz**! **180%+ EE Overclock** + `UpdateAllScreenEFX` (`1/30f → 1/60f`) delivers smooth 60 FPS at 1.0× speed |
| **Gameplay & World Data** | Uncompressed PS2 `VPAK` VLT vaults (`ATTRIBUTES.BIN`, `GAMEPLAY.BIN`) fail in PC tools (`NFS-VltEd`, `OGVI`); `GLOBALMEMORYFILE.BIN` shadows `ATTRIBUTES.BIN` | **v0.2 Done:** Pure-Python PS2 VLT toolkit (`56/56` classes, `1,156/1,156` fields) + `GLOBALMEMORYFILE.BIN` syncing + **4 verified recipes (75 field patches)** for camera, handling/NOS, traffic/pursuits, and `visuallook` |
| **Disc Packing & Size** | Tightly packed `ZZDATA0..3.BIN` with duplicated seek-optimized entries & `bCalculateCrc32` checksums | **v0.2 Done:** `94.2%` `ZDIR.BIN` path dictionary (`1,363/1,447`), `1,294/1,294` verified `bCalculateCrc32` checksums, pure-Python `JDLZ` codec, multi-duplicate updates & `ZZDATA3.BIN` append + ISO9660/PVD scaling |
| **Code Modding & Plugins** | No ThirteenAG `WidescreenFixesPack` plugin for PS2 NFSMW; hand-written hex `.pnach` patches don't scale to complex systems | Ship modular `.pnach` toggles in Phases 1–3; graduate to a **`ps2sdk` compiled C plugin loader** in Phase 4 informed by `dbalatoni13/nfsmw` (1:1 `SLUS-21351` decompilation) |
| **Hard Engine Ceilings** | No native ray tracing, no PBR G-buffer, no dynamic rain system, no physics-engine swap, 32 MB EE RAM limit | Explicitly out of scope; focus engineering on maximum visual clarity, modern handling/camera feel, 60 FPS pacing, and clean moddability |

---

## Phase 1 — v0.1 Foundation *(Completed)*

**Goal:** Establish verified ground-truth forensics, a clean-room legal foundation, and an immediate, toggleable visual baseline for `SLUS-21351` on macOS PCSX2 v2.6.3.

- **Disc & Binary Forensics Completed:**
  - Verified `SLUS-21351` (NTSC-U Black Edition, `v1.01`, ELF CRC `0518D274`, `3.449 GiB`, `1,808,064` sectors) ISO9660/UDF structure (`25` entries, `SLUS_213.51` at LBA `300`, `ZDIR.BIN` at LBA `140141`, `ZZDATA0..3.BIN` at LBAs `140158` / `549758` / `941549` / `1304059`).
  - Confirmed path-based ISO9660 loading (`cdrom0:\NFS\ZDIR.BIN;1`, `cdrom0:\NFS\ZZDATA%d.BIN;1`) with **zero hardcoded ELF LBAs** and ~20 MB trailing disc space + ~900 MB DVD-5 headroom after `ZZDATA3.BIN`.
  - Reverse-engineered exact `ZDIR.BIN` 24-byte `<6I` format (`1,447` records sorted ascending by `bStringHash`) and catalogued all major asset magic signatures (`ABKC`, `00a10300`, `000030b3`, `00401380`, `Gnsu`, `MPCh`, `LOCH`, `SCHl`).
  - Verified `GLOBAL\ATTRIBUTES.BIN` (`703,296` bytes) and `GLOBAL\GAMEPLAY.BIN` (`2,103,680` bytes) uncompressed `VPAK` VLT chunk layouts (`DepN`/`StrN`/`DatN`/`ExpN`/`PtrN`).
  - Verified `SLUS_213.51` single `PT_LOAD` segment (`off=0x1000`, `vaddr=0x00100000`, `filesz=0x4abbdc`, `memsz=0x4d6ed0`, `entry=0x00436128`) and directly checked all baseline `.pnach` patch addresses (`0x0054BA38` / `0x00AAFDD4`, `0x00550D58`, `0x0055014C` / `0x001FEDB8` / `0x001FEDBC` / `0x00550D28`, `0x005500F0`).
- **Repository & Legal Hygiene Established:**
  - Zero-EA-content policy documented in [`ASSETS_LICENSE.md`](../ASSETS_LICENSE.md) following the BYO-ISO legal model.
  - Core architecture, PCSX2 macOS rendering, and open-source asset pipeline documentation authored in [`docs/research/`](research/).
  - Baseline visual `.pnach` suite and initial disc/archive tooling foundation ready for Phase 2 expansion.

**Exit Criteria:** Verified ground-truth specs documented, baseline visual `.pnach` patches validated against `SLUS_213.51`, and zero copyrighted assets present in the repository.

---

## Phase 2 — Visual Overhaul, 4K CC0/AI Texture Pack & PS2 VLT Visual Treatment Editor

**Goal:** Deliver a dramatic generational visual leap ("Rockport Reborn") via a reproducible local 4K texture pipeline, crisp original HUD/typography, and native PS2 VLT Visual Treatment editing.

### Key Deliverables

1. **Pure-Python PS2 VLT Toolkit (`GLOBAL\ATTRIBUTES.BIN` & `GLOBAL\GAMEPLAY.BIN`):**
   - Full lossless parser and writer for uncompressed EA `VPAK` containers (`NpeD`/`NrtS`/`NtaD`/`NpxE`/`NrtP`), filling the gap left by `NFS-VltEd` and `OGVI`.
   - Byte-identical round-trip unit tests on synthetic fixtures and local disc verification.
   - **Visual Treatment Editor:** Export/edit Rockport's time-of-day color curves, bloom thresholds, sky/sun tint, and contrast natively in YAML (shipping both a crisp **"Rockport 2026 Neutral"** preset and an optional **"Classic 2005 Enhanced"** toggle).
2. **ISO & `ZDIR.BIN` Round-Trip Patcher:**
   - Safe read-only source ISO handling (APFS copy-on-write clone on macOS), in-place sector replacement, and `ZZDATA3.BIN` append-relocation with ISO9660 extent updates.
   - Validate runtime behavior of `ZDIR.BIN`'s `secondary_hash` field on real PCSX2 boots.
3. **Apple Silicon 4K Local Texture Pipeline:**
   - **PCSX2 Dump Classifier:** Automatically tags world/road/building/foliage/sky/UI hashes and quarantines unstable CLUT vinyl/rim palette permutations.
   - **Original & CC0 Replacements (Committed/Manifest-Driven):**
     - Vector SVG HUD, tachometer, speedometer, minimap markers, and typography built with `Rajdhani` / `Chakra Petch` / `Exo 2` (`SIL OFL 1.1`) and Tabler / Lucide / Phosphor (`MIT`/`ISC`) + Kenney (`CC0`).
     - CC0 PBR surface replacements fetched via `api.polyhaven.com` and `ambientcg.com/api/v3/assets`.
     - Procedural Blender (`bpy`) automotive detail bakes (metallic flake, drilled/slotted brake rotors, tire treads, Fresnel headlight lenses, wet-puddle asphalt masks).
   - **Local AI Upscaler (User Machine Only):** `chaiNNer-org/spandrel` on PyTorch MPS (`PYTORCH_ENABLE_MPS_FALLBACK=1`, tiled inference, `torch.mps.empty_cache()`, RGB alpha-bleed + separate alpha upscale) using permissive OpenModelDB weights (`PBRify`, `4x-NMKD-Siax`, `4x-Nomos8k` — strictly excluding NC models and HUD text).
   - **Depth & Compression Stage:** Subtle **DeepBump** AO/cavity baking into diffuse RGB, followed by native macOS `arm64` encoding (`alecazam/kram`, `dds_tool-macos-arm64`, `Cuttlefish`, or `image_dds`) to **`BC7_UNORM` `.dds` with internal mipmaps**, deployed to `~/Library/Application Support/PCSX2/textures/SLUS-21351/replacements/` with full RAM precaching.
4. **PCSX2 v2.6.3 macOS Metal Profile:**
   - Curated `SLUS-21351_0518D274.ini` enabling Metal renderer, 5×/6× internal resolution, 16× AF, AMD FidelityFX CAS, ShadeBoost calibration, FXAA, Async Loading, and full RAM Precache.

**Exit Criteria:** Automated before/after screenshot benchmarks showing crisp 4K textures, vector HUD, balanced neutral Visual Treatment, zero mipmap shimmer, and zero streaming stutter on Apple Silicon.

---

## Phase 3 — Gameplay, Physics, Camera FOV, Draw Distance & True 60 FPS MIPS Engineering

**Goal:** Modernize how *Most Wanted* drives, frames the road, and runs at 60 Hz without breaking simulation integrity.

### Key Deliverables

1. **Handling, Pursuit & AI Overhaul (via PS2 VLT YAML Recipes):**
   - Modular handling profiles (`arcade-plus` default + optional `sim-cade` grip/weight-transfer profile) written cleanly into `GLOBAL\ATTRIBUTES.BIN`.
   - Rebalanced pursuit heat escalation, smarter roadblocks, tuned cop density, and reduced AI racer rubber-banding.
   - Traffic density and career pacing tweaks via `GLOBAL\GAMEPLAY.BIN`.
2. **Camera FOV & Draw Distance Reverse Engineering:**
   - Ghidra Emotion Engine analysis + PCSX2 live debugger + cross-referencing symbols from **`dbalatoni13/nfsmw`** (1:1 `SLUS-21351` decompilation).
   - Modernized chase, hood, and bumper camera geometry with smooth speed-sensitive FOV scaling.
   - Extended world LOD and draw-distance thresholds tuned to stay safely within PS2/PCSX2 geometry budgets.
3. **True 60 FPS MIPS Engineering:**
   - Identify and decouple frame-rate-bound simulation loops, physics integrators, timers, and camera smoothing in EE MIPS assembly so 60 FPS runs at **exact 1.0× real-world speed** (eliminating the 2× speedup bug of naive frame-skip hacks).
   - Benchmark **EE Cycle Rate overclock profiles (130%–180%+)** under Rosetta 2 on Apple Silicon across worst-case downtown Heat 5 pursuits and multi-car races.
   - Validate parity against 30 FPS baseline using repeatable telemetry runs (0–100 mph acceleration, top speed, lap splits, pursuit cooldown timers).

**Exit Criteria:** Toggleable handling/camera/LOD patches plus a telemetry-verified, speedup-free 60 FPS patch running stably on M5 Pro.

---

## Phase 4 — Audio, Cars & Track Upgrades + `ps2sdk` C Plugin Loader

**Goal:** Graduate from hex-level `.pnach` patches to a sustainable compiled-C PS2 modding SDK while unlocking deeper audio, mesh, and disc-level content upgrades.

### Key Deliverables

1. **`ps2sdk` Compiled C Plugin Loader:**
   - Introduce a clean-room `ps2dev/ps2sdk` EE C build pipeline (inspired by `ThirteenAG/PCSX2PluginInjector` and `Nenkai/GT4Hooks`) so complex features — dynamic camera controllers, free-cam/photo mode, live HUD telemetry widgets, quick-restart QoL, and conditional 60 FPS hooks — are authored in readable, maintainable C rather than fragile hand-assembled `.pnach` hex blocks.
2. **Audio Toolchain (`ABKC`, `Gnsu`, `SCHl`):**
   - Inspection and clean replacement tooling for EA `ABKC` sound banks, `Gnsu` engine synths, and `SCHl` streamed audio.
   - Optional CC0 audio enhancement pack sourced strictly from **Freesound (CC0)**, **OpenGameArt (CC0)**, and **Kenney (CC0)** — strictly enforcing the ban on redistributing Sonniss GDC raw audio.
3. **Geometry (`00401380`), TPK (`000030b3`) & Disc Mastering Upgrades:**
   - Mature parsers/writers for PS2 `000030b3` (TPK/BIN) bundles and `00401380` geometry/track chunks, referencing `NFSTools/Binary` and `xan1242/NFSMW-PS2-ICE`.
   - Integrate **`N4gtan/mkps2iso`** for full clean disc mastering when expanded content exceeds in-place or trailing `ZZDATA3.BIN` space (~20 MB original tail / ~900 MB DVD-5 headroom).
   - Author new race events/routes on the Rockport map (`GLOBAL\GAMEPLAY.BIN`), original liveries, and investigate stretch-goal car/track geometry upgrades within the PS2's 32 MB EE RAM envelope.

**Exit Criteria:** First `ps2sdk`-compiled C plugin running inside PCSX2 alongside reproducible audio/event disc builds verified on the user's own ISO.

---

## Phase 5 — Next-Gen Shaders via `librashader` Metal & Community Ecosystem

**Goal:** Bring modern multi-pass post-processing to macOS Metal and cement **NFSMW: Rockport 2026** as a durable, cross-platform open-source community project.

### Key Deliverables

1. **`librashader` Metal `.slangp` Multi-Pass Shader Suite:**
   - Once **PCSX2 PR #14433** (`librashader` Metal support) lands, ship custom open-source `.slangp` multi-pass shader presets tailored specifically for Rockport 2026:
     - Filmic ACES-inspired tonemapping & contrast curve
     - Soft perceptual bloom (replacing the crude 2005 PS2 fullscreen blur)
     - Optional stylized presets (*Rockport 2026 Daylight*, *Late-Autumn Gold Remastered*, *Midnight Pursuit*)
2. **Native ARM64 Emulation Readiness:**
   - Track and test native ARM64 JIT milestones (including community **`ARMSX2`** progress and future native macOS ARM64 PCSX2 releases) to unlock even higher EE overclock efficiency and cooler sustained performance on Apple Silicon laptops.
3. **Community Ecosystem & Release Engineering:**
   - Declarative mod-recipe and profile manager (`purist+`, `rockport-2026`, `custom`) with deterministic BYO-ISO builds across macOS, Linux, and Windows.
   - Optional `xdelta3` workflow documentation for local personal archival/sharing of non-copyrighted recipe builds, paired with strict automated CI checks enforcing our zero-EA-content policy.
   - Public contributor guides, format specs (`docs/formats/`), symbol maps synchronized with `dbalatoni13/nfsmw`, and an automated before/after visual regression gallery.

---

## Milestone Summary

| **Phase** | **Target Release** | **Headline Outcome** |
| :--- | :--- | :--- |
| **Phase 1** | `v0.1` *(Done)* | Verified `SLUS-21351` forensics, clean-room repo & policy, baseline visual `.pnach` & v0.1 ISO |
| **Phase 2** | `v0.2` *(Done)* | Full PS2 VLT patcher (`56/56` classes, `GLOBALMEMORYFILE` sync), `19` MIPS patches (`60Hz` EFX, High-LOD, 5-face reflections, 3× scenery), `94.2%` ZDIR + `JDLZ` + `ZZDATA3` append, CC0 PBR wet-road baker, 4K HUD kit, `721` TPK catalog, PCSX2 2.6.3 Metal + CI |
| **Phase 3** | `v0.3` *(Done)* | `GLOBALB.LZC` `SunInfos`/`LightMaterials` daylight & golden-hour editor (dual-synced `GLOBALMEMORYFILE`), `916`-texture PS2 GS TPK pixel decoder + perceptual dump auto-matcher, `ABKC`/`Gnsu`/`SCHl` audio & `0x80134000` VU1 `.obj` geometry inspectors, 39-car `full_roster_handling` (`360` VLT edits), `6` curated presets & one-command `rockport_cli.py build` |
| **Phase 4** | `v0.8` *(Next)* | `ps2sdk` compiled C plugin loader, CC0 audio & event authoring, full Rockport 4K texture replacement dump pack |
| **Phase 5** | `v1.0` | `librashader` Metal `.slangp` multi-pass shaders, ARM64 JIT readiness, full community modding ecosystem |
