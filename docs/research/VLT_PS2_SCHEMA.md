# PS2 NFSMW VPAK / VLT Binary Schema & Lighting Vault Map (`SLUS-21351`)

Empirical specification of EA Black Box's **AttribSys (`VPAK` / `VLT`)** databases in *Need for Speed: Most Wanted – Black Edition* (PS2 NTSC-U, **`SLUS-21351`**), verified directly against the untouched retail disc via [`tools/vlt_ps2.py`](../../tools/vlt_ps2.py) and cross-checked with MIT [`NFSTools/VaultLib`](https://github.com/NFSTools/VaultLib) and CC0 [`dbalatoni13/nfsmw`](https://github.com/dbalatoni13/nfsmw).

---

## 1. Disc Inventory of VPAK Archives & MemoryFile Packaging

Scanning all 1,447 entries of `NFS/ZDIR.BIN` on `SLUS-21351` reveals exactly **three VLT attribute packs** (plus one compressed mirror and one embedded MemoryFile copy):

| Virtual Path (ZDIR) | ZDIR Index | Size (Bytes) | Format | Vaults | Classes Defined | Collections | Notes |
|---|---:|---:|---|---:|---:|---:|---|
| `GLOBAL\ATTRIBUTES.BIN` | 841 | 703,296 | Raw `VPAK` | 1 (`db`) | 56 | 2,301 | Primary schema + global game tuning DB |
| `GLOBAL\GLOBALMEMORYFILE.BIN` | 726 | 1,722,848 | `MEMO` (`0x53219999`) | — | — | — | **Entry `[1]` (`0x9775379a`) embeds `GLOBAL\ATTRIBUTES.BIN` byte-identically at offset `0x000380`!** |
| `GLOBAL\FE_ATTRIB.BIN` | 697 | 116,352 | Raw `VPAK` | 1 (`frontend`) | 0 (uses `db`) | 411 | Frontend UI / car-lot / customization (`frontend` class) |
| `GLOBAL\GAMEPLAY.BIN` | 463, 464 | 2,103,680 | Raw `VPAK` | 272 | 0 (uses `db`) | 6,287 | Career/race/event vaults (`gameplay` class); stored twice in ZDIR for seek optimization |
| `GLOBAL\GAMEPLAY.LZC` | 465 | 779,834 | `JDLZ` v2 | 272 | 0 (uses `db`) | 6,287 | Decompresses byte-identically to `GLOBAL\GAMEPLAY.BIN` |

> [!CAUTION]
> **Critical PS2 Runtime Gotcha (`GLOBAL\GLOBALMEMORYFILE.BIN`)**:
> In EA's `bFile` loader, loaded MemoryFiles take precedence over `ZDIR.BIN` (`02-ps2-data-formats.md` §2.5). On `SLUS-21351`, `GLOBAL\GLOBALMEMORYFILE.BIN` embeds the entire 703,296-byte `GLOBAL\ATTRIBUTES.BIN` verbatim at offset **`0x000380`** (`FileSize == MemorySize == 703,296`).
>
> **If you only replace standalone `GLOBAL\ATTRIBUTES.BIN` inside `ZZDATA2.BIN`, the PS2 engine will still load the unpatched copy from `GLOBALMEMORYFILE.BIN`!**
> Always inject BOTH the patched `GLOBAL\ATTRIBUTES.BIN` AND a synced `GLOBAL\GLOBALMEMORYFILE.BIN` (generated automatically by `tools/vlt_ps2.py patch --memfile-out <path>`).

---

## 2. Binary Container Layout (32-bit Little-Endian, MIPS R5900)

### 2.1 `VPAK` Container Header

```c
struct VpakHeader {          // offset 0x00, 16 bytes
    char   Magic[4];         // "VPAK" (0x4B415056)
    uint32 NumVaults;        // 1 for ATTRIBUTES/FE_ATTRIB, 272 for GAMEPLAY
    uint32 StringTableOff;   // file offset of vault name string table
    uint32 StringTableSize;  // byte size of vault name string table
};

struct VpakVaultEntry {      // offset 0x10 + i * 20, 20 bytes each
    uint32 NameOffset;       // relative to StringTableOff (NUL-terminated ASCII)
    uint32 BinSize;          // size of .bin payload stream
    uint32 VltSize;          // size of .vlt metadata stream
    uint32 BinOffset;        // absolute file offset of .bin stream
    uint32 VltOffset;        // absolute file offset of .vlt stream
};
```

