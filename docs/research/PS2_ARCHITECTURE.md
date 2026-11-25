# PlayStation 2 Architecture & Disc Forensics (`SLUS-21351`)

> **Project:** NFSMW: Rockport 2026  
> **Target Disc:** *Need for Speed: Most Wanted — Black Edition* (NTSC-U, `SLUS-21351`, `v1.01`)  
> **Verification Status:** All offsets, counts, structures, and ELF patch addresses in this document were directly verified against an unmodified `SLUS-21351` retail disc image.

---

## 1. Disc Identity & Baseline Specifications

| Property | Verified Value |
| :--- | :--- |
| **Title** | *Need for Speed: Most Wanted — Black Edition* |
| **Region / Serial** | NTSC-U / `SLUS-21351` |
| **Disc Version** | `v1.01` |
| **PCSX2 ELF CRC** | `0518D274` (matches `SLUS-21351_0518D274.pnach`) |
| **Total Image Size** | `3.449 GiB` (`3,702,915,072` bytes) |
| **Total Sectors** | `1,808,064` sectors (`2,048` bytes/sector) |
| **Boot Executable** | `SLUS_213.51` (`4,918,740` bytes, LBA `300`) |
| **Primary Asset Index** | `NFS/ZDIR.BIN` (`34,728` bytes, `1,447` records, LBA `140141`) |
| **Primary Asset Containers** | `NFS/ZZDATA0.BIN` – `NFS/ZZDATA3.BIN` (contiguous on disc) |

---

## 2. ISO9660 / UDF Disc Layout

The retail disc contains **25 filesystem entries** spanning the PlayStation 2 boot loader, a bundled playable demo of *007: From Russia With Love* (`FRWL/`), IOP system modules, and the main Need for Speed archive payload (`NFS/`).

### Key Disc Entries

| Path | LBA (Sector) | Size | Notes |
| :--- | :---: | :---: | :--- |
| `SYSTEM.CNF` | Early FS | Metadata | Boot configuration pointing to `cdrom0:\SLUS_213.51;1` (`v1.01`) |
| `SLUS_213.51` | `300` | `4,918,740` bytes | Main Emotion Engine (EE) MIPS ELF executable |
| `SCE_DD/SCE_DD.ELF` | — | — | Sony disc/demo launcher executable |
| `FRWL/` | — | ~275 MB | Bundled *007: From Russia With Love* bonus demo |
| `NFS/IOPRP280.IMG` | — | — | PlayStation 2 IOP replacement image |
| `NFS/IRX281A.BUN` | — | — | Bundled IOP IRX driver modules (set A) |
| `NFS/IRX281B.BUN` | — | — | Bundled IOP IRX driver modules (set B) |
| `NFS/ZDIR.BIN` | `140141` | `34,728` bytes | Master asset directory (17 sectors; sits directly before `ZZDATA0.BIN`) |
| `NFS/ZZDATA0.BIN` | `140158` | ~800 MiB | Archive volume `0` (409,600 sectors) |
| `NFS/ZZDATA1.BIN` | `549758` | ~765 MiB | Archive volume `1` (391,791 sectors) |
| `NFS/ZZDATA2.BIN` | `941549` | ~708 MiB | Archive volume `2` (362,510 sectors) |
| `NFS/ZZDATA3.BIN` | `1304059` | ~964 MiB | Archive volume `3` — **last file on the disc** |

```text
Disc Sector Map (1,808,064 total sectors @ 2,048 B/sector = 3.449 GiB)
┌────────────┬──────────────┬───────────┬────────┬─────────┬─────────┬─────────┬─────────┬────────────┐
│ ISO/UDF FS │ SLUS_213.51  │ SCE_DD +  │ ZDIR   │ ZZDATA0 │ ZZDATA1 │ ZZDATA2 │ ZZDATA3 │ Free Tail  │
│  Headers   │  (LBA 300)   │ FRWL +    │(140141)│(140158) │(549758) │(941549) │(1304059)│  (~20 MB)  │
│            │              │ IOP/IRX   │        │         │         │         │         │            │
└────────────┴──────────────┴───────────┴────────┴─────────┴─────────┴─────────┴─────────┴────────────┘
                                                                                           └─► + ~900 MB
                                                                                               DVD-5 headroom
```

### Crucial Modding Discovery: Path-Based I/O & Expansion Headroom

Inspection of `SLUS_213.51` confirms a major structural advantage for non-destructive modding:

1. **No Hardcoded ELF LBAs:** `SLUS_213.51` opens `cdrom0:\NFS\ZDIR.BIN;1` and `cdrom0:\NFS\ZZDATA%d.BIN;1` via standard ISO9660 path resolution rather than hardcoded sector offsets baked into the executable.
2. **Preserve the Boot ELF CRC (`0518D274`):** Because LBAs are resolved dynamically through ISO9660 and `ZDIR.BIN`, mod builders can modify or relocate game data without modifying `SLUS_213.51`. Keeping the ELF untouched preserves CRC `0518D274`, ensuring PCSX2's built-in `GameIndex.yaml` hardware fixes, `.pnach` code patches, and texture replacement directory continue to match automatically.
3. **Zero-Friction Expansion Space:** `ZZDATA3.BIN` is the **final file on the disc**:
   - **~20 MB of free trailing sectors** exist inside the original `3.449 GiB` image immediately after `ZZDATA3.BIN`.
   - **~900 MB of DVD-5 headroom** remains available before hitting the single-layer DVD-5 boundary (~4.37 GiB), allowing relocated or expanded archives to be appended directly to `ZZDATA3.BIN` by updating its ISO9660 extent size and volume descriptor.

---

## 3. `NFS/ZDIR.BIN` Archive Directory Format

`ZDIR.BIN` (`34,728` bytes) is a flat, headerless table of **1,447 fixed-size records** (`1,447 × 24 = 34,728` bytes). Each record consists of six unsigned 32-bit little-endian integers (`<6I`).

### Binary Record Layout

```c
// 24 bytes per entry, little-endian (<6I)
typedef struct {
    uint32_t bStringHash;              // EA Black Box hash of uppercase '\'-delimited path
    uint32_t zzdata_index;             // Archive index: 0..3 -> NFS\ZZDATA%d.BIN
    uint32_t sector_offset_in_zzdata;  // 2048-byte sector offset within ZZDATA<index>.BIN
    uint32_t cumulative_sector_offset; // 2048-byte sector offset from start of ZZDATA0.BIN
    uint32_t byte_size;                // Payload size in bytes
    uint32_t secondary_hash;           // Secondary hash / validation field
} ZDirRecord;
```

### Ordering & Path Hashing (`bStringHash`)

- **Binary Search Ordering:** Records in `ZDIR.BIN` are **sorted strictly ascending by `bStringHash`**, enabling fast binary search lookups at runtime. Any tool that adds or rehashes entries must preserve ascending `bStringHash` order.
- **Hash Algorithm:** EA Black Box `bStringHash` operates on **ASCII uppercase backslash-separated paths** (for example: `GLOBAL\ATTRIBUTES.BIN`, `GLOBAL\GAMEPLAY.BIN`, `CARS\WHEELS\GEOMETRY.BIN`, `FRONTEND\PLATFORMS\CAREER_SAFEHOUSE.BIN`):

```python
def bstring_hash(path: str) -> int:
    """EA Black Box bStringHash (init = 0xFFFFFFFF, multiplier = 33)."""
    h = 0xFFFFFFFF
    for ch in path.upper().encode("ascii"):
        h = (h * 33 + ch) & 0xFFFFFFFF
    return h
```

### Patching & Relocation Rules

Because assets inside `ZZDATA0..3.BIN` are packed back-to-back on 2,048-byte sector boundaries:

- **In-Place Replacement:** Valid whenever `ceil(new_byte_size / 2048) <= ceil(old_byte_size / 2048)`. Update `byte_size` (and `secondary_hash` if required by runtime checks) in `ZDIR.BIN`, pad trailing bytes of the final sector with zeros, and overwrite in place.
- **Relocation (Growing Files):** When a modified asset requires more sectors than its original slot, append the new payload to the end of `ZZDATA3.BIN` (using the ~20 MB free tail or extending the ISO within DVD-5 headroom), point the `ZDIR.BIN` record's `zzdata_index = 3`, update `sector_offset_in_zzdata`, `cumulative_sector_offset`, and `byte_size`, and update `ZZDATA3.BIN`'s file size in the ISO9660/UDF metadata.

---

## 4. Asset Magic Census (`1,447` `ZDIR.BIN` Entries)

Scanning the leading 4-byte signatures across all `1,447` `ZDIR.BIN` payloads reveals the asset composition of the PS2 build:

| Leading 4-Byte Magic | Entry Count | Asset Category | Modding Relevance |
| :--- | :---: | :--- | :--- |
| `ABKC` | `301` | EA Audio Banks | SFX, ambient, and sound-bank inspection/replacement |
| `00a10300` | `267` | Front-End / UI Chunks | Menus, safehouse screens, HUD layouts (`FRONTEND\...`) |
| `000030b3` | `247` | EA TPK / BIN Bundles | PS2 texture packs (`TEXTURES.BIN`, `VINYLS.BIN`, bundled resources) |
| `00401380` | `161` | Geometry / Track Chunks | Vehicle meshes (`GEOMETRY.BIN`), world/track streaming chunks |
| `Gnsu` | `160` | Ginsu Engine Synths | Procedural engine audio synthesis data |
| `MPCh` | `34` | Video Streams | Pre-rendered FMV cutscenes and intro movies |
| `LOCH` | `19` | Localization Tables | Language string tables (menus, subtitles, Blacklist text) |
| `SCHl` | Streamed | Streamed Music | Interactive/streamed EA `SCHl` soundtrack and voice assets |

*(The remaining entries include `SCHl` streams, multi-part/split archive segments, and uncompressed vault payloads such as `VPAK`.)*

---

## 5. VLT Vaults: `ATTRIBUTES.BIN` & `GAMEPLAY.BIN`

Two uncompressed EA **`VPAK`** VLT (Vault) databases inside `ZDIR.BIN` control almost all gameplay tuning, vehicle physics/handling, pursuit behavior, visual treatment parameters, and race/career configuration:

| Path | Verified Size | Role |
| :--- | :---: | :--- |
| `GLOBAL\ATTRIBUTES.BIN` | `703,296` bytes | Vehicle physics/handling, AI, pursuit heat, visual treatment, camera & global attributes |
| `GLOBAL\GAMEPLAY.BIN` | `2,103,680` bytes | Career progression, Blacklist events, race triggers, markers, and world gameplay nodes |

### Internal VLT Chunk Structure

Both files are **uncompressed `VPAK`** containers holding standard EA VLT section blocks. Because PlayStation 2's Emotion Engine is little-endian, the 4-character chunk tags appear byte-reversed on disc:

| Logical Tag | Little-Endian On-Disc 4CC | Function |
| :--- | :--- | :--- |
| `DepN` | `NpeD` | Dependency table (external vault/hash references) |
| `StrN` | `NrtS` | String pool for vault names and string-typed fields |
| `DatN` | `NtaD` | Raw attribute record binary payloads |
| `ExpN` | `NpxE` | Export table mapping collection/class hashes to data |
| `PtrN` | `NrtP` | Pointer relocation / fixup table patched at load time |

### Why a Pure-Python PS2 VLT Toolkit Is Essential

Legacy PC modding tools — specifically **`NFS-VltEd`** and **`OGVI`** — target the Windows PC build (`GLOBAL\ATTRIBUTES.BIN` / `FE_ATTRIB.BIN` layout and PC-specific vault packing) and **fail to load or safely edit PS2 `VPAK` vaults**.

Building a clean-room, pure-Python PS2 VLT parser and serializer in **NFSMW: Rockport 2026** closes one of the biggest tooling gaps in the PS2 NFS community, unlocking reproducible YAML-driven edits for:

- **Visual Treatment** tuning (color grading, bloom curves, lighting tint) directly in engine data
- **Handling & Physics** rebalancing (grip, suspension, weight transfer, braking, nitro)
- **Pursuit & AI** modernization (heat escalation, cop aggression, roadblocks, rubber-banding)
- **Gameplay & Event** authoring inside `GLOBAL\GAMEPLAY.BIN`

---

## 6. Executable (`SLUS_213.51`) Layout & Verified Patches

### ELF Program Header (`PT_LOAD`)

`SLUS_213.51` is a 32-bit little-endian MIPS R5900 (Emotion Engine) ELF executable containing a **single `PT_LOAD` segment**:

| ELF Field | Verified Value | Notes |
| :--- | :--- | :--- |
| **Segment Type** | Single `PT_LOAD` | All static `.text`, `.rodata`, `.data`, and `.bss` live in one loadable range |
| **File Offset (`p_offset`)** | `0x1000` | Start of loadable bytes inside `SLUS_213.51` |
| **Virtual Address (`p_vaddr`)** | `0x00100000` | EE RAM base address |
| **File Size (`p_filesz`)** | `0x4abbdc` | Loadable image bytes (`vaddr` range `0x00100000` – `0x005ABBDC`) |
| **Memory Size (`p_memsz`)** | `0x4d6ed0` | Total EE RAM footprint including `.bss` (`0x00100000` – `0x005D6ED0`) |
| **Entry Point (`e_entry`)** | `0x00436128` | Initial EE program counter |

