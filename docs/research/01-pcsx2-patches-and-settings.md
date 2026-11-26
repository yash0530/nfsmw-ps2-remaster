# 01: PCSX2 patches and settings for NFS Most Wanted Black Edition (SLUS-21351, CRC 0518D274)

Research date: 2026-11-24. Target: PCSX2 **v2.6.3** (macOS, Metal) and disc **SLUS-21351**, ELF `SLUS_213.51`, CRC **0518D274**.
Method: I read the PCSX2 source at tag `v2.6.3`, the PCSX2/pcsx2_patches repo, the patch authors' own GitHub repos, the PCSX2 docs and the NFSMW decompilation's symbol map. I downloaded and executed nothing, and I did not open the disc or ELF.

> **Limits of this research.** forums.pcsx2.net, wiki.pcsx2.net, wsgf.org and consolemods.org all returned HTTP 403 or a bot-verification page to automated readers. I did not get past those checks, so anything known only from those forums is **not** covered here, beyond what search-engine snippets showed. Reddit and YouTube searches turned up nothing specific to this game.

Confidence labels used below:
- **[Code-verified]**: confirmed by reading PCSX2 v2.6.3 source.
- **[Author-published]**: the exact lines come from the author's repo for this exact CRC. I have not tested them in-game.
- **[Corroborated]**: author-published, and a second independent source or the decomp symbol map supports what the address is.
- **[Unverified]**: a port, a guess, an unmerged PR, or a file with an unclear origin. Test it before trusting it.

---

## 0. TL;DR: what you can do today