For `GLOBAL\ATTRIBUTES.BIN` (`db`):
- `.bin` stream: `off = 0x000080`, `size = 362,831 B`
- `.vlt` stream: `off = 0x058A00`, `size = 340,240 B`

### 2.2 `.vlt` Chunk Stream

All `.vlt` chunks begin with an 8-byte little-endian header `{ uint32 Tag; uint32 Size; }` where `Size` includes the 8-byte header and `Tag` appears reversed in ASCII on disk (`NpeD` -> `DepN`):

| Chunk Tag | LE Bytes | Role | `ATTRIBUTES.BIN` Size |
|---|---|---|---:|
| `DepN` | `NpeD` | Dependency asset hashes + NUL-terminated filenames (`db.vlt`, `db.bin`) | 48 B |
| `StrN` | `NrtS` | VLT-side string table (empty padding on retail PS2) | 16 B |
| `DatN` | `NtaD` | Serialized `DatabaseLoadData`, `ClassLoadData`, and `CollectionLoadData` records | 162,112 B |
| `ExpN` | `NpxE` | Export table: `uint32 Count`, followed by `Count × 20 B` export descriptors | 47,184 B (2,358 exports) |
| `PtrN` | `NrtP` | Pointer relocation table: `12 B` fixup records patching `0xEFFECADD` placeholders | 130,880 B (10,906 records) |

### 2.3 `ExpN` Export Entry (20 Bytes)

```c
struct VltExportEntry {      // 20 bytes (32-bit VLT)
    uint32 ExportID;         // VLT32 hash (db / class / "class/collection")
    uint32 TypeHash;         // 0xCBBC628F Database | 0x5E970CBC Class | 0x8E112EB7 Collection
    uint32 Reserved;         // 0x00000000 on 32-bit PS2/PC
    uint32 Size;             // payload byte size inside DatN
    uint32 VltOffset;        // offset relative to start of .vlt stream (VltOffset)
};
```

In `GLOBAL\ATTRIBUTES.BIN`:
- `0xCBBC628F` (`DatabaseLoadData`): **1** export
- `0x5E970CBC` (`ClassLoadData`): **56** exports
- `0x8E112EB7` (`CollectionLoadData`): **2,301** exports

### 2.4 `PtrN` Pointer Relocation Stream (12 Bytes per Record)

```c
struct AttribPtrRef {        // 12 bytes
    uint32 FixupOffset;      // offset inside currently selected target stream (.vlt or .bin)
    uint16 PtrType;          // 0=PtrEnd, 1=PtrNull, 2=PtrSetFixupTarget, 3=PtrDepRelative
    uint16 Index;            // for PtrSetFixupTarget: 0=.vlt target, 1=.bin target
    uint32 Destination;      // destination offset inside .bin stream (when PtrType==3)
};
```

At load time, placeholders (`0xEFFECADD` or `0x00000000`) at `FixupOffset` inside `.vlt` (when `is_vlt=True`) or `.bin` (when `is_vlt=False`) are relocated to `bin_base + Destination` (or `NULL` when `PtrType == 1`). In `ATTRIBUTES.BIN`: **4,738** VLT-side fixups and **6,165** BIN-side fixups.

### 2.5 `.bin` Stream Structure

1. **`StrE` Chunk** (`off = 0x0000` in `.bin`, tag `ErtS`, `31,216 B` in `ATTRIBUTES.BIN`): NUL-terminated ASCII string pool referenced by `EA::Reflection::Text` and `Attrib::StringKey` pointers.
2. **Type Names Pool**: immediately after `StrE` (`0x79F0` in `ATTRIBUTES.BIN`), pointed to by `DatabaseLoadData.mTypeNames` — 94 NUL-terminated AttribSys type names.
3. **Class Definitions & Collection Data**: 16-byte `AttribDefinition` tables, collection layout blocks, out-of-line scalar structs, and `Attrib::Array` buffers.

### 2.6 Core LoadData Structs & Exact File Byte Offsets

