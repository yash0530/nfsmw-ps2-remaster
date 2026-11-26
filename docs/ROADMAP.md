# NFSMW: Rockport 2026 — Engineering Roadmap

> **Mission:** Transform *Need for Speed: Most Wanted — Black Edition* (PS2, `SLUS-21351`) into a sharp, responsive, modern open-world racer on Apple Silicon macOS (and cross-platform PCSX2) via a 100% legally clean, BYO-ISO open-source modding toolchain — while laying an honest, rigorous foundation toward native Apple Silicon real-time ray-traced Rockport City rendering.

---

## PS2 / PCSX2 Reality vs. 2026 Vision

Great remaster engineering starts with total honesty about hardware and emulator constraints. Every milestone below is grounded in **measured `SLUS-21351` binary truth and independent 4-panel adversarial video QA** — pushing past 2005 limits where the architecture allows and stating remaining boundaries plainly.

| Dimension | PS2 / PCSX2 Hard Reality | Rockport 2026 Strategy (`v0.5.0` Status) |
| :--- | :--- | :--- |
| **Resolution & Aspect Ratio** | 480i interlaced 4:3 output; upscaling exposes bloom, reflection, and HUD alignment artifacts | **Done (`v0.5.0`):** PCSX2 **Metal (`Renderer = 17`)** 6× (4K) + 16× AF + native `VideoSettings::Default` 16:9 (`0x0054BA38 = 0.75f` + `0x00AAFE54`) or 21:9 Ultrawide; verified true circular 16:9 HUD gauges (`W/H = 0.99–1.02`) |
| **Lighting, Sky & Wet Roads** | Heavy 2005 olive/brown cast, blown-out direct sun, dim corridors, and dry-only road shader defaults | **Done (`v0.5.0`):** 293-edit `rockport2026_forza_ultra` lighting packed in-place into `GLOBALB.LZC` + `GLOBALMEMORYFILE.BIN`, balanced `ShadeBoost [52,50,54,50]`, plus ELF patches **`always_wet_road_reflections`** & **`high_detail_reflection_scenery`** (`2.67×`). Verified `+20%` corridor luma (`70.88`), `0.48%` crush, `0` highlight-clip spikes |
| **World Textures & Alpha** | 4 MB GS VRAM; 4-bit/8-bit CLUT indexed textures; PCSX2 replaces diffuse RGBA only and expects exact PS2 GS `0x80` opaque alpha | **Done (`v0.5.0`):** Detail-preserving 4× local world enhancer (`tools/enhance_world_dumps.py`) + native `arm64` Rust BC7 DDS encoder (`170` BC7 DDS in `world_enhanced_v2`, up to `2048×2048` Rosewood roads/storefronts) preserving exact PS2 GS `0x80` alpha with **`0` UI/icon/livery leaks** |
| **Car Geometry (VU1)** | 3–4K poly PS2 car bodies; `GLOBALMEMORYFILE.BIN` caches shared `BRAKES`, `WHEELS`, `SPOILER*`, `ROOF` chunks | **Done (`v0.5.0`):** Full `GEOMETRY.BIN` compiler + GF(2) VU1 LFSR ADC solver subdividing **84 cars / 968 solids (`708K → 2.83M` tris)** while excluding shared cached parts (`0` VU1 errors; Golf GTI aftermarket wheels & roof scoop 100% intact) |
| **Frame Rate (60 FPS)** | Stock hardware runs 30 FPS due to VBlank lockstep when EE+GS exceeds 16.67 ms | **Done (`v0.2–v0.5`):** Disassembly proved `g_FrameLimiter = 0` & 60 Hz `Scheduler`/`Sim`; `180%+` EE Overclock + `UpdateAllScreenEFX` (`1/30f → 1/60f`) delivers verified `59.95 fps` full-session captures |
| **Live Verification & QA** | Blind timed scripts grind walls and miss visual regressions | **Done (`v0.5.0`):** 20 Hz PINE IPC RAM-telemetry autopilot (`tools/pcsx2_pine_driver.py`, `0.389 ms` read, live RPM/Gear/NOS/road-nav/traffic avoidance) + native AVFoundation MP4 QA pipeline (`tools/video_qa_pipeline.py`) + 4-panel adversarial rubric (`8.9 / 10` PASS) |
| **Native Metal RT Track** | PCSX2 cannot expose hardware ray tracing or PBR G-buffers to PS2 games | **Honest `v0.5.0` State:** `native_metal_rt/` builds on **Metal 3.1** with authentic UV-mapped 3-car PS2 meshes using **offline 64-SPP progressive accumulation on a synthetic viaduct**. **`v0.6.0`** pivots to real Rockport City (`STREAML2RA.BUN` + world TPKs) + real-time temporal/SVGF or MetalFX denoising |

