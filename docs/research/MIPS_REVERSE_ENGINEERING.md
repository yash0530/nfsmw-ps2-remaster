# Deep `SLUS_213.51` MIPS R5900 Reverse Engineering & Engine Patch Reference

> **Project:** NFSMW: Rockport 2026  
> **Target Binary:** `SLUS_213.51` — *Need for Speed: Most Wanted — Black Edition* (PS2 NTSC-U `v1.01`, `SLUS-21351`)  
> **Binary Fingerprints:** Size `4,918,740` bytes · PCSX2 XOR CRC `0518D274` · SHA-1 `60289f3f8b643822a302720f9649ddc4ba55034b`  
> **Symbol Source:** CC0 1.0 Universal `config/SLUS-21351/symbols.txt` from [`dbalatoni13/nfsmw`](https://github.com/dbalatoni13/nfsmw) (`15,994` lines, exact SHA-1 match)  
> **Disassembler:** Pure-Python MIPS R5900 (PS2 EE) tool at [`tools/elf_disasm.py`](../../tools/elf_disasm.py)

Every address, original 32-bit word, IEEE-754 float, and MIPS R5900 instruction in this document has been directly read and verified against an unmodified `SLUS_213.51` executable extracted read-only from the retail ISO.

---

## 1. ELF Memory Map & Address Translation

`SLUS_213.51` contains a single `PT_LOAD` segment (`e_entry = 0x00436128`):

| Property | Value |
| :--- | :--- |
| `p_vaddr` | `0x00100000` |
| `p_offset` | `0x00001000` |
| `p_filesz` | `0x004ABBDC` (static ELF covers `0x00100000` – `0x005ABBDC`) |
| `p_memsz` | `0x004D6ED0` (EE RAM including `.bss` covers `0x00100000` – `0x005D6ED0`) |
| Translation | `elf_offset = vaddr - 0x00100000 + 0x1000` (`vaddr - 0x000FF000`) |

Reproduce any lookup or disassembly locally (read-only):

```bash
python3 tools/elf_disasm.py fetch-symbols
python3 tools/elf_disasm.py --iso /path/to/SLUS-21351.iso verify-patches patches/elf_patches.json
python3 tools/elf_disasm.py --iso /path/to/SLUS-21351.iso disasm Default__13VideoSettings -n 10
python3 tools/elf_disasm.py --iso /path/to/SLUS-21351.iso xrefs 0x00550D58
```

---

## 2. Key Symbol Table Highlights (`SLUS_213.51`)

| EE vaddr | Symbol | Source TU | Role |
| :---: | :--- | :--- | :--- |
| `0x001FC3E8` | `CalculateH__FUsP5eView` | `zEcstasy.cpp` | Computes perspective focal scale `H = (width/2) / tan(fov/2)` |
| `0x001FDAD8` | `eGetRenderTarget__Fi` | `zEcstasy.cpp` | Returns `0x00552940 + idx * 0xA90` (`idx=1` is `TARGET_PLAYER1` @ `0x005533D0`) |
| `0x001FE000` | `UpdateGifTags__13eRenderTarget...` | `zEcstasy.cpp` | Propagates framebuffer aspect scales `+0x60..+0x6C` to `TARGET_PLAYER1` |
| `0x001FFF68` | `SetScreenBuffers__Fv` | `zEcstasy.cpp` | Initializes render targets (`512x448` NTSC, aspect table `0x0054BA34..0x0054BA60`) |
| `0x002014A8` | `MaybeChangeViewMode__Fv` | `zEcstasy.cpp` | Applies 4:3 vs 16:9 aspect ratios from `VideoSettings.WideScreen` |
| `0x00206518` | `CreateViewMatricies__FP5eView...` | `zEcstasy.cpp` | Builds 3D projection (`0x60`/`0x64` of `eRenderTarget`) and culling frustum (`0x0054E178`) |
| `0x00207F30` | `ServiceRenderStageQueue__Fv` | `zEcstasy.cpp` | VIF1 finish callback; clears `0x0054B5F0`, sets buffer-swap pending `0x0054B5F4=1` |
| `0x002082E0` | `WaitUntilVSync__Fv` | `zEcstasy.cpp` | Busy-waits for 60 Hz VBlank counter `0x0054B5EC` to increment |
| `0x00208350` | `eWaitUntilRenderingDone__Fv` | `zEcstasy.cpp` | Waits until prior frame's VIF1 DMA (`0x0054B5F0`) and VBlank swap (`0x0054B5F4`) finish |
| `0x0020B2E8` | `eDisplayFrame__Fv` | `zEcstasy.cpp` | Main render submission, optional 30 FPS 2-VBlank gate (`0x0054B5D8`), buffer swap |
| `0x0020F598` | `IsFaceEnabled__Fi` | `zEcstasy.cpp` | Selects static 5-face (`0x0054FE50`) vs 5-frame staggered (`0x0054FEE0`) car reflections |
| `0x00211030` | `RainDrop__FP5eViewffff` | `zEcstasy.cpp` | Renders camera-lens raindrop sprites |
| `0x00211220` | `eAttachFog__FP9eDmaChain...` | `zEcstasy.cpp` | Atmospheric fog/brume pass reading `FogModeInfos` (`0x00550028`) and `0x005500F0` |
| `0x00213DA0` | `eAttachSmear2__FP9eDmaChainUif` | `zEcstasy.cpp` | Accumulation motion-blur ("smear") pass controlled by `0x0055014C` and `0x00550150` |
| `0x00219210` | `IVisualTreatmentPlat::ApplyColorBloom` | `zEcstasy.cpp` | Sepia/brown tint (`0x00550D58`) and bloom (`0x00550D5C`) post-processing passes |
| `0x00219818` | `IVisualTreatmentPlat::ApplyDetailMap` | `zEcstasy.cpp` | Post-process detail softening pass |
| `0x00219938` | `IVisualTreatmentPlat::ApplyPulseBrightness` | `zEcstasy.cpp` | Post-process brightness pulse pass |
| `0x00219A68` | `IVisualTreatmentPlat::Radialblur` | `zEcstasy.cpp` | High-speed / NOS radial blur pass |
| `0x0021A008` | `IVisualTreatmentPlat::RenderMWVisualLook` | `zEcstasy.cpp` | Master post-processing dispatcher gated by `0x00550D28` |
| `0x002278C0` | `VOWideScreen::Act` | `zFe.cpp` | Options menu widescreen toggle (`xori 1` on `FEDatabase->0x1c->0x34`) |
| `0x00245808` | `MoviePlayer::Play` | `zFe.cpp` | Checks `SkipMovies` (`0x00574660`) before starting FMV playback |
| `0x00253A78` | `Minimap::AdjustForWidescreen` | `zFe2.cpp` | Adjusts minimap coordinates for native 16:9 HUD |
| `0x0025D788` | `FEngHud::SetWideScreenMode` | `zFe2.cpp` | Switches HUD layout based on `FEDatabase->0x1c->0x34` |
| `0x0026D9F0` | `VideoSettings::Default` | `zFe2.cpp` | Initializes default `VideoSettings` struct (`+0x0C` = `WideScreen`) |
| `0x002B5958` | `GRaceParameters::GetCatchUp` | `zGameplay.cpp` | Controls race AI catch-up / rubber-banding |
| `0x00302E80` | `Scheduler::Init(float)` | `zMain.cpp` | Initialized with `0x3C888889` (`1/60` s) fixed-step period |
| `0x003030C0` | `Scheduler::Run(bool)` | `zMain.cpp` | 60 Hz fixed-timestep simulation accumulator |
| `0x00322CE0` | `PrepareRealTimestep__Ff` | `zMisc.cpp` | Quantizes frame delta time against 60 Hz NTSC (`1/60`) / 50 Hz PAL (`1/50`) |
| `0x00385BB0` | `PSX2VBlank__Fi` | `zPlatform.cpp` | 60 Hz VBlank ISR; invokes `0x002085A0` to increment `0x0054B5EC` & flip display env |
| `0x00390BF0` | `SimSystem::SimSystem` | `zSim.cpp` | Inherits `Scheduler->mTimeStep` (`1/60` s) as base physics step |
| `0x00394918` | `Sim::GetTimeStep` | `zSim.cpp` | Returns `SimSystem->0x24` (`(1/60) * SimSpeed`) |
| `0x003DAE88` | `ScenerySectionHeader::DrawAScenery` | `zTrack.cpp` | World scenery pixel-size culling (`0x003DB110`) & high/low LOD switch (`0x003DB178`) |
| `0x003DE740` | `UpdateAllScreenEFX__Fv` | `zTrack.cpp` | Advances `ScreenEffectDB` once per display frame with hardcoded `1/30f` (`0x003DE778`) |
| `0x003EC150` | `CarRenderInfo::Render` | `zWorld.cpp` | Selects car/tire LOD using `.rodata` tables (`0x00505178`, `0x00505190`) and overrides (`0x0059FF28`, `0x0059FF2C`) |

---

## 3. Binary-Verified Engine Patches (`patches/elf_patches.json` & `.pnach`)

### 3.1 Visual Treatment & Post-Processing (`zEcstasy.cpp`)

Disassembly of `IVisualTreatmentPlat::RenderMWVisualLook` (`0x0021A008`), `ApplyColorBloom` (`0x00219210`), `eAttachSmear2` (`0x00213DA0`), and `eAttachFog` (`0x00211220`) verifies every official PCSX2 March 2026 (`Steelmax` / `fobes`) switch plus additional clean switches:

```mips
; RenderMWVisualLook @ 0x0021A008
0x0021A01C: lw   $v1, 0xd28($v0)        ; -> 0x00550D28 (orig 1): Master Post-Process Enable
0x0021A02C: beq  $v1, $zero, 0x0021A10C ; if 0 -> skip ApplyColorBloom, 0x218D08, ApplyDetailMap, ApplyPulseBrightness

; ApplyColorBloom @ 0x00219210
0x0021931C: lw   $v0, 0xd58($v1)        ; -> 0x00550D58 (orig 0): Skip Brown/Sepia Tint Pass
0x00219344: bne  $v0, $zero, 0x002193A8 ; if != 0 -> skip sepia eAttachTargetMoveSprites
0x002193A8: lw   $v1, 0xd5c($v0)        ; -> 0x00550D5C (orig 0): Skip Bloom Pass
0x002193AC: bne  $v1, $zero, 0x002197D8 ; if != 0 -> return early (skip bloom)
0x002196FC: lw   $v0, 0xd58($a1)        ; second check of 0x00550D58: neutralizes bloom tint RGBA

; eAttachSmear2 @ 0x00213DA0 (Motion Blur / Nitrous Smear)
0x00214184: lw   $t3, 0x150($v0)        ; -> 0x00550150 (orig 6): SmearNumSubStages (0 = off)
0x00214188: blez $t3, 0x00214238
0x002146CC: lw   $v0, 0x14c($v1)        ; -> 0x0055014C (orig 1): SmearEnable (0 = skip DMA draw)
0x002146D0: beq  $v0, $zero, 0x00214DE0

; eAttachFog @ 0x00211220
0x00211324: lw   $v1, 0xf0($v0)         ; -> 0x005500F0 (orig 1): Brume / Fog Volume Enable
0x00211404: addiu $a2, $a2, 0x28        ; -> 0x00550028 (FogModeInfos[0], orig 0x01000100 -> 0x01000000 clears byte +1)
```

> **Important Bug Fixed vs. v0.1 (`patches/elf_patches.json`):**  
> Legacy PeterDelta `[Remove Blur]` wrote `0x00550D28 = 0`, which is actually the **master post-processing kill switch** in `RenderMWVisualLook`. Putting `0x00550D28 = 0` inside `remove_motion_blur` unintentionally disabled bloom, detail softening, and pulse brightness even when users only wanted to remove motion blur. In `v02`, `remove_motion_blur` cleanly patches `0x00550150 = 0` + `0x0055014C = 0` (preserving bloom), while `disable_all_post_processing` (`0x00550D28 = 0`), `disable_bloom` (`0x00550D5C = 1`), `disable_radial_blur` (`0x00219A68: jr $ra; nop`), and `disable_screen_raindrops` (`0x00211030: jr $ra; nop`) are cleanly separated toggles.

### 3.2 True Native 16:9 Widescreen, 21:9 Ultrawide & Wider Camera FOV

Disassembling `VideoSettings::Default`, `VOWideScreen::Act`, `MaybeChangeViewMode`, `UpdateGifTags`, `eGetRenderTarget`, and `CreateViewMatricies` resolved every long-standing address discrepancy between standard `SLUS-21267` and Black Edition `SLUS-21351`:

1. **Runtime Widescreen Flag (`0x00AAFE54` vs. wrong `0x00AAFDD4`):**
   - `0x0056F0EC` holds `FEDatabase*`; `FEDatabase->0x1c` is the heap-allocated active `UserProfile*`; offset `+0x28` is `VideoSettings`; offset `+0x34` (`+0x28 + 0x0C`) is `int WideScreen` (`0` = 4:3, `1` = 16:9).
   - `VOWideScreen::Act` (`0x00227904`), `MaybeChangeViewMode` (`0x0020152C`), `eDisplayFrame` (`0x0020B918`), and `FEngHud::SetWideScreenMode` (`0x0025D7A8`) all read `*(FEDatabase->0x1c + 0x34)`.
   - On Black Edition `SLUS-21351`, this heap address is **`0x00AAFE54`** (whereas `0x00AAFDD4` in v0.1 was carried over from standard `SLUS-21267` and pointed 0x80 bytes too early!).
2. **Static Auto-Default Widescreen ON (Standalone Modded ISO Support, Zero `.pnach` Needed):**
   - `Default__13VideoSettings` (`0x0026D9F0`) originally zeroed `WideScreen` (`0xc($a0)`).
   - Felixthecat1970's community patch wrote `swc1 $f0, 0xc($a0)` (`0x3F800000` float 1.0f into an integer bool), which breaks the Options menu toggle (`xori $v0, $v0, 1` turns `0x3F800000` into `0x3F800001`, still truthy!).
   - Our 3-word static patch stores `1.0f` at `0x0($a0)` directly via GPR `$at` and stores exact integer **`1`** at `0xc($a0)` in the exact same 7-instruction footprint:
   ```mips
   ; Original Default__13VideoSettings (0x0026D9F0..0x0026DA08):
   0x0026D9F0: 3C013F80  lui   $at, 0x3f80
   0x0026D9F4: 44810000  mtc1  $at, $f0
   0x0026D9F8: AC80000C  sw    $zero, 0xc($a0)   ; WideScreen = 0
   0x0026D9FC: E4800000  swc1  $f0, 0x0($a0)     ; Brightness = 1.0f

   ; Patched Default__13VideoSettings (clean integer bool 1):
   0x0026D9F0: 3C013F80  lui   $at, 0x3f80       ; (unchanged)
   0x0026D9F4: AC810000  sw    $at, 0x0($a0)     ; Brightness = 1.0f via GPR
   0x0026D9F8: 24010001  li    $at, 1            ; exact integer 1
   0x0026D9FC: AC81000C  sw    $at, 0xc($a0)     ; WideScreen = 1 (menu xori 1 works!)
   ```
3. **Why Static `0x0054BA38` Controls Runtime `0x00553430` (`TARGET_PLAYER1+0x60`):**
   - `eGetRenderTarget(idx)` (`0x001FDAD8`) returns `0x00552940 + idx * 0xA90`. Thus `TARGET_PLAYER1` (`idx=1`) lives at `0x005533D0`, and `TARGET_PLAYER1.AspectRatio3dx` (`+0x60`) is **`0x00553430`** (the runtime address patched by PeterDelta's BE `.pnach`).
   - When `WideScreen != 0`, `MaybeChangeViewMode` (`0x00201578`) reads static float **`0x0054BA38`** (originally `0.71f` = `0x3F35C28F`, EA's CRT-overscanned widescreen factor), writes it to `TARGET_FRAMEBUFFER0/1+0x60` and car reflection targets `4/5+0x60`, and `UpdateGifTags` (`0x001FE130`) copies it directly into `TARGET_PLAYER1+0x60` (`0x00553430`)!
   - Patching `0x0054BA38` from `0.71f` (`0x3F35C28F`) to **`0.75f` (`0x3F400000`)** fixes BOTH gameplay and reflection targets statically for exact `4:3 -> 16:9` (`3/4 = 0.75`) Hor+ projection.
4. **Ultrawide 21:9 (64:27) & Frustum Culling Discovery (`0x0054E174`):**
   - `MaybeChangeViewMode` (`0x0020158C`) also copies static float **`0x0054E174`** (`0.71f`) into `0x0054E178`, which `CreateViewMatricies` (`0x00206938`) uses exclusively to scale the **horizontal frustum culling matrix** (`width * *(0x0054E178)`).
   - For **21:9 (`64:27`)**, setting `0x0054BA38 = 0.5625f` (`0x3F100000` = `9/16`) **and** widening `0x0054E174 = 0.90f` (`0x3F666666`) eliminates left/right screen-edge object pop-in!
   - For **+12% Wider Camera FOV on 16:9**, scaling both 3D X (`0x0054BA38`: `0.75 / 1.12 = 0.669643f`, `0x3F2B6DB7`) and 3D Y (`0x0054BA3C`: `1.10 / 1.12 = 0.982143f`, `0x3F7B6DB7`) plus frustum width (`0x0054E174`: `0.80f`, `0x3F4CCCCD`) widens 3D FOV symmetrically while preserving exact 16:9 proportions and untouched 2D HUD (`0x68`/`0x6C` remain unchanged).

### 3.3 High-LOD Cars, Full-Rate Reflections & Extended Scenery Draw Distance

- **Force Highest Available Car & Wheel LOD (`high_lod_cars`):**
  - Inside `CarRenderInfo::Render` (`0x003EC7A4..0x003EC7E4`), EA's `.rodata` pixel-size tables (`0x00505178`: `[120, 25, 20, 10, 0]` for racers; `0x00505190`: `[20, 10, 4, 0]` for traffic) select LOD levels, and are immediately followed by two built-in debug overrides: **`0x0059FF28`** (`g_ForceCarBodyLOD`, orig `0xFFFFFFFF` = `-1`) and **`0x0059FF2C`** (`g_ForceCarTireLOD`, orig `0xFFFFFFFF` = `-1`).
  - Crucially, the engine clamps both overrides using R5900 `pmaxw`/`pminw` against `[CarRenderInfo->mMinLodLevel, mMaxLodLevel]`, guaranteeing traffic/cop vehicles without LOD A meshes never crash or disappear when both are set to `0` (`LOD A`)!
- **Full-Rate 5-Face Real-Time Car Reflections (`full_rate_car_reflections`):**
  - `IsFaceEnabled__Fi` (`0x0020F598`) uses static table `0x0054FE50` (`[1, 1, 0, 1, 1, 1]` — all 5 visible faces, skipping bottom face 2) in the Garage/FE, but switches to a 5-frame round-robin table at `0x0054FEE0` (`frame % 5`, updating only 3 of 5 faces per frame) while driving.
  - Patching `0x0020F5B0` (`beq $v0, $zero, 0x0020F684` `0x10400034` -> unconditional `b 0x0020F684` `0x10000034`) forces all 5 visible cubemap faces to update every single frame!
- **Extended Scenery Draw Distance & High-Detail World LOD (`extended_scenery_draw_distance`):**
  - `ScenerySectionHeader::DrawAScenery` (`0x003DAE88`) computes `$s0`, the projected pixel size `(radius + 6) * H / (dist - (radius + 6))`.
  - In single-player mode:
    - `0x003DB110`: `slti $v0, $s0, 0x12` (`0x2A020012`) culls any scenery object under **18 PS2 pixels** (~108–144 px wide at 4K!). Patching to `0x2A020006` (**6 pixels**) extends scenery draw distance by **3x**.
    - `0x003DB178..0x003DB17C`: loads **`8.7f`** (`0x410B3333`) as the `pixel_size / max(radius, 6.0f)` cutoff between high-detail (`0x28($s1)`) and low-detail (`0x30($s1)`) world meshes. Patching to **`3.0f`** (`0x40400000`: `0x3C014040`, `0x34210000`) pushes high-poly building/tree transitions out by **~2.9x**.
  - *(Also located: `_20ScenerySectionHeader$mLodLevelDistance` @ `0x0058C0E0` = `[80.0f, 160.0f, 99999.0f]` in `.data`.)*

### 3.4 Gameplay & QoL Patches

- **Disable AI Catch-Up / Rubber-Banding (`disable_ai_catchup`):**
  - `GRaceParameters::GetCatchUp` (`0x002B5958`) is called by `AIPerpVehicle::ComputeSkill` (`0x0011EAFC`) and `GRaceStatus::SyncronizeAdaptiveBonus` (`0x002B11C4`). When it returns `0`, `SyncronizeAdaptiveBonus` immediately skips adaptive rubber-banding (`beq $v0, $zero, return_zero`).
  - Patching `0x002B5958..0x002B595C` to `jr $ra; move $v0, $zero` (`0x03E00008`, `0x0000102D`) disables AI catch-up globally.
- **Skip Intro / FMV Movies (`skip_intro_movies`):**
  - Setting built-in global **`0x00574660`** (`0 -> 1`) causes `MoviePlayer::Play` (`0x00245814`) and `FEngMovieStarter::Callback` (`0x002423D0`) to immediately emit `MOVIE_DONE` (`0xC3960EB9`) without touching movie pointers.
- **Unlock All Cars, Tracks & Parts (`unlock_all_cars_and_parts`):**
  - Built-in global **`0x00574624`** (`0 -> 1`) is checked across 18 unlocker functions (`QuickRaceUnlocker`, `CareerUnlocker`, `UnlockSystem`, `GRaceParameters::GetIsAvailable`).

---

## 4. Deep 60 FPS & Frame-Pacing Disassembly Analysis

A widespread myth in the PS2 community claims that *Need for Speed: Most Wanted* on PS2 is hardcoded to 30 FPS and needs a 60 FPS hack. **Direct MIPS R5900 disassembly of `SLUS_213.51` disproves this completely** and reveals the exact engine timing architecture:

### 4.1 Finding 1: No 30 FPS Cap Is Enabled in Retail `SLUS_213.51`

Inspecting `eDisplayFrame__Fv` at `0x0020DA84..0x0020DAF8`:

```mips
0x0020DA84: addiu   $v1, $t0, 0x3b30      ; &TheGameFlowManager (0x00573B30)
0x0020DA88: lw      $v0, 0x20($v1)        ; CurrentGameFlowState
0x0020DA8C: xori    $v0, $v0, 0x6         ; == GAMEFLOW_STATE_RACING (6)?
0x0020DA90: bne     $v0, $zero, 0x0020DAEC ; if not in-game -> skip cap
0x0020DA9C: lw      $v1, -0x4a28($v0)     ; -> 0x0054B5D8 (g_Lock30FPS, STATIC VALUE = 0x00000000!)
0x0020DAA0: beq     $v1, $zero, 0x0020DAEC ; ALWAYS TAKEN in retail binary (0 writes to 0x0054B5D8 in entire ELF!)
0x0020DAAC: lw      $v0, -0x4a14($v1)     ; VBlankCounter (0x0054B5EC, incremented @ 60 Hz in PSX2VBlank)
0x0020DAB0: lw      $v1, -0x4a18($a0)     ; LastSwapVBlank (0x0054B5E8)
0x0020DAB4: subu    $v0, $v0, $v1
0x0020DAB8: sltiu   $v0, $v0, 0x2         ; wait until >= 2 VBlanks (30 FPS)
0x0020DABC: beq     $v0, $zero, 0x0020DAF0
0x0020DAC8: jal     0x002082E0            ; WaitUntilVSync__Fv
...
0x0020DAF0: lw      $v0, -0x4a14($a2)
0x0020DAF4: jal     0x00201AB0            ; eSwapScreenBuffers__Fv
0x0020DAF8: sw      $v0, -0x4a18($a3)     ; LastSwapVBlank = VBlankCounter
```

- EA Black Box built a 2-VBlank 30 FPS limiter gated on `0x0054B5D8`, **and shipped it disabled (`0x00000000`)** with zero runtime writers anywhere in `.text`!
- Setting `0x0054B5D8 = 1` (`lock_30fps_frame_pacing`) cleanly *enables* EA's 30 FPS frame pacer for weaker hardware; leaving it `0` allows the engine to present up to **60 FPS** natively!

### 4.2 Finding 2: Why Stock PS2 Hardware Runs at ~25–30 FPS (VBlank Lockstep)

The retail frame loop synchronizes CPU frame building with VIF1/GS DMA and 60 Hz VBlank interrupts via two flags:

1. End of Frame `N` (`eDisplayFrame` @ `0x0020DC38`): `ExecuteRenderStageQueue` kicks VIF1 DMA and sets **`0x0054B5F0 = 1`** (`render_in_flight`).
2. `Vif1InteruptHandler` (`0x00208640`) -> `ServiceRenderStageQueue` (`0x00207F30`): upon VIF1 completion, clears `0x0054B5F0 = 0` and sets **`0x0054B5F4 = 1`** (`swap_pending`).
3. `PSX2VBlank` (`0x00385BB0`) -> `0x002085A0` (60 Hz NTSC ISR): increments `VBlankCounter` (`0x0054B5EC`), sees `0x0054B5F4 != 0`, calls `ePutDisplayEnvironment()`, and clears `0x0054B5F4 = 0`.
4. Meanwhile, Frame `N+1` calls `eWaitUntilRenderingDone` (`0x0020DA30`), which spins until **both** `0x0054B5F0 == 0` **and** `0x0054B5F4 == 0`.

Consequently, **if EE CPU + VIF1/GS finish within 16.67 ms (1 NTSC VBlank), the game naturally renders at true 60 FPS**. On stock 294 MHz PS2 hardware (or PCSX2 at `100%` EE Cycle Rate), Rockport's scene complexity exceeds 16.67 ms, missing VBlank #1 and snapping to 2 VBlanks (**30 FPS**). Raising PCSX2's **EE Cycle Rate to `180%` (`EECycleRate = 2`) or `300%` (`EECycleRate = 3`)** eliminates the EE bottleneck and lets the retail binary run at **true native 60 FPS**!

### 4.3 Finding 3: Physics & Simulation Timestep Audit (`1/60` s Native!)

All core simulation clocks already operate natively at **60 Hz (`1/60` s = `0x3C888889`)**:

- **`InitBigFiles` (`0x00312DA4..0x00312DB0`):** Calls `Scheduler::Init(0x3C888889)` (`1/60.0f`), creating 4 sub-schedules (`divisor = 1, 1, 2, 4` -> 60 Hz, 60 Hz, 30 Hz, 15 Hz).
- **`Scheduler::Run` (`0x003030C0`):** Reads real elapsed `Timer` ticks (`1/4000` s resolution), divides by `Scheduler->0x20` (`1/60` s), runs `N` integer fixed steps (`N = 1` at 60 FPS, `N = 2` at 30 FPS), and retains the fractional remainder in `Scheduler->0x1c`.
- **`SimSystem` (`0x00390C60..0x00390C88`) & `Sim::GetTimeStep` (`0x00394918`):** Copies `Scheduler->0x20` (`1/60` s) into `SimSystem->0x20`; `SimSystem::UpdateFrame` (`0x00393B74`) computes `Sim::GetTimeStep() = (1/60) * SimSpeed` (supporting Speedbreaker dynamically).
- **`PrepareRealTimestep` (`0x00322CE0`), `AdvanceRealTime` (`0x00322E10`), `AdvanceWorldTime` (`0x00322F78`):** Dynamically quantize and accumulate real elapsed `Count` cycles in `1/60` s (NTSC `0x3C888889`) or `1/50` s (PAL `0x3CA3D70A`) units, capped at `4.0 * dt` (`0x005742E4`).

### 4.4 Finding 4: Exhaustive `1/30f` (`0x3D088889`) Constant Scan & The One 60 FPS Bug

Scanning the entire `.text` segment (`0x00100000..0x004A0000`) for IEEE-754 `1/30f` (`0x3D088889`) finds 22 occurrences:
- `CAnimCtrl` / `CWorldAnimCtrl` / `CameraMover` / `ICEMover`: 30 Hz spline/keyframe authoring sample rate (properly integrated against dynamic `dt`).
- `SuspensionRacer::DoDrifting` (`0x00361D14..0x00361DF8`) & `SuspensionSimple::Tire::UpdateLoaded` (`0x00365C3C`): slip-angle / threshold constants (not frame-rate timers).
- **`UpdateAllScreenEFX__Fv` (`0x003DE778`) — Genuine 60 FPS Timing Bug!**
  - Called once per **displayed render frame** from `ePostDisplay__Fv` (`0x001F1040`).
  - Passes hardcoded `0x3D088889` (`1/30.0f`) to `ScreenEffectDB::Update(float dt)`, causing screen effects to advance at 2x speed when rendering at 60 FPS!
  - Our **`screen_efx_60fps_timing`** patch changes a single word at `0x003DE778` from `lui $at, 0x3d08` (`0x3C013D08`) to `lui $at, 0x3c88` (`0x3C013C88`), turning `0x3D088889` (`1/30f`) cleanly into `0x3C888889` (`1/60f`).

---

## 5. Complete Verified Patch Inventory (`38/38` Writes Verified)

| Patch ID | Default | Writes | EE Addresses | Category |
| :--- | :---: | :---: | :--- | :--- |
| `remove_brown_filter` | **ON** | 1 | `0x00550D58` | Visual |
| `remove_motion_blur` | **ON** | 2 | `0x00550150`, `0x0055014C` | Visual |
| `widescreen_16_9_native` | **ON** | 4 | `0x0054BA38`, `0x0026D9F4..FC` | Display |
| `high_lod_cars` | **ON** | 2 | `0x0059FF28`, `0x0059FF2C` | Graphics |
| `full_rate_car_reflections` | **ON** | 1 | `0x0020F5B0` | Graphics |
| `extended_scenery_draw_distance` | **ON** | 3 | `0x003DB110`, `0x003DB178..7C` | Graphics |
| `disable_bloom` | off | 1 | `0x00550D5C` | Visual |
| `disable_all_post_processing` | off | 1 | `0x00550D28` | Visual |
| `disable_radial_blur` | off | 2 | `0x00219A68..6C` | Visual |
| `remove_brume_fog` | off | 1 | `0x005500F0` | Visual |
| `no_distant_fog` | off | 1 | `0x00550028` | Visual |
| `disable_screen_raindrops` | off | 2 | `0x00211030..34` | Visual |
| `ultrawide_21_9_native` | off | 5 | `0x0054BA38`, `0x0054E174`, `0x0026D9F4..FC` | Display (alt) |
| `wider_camera_fov_12pct_16_9` | off | 6 | `0x0054BA38`, `0x0054BA3C`, `0x0054E174`, `0x0026D9F4..FC` | Camera (alt) |
| `disable_ai_catchup` | off | 2 | `0x002B5958..5C` | Gameplay |
| `skip_intro_movies` | off | 1 | `0x00574660` | QoL |
| `unlock_all_cars_and_parts` | off | 1 | `0x00574624` | Gameplay |
| `screen_efx_60fps_timing` | off | 1 | `0x003DE778` | Performance |
| `lock_30fps_frame_pacing` | off | 1 | `0x0054B5D8` | Performance |

Total: **19 patch groups** (`6` proven safe defaults, `13` optional/alternative toggles), **38 static ELF writes**, **100% verified byte-for-byte** against `SLUS_213.51` (CRC `0518D274`).