1. **`DatabaseLoadData` (`0xCBBC628F`)**:
   - `{ uint32 NumClasses; uint32 MaxTypeSize; uint32 NumTypes; ptr32 TypeNames; uint32 TypeSizes[NumTypes]; }`
   - On PS2 `SLUS-21351`: `NumClasses = 88` (capacity), `MaxTypeSize = 289`, `NumTypes = 94`.
2. **`ClassLoadData` (`0x5E970CBC`, 28 bytes)**:
   - `{ uint32 ClassKey; uint32 CollectionReserve; uint32 NumDefinitions; ptr32 Definitions; uint32 LayoutSize; uint32 LayoutKeyShift; uint32 LayoutCount; }`
   - Each `AttribDefinition` in `.bin` is **16 bytes**: `{ uint32 Key; uint32 TypeHash; uint16 Offset; uint16 Size; uint16 MaxCount; uint8 Flags; uint8 AlignmentLog2; }`.
   - `Flags`: `0x01 = Array`, `0x02 = InLayout`, `0x04 = IsBound`, `0x08 = IsNotSearchable`, `0x10 = IsStatic`, `0x20 = HasHandler`.
3. **`CollectionLoadData` (`0x8E112EB7`, 32 + `4*NumTypes` + `12*NumEntries` bytes)**:
   - Header: `{ uint32 Key; uint32 ClassKey; uint32 ParentKey; uint32 TableReserve; uint32 TableKeyShift; uint32 NumEntries; uint32 NumTypes; ptr32 LayoutPtr; }`
   - Followed by `uint32 Types[NumTypes]` and `AttribEntry Entries[NumEntries]` (12 bytes each: `{ uint32 Key; uint32 DataOrPtr; uint16 TypeIndex; uint8 NodeFlags; uint8 EntryFlags; }`).
4. **Exact File Byte Offset Resolution Rules**:
   - **Required layout scalar** (`InLayout` and not `Array`): `vault.bin_offset + layout_bin_off + def.Offset`
   - **Required layout array** (`InLayout` and `Array`): `Attrib::Array` header (`{ uint16 Alloc, Count, Size, EncodedTypePad }`) at `arr_off = layout_bin_off + def.Offset`. If `EncodedTypePad & 0x8000` (`Flag_AlignedAt16`), insert **8 bytes** of padding before element 0. Element `i` lives at `vault.bin_offset + arr_off + 8 + pad + i * Size`.
   - **Optional inline value** (`NodeFlags & 0x20` = `Flag_IsByValue`, no `PtrN` entry): lives directly inside `.vlt` (`DatN`) at `vault.vlt_offset + entry_vlt_off + 4`!
   - **Optional inline pointer** (`NodeFlags & 0x20`, with `PtrN` entry — used 8,450× in `GAMEPLAY.BIN` for `EA::Reflection::Text`): `PtrN` resolves `entry_vlt_off + 4` -> string in `.bin` `StrE`.
   - **Optional out-of-line scalar** (`NodeFlags == 0x00`): `PtrN` resolves `entry_vlt_off + 4` -> `vault.bin_offset + dst_bin_off`.
   - **Optional out-of-line array** (`NodeFlags & 0x02` = `Flag_IsArray`): `PtrN` resolves `entry_vlt_off + 4` -> `Attrib::Array` in `.bin`.

### 2.7 Notable PS2 Type Sizes

- **`EA::Reflection::Bool`**: **4 bytes** (`uint32`, `0`/`1`) on PS2 (`ATTRIBUTES.BIN` `DatabaseLoadData`), matching the 32-bit layout of `camerainfo.TILTING` and `SELECTABLE`.
- **`Attrib::StringKey`**: **16 bytes** `{ uint64 Hash64; uint32 Hash32; ptr32 TextInStrE; }`.
- **`Attrib::RefSpec`**: **12 bytes** `{ uint32 ClassKey; uint32 CollectionKey; uint32 RuntimePtrZero; }`.
- **`AxlePair`**: **8 bytes** `{ float Front; float Rear; }`.
- **`TrafficPatternRecord`**: **24 bytes** `{ RefSpec Vehicle; float Rate; uint32 MaxInstances; uint32 Percent; }`.
- **`CopCountRecord`**: **24 bytes** `{ StringKey CopType; uint32 Count; uint32 Chance; }`.
- **`Attrib::Types::Matrix`**: **64 bytes** (`float[4][4]`, 16-byte aligned; spline curves in `visuallook` / `visualrgbtweaker`).

---