---

## Completed Milestones (`v0.1` – `v0.5.0`)

### Phase 1 — `v0.1` Foundation *(Done)*
- Ground-truth `SLUS-21351` (NTSC-U Black Edition `v1.01`, CRC `0518D274`) ISO9660/UDF, `ZDIR.BIN` (`1,447` 24-byte `<6I` records), `VPAK` (`ATTRIBUTES.BIN`/`GAMEPLAY.BIN`), and `SLUS_213.51` ELF forensics.
- Clean-room BYO-ISO legal policy (`ASSETS_LICENSE.md`) and automated repo hygiene auditing (`tools/audit_repo_clean.py`).

### Phase 2 — `v0.2` VLT, MIPS 60 Hz & Archive Packing *(Done)*
- Pure-Python PS2 `VPAK` VLT toolkit (`56/56` classes, `1,156/1,156` fields) + `GLOBALMEMORYFILE.BIN` syncing, `94.2%` `ZDIR.BIN` path resolution, `1,294/1,294` verified EA `bCalculateCrc32` checksums, pure-Python `JDLZ` codec, and `ZZDATA3.BIN` append + ISO9660/PVD scaling.
- `19` binary-verified MIPS patches (native 16:9/21:9, brown filter & smear removal, High-LOD cars, 5-face reflections, 3× scenery draw distance, 60 Hz `UpdateAllScreenEFX`), CC0 wet-road PBR baker, and dual-theme 4K procedural HUD kit.

### Phase 3 — `v0.3` Lighting, TPK Decoder, Audio & Roster Overhaul *(Done)*
- `GLOBALB.LZC` `SunInfos`/`LightMaterials` editor dual-synced with `GLOBALMEMORYFILE.BIN`, `916`-texture PS2 GS TPK pixel decoder, `ABKC`/`Gnsu`/`SCHl` audio & `0x80134000` VU1 `.obj` geometry inspectors, 39-car `full_roster_handling`, and one-command `rockport_cli.py build`.

### Phase 4 — `v0.4` Geometry Compiler, Native Rust BC7 & Metal Track *(Done)*
- PS2 `GEOMETRY.BIN` compiler + GF(2) VU1 LFSR ADC solver + PN-triangle subdivision, SPU-ADPCM/`Gnsu`/`ABKC` audio patcher + CC0 synth, MIPS R5900 code caves + `ps2sdk` C scaffold, zero-dep native `arm64` Rust BC7 DDS encoder (`tools/dds_encoder_rs/`), live PCSX2 boot verifier, `native_metal/` PBR showcase, `native_metal_rt/` Metal 3.1 BVH garage, and `dbalatoni13/nfsmw` / `SpeedBreaker` C++20/Metal bridge.

