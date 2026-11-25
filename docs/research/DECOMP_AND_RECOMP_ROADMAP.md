# Dual-Path Native Port & Recompilation Roadmap (`v0.5` – `v1.0`)

> **Project:** NFSMW: Rockport 2026 (`v0.4.0` Wave 3 — Path 2 Architecture)  
> **Primary Reference Target:** *Need for Speed: Most Wanted — Black Edition* (PS2 NTSC-U, `SLUS-21351`, `SLUS_213.51` SHA-1 `60289f3f8b643822a302720f9649ddc4ba55034b`, PCSX2 XOR CRC `0518D274`)  
> **Code Generator:** [`tools/decomp_bridge.py`](../../tools/decomp_bridge.py)  
> **Shader & Material ABI:** [`docs/research/SHADER_BRIDGE_SPEC.md`](SHADER_BRIDGE_SPEC.md)

---

## 1. Executive Summary & Strict License Boundaries

While **Path 1** (*PS2 Max-Ceiling*) pushes the retail Emotion Engine + Graphics Synthesizer binary (`SLUS_213.51`) as far as PCSX2 Metal (`6x` 4K, 60 FPS, VLT, `SunInfos`, BC7 DDS packs, subdivided `_B` geometry, and MIPS code caves) allows, **Path 2** bridges Rockport 2026's verified data, tuning, lighting, and 4K CC0 PBR material pipeline onto **true native Apple Silicon execution** without PS2 GS/VU1 hardware constraints.

Two complementary native upstream ecosystems exist in 2026, each with distinct legal licenses and integration surfaces:

| Track | Upstream Project | Target Binary / ISA | Upstream License | Graphics Stack (macOS) | Integration Strategy for Rockport 2026 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Track A** | `dbalatoni13/nfsmw` | PS2 `SLUS-21351` (plus GC `GOWE69`, PS2 PAL `SLES-53558-A124`, X360 PAL proto, PC `SPEED_EXE_1_3`) | **CC0-1.0** (Public Domain) | Native C++ source port (downstream SDL3 + Metal `MTKView` / `Shaders.metal`) | Direct downstream fork/backend integrating `Rockport2026Config.hpp`, `Rockport2026Lighting.h`, and `bridge_manifest.json` |
| **Track B** | `SpeedBreakerProject/speedbreaker` | Xbox 360 retail ISO via **XenonRecomp** (PowerPC -> C++) | **GPL-3.0** | Vulkan 1.2 -> **MoltenVK** -> Apple Metal (macOS 15+ Sequoia, iOS, Linux/Steam Deck) | Clean-room **data/mod bridge only** (zero GPL code copied into MIT/CC0 repo): endian-aware VLT/SunInfo/Texture pack + external patch/override documentation |
| **Track C** | Complementary 2025–2026 Efforts | PC x86 Recomp (`nfsmwpc-recompiled`), `nfsmw-unity`, NVIDIA RTX Remix | Mixed | Vulkan / Unity / DX9->Vulkan Path Tracing | Shared `bridge_manifest.json` PBR material table (`roughness`, `metallic`, `clearcoat`, `ior`, `emissive`) |

> [!IMPORTANT]
> **Strict License Separation Rule:**
> - `dbalatoni13/nfsmw` is **CC0-1.0**; consuming its public symbol maps (`config/SLUS-21351/symbols.txt`) and structural class layout is compatible with our MIT/CC0 toolchain.
> - `SpeedBreakerProject/speedbreaker` is **GPL-3.0**. Rockport 2026 **never** copies, embeds, or derives source code from GPL-3.0 repositories into our MIT/CC0 repository. All Track B interoperability is strictly at the **interface, file-format, VLT/bChunk data, and texture-asset boundary** ("Bring Your Own Disc").

---

## 2. 2026 Ecosystem State of Play

### 2.1 Track A — `dbalatoni13/nfsmw` (CC0 1:1 Multi-Platform C++ Decompilation)

