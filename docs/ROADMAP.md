# NFSMW: Rockport 2026 — Engineering Roadmap

> **Mission:** Transform *Need for Speed: Most Wanted — Black Edition* (PS2, `SLUS-21351`) across BOTH maximum-ceiling PCSX2 hardware remastering (Track 1, frozen at `v0.5.0`) AND a full-island, 120 FPS native Apple Silicon **Metal 4 Hardware Ray Tracing** playable experience (Track 2, `v1.0.0`+) — 100% clean-room, open-source, and non-destructive.

**See also:** [Live Progress Table (`docs/PROGRESS.md`)](PROGRESS.md) · [Player's Guide (`docs/PLAY_GUIDE.md`)](PLAY_GUIDE.md)

---

## PS2 / PCSX2 Reality vs. Native Metal 4 RT Delivery

Every milestone below is grounded in **measured `SLUS-21351` binary truth and independent adversarial QA** — pushing past 2005 boundaries on both tracks with total technical honesty.

| Dimension | PS2 / PCSX2 Hard Reality | Rockport 2026 Delivery (`v1.0.0` Status) |
| :--- | :--- | :--- |
| **Resolution & Aspect Ratio** | 480i interlaced 4:3 output; upscaling exposes bloom, reflection, and HUD alignment artifacts | **Done (`v0.5.0`):** PCSX2 Metal (`Renderer = 17`) 6× (4K) + 16× AF + native 16:9 / 21:9; **Done (`v1.0.0`):** Native Apple Silicon `1080p` @ `103–128 FPS` Frame-Gen & `4K` (`3840×2160`) path-traced showcase |
| **Lighting, Sky & Wet Roads** | Heavy 2005 olive/brown cast, blown-out direct sun, dry-only defaults, synthetic light grids | **Done (`v0.5.0`):** 293-edit `forza_ultra` + planar wet reflections; **Done (`v0.8.0–v1.0.0`):** 24h dynamic solar/lunar sky + live rain/concentric puddle ripples + **`4,309` real Rockport lights** in a `128×128×8` XZ light grid + volumetric headlights |
| **Full-Island Streaming & Textures** | 4 MB GS VRAM; 4-bit/8-bit CLUT indexed textures; partial pilot districts | **Done (`v0.7.0–v0.8.0`):** All **`718` Rockport sections** (`638` spatial + `80` panorama, `6.66 × 6.27 km`, `10,213` meshes, `86,033` instances) packed into **`209.24 MiB` `.rkpt` v2** with **`1,497` BC7+sRGB mipmap** textures (encoded in `1.23s` by native Rust), XZ AABB streaming + `MTLResidencySet` in **`~305 MB` VRAM** |
| **Car Fleet & Geometry** | 3–4K poly PS2 car bodies; 23-char truncated TPK texture names | **Done (`v0.5.0`):** 84-car wheel-safe VU1 fleet (`2.83M` tris); **Done (`v0.8.0`):** On-demand **40-car playable garage** (`tools/extract_rt_car.py`) w/ `bstringhash` headlights, seam-locked adaptive PN subdivision & live `G`/`Shift+G` hot-swap |
| **Physics, Engine Audio & Pursuits** | PCSX2 cannot expose native BVH, granular audio, or RT cop strobes | **Done (`v0.7.0–v0.9.0`):** `48`-car VLT physics (`ATTRIBUTES.BIN`), NOS flames, 0.35× Speedbreaker, PS5/Xbox `GameController`, lock-free **EA-XAS v0 Ginsu PSOLA** audio (`9` engines + M3 straight-cut whine + `20` SPU-ADPCM SFX + Doppler sirens), **20 live AI/cops** with flashing **red/blue RT roof lightbars**, and post-FI NFSMW HUD & rotating minimap |

---

## Completed Milestones (`v0.1` – `v1.0.0`)

### Phase 1–4 — `v0.1.0` – `v0.4.0` Clean-Room Foundation *(Done)*
- Verified `SLUS-21351` ISO9660/UDF, `ZDIR.BIN` (`1,294/1,294` CRC32), `JDLZ` codec, `VPAK` VLT (`56/56` classes) + `GLOBALMEMORYFILE.BIN` sync, `19` MIPS 60 Hz patches, `GLOBALB.LZC` lighting editor, `916`-tex TPK decoder, PS2 `GEOMETRY.BIN` + GF(2) VU1 LFSR ADC compiler, native `arm64` Rust BC7 DDS encoder, and automated clean-room hygiene CI (`tools/audit_repo_clean.py`).

### Phase 5 — `v0.5.0` Track 1 PS2/PCSX2 Flagship (`8.9 / 10 RELEASE PASS`, Frozen) *(Done — `b1bff22`)*
- Wheel-safe **84-car VU1 fleet (`2.83M` tris, `0` VU1 errors)**, `293`-edit `forza_ultra` lighting + `always_wet_road_reflections` & `high_detail_reflection_scenery` (`2.67×`), `170` 4× BC7 world textures (`0` leaks, `0x80` alpha), 20 Hz PINE RAM autopilot, and 4-panel adversarial MP4 Video QA (**`8.9 / 10`**, `0 P0`, `0 P1`). Track 1 is frozen at this verified milestone.

### Phase 6 — `v0.6.0` Real Hardware-Ray-Traced Rockport City Pilot (`8.7 / 10 PASS`) *(Done — `5fccacb`)*
- Streaming binary world extractor (`tools/export_rockport_world.py`) packing Rosewood (`25` secs) & Downtown (`28` secs) into `.rkpt` v1 + native Metal 4 / MSL 4.0 + `MTLFXTemporalDenoisedScaler` MXU neural denoising at `52.9–57.0 FPS` @ `1080p` (`8.7 / 10`).

### Phase 7 — `v0.7.0` Full-Map `.rkpt` v2, 127 FPS Frame-Gen, VLT Physics & Ginsu Audio (`9.1 / 10 PASS`) *(Done — `2558838`)*
- Multi-threaded Rust BC7 `--batch` encoder (`1,497` tex in `1.23s`, `124.6 MB/s`) + **`rockport_full.rkpt` v2 (`209.26 MiB`, `718` secs, `4,385` `WRoadNetwork` nodes, `6,538` segments, `4,309` real lights, `6` landmarks)** + botanical Italian cypresses & crisp narrow-axis upscaled sidewalks.
- Standalone post-ACES, pre-HUD **`MTLFXFrameInterpolator`** + split `RTHud.metal` (`126.7–127.4 FPS` @ `1080p`, zero HUD warp) + trunk cove, splitter tab, and `simTimeSec` raindrop ripple fixes (**`9.1 / 10`**, `0 P0`, `0 P1`).
- Gameplay pack (`tools/export_gameplay_pack.py`, `8.52 MB`): `48` VLT vehicles, `9` EA-XAS v0 Ginsu engine pairs + `20` SPU-ADPCM SFX, `7` grounded AI/cop archetypes, standalone `RTPhysics.swift` & lock-free `RTAudioEngine.swift`.

### Phase 8 — `v0.8.0` Full-Island Streaming, 24h Dynamic Sky, 40-Car Garage & NFSMW HUD (`9.2 / 10 PASS`) *(Done — `c074134`)*
- `RTWorldLoader.swift` v2 BC7 mip-chain upload + `MTLResidencySet` + `RTWorldStreamer.swift` XZ AABB hysteresis (`350/430m` + panorama ring, **`301.5 MB` VRAM**, `63–68 FPS` native / `112–128 FPS` Frame-Gen across all 6 districts).
- Real **`4,309`-light `128×128×8` XZ light grid** (replacing the synthetic grid), live 24h solar/lunar sky + rain buildup + analytical volumetric headlight beams, **40-car on-demand extractor (`tools/extract_rt_car.py`)** + live `G`/`Shift+G` hot-swap, and `RTGameWorld.swift` + post-FI NFSMW HUD & rotating minimap (**`9.2 / 10`**, `0 P0`, `0 P1`).

### Phase 9 — `v0.9.0` Live 20-AI Traffic, Heat 1–5 Pursuits, RT Cop Lightbars & Ginsu Audio (`9.3 / 10 PASS`) *(Done — `6c77d6d`)*
- Full live playable loop integrating `RTPhysics`, `RTAudioEngine` (`AVFoundation` + `GameController`), `RTGameWorld`, `RTAIVehicleLoader.swift` (7 deduplicated AI/cop RT BLASes), hill-hugging `aiRoadProbeKernel`, flashing red/blue RT cop lightbar sweeps, dual Mach-diamond NOS flames, burnt-amber Speedbreaker, and verified 6-district drive bench (**`9.3 / 10`**, `0 P0`, `0 P1`, `57.9 FPS` native / `103–115 FPS` Frame-Gen w/ 20 live AI/cops, `305.3 MB` VRAM).

### Phase 10 — `v1.0.0` Grand Release, Dead-Code Prune & Player Guide (`9.3 / 10 PASS`) *(Done)*
- Final `4K` (`9.5–9.6 / 10`) + `1080p` Frame-Gen (`105–128 FPS`, `305.3 MB` VRAM) flagship showcase (`renders_v10/`) graded **`9.3 / 10` (`0 P0`, `0 P1`)** by independent Visual Critic, legacy raster PBR prototype prune (`~2.2K LOC`), one-command `Makefile` launchers (`make export-rockport-full`, `make play-rockport`, `make play-pursuit`, `make bench-rockport`), complete [`docs/PLAY_GUIDE.md`](PLAY_GUIDE.md), and full live + offline CI verification.

---

## Wave 2 — 10-Hour Frontier Expansion (`v1.1.0` – `v1.4.0`)

| Release | Theme | Planned Deliverables |
| :--- | :--- | :--- |
| **`v1.1.0`** | **EA TRAX Soundtrack & Tunnel Reverb** | Clean-room `MW_MUSIC.MUS` (`adpcm_ea_r3`) + `MW_MUSIC.MPF` exact 26-track licensed + 14 pursuit soundtrack decoder, `RTHud.metal` sliding EA TRAX banner (`[`/`]`/`U`), and upward sky-ray `AVAudioUnitReverb` tunnel/overpass acoustic echo |
| **`v1.2.0`** | **Sprints, Speedtraps, Blacklist Boss Duels & World Map** | `GAMEPLAY.BIN` 272-vault VLT events + `GLOBALB.BUN` `0x00030220` 15 Blacklist Boss `PresetCar` duels, Checkpoint Sprints, Highway 99 Speedtraps, and O(1) GPU-baked `1024×1024` interactive full-island World Map (`M`) |
| **`v1.3.0`** | **GT7 Photo Mode, Drift VFX, Cockpits & RIS Lamps** | 6-DoF Photo Mode (`O`) w/ hardware RT thin-lens DoF bokeh & tiled `4K` 64-SPP snap; analytical RT skidmarks, sparks & tire smoke; Hood, Bumper & Cockpit cameras (`C`); 1-ray RIS real streetlamp shadows |
| **`v1.4.0`** | **Full-Island Visual Ultra & Top-15 Car QA** | EA PVS (`0x80034150`, `435/435` vis-lists) `2–4.6 km` highway vistas (`J`), 6 all-district botanical foliage BLASes (`7,940+` trees/bushes), 6-wave animated Gerstner harbor waves, and Top-15 flagship/Blacklist car visual QA (`>= 9.4 / 10`) |