### Phase 5 — `v0.5.0` Live-QA-Verified Flagship Release (`8.9 / 10 RELEASE PASS`) *(Done)*
- **84-Car Wheel-Safe VU1 Fleet (`2.83M` tris, `968` solids, `151,729` strips, `0` VU1 errors):** Upgraded the full car roster while excluding `GLOBALMEMORYFILE`-cached `BRAKES`, `WHEELS`, `SPOILER*`, and `ROOF` chunks — 100% preserving aftermarket wheels, brake calipers, and roof scoops.
- **Forza Ultra Lighting (`293` edits) + Planar Wet Reflections:** Added `always_wet_road_reflections` and `high_detail_reflection_scenery` (`2.67×` reflection scenery) ELF patches (`21` total), anti-clipping `rockport2026_forza_ultra.json` lighting, `forza_hero_camera` + cleaned `forza_ultra_look` VLT recipes, and balanced `ShadeBoost [52,50,54,50]`.
- **Detail-Preserving 4× World Texture Enhancer (`tools/enhance_world_dumps.py`):** Replaced error-prone generic texture swaps with native-preserving 4× enhancement (`170` BC7 DDS in `world_enhanced_v2`, up to `2048×2048` Rosewood roads/sidewalks/storefronts) preserving exact PS2 GS `0x80` alpha and achieving **`0` UI/icon/sign/livery leaks**.
- **Closed-Loop PINE Autopilot & 4-Panel Adversarial Video QA (`iter_17`):** Disassembly-verified `PVehicle` (`IEngine 0x1B4`, `ITransmission 0x1BC`, `NOS_ENGAGED 0x180`) + `WRoadNav` autopilot driving `808.11 m` up to `105.1 mph` with live NOS consumption, clean `17.8 mph` open-road reverse, and `4` unique traffic dodges, passing all 4 independent QA panels (**QA-1 `9.2`**, **QA-2 `9.1`**, **QA-3 `8.6`**, **QA-4 `8.6`** → **`8.9 / 10`**, **`0 P0`**, **`0 P1`**).
- **Authentic UV-Mapped 3-Car `native_metal_rt/` Garage:** Upgraded `native_metal_rt/` to render 3 UV-mapped extracted PS2 hero cars on a synthetic viaduct with Metal 3.1 BVH ray tracing (offline 64-SPP accumulation).

---

## Phase 6 — `v0.6.0` Frontier Pivot: Real Rockport City + Real-Time RT Driving

Now that the PS2/PCSX2 `v0.5.0` track has cleared the `>= 8.5 / 10` (`8.9 / 10`) release bar, **`v0.6.0`** pivots aggressively on two fronts:

### 1. Native Metal RT Frontier — From Synthetic Garage to Real-Time Ray-Traced Rockport City
- **Honest Gap Today:** `native_metal_rt/` currently builds with **Metal 3.1** using **offline progressive 64-SPP accumulation** on a **synthetic viaduct** — great for still-frame hero renders, not yet a real-time drivable city.
- **`v0.6.0` Deliverables:**
  1. **Real Rockport City Geometry & World TPK Loader (`STREAML2RA.BUN`):** Clean-room parser/exporter for NFSMW PS2 streamed world geometry sections (`STREAML2RA.BUN` + companion world `TPK` textures + collision/road-nav splines) into streamed GPU BLAS chunks so `native_metal_rt/` renders authentic Rosewood / Downtown / Camden districts instead of a synthetic viaduct.
  2. **Real-Time Temporal / SVGF or MetalFX Driving Denoiser:** Replace offline 64-SPP static accumulation with a **1–2 SPP real-time path/reflection tracer** backed by motion vectors, temporal reprojection + À-Trous **SVGF** filtering (and/or **MetalFX Spatial/Temporal Upscaling & Denoising** on Apple Silicon), targeting locked **60 FPS interactive driving** with hardware-ray-traced wet-asphalt reflections, shadows, and GI.
  3. **Live Camera & Vehicle Physics Prototype:** Couple our exported `ATTRIBUTES.BIN` VLT handling curves (`tools/decomp_bridge.py`) and `WRoadNav` spline graph to an interactive drivable car inside streamed Rockport geometry.