1. **Scope & Targets:**
   - Targets five exact EA Black Box builds under a unified `src/Speed/...` tree:
     - `GOWE69` (GameCube USA Rev 0)
     - `SLUS-21351` (PS2 USA Black Edition — our exact target ELF!)
     - `SLES-53558-A124` (PS2 PAL Alpha 124 prototype, Sept 20, 2005)
     - `EUROPEGERMILESTONE` (Xbox 360 PAL prototype, Oct 21, 2005)
     - `SPEED_EXE_1_3` (PC v1.3)
2. **Build System & Verification Workflow:**
   - Configured via `python configure.py [--version SLUS-21351]` and built with **Ninja** (`ninja`), requiring user-supplied original binaries placed under `orig/<version>/` (no copyrighted EA/Sony assets in Git).
   - Uses `encounter/objdiff`, Ghidra/DWARF-assisted splitting, and `decomp.dev` progress tracking (overall matching percentage across targets sits in the early-to-mid teens, ~13–15% range, with core math/allocator/render-interface skeletons and `SLUS-21351` symbols thoroughly mapped).
   - Upstream enforces a strict anti-AI-slop policy (no raw LLM output, no fake register/offset matches) and rejects feature/porting PRs until the 1:1 matching base skeleton is complete. Therefore, **native SDL3/Metal rendering work belongs in a downstream Rockport 2026 fork/branch** that consumes upstream matching TUs rather than upstream PRs.
3. **Verified `SLUS-21351` Symbol Grounding (from `symbols.txt`, 15,994 lines):**
   Every hook and class interface listed below is directly verified in `SLUS_213.51`:

| Subsystem / Class | Verified `SLUS-21351` Symbols & EE Virtual Addresses | Source TU | Native Metal / C++ Port Role |
| :--- | :--- | :--- | :--- |
| **`eView` & `eViewPlatInterface`** | `__5eView` (`0x001f84c0`), `CreateViewMatricies` (`0x00206518`), `SetupSceneryCullInfo` (`0x001f12f0`), `Render__18eViewPlatInterfaceP6eModel...` (`0x00209810`), `Render__18eViewPlatInterfaceP5ePoly...` (`0x0020a280`), `eDisplayFrame` (`0x0020b2e8`) | `zEcstasy.cpp` | Replace GS DMA chain submission with Metal command encoder pass (`MTLRenderCommandEncoder`) + `RockportFrameUniforms` |
| **`eModel` & `eSolid` (`eSolidPlatInterface`)** | `Init__6eModelUi` (`0x001f3300`), `ConnectSolid__6eModelP6eSolid` (`0x001f33e8`), `InternalLoaderSolidChunks` (`0x001f4f38`), `LoaderPlatChunks__19eSolidPlatInterfaceP6bChunk` (`0x002164d0`), `epRenderStripsLit` (`0x002092c0`), `epRenderStripsUnlit` (`0x00208e38`) | `zEcstasy.cpp` | Replace PS2 VIF1 strip unpacking (`0x00134A02`) with cached `MTLBuffer` vertex/index uploads using `geometry_inspect.py` / `SHADER_BRIDGE_SPEC.md` layout |
| **`eRenderTarget`** | `eGetRenderTarget__Fi` (`0x001fdad8`), `SetupMatrixPacks__13eRenderTarget` (`0x001fdaf0`), `UpdateGifTags__13eRenderTarget` (`0x001fe000`), `SetScreenBuffers` (`0x001fff68`), `MaybeChangeViewMode` (`0x002014a8`), `CreateEnvMap` (`0x0020f060`), `IsFaceEnabled` (`0x0020f598`) | `zEcstasy.cpp` | Replace `512x448` GS VRAM targets and 5-face reflection targets with `MTLPixelFormatRGBA16Float` HDR render targets + native cubemap / IBL |
| **`IVisualTreatment` & `IVisualTreatmentPlat`** | `UpdateVisualLook` (`0x0040a6d0`), `BlendVisualLookAttribute` (`0x0040a280`), `RenderMWVisualLook` (`0x0021a008`), `ApplyColorBloom` (`0x00219210`), `Radialblur` (`0x00219a68`), `eAttachFog` (`0x00211220`), `eAttachSmear2` (`0x00213da0`) | `zEcstasy.cpp` / `zWorld.cpp` | Replace CLUT8 / sepia sprites with Metal compute/fragment Gaussian HDR bloom + ACES Filmic tone map driven by VLT `visuallook` |
| **`SunInfo` & `LightMaterial`** | `LoaderSun__FP6bChunk` (`0x001fac58`), `SetCurrentSunInfo__FUi` (`0x001facc0`), `SetCurrentSunInfo__Fv` (`0x001fadc0`), `StuffSun` (`0x00386a70`), `LoaderLights` (`0x001f5e00`), `elGetLightMaterial` (`0x001f8408`), `UpdatePlatInfo__27eLightMaterialPlatInterface` (`0x0020e118`) | `zEcstasy.cpp` / `zPlatform.cpp` | Populate `RockportSunUniforms` and `RockportPBRMaterial` directly from `GLOBALB.BUN` + `Rockport2026Lighting.h` |
| **`CarRenderInfo` & `ScenerySectionHeader`** | `CarRenderInfo::Render` (`0x003ec150`), `RenderPart` (`0x003f0688`), `DrawAmbientShadow` (`0x003efbf0`), `ScenerySectionHeader::DrawAScenery` (`0x003dae88`), `mLodLevelDistance` (`0x0058c0e0`) | `zWorld.cpp` / `zTrack.cpp` | Force `LOD A/B` and widen frustum/pixel cutoffs (`Rockport2026Config.hpp`); drive dual-lobe clearcoat paint & emissive lights |
| **`World` & `Sim` (`Scheduler`, `SimSystem`)** | `Scheduler::Init` (`0x00302e80`), `Scheduler::Run` (`0x003030c0`), `SimSystem::SimSystem` (`0x00390bf0`), `SimSystem::UpdateFrame` (`0x00393a88`), `Sim::GetTimeStep` (`0x00394918`), `World_UpdateBody` (`0x0042e940`), `UpdateAllScreenEFX` (`0x003de740`) | `zMain.cpp` / `zSim.cpp` / `zTrack.cpp` | Keep 60 Hz (`1/60f`) fixed-step physics untouched; patch `UpdateAllScreenEFX` `1/30f -> 1/60f` (`0x003de778`) and inject VLT deltas |

---

### 2.2 Track B — `SpeedBreakerProject/speedbreaker` (GPL-3.0 Xbox 360 Apple Silicon Static Recomp)

1. **Architecture & 2026 macOS Status:**
   - Uses **XenonRecomp** to statically translate the Xbox 360 PowerPC executable (`.xex`) ahead-of-time into C++, compiled with LLVM/Clang, CMake, and Ninja into a native Apple Silicon (`arm64`) binary (no CPU emulator overhead).
   - **Windowing, Input, Audio:** Uses **SDL3** for window/controller handling and **FFmpeg** for Xbox 360 XMA audio decoding.
   - **Graphics Backend:** Implements a custom **Vulkan 1.2** renderer (requiring BC/DXT texture support) routed through **MoltenVK** to **Apple Metal** on macOS 15 (Sequoia) or later (plus iOS/iPadOS and Linux/Steam Deck).
   - **macOS User Paths & Launch:** Saves and configuration live under `~/Library/Application Support/SpeedBreaker/save` (requiring unsigned-app Gatekeeper allowance via Privacy & Security on local builds).
   - **Built-in Enhancements (`F1` overlay):** Supports 4K+ internal resolution, unlocked 60+ FPS, native 21:9 ultrawide & 16:10 aspect scaling (Hor+ FOV), HDR10/PQ and Apple EDR output, anisotropic filtering, anti-aliasing, FSR upscaling, and shadow fixes.
   - **Legal Model:** Strictly "Bring Your Own Disc" — an Xbox 360 retail ISO (USA/PAL) is verified file-by-file during setup; zero game assets are hosted.
