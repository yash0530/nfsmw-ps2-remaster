# NFSMW: Rockport 2026 — Engineering Roadmap

> **Mission:** Transform *Need for Speed: Most Wanted — Black Edition* (PS2, `SLUS-21351`) across BOTH maximum-ceiling PCSX2 hardware remastering AND native Apple Silicon **Metal 4 Hardware Ray Tracing** in real streamed Rockport City — 100% clean-room, open-source, and non-destructive.

---

## PS2 / PCSX2 Reality vs. Native Metal 4 RT Vision

Every milestone below is grounded in **measured `SLUS-21351` binary truth and independent adversarial QA** — pushing past 2005 boundaries on both tracks with total technical honesty.

| Dimension | PS2 / PCSX2 Hard Reality | Rockport 2026 Strategy (`v0.6.0` Status) |
| :--- | :--- | :--- |
| **Resolution & Aspect Ratio** | 480i interlaced 4:3 output; upscaling exposes bloom, reflection, and HUD alignment artifacts | **Done (`v0.5.0`):** PCSX2 **Metal (`Renderer = 17`)** 6× (4K) + 16× AF + native `VideoSettings::Default` 16:9 (`0x0054BA38 = 0.75f` + `0x00AAFE54`) or 21:9 Ultrawide; verified true circular 16:9 HUD gauges (`W/H = 0.99–1.02`) |
| **Lighting, Sky & Wet Roads** | Heavy 2005 olive/brown cast, blown-out direct sun, dim corridors, and dry-only road shader defaults | **Done (`v0.5.0`):** 293-edit `rockport2026_forza_ultra` lighting packed in-place into `GLOBALB.LZC` + `GLOBALMEMORYFILE.BIN`, balanced `ShadeBoost [52,50,54,50]`, plus ELF patches **`always_wet_road_reflections`** & **`high_detail_reflection_scenery`** (`2.67×`). Verified `+20%` corridor luma (`70.88`), `0.48%` crush, `0` highlight-clip spikes |
| **World Textures & Alpha** | 4 MB GS VRAM; 4-bit/8-bit CLUT indexed textures; PCSX2 expects exact PS2 GS `0x80` opaque alpha | **Done (`v0.5.0`):** Detail-preserving 4× local world enhancer (`tools/enhance_world_dumps.py`) + native `arm64` Rust BC7 DDS encoder (`170` BC7 DDS in `world_enhanced_v2`, up to `2048×2048`) preserving exact PS2 GS `0x80` alpha with **`0` UI/icon/livery leaks** |
| **Car Geometry (VU1)** | 3–4K poly PS2 car bodies; `GLOBALMEMORYFILE.BIN` caches shared `BRAKES`, `WHEELS`, `SPOILER*`, `ROOF` chunks | **Done (`v0.5.0`):** Full `GEOMETRY.BIN` compiler + GF(2) VU1 LFSR ADC solver subdividing **84 cars / 968 solids (`708K → 2.83M` tris)** while excluding shared cached parts (`0` VU1 errors; Golf GTI aftermarket wheels & roof scoop 100% intact) |
| **Live Verification & QA** | Blind timed scripts grind walls and miss visual regressions | **Done (`v0.5.0`):** 20 Hz PINE IPC RAM-telemetry autopilot (`tools/pcsx2_pine_driver.py`, `0.389 ms` read) + native AVFoundation MP4 QA pipeline (`tools/video_qa_pipeline.py`) + 4-panel adversarial rubric (`8.9 / 10` PASS) |
| **Real Rockport City Metal 4 RT** | PCSX2 cannot expose hardware ray tracing, BVH physics, or neural denoising to PS2 games | **Done (`v0.6.0` — `8.7 / 10` PASS):** `tools/export_rockport_world.py` extracts authentic **`TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN`** into `.rkpt` v1 (`rosewood.rkpt` `96.59 MiB`, `downtown.rkpt` `109.59 MiB`); `native_metal_rt/` runs **Metal 4 / MSL 4.0** + **`MTLFXTemporalDenoisedScaler` MXU neural denoising** at **`52.9–57.0 FPS` (`1080p`)** live driving with 8-ray GPU BVH road/wall physics |

---

## Completed Milestones (`v0.1` – `v0.6.0`)

### Phase 1 — `v0.1` Foundation *(Done)*
- Ground-truth `SLUS-21351` (NTSC-U Black Edition `v1.01`, CRC `0518D274`) ISO9660/UDF, `ZDIR.BIN` (`1,447` records), `VPAK`, and `SLUS_213.51` ELF forensics; clean-room BYO-ISO legal policy (`ASSETS_LICENSE.md`) and `tools/audit_repo_clean.py`.