### 2. PS2 / PCSX2 Wave-6 Polish (Closing the 10 Logged `iter_17` P2s → `>= 9.3 / 10`)
- **World Texture Coverage (`world_enhanced_v3`):** Capture and 4×-enhance the remaining unreplaced Central Rosewood curbside leaf-gutter/storm-grate road strips, spawn-plaza concrete, grazing retaining walls, and native-preserving `POLICE` gantry / kiosk textures (`P2-5`–`P2-8`).
- **PINE Autopilot Cornering & Quick Race Coverage:** Add speed-scaled heading caution (`speed > 80 mph` & `|err| > 11°`), dampen post-dodge curb overshoot (`P2-1`, `P2-2`, `P2-10`), delay `frame_01_menu.png` F8 snap until the 3D Main Menu settles (`t≈19.5s`, `P2-3`), and add a Quick Race grid + Heat-2 pursuit QA capture to stress-test multi-car AI/cop LODs in 4K.
- **`librashader` Metal `.slangp` Activation:** Enable `shaders/rockport2026_ssgi.slang` & filmic ACES grading once PCSX2 PR `#14433` lands on macOS Metal.

---

## Milestone Summary

| **Phase** | **Release** | **Headline Outcome** |
| :--- | :--- | :--- |
| **Phase 1** | `v0.1` *(Done)* | Verified `SLUS-21351` forensics, clean-room repo & policy, baseline visual `.pnach` & v0.1 ISO |
| **Phase 2** | `v0.2` *(Done)* | Full PS2 VLT patcher (`56/56` classes, `GLOBALMEMORYFILE` sync), `19` MIPS patches (`60Hz` EFX, High-LOD, 5-face reflections, 3× scenery), `94.2%` ZDIR + `JDLZ` + `ZZDATA3` append, CC0 PBR baker, 4K HUD kit, PCSX2 2.6.3 Metal + CI |
| **Phase 3** | `v0.3` *(Done)* | `GLOBALB.LZC` daylight & golden-hour editor (dual-synced `GLOBALMEMORYFILE`), `916`-texture PS2 GS TPK decoder, `ABKC`/`Gnsu`/`SCHl` audio & VU1 `.obj` inspectors, 39-car handling, `rockport_cli.py build` |
| **Phase 4** | `v0.4` *(Done)* | PS2 `GEOMETRY.BIN` compiler + GF(2) LFSR ADC solver, SPU-ADPCM/`Gnsu`/`ABKC` audio patcher, MIPS code caves + `ps2sdk` C scaffold, native `arm64` Rust BC7 encoder, `native_metal/` PBR + `native_metal_rt/` BVH prototype + decomp bridge |
| **Phase 5** | **`v0.5.0` *(Done — `8.9/10` PASS)*** | Wheel-safe **84-car VU1 fleet (`2.83M` tris)**, **21** MIPS patches (`always_wet_road_reflections`, `high_detail_reflection_scenery`), `293`-edit `forza_ultra` lighting, detail-preserving 4× world enhancer (`170` BC7 DDS, `0` leaks, `0x80` alpha), 20 Hz PINE RAM autopilot, native MP4 4-panel adversarial QA (`8.9/10`, `0 P0`, `0 P1`), UV-mapped 3-car Metal 3.1 RT garage (64-SPP synthetic viaduct) |
| **Phase 6** | **`v0.6.0` *(Next — Frontier Pivot)*** | **Real Rockport City (`STREAML2RA.BUN` + world TPKs)** in `native_metal_rt/` + **real-time temporal/SVGF or MetalFX driving denoiser** + PS2 Wave-6 P2 polish (`world_enhanced_v3`, Quick Race/pursuit PINE QA, `librashader` Metal) |
| **Phase 7** | `v1.0` | Full native Apple Silicon gameplay via `dbalatoni13/nfsmw` C++/Metal renderer & `SpeedBreaker` bridge alongside complete Rockport 4K PCSX2 dump pack |