1. **Widescreen exists for your exact CRC, but it is not in PCSX2's bundled patches.** PeterDelta publishes a `[Widescreen 16:9]` group for SLUS-21351/0518D274 in his own repo. It has not been merged into PCSX2/pcsx2_patches. Put it in `~/Library/Application Support/PCSX2/patches/`. The group name is exactly `Widescreen 16:9`, so the global **Apply Widescreen Patches** toggle enables it automatically (§2.2, §4.2).
2. **No 60 FPS patch exists** for any NFSMW version, PS2 NTSC-U, PAL or Black Edition, in pcsx2_patches, PeterDelta's repo, Felixthecat1970's repo or anywhere else I found. One review says the PS2 version is *"still locked at 30 FPS"* ([DarkZero](https://darkzero.co.uk/game-reviews/need-for-speed-most-wanted-ps2/)). A file in the NetherSX2-cheats repo has a group labelled `[60 FPS]`, but it actually contains a "Remove Sun Effects" line (§2.9).
3. **Post-processing removal codes exist for your CRC:** sepia/brown filter, bloom, motion blur ("smear"), distant fog, all post-processing, and "brume". PCSX2 2.6.3 bundles **only** fobes' `Remove Brown Filter`. Steelmax's 5-code set (March 2026) is bundled only in later builds such as 2.8.x (§2.1).
4. **GameDB already applies upscaling fixes for this disc** (HPO "Align to Native w/ Texture Offsets", Native Scaling "Aggressive", CPU CLUT, GPU Target CLUT, Force-Nearest dirty upscale). They apply **only if "Manual Hardware Renderer Fixes" is OFF** (§3).
5. **PCSX2 on Apple Silicon runs through Rosetta 2**. Even v2.8.2's released macOS build is x86_64 (§4.6). The EE/VU emulation on the CPU is usually the limit, not the M5 Pro GPU.

---

## 1. Version map and address relationships

| Release | Serial | CRC | Patches file in pcsx2_patches |
|---|---|---|---|
| NTSC-U Black Edition (**yours**) | SLUS-21351 | 0518D274 | `SLUS-21351_0518D274.pnach` |
| NTSC-U standard | SLUS-21267 | 270F8C03 | `SLUS-21267_270F8C03.pnach` |
| NTSC-U standard "1.01" (per Sal89 PR #614 and pcsx2-rr) | SLUS-21267 | 0518D275 | none |
| PAL-M Black Edition | SLES-53857 | 805C3B3A | `SLES-53857_805C3B3A.pnach` |
| PAL-M (M7) | SLES-53559 | CA2A1B04 | `SLES-53559_CA2A1B04.pnach` |
| PAL-E | SLES-53557 | 692CBA8E | `SLES-53557_692CBA8E.pnach` |
| PAL-M8 | SLES-53558 | 1FA82CDF | `SLES-53558_1FA82CDF.pnach` |

Sources: [pcsx2_patches/patches](https://github.com/PCSX2/pcsx2_patches/tree/main/patches), [PR #614](https://github.com/PCSX2/pcsx2_patches/pull/614), [pcsx2-rr 0518D275.pnach](https://github.com/EmulatorArchive/pcsx2-rr/blob/HEAD/bin/patches/0518D275.pnach).

**Porting rules observed in the published codes.** I inferred these by comparing files; they are not authoritative.
- **Code (.text)** addresses are identical across NTSC-U standard, NTSC-U BE, PAL and PAL BE. Examples: PeterDelta's `001FEDB8/001FEDBC` blur code in every file, and Felix's `2026D9F8` in NTSC BE, PAL BE and PAL.
- **Data in the 0x0054B000–0x00551xxx range:** **NTSC-U BE = PAL BE − 0x300**. Steelmax's five codes and Felix's "less blur" addresses all differ by exactly 0x300. **NTSC-U standard and NTSC-U BE share** the addresses fobes 0x00550D58, PeterDelta 0x00550D28/0x0055014C/0x005500F0, and Sal89 0x00574228/0x00574264.
- **Heap or high addresses** such as `0x00AAFxxx` differ per release (0x00AAFDD4 standard US, 0x00AAFE54 BE US, 0x00AAF954 PAL-M, 0x00AAFD54 PAL-E). They lie beyond the ELF image (your ELF ends at 0x005ABBDC), so they are runtime-allocated. Do **not** port them by offset arithmetic.

---

## 2. Patches for SLUS-21351 / 0518D274

### 2.1 Official PCSX2/pcsx2_patches file (bundled in `patches.zip`)

URL: https://github.com/PCSX2/pcsx2_patches/blob/main/patches/SLUS-21351_0518D274.pnach. History: [commits](https://github.com/PCSX2/pcsx2_patches/commits/main/patches/SLUS-21351_0518D274.pnach).

**What PCSX2 v2.6.3 ships** (built 2026-01-28): only the 2023 version, commit `8317712`, "Add NFS MW Black Edition brown filter patch" by F0bes, committed by refractionpcsx2.
```
gametitle=Need for Speed - Most Wanted [Black Edition] [NTSC-U] [SLUS-21351]

[Remove Brown Filter]
author=fobes
description=Disables the brownish yellow post processing filter
patch=1,EE,00550d58,extended,20000001
```
**Current file** after [PR #688](https://github.com/PCSX2/pcsx2_patches/pull/688) by Userbots ("Steelmax"), merged 2026-03-26. Later builds bundle this, for example v2.8.2, released 2026-09-04:
```
gametitle=Need for Speed - Most Wanted [Black Edition] [NTSC-U] [SLUS-21351]

[Disable All Post-processing]
author=Steelmax
description=Disables rendering of yellow sepia, bloom, rain softening (except for motion blur).
patch=1,EE,00550D28,word,00000000

[Disable Brown Filter]
author=fobes
description=Disables the brownish yellow post processing filter.
patch=1,EE,00550d58,extended,20000001

[Disable Bloom]
author=Steelmax
description=Disables excessive bloom post-process effect.
patch=1,EE,00550D5C,word,00000001

[Disable Motion Blur Effect]
author=Steelmax
description=Disables the smear while accelerating, using Nitro (including blue tint) and driving at high speed.
patch=1,EE,00550150,word,00000000

[No Distant Fog]
author=Steelmax
description=Removes fog from the game.
patch=1,EE,00550029,byte,00
```
- Notes from the PR author: "Disable All Post-processing" disables everything and *"also remedies blending offset rendering problems"* (upscaling misalignment). fobes' code can be combined with Disable Bloom. Motion blur is a separate code. A maintainer marked the PR "Assumed working and tested", and the author re-tested NTSC and PAL. ([PR #688](https://github.com/PCSX2/pcsx2_patches/pull/688))
- `extended` with address `00550d58` is RAW type `0`, an **8-bit write** of the low byte. So fobes' code writes byte `0x01` to 0x00550D58 **[Code-verified]** ([Patch.cpp v2.6.3, `0aaaaaaa 000000vv`](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Patch.cpp)).
- **Is any of this auto-applied?** No. None of these groups is named `Widescreen 16:9` or `No-Interlacing`, so you must tick them per game in *Game Properties → Patches* **[Code-verified]** (§4.2). **The "Apply Widescreen Patches" toggle does nothing for this disc with the bundled file alone.**
- Decomp symbol context ([dbalatoni13/nfsmw symbols](https://github.com/dbalatoni13/nfsmw/blob/main/config/SLUS-21351/symbols.txt)): 0x00550029 is inside `FogModeInfos` (0x00550028, size 0xC0). 0x00550150 is just before `SmearSubStageEnable` (0x00550158), where "smear" is the game's motion blur. The 0x00550D28–0x00550D5C words fall between `DmaSrcChannelIDs` and `rbZ` and have no names. All of these sit in `zEcstasy.cpp` .data, the render-engine translation unit. **[Corroborated]** for fog and smear.
- License: the pcsx2_patches repo has **no LICENSE file**. Its README says the maintainers *"only supply the patches with the emulator"*. Treat each code as the named author's work.

### 2.2 PeterDelta: widescreen, blur, brume for SLUS-21351 (not in pcsx2_patches)

URL: https://github.com/PeterDelta/PCSX2/blob/main/patches/SLUS-21351_0518D274.pnach. Listed as "Need for Speed - MW Black Edition — USA" in the [README](https://github.com/PeterDelta/PCSX2). The repo is a *"Compilation of 50/60 fps and widescreen PS2 patches that I've created or fixed"* and has no LICENSE file. Last commit touching the file: 2026-08-12.
```
gametitle=Need for Speed - Most Wanted [Black Edition] (NTSC-U) SLUS-21351 0518D274

[Widescreen 16:9]
gsaspectratio=16:9
author=PeterDelta
description=Corrects display cropping to 16:9.
patch=1,EE,00AAFE54,word,00000001
patch=1,EE,00553430,word,3F400000

[Remove Brown Filter]
author=fobes
description=Disables the brownish yellow post processing filter
patch=1,EE,00550D58,extended,20000001

[Remove Blur]
author=PeterDelta
description=Removes the blur effect
patch=0,EE,0055014C,word,00000000
patch=0,EE,001FEDB8,word,24020000
patch=0,EE,001FEDBC,word,10620004
patch=0,EE,00550D28,word,00000000

[Remove Brume]
author=PeterDelta
description=Removes background brume effect.
patch=0,EE,005500F0,word,00000000
```
- **Widescreen: [Author-published], partly [Corroborated].** A second, independent BE widescreen code by "NMY" (§2.4) also targets **0x00553430**, which supports that address as the BE-specific aspect constant. `0x3F400000` = 0.75 = 3/4, the usual 4:3→16:9 horizontal factor. `0x00AAFE54 = 1` is a runtime (heap) variable and is probably the game's widescreen option flag. The ELF contains `eIsWidescreen__Fv` (0x001F15A0), `VOWideScreen`, `FEngHud::SetWideScreenMode` and `Minimap::AdjustForWidescreen` ([decomp symbols](https://github.com/dbalatoni13/nfsmw/blob/main/config/SLUS-21351/symbols.txt)), so the engine has a **native widescreen mode** and this patch forces it on and fixes the cropping. HUD and minimap presumably use the game's own widescreen layout; this is not tested.
- `gsaspectratio=16:9` makes PCSX2 present 16:9 whenever the group is enabled **[Code-verified]**.
- **Remove Blur [Author-published]:** this group **also** contains `00550D28 = 0`, which is Steelmax's "Disable All Post-processing" word. Enabling it therefore also removes sepia, bloom and rain softening. The two code words (`24020000` = `li v0,0`; `10620004` = `beq v1,v0,+4`) patch code at 0x001FEDB8, just after `UpdateGifTagsOLD__13eRenderTarget...` (0x001FEBC8) in the decomp map. These are `place=0` lines, applied once at boot, so **restart the game after enabling them**.
- **Remove Brume [Author-published]:** writes 0 to 0x005500F0, just past the end of `FogModeInfos`. This is related to, but different from, Steelmax's "No Distant Fog".
- The same author's standard US file ([SLUS-21267](https://github.com/PCSX2/pcsx2_patches/blob/main/patches/SLUS-21267_270F8C03.pnach)) uses `00AAFDD4` and `0054BA38` for widescreen. **Do not use those on BE** (see §2.5).

### 2.3 Felixthecat1970: auto-enable widescreen, less blur, extra languages

URL: https://github.com/felixthecat1970/gamepatches/blob/main/Playstation_2/1-PCSX2-QT-Felixthecat1970-sync/patches/SLUS-21351_0518D274.pnach (2024-11-23). OPL/real-hardware form: [`SLUS_213.51.cht`](https://github.com/felixthecat1970/gamepatches/blob/main/Playstation_2/OPL/CHT/SLUS_213.51.cht). There is no license; the README says *"nothing is granted to work perfect"*.
```
gametitle=Need for Speed - Most Wanted - Black Edition (USA) SLUS-21351 0518D274

[Unlock multilanguage b2]
author=Felixthecat1970
comment=set in PAL bios german, french, italian, spanish, dutch, it will autoboot it, fmv and audio will play in english
patch=0,EE,2026A8A8,extended,0C0C4A7E
patch=0,EE,203860E0,extended,0C0C4A7E
patch=0,EE,202797D4,extended,0C14CE70
patch=0,EE,202797F0,extended,0C14CE70
patch=0,EE,205339C0,extended,3C030057
patch=0,EE,205339C4,extended,8C62EB08
patch=0,EE,205339C8,extended,2C5B0005
patch=0,EE,205339CC,extended,13600002
patch=0,EE,205339D0,extended,00000000
patch=0,EE,205339D4,extended,0000102D
patch=0,EE,205339D8,extended,03E00008

[autoenable widescreen]
author=Felixthecat1970
comment=enable boot default option
gsaspectratio=16:9
patch=0,EE,2026D9F8,extended,E480000C

[Less blur screen test]
author=Felixthecat1970
comment=for scaling 2x+
patch=0,EE,00201C78,extended,60
patch=0,EE,0054B7F0,extended,00
patch=0,EE,0054B794,extended,00
patch=0,EE,0054B804,extended,00
patch=0,EE,0054B7A8,extended,00
```
- **autoenable widescreen [Corroborated as to target]:** 0x0026D9F8 is inside `Default__13VideoSettings` (0x0026D9F0) in the decomp map. The patch changes the *default* video settings so widescreen starts ON (`E480000C` = `swc1 $f0,0xC($a0)`). The same address appears in Felix's PAL and PAL-BE files. Unlike PeterDelta's group, it does **not** apply the 0.75 cropping fix. The group name is not `Widescreen 16:9`, so the global toggle does not auto-enable it.
- **Less blur screen test [Author-published, labelled "test"]:** byte writes into `BufferParameters` (0x0054B698, size 0x398) plus one byte in code at 0x00201C78, inside `ePutDisplayEnvironment__Fv` (0x00201B58). This probably reduces the display-buffer blend/flicker filter. The PAL version has one extra line (`201BF0 FF`) that the NTSC file lacks.

### 2.4 "NMY" widescreen variant (NetherSX2-cheats repo) [Unverified]

URL: https://github.com/Saramagrean/NetherSX2-cheats/blob/HEAD/cheats_ws/0518D274.pnach. The repo has no license. The file uses the legacy `cheats_ws` naming.
```
[widescreen 16:9]
author=by NMY
patch=1,EE,20553430,extended,3F800fff
patch=1,EE,20553434,extended,3FF11111
patch=1,EE,20AAFED0,extended,00000006
```
This takes a different approach from PeterDelta's: it sets two floats at 0x00553430/34 and writes 6 to a different heap address. It is useful as **corroboration that 0x00553430 is the BE aspect constant**. Do not enable it together with PeterDelta's group. It also has no `gsaspectratio` line, so you would have to set 16:9 manually.

### 2.5 Ported SLUS-21267 widescreen on BE (PSH5 repo) [Unverified, likely wrong]

URL: https://github.com/PSH5JB/PSH5/blob/HEAD/payloads/emulators/PS5SX2/PCSX2/patches/SLUS-21351_0518D274.pnach. Its own comment says *"ported unchanged … Not yet confirmed on this disc"*.
```
[Widescreen 16:9]
gsaspectratio=16:9
author=PeterDelta
description=Corrects display cropping to 16:9 (ported from SLUS-21267).
patch=1,EE,00AAFDD4,word,00000001
patch=1,EE,0054BA38,word,3F400000
```
PeterDelta made a separate BE code with **different** addresses (§2.2), and the heap flag address differs per release (§1). In BE, 0x0054BA38 sits next to `NTSCAspectRatio2dy` (0x0054BA48), so it may be a related constant. The heap write to 0x00AAFDD4, however, is very likely the wrong variable on BE. **Avoid it; use PeterDelta's BE code.**

### 2.6 Unlock codes

**Sal89, [PR #614](https://github.com/PCSX2/pcsx2_patches/pull/614), closed and not merged because of formatting [Unverified].** The BE already has Black Edition content, so only the "Burger King Challenge + Castrol Ford GT always enabled" group applies:
```
[Enable Burger King Challenge]
description=Burger King Challenge and Castrol Ford GT always enabled
author=Sal_89
patch=1,EE,20574228,byte,1
patch=1,EE,20574264,byte,1
```
(The `2` prefix on a `byte` patch addresses the 0x20000000 uncached mirror of 0x00574228. Its standard-US counterparts use the same addresses.)

**Cheat Device PS2 database [Unverified on PCSX2]:** [root670/CheatDevicePS2 `CheatDatabase.txt`](https://github.com/root670/CheatDevicePS2/blob/master/CheatDatabase.txt) (GPL-3.0), entry `"Need For Speed-Most Wanted-Black Edition"`. A pnach conversion exists at [EmuCoreX-Cheat](https://github.com/sashkinbro/EmuCoreX-Cheat/blob/HEAD/files/root670-cheatdevice/SLUS-21351_0518D274.pnach). Selected raw lines (put them in **cheats/** and enable cheats):
```
[Access All Cars (Not Saveable)]
patch=1,EE,20574624,extended,00000001

[Infinite NOS Usage]
patch=1,EE,2036F0DC,extended,00000000

[Infinite Speedbreaker]
patch=1,EE,203963D4,extended,00000000

[Max Cash]
patch=1,EE,20AAFF10,extended,05F5E0FF
```
The `Enable Code (Must Be On)` line (`90494824 0C1298E3`) is a type-9 hook for real-hardware cheat devices. PCSX2 doesn't need it.

**Skip intro videos ("General Plot", via pcsx2-rr) [Unverified]:** [0518D274.pnach](https://github.com/EmulatorArchive/pcsx2-rr/blob/HEAD/bin/patches/0518D274.pnach)
```
[Skip Videos]
patch=0,EE,00242418,word,00000000
```

**Related:** NFS Carbon and ProStreet originally checked for the wrong Most Wanted BE serial when granting the career bonus. [CookiePLMonster's fix](https://github.com/PCSX2/pcsx2_patches/blob/main/patches/SLUS-21493_7841A89E.pnach) ("Fix Most Wanted Black Edition save detection") patches Carbon to look for `BASLUS-21351`. GameDB `memcardFilters` for SLUS-21351 include SLUS-21267 and SLUS-21065 (an Underground 2 save grants extra money).

### 2.7 Other NetherSX2-cheats groups for 0518D274 [Unverified]

[cheats_mi/0518D274.pnach](https://github.com/Saramagrean/NetherSX2-cheats/blob/HEAD/cheats_mi/0518D274.pnach) contains `[60 FPS]` (**mislabelled**: its only line is `//Remove Sun Effects patch=1,EE,00386D04,word,C4000000`), `[Remove Yellow Filter] 0040A500=C4000000` (this lands inside `IVisualTreatment` code), and `[Driving Easier] 2057E70C=40500000` by NMY. The authors are unknown or uncredited. Treat these as experiments.

### 2.8 Draw distance, LOD, FOV, camera, HUD removal

**No published codes were found** for SLUS-21351, the standard US release or PAL. The only fog-related codes are Steelmax "No Distant Fog" and PeterDelta "Remove Brume". The decomp symbol map gives starting points for writing your own (CC0, [symbols.txt](https://github.com/dbalatoni13/nfsmw/blob/main/config/SLUS-21351/symbols.txt)):
- Camera/FOV: `GetFOV__8ICEMoverf` 0x0017AD60, `ConvertLensLengthToFovAngle__Ff` 0x00178F10, `FovRelativeAngle__6CameraUs` 0x00165678.
- Fog: `BaseWeatherFogStart` 0x00593E94, `HorizWeatherFogStart` 0x00593ED0, `BaseFogFalloffX` 0x00593E88, `FogModeInfos` 0x00550028.
- Post-processing ("Visual Treatment"): `IVisualTreatmentPlat::ApplyColorBloom` 0x00219210, `::Radialblur` 0x00219A68, `::RenderMWVisualLook` 0x0021A008, `IVisualTreatment::UpdateVisualLook` 0x0040A6D0.
- Smear (motion blur): `SmearSubStageEnable` 0x00550158, `SmearDistances` 0x00550188, `SmearAlphas` 0x005501B8.
- Widescreen/HUD: `eIsWidescreen__Fv` 0x001F15A0, `FEngHud::SetWideScreenMode` 0x0025D788, `Minimap::AdjustForWidescreen` 0x00253A78.
- Mip/LOD: `MipMapMinMag` 0x0054BA70.

### 2.9 60 FPS

- **None exists** for any NFSMW PS2 version in pcsx2_patches, PeterDelta's repo (which has 50/60 FPS groups for Underground 1 and 2 but not for MW), Felix's repo, or the other GitHub copies I found.
- The PS2 version is reportedly *"locked at 30 FPS"* ([DarkZero PS2 review](https://darkzero.co.uk/game-reviews/need-for-speed-most-wanted-ps2/)). The NTSC-U disc already outputs 60 Hz; reports of "60 FPS" on PCSX2 refer to VPS (vsyncs per second), not the game's internal frame rate. **How to check:** enable the OSD "Show FPS" (`OsdShowFPS`), which shows internal frames, alongside `OsdShowVPS`.
- Felix's PAL `[autoboot NTSC60hz\60fps]` only switches PAL discs to NTSC 60 Hz. It does nothing for your NTSC disc.
- The PCSX2 forum "60 fps codes" thread (pp. 128–129 per search results) mentions MW PAL codes, but I could not read it (bot check). Search-engine summaries of it conflated other games' lines, so I am **not** reproducing them.

### 2.10 Portable codes from other releases

| Feature | Exists for | Port to BE? |
|---|---|---|
| Widescreen (PeterDelta) | SLUS-21267, SLES-53557/53559, SLES-53857 | **Not needed**: a BE-specific version exists (§2.2) |
| Remove Blur / Brume (PeterDelta) | all of the above | Already in the BE file |
| NTSC Mode (Felixthecat1970) | PAL only | Not applicable |
| Languages (Felix) | PAL BE, NTSC BE | Already in the NTSC BE file |
| Steelmax set | SLUS-21351 + SLES-53857 (+0x300) | Native |

### 2.11 Suggested user file for v2.6.3

This file is **assembled from the codes above**, with authors credited and nothing new added. Save it as `~/Library/Application Support/PCSX2/patches/SLUS-21351_0518D274_extras.pnach`; any `SLUS-21351_0518D274*.pnach` name loads, see §4.2. It leaves out fobes' brown filter because 2.6.3 already bundles it. Enable groups individually.
```
gametitle=Need for Speed - Most Wanted [Black Edition] (NTSC-U) SLUS-21351 0518D274

[Widescreen 16:9]
gsaspectratio=16:9
author=PeterDelta
description=Corrects display cropping to 16:9.
patch=1,EE,00AAFE54,word,00000001
patch=1,EE,00553430,word,3F400000

[Disable All Post-processing]
author=Steelmax
description=Disables rendering of yellow sepia, bloom, rain softening (except for motion blur).
patch=1,EE,00550D28,word,00000000

[Disable Bloom]
author=Steelmax
description=Disables excessive bloom post-process effect.
patch=1,EE,00550D5C,word,00000001

[Disable Motion Blur Effect]
author=Steelmax
description=Disables the smear while accelerating, using Nitro (including blue tint) and driving at high speed.
patch=1,EE,00550150,word,00000000

[No Distant Fog]
author=Steelmax
description=Removes fog from the game.
patch=1,EE,00550029,byte,00

[Remove Brume]
author=PeterDelta
description=Removes background brume effect.
patch=0,EE,005500F0,word,00000000
```
If you upgrade to PCSX2 ≥ 2.8, drop the Steelmax groups from this file; the bundled zip has them, and you would otherwise see duplicates.

**Combination guidance (inferred):**
- Mild look: Brown Filter plus Motion Blur only.
- "Clean" look: Disable All Post-processing plus Motion Blur. Brown Filter and Bloom then add nothing.
- PeterDelta's Remove Blur ≈ All Post-processing plus an extra smear word plus a code tweak.

**Verifying offline (no emulator needed).** ELF file offset = vaddr − 0x00100000 + 0x1000. Examples: 0x00553430 → `0x454430`; 0x0054BA38 → `0x44CA38`; 0x00550D28 → `0x451D28`; 0x00550D58 → `0x451D58`; 0x00550D5C → `0x451D5C`; 0x00550150 → `0x451150`; 0x00550029 → `0x451029`; 0x005500F0 → `0x4510F0`; 0x001FEDB8 → `0x0FFDB8`; 0x0026D9F8 → `0x16E9F8`; 0x00201C78 → `0x102C78`; 0x00574228 → `0x475228`; 0x00242418 → `0x143418`. Addresses 0x00AAFxxx are heap and **not** in the ELF; check them with the PCSX2 debugger's memory view at runtime.

---

## 3. GameDB entry and upscaling fixes

**PCSX2 v2.6.3** [`bin/resources/GameIndex.yaml`](https://github.com/PCSX2/pcsx2/blob/v2.6.3/bin/resources/GameIndex.yaml) **[Code-verified]:**
```yaml
SLUS-21351:
  name: "Need for Speed - Most Wanted [Black Edition]"
  region: "NTSC-U"
  compat: 5
  gsHWFixes:
    bilinearUpscale: 2 # Smooths out sun glare textures like native.
    halfPixelOffset: 5 # Fixes blurriness.
    nativeScaling: 2 # Fixes post effects.
    cpuCLUTRender: 1 # Final colour adjustment LUT.
    gpuTargetCLUT: 1 # Fixes sun penetrating bridges (along with HPO special).
  memcardFilters:
    - "SLUS-21267"
    - "SLUS-21351"
    - "SLUS-21065" # Underground 2 save grants extra money.
```
[master](https://github.com/PCSX2/pcsx2/blob/master/bin/resources/GameIndex.yaml) and [v2.8.2](https://github.com/PCSX2/pcsx2/blob/v2.8.2/bin/resources/GameIndex.yaml) add `drawBuffering: 1 # Reduces draws and improves performance.` That feature arrived after 2.6.3 (commits [76c401b0f](https://github.com/PCSX2/pcsx2/commit/76c401b0f) "GS: Implement draw buffering", 2026-01-25, and [1fa3c6aa4](https://github.com/PCSX2/pcsx2/commit/1fa3c6aa4), 2026-02-03), and 2.6.3 has no `UserHacks_DrawBuffering` key. The entry has **no gameFixes, clampModes, roundSprite, autoFlush or blending level**. Every other NFSMW serial (SLUS-21267, SLES-53557/8/9, SLES-53857) has identical gsHWFixes.

Meaning of each fix (values from [GameIndex.md](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/Docs/GameIndex.md) and [Config.h v2.6.3](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Config.h)):

| GameDB key | Value | UI name (Graphics →) | ini key `[EmuCore/GS]` | Fixes |
|---|---|---|---|---|
| bilinearUpscale | 2 = **Force Nearest** | Upscaling Fixes → Bilinear Dirty Upscale | `UserHacks_BilinearHack=2` | sun glare textures |
| halfPixelOffset | 5 = **Align to Native with Texture Offsets** | Upscaling Fixes → Half Pixel Offset | `UserHacks_HalfPixelOffset=5` | blur/ghosting when upscaled |
| nativeScaling | 2 = **Aggressive** | Upscaling Fixes → Native Scaling | `UserHacks_native_scaling=2` | misaligned post effects (bloom/visual treatment) |
| cpuCLUTRender | 1 = Normal | HW Fixes → CPU CLUT Render | `UserHacks_CPUCLUTRender=1` | final colour LUT |
| gpuTargetCLUT | 1 = Enabled (Exact Match) | HW Fixes → GPU Target CLUT | `UserHacks_GPUTargetCLUTMode=1` | sun shining through bridges ([issue #3993](https://github.com/PCSX2/pcsx2/issues/3993): *"This game renders into the clut to update the alpha channel of the sun texture"*) |

**Critical caveat [Code-verified]:** GameDB applies these **only when "Manual Hardware Renderer Fixes" (`UserHacks`) is off**. With it on, each one is skipped unless your manual value already matches, and PCSX2 shows an OSD warning ([GameDatabase.cpp v2.6.3](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/GameDatabase.cpp), `apply_auto_fixes = !config.ManualUserHacks`). Upscaling fixes count as "matching" at 1x. Leave Manual HW Fixes **OFF** unless you are deliberately overriding.

**Known glitches and their status:**
- Sun visible through bridges and walls on the HW renderer: fixed by gpuTargetCLUT ([#3993](https://github.com/PCSX2/pcsx2/issues/3993)).
- Upscaled bloom/blur offset: handled by Native Scaling Aggressive and HPO 5. Steelmax says "Disable All Post-processing" removes any remaining blending offset ([PR #688](https://github.com/PCSX2/pcsx2_patches/pull/688)).
- Fog appearing as transparent planes and over-bright tunnel bloom on HW ([#2115](https://github.com/PCSX2/pcsx2/issues/2115), 2017, closed without a fix note): current status unknown. The No Distant Fog and Disable Bloom patches work around both.
- Square shadow/light blocks on PAL BE, 2017 ([#1913](https://github.com/PCSX2/pcsx2/issues/1913)): old and not reproduced since.
- Sun rays become halos with an HD texture pack, on macOS/Metal ([#10351](https://github.com/PCSX2/pcsx2/issues/10351)): a texture-pack problem, per the devs.
- Occasional freeze on 2.5.349 ([#13642](https://github.com/PCSX2/pcsx2/issues/13642)): reported "Solved" on later nightlies.
- Official general guidance: for ghosting or bloom misalignment, *"try the various Half Pixel Offset modes"* and stop at the first that works ([Configuration Guide](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Docs/Configuration_Guide/Configuration_Guide.md)). For this game, GameDB already picks the mode.

---

## 4. PCSX2 2.6.x on macOS: mechanics

### 4.1 Folders [Code-verified: `Pcsx2Config.cpp` `MAC_DATA_DIR`]
Data root: `~/Library/Application Support/PCSX2/`. Defaults, overridable in `[Folders]` of `inis/PCSX2.ini`:
`patches/`, `cheats/`, `gamesettings/`, `textures/`, `snaps/`, `covers/`, `resources/`. The bundled `patches.zip` lives inside the app bundle's resources ([Patch.cpp `PATCHES_ZIP_NAME`](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Patch.cpp)). Don't edit it, because that breaks the app signature; use `patches/` instead.

### 4.2 pnach format and loading rules [Code-verified unless noted]
Sources: [Patch.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Patch.cpp), [Patch.h](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Patch.h), [docs: writing-patches](https://github.com/PCSX2/pcsx2-net-www/blob/main/docs/advanced/writing-patches.md) (published at pcsx2.net/docs/advanced/writing-patches).
- **Filename:** `SERIAL_CRC.pnach`, for example `SLUS-21351_0518D274.pnach`. The disk search pattern is `SLUS-21351_0518D274*.pnach`, so suffixed names like `SLUS-21351_0518D274_extras.pnach` load too, as do legacy CRC-only `0518D274*.pnach` files.
- **patches/ vs cheats/:** `patches/` merges with `patches.zip` and is toggled per game in Game Properties → **Patches**. `cheats/` needs **Enable Cheats** and is never merged with the zip. In RetroAchievements hardcore mode, disk files are ignored.
- **Merge behaviour:** disk files load **and then** the zip's file for the same serial/CRC also loads. **Exception:** if any disk file contains an *unlabelled* patch line (outside a `[group]`), the zip is skipped for that game and unlabelled lines are always active. Always put your lines inside named groups.
- **Group syntax:**
  ```
  gametitle=...            (informational)
  [Group Name]             (a backslash in the name nests groups in the UI)
  author=...
  description=...          (or comment=)
  gsaspectratio=16:9       (2.6.3 parses any N:M ratio)
  gsinterlacemode=1        (0 Auto, 1 Off, 2 WeaveTFF, 3 WeaveBFF, 4 BobTFF, 5 BobBFF, 6 BlendTFF, 7 BlendBFF, 8 AdaptiveTFF, 9 AdaptiveBFF)
  patch=<place>,<EE|IOP>,<addr>,<type>,<value>
  dpatch=<type>,<pattern count>,<replacement count>,<offset,value pairs>...
  // end-of-line comments with two slashes
  ```
- **place:** `0` once at boot, when the entry point first runs. `1` every vsync. `2` both. `3` at boot **and** immediately when enabled in the GUI.
- **type:** `byte`, `short`, `word`, `double`, `extended` (RAW/CodeBreaker semantics: top nibble of the address is the code type), `beshort`, `beword`, `bedouble`, `bytes`.
- **Auto-enable by name:** the global **Graphics → Display → "Apply Widescreen Patches"** (`[EmuCore] EnableWideScreenPatches`) enables any group named exactly `Widescreen 16:9`. **"Apply No-Interlacing Patches"** (`EnableNoInterlacingPatches`) enables `No-Interlacing`. These checkboxes are removed from per-game settings and migrated into `[Patches] Enable/Disable` entries.
- **Per-game enable list** in `gamesettings/SLUS-21351_0518D274.ini`, as repeated keys:
  ```ini
  [Patches]
  Enable = Widescreen 16:9
  Enable = Disable Motion Blur Effect
  Disable = No-Interlacing
  [Cheats]
  Enable = Access All Cars (Not Saveable)
  ```
- There is **no No-Interlacing patch** for NFSMW anywhere I checked. PCSX2's automatic deinterlacing handles it.

### 4.3 Per-game settings ini [Code-verified: `VMManager::GetGameSettingsPath`, `Pcsx2Config.cpp`, `GraphicsSettingsWidget.cpp` @ v2.6.3]
Path: `~/Library/Application Support/PCSX2/gamesettings/SLUS-21351_0518D274.ini`. Booleans are `true`/`false`. Edit it only while PCSX2 is closed, or use Game Properties.

| Setting (UI) | Section / key | Values (default) |
|---|---|---|
| Renderer | `[EmuCore/GS] Renderer` | -1 Auto, **17 Metal**, 13 Software (-1) |
| Internal Resolution | `upscale_multiplier` | float, 1 = native (1). `ExtendedUpscalingMultipliers=true` unlocks more |
| Texture Filtering | `filter` | 0 Nearest, 1 Bilinear Forced, **2 Bilinear (PS2)**, 3 Forced excl. sprite |
| Trilinear Filtering | `TriFilter` | **-1 Auto**, 0 Off, 1 Trilinear (PS2), 2 Trilinear (Forced) |
| Anisotropic Filtering | `MaxAnisotropy` | **0**, 2, 4, 8, 16 |
| Mipmapping | `hw_mipmap` | **true** |
| Blending Accuracy | `accurate_blending_unit` | 0 Min, **1 Basic**, 2 Medium, 3 High, 4 Full, 5 Maximum |
| Dithering | `dithering_ps2` | 0 Off, 1 Scaled, **2 Unscaled**, 3 Force 32bit |
| Texture Preloading | `texture_preloading` | 0 None, 1 Partial, **2 Full (Hash Cache)** |
| Manual HW fixes | `UserHacks` | **false** |
| HPO / Native Scaling / Bilinear dirty / CLUT | `UserHacks_HalfPixelOffset`, `UserHacks_native_scaling`, `UserHacks_BilinearHack`, `UserHacks_CPUCLUTRender`, `UserHacks_GPUTargetCLUTMode` | see §3 |
| Round sprite / Align sprite / Merge sprite | `UserHacks_round_sprite_offset`, `UserHacks_align_sprite_X`, `UserHacks_merge_pp_sprite` | 0/false |
| Aspect Ratio | `AspectRatio` | `Stretch`, **`Auto 4:3/3:2`**, `4:3`, `16:9`, `10:7` |
| FMV Aspect Ratio Override | `FMVAspectRatioSwitch` | **`Off`**, `Auto 4:3/3:2`, `4:3`, `16:9`, `10:7` |
| Deinterlacing | `deinterlace_mode` | **0 Auto** … 9 (same list as gsinterlacemode) |
| Anti-Blur | `pcrtc_antiblur` | **true** |
| Bilinear upscaling of the output | `linear_present_mode` | 0 Off, **1 Smooth**, 2 Sharp |
| Integer scaling | `IntegerScaling` | false |
| FXAA | `fxaa` | false |
| CAS | `CASMode`, `CASSharpness` | 0 None, 1 Sharpen Only, 2 Sharpen+Resize; sharpness **50** |
| Shade Boost | `ShadeBoost`, `ShadeBoost_Brightness/Contrast/Saturation/Gamma` | false; 50 each |
| TV Shader | `TVShader` | **0 None**, 1 Scanline, 2 Diagonal, 3 Triangular, 4 Wave, 5 Lottes CRT, 6 4xRGSS, 7 NxAGSS |
| Texture replacement | `LoadTextureReplacements`, `LoadTextureReplacementsAsync`, `PrecacheTextureReplacements`, `DumpReplaceableTextures`, `DumpReplaceableMipmaps`, `DumpTexturesWithFMVActive` | false, true, false, false… |
| OSD | `OsdShowFPS`, `OsdShowVPS`, `OsdshowPatches` | false |
| Speedhacks | `[EmuCore/Speedhacks] EECycleRate` (-3…3), `EECycleSkip`, `vuThread` (MTVU), `vu1Instant`, `WaitLoop`, `IntcStat` | 0, 0, … |

### 4.4 Texture replacement [Code-verified: `GSTextureReplacements.cpp`, `GSTextureReplacementLoaders.cpp` @ v2.6.3]
- **Folders:** `~/Library/Application Support/PCSX2/textures/SLUS-21351/dumps/` and `…/SLUS-21351/replacements/`. The folder is named by serial, not CRC. Both are created on first dump. Subfolders inside `replacements/` are scanned recursively. Directory names are case-sensitive, and PCSX2 warns if the case is wrong.
- **Dump filename scheme** (all lowercase hex):
  - `<TEX0hash>-<bits>.png` (no palette)
  - `<TEX0hash>-<CLUThash>-<bits>.png` (paletted)
  - `<TEX0hash>-r<W>x<H>-<bits>.png` / `<TEX0hash>-<CLUThash>-r<W>x<H>-<bits>.png` (region textures)
  - Mip levels add a `-mip<N>` suffix.
  - `<bits>` packs the TEX0 TBW/PSM/TW/TH and TEXA fields. Replacements must reuse the dump's exact name; only the extension may change.
- **Formats:** `.png` (RGBA8) and `.dds`: uncompressed RGBA, or BC1/DXT1, BC2/DXT2-3, BC3/DXT4-5, BC7 (DX10 header). Metal supports BC1–3 and BC7 (`dxt_textures`/`bptc_textures = true` in [GSDeviceMTL.mm](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/GS/Renderers/Metal/GSDeviceMTL.mm)).
- **Mipmaps:** DDS files can carry their own mip chain. If the game uses mipmapping, PCSX2 auto-generates mips for PNG, but **not for compressed DDS**, and shows a warning that you must *"generate mipmaps when compressing your textures"*.
- **Alpha:** dumps contain the raw GS-memory RGBA, with no alpha rescaling in the dump path. PS2 alpha 0x80 (128) means opaque, so dumps look semi-transparent in image viewers. Keep the dumped alpha range. This is inferred from code; worth checking with one test texture.
- Options: Load (`LoadTextureReplacements`), async loading, Precache (loads all into VRAM at boot), Dump (+mipmaps, +during FMV). If replacements exist and GPU Palette Conversion is on without Full preloading, paletted replacements are disabled. Replacement does not work on the Software renderer.
- No NFSMW PS2 HD texture pack was confirmed in my searches.

### 4.5 Post-processing on Metal [Code-verified]
- **Available:** FXAA, CAS (Contrast Adaptive Sharpening, `cas_sharpening = true` on Metal; "Sharpen Only (Internal Resolution)" or "Sharpen and Resize"), Shade Boost (brightness/contrast/saturation/gamma), TV shaders (Scanline, Diagonal, Triangular, Wave, Lottes CRT, 4xRGSS and NxAGSS supersampling), Anti-Blur, output Bilinear Smooth/Sharp, Integer Scaling ([present.metal](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/GS/Renderers/Metal/present.metal), [GraphicsPostProcessingSettingsTab.ui](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2-qt/Settings/GraphicsPostProcessingSettingsTab.ui)).
- **Not available:** user-loadable shaders. PCSX2 has no ReShade or librashader integration, and the v2.6.3 and master trees contain none.
- Metal uses framebuffer fetch on Apple GPUs (`framebuffer_fetch`), which makes higher Blending Accuracy relatively cheap.

### 4.6 macOS build notes
- **Rosetta 2:** v2.6.3 is built with `CMAKE_OSX_ARCHITECTURES="x86_64"` ([macos_build.yml](https://github.com/PCSX2/pcsx2/blob/v2.6.3/.github/workflows/macos_build.yml)). The official docs say M-series Macs run PCSX2 *"via the translation layer Rosetta 2"* ([requirements](https://github.com/PCSX2/pcsx2-net-www/blob/main/docs/setup/requirements.md)). v2.8.2 still releases one x86_64 macOS build; arm64 is built only for CI caching ([release_cut_new.yml](https://github.com/PCSX2/pcsx2/blob/v2.8.2/.github/workflows/release_cut_new.yml)).
- **patches.zip freshness:** each build downloads the then-latest `pcsx2_patches` release (`patchesUrl … releases/latest/download`, `macos_build.yml`). 2.6.3 therefore has only fobes' code for this disc, and 2.8.2 (latest stable, 2026-09-04) has the Steelmax set and `drawBuffering`.

---

## 5. Recommended "best quality" setup for an M5 Pro

These are my recommendations, built from the facts above. They have **not** been benchmarked on your machine.

**Keep GameDB in control**
- Renderer **Metal**.
- **Manual Hardware Renderer Fixes OFF**.
- Leave Native Scaling, HPO, CLUT and Bilinear Dirty on Automatic. GameDB sets them.

**Resolution and filtering**
- **Internal resolution 4x** (2560×1792 from 640×448) as the baseline. Try **5–6x** on a 4K or 5K display.
- Native Scaling "Aggressive" keeps the post-process passes at native resolution, which helps performance.
- If you see stutter, the cause is almost certainly EE/VU emulation under Rosetta, not the GPU.
- Texture filtering **Bilinear (PS2)**, Trilinear **Automatic**, Anisotropic **16x**, Mipmapping **on**.
- Blending **Basic**. Try **High** if transparency or lighting look wrong; framebuffer fetch makes it cheaper on Metal.
- Dithering **Unscaled**, Texture Preloading **Full**, Anti-Blur **on**, Deinterlace **Automatic**.

**Widescreen**
- Enable **Apply Widescreen Patches** globally (or `Enable = Widescreen 16:9` per game), with PeterDelta's BE file in `patches/`.
- Turn on the game's own widescreen option in its Options menu as well. The patch already forces it via the heap flag, so this is belt-and-braces. The menu option probably exists, based on the `VideoSettings` and widescreen symbols in the ELF, but I have not confirmed it in-game.
- Set **FMV Aspect Ratio Override = 4:3** so intros are not stretched. This may not trigger: PCSX2 detects FMVs through IPU use, and NFSMW's EA video format (reportedly VP6 or MPC) may be decoded on the EE. Unverified.

**Look**
- Brown filter off. Optionally turn off motion blur and bloom (§2.11 combinations).
- CAS **Sharpen Only, 40–60** if the upscaled image looks soft.
- Skip FXAA: it blurs HUD text and adds little at 4x+.
- Shade Boost is only worth using if you keep the sepia grading and want to rebalance it. Removing the filter with the patch is cleaner.

**Display**
- VSync on. 59.94 Hz content on a 120 Hz ProMotion panel is fine.
- Optionally show OSD FPS and VPS to confirm the internal 30 fps.

**Avoid**
- Manual HW fixes ON: it silently drops the GameDB fixes.
- Stacking two widescreen codes (PeterDelta + NMY + PSH5 or Felix) at once.
- PSH5's ported `00AAFDD4/0054BA38` widescreen on BE.
- The Software renderer at high resolution.
- Leaving texture dumping on during normal play: disk spam and stutter.
- Expecting `place=0` patches (Remove Blur, Remove Brume, Felix's) to take effect without restarting the game.
- EE overclocking or cycle-rate changes unless you have a reason. NFSMW is EE-heavy, and those settings can change game timing.
- Editing `patches.zip` inside the app bundle.

---

## 6. Resources for the long-term modding project
- **dbalatoni13/nfsmw**: a 1:1 decompilation that explicitly supports `SLUS-21351` (CC0-1.0). It includes a full `symbols.txt` (about 16k symbols) and `config.yml` section splits for your exact ELF: https://github.com/dbalatoni13/nfsmw. The README asks people not to release AI-generated ports.
- The PS2 "Visual Treatment" (post-processing) naming is confirmed by the Alpha 124 hacks gist ("Disable Visual Treatment") by Xanvier and nlgzrgn: https://gist.github.com/nlgxzef/e98503a67eeaa17ea83a258f650d1f99. That gist is for the **prototype** ELF (CRC DDA3B292), so its addresses do not apply to your disc.
- Mastercode for real hardware / OPL: `90497028 0C125BB2` ([PS2-Widescreen/Bare-Mastercodes-bin](https://github.com/PS2-Widescreen/Bare-Mastercodes-bin/blob/HEAD/MASTERCODES/SLUS_213.51.cht)). Felix's OPL cht uses `90436328 0C0C4D42`.

## 7. Source index
- PCSX2 v2.6.3 source: [Patch.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Patch.cpp), [Patch.h](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Patch.h), [Pcsx2Config.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Pcsx2Config.cpp), [Config.h](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Config.h), [GameDatabase.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/GameDatabase.cpp), [VMManager.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/VMManager.cpp), [GraphicsSettingsWidget.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2-qt/Settings/GraphicsSettingsWidget.cpp), [GamePatchSettingsWidget.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2-qt/Settings/GamePatchSettingsWidget.cpp), [GSTextureReplacements.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/GS/Renderers/HW/GSTextureReplacements.cpp), [GSTextureReplacementLoaders.cpp](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/GS/Renderers/HW/GSTextureReplacementLoaders.cpp), [GSDeviceMTL.mm](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/GS/Renderers/Metal/GSDeviceMTL.mm), [GameIndex.yaml](https://github.com/PCSX2/pcsx2/blob/v2.6.3/bin/resources/GameIndex.yaml), [GameIndex.md](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/Docs/GameIndex.md), [Configuration Guide](https://github.com/PCSX2/pcsx2/blob/v2.6.3/pcsx2/Docs/Configuration_Guide/Configuration_Guide.md).
- Releases: [v2.6.3](https://github.com/PCSX2/pcsx2/releases/tag/v2.6.3) (2026-01-28), [v2.8.2](https://github.com/PCSX2/pcsx2/releases/tag/v2.8.2) (2026-09-04).
- Patch repos: [PCSX2/pcsx2_patches](https://github.com/PCSX2/pcsx2_patches), [PeterDelta/PCSX2](https://github.com/PeterDelta/PCSX2), [felixthecat1970/gamepatches](https://github.com/felixthecat1970/gamepatches), [Saramagrean/NetherSX2-cheats](https://github.com/Saramagrean/NetherSX2-cheats), [PSH5JB/PSH5](https://github.com/PSH5JB/PSH5), [root670/CheatDevicePS2](https://github.com/root670/CheatDevicePS2), [EmulatorArchive/pcsx2-rr](https://github.com/EmulatorArchive/pcsx2-rr).
- Docs: [writing-patches.md](https://github.com/PCSX2/pcsx2-net-www/blob/main/docs/advanced/writing-patches.md), [requirements.md](https://github.com/PCSX2/pcsx2-net-www/blob/main/docs/setup/requirements.md).
- Issues: [#3993](https://github.com/PCSX2/pcsx2/issues/3993), [#2115](https://github.com/PCSX2/pcsx2/issues/2115), [#1913](https://github.com/PCSX2/pcsx2/issues/1913), [#10351](https://github.com/PCSX2/pcsx2/issues/10351), [#11580](https://github.com/PCSX2/pcsx2/issues/11580), [#13642](https://github.com/PCSX2/pcsx2/issues/13642), [#4286](https://github.com/PCSX2/pcsx2/issues/4286).
- Frame rate: [DarkZero PS2 review](https://darkzero.co.uk/game-reviews/need-for-speed-most-wanted-ps2/).
- Not readable (bot check): [PCSX2 forum 60 fps codes p.128](https://forums.pcsx2.net/Thread-60-fps-codes?page=128), wiki.pcsx2.net NFSMW page, wsgf.org, consolemods.org.
