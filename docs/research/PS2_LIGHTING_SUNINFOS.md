# PS2 NFSMW Lighting Architecture: `SunInfos`, `LightMaterials`, `GLOBALB.LZC` & `GLOBALMEMORYFILE.BIN`

This document records clean-room binary format findings for *Need for Speed: Most Wanted – Black Edition* (PS2 NTSC-U, `SLUS-21351`), verified directly against untouched retail `SLUS-21351` ISO archives (`GLOBAL\GLOBALB.LZC`, `GLOBAL\GLOBALB.BUN`, `GLOBAL\GLOBALMEMORYFILE.BIN`, `GLOBAL\ATTRIBUTES.BIN`) and retail EE ELF (`SLUS_213.51`) disassembly.

> **Important Precedence Warning (`GLOBALMEMORYFILE.BIN`):**  
> On PS2, `GLOBAL\GLOBALB.LZC` is mounted **inside `GLOBAL\GLOBALMEMORYFILE.BIN`** as entry `[2]` (518,707 bytes, JDLZ v2). A modded ISO that updates `GLOBAL\GLOBALB.LZC` in `ZDIR`/`ZZDATA` without updating `GLOBAL\GLOBALMEMORYFILE.BIN` will continue loading the stock embedded lighting from memory-file RAM! `tools/lighting_ps2.py` always updates both outputs together when `--memfile-out` is supplied.

---

## 1. High-Level Lighting Pipeline on PS2 (`SLUS-21351`)

Unlike PC community assumptions that `SunInfos` stores diffuse/ambient/fog/sky RGBA tables, retail PS2 `SLUS-21351` splits outdoor illumination across **four** distinct layers:

1. **Sun Geometry & Lens-Flare Discs — `GLOBAL\GLOBALB.LZC` (`SunInfos`, `0x00034202`):**
   - Stores sky `Position`, `CarShadowPosition`, and 6 additive/blended billboard `SunLayers` for 5 named profiles (`PARKLAND`, `SUNSET`, `PROTOTYPE`, `E3DEMO`, `MIDDAY`).
   - Parsed by `LoaderSun__FP6bChunk` (`0x001FAC58`); selected by `SetCurrentSunInfo__Fv` (`0x001FADC0`).
2. **Surface & Vehicle Shading — `GLOBAL\GLOBALB.LZC` (`LightMaterials`, `0x00135200`):**
   - 156 `eLightMaterial` chunks controlling angle-dependent diffuse, specular, envmap tint/scale, metallic scale, and hotspot parameters across car paints, chrome, glass, carbon fiber, wheels, and road materials (`ROAD`, `WHEELBLUR`, `CHROME`, `WINDSHIELD`, `METPAINT*`, `REGPAINT*`, etc.).
   - Parsed by `LoaderLights__FP6bChunk` (`0x001F5E00`) and uploaded via `UpdatePlatInfo__27eLightMaterialPlatInterface` (`0x0020E118`).
3. **World Primary/Fill Light & Fog Globals — `SLUS_213.51` `.data`:**
   - Primary & fill light rig constants and fog tables live in ELF `.data` (`WorldLightDirectionVector` @ `0x00547480`, `PrimaryLightDiffuseIntensity` @ `0x00547490 = 6.5`, `FillLightDiffuseColour` @ `0x005474A0 = [0.87, 0.90, 1.00, 1.00]`, `FillLightDiffuseIntensity` @ `0x005474B0 = 2.1`, `FogModeInfos` @ `0x00550028`, `BaseWeatherFog` @ `0x00593E90 = 0.74`, `BaseWeatherFogStart` @ `0x00593E94 = 212.0`).
   - `SetCurrentSunInfo` updates `WorldLightDirectionVector` from the active sun's `Position`.
