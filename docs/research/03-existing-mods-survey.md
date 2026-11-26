# 03 — Existing mods survey: NFS Most Wanted (2005), PS2 focus

**Target:** NFS Most Wanted Black Edition, PS2 NTSC-U `SLUS-21351`, PCSX2 CRC `0518D274`, running on PCSX2 2.6.3 (Apple Silicon Mac).
**Date of survey:** 2026-11-24
**Method:** Web reading only (search engines, GitHub API, project pages). No files were downloaded or run, and nothing under the PS2Games folder was touched.

> **Coverage caveats**
> - Reddit, Nexus Mods listings, GBAtemp, the PCSX2 wiki and some news sites refused automated reads (403 errors or blocks). Community-demand evidence comes from EA forums, mod-site download counts, mod changelogs and GitHub instead.
> - YouTube video descriptions could not be read. Only titles and channel names (via oEmbed) were checked, so texture packs shown only in videos are **unverified**.
> - Pages fetched from **tcrf.net** (The Cutting Room Floor) came back with text aimed at AI agents (asking for a `key.bin` file to be written and money to be moved) instead of the article. That text was ignored. TCRF facts here come from search snippets and Hidden Palace only. Read TCRF yourself in a browser.

---

## 0. TL;DR

1. **The PS2 version has almost no "mods" in the PC sense, only PCSX2 patches.** For `SLUS-21351`, PCSX2's built-in patch database already has toggles for: disable all post-processing, disable brown filter, disable bloom, disable motion blur, and no distant fog (Steelmax, fobes; added March 2026). **No widescreen patch for SLUS-21351 is in the official database yet**, but PeterDelta's personal repo has one. **No 60 FPS patch exists for any MW PS2 version.** It does exist for NFSU and NFSU2 PS2 (asasega).
2. **The biggest enabler is the decompilation.** [dbalatoni13/nfsmw](https://github.com/dbalatoni13/nfsmw) (CC0) targets **exactly our build, SLUS-21351**, with a public `symbols.txt` of about 16,000 named functions and variables (for example `PrepareRealTimestep`, `Sim::GetTimeStep`, `IVisualTreatmentPlat::RenderMWVisualLook`, `DebugWorldCameraMover`, `VOTimeOfDay`, `GRaceParameters::GetCatchUp`, `SetTrafficDensity`). This turns PS2 reverse engineering from guesswork into lookup.
3. **A native PS Vita port of SLUS-21351** ([germanalvarez15/nfsmw-vita](https://github.com/germanalvarez15/nfsmw-vita), Oct 2026, built on the decomp) shows what the PS2 code can do on better hardware: a 30/60 FPS toggle, MSAA, trilinear mipmaps, a high-LOD option, dynamic car reflections, an MW colour-filter toggle, stick sensitivity, and split-screen and online multiplayer. It has no license, so use it for ideas only.
4. **SpeedBreaker** ([SpeedBreakerProject/speedbreaker](https://github.com/SpeedBreakerProject/speedbreaker), GPL-3.0) is a static recompilation of the **Xbox 360** version. It runs **natively on Apple Silicon Macs** at up to 4K and 60 FPS (120 with Smooth Motion), with ultrawide support, HDR, cops in Quick Race and wheel support. It is the bar the community will compare any "modern MW" against, so our PS2 mod needs its own reason to exist: authenticity to the PS2 version, real-hardware compatibility, and data and gameplay mods.
5. **PC data mods are largely portable.** The PS2 disc carries the same `GLOBAL\ATTRIBUTES.BIN`, `FE_ATTRIB.BIN` and `GAMEPLAY.BIN` VLT/AttribSys databases as PC. Attribulator already loads **Carbon PS2** VLT files as the same little-endian x86 layout. Pursuit, handling, traffic, catch-up and race tweaks made with VltEd on PC are therefore *plausibly* portable to PS2. Shader and DirectX features (Xbox 360 Stuff Pack, NextGenGraphics, ReShade, SMAA) are not.
6. **PS2 files are packed** in `NFS\ZZDATA0-3.BIN` with a hashed index, `NFS\ZDIR.BIN`. Entries are hash, file number, sector, size and checksum, and the hash is a DJB-style hash of the uppercase path seeded with `0xFFFFFFFF`. [Ekey/NFS.BIN.Tool](https://github.com/Ekey/NFS.BIN.Tool) unpacks and repacks these, and lists MW PS2 as tested for unpacking. During development, PCSX2's **Host Filesystem** mode avoids repacking entirely.
7. **Mac constraint:** the PCSX2 code-plugin route (ASI-Factory "PCSX2 Fork With Plugins" + ThirteenAG's PCSX2PluginInjector, used by xan1242's MW ICE plugin and the GTA/Burnout "FusionFix" PCSX2 plugins) is **Windows-only**. ReShade is also not available. On Mac our tools are: pnach patches (code caves included), Host FS, PCSX2 texture replacement, PCSX2 GS options, and ISO xdelta patches.

---

## 1. PS2-specific work for NFSMW

### 1.1 PCSX2 patch (pnach) coverage per serial

Source: [PCSX2/pcsx2_patches](https://github.com/PCSX2/pcsx2_patches). These ship inside PCSX2. The repo has **no license file**, and its README says the maintainers only distribute patches, they don't author or fix them.

| Serial / CRC | Edition | Patches in PCSX2 DB (author) | Missing |
|---|---|---|---|
| **SLUS-21351 / 0518D274** (our target) | Black Edition NTSC-U | Disable All Post-processing (Steelmax: removes sepia, bloom and rain softening, not motion blur); Disable Brown Filter (fobes); Disable Bloom (Steelmax); Disable Motion Blur Effect (Steelmax); No Distant Fog (Steelmax). Added in [PR #688](https://github.com/PCSX2/pcsx2_patches/pull/688), Mar 2026; the brown-filter patch dates from Sep 2023. | **Widescreen**, 60 FPS, controls, gameplay |
| SLUS-21267 / 270F8C03 | Original NTSC-U | Widescreen 16:9 (PeterDelta), Remove Brown Filter (fobes), Remove Blur (PeterDelta) | 60 FPS |
| SLES-53857 / 805C3B3A | Black Edition PAL | Widescreen 16:9 (PeterDelta), Languages (Felixthecat1970), NTSC Mode, plus Steelmax's 5 filter patches | 60 FPS |
| SLES-53558 / 1FA82CDF | PAL-M | Remove Brown Filter only | WS, 60 FPS |

**Outside the official DB:** [PeterDelta/PCSX2](https://github.com/PeterDelta/PCSX2) (no license) has `SLUS-21351_0518D274.pnach` with a **Widescreen 16:9** patch (two writes plus `gsaspectratio=16:9`, last updated Aug 2026), as well as Remove Brown Filter, Remove Blur and Remove Brume (background haze). This is the widescreen patch for our build. **Don't duplicate it. Credit and link it**, and if it works, encourage or help upstream it with the author's agreement.

**PCSX2 GameDB** (`GameIndex.yaml`) settings for SLUS-21351 are applied automatically: bilinear upscale for sun glare, Half-Pixel Offset "special", native scaling for post effects, **CPU CLUT render for the final colour-adjustment LUT**, GPU target CLUT (stops the sun shining through bridges), and draw buffering. This confirms that the PS2 *visual treatment* finishes with a palette/LUT pass, which matters for any colour-grading mod (§5, idea 3).

**Related NFS PS2 patches that act as models:**
- NFSU (`SLUS-20811`): 60 FPS (asasega, needs about 130% EE overclock), widescreen, **SilentPatch** and **trigger control mappings** (Silent/CookiePLMonster), both now merged into PCSX2. See [silentsblog.com/mods/need-for-speed-underground-ps2](https://silentsblog.com/mods/need-for-speed-underground-ps2).
- NFSU2 (`SLUS-21065`): 60 FPS (asasega, up to 180% EE overclock, cutscenes speed up), several widescreen ratios, plus kozarovv's filter, fog, rain, car-brightness and light-flare tweaks. **The NFSU2 list is a template for what we could offer for MW.**
- HP2 (`SLUS-20362`): Silent's **Disable Catchup**, **Speed Boost for all cars** and trigger mappings ([silentsblog HP2 page](https://silentsblog.com/mods/need-for-speed-hot-pursuit-2-ps2), [Console-Cheat-Codes](https://github.com/CookiePLMonster/Console-Cheat-Codes), MIT).
- Carbon and ProStreet PS2: Silent's "Fix Most Wanted Black Edition save detection" (Carbon checks the wrong BE serial when granting its cash bonus). Worth noting because our BE saves are what Carbon looks for.

### 1.2 PS2 mods, plugins and cheats

| Name | Author | Link | What it does | Distribution | License | Status | Media |
|---|---|---|---|---|---|---|---|
| NFSMW-PS2-ICE | xan1242 | [GitHub](https://github.com/xan1242/NFSMW-PS2-ICE) | Restores the hidden **ICE (in-game camera editor)** in the **OPM demo** build and wires up the **Debug World Camera**. Rebuilds ScreenPrintf and 2D poly rendering. NTSC-U demo only. | `.elf` plugin for *PCSX2 Fork With Plugins* (Windows) plus pnach to unlock the demo | MIT | Active (2025–26) | Screenshots in repo |
| PCSX2 Fork With Plugins / PCSX2PluginInjector | ASI-Factory / ThirteenAG | [fork](https://github.com/ASI-Factory/PCSX2-Fork-With-Plugins), [injector](https://github.com/ThirteenAG/PCSX2PluginInjector) | Injects C/C++ ELF "guest modules" into PS2 games. Needs PCSX2's 128 MB RAM option. Pinned to a specific PCSX2 dev build. | Windows binaries | GPL-3.0 (fork) | Active | — |
| ps2plugininjector | ermaccer | [GitHub](https://github.com/ermaccer/ps2plugininjector) | Experimental: adds a new ELF section with plugin code and hooks the first EI call, so plugins can run on **real PS2**. Fixed load address, no relocation. | CLI | MIT | Dormant (2023) | — |
| HD texture packs for MW BE (PCSX2) | unknown (videos by "Emulation_Wrld", "SnowAngel 05") | [YouTube 1](https://www.youtube.com/watch?v=R28TqTyQrYg), [YouTube 2](https://www.youtube.com/watch?v=uhKdaZcyUzM) | Videos titled "HD Texture Pack" for BE on PCSX2 1.7 and later. Content, source and author **unverified**; the pack was probably built from PC or Xbox 360 textures. | PCSX2 `textures/SLUS-21351/replacements` | Unknown (probably EA-derived) | Unknown | Videos |
| GBAtemp "PCSX2 HD texture pack group" | various | [thread](https://gbatemp.net/threads/my-ps2-packs.621422/) | A poster lists NFS MW among their packs (MediaFire). Couldn't verify (403). The same poster made **GameCube** MW and NFSU2 packs for Dolphin from PC and Xbox 360 textures. | texture folders | Unknown | Unknown | — |
| CodeBreaker codes for MW BE | collected by Almar's Guides | [page](https://almarsguides.com/retro/walkthroughs/PS2/Games/NeedForSpeedMostWanted/CodeBreaker/BlackEdition/) | Cash, bounty, all cars (not saved), infinite NOS and Speedbreaker, completion flags, 1-racer races. | CB codes (convertible to pnach) | n/a | Static | — |
| "Debug car customize" cheat for BE | "NMY" | [YouTube](https://www.youtube.com/watch?v=sFeJCfGMOPY) | Shows a cheat that enables the debug car-customize menu on AetherSX2/PCSX2. Code source not visible. | cheat | n/a | — | Video |
| Alpha 124 debug-menu research | nlgxzef | [YouTube](https://www.youtube.com/watch?v=6D9AYPVkVAo), [PC equivalent](https://www.youtube.com/watch?v=SJD3TPQ4iik) | nlgxzef found a debug-menu opacity setting by reading the Alpha 124 `NFS.elf` in IDA. Also shows Debug Car Customize on the main menu (PC). | video | — | — | Video |
| PS2 Vita settings plugin | Electry | [NFSMW_Settings](https://github.com/Electry/NFSMW_Settings) | Fixes for the older *PS2-data-on-Vita* builds (2021 "Redux Vita" and "Updated Gameplay Vita" on GameBrew). | plugin | — | Old | — |

**Also found, with caution:** [marcothu2001-coder/nfs-most-wanted-android](https://github.com/marcothu2001-coder/nfs-most-wanted-android), billed as a "PS2 → Android native port" with automated CI builds. It has 0 stars and no license, and looks low-effort. Don't rely on it or run it.

### 1.3 Prototypes and demos (PS2 unless noted)

| Build | Date | Source | What it reveals |
|---|---|---|---|
| OPM demo (Official PS2 Magazine Special Ed. 2005/03) | Jul 24, 2005 | [Hidden Palace](https://hiddenpalace.org/Need_for_Speed:_Most_Wanted_(Jul_14,_2005_demo)) | Beta HUD, cut customisation gauges, older police cruiser and helicopter models. The **ICE editor** exists in it (xan1242). The **Beta Content Mod** (PC) ported cop models and HUD ideas from it. Boots to the menu in PCSX2 2.0. |
| **Alpha 124** | Sep 20, 2005 | [Hidden Palace](https://hiddenpalace.org/Need_for_Speed:_Most_Wanted_(Sep_20,_2005_prototype)), TCRF "PS2 Alpha 124" | Ships **`NFS.ELF` with debug symbols and `NFS.MAP`** (linker map). **L3+R3 opens a debug menu.** A one-line patch enables Black Edition mode (found by Zenii). Includes demos of other EA games. The decomp's PS2 PAL target (`SLES-53558-A124`) and the PC **Unlimiter** both credit these symbols. |
| Beta 1.1 | Oct 12, 2005 | [Hidden Palace](https://hiddenpalace.org/Need_for_Speed:_Most_Wanted_(Oct_12,_2005_prototype)) | 3 days before the EU final. Contents not documented. |
| Alpha 138 (Xbox, review code) | Oct 8, 2005 | [Hidden Palace](https://hiddenpalace.org/Need_for_Speed:_Most_Wanted_(Oct_8,_2005_prototype)) | Debug enabled. Needs 128 MB (dev-kit RAM). |
| "EuropeGerMilestone" (Xbox 360) | Oct 21, 2005 | decomp target | Used for symbol matching. |

**Takeaway:** we don't need prototype *files*; the decomp has already pulled symbols from Alpha 124 and the GameCube build and mapped them onto SLUS-21351. Prototypes matter mainly as references for **cut content** (beta HUD, cut races, cut cars) and **debug features** that still exist in retail code.

### 1.4 Decompilation, ports and recompilations (the 2026 landscape)

| Project | Base | Link | Relevance to us | License |
|---|---|---|---|---|
| **NFS:MW decompilation** | GC USA, X360 proto, **PS2 Alpha 124, PS2 SLUS-21351**, PC 1.3 | [dbalatoni13/nfsmw](https://github.com/dbalatoni13/nfsmw) ([progress](https://decomp.dev/dbalatoni13/nfsmw): PC about 14% decompiled) | **Primary RE resource.** `config/SLUS-21351/symbols.txt` has about 16k symbols. The config pins the BE ELF `SLUS_213.51` at SHA-1 `60289f3f8b643822a302720f9649ddc4ba55034b`, which we can use to verify the user's dump. Has a Discord. Its README strongly rejects AI-generated ("slop") contributions and ports: don't send AI-written code there, and credit it prominently. Says future ports will appear under the "RevEngin3" org. | CC0-1.0 |
| NFSMW Vita | **PS2 BE SLUS-21351** data + decomp | [germanalvarez15/nfsmw-vita](https://github.com/germanalvarez15/nfsmw-vita) | Proof that the PS2 code supports 60 FPS, LOD High, dynamic reflections, an MW filter toggle, stick sensitivity and multiplayer. Documents the `ZDIR.BIN` hash and index format in its data-extraction script. Imports and exports PS2 BE saves (`BASLUS-21351-<NAME>`, 62,689 bytes). | **None** (all rights reserved: ideas only) |
| SpeedBreaker | X360 USA/PAL-ES | [GitHub](https://github.com/SpeedBreakerProject/speedbreaker), [site](https://projectugarte.com/speedbreaker/) | Native on **Apple Silicon**, 4K, 60/120 FPS, ultrawide with correct HUD, HDR, split-screen, cops in Quick Race, wheel support. **The benchmark to differentiate from.** | GPL-3.0 |
| Other 2026 recomps | X360 / PC | [nfsmw-nx](https://github.com/StevensND/nfsmw-nx), [NFSMW-Recompiled](https://github.com/madelrandel-blip/NFSMW-Recompiled), [nfsmwpc-recompiled](https://github.com/codepdbh/nfsmwpc-recompiled), [veritr1x/nfsmw-recomp](https://github.com/veritr1x/nfsmw-recomp), [nfsmw-linux](https://github.com/Katon17/nfsmw-linux), [nfsmw-android](https://github.com/codepdbh/nfsmw-android), [nfsmw-ios](https://github.com/bep1s69/nfsmw-ios) | Show how much demand there is for "modern MW". None targets PS2. Mostly bring-your-own-disc, GPL-3.0. Quality varies; read, don't run. | mostly GPL-3.0 |

### 1.5 PS2 tooling that exists (and the gaps)

| Need | Existing tool | PS2 MW support | Gap / note |
|---|---|---|---|
| Unpack/repack disc archive (`ZDIR.BIN` + `ZZDATA*.BIN`) | [Ekey/NFS.BIN.Tool](https://github.com/Ekey/NFS.BIN.Tool) (C#, no license); NFS-Disculator ([blog](https://nfs-tools.blogspot.com/2018/09/nfs-disculator-v10-released.html), extract only) | Unpack tested on MW PS2. The author says the packer is untested in-game. | A Mac-friendly, open-source Python unpacker/repacker with checksum handling would be a good first tool for our repo. |
| Filename hash | algorithm shown in nfsmw-vita's extract script (DJB-style, seed `0xFFFFFFFF`, uppercase, backslashes) | yes | Reimplement it independently; don't copy their unlicensed code. |
| VLT/AttribSys (`attributes.bin`, `fe_attrib.bin`, `gameplay.bin`) | [NFSTools/Attribulator](https://github.com/NFSTools/Attribulator) (has `CarbonProfilePs2`, x86 DB layout, same 3 files as MW PC); NFS-VltEd 4.6 (Windows); attribute-name cracks in [s-b-repo/nfsmw-2005-re](https://github.com/s-b-repo/nfsmw-2005-re) | **No MW-PS2 profile yet**, but the MW PC profile uses the same files and DB type | A **"MostWantedProfilePs2"** (probably trivial) would make all VLT-script mods portable. Verify on real data. Attribulator has no license file; check before redistributing it. |
| Global/BUN/texture packs (TPK) | Binary (Windows, PC formats); [xnfstpktool](https://github.com/xan1242/xnfstpktool) (PC/PS3/X360/Xbox, **no PS2**) | No | PS2 TPK (paletted, swizzled) editing is a real gap. For visuals, **PCSX2 texture replacement** sidesteps it entirely. |
| Code patches | pnach (PCSX2 native); PluginInjector (Windows only) | yes | On Mac, write MIPS code caves as pnach, or patch the ELF and ship as xdelta. |
| HUD/FE (FEng) layouts | Binary FEng editor (PC) | unknown | Investigate later. |

---

## 2. PC mod ecosystem (idea sources) and PS2 portability

Portability key:
- **Data**: an edit to VLT/GIN databases (`attributes.bin` / `gameplay.bin`) that should carry across to the PS2 disc. Needs a repack or Host FS.
- **pnach**: changes hard-coded constants or logic; reimplement as an EE memory patch using the SLUS-21351 symbols.
- **Tex**: texture work; on PS2 only through PCSX2 texture replacement or a (missing) PS2 TPK tool.
- **No**: a PC renderer, shader, DirectX or Windows feature.
- **Native**: already present on PS2.

| Mod | Author | Link | What it changes | Mechanism (PC) | Portable to PS2? |
|---|---|---|---|---|---|
| Widescreen Fix | ThirteenAG | [WFP](https://github.fusionfix.io/wfp) / [releases](https://github.com/ThirteenAG/WidescreenFixesPack/releases/tag/nfsmw) | Any resolution, corrected aspect, HUD scaling and constraints, hor+ FOV, FMV scaling, **sim-rate/FPS options**, shadow resolution and fixes, console gamma, SMAA, re-enables UG2 leftovers ("Light Streaks", "Bleach Bypass"), **stick/mouse free-look**, deadzones, skip intro. Most downloaded MW mod (about 917k on GamePressure). | ASI | **Partial.** WS FOV/aspect via pnach (exists, see §1.1). FPS/sim rate via pnach (to do). Deadzones and free-look via pnach (feasible). Gamma or LUT via the VT pass. SMAA, shadow formats: No (use PCSX2 GS features). |
| HUD Adapter | nlgxzef | [GitHub](https://github.com/nlgxzef/NFSMWHUDAdapter) | Repositions HUD objects for the aspect ratio | ASI | **pnach/Native.** The PS2 ELF already has `FEngHud::SetWideScreenMode` and `Minimap::AdjustForWidescreen`. |
| **Extra Options (ExOpts)** | ExOpts Team (nlgxzef et al.) | [GitHub](https://github.com/ExOptsTeam/NFSMWExOpts) | Big ini of tweaks (hotkeys, limits, pursuit, weather, fixes). See the feature table below. | ASI, GPL-3.0 | **Mostly pnach.** Most options change constants or limits. Hotkeys need code. |
| Race Fixes | ExOpts Team | [GitHub](https://github.com/ExOptsTeam/NFSMWRaceFixes), [nfsmods](https://nfsmods.xyz/mod/2302) | Starting pursuit condition and heat in races, cops in races, traffic-density fix | ASI + data | **pnach + Data** |
| Unlimiter | nlgxzef | [GitHub](https://github.com/nlgxzef/NFSMWUnlimiter), [nfsmods](https://nfsmods.xyz/mod/688) | Dynamic car array for add-on cars, rain flicker fix, bigger memory pools, extra customisation menus, Backroom, separate front/rear rims, reverting to stock parts, beta features with the Beta Content Mod | ASI + FNG | **Mostly No/hard.** Adding cars on PS2 is limited by memory (32 MB) and the PS2 TPK and geometry formats. Small fixes may be pnach. |
| XNFSMusicPlayer | xan1242 | [GitHub](https://github.com/xan1242/XNFSMusicPlayer) | Custom playlists, many formats, streaming radio, chyron metadata | ASI + BASS, MIT | **Partial.** A user-supplied music *replacement* tool that re-encodes the user's own songs into EA stream format and repacks them is plausible. Streaming and new formats: No. |
| XenonEffects | xan1242 | [GitHub](https://github.com/xan1242/NFSMW_XenonEffects) | Back-ports sparks and contrails missing from PC MW | ASI, MIT | **Native.** The README notes these effects were on the consoles, so PS2 should already have them. |
| Xbox 360 Stuff Pack (v4.2) | community, uploaded by elaymm4 | [nfsmods](https://nfsmods.xyz/mod/1200) | X360 shaders (car paint with flakes, normal maps, AO), next-gen sky, water, X360 TOD lighting and visual treatment with adaptive exposure, rain droplets, X360 textures | Shaders + EA X360 assets | **No** (shaders). Using its *VT and TOD values* as a grading reference is OK. Its textures are EA assets and must not be redistributed. |
| NextGenGraphics / Sun Set | Sh2dow / ARCHIE (ArchieGoldmill) | [Sun Set nfsmods](https://nfsmods.xyz/mod/6659), [GitHub](https://github.com/ArchieGoldmill/NFSMW_SunSet) | Full day/night cycle, custom world and car shaders, road reflections, dynamic lighting | D3D shader replacement. Repo license says "no redistribution". | **TOD cycle: maybe** (pnach driving `SetCurrentTimeOfDay`; lighting may not blend). Shaders: **No**. |
| Improved Visual Treatment | ShreN | [nfsmods](https://nfsmods.xyz/mod/822) | Keeps the MW look without the yellow cast (lower RGB, saturation and colour bloom) | ExOpts values | **pnach/Data**, if PS2 VT parameters are data. The PCSX2 DB already offers on/off toggles; *tunable* grading would be new. |
| Motion Blur Controller | nlgxzef | [GitHub](https://github.com/nlgxzef/NFSMWMotionBlurController) | Toggle and adjust motion-blur strength and trigger speed | ASI | **pnach** (on/off already in PCSX2 DB; strength would be new) |
| HD Reflections / FE Shadows | AeroWidescreen | [FEShadows](https://github.com/AeroWidescreen/NFSMWFEShadows) | Higher reflection resolution; shadows in menus | ASI | **Maybe** (reflection render-target size is a constant; VRAM-limited on PS2, but PCSX2 upscaling helps) |
| NFS MW REDUX | KryZee | [GamePressure](https://www.gamepressure.com/download/need-for-speed-most-wanted-nfs-most-wanted-redux-ultimate-overha/z1142f4) | Modpack (v2.3, Oct 2022, about 5 GB, 331k downloads): X360 textures and shaders, remastered HUD, HD skyboxes, playable cops, new race options, cars, adjustable TOD | Bundle of the above | **Concept only.** Shows a curated "everything pack" is what players install. |
| Pepega Edition | Team Pepega | [TV Tropes](https://tvtropes.org/pmwiki/pmwiki.php/VideoGame/NeedForSpeedMostWantedPepegaEdition) | Meme total-conversion (Java-game cars, memes), April 2021 | Data/assets | **Not a goal.** Only interesting as proof that a heavy data-only conversion is possible. |
| Beta Content Mod | elaymm4 et al. | [nfsmods](https://nfsmods.xyz/mod/1711) | Cut cars (350Z, R34, M3 E46, M3 CSL, RX-8), beta HUD (north-up minimap), cut drag races, "Cash Grab" mode, beta UI and text, older cop models from the OPM PS2 demo | Data + assets | **Partial.** Cut races and text/data: Data. Cars and HUD art: hard on PS2. Borrow the *research* and credit it. |
| Revamped Pursuit System | Splash45 & Frexellia | [nfsmods](https://nfsmods.xyz/mod/1698) | Faster response, reworked heat 1–10 lineups, more units, roadblocks and spikes, helicopters at heat 8–10 | VltEd + ExOpts | **Data** (+ pnach for the ExOpts parts) |
| Crossfire Pursuit Mod | Orsal | [nfsmods](https://nfsmods.xyz/mod/7141) | Harder heat 1–6 and special heat 7–10 modes (bounty, road rage, FBI). Speedbreaker becomes 2 s of invincibility. Lower cop mass. Pursuit leaders. | VltEd + Binary (May 2026) | **Data** for VLT parts. Binary/global parts: unknown on PS2. |
| Bartender | rng-guy | [GitHub](https://github.com/rng-guy/NFSMWBartender) | Per-heat hard-coded pursuit parameters (busted and evade bar rates, passive bounty, combo bounty, cop spawning tables) | ASI, CC0 | **pnach.** CC0 means its *findings* (which constants matter) can be reused freely. |
| Modernization Patch | iammostwanted | [nfsmods](https://nfsmods.xyz/mod/3007) | Career pacing, more cops and roadblocks, longer cooldowns, less low-speed wheelspin, YAW_CONTROL off, +15% grip, re-tuned weak cars, AI collision fix | VltEd (.nfsms) | **Data** |
| UG2ish handling / Wheelspin & Grip Fix / ReTuned Vanilla Cars | Fierelier / iammostwanted / AlanGamer456 & Wiler | [4589](https://nfsmods.xyz/mod/4589), [465](https://nfsmods.xyz/mod/465), [6829](https://nfsmods.xyz/mod/6829) | Handling re-tunes (chassis, tires, rigidbody, pvehicle) | VltEd | **Data** |
| Performance Scaling (no rubber-band) | gaycoderprincess | [GitHub](https://github.com/gaycoderprincess/MostWantedPerformanceScaling) | Turns off catch-up and makes opponent pace come from raw performance | ASI, MPL-2.0 | **Data/pnach.** Catch-up parameters live in `GRaceParameters` (gameplay DB). |
| Time Trial Ghosts | gaycoderprincess | [GitHub](https://github.com/gaycoderprincess/MostWantedTimeTrialGhosts) | TrackMania-style ghosts, file checksums, 120 Hz physics tick | ASI, MPL-2.0 | **Hard.** PS2 has `Joylog` input-recording code, but saving ghosts needs memory-card or Host FS I/O. |
| Racer AI Traffic / Chaos / Powerups etc. | gaycoderprincess | [profile](https://github.com/gaycoderprincess) | Fun experiments | ASI | Low priority |
| Effects Overhaul | Opex Rah (Fiscal) | [nfsmods](https://nfsmods.xyz/mod/5415) | Remade smoke, debris, NOS, finish-line and trigger effects | Binary + VLT | **Data** for emitter VLTs. Textures need PS2 TPK work. |
| 8K & 4K Remade Textures | CaptainTany, Awentador et al. | [nfsmods](https://nfsmods.xyz/mod/5279) | Roads, sidewalks, cliffs, grass and more. Updated Nov 2026. | TexWizard | **Tex** via PCSX2 replacement, **only with the authors' permission**. The art may be partly derived from EA textures. |
| Upscaled HUD | LunicAura106 | [nfsmods](https://nfsmods.xyz/mod/1096) | UI recreated **mostly from vector art** (logos, fonts, rival portraits, track maps). Updated Jan 2026. | Binary textures | **Tex** via PCSX2 replacement. Ask the author first (no license stated). The best candidate for an "HD UI" pack. |
| Remastered UI | edoo1 | [GamePressure](https://www.gamepressure.com/download/need-for-speed-most-wanted-2005-remastered-ui-v24-mod/zf142f2) | HUD restyle | Textures | **Tex** (with permission) |
| Camera ToolKit | ARCHIE | [nfsmods](https://nfsmods.xyz/mod/6681) | Free camera, attach and look-at, game speed and pause, FOV keys, HUD hide, forced LOD | Script/ASI | **pnach.** `DebugWorldCameraMover` (with TurboSpeed and SuperTurboSpeed) exists in the BE ELF. |
| Realistic Plugin | Racer_S | [GamePressure](https://www.gamepressure.com/download/need-for-speed-most-wanted-2005-nfsmw-realistic-plugin-v40-mod/z614fb9) | Wheel and gamepad force feedback, steering rate, crash-cam off | external exe | **Partial.** Steering-rate curves via pnach. Force feedback is limited to PS2 rumble. |
| XtendedInput | xan1242 | [GitHub](https://github.com/xan1242/NFS-XtendedInput) | XInput, button prompts | ASI, MIT | **Native** (PS2 pad) |
| MWCrashFix / OpenLimitAdjuster | x0reaxeax / gaycoderprincess | [1](https://github.com/x0reaxeax/MWCrashFix), [2](https://github.com/gaycoderprincess/MostWantedOpenLimitAdjuster) | Pursuit crash fix, bigger memory limits | ASI | Check whether the crash happens on PS2. Pool limits are constrained by 32 MB (or 128 MB in PCSX2's dev-RAM mode, which real hardware lacks). |
| ENIS Time-of-Day fix | xan1242 | [GitHub](https://github.com/xan1242/NFSMW_ENISTimeOfDay) | Fixes TOD in story cutscenes | ASI | Check whether it applies on PS2 |
| Traffic Pack V4 / On the Edge / More Opponents | AJM3899 / Balathruin / Hier | [Traffic Pack](https://www.nfsaddons.com/downloads/nfsmw/cars/traffic/4838/nfsmw-traffic-pack-v4.html), [More Opponents](https://nfsmods.xyz/mod/2612) | More traffic variety and density, unused traffic cars, more opponents | ModLoader / VltEd / ExOpts | **Data** for density, pattern and opponent count. New traffic models: No. |
| Save editors | x07x08, BilawalAhmed0900 | [web](https://github.com/x07x08/nfsmw-save-editor), [NFSMWSaveE](https://github.com/BilawalAhmed0900/NFSMWSaveE) | Save editing (PC) | — | PS2 save structure differs (62,689-byte profile); could be adapted. |

### 2.1 Extra Options feature by feature (from `NFSMWExtraOptionsSettings.ini`)

| ExOpts option group | Examples | PS2 route | Value for a "modern" feel |
|---|---|---|---|
| Lap/opponent controllers | 0–127 laps, up to 15 opponents, unique opponent cars, bonus or cop cars for AI | pnach on limit constants (watch PS2 memory with many AI cars) | Medium |
| Traffic controllers | density values for Low/Med/High (10/30/50 by default) | pnach on constants (or GRaceCustom::SetTrafficDensity) | High (denser, livelier city) |
| Pursuit | heat override min/max, in-race pursuit action mode, show cops on minimap, infraction speed limits, uncensored busted screen | pnach | High |
| Weather | Always rain, rain amount, size, intensity, speed, gravity, road reflection | pnach on floats | High (weather variety) |
| Gameplay | hidden camera modes, **debug world camera**, HUD on look-back, game speed, car scale, remove neon barriers, **no catch-up**, no rev limiter, Speedbreaker recharge time, speed and mass, impound strikes, car-lot limit, max-perf shop key, world map anywhere | pnach | High (camera, no catch-up), Medium (others) |
| Menu | show Time of Day option (X360), hidden tracks, better random races, replay Blacklist races, longer profile names, Debug Car Customize, showcase-cam hacks | pnach (PS2 has a `VOTimeOfDay` video-option widget) | High (TOD), Medium |
| Fixes | Helicopter and zero-bounty fixes, D-Day fix, unused Busted NISs, car-skin and immobile-collision fixes | pnach (check each bug exists on PS2) | Medium (polish) |
| Misc | skip movies and NISs, force car and tire LOD, screen printf | pnach | Low–Medium |

---

## 3. Modern-racer features people want, and where they've been done

Evidence comes from EA forum remaster threads (requests for modern graphics and physics, better AI, online, 4K/60, DualSense haptics, a bigger map, the original soundtrack, and Heat-style customisation), GamePressure download counts (Widescreen Fix 917k, REDUX 331k, Save Editor 227k, Extra Options 186k, HD Reflections 75k, Remastered UI 75k), and the features that the 2026 ports (SpeedBreaker, Vita) chose to build first.

| Feature | Demand signal | Done on PC? | Done elsewhere? | PS2 feasibility |
|---|---|---|---|---|
| Widescreen / ultrawide with correct HUD | Highest (WS Fix downloads) | Yes (WSFix, HUD Adapter) | SpeedBreaker, Vita | **Done for 16:9** (PeterDelta pnach). Ultrawide: not yet. |
| 60 FPS | Very high (EA forum asks for 4K/60) | Yes (WSFix sim-rate) | SpeedBreaker, Vita (toggle) | **Not done.** Feasible with EE overclock; needs physics/timestep work. |
| Less yellow / modern colour grade | High (VT mods, PCSX2 patches) | Yes (VT mods, X360 pack) | PCSX2 DB on/off toggles; Vita toggle | **Partly done** (on/off). Tunable presets would be new. |
| Day/night or time-of-day choice | High (Sun Set, ExOpts TOD, REDUX) | Yes | SpeedBreaker (X360 TOD lighting) | Plausible (`VOTimeOfDay`, `SetCurrentTimeOfDay`) |
| Weather variety | Medium | ExOpts rain options | — | pnach on rain floats |
| Harder, smarter pursuits | High (several overhauls, Bartender) | Yes | — | **Data + pnach** |
| No rubber-banding | Medium | PerformanceScaling, ExOpts NoCatchUp | Silent did it for HP2 PS2 | **Data/pnach** |
| Better handling (less wheelspin, more grip) | High (several handling mods) | Yes | — | **Data** |
| Cops in Quick Race and custom race options | Medium-high | ExOpts / Race Fixes | SpeedBreaker v0.2 | pnach |
| Denser, more varied traffic | Medium | ExOpts, traffic packs | — | pnach and data (density, patterns). No new models. |
| Photo mode / free camera | Medium | Camera ToolKit, ExOpts debug cam | — | pnach (debug camera code exists) |
| Modern controls (analog triggers, deadzones, sensitivity, rumble) | Medium (DualSense asks) | XtendedInput, Realistic Plugin | Silent's trigger maps for NFSU/HP2 PS2; Vita stick sensitivity | **pnach** (not done for MW) |
| HD UI and textures | High (Remastered UI, Upscaled HUD, Eternal Textures) | Yes | Unverified PCSX2 packs | PCSX2 texture replacement (original art only) |
| Custom music | Medium | XNFSMusicPlayer, Custom Jukebox | — | Replacement tool (user's own files) |
| Ghosts / time trials | Niche | TimeTrialGhosts | — | Hard |
| Online / split-screen | Medium | LAN server emulators | SpeedBreaker, Vita | Out of scope for PCSX2 MW (PS2 MW had no online play that I could confirm) |

---

## 4. GitHub repos and legal publishing patterns

### 4.1 Public repos directly relevant to NFSMW PS2 RE/tooling

- [dbalatoni13/nfsmw](https://github.com/dbalatoni13/nfsmw): decomp with SLUS-21351 symbols (CC0)
- [germanalvarez15/nfsmw-vita](https://github.com/germanalvarez15/nfsmw-vita): PS2-BE port (no license)
- [xan1242/NFSMW-PS2-ICE](https://github.com/xan1242/NFSMW-PS2-ICE): PS2 demo plugin (MIT)
- [PCSX2/pcsx2_patches](https://github.com/PCSX2/pcsx2_patches): official pnach DB (no license)
- [PeterDelta/PCSX2](https://github.com/PeterDelta/PCSX2): WS/FPS pnach collection incl. SLUS-21351 WS (no license)
- [CookiePLMonster/Console-Cheat-Codes](https://github.com/CookiePLMonster/Console-Cheat-Codes): NFS PS2 pnach models (MIT)
- [Ekey/NFS.BIN.Tool](https://github.com/Ekey/NFS.BIN.Tool): ZDIR/ZZDATA unpack/pack (no license)
- [NFSTools/Attribulator](https://github.com/NFSTools/Attribulator): VLT text export/import, Carbon PS2 profile
- [s-b-repo/nfsmw-2005-re](https://github.com/s-b-repo/nfsmw-2005-re): PC RE notes, attribute hash cracks (BSD-3 per README)
- [xan1242/nya-common-nfsmw](https://github.com/xan1242/nya-common-nfsmw), [gaycoderprincess/nya-common-nfsmw](https://github.com/gaycoderprincess/nya-common-nfsmw): PC class and function headers (MPL-2.0), useful for cross-referencing structures
- [ThirteenAG/PCSX2PluginInjector](https://github.com/ThirteenAG/PCSX2PluginInjector), [ermaccer/ps2plugininjector](https://github.com/ermaccer/ps2plugininjector): code-plugin routes

No public GitHub repo was found that offers an **MW PS2 mod** in the gameplay or visual sense. That niche is open.

### 4.2 How PS2 mod projects publish legally (patterns found)

| Pattern | Example | How it works | Fit for us |
|---|---|---|---|
| **pnach files** (text, codes only) | PCSX2 DB, Silent's Console-Cheat-Codes (MIT), PeterDelta | `SERIAL_CRC.pnach` with named `[sections]` the user toggles in PCSX2. No game data. | **Primary channel** for code-level features. Can be upstreamed to `pcsx2_patches` so everyone gets them. |
| **xdelta patch to the user's own ISO** | [ProStreet Improvised Mod V2](https://nfsmods.xyz/mod/6310) (Team Phreaks & Seban; xdelta + DeltaPatcher, runs on real PS2 via FreeMCBoot); GT4 Spec II (patch against an ISO with a stated hash) | Ship only the diff. State the exact base dump (MD5/SHA-1). Users patch with xdelta3, DeltaPatcher or Rom Patcher JS. | **Release channel** for data mods (VLT, gameplay) and ELF changes that must work on real hardware. Our repo would hold the source edits and build scripts; releases would hold the `.xdelta`. |
| **Host filesystem (loose files)** | ProStreet Improvised v1, xan1242's UnwiishedPS2Hacks | User extracts their own disc, PCSX2 boots the ELF with "Enable Host Filesystem", and modified files load from disk | **Dev workflow** (no repacking). PCSX2 only. |
| **Texture replacement pack** | GT4 HD HUD pack, Silent Hill 3, Star Wars ROTS packs | `textures/<SERIAL>/replacements/*.png` keyed by texture hash | Fine if the art is **original** or permission-cleared. Packs built from EA PC or X360 textures are redistributing EA assets, so avoid that. |
| **Bring-your-own-disc native port** | SpeedBreaker, nfsmw-vita, OpenGOAL-style decomps | Code only. An installer reads the user's disc and verifies hashes. | Not our scope (PS2 on PCSX2), but the same "no game data" rules apply. |

**Rules to adopt for our repo:** no ISO, ELF, BIN or texture dumps from the game; no code copied from unlicensed repos (PeterDelta, pcsx2_patches, Vita port, NFS.BIN.Tool, Attribulator) without permission (link and credit instead, or reimplement); patches identified by serial, CRC and base-dump hash; a clear "not affiliated with EA" notice; and a license for our own code and patches (MIT or CC0 match the ecosystem; GPL-3.0 if we ever derive from GPL tools).

---

## 5. Ranked top 15 ideas for our mod

Ranking weighs (a) how much it moves the game toward a 2026 feel, (b) feasibility on PS2 and PCSX2 on Mac, (c) whether it fills a real gap rather than duplicating existing work, and (d) whether it could also run on real hardware.

| # | Idea | Why / rationale | Mechanism | Prior art to credit | Effort |
|---|---|---|---|---|---|
| 1 | **60 FPS mode for SLUS-21351** | The most-requested "modern" feature. **Doesn't exist for any MW PS2 version.** The decomp names the timing functions (`PrepareRealTimestep`, `Sim::GetTimeStep`, `UpdateTimeStep` variants). The Vita port shows the code runs at 60. NFSU and NFSU2 PS2 precedents show the approach and EE-overclock needs. | pnach (code caves), maybe with an EE overclock recommendation | asasega (NFSU/U2 60 FPS), WSFix sim-rate, decomp | High |
| 2 | **Widescreen done right** (16:9 now; check HUD, minimap and FMV; investigate 21:9 and 16:10 variants) | Highest-download PC fix. The 16:9 pnach already exists (PeterDelta), so **test, credit and help upstream it** rather than rewrite it. Then add the missing pieces (ultrawide FOV, HUD constraint) as new sections. | pnach (`eIsWidescreen`, `FEngHud::SetWideScreenMode`, aspect constants `NTSCAspectRatio2dy` etc.) | PeterDelta, ThirteenAG WSFix, PsxFan107/pgert (NFSU2 multi-ratio) | Low–Med |
| 3 | **Tunable colour grading** ("Classic MW", "Neutral", "X360-like", "Cinematic" presets) instead of just on/off filters | The yellow filter is iconic but dated. The PCSX2 DB only removes it. ShreN's VT and the X360 VT values show which knobs matter. The GameDB notes a final colour LUT pass, so grading may be a LUT or parameter edit. | pnach on VT parameters (`IVisualTreatmentPlat`: ColorBloom, PulseBrightness, DetailMap) or VLT if data-driven | fobes, Steelmax, PeterDelta (toggles); ShreN; X360 Stuff Pack (reference values only) | Med |
| 4 | **Time-of-day selector**, possibly a slow day/night cycle | Strong demand (Sun Set, REDUX, ExOpts TOD). PS2 code contains a `VOTimeOfDay` video-option widget and `SetCurrentTimeOfDay`, so the option may only be hidden. | pnach (unhide option; drive TOD) | ExOpts `ShowTimeOfDay`, ARCHIE Sun Set, xan1242 ENISTimeOfDay | Med |
| 5 | **Photo mode / free camera** (pause, HUD hide, FOV, orbit) | A modern staple. `DebugWorldCameraMover` (with turbo speeds) is in the retail BE ELF. xan1242 showed how to trigger the debug camera on PS2 (demo). | pnach | ExOpts `EnableDebugCamera`, ARCHIE Camera ToolKit, xan1242 ICE | Med |
| 6 | **Modern controls pack**: analog-trigger throttle and brake mapping, steering deadzone and sensitivity curve, rumble tuning | Silent did trigger maps for NFSU and HP2 PS2 but **not MW**. The Vita port added stick sensitivity. PCSX2 forwards rumble to modern pads. The rumble curves (`SmallRumblePoints`, `BigRumblePoints`) are data. | pnach | Silent (trigger maps), Vita port (idea), xan1242 XtendedInput | Low–Med |
| 7 | **Catch-up (rubber-band) off / "fair AI" toggle** | Cheap, high gameplay value. Catch-up settings are race parameters (`GRaceParameters::GetCatchUp*`). | Data (gameplay.bin) or pnach | Silent HP2 Disable Catchup, gaycoderprincess PerformanceScaling, ExOpts NoCatchUp | Low |
| 8 | **Pursuit overhaul** (heat-tier rebalance, faster response, roadblock and spike tuning, optional "hard mode") | Pursuits are MW's soul and the most-modded system. Mostly VLT data, plus a few hard-coded constants. | Data (attributes.bin) + pnach (Bartender-style constants) | Revamped Pursuit (Splash45/Frexellia), Crossfire (Orsal), Bartender (CC0), Modernization Patch: credit and ask before porting their values | Med–High |
| 9 | **Handling refresh** (less low-speed wheelspin, more stable at speed, optional "UG2-ish" and "sim-lite" profiles) | Common complaint. Pure VLT. | Data | iammostwanted, Fierelier, AlanGamer456 & Wiler | Med |
| 10 | **Custom race options**: cops in Quick Race, heat override, more opponents (memory permitting), lap limits, traffic density slider | Matches what SpeedBreaker and Race Fixes chose to add. Mostly constants. | pnach | ExOpts / Race Fixes, SpeedBreaker | Med |
| 11 | **Living-city pass**: denser traffic presets, more traffic variety from unused traffic patterns, weather presets (rain on demand) | Makes Rockport feel less empty. Density and rain are floats and constants. New models are out of scope. | pnach + Data | ExOpts traffic and weather, On the Edge, Traffic Pack | Med |
| 12 | **Draw distance / LOD and reflection quality** ("High" preset for PCSX2 only) | The Vita port shows the "LOD High" and dynamic reflection paths exist. PCSX2 has GPU headroom. Could break on real hardware, so make it opt-in. | pnach (force LOD and reflection settings) | Vita port (idea), ExOpts ForceCarLOD, AeroWidescreen HD Reflections | Med |
| 13 | **Quality-of-life and career polish**: skip intros and movies, replay Blacklist races, longer profile names, max-performance shop shortcut, bounty and helicopter fixes, unused Busted NISs, Debug Car Customize unlock | Many small wins with clear PC precedent. Each is a small pnach section. | pnach | ExOpts Team, nlgxzef | Low each |
| 14 | **HD UI/HUD texture pack (original art)** for PCSX2 texture replacement | Upscaled HUD and Remastered UI are popular on PC. On PS2 the HUD is the most visibly low-res part when upscaled. Only use art we make or have permission for. | PCSX2 `textures/SLUS-21351/replacements` | LunicAura106 (Upscaled HUD, mostly vector; ask permission), edoo1 | Med–High |
| 15 | **Cut-content and debug restoration** on PS2: cut races, beta text, BE-flag and debug menus, and possibly a "beta HUD" option | Unique to PS2 lineage (OPM demo, Alpha 124). Gives the mod an identity SpeedBreaker lacks. | Data (gameplay.bin) + pnach | elaymm4 Beta Content Mod (research), nlgxzef (Alpha 124 debug menu), Hidden Palace and Damien (proto dumps), Zenii | Med |

**Honourable mentions:** a user-side **custom soundtrack replacer** (re-encode the user's own music into EA stream slots, inspired by XNFSMusicPlayer); **ghost time trials** using the PS2 `Joylog` code (hard); a **PS2-format TPK tool** (a tooling gap that benefits the whole scene); an **MW-PS2 Attribulator profile** (an upstream contribution). XenonEffects-style sparks and contrails are probably **already native on PS2**, so check before spending time on them.

**Foundational tooling, needed before most of the above:** (1) a dump verifier (serial, CRC `0518D274`, ELF SHA-1 from the decomp config); (2) a `ZDIR`/`ZZDATA` unpack and repack tool in Python, with correct hash and checksum; (3) Host FS dev setup docs for PCSX2 on Mac; (4) VLT round-trip (Attribulator or our own) verified on PS2 data; (5) a pnach build pipeline that generates per-feature sections from source with symbols from the decomp; (6) an xdelta release script.

---

## 6. Avoid duplicating; credit these

**Don't redo (link to them instead):**
- PCSX2 DB filter toggles for SLUS-21351 (Steelmax, fobes): reference them and build *tunable* grading on top.
- PeterDelta's SLUS-21351 widescreen pnach: test it, credit it, extend it.
- The decomp's symbol work (dbalatoni13 and contributors): use it, don't re-derive it. Respect their no-AI-contribution stance: don't open AI-generated PRs there.
- SpeedBreaker / X360 recomps: don't try to match X360 shaders on PS2. Say clearly that our mod is the PS2-authentic, PCSX2- and real-hardware-friendly path.
- Vita port features: take ideas only (no license).

**Credit list (as applicable):** dbalatoni13 and NFS:MW decomp contributors; PeterDelta; Steelmax; fobes; Userbots; Felixthecat1970; asasega; kozarovv; Silent (CookiePLMonster); xan1242; ThirteenAG; ASI-Factory; ermaccer; nlgxzef and the ExOpts Team; Ekey; heyitsleo (NFSTools/Attribulator); MaxHwoy (Binary); elaymm4 (Beta Content Mod); Splash45, Frexellia, Orsal, rng-guy (pursuit research); iammostwanted, Fierelier, AlanGamer456, Wiler (handling); gaycoderprincess; ARCHIE; ShreN; LunicAura106; Damien and Hidden Palace (prototype dumps); Zenii; germanalvarez15 (Vita port, ideas); Alex Ugarte / project(u) (SpeedBreaker, benchmark).

**Legal red flags to avoid:** bundling EA textures (PC or X360) in a texture pack; redistributing patched ISOs or ELFs; copying code or patch files from unlicensed repos; using the EA, NFS or Black Box logos as our project branding.

---

## 7. Open questions / next research

1. Is PeterDelta's SLUS-21351 widescreen patch complete (HUD, minimap, FMVs, menus)? Test in PCSX2 2.6.3.
2. Does MW PS2 run 30 FPS fixed or variable, and how is the sim step decoupled from rendering? Read the `PrepareRealTimestep` and `Sim::GetTimeStep` call sites in the decomp asm.
3. Are VT and grading parameters on PS2 in `attributes.bin` (VLT) or hard-coded? Is the final LUT a texture we could swap?
4. Does PS2 `attributes.bin` round-trip through Attribulator's MW PC profile unchanged?
5. Does the game check the `ZDIR.BIN` checksum field at load (which would decide repack requirements)?
6. Is the `VOTimeOfDay` option reachable on PS2 (hidden like on PC), and which TOD presets exist on the disc (`L2RA_SUNSET.BUN` and `STREAML2RA_SUNSET.BUN` suggest at least a sunset world set)?
7. PCSX2 on Apple Silicon: confirm that pnach code caves, the 128 MB RAM option, Host FS and texture replacement all work in 2.6.3 (expected yes; PluginInjector and ReShade are definitely Windows-only).
8. Read TCRF's MW pages manually in a browser (automated reads returned injected text).

---

## Sources (main)

- PCSX2 patches DB: https://github.com/PCSX2/pcsx2_patches (files `SLUS-21351_0518D274.pnach`, `SLUS-21267_270F8C03.pnach`, `SLES-53857_805C3B3A.pnach`; PR #688)
- PCSX2 GameIndex: https://github.com/PCSX2/pcsx2/blob/master/bin/resources/GameIndex.yaml
- PeterDelta patches: https://github.com/PeterDelta/PCSX2
- Decomp: https://github.com/dbalatoni13/nfsmw, https://decomp.dev/dbalatoni13/nfsmw
- Vita port: https://github.com/germanalvarez15/nfsmw-vita
- SpeedBreaker: https://github.com/SpeedBreakerProject/speedbreaker, https://www.notebookcheck.net/For-many-the-best-in-the-series-Need-for-Speed-classic-returns-in-4K.1420184.0.html
- xan1242 ICE: https://github.com/xan1242/NFSMW-PS2-ICE ; PluginInjector: https://github.com/ThirteenAG/PCSX2PluginInjector ; ps2plugininjector: https://github.com/ermaccer/ps2plugininjector
- Hidden Palace protos: https://hiddenpalace.org/Need_for_Speed:_Most_Wanted_(Sep_20,_2005_prototype), …(Oct_12,_2005_prototype), …(Oct_8,_2005_prototype), …(Jul_14,_2005_demo)
- Silent's PS2 NFS pages: https://silentsblog.com/mods/need-for-speed-underground-ps2, https://silentsblog.com/mods/need-for-speed-hot-pursuit-2-ps2, https://github.com/CookiePLMonster/Console-Cheat-Codes
- NFS.BIN.Tool: https://github.com/Ekey/NFS.BIN.Tool ; Attribulator: https://github.com/NFSTools/Attribulator ; Binary: https://github.com/NFSTools/Binary ; xnfstpktool: https://github.com/xan1242/xnfstpktool
- ExOpts: https://github.com/ExOptsTeam/NFSMWExOpts ; Unlimiter: https://nfsmods.xyz/mod/688 ; WSFix: https://github.fusionfix.io/wfp
- nfsmods.xyz pages: 1200, 1711, 1698, 7141, 5415, 5279, 1096, 3007, 6681, 6310, 822, 4589, 465, 6829, 2612, 2302
- gaycoderprincess repos: https://github.com/gaycoderprincess ; rng-guy Bartender: https://github.com/rng-guy/NFSMWBartender
- GamePressure MW mod list (download counts): https://www.gamepressure.com/games/need-for-speed-most-wanted-2005/mods-trainers/zf708
- Almar's CodeBreaker (BE): https://almarsguides.com/retro/walkthroughs/PS2/Games/NeedForSpeedMostWanted/CodeBreaker/BlackEdition/
- EA forum remaster threads (demand): https://forums.ea.com/discussions/need-for-speed-franchise-discussion-en/official-request-%E2%80%94-remasterremake-of-need-for-speed-most-wanted-2005-for-ps5/13298821
- GT4 Spec II (xdelta pattern): https://www.timeextension.com/news/2024/09/20-years-on-ps2-classic-gran-turismo-4-gets-a-fan-made-retune