### Phase 2 — `v0.2` VLT, MIPS 60 Hz & Archive Packing *(Done)*
- Pure-Python PS2 `VPAK` VLT toolkit (`56/56` classes, `1,156/1,156` fields) + `GLOBALMEMORYFILE.BIN` sync, `94.2%` `ZDIR.BIN` path resolution, `1,294/1,294` verified `bCalculateCrc32` checksums, pure-Python `JDLZ` codec, `ZZDATA3.BIN` append + ISO9660/PVD scaling, and `19` verified MIPS patches (including 60 Hz `UpdateAllScreenEFX`).

### Phase 3 — `v0.3` Lighting, TPK Decoder, Audio & Roster Overhaul *(Done)*
- `GLOBALB.LZC` `SunInfos`/`LightMaterials` editor dual-synced with `GLOBALMEMORYFILE.BIN`, `916`-texture PS2 GS TPK pixel decoder, `ABKC`/`Gnsu`/`SCHl` audio & VU1 `.obj` geometry inspectors, 39-car `full_roster_handling`, and one-command `rockport_cli.py build`.

### Phase 4 — `v0.4` Geometry Compiler, Native Rust BC7 & Metal Track *(Done)*
- PS2 `GEOMETRY.BIN` compiler + GF(2) VU1 LFSR ADC solver + PN-triangle subdivision, SPU-ADPCM/`Gnsu`/`ABKC` audio patcher + CC0 synth, MIPS R5900 code caves + `ps2sdk` C scaffold, zero-dep native `arm64` Rust BC7 DDS encoder, live PCSX2 boot verifier, `native_metal/` PBR showcase, initial `native_metal_rt/` BVH garage, and `dbalatoni13/nfsmw` / `SpeedBreaker` C++20/Metal bridge.

### Phase 5 — `v0.5.0` Live-QA-Verified PS2 Flagship Release (`8.9 / 10 RELEASE PASS`) *(Done)*
- **84-Car Wheel-Safe VU1 Fleet (`2.83M` tris, `968` solids, `151,729` strips, `0` VU1 errors):** Excluded `GLOBALMEMORYFILE`-cached `BRAKES`, `WHEELS`, `SPOILER*`, and `ROOF`.
- **Forza Ultra Lighting (`293` edits) + Planar Wet Reflections:** Added `always_wet_road_reflections` & `high_detail_reflection_scenery` (`2.67×`), balanced `ShadeBoost [52,50,54,50]`, `+20%` corridor luma lift, `0` highlight-clip spikes.
- **Detail-Preserving 4× World Texture Enhancer (`tools/enhance_world_dumps.py`):** `170` BC7 DDS (`world_enhanced_v2`) preserving exact PS2 GS `0x80` alpha with `0` UI/icon/sign/livery leaks.
- **Closed-Loop PINE Autopilot & 4-Panel Adversarial Video QA (`iter_17`):** `808.11 m` driven up to `105.1 mph`, live NOS consumption, clean `17.8 mph` `R` reverse, `4` unique traffic dodges — **QA-1 `9.2`**, **QA-2 `9.1`**, **QA-3 `8.6`**, **QA-4 `8.6`** → **`8.9 / 10` (`0 P0`, `0 P1`)**.

### Phase 6 — `v0.6.0` Real Hardware-Ray-Traced Rockport City (`8.7 / 10 RELEASE PASS`) *(Done)*
- **Streaming Binary World Extractor (`tools/export_rockport_world.py`):** Clean-room parser for `TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN` emitting compact memory-mapped `.rkpt` v1 city archives:
  - **`rosewood.rkpt`**: `96.59 MiB` / `25` sections / `2,575` instances / `339/339` world textures
  - **`downtown.rkpt`**: `109.59 MiB` / `28` sections / `3,498` instances / `345/345` world textures
  - Filters `855` Free-Roam-invisible neon chevron barriers, applies `4×`/`8×` detail-preserving world textures, and computes verified bidirectional boulevard spawns (`120m` fwd / `40m` rear).
- **Native Apple Silicon Metal 4 / MSL 4.0 + `MTLFXTemporalDenoisedScaler` (`native_metal_rt/`):**
  - `[MetalFX] UBFNet/dBFNet fused-MXU path ACTIVE` delivering **`52.9–57.0 FPS` (`~17.7–18.9 ms/frame`) at `1920×1080`** live Rosewood driving on Apple M5 Pro.
  - 8-ray GPU BVH road/wall driving physics, bindless `MTLHeap` + `MTLResidencySet`, grazing-angle 4-tap anisotropic world sampling, perceptual sRGB ACES grading, night-rain glowing storefronts/streetlamps/projectors/raindrop ripples, pure vulcanized tires, and crystal-clear glass headlights.