4. **Screen-Space Post-Processing & Color Grading — `GLOBAL\ATTRIBUTES.BIN` (`visuallook`):**
   - `IVisualTreatmentPlat::RenderMWVisualLook` (`0x0021A008`) invokes `ApplyColorBloom` (`0x00219210`) and GS CLUT lookup passes (`RemapLinearIntoClut8` @ `0x0020F820`) driven by VLT `visuallook` splines/tints (patched separately via `patches/vlt/modern_visual_look.json`).

---

## 2. `GLOBAL\GLOBALB.LZC` & `GLOBAL\GLOBALMEMORYFILE.BIN` Container Layout

- `GLOBAL\GLOBALB.LZC` is a **518,707-byte** EA Black Box **JDLZ v2** compressed archive (`JDLZ`, version `2`, header size `16`).
- Decompressing `GLOBAL\GLOBALB.LZC` yields an **871,900-byte** stream identical byte-for-byte to `GLOBAL\GLOBALB.BUN` (189 top-level little-endian `{uint32 id, uint32 size}` EA `bChunk` records).

### Retail `GLOBAL\GLOBALMEMORYFILE.BIN` Embedding (`0x53219999`)

`GLOBAL\GLOBALMEMORYFILE.BIN` (total **1,722,848 bytes**, **25 entries**) uses the standard EA MemoryFile header (`n_next, n_prev, magic = 0x53219999, count = 25`) followed by 25 × 20-byte directory entries (`hash, offset, file_size, memory_size, data_ptr`):

| Entry Index | Virtual Path (`bstringhash`) | Offset in File | `file_size` | `memory_size` | 16B Slot Capacity | Embedded Format |
|---:|---|---:|---:|---:|---:|---|
| `[0]` | `0xda18a301` | `0x000210` | `49,408` | `356` | `368` | Raw/Header |
| `[1]` | `0x9775379a` (`ATTRIBUTES.BIN`) | `0x000380` | `703,296` | `703,296` | `703,296` | `VPAK` (`0x5650414B`) |
| **`[2]`** | **`0x4c939b36` (`GLOBAL\GLOBALB.LZC`)** | **`0x0abec0`** | **`518,707`** | **`518,707`** | **`518,720`** | **JDLZ v2 (`0x4A444C5A`, 871,900 B uncomp.)** |
| `[3]` | `0xab50136b` | `0x12a900` | `8,768` | `8,768` | `8,768` | Raw chunk |

- **Storage format verified:** `GLOBALMEMORYFILE.BIN[0x0abec0 : 0x0abec0 + 518707]` is byte-for-byte identical to the compressed `GLOBAL\GLOBALB.LZC` stream inside `ZDIR`/`ZZDATA` — NOT uncompressed `GLOBALB.BUN`.
- **Retail EE Loader Behavior:**
  - `AddMemoryFile__FPv` (`0x0031A7C0`) walks all `count` entries on load and dynamically writes `Entry.Data = memfile_base + Entry.Offset` (`sw $v0, 0x10($v1)`), ignoring stale on-disk pointers.
  - `LoadMemoryFile__FPCc` (`0x00315D68`) dynamically queries queued file size (`GetQueuedFileSize` @ `0x0031A4A8`) and allocates (`bMalloc`, alignment `4160`).
- **Compact JDLZ In-Place Re-compression:**
  - `tools/lighting_ps2.py` uses a lazy-matching JDLZ v2 compressor (`compress_jdlz_compact`) built on `tools/jdlz.py` bitstream primitives and round-trip verified via `jdlz_decompress()`.
  - On `rockport2026_daylight.json` and `rockport2026_golden_hour.json`, `compress_jdlz_compact` achieves **516,777 B** and **516,858 B** respectively (~1.9 KiB *smaller* than stock `518,707 B`), fitting cleanly **in-place** inside entry `[2]`'s `518,720 B` slot with zero MemoryFile growth.
  - If a custom user patch ever exceeds `518,720 B`, `patch_globalmemoryfile_globalb()` automatically falls back to `repacked-shifted` mode, rebuilding 16-byte aligned entry offsets cleanly.

---