## 3. Complete Class Catalog (`GLOBAL\ATTRIBUTES.BIN`)

All **56 classes** (100% resolved) and all **1,156 unique field definitions** (100% resolved) in `SLUS-21351` `GLOBAL\ATTRIBUTES.BIN`:

| Class | VLT32 Hash | Defs | Layout | Collections | Role |
|---|---|---:|---:|---:|---|
| `acceltrans` | `0xff77f451` | 5 | 16 B | 28 | Acceleration transition curves |
| `aivehicle` | `0x22515733` | 9 | 12 B | 23 | AI driver speed caps & rubber-banding |
| `aud_moment_strm` | `0xd2410816` | 6 | 13 B | 45 | Audio moment streams |
| `aud_stitch_loop` | `0x3473edcd` | 2 | 0 B | 2 | Audio stitch loops |
| `audioimpact` | `0xfbffb107` | 7 | 0 B | 131 | Collision impact SFX |
| `audioscrape` | `0x11b47832` | 2 | 0 B | 7 | Surface scrape SFX |
| `audiosystem` | `0xd3c18f03` | 20 | 64 B | 9 | AEMS bank & mixer parameters |
| `brakes` | `0x36350867` | 3 | 20 B | 82 | Vehicle braking (`BRAKES`, `BRAKE_LOCK`, `EBRAKE`) |
| `camerainfo` | `0x93c171e4` | 9 | 132 B | 66 | Driving cameras (`bumper`, `hood`, `close`, `far`, `superfar`, `drift`, `pursuit`, per-car overrides) |
| `chassis` | `0xafa210f0` | 19 | 116 B | 89 | Suspension, ride height, aero, drag, weight bias |
| `chopperspecs` | `0x5d898ee7` | 24 | 108 B | 3 | Police helicopter behavior |
| `collisionreactions` | `0xb32682f1` | 4 | 96 B | 31 | Vehicle collision physics reactions |
| `controller` | `0x2dee1998` | 152 | 8 B | 21 | DualShock 2 / wheel controller mappings & deadzones |
| `damagespecs` | `0xc1f0b434` | 16 | 104 B | 14 | Visual & hitbox damage multipliers |
| `ecar` | `0xa5b543b7` | 49 | 285 B | 92 | Render wheel offsets, kit dimensions, car visuals |
| `effects` | `0xebcee74c` | 20 | 16 B | 198 | VFX linkages |
| `emitterdata` | `0xb30b18af` | 45 | 289 B | 205 | Particle emitters (smoke, sparks, rain, debris) |
| `emittergroup` | `0xaba86e60` | 4 | 136 B | 128 | Grouped particle emitters |
| `emitteruv` | `0xe4983a7d` | 4 | 16 B | 4 | Particle UV atlases |
| `engine` | `0xf1f5fbc7` | 7 | 96 B | 80 | Torque curves, RPM limits, speed limiters |
| `engineaudio` | `0x50eab0e6` | 40 | 170 B | 70 | `.GIN` / `.ABK` engine sound profiles |
| `explosion` | `0x6434f1fb` | 6 | 32 B | 1 | Explosion parameters |
| `fecooling` | `0x5d417978` | 11 | 0 B | 1 | Safehouse heat cooldown rules |
| `frontend` | `0x85885722` | 55 | 97 B | 0 *(411 in `FE_ATTRIB.BIN`)* | Car lot, upgrades, unlock costs |
| `fuelcell_effect` | `0x6f5943f1` | 2 | 4 B | 4 | Fuelcell particle effects |
| `fuelcell_emitter` | `0xb267a856` | 19 | 148 B | 8 | Fuelcell emitters |
| `gameplay` | `0x5cea9d46` | 220 | 8 B | 0 *(6,287 in `GAMEPLAY.BIN`)* | Race events, checkpoints, Blacklist progression |
| `induction` | `0xc92a0142` | 7 | 28 B | 64 | Turbo / supercharger spool & PSI boost |
| `infractions` | `0x2870de90` | 1 | 4 B | 8 | Pursuit infraction bounties |
| `junkman` | `0x171737e9` | 7 | 0 B | 1 | Junkman bonus part modifiers |
| `light_flares_cg` | `0xc7c5806d` | 8 | 44 B | 22 | Headlight, taillight, streetlight & siren lens flares |
| `milestonetypes` | `0xe4c3d904` | 4 | 8 B | 30 | Pursuit milestone categories |
| `music` | `0x565465f8` | 5 | 52 B | 27 | Interactive music triggers |
| `nos` | `0xb1669f64` | 8 | 32 B | 6 | Nitrous boost, flow, capacity & recharge (`nos_base`, `nos_top`, `starter`, `cops`) |
| `ocean` | `0x093d7c56` | 5 | 0 B | 1 | Water/ocean shader parameters |
| `presetride` | `0x27e73952` | 7 | 0 B | 25 | Preset / Blacklist / Bonus cars |
| `pursuitescalation` | `0xd6d4330b` | 4 | 0 B | 1 | Maps Heat 1–10 (`heattable`, `racetable`, `supporttable`, `supportracetable`) |
| `pursuitlevels` | `0x551e22b3` | 57 | 196 B | 21 | Heat 1–10 (`heat_01`..`heat_10`, `race_01`..`race_10`, `default`) cop rules |
| `pursuitsupport` | `0x77b93104` | 4 | 0 B | 21 | Heavy SUV, leader, and helicopter tactical strategies |
| `pvehicle` | `0x4a97ec8f` | 66 | 78 B | 113 | Master vehicle specs (`MASS`, `TENSOR_SCALE`, `MODEL`, part links) |
| `rigidbodyspecs` | `0x7c90bb38` | 23 | 272 B | 22 | Rigid body inertia & friction |
| `shiftpattern` | `0xdb01b754` | 24 | 88 B | 25 | Automatic transmission shift schedules |
| `simsurface` | `0xfb111fef` | 24 | 251 B | 47 | Asphalt, wet road, grass, gravel surface grip/drag |
| `smackable` | `0xce70d7db` | 30 | 36 B | 181 | Breakable props & pursuit breakers |
| `speech` | `0xc593dd47` | 28 | 30 B | 133 | Police dispatch speech cues |
| `speechtune` | `0xbc683501` | 39 | 178 B | 1 | Dispatch chatter pacing |
| `system` | `0x4e8dce05` | 3 | 232 B | 1 | Engine system globals |
| `tires` | `0xbd38d1ca` | 9 | 80 B | 86 | Tire grip (`GRIP_SCALE`, `STATIC_GRIP`, `DYNAMIC_GRIP`, `STEERING`, `YAW_SPEED`, `YAW_CONTROL`) |
| `trafficpattern` | `0x20d08342` | 5 | 4 B | 10 | District traffic mix, `SpawnTime`, `SpeedStreet`, `SpeedHighway` |
| `transmission` | `0x07a7a3e5` | 9 | 132 B | 80 | Gear ratios, final drive, shift speed |
| `turbosfx` | `0x55624a85` | 6 | 32 B | 18 | Blow-off valve & turbo whistle |
| `visuallook` | `0x339f7d3d` | 8 | 224 B | 5 | **PS2 Visual Treatment grading** (`default`, `sunset`, `uves`, `extreme_uves`, `copcam`) |
| `visuallookeffect` | `0x6ab2d241` | 7 | 80 B | 7 | Radial blur, NOS blur, camera flash, pursuit breaker pulse |
| `visuallooktransition` | `0x0f409aa6` | 7 | 0 B | 1 | Spline curves blending normal ↔ UVES looks |
| `visualrgbtweaker` | `0xaf1837ca` | 3 | 0 B | 1 | Per-channel R/G/B 4×4 color-curve LUT splines |
| `world` | `0x6d90da55` | 33 | 0 B | 1 | Global `CONTROLLER_CURVE`, `MAX_FRAGMENTS`, `MAX_SMACKABLES`, `MAX_NEWTONS` |