2. **Why PC Mods Are Incompatible & How Rockport 2026 Bridges the Gap:**
   - Windows PC mods (`.asi` loaders, `ModLoader`, `VltEd` PC scripts, `TexMod`) target **little-endian x86** `speed.exe` memory addresses and PC file layouts. SpeedBreaker runs **big-endian PowerPC** Xbox 360 data structures (`ATTRIBUTES.BIN`, `GLOBALB.BUN`, `TPK` / `XPR2` textures) inside a big-endian virtual memory space translated by XenonRecomp.
   - However, EA Black Box's **core hash keys and schemas are shared across PS2, GameCube, PC, and Xbox 360** (`VLT32` hashes for classes/collections/fields, `bStringHash` for `SunInfos` and `LightMaterials`, and texture name hashes)! That makes our Rockport 2026 data recipes **portable across endianness** via clean-room transcoding.

---

### 2.3 Track C — Complementary 2025–2026 Native & Rendering Efforts

- **NVIDIA RTX Remix (Path Tracing):** Community path-tracing efforts on PC `speed.exe` replace fixed-function/DX9 car & world materials with full PBR (`albedo`, `roughness`, `metallic`, `normal`, `emissive`) while wrestling with EA's baked sepia/bloom post-processing. Our `bridge_manifest.json` PBR material table directly serves both our native Metal PBR renderer (`native_metal/`) and any future RTX Remix / USD material export.
- **`nfsmwpc-recompiled` & `nfsmw-unity`:** PC x86 static recompilation and function-by-function Unity rebuilds validate the same `CarRenderInfo`, `AttribSys`, and `SunInfo` behaviors across platforms, reinforcing the value of keeping Rockport 2026's tuning and material definitions in declarative, platform-neutral JSON + generated C++/Metal headers.

---

## 3. Concrete Engineering Design: Track A SDL3 + Metal Backend (`dbalatoni13/nfsmw`)

### 3.1 Platform Abstraction Layer (`Ecstasy` -> `EcstasyMetal`)

In `dbalatoni13/nfsmw`, platform-specific rendering is isolated behind `*PlatInterface` and `Plat` wrappers (`eViewPlatInterface`, `eSolidPlatInterface`, `eLightMaterialPlatInterface`, `IVisualTreatmentPlat`). Adding a macOS Metal backend requires zero changes to `Sim`, `World`, `GRaceStatus`, or `CarRenderInfo`:

1. **Window & Swapchain (`SDL3` + `CAMetalLayer`):**
   - `SDL_CreateWindow(..., SDL_WINDOW_METAL | SDL_WINDOW_HIGH_PIXEL_DENSITY)` + `SDL_Metal_CreateView()`.
   - Bind `CAMetalLayer` (`pixelFormat = MTLPixelFormatBGRA8Unorm_sRGB` or `RGBA16Float` with `wantsExtendedDynamicRangeContent = YES` for Apple EDR).
2. **Geometry Upload (`eSolidPlatInterface`):**
   - Replace PS2 VU1 VIF strip buffers (`0x00134A02`) with immutable `MTLBuffer` vertex + index buffers built once inside `LoaderPlatChunks__19eSolidPlatInterfaceP6bChunk` (`0x002164d0`).
   - Expand GS triangle-strip restart bits (`ADC` via LFSR or PC/GC index lists) into indexed triangle lists keyed by `(texture_index, light_material_index)` submeshes.
