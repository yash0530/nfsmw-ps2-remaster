# NFSMW: Rockport 2026 — Engineering Roadmap

> **Mission:** Transform *Need for Speed: Most Wanted — Black Edition* (PS2, `SLUS-21351`) across BOTH maximum-ceiling PCSX2 hardware remastering (Track 1, frozen at `v0.5.0`) AND a full-island, 120 FPS native Apple Silicon **Metal 4 Hardware Ray Tracing** playable experience (Track 2, **`v1.4.0` ALL MILESTONES DONE**) — 100% clean-room, open-source, and non-destructive.

**See also:** [Live Progress Table (`docs/PROGRESS.md`)](PROGRESS.md) · [Player's Guide (`docs/PLAY_GUIDE.md`)](PLAY_GUIDE.md)

---

## PS2 / PCSX2 Reality vs. Native Metal 4 RT Delivery

Every milestone below is grounded in **measured `SLUS-21351` binary truth and independent adversarial QA** (`8.7` → `9.1` → `9.2` → `9.3` → `9.5` → `9.4 / 9.8 4K` → **`9.5 / 10` Grand Finale**, `0 P0`, `0 P1`).

| Dimension | PS2 / PCSX2 Hard Reality | Rockport 2026 Delivery (`v1.4.0` Final Status) |
| :--- | :--- | :--- |
| **Resolution, Frame Rate & Photo Mode** | 480i interlaced 4:3 output | **Done (`v0.5.0`):** PCSX2 Metal 6× (4K); **Done (`v1.0–v1.4`):** Native Apple Silicon **`101–124 FPS` 6-district Frame-Gen** @ `1080p` + **`9.8 / 10` 4K (`3840×2160`) 64-SPP RT Thin-Lens Bokeh Photo Mode** (`0 MB` extra VRAM) |
| **Lighting, Sky, Water & Shadows** | Heavy 2005 olive/brown cast, dry roads, synthetic light grids, static water | **Done (`v0.5.0`):** 293-edit `forza_ultra` + planar wet reflections; **Done (`v0.8–v1.4`):** 24h solar/lunar sky + live rain/concentric ripples + **`4,309` real Rockport lights w/ 1-ray RIS shadows** + volumetric headlights + **6-wave Gerstner animated harbor waves & shoreline foam** |
| **Full-Island Streaming, PVS & Foliage** | 4 MB GS VRAM; blocky low-poly trees; short draw distance | **Done (`v0.7–v1.4`):** All **`718` Rockport sections** (`6.66 × 6.27 km`) BC7-compressed in `.rkpt` v2 + **`435` EA PVS zones (`2–4.8 km` highway & bay vistas)** + **`8,740` sculpted botanical trees/bushes/beachgrass** in **`306.0 MB` VRAM** |
| **Car Fleet & Geometry** | 3–4K poly PS2 car bodies; 23-char truncated TPK names | **Done (`v0.5.0`):** 84-car wheel-safe VU1 fleet (`2.83M` tris); **Done (`v0.8–v1.4`):** On-demand **40-car playable garage** (`tools/extract_rt_car.py`) w/ `bstringhash` optics, seam-locked adaptive PN subdivision & **`15/15` Top-15 flagship visual QA (`9.5 / 10`)** |
| **Physics, Audio, Music, Events & Pursuits** | PCSX2 cannot expose native BVH, granular synth, or RT cop strobes | **Done (`v0.7–v1.3`):** `48`-car VLT physics, NOS flames, Speedbreaker, PS5/Xbox controller, **EA-XAS v0 Ginsu PSOLA** audio + **26 EA TRAX + 14 pursuit tracks (`MW_MUSIC.MUS`)** + sky-ray **`AVAudioUnitReverb` tunnel echo**, **20 live AI/cops** w/ RT strobes, **15 Blacklist Boss Duels / 18 Sprints / 10 Speedtraps**, O(1) `6,538`-road **World Map**, **drift VFX**, and **5-cam rig incl. GT3 cockpit** |

---

## Completed Milestones — Wave 1 Core (`v0.1.0` – `v1.0.0`)

### Phase 1–4 — `v0.1.0` – `v0.4.0` Clean-Room Foundation *(Done — `ef5e714`..`a3dc4de`)*
- Verified `SLUS-21351` ISO9660/UDF, `ZDIR.BIN` (`1,294/1,294` CRC32), `JDLZ` codec, `VPAK` VLT (`56/56` classes) + `GLOBALMEMORYFILE.BIN` sync, `19` MIPS 60 Hz patches, `GLOBALB.LZC` lighting editor, `916`-tex TPK decoder, PS2 `GEOMETRY.BIN` + GF(2) VU1 LFSR ADC compiler, native `arm64` Rust BC7 DDS encoder, and automated clean-room hygiene CI (`tools/audit_repo_clean.py`).

### Phase 5 — `v0.5.0` Track 1 PS2/PCSX2 Flagship (`8.9 / 10 PASS`, Frozen) *(Done — `b1bff22`)*
- Wheel-safe **84-car VU1 fleet (`2.83M` tris, `0` VU1 errors)**, `293`-edit `forza_ultra` lighting + `always_wet_road_reflections` & `high_detail_reflection_scenery` (`2.67×`), `170` 4× BC7 world textures (`0` leaks, `0x80` alpha), 20 Hz PINE RAM autopilot, and 4-panel adversarial MP4 Video QA (**`8.9 / 10`**, `0 P0`, `0 P1`). Track 1 is frozen at this verified milestone.

### Phase 6 — `v0.6.0` Real Hardware-Ray-Traced Rockport City Pilot (`8.7 / 10 PASS`) *(Done — `5fccacb`)*
- Streaming binary world extractor (`tools/export_rockport_world.py`) packing Rosewood (`25` secs) & Downtown (`28` secs) into `.rkpt` v1 + native Metal 4 / MSL 4.0 + `MTLFXTemporalDenoisedScaler` MXU neural denoising at `52.9–57.0 FPS` @ `1080p` (`8.7 / 10`).

### Phase 7 — `v0.7.0` Full-Map `.rkpt` v2, 127 FPS Frame-Gen, VLT Physics & Ginsu Audio (`9.1 / 10 PASS`) *(Done — `2558838`)*
- Multi-threaded Rust BC7 `--batch` encoder (`1,497` tex in `1.23s`, `124.6 MB/s`) + **`rockport_full.rkpt` v2 (`718` secs, `4,385` `WRoadNetwork` nodes, `6,538` segments, `4,309` real lights, `6` landmarks)** + botanical Italian cypresses & crisp narrow-axis upscaled sidewalks.
- Standalone post-ACES, pre-HUD **`MTLFXFrameInterpolator`** + split `RTHud.metal` (`126.7–127.4 FPS` @ `1080p`, zero HUD warp) + trunk cove, splitter tab, and `simTimeSec` raindrop ripple fixes (**`9.1 / 10`**, `0 P0`, `0 P1`).
- Gameplay pack (`tools/export_gameplay_pack.py`, `8.52 MB`): `48` VLT vehicles, `9` EA-XAS v0 Ginsu engine pairs + `20` SPU-ADPCM SFX, `7` grounded AI/cop archetypes, standalone `RTPhysics.swift` & lock-free `RTAudioEngine.swift`.

### Phase 8 — `v0.8.0` Full-Island Streaming, 24h Dynamic Sky, 40-Car Garage & NFSMW HUD (`9.2 / 10 PASS`) *(Done — `c074134`)*
- `RTWorldLoader.swift` v2 BC7 mip-chain upload + `MTLResidencySet` + `RTWorldStreamer.swift` XZ AABB hysteresis (`350/430m` + panorama ring, **`301.5 MB` VRAM**, `63–68 FPS` native / `112–128 FPS` Frame-Gen across all 6 districts).
- Real **`4,309`-light `128×128×8` XZ light grid**, live 24h solar/lunar sky + rain buildup + analytical volumetric headlight beams, **40-car on-demand extractor (`tools/extract_rt_car.py`)** + live `G`/`Shift+G` hot-swap, and `RTGameWorld.swift` + post-FI NFSMW HUD & rotating minimap (**`9.2 / 10`**, `0 P0`, `0 P1`).

### Phase 9 — `v0.9.0` Live 20-AI Traffic, Heat 1–5 Pursuits, RT Cop Lightbars & Ginsu Audio (`9.3 / 10 PASS`) *(Done — `6c77d6d`)*
- Full live playable loop integrating `RTPhysics`, `RTAudioEngine` (`AVFoundation` + `GameController`), `RTGameWorld`, `RTAIVehicleLoader.swift` (7 deduplicated AI/cop RT BLASes), hill-hugging `aiRoadProbeKernel`, flashing red/blue RT cop lightbar sweeps, dual Mach-diamond NOS flames, burnt-amber Speedbreaker, and verified 6-district drive bench (**`9.3 / 10`**, `0 P0`, `0 P1`, `103–115 FPS` Frame-Gen w/ 20 live AI/cops, `305.3 MB` VRAM).

### Phase 10 — `v1.0.0` Grand Release, Dead-Code Prune & Player Guide (`9.3 / 10 PASS`) *(Done — `0c6d87d`)*
- Final `4K` (`9.5–9.6 / 10`) + `1080p` Frame-Gen (`105–128 FPS`, `305.3 MB` VRAM) flagship showcase (`renders_v10/`) graded **`9.3 / 10` (`0 P0`, `0 P1`)**, legacy raster PBR prototype prune (`~2.2K LOC`), one-command `Makefile` launchers, complete [`docs/PLAY_GUIDE.md`](PLAY_GUIDE.md), and full live + offline CI verification.

---

## Completed Milestones — Wave 2 Frontier Expansion (`v1.1.0` – `v1.4.0`)

### Wave 2.1 — `v1.1.0` Real `MW_MUSIC.MUS` EA TRAX Soundtrack & Sky-Ray Tunnel Reverb (`9.3 / 10 PASS`) *(Done — `d71090e`)*
- Clean-room 32 kHz Stereo EA-XA v2 (`adpcm_ea_r3`) decoder (`tools/export_music_pack.py`) decoding **26/26 licensed EA TRAX** (fixing the `MW_MUSIC.MPF` `ev[10..12]` permutation) + **14 interactive pursuit score tracks** into gitignored `build/music_pack/`, `.m4a`/`.caf`/`.aac`/`.mp3` CI hygiene bans, post-FI `[EA TRAX]` + `[REVERB]` OLED banner (`[`/`]`/`U`), and upward sky-occlusion ray **`AVAudioUnitReverb`** tunnel acoustics (**`9.3 / 10`**, `107–145 FPS` Frame-Gen, `0 P0`, `0 P1`).

### Wave 2.2 — `v1.2.0` Blacklist 15 Boss Duels, Sprints, Speedtraps & O(1) World Map (`9.5 / 10 PASS`) *(Done — `5920b0e`)*
- Decoded `GLOBAL\GAMEPLAY.BIN` (`272`-vault VLT `VPAK`) + `GLOBALB.BUN` (`0x00030220` 15 Blacklist Boss `PresetCar`s) via `tools/export_events_pack.py` + `RTEventManager.swift`: **15 Blacklist Boss Duels** (`Sonny` #15 → `Razor` #1), **18 Checkpoint Sprints** (3D glowing chevron arches), **10 Speedtraps** (holographic radar laser gates), and an O(1) pre-baked `1024×1024` **`6,538`-road interactive World Map (`M`)** (**`9.5 / 10`**, `9.6/10` World Map, `111–143 FPS` Frame-Gen, `0 P0`, `0 P1`).

### Wave 2.3 — `v1.3.0` GT7 4K RT Photo Mode Bokeh, Drift VFX, 5-Cam Rig & 1-Ray RIS Shadows (`9.4 / 10`, `9.8 / 10` 4K PASS) *(Done — `2f9fcaa`)*
- 6-DoF GT7 Photo Mode (`O`) with physically authentic 7-blade hardware-RT thin-lens DoF bokeh and zero-extra-VRAM `2×2` tiled **4K 64-SPP** snapshots (**`9.8 / 10`**), `RTVFXSystem.swift` analytical RT drift skidmarks, ballistic sparks & volumetric tire smoke (`6,720 B` buffer, zero TLAS churn), 5-camera rig (`C`) incl. real-RT Hood Cam & post-FI GT3 carbon steering wheel + 10-LED shift-bar Cockpit Cam (`9.4 / 10`), and **1-ray RIS real streetlamp shadows** (**`9.4 / 10`**, `101–114 FPS` Frame-Gen, `0 P0`, `0 P1`).

### Wave 2.4 — `v1.4.0` Full-Island Visual Ultra Grand Finale (`9.5 / 10 PASS`) *(Done — `v1.4.0`)*
- **EA PVS (`0x80034150`) `435`-Zone Vistas**: `2–4.8 km` highway & cross-bay skyline visibility streaming (`J` toggle, default ON) with dynamic local clamp keeping slots within `8,192`.
- **All-District Botanical Foliage**: `8,740` city-wide sculpted botanical trees, bushes, hedges & coastal beachgrass across Rosewood, Downtown & Camden Beach (`6` deduplicated BC7 archetypes).
- **6-Wave Directional Gerstner Harbor Waves**: Animated coastal water normals, Beer-Lambert deep-bay absorption, pier shoreline foam & sun/moon glints (`9.6 / 10` 4K Camden Harbor).
- **Top-15 Flagship Car QA**: Permanently cured Carrera GT fender-door seam (`8.8 → 9.5 / 10` 4K) & RX-7 headlight housing; verified `15/15` iconic NFSMW cars spotless on a 4K contact sheet (`9.5 / 10`) — **`9.5 / 10` Grand Finale (`9.55 / 10` 4K Stills, `0 P0`, `0 P1`, `101–124 FPS` 6-district Frame-Gen, `306.0 MB` VRAM)**.