---

## 4. Lighting & Visual-Treatment Vaults on PS2 (`SLUS-21351`)

A major difference between the PC/Xbox 360 and PS2 builds of *Most Wanted* was verified empirically by scanning `GLOBAL\ATTRIBUTES.BIN`, `GLOBAL\FE_ATTRIB.BIN`, `GLOBAL\GAMEPLAY.BIN`, `GLOBAL\GLOBALB.LZC`, `GLOBAL\INGAMEB.LZC`, `TRACKS\L2RA.BUN`, `TRACKS\L2RA_SUNSET.BUN`, and `TRACKS\STREAML2RA.BUN`:

### 4.1 What **IS** in `GLOBAL\ATTRIBUTES.BIN` (VLT-Patchable Today!)

1. **`visuallook` (`0x339f7d3d`, 5 collections)** — Controls the iconic MW2005 color bloom tint, desaturation, and shadow-crushing black bloom rendered by `IVisualTreatmentPlat::RenderMWVisualLook` (`0x21a008` in `SLUS_213.51`):

| Collection | Key Hash | Layout Offset | Stock `ColourBloomTint` (RGBA) | `ColourBloomIntensity` | `Desaturation` | `DetailMapIntensity` | `BlackBloomIntensity` |
|---|---|---:|---|---:|---:|---:|---:|
| `default` (Midday) | `0xeec2271a` | `0x000456f0` | `[0.87, 0.68, 0.04, 1.0]` @ `0x0457b0` | `0.32` @ `0x0457c0` | `0.25` @ `0x0457c4` | `0.0` @ `0x0457c8` | `0.38` @ `0x0457cc` |
| `sunset` | `0xceda4e4f` | `0x00045610` | `[1.00, 0.44, 0.05, 1.0]` @ `0x0456d0` | `0.30` @ `0x0456e0` | `0.25` @ `0x0456e4` | `0.0` @ `0x0456e8` | `0.34` @ `0x0456ec` |
| `uves` | `0x681bef75` | `0x00045450` | `[0.50, 0.81, 1.00, 1.0]` @ `0x045510` | `0.47` @ `0x045520` | `0.25` @ `0x045524` | `0.0` @ `0x045528` | `0.34` @ `0x04552c` |
| `extreme_uves` | `0xd42906b1` | `0x00045530` | `[0.99, 0.77, 0.20, 0.99]` @ `0x0455f0` | `0.28` @ `0x045600` | `0.20` @ `0x045604` | `0.0` @ `0x045608` | `0.34` @ `0x04560c` |
| `copcam` | `0xee6074a3` | `0x000457d0` | `[0.37, 0.42, 0.49, 0.99]` @ `0x045890` | `0.65` @ `0x0458a0` | `0.00` @ `0x0458a4` | `0.19` @ `0x0458a8` | `0.39` @ `0x0458ac` |