3. **Scene Submission (`eViewPlatInterface::Render` + `eDisplayFrame`):**
   - Each `eView` view pass (`PLAYER1`, `RVM`, 5-face dynamic envmap) encodes draw calls into an `MTLRenderCommandEncoder` targeting an offscreen `RGBA16Float` HDR color + `Depth32Float` buffer.
   - Replace PS2's 5-frame staggered `IsFaceEnabled` (`0x0020f598`) with a single-pass or full-rate 5-face `MTLTextureTypeCube` dynamic reflection probe updated every frame.
4. **Post-Processing (`IVisualTreatmentPlat::RenderMWVisualLook`):**
   - Replace `ApplyColorBloom` (`0x00219210`) sepia sprite + CLUT8 remapping with:
     1. Bright-pass threshold (`luminance > 1.0`, capturing xenon headlights, taillights, and sun speculars).
     2. Separable 2-pass Gaussian HDR bloom (compute or fragment).
     3. ACES Filmic tone-mapping + `visuallook` (`Desaturation`, `BlackBloomIntensity`) from `Rockport2026Config.hpp`.
5. **Compile-Time & Runtime Data Injection (`Rockport2026Config.hpp` + `Rockport2026Lighting.h`):**
   - Apply our 360 verified VLT overrides (`kCameraDeltas`, `kHandlingDeltas`, `kRosterHandlingDeltas`, `kPursuitTrafficDeltas`, `kVisualLookDeltas`) inside `Attrib::Database::Load` by matching `(class_hash, collection_hash, field_hash, index)` rather than fragile PS2 file offsets.
   - Apply `RockportSunUniforms` (`daylight` / `golden_hour`) inside `SetCurrentSunInfo` (`0x001facc0` / `0x001fadc0`) and `RockportPBRMaterial` inside `UpdatePlatInfo__27eLightMaterialPlatInterface` (`0x0020e118`).

---

## 4. Clean-Room Mod Bridge Design: Track B (`SpeedBreakerProject/speedbreaker`)

To bring Rockport 2026's **VLT handling**, **lighting**, and **4K CC0 PBR textures** to SpeedBreaker on Apple Silicon without copying any GPL-3.0 code:

1. **Step 1 — Endian-Neutral VLT (`ATTRIBUTES.BIN`) Transcoding:**
   - Generate a clean-room **Big-Endian VLT Patch Recipe** from `patches/vlt/*.json` using `VLT32` hashes (`class`, `node`, `field`, `index/member`) instead of PS2 byte offsets (`expected_offset`).
   - Using MIT `NFSTools/VaultLib` or a clean-room big-endian `VPAK` parser (reading the user's extracted Xbox 360 `GLOBAL\ATTRIBUTES.BIN` from their BYOD install directory), resolve `(class_hash, node_hash, field_hash)` and write big-endian IEEE-754 floats (`>f`) / `int32` (`>i`) after verifying big-endian `expected_orig`.
   - **Important:** Just like PS2 `GLOBALMEMORYFILE.BIN`, check if Xbox 360 `GLOBALMEMORYFILE.BIN` embeds `ATTRIBUTES.BIN` or `GLOBALB.LZC` and patch both consistently.
2. **Step 2 — `SunInfos` & `LightMaterials` Lighting Bridge:**
   - Xbox 360 `GLOBALB.BUN` stores `SunInfos` (`0x00034202`) and `LightMaterials` (`0x00135200`) with big-endian `bChunk` headers and floats, keyed by the **same `bStringHash` (`0x...`)** values exported in `bridge_manifest.json`!
   - A clean-room big-endian chunk patcher updates `MIDDAY`/`SUNSET` `SunInfos` and `ROAD`/`CHROME`/`METPAINT*`/`WINDSHIELD` `LightMaterials` in the user's local Xbox 360 `GLOBALB.BUN` (or feeds `Rockport2026Lighting.h` presets to any future SpeedBreaker external mod/shader hook).
3. **Step 3 — 4K CC0 Texture Pack Bridge:**
   - Our `tools/dds_encoder_rs` / `tools/build_4k_texture_pack.py` pipeline produces standard 4K mipmapped BC7/BC3/BC1 `.dds` files from 100% CC0 sources (`assets_cc0/`, Poly Haven, ambientCG, procedural HUD).
   - Xbox 360 `TPK` textures use big-endian `TextureInfo` headers and tiled DXT1/DXT5 payloads, but carry the same EA texture name strings and `bStringHash` identifiers (`*ASPHALT*`, `*CONCRETE*`, `*CARBON*`, `*RADAR*`, `*TACH*`). Mapping `assets_cc0/material_pack_rules.json` patterns against Xbox 360 `TPK` hashes lets users inject Rockport 2026 4K surfaces via SpeedBreaker's asset/texture replacement workflow or Vulkan/MoltenVK texture overrides as SpeedBreaker's mod loader matures.
4. **Step 4 — Apple Silicon Verification Checklist:**
   - Run SpeedBreaker on macOS 15+ (Apple Silicon), verify `60+ FPS` + native `16:9` / `21:9` aspect + Apple EDR/HDR output via `F1` menu, confirm `~/Library/Application Support/SpeedBreaker/save` persistence, and test patched VLT handling (`bmwm3gtre46`, `camerainfo/close`) and lighting (`MIDDAY`, `SUNSET`).

---

## 5. Dual-Path Milestones (`v0.5` – `v1.0`)

| Milestone | Target | Concrete Deliverables & Exit Criteria |
| :--- | :--- | :--- |
| **`v0.5` — Shared Bridge & Metal PBR Prototype** *(Wave 3 foundation)* | Path 2 Prototype | `tools/decomp_bridge.py` exports `Rockport2026Config.hpp`, `Rockport2026Lighting.h`, and `bridge_manifest.json`; `native_metal/` renders extracted `SLUS-21351` BMW M3 GTR in 1080p/4K HDR Metal PBR with `daylight` & `golden_hour` presets. |
| **`v0.6` — Multi-Vehicle & Track Segment Metal Viewer** | Path 2 Scene Expansion | Extend `tools/export_pbr_scene.py` + `native_metal/` to stream full Rockport track chunks (`ScenerySectionHeader`) + traffic/cop `CarRenderInfo` assemblies using `bridge_manifest.json` across all 156 `LightMaterials`. |
| **`v0.7` — Track B Big-Endian VLT & Lighting Exporter** | SpeedBreaker BYOD Bridge | Add `--endian big --target x360` mode to Rockport VLT/lighting tools (clean-room MIT/CC0) to emit big-endian `ATTRIBUTES.BIN` & `GLOBALB.BUN` patch recipes + 4K CC0 DDS pack compatible with Xbox 360 BYOD assets. |
| **`v0.8` — Track A `EcstasyMetal` Backend Prototype** | `dbalatoni13/nfsmw` Fork | Implement SDL3 + `CAMetalLayer` stub for `eViewPlatInterface`, `eSolidPlatInterface`, and `IVisualTreatmentPlat` linking `Rockport2026Config.hpp` & `Rockport2026Lighting.h` as upstream `SLUS-21351` TUs reach playable boot. |
| **`v0.9` — Full Sim + Audio + Metal PBR Integration** | Track A Playable Alpha | Connect `Scheduler` (60 Hz) + `SimSystem` + CoreAudio/SPU-ADPCM decoder + `Shaders.metal` HDR/IBL pipeline on Apple Silicon with 4K CC0 PBR normal/roughness maps (`asphalt_02`, `concrete_wall_004`, `Fabric004`). |
| **`v1.0` — Native Rockport 2026 Apple Silicon Release** | Unified Native Edition | Universal Apple Silicon (`arm64`) Metal 3+ remaster experience: 120 Hz ProMotion, Apple EDR HDR, dual-lobe automotive clearcoat + procedural wet asphalt, full 39-car VLT handling, and clean BYOD disc verifier. |