## 3. `SunInfos` Binary Layout (`bChunk` `0x00034202`)

Location in untouched `SLUS-21351` `GLOBALB.BUN`:
- Top-level chunk header at `0x0abab8`: `id = 0x00034202`, `size = 1360` bytes (`5 × 272` bytes).
- Retail `LoaderSun__FP6bChunk` (`0x001FAC58`) aligns the array base with `aligned_start = (chunk_addr + 0x17) & ~0x0F`, yielding `0x0abac0` (0 padding bytes here because `0x0abab8 + 8 = 0x0abac0`, which is already 16-byte aligned).

### `SunChunkInfo` Record (272 bytes = `0x110`, `Version = 2`)

| Relative Offset | Size | Type | Field | Notes |
|---|---:|---|---|---|
| `+0x00` | 4 | `int32` | `Version` | Always `2` (`SUN_INFO_VERSION`) |
| `+0x04` | 4 | `uint32` | `NameHash` | `bStringHash(Name)` |
| `+0x08` | 24 | `char[24]` | `Name` | NUL-padded ASCII profile name |
| `+0x20` | 12 | `float[3]` | `Position` | World sun direction/position (`X, Y, Z`) |
| `+0x2C` | 12 | `float[3]` | `CarShadowPosition` | Car projected ground-shadow vector (`X, Y, Z`) |
| `+0x38` | 216 | `SunLayer[6]` | `SunLayers[6]` | 6 lens-flare / solar disc layers (`6 × 36` bytes) |

### `SunLayer` Element (36 bytes = `0x24`, up to 6 per `SunChunkInfo`)

| Relative Offset | Size | Type | Member | Notes |
|---|---:|---|---|---|
| `+0x00` | 4 | `int32` | `Texture` | `0=SUNTEX_CENTER`, `1=SUNTEX_HALO`, `2=SUNTEX_MAJORRAYS`, `3=SUNTEX_MINORRAYS`, `4=SUNTEX_RING` |
| `+0x04` | 4 | `int32` | `AlphaType` | `0=SUNALPHA_BLEND`, `1=SUNALPHA_ADD` |
| `+0x08` | 4 | `float` | `IntensityScale` | Layer brightness multiplier |
| `+0x0C` | 4 | `float` | `Size` | Billboard quad radius/size |
| `+0x10` | 4 | `float` | `OffsetX` | Flare axis screen offset X |
| `+0x14` | 4 | `float` | `OffsetY` | Flare axis screen offset Y |
| `+0x18` | 4 | `uint8[4]` | `Colour` | `[R, G, B, A]` — **PS2 GS alpha scale uses `128` (`0x80`) = full `1.0`!** |
| `+0x1C` | 4 | `int32` | `Angle` | Fixed-point rotation (`16384 = 90°`) |
| `+0x20` | 4 | `float` | `SweepAngleAmount` | View-sweep rotation factor |

### Stock `SLUS-21351` `SunInfos` Profiles & `SetCurrentSunInfo` Override

| Index | Offset (`BUN`) | Name | `NameHash` | `Position` / `CarShadowPosition` | Active Layers | Runtime Selection |
|---:|---|---|---|---|---:|---|
| `[0]` | `0x0abac0` | `PARKLAND` | `0xb05e7f4c` | `[4500.0, -300.0, 2400.0]` | 0 | Referenced by all 18 `SunTrack` (`0x00034201`) entries (`0x0aa670`, including `L2RA` `"Most Wanted World"`), but **overridden** at runtime by `SetCurrentSunInfo__Fv` |
| `[1]` | `0x0abbd0` | `SUNSET` | `0x7ad04221` | `[4500.0, -300.0, 1700.0]` | 4 (`0..3`) | Selected by `SetCurrentSunInfo__Fv` (`rodata 0x004d4840`) when `TimeOfDay` branch != `MIDDAY` |
| `[2]` | `0x0abce0` | `PROTOTYPE` | `0x9ade2875` | `[5300.0, -3550.0, 3000.0]` | 0 | Unused fallback profile |
| `[3]` | `0x0abdf0` | `E3DEMO` | `0x57bb11dc` | `[5300.0, -3550.0, 3000.0]` | 4 (`0..3`) | Unused E3 preset |
| **`[4]`** | **`0x0abf00`** | **`MIDDAY`** | **`0x6bf26c37`** | **`[5300.0, -3550.0, 3000.0]`** | **4 (`0..3`)** | **Active Career/Free Roam profile (`rodata 0x004d4838`). Stock layers `0..3` are copied identically from `SUNSET` (orange/sepia)!** |