For any static address `vaddr` within `[0x00100000, 0x005ABBDC)`, the exact byte offset inside `SLUS_213.51` is:

```text
file_offset = vaddr - 0x00100000 + 0x1000 = vaddr - 0x000FF000
```

### Directly Verified Patch Addresses (`SLUS_213.51` / PCSX2 `.pnach`)

Every static address below was checked directly against the `SLUS_213.51` binary to verify original bytes prior to authoring our `.pnach` patch suite:

| Feature | EE Virtual Address | Original Value | Patched Value | Notes |
| :--- | :---: | :---: | :---: | :--- |
| **16:9 Widescreen (Aspect Ratio)** | `0x0054BA38` | `0x3F35C28F` (`0.71f`) | `0x3F400000` (`0.75f`) | Static float in ELF; paired with runtime RAM flag `0x00AAFDD4 = 1` |
| **16:9 Widescreen (Runtime Flag)** | `0x00AAFDD4` | *(runtime RAM)* | `0x00000001` | Runtime state flag outside `PT_LOAD` (applied via `.pnach`) |
| **Remove Brown/Yellow Filter** | `0x00550D58` | `0x00000000` | `0x00000001` | Matches official PCSX2 `fobes` patch; disables heavy amber color wash |
| **Remove Motion Blur (Flag 1)** | `0x0055014C` | `0x00000001` | `0x00000000` | Disables motion-blur enable flag |
| **Remove Motion Blur (Code 1)** | `0x001FEDB8` | `0x24020001` | `0x24020000` | MIPS: `addiu $v0, $zero, 1` → `addiu $v0, $zero, 0` |
| **Remove Motion Blur (Code 2)** | `0x001FEDBC` | `0x14620004` | `0x10620004` | MIPS: `bne $v1, $v0, +4` → `beq $v1, $v0, +4` |
| **Remove Motion Blur (Flag 2)** | `0x00550D28` | `0x00000001` | `0x00000000` | Secondary motion-blur state toggle |
| **Remove Atmospheric Brume/Haze** | `0x005500F0` | `0x00000001` | `0x00000000` | Clears heavy distance haze/brume post-pass |

> **Design Rule:** Ship all code/memory patches via **`SLUS-21351_0518D274.pnach`** by default rather than permanently modifying `SLUS_213.51`. This preserves CRC `0518D274` in PCSX2 and allows every visual toggle to be enabled or disabled independently.

---

## 7. Open-Source Ecosystem & Technical References

The following community repositories form the technical ecosystem surrounding PS2 *Need for Speed: Most Wanted* reverse engineering, disc mastering, and C/MIPS development:

| Repository / Project | Domain | Relevance to Rockport 2026 |
| :--- | :--- | :--- |
| **`Ekey/NFS.BIN.Tool`** | Archive Extraction | Reference implementation for EA NFS `.BIN` / `ZDIR` / `ZZDATA` structures |
| **`N4gtan/mkps2iso`** | Disc Mastering | Open-source PS2-compliant ISO9660/UDF image builder for clean disc rebuilds |
| **`dbalatoni13/nfsmw`** | 1:1 Decompilation | Open-source decompilation specifically targeting **`SLUS-21351`** — key reference for symbols, structs, physics, and 60 FPS delta decoupling |
| **`xan1242/NFSMW-PS2-ICE`** | PS2 NFSMW Tooling | Community PS2 NFSMW research and modification utilities |
| **`ps2dev/ps2sdk`** | PS2 C/MIPS Toolchain | Official open-source EE GCC/MIPS toolchain for compiling future C plugins and runtime hooks |
| **`ThirteenAG/PCSX2PluginInjector`** | Runtime Plugin Injection | Blueprint for injecting compiled C plugins under PCSX2 *(note: ThirteenAG's `WidescreenFixesPack` does **not** currently include an NFSMW PS2 plugin)* |
| **`NFSTools/VaultLib`** | VLT Schema Reference | Open-source C# VLT format library; invaluable schema guide for our pure-Python PS2 VLT editor |
| **`NFSTools/Attribulator`** | VLT CLI Architecture | Reference workflow for YAML-based VLT attribute modding and serialization |
| **`NFSTools/Binary`** | EA Chunk Formats | Reference for EA `.BUN`, TPK, and geometry chunk conventions across Black Box titles |