2. **`visualrgbtweaker` (`0xaf1837ca`, 1 collection `default`)** — Three 4×4 spline matrices (`green` @ `0x00012880`, `blue` @ `0x000128c0`, `red` @ `0x00012900`) driving the PS2 GS CLUT color-grade transfer curve. Notice the stock blue curve drops sharply at highlights (`[0.936, 0.446]`), reinforcing the yellow-sepia highlight cast!
3. **`visuallookeffect` (`0x6ab2d241`, 7 collections)** — `default`, `pursuitbreaker` (`0x90d06c71`), `uves_transition`, `uves_radialblur`, `nos_radialblur`, `camera_flash`, `uves_pulse`.
4. **`visuallooktransition` (`0x0f409aa6`, 1 collection `default`)** — Transition curve matrices (`normal`, `uves`, `uves_extreme`) and `_testvalue`.
5. **`light_flares_cg` (`0xc7c5806d`, 22 collections)** — Headlight, taillight, police lightbar, and streetlamp flare sizes/colors.

### 4.2 Where Non-VLT Lighting Lives on PS2 (Why `timeofdaylighting` Is Absent!)

Although the cross-platform allocator strings `Attrib::Gen::timeofdaylighting`, `Attrib::Gen::lightmaterials`, and `Attrib::Gen::lightshaders` remain compiled inside `SLUS_213.51` (`.rodata` at `0x3eacc0..0x3eb000`), **none of those three classes exist in PS2 `GLOBAL\ATTRIBUTES.BIN` or any other VPAK on the disc**.