Because stock `MIDDAY` carries the exact same warm-amber/sepia flare colors as `SUNSET` (`[255, 200, 100, 128]`, `[130, 110, 25, 128]`, `[159, 122, 75, 128]`, `[166, 119, 72, 128]`), updating `MIDDAY` has immediate impact on Career & Free Roam, while updating `SUNSET` affects Sunset tracks/weather events.

---

## 4. `LightMaterials` Binary Layout (`bChunk` `0x00135200`)

Retail `GLOBALB.BUN` begins with **156 consecutive `0x00135200` chunks** from `0x000000` (`ALUMINUM`) through `0x006a90` (`WINDSHIELD_TINT_L3_YELLOW`). Each chunk is **176 bytes** (`8`-byte `bChunk` header + **168-byte** payload, `Version = 3`):

| Payload Offset | Size | Type | Field | Notes |
|---|---:|---|---|---|
| `+0x00`..`+0x0B` | 12 | `uint32[3]` | Runtime/list header words | Stored as `1, 3, 3` in stock chunks |
| `+0x0C` | 4 | `uint32` | `NameHash` | `bStringHash(Name)` |
| `+0x10` | 4 | `uint32` | `Version` | Always `3` (`LIGHT_MATERIAL_VERSION`) |
| `+0x14` | 28 | `char[28]` | `Name` | NUL-padded ASCII material name |
| `+0x30` | 4 | `float` | `DiffuseMinScale` | Grazing-angle diffuse scalar |
| `+0x34` | 12 | `float[3]` | `DiffuseMinRGB` | `DiffuseMinR`, `DiffuseMinG`, `DiffuseMinB` |
| `+0x40` | 4 | `float` | `DiffuseMaxScale` | Facing-angle diffuse scalar |
| `+0x44` | 12 | `float[3]` | `DiffuseMaxRGB` | `DiffuseMaxR`, `DiffuseMaxG`, `DiffuseMaxB` |
| `+0x50` | 4 | `float` | `DiffuseMinA` | Grazing alpha |
| `+0x54` | 4 | `float` | `DiffuseMaxA` | Facing alpha |
| `+0x58` | 4 | `float` | `SpecularPower` | Specular exponent |
| `+0x5C` | 4 | `float` | `SpecularMinScale` | Grazing specular scale |
| `+0x60` | 12 | `float[3]` | `SpecularMinRGB` | `SpecularMinR`, `SpecularMinG`, `SpecularMinB` |
| `+0x6C` | 4 | `float` | `SpecularMaxScale` | Facing specular scale |
| `+0x70` | 12 | `float[3]` | `SpecularMaxRGB` | `SpecularMaxR`, `SpecularMaxG`, `SpecularMaxB` |
| `+0x7C` | 4 | `float` | `EnvmapPower` | Environment Fresnel falloff exponent |
| `+0x80` | 4 | `float` | `EnvmapMinScale` | Grazing reflection strength |
| `+0x84` | 12 | `float[3]` | `EnvmapMinRGB` | `EnvmapMinR`, `EnvmapMinG`, `EnvmapMinB` |
| `+0x90` | 4 | `float` | `EnvmapMaxScale` | Facing reflection strength |
| `+0x94` | 12 | `float[3]` | `EnvmapMaxRGB` | `EnvmapMaxR`, `EnvmapMaxG`, `EnvmapMaxB` |
| `+0xA0` | 4 | `float` | `MetallicScale` | Metallic paint flake blend (`1.0` on `METPAINT*`) |
| `+0xA4` | 4 | `float` | `SpecularHotSpot` | Tight sun hotspot factor |