- **Adversarial Visual QA Progression (`renders_v5`):** **`5.9 → 6.9 → 8.2 → 8.7 / 10` (`0 P0`)** — cleared `v0.6.0` release gate!

---

## Upcoming Roadmap (`v0.6.1` – `v1.0`)

### Phase 6.1 — `v0.6.1` Visual & Driving Polish
- **Native Metal 4 RT Polish (`8.7 → 9.3+ / 10`):** Fix minor M3 trunk license-plate cove surround + remaining P2 shader/prop refinements from `renders_v5`; expand `.rkpt` extraction to Camden Beach & full Rockport seamless district streaming; wire live VLT handling physics & AI traffic splines into `native_metal_rt/`.
- **PS2 / PCSX2 Wave-6 Polish (`8.9 → 9.3+ / 10`):** Close the 10 logged `iter_17` P2s — `world_enhanced_v3` (curbside leaf-gutter/storm-grate strips, spawn-plaza concrete, `POLICE` gantry), PINE high-speed corner caution (`|err| > 11°` @ `> 80 mph`) + 3D Main Menu `frame_01` timing, Quick Race grid / Heat-2 pursuit QA capture, and `librashader` Metal `.slangp` activation once PCSX2 PR `#14433` lands.

### Phase 7 — `v1.0` Full Native Rockport Gameplay & Complete 4K Pack
- Connect live gameplay loops via `dbalatoni13/nfsmw` C++/Metal renderer & `SpeedBreaker` bridge alongside the complete 4K PCSX2 Rockport dump pack.

---

## Milestone Summary

| **Phase** | **Release** | **Headline Outcome** |
| :--- | :--- | :--- |
| **Phase 1** | `v0.1` *(Done)* | Verified `SLUS-21351` forensics, clean-room repo & policy, baseline visual `.pnach` & v0.1 ISO |
| **Phase 2** | `v0.2` *(Done)* | Full PS2 VLT patcher (`56/56` classes, `GLOBALMEMORYFILE` sync), `19` MIPS patches (`60Hz` EFX, High-LOD, 5-face reflections, 3× scenery), `94.2%` ZDIR + `JDLZ` + `ZZDATA3` append, CC0 PBR baker, 4K HUD kit, PCSX2 2.6.3 Metal + CI |
| **Phase 3** | `v0.3` *(Done)* | `GLOBALB.LZC` daylight & golden-hour editor (dual-synced `GLOBALMEMORYFILE`), `916`-texture PS2 GS TPK decoder, `ABKC`/`Gnsu`/`SCHl` audio & VU1 `.obj` inspectors, 39-car handling, `rockport_cli.py build` |
| **Phase 4** | `v0.4` *(Done)* | PS2 `GEOMETRY.BIN` compiler + GF(2) LFSR ADC solver, SPU-ADPCM/`Gnsu`/`ABKC` audio patcher, MIPS code caves + `ps2sdk` C scaffold, native `arm64` Rust BC7 encoder, `native_metal/` PBR + initial `native_metal_rt/` BVH prototype + decomp bridge |
| **Phase 5** | `v0.5.0` *(Done — `8.9/10` PASS)* | Wheel-safe **84-car VU1 fleet (`2.83M` tris)**, **21** MIPS patches (`always_wet_road_reflections`, `high_detail_reflection_scenery`), `293`-edit `forza_ultra` lighting, detail-preserving 4× world enhancer (`170` BC7 DDS, `0` leaks, `0x80` alpha), 20 Hz PINE RAM autopilot, native MP4 4-panel adversarial QA (`8.9/10`, `0 P0`, `0 P1`) |
| **Phase 6** | **`v0.6.0` *(Done — `8.7/10` PASS)*** | **Real Hardware-Ray-Traced Rockport City**: `TRACKS\L2RA.BUN` + `STREAML2RA.BUN` → mmap `.rkpt` v1 (`rosewood.rkpt` `96.59 MiB`, `downtown.rkpt` `109.59 MiB`, `855` chevron barriers filtered, `4×`/`8×` world textures) + **Metal 4 / MSL 4.0** + **`MTLFXTemporalDenoisedScaler` MXU neural denoiser** (`52.9–57.0 FPS` @ `1080p` live driving, 8-ray GPU BVH physics, `5.9 → 6.9 → 8.2 → 8.7/10`, `0 P0`) |
| **Phase 6.1** | `v0.6.1` *(Next)* | M3 trunk plate cove surround & P2 polish, Camden Beach + seamless full-city `.rkpt` streaming, `world_enhanced_v3` & PINE Quick Race QA |
| **Phase 7** | `v1.0` | Full native Apple Silicon gameplay via `dbalatoni13/nfsmw` C++/Metal renderer & `SpeedBreaker` bridge alongside complete Rockport 4K PCSX2 dump pack |