Instead, PS2's GS/VU1 renderer stores world lighting in binary `bChunk` packs:
- **`SunInfos` (`bChunk 0x00034202`)**: lives in **`GLOBAL\GLOBALB.LZC`** (decompressed offset `0x0ABAB8`, `1,360 B` = 5 × 272-byte sun lighting presets: `PARKLAND`, `SUNSET`, `PROTOTYPE`, `E3DEMO`, `MIDDAY`). Note that `GLOBAL\GLOBALB.LZC` is *also* embedded inside `GLOBAL\GLOBALMEMORYFILE.BIN` at `0x0ABEC0`!
- **`LightMaterials` (`bChunk 0x00135200`, ×156)**: also lives in **`GLOBAL\GLOBALB.LZC`**.
- **Separate Sunset Map Geometry/Vertex Lighting**: PS2 ships dedicated sunset world files (`TRACKS\L2RA_SUNSET.BUN` = 1.38 MB, `TRACKS\STREAML2RA_SUNSET.BUN` = 191.01 MB) alongside daytime `TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN`, plus `GenericRegions / Weatherman` (`0x00034250`) inside `L2RA.BUN`.
- **Traffic Density Spawn Curve**: While `trafficpattern` VLT controls district vehicle rosters, `SpawnTime`, `SpeedStreet`, and `SpeedHighway`, global traffic density scaling lives in ELF `.data` (`Tweak_TrafficDensitySpawnRates[11]` in `SLUS_213.51`).

---

## 5. Toolchain Usage (`tools/vlt_ps2.py`)

```bash
# 1. Backwards-compatible summary inspection
python3 tools/vlt_ps2.py "/path/to/NFSMW_BE.iso" --virtual-path 'GLOBAL\ATTRIBUTES.BIN'

# 2. List all 56 classes & collection counts (also works on GAMEPLAY.BIN / FE_ATTRIB.BIN)
python3 tools/vlt_ps2.py classes "/path/to/NFSMW_BE.iso"
python3 tools/vlt_ps2.py classes "/path/to/NFSMW_BE.iso" --virtual-path 'GLOBAL\GAMEPLAY.BIN'

# 3. List collections in any class
python3 tools/vlt_ps2.py nodes "/path/to/NFSMW_BE.iso" camerainfo
python3 tools/vlt_ps2.py nodes "/path/to/NFSMW_BE.iso" visuallook

# 4. Dump decoded fields, types, storage, and exact file byte offsets
python3 tools/vlt_ps2.py dump-node "/path/to/NFSMW_BE.iso" camerainfo close
python3 tools/vlt_ps2.py dump-node "/path/to/NFSMW_BE.iso" visuallook default
python3 tools/vlt_ps2.py dump-node "/path/to/NFSMW_BE.iso" tires bmwm3gtre46 --inherited

# 5. Apply verified JSON recipe(s) — aborts untouched if any expected_orig or offset mismatches!
python3 tools/vlt_ps2.py patch \
  --iso "/path/to/NFSMW_BE.iso" \
  --virtual-path 'GLOBAL\ATTRIBUTES.BIN' \
  --recipe patches/vlt/modern_camera.json \
  --recipe patches/vlt/modern_handling_tuning.json \
  --recipe patches/vlt/pursuit_and_traffic.json \
  --recipe patches/vlt/modern_visual_look.json \
  --out build/ATTRIBUTES_patched.bin \
  --memfile-out build/GLOBALMEMORYFILE_patched.bin
```

### Verified Starter Recipes (`patches/vlt/`)

| Recipe File | Writes | Summary |
|---|---:|---|
| [`patches/vlt/modern_camera.json`](../../patches/vlt/modern_camera.json) | 18 | Modern 16:9 chase (`close`/`far`), `hood`, and `bumper` FOV, height, lag & stiffness |
| [`patches/vlt/modern_handling_tuning.json`](../../patches/vlt/modern_handling_tuning.json) | 25 | Linearized global `CONTROLLER_CURVE`, snappier `nos_base`/`nos_top` boost & regen, sharper grip/steering/braking on `bmwm3gtre46`, `m3gtre46careerstart`, `gallardo`, `carreragt`, `corvette`, `fordgt`, `cobaltss` |
| [`patches/vlt/pursuit_and_traffic.json`](../../patches/vlt/pursuit_and_traffic.json) | 21 | Faster Rockport traffic pacing (`SpawnTime`, `SpeedStreet`, `SpeedHighway`, district vehicle spawn rates) & heightened Heat 1–5 patrol/roadblock/engagement rules |
| [`patches/vlt/modern_visual_look.json`](../../patches/vlt/modern_visual_look.json) | 11 | Neutralizes heavy 2005 amber bloom tint (`ColourBloomTint`) and harsh `BlackBloomIntensity`/`Desaturation` on `visuallook` (`default`, `sunset`, `uves`) |