Notable retail `SLUS-21351` discoveries:
- `ROAD` (`0x004788`) and `WHEELBLUR` (`0x0053e8`) carry baked reddish-brown diffuse RGB `[0.90, 0.57, 0.56]` on both `DiffuseMinRGB` and `DiffuseMaxRGB`. Neutralizing or warming these eliminates EA's heavy dark-brown crush without losing asphalt contrast.
- `CHROME` (`0x000bb8`) ships with `SpecularMinScale = 0.0` and `SpecularMaxScale = 0.0` (relies purely on envmap); adding subtle specular (`0.45` / `0.25`) gives realistic crisp sun glints.
- `WINDSHIELD` (`0x0055f8`) has a baked red-shifted `SpecularMinRGB = [1.1, 1.0, 1.0]` and modest `EnvmapMaxScale = 0.5`.

---

## 5. `visualrgbtweaker` vs `visuallook` Interaction (`GLOBAL\ATTRIBUTES.BIN`)

- `visualrgbtweaker/default` (`collection 0xeec2271a`) exists inside `GLOBAL\ATTRIBUTES.BIN` (`green` @ `0x00012880`, `blue` @ `0x000128c0`, `red` @ `0x00012900`), storing three 4×4 float cubic-Bezier channel matrices.
- Disassembly & cross-reference analysis of `SLUS_213.51` confirms the `.rodata` string `"visualrgbtweaker"` (`0x004e9d65`) has **zero (`0`) EE code xrefs** — retail PS2 ignores `visualrgbtweaker`!
- Instead, PS2 final color grading runs through `IVisualTreatmentPlat::RenderMWVisualLook` (`0x0021A008`), evaluating `visuallook` (`default`, `sunset`, `uves`, `extreme_uves`, `copcam`) bloom tints/intensities, `BlackBloomIntensity`, and `Desaturation`.
- Therefore, the cleanest full remaster combines:
  1. **VLT `visuallook` curves** (`GLOBAL\ATTRIBUTES.BIN` + `GLOBALMEMORYFILE.BIN` `[1]`) for full-screen bloom, shadow black-crush, and desaturation balance.
  2. **`SunInfos` & `LightMaterials`** (`GLOBAL\GLOBALB.LZC` + `GLOBALMEMORYFILE.BIN` `[2]`) via `patches/lighting/*.json` for sky sun vector/optics and car/road/chrome/glass material light response.

---

## 6. Usage (`tools/lighting_ps2.py`)

```bash
# Inspect all SunInfos, SunTrack, LightMaterials, GLOBALMEMORYFILE.BIN[2], and VLT grading
python3 tools/lighting_ps2.py inspect /path/to/NFSMW_BlackEdition.iso

# Dump exact JSON field offsets (great for authoring new recipes)
python3 tools/lighting_ps2.py inspect /path/to/NFSMW_BlackEdition.iso --sun MIDDAY --material ROAD --json

# Dry-run validate a recipe (zero disk writes)
python3 tools/lighting_ps2.py patch \
  --iso /path/to/NFSMW_BlackEdition.iso \
  --recipe patches/lighting/rockport2026_daylight.json \
  --out-globalb build/GLOBALB.LZC \
  --dry-run

# Patch GLOBALB.LZC AND keep GLOBALMEMORYFILE.BIN synchronized in-place
python3 tools/lighting_ps2.py patch \
  --iso /path/to/NFSMW_BlackEdition.iso \
  --recipe patches/lighting/rockport2026_golden_hour.json \
  --out-globalb build/GLOBALB.LZC \
  --memfile-out build/GLOBALMEMORYFILE.BIN
```
