# 02 — NFS: Most Wanted (PS2) data formats: read / extract / modify / repack

Target disc: **NFS Most Wanted Black Edition, NTSC-U, SLUS-21351** (boot ELF `SLUS_213.51`).
Research date: 2026-11-24. Research only: nothing was downloaded or executed, and nothing under
`~/Documents/PS2Games` was read. Source code was read on GitHub. The three ZDIR records
given in the brief were used to check the findings.

Confidence legend: **[H]** high: confirmed in game code (decompilation/symbols) *and* checked against our disc's
records. **[M]** medium: in shared engine code or in a sister game, not yet checked on our disc. **[L]** low:
inference or community claim.

---

## 0. TL;DR

* **ZDIR.BIN = array of 24-byte little-endian `bFileDirectoryEntry` records, sorted ascending by hash.** [H]
  `{u32 Hash; s32 FileNumber; s32 LocalSectorOffset; s32 TotalSectorOffset; s32 Size; u32 Checksum}`.
  * `FileNumber` selects `NFS\ZZDATA<n>.BIN`.
  * The data is at `LocalSectorOffset * 2048` inside that file.
  * `TotalSectorOffset` is the same sector counted across ZZDATA0..n laid end to end.
  * `Size` is in bytes and the data is **not** compressed at the archive level.
* **Name hash = EA `bStringHash`:** `h = 0xFFFFFFFF; for c in path: h = h*33 + c` (u32), applied to the
  **upper-cased path with `/`→`\`**, relative to `NFS\` (e.g. `GLOBAL\GLOBALB.LZC`). [H] All 3 sample
  records from our disc match:
  * `0x0035FC77` = `MOVIES\BLACKLIST_12_ENGLISH_NTSC.MPC`
  * `0x00621487` = `SOUND\ENGINE\SWTN_CAR_16_MB.ABK`
  * `0x00766252` = `CARS\GALLARDO\VINYLS.BIN`
* **Checksum = engine `bCalculateCrc32(data, size, 0xFFFFFFFF)`.** This is a CRC-32 with polynomial 0x04C11DB7, MSB-first, with the byte length appended (POSIX-`cksum`-like)
  and a final complement. The retail game does **not** verify it (`bVerifyGiantFileChecksums = 0`). [M/H]
* **The game reads ZZDATA by byte offset through a normal file handle opened by name, so appending data works.**
  * You can put a file anywhere sector-aligned in *any* ZZDATA file. You can even add a new `ZZDATA4.BIN`:
    the PS2 driver probes ZZDATA0…ZZDATA15 by name.
  * Then repoint the ZDIR record and keep the table sorted.
  * `TotalSectorOffset` is only used to pick the nearest of several **duplicate** copies and for seek statistics. [H]
* **Compression lives inside files, behind the 16-byte `LZHeader`** (`JDLZ` v2, `HUFF`, `RAWW`, legacy `COMP`).
  `.LZC` files are whole compressed `.BUN`s. TPK textures can also be compressed one by one. [H]
* **PS2 is little-endian (MIPS R5900).** PC-format structures such as VLT/AttribSys, bChunks and string tables share endianness and
  32-bit pointers with PS2. The PS2-specific parts are textures (GS formats/swizzle/CLUT), meshes (VU1/VIF strips),
  audio codecs and FMV (EA MPC = MPEG-2). [M]
* **The single best resource is the CC0 decompilation `dbalatoni13/nfsmw`.** It **targets SLUS-21351 directly**
  (ELF sha1 `60289f3f8b643822a302720f9649ddc4ba55034b`) and ships:
  * a 16k-symbol address map for our exact ELF,
  * full PS2 type dumps from the PS2 Alpha-124 prototype,
  * the bChunk ID list,
  * the AttribSys hash list,
  * generated VLT class layouts.

---

## 1. Disc layout and boot chain

| Path | What it is | Notes |
|---|---|---|
| `/SLUS_213.51` | EE boot ELF | Decomp config: sha1 `60289f3f8b643822a302720f9649ddc4ba55034b`, `.text` at vram 0x100000. Sections include `.vutext` (VU microcode) and `.over` (code overlay region, §6.4). [H] |
| `/NFS/ZDIR.BIN` | Disculator directory, 34,728 B = 1447 × 24 | §2 [H] |
| `/NFS/ZZDATA0..3.BIN` | "Giant files" holding every game file | §2 [H] |
| `/NFS/IOPRP280.IMG` | IOP replacement-module image (SDK 2.8.0 era IOPRP) | Not part of ZDIR. Leave untouched. [L] |
| `/NFS/IRX281A.BUN`, `IRX281B.BUN` | EA bundles of IOP `.irx` modules | Not part of ZDIR. [L] |
| `/FRWL/` | 007 From Russia With Love demo | Independent. Ignore it, but note where it sits on the disc (LBA) if you plan to grow ZZDATA3 in place. |

At boot the game runs `InitBigFiles()`. If `NFS\ZDIR.BIN` exists it calls
`bInitDisculatorDriver("NFS\\ZDIR.BIN", "NFS\\ZZDATA")`, which registers a device `"discu:"` and adds it as a
high-priority search location. Files not found in ZDIR fall through to the other search locations (the
plain disc/host file system). [H for the code, M for the fall-through behaviour on retail PS2]
Source: `src/Speed/Indep/Src/Misc/Main.cpp`, `bFile.cpp` in the decomp.

The retail ELF also contains a `Ps2HostFileDeviceDriver` (symbols `Getsize__23Ps2HostFileDeviceDriveri` @0x48fd30
etc.). This means a **PCSX2 "Enable Host Filesystem" + boot-the-ELF** development workflow is plausible. xan1242
documents exactly this workflow for the MW PS2 OPM demo. It is untested on retail; see open questions. [M]

---

## 2. ZDIR.BIN / ZZDATA*.BIN — the "Disculator" giant-file system

### 2.1 Record layout (PS2, little-endian, 24 bytes) [H]

```c
// DisculatorDriver::bFileDirectoryEntry  (decomp bFile.hpp; identical in PS2 Alpha-124 type dump)
struct bFileDirectoryEntry {          // sizeof == 0x18
    uint32 Hash;               // 0x00  bFileGetFilenameHash(path)  (table sorted ascending, unsigned)
    int32  FileNumber;         // 0x04  n in NFS\ZZDATA<n>.BIN
    int32  LocalSectorOffset;  // 0x08  2048-byte sector inside ZZDATA<n>   -> byte offset = value << 11
    int32  TotalSectorOffset;  // 0x0C  LocalSectorOffset + sum(sectors of ZZDATA0..n-1)
    int32  Size;               // 0x10  exact byte size (no archive-level compression)
    uint32 Checksum;           // 0x14  bCalculateCrc32(data, Size, 0xFFFFFFFF)  (not verified at runtime)
};
// Hot Pursuit 2 (PS2) used an older 12-byte record {Hash, Sector, Size} with a single ZZDATA.BIN.
```

**Checked against the brief's records:**

| Hash | Name (resolved) | File | Local sector | Total sector | Total − Local | Size |
|---|---|---|---|---|---|---|
| 0x0035FC77 | `MOVIES\BLACKLIST_12_ENGLISH_NTSC.MPC` | 1 | 194,565 | 604,165 | **409,600** | 18,309,040 |
| 0x00621487 | `SOUND\ENGINE\SWTN_CAR_16_MB.ABK` | 2 | 357,613 | 1,159,004 | **801,391** | 43,872 |
| 0x00766252 | `CARS\GALLARDO\VINYLS.BIN` | 2 | 245,110 | 1,046,501 | **801,391** | 2,448,384 |

From these rows: ZZDATA0 = 409,600 sectors = **838,860,800 bytes (exactly 800 MiB)**, and ZZDATA1 = 801,391 − 409,600 =
391,791 sectors = **802,387,968 bytes**. Checking these against the real file sizes is a cheap first test for any
tool. (Ekey's packer computes the same cumulative value; see §2.6.)

### 2.2 Name hash [H]

```python
def bStringHash(s: bytes, h: int = 0xFFFFFFFF) -> int:      # EA bWare Strings.cpp
    for c in s:
        h = (h * 33 + c) & 0xFFFFFFFF                        # (h<<5)+h+c, chars as unsigned bytes
    return h

def bFileGetFilenameHash(path: str) -> int:                  # bFile.cpp
    p = path.upper().replace('/', '\\')                       # discu: driver also strips "discu:" + leading slashes
    return bStringHash(p.encode('ascii'))
```

* Paths are **relative to `NFS\`**: `GLOBAL\ATTRIBUTES.BIN`, `TRACKS\STREAML2RA.BUN`, `CARS\<CAR>\GEOMETRY.BIN`,
  `FRONTEND\FRONTB.LZC`, and so on.
* `bStringHashUpper` (upper-cases while hashing) also exists.
* The 32-bit hash has a negligible collision risk at about 1.5k entries.

**Recovering names:**
* Ekey's `FileNames.list` has 33,655 candidate paths across all console NFS games, and all 3 of our sample hashes are in it.
* For the rest, use the nfsu2-native method (`tools/zdir_names.py`): hash candidate paths built from ELF strings and
  `printf` patterns found in code. Examples:
  * `CARS\%s\GEOMETRY.BIN`, `TEXTURES.BIN`, `VINYLS.BIN`
  * `TRACKS\STREAM%s.BUN`, `TRACKS\%s.BUN`
  * `Frontend\CodeOverlay%s.bin`
  * `TRACKS\L2RA\%s.BIN` (minimaps)
  * `Global\*MemoryFile.bin`

### 2.3 Lookup and read path (what the game actually does) [H]

From `DisculatorDriver` in decomp `bFile.cpp`. The symbols exist in our ELF: `LoadGiantFiles` @0x31af40,
`FindDirectoryEntry` @0x31b0d8, `Open` @0x31ac98, `Read` @0x31adf0.

1. **Load the directory.** `LoadGiantFiles` reads the whole ZDIR (`num_entries = filesize / 24`) and
   endian-swaps it (a no-op on PS2). It then builds names `NFS\ZZDATA%d.BIN` for n = 0..MAX−1 and opens them with
   `FILESYS_opensync` **until the first missing one**.
   * MAX is **16 on PS2** (`GiantDataFileName[16][64]` in the PS2 type dump) and 30 on GameCube.
   * So **adding `ZZDATA4.BIN` (up to `ZZDATA15.BIN`) needs no code change.**
2. **Find the entry.** `FindDirectoryEntry` does a **binary search** on `Hash` for the *first* record with that hash.
   It then walks forward over **all records with the same hash** and picks the one whose
   `TotalSectorOffset` is closest to the drive's current sector.
   * **Duplicate records (same file stored several times on disc to cut seeks) are a supported feature.**
   * When you replace a file, repoint/replace **every** duplicate, or delete the extras.
3. **Read.** `Read` seeks the parent handle (the opened ZZDATA file) to
   `seekPos + (LocalSectorOffset << 11)` and reads. `TotalSectorOffset` only feeds the seek-distance
   heuristic and `gFileStats`.
   * It is **not** used for addressing, so a slightly wrong value is harmless. Keep it correct anyway (cumulative sum).
4. **Memory files win.** The `bFile` constructor checks **memory files first** (§2.5): if a hash is
   found in a loaded memory file, ZDIR is never consulted.

**Answer to "does it read by sector / can we append?":** Yes. ZZDATA files are opened **by name** through the ISO9660
file system, and data is addressed as a byte offset that is a multiple of 2048 inside that file. You can:

* **(a) Overwrite in place** if the new data fits in the old allocation. Space available = next file's start sector − this
  start sector; the gap is padding.
* **(b) Append to the end of any ZZDATA*n*.** Grow that file's ISO9660 extent and update `FileNumber` /
  `LocalSectorOffset` / `TotalSectorOffset` / `Size`.
* **(c) Ship a new `ZZDATA4.BIN`** containing every modified file, and repoint their records to `FileNumber = 4`.
  This is the cleanest mod layout because the original ZZDATA0–3 stay byte-identical.

In every case: keep the table **sorted unsigned-ascending by Hash**, and fix up **all** duplicates.

### 2.4 Checksum [M/H]

```python
# bWare bCrc32.cpp: table = CRC-32/MPEG-2 style (poly 0x04C11DB7, MSB-first, non-reflected)
def bCalculateCrc32(data: bytes, prev: int = 0xFFFFFFFF) -> int:
    crc = prev
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ TABLE[((crc >> 24) ^ b) & 0xFF]
    n = len(data)
    while n > 0:                       # append length, LSB first (like POSIX cksum)
        crc = ((crc << 8) & 0xFFFFFFFF) ^ TABLE[((crc >> 24) ^ (n & 0xFF)) & 0xFF]
        n >>= 8
    return ~crc & 0xFFFFFFFF
```

* Ekey derived the same function, with a do/while length loop that differs only for 0-byte files. He verified it on
  `GLOBAL\FRONTENDMEMORYFILE.BIN` (16 bytes → 0xC08BE7BA).
* The decomp has `static const int bVerifyGiantFileChecksums = 0;`, so retail does not check. Write correct values anyway.
* The same `bCalculateCrc32` also validates the code overlay (§6.4).

### 2.5 Memory files (bundles of small files, checked *before* ZDIR) [H]

Files: `GLOBAL\GLOBALMEMORYFILE.BIN`, `INGAMEMEMORYFILE.BIN`, `FRONTENDMEMORYFILE.BIN`,
`CARHEADERSMEMORYFILE.BIN`, `PERMANENTMEMORYFILE.BIN` (some may not exist on PS2).

```c
struct MemoryFile {                 // file image is used in-place
    uint32 node_next, node_prev;    // 0x00 bTNode (runtime list links; zero/garbage on disk)
    uint32 Magic;                   // 0x08 0x53219999 ("MEMO" in bchunks.txt)
    int32  NumFileEntries;          // 0x0C
    MemoryFileEntry FileEntries[];  // 0x10, 0x14 each (max 8192)
};
struct MemoryFileEntry {            // 0x14
    uint32 Hash;        // bFileGetFilenameHash(path)
    int32  Offset;      // from start of the memory file
    int32  FileSize;
    int32  MemorySize;
    uint32 Data;        // runtime pointer (= base + Offset), ignore on disk
};
```

`FRONTENDMEMORYFILE.BIN` being 16 bytes fits an empty memory file (8 + 4 + 4). **If a file is inside a memory
file, editing its standalone ZZDATA copy has no effect.** You must rebuild the memory file instead.

### 2.6 Existing archive tools

| Tool | Lang / License | Does | Notes |
|---|---|---|---|
| [Ekey/NFS.BIN.Tool](https://github.com/Ekey/NFS.BIN.Tool) | C# / **no license** | Unpack + pack ZDIR/ZZDATA. Lists MW PS2 as tested (unpack). Bundled `FileNames.list` | Packer computes `TotalSectorOffset` = cumulative sectors; "packer not tested in games". Rewrites all archives (no incremental mode). Treat as reference only (no license). |
| [Sh2dow/NFS.BIN.Tool](https://github.com/Sh2dow/NFS.BIN.Tool) fork | C# / no license | `-flat` output, .NET 10/Linux, "TotalOffset fix" | 2026 commits |
| [mad0xStrixhadorami/nfsu2-native](https://github.com/mad0xStrixhadorami/nfsu2-native) `tools/zz_extract.py`, `tools/zdir_names.py` | **Python / MIT** (LICENSE file) | UG2 PS2 extractor + ELF-strings name recovery | Same 24-byte format. Handles duplicates (`.dupN`). Best direct Python base. |
| [z3rotheone/nfshp2-tools](https://github.com/z3rotheone/nfshp2-tools) | Python (+C, Java) / MIT | HP2 PS2: extract, COMP, TPK↔PNG with swizzle, music, **in-place ISO writing** (`hp2_iso.py`) | Old 12-byte ZDIR. Good patterns for the ISO patcher and the TPK round-trip verify. |
| NFS-Disculator (nfsu360) [blog post](https://nfs-tools.blogspot.com/2018/09/nfs-disculator-v10-released.html) | .NET GUI / closed | Browse/extract ZDIR from ISO (HP2…Undercover; PS2/PS3/Xbox/X360) | Extract only |
| aluigi `nfs_bun.bms` (QuickBMS), see [ZenHAX t=3547](https://www.zenhax.com/viewtopic.php@t=3547.html) | BMS / – | Extract | Reimport failed for Carbon PS2 in that thread |

Successful in-game repacks: **no public report found for MW PS2.** HP2 PS2 in-place writing is reported to work
(nfshp2-tools). ProStreet PS2 has an xdelta mod on nfsmods.xyz (mod 6310), so console NFS repacks are possible in
practice. [L]

### 2.7 ISO-level notes for repacking

* The game opens ZZDATA by name, so any ISO rebuild that keeps `/NFS/ZZDATA*.BIN` names is fine.
  * LBAs may move. Order only matters for seek time on real hardware.
  * ISO9660 32-bit file size: each ZZDATA must stay below 4 GiB.
* **Grow-in-place:** if `ZZDATA3.BIN` (or whichever file) is the **last extent on the image**, you can append sectors at the end of the
  image. Then patch that file's directory record (both-endian extent length) and the PVD volume-space size, plus the UDF
  descriptors if present. Check the LBAs: `/FRWL/` may lie after it.
* **New ZZDATA4.BIN** needs an ISO rebuild, e.g. `pycdlib` (Python, LGPL-2.1,
  [clalancette/pycdlib](https://github.com/clalancette/pycdlib)) or `xorriso`/`mkisofs` on macOS.
* The PS2 BIOS/CDVD use ISO9660. PCSX2 doesn't need UDF. Real-hardware rebuilds usually keep the ISO9660+UDF bridge. [M]
* Total > 4.38 GiB would need DVD9 on real hardware; PCSX2/OPL don't care. [M]

---

## 3. Compression (inside files)

### 3.1 Common 16-byte header [H]

```c
// LZCompress.hpp (decomp) — identical in PS2 Alpha-124 type dump
struct LZHeader {                  // 0x10
    uint32 ID;                // 'JDLZ' | 'HUFF' | 'RAWW' | 'COMP'   (ASCII in file order)
    uint8  Version;           // JDLZ: 2 ; HUFF/RAWW/COMP: 1
    uint8  HeaderSize;        // 0x10
    uint16 Flags;             // COMP: 1 = stored (uncompressed)
    uint32 UncompressedSize;
    uint32 CompressedSize;    // INCLUDING this header
};
```

* `LZDecompress()` (@0x31f878 in SLUS-21351) dispatches on ID. Other functions: `JLZDecompress` @0x31c778, `OldLZDecompress` (COMP) @0x31f5b8.
* `LZCompress()` in the engine tries HUFF and JDLZ and keeps the smaller (RAWW if neither helps). You can **always
  write `RAWW` (stored)** or `JDLZ`; Attribulator writes `gameplay.lzc` as RAWW and the game accepts it (PC/Carbon PS2).
* Where it applies:
  * `ResourceFile` (`LoadResourceFile(..., flags)`): flag bit 3 (`flags & 8`) = whole file compressed, e.g. `GLOBAL\GLOBALB.LZC`,
    `FRONTEND\FRONTB.LZC`, `GLOBAL\INGAMEB.LZC` (loaded with flags 9).
  * Per-texture compressed TPKs (§5.3).
  * The bChunk list also has `0x55441122 LZCompressed` and `0x5A4C444A` ('JDLZ' as u32) "compressed chunk".

### 3.2 JDLZ v2 decoder (port of CC0 `JLZDecompress`; untested sketch) [H for algorithm]

```python
def jdlz_decompress(src: bytes) -> bytes:
    assert src[:4] == b'JDLZ' and src[4] == 2
    usize, csize = struct.unpack_from('<II', src, 8)
    out = bytearray(); p = 18; size = csize - 18
    control, runtype = src[16] | 0x100, src[17] | 0x100
    while size > 0:
        if control & 1:
            b0, b1 = src[p], src[p + 1]
            if runtype & 1:   # near: 12-bit run, 4-bit offset
                run, off = (((b0 >> 4) << 8) | b1) + 3, (b0 & 0x0F) + 1
            else:             # far: 5-bit run, 11-bit offset
                off, run = (((b0 >> 5) << 8) | b1) + 17, (b0 & 0x1F) + 3
            for _ in range(run): out.append(out[-off])
            p += 2; size -= 2; runtype >>= 1
        else:
            if len(out) < usize: out.append(src[p]); p += 1
            size -= 1
        control >>= 1
        if control == 1: size -= 1; control = src[p] | 0x100; p += 1
        if runtype == 1: size -= 1; runtype = src[p] | 0x100; p += 1
    return bytes(out[:usize])
```

### 3.3 COMP (legacy LZ, used heavily by HP2) [H]

* After the header: a 16-bit little-endian control word (with a 0x10000 sentinel).
* Bit = 1: two bytes `lenmt, lo`. Distance = `((lenmt & 0xF0) << 4) | lo`. Copy `3 + (lenmt & 0x0F)` bytes.
* Bit = 0: one literal.
* `Flags == 1` means stored.

### 3.4 HUFF [H]

EA "Huff" (types 0x30FB/0x32FB/0x34FB, the last two with delta post-filters). It is long: port it directly from the
decomp `src/Speed/Indep/Src/Misc/LZCompress.cpp` (`HUFF_decompress`), which is CC0.

---

## 4. bChunk containers (.BUN, .BIN, .LZC payloads) [H]

```c
struct bChunk { uint32 ID; int32 Size; /* Size bytes of payload follow */ };
// ID & 0x80000000  -> payload is a list of child chunks
// ID == 0          -> padding chunk; padding bytes are 0x11 (BCHUNK_ALIGNMENT_PADDING 0x11111111)
// Loaders are registered per ID (bChunkLoader); unknown IDs are skipped.
```

Selected IDs. Full list: decomp [`symbols/bchunks.txt`](https://github.com/dbalatoni13/nfsmw/blob/main/symbols/bchunks.txt).

| ID | Meaning |
|---|---|
| 0xB3300000 | TexturePack (TPK) → 0xB3310000 TPK Info {0x33310001 Header, 0x33310002 Keys, 0x33310003 Entries / compressed-entry table, 0x33310004 TextureInfo[], 0x33310005 Comps (PS2: TextureInfoPlatInfo[])}, 0xB3320000 TPK Data {0x33320001 header, 0x33320002 data} |
| 0xB0300100 / 0x30300101..3 | Texture animation pack |
| 0x80134000 | GeometryPack (solid list) → 0x80134001 info {0x00134002 header, 0x00134003 keys, 0x00134004 offsets}, 0x80134010 SolidPack {0x00134011 solid header, 0x00134012 texture hashes, 0x00134013 light materials, …}, 0x80134100 mesh container |
| 0x00134900 / 0x00134B01..03 | (PC) mesh descriptor / vertex buffer / shading groups / faces |
| 0x00134A01 / 0x00134A02 | (PS2, per UG2 PS2 research) strip-entry table / VIF strip data. Verify on MW. [M] |
| 0x00034110..0x00034113 | TrackStreaming Sections / Infos / Barriers / DiscBundle |
| 0x80034100, 0x00034101..0x0003410C | Scenery sections, infos, instances, tree nodes, overrides, model hierarchy |
| 0x00034600 | CarTypeInfos (PS2: 0xD0 bytes each) |
| 0x80034602 / 0x00034603..0x0003460D | DBCarParts (car parts DB) |
| 0x00030201/0x00030203/0x00030210 | FEng font / FEng packages / compressed FEng |
| 0x00039000 / 0x00039001 / 0x00039010 / 0x00039020 | String blocks (language), lang font, subtitles, movie catalog |
| 0x8003CE00.. | Vinyl system |
| 0x0003B200.., 0x8003B200.. | ICE camera data (NIS cameras) |
| 0x00036001..3 / 0x80036000 | Event triggers |
| 0x53219999 | MEMO (memory file) · 0x55441122 LZCompressed · 0x4B415056 'VPAK' (attrib pack) · 0x434B4241 'ABKC' |

---

## 5. Textures — PS2 TPK

### 5.1 Structures (PS2 build; from the Alpha-124 PS2 type dump) [H for layout, M for the final build]

```c
struct TexturePackHeader {          // chunk 0x33310001, 0x7C
    int32  Version;                 // 0x00  (value for MW PS2: TBD)
    char   Name[28];                // 0x04
    char   Filename[64];            // 0x20
    uint32 FilenameHash;            // 0x60
    uint32 PermChunkByteOffset;     // 0x64
    uint32 PermChunkByteSize;       // 0x68
    int32  EndianSwapped;           // 0x6C
    uint32 pTexturePack, TextureIndexEntryTable, TextureStreamEntryTable; // 0x70.. runtime ptrs
};

struct TextureInfo {                // chunk 0x33310004, element 0x7C on PS2
    uint32 PlatInfo;                // 0x00 runtime ptr (TextureInfoPlatInterface)
    uint32 next, prev;              // 0x04 bTNode
    char   DebugName[24];           // 0x0C
    uint32 NameHash;                // 0x24  bStringHash(name)
    uint32 ClassNameHash;           // 0x28
    uint32 ImageParentHash;         // 0x2C
    int32  ImagePlacement;          // 0x30  offset into TPK data
    int32  PalettePlacement;        // 0x34  -1 if none
    int32  ImageSize;               // 0x38
    int32  PaletteSize;             // 0x3C
    int32  BaseImageSize;           // 0x40
    int16  Width, Height;           // 0x44, 0x46
    int8   ShiftWidth, ShiftHeight; // 0x48, 0x49 (log2)
    uint8  ImageCompressionType;    // 0x4A  TEXCOMP_*: 4, 8, 16, 24, 32, 17(1555),18(565),19(3555),128(8BIT_16),129(8BIT_64)
    uint8  PaletteCompressionType;  // 0x4B
    int16  NumPaletteEntries;       // 0x4C
    int8   NumMipMapLevels;         // 0x4E
    int8   TilableUV, BiasLevel, RenderingOrder, ScrollType, UsedFlag; // 0x4F..0x53
    int8   ApplyAlphaSorting, AlphaUsageType, AlphaBlendType, Flags;   // 0x54..0x57
    int16  ScrollTimeStep, ScrollSpeedS, ScrollSpeedT, OffsetS, OffsetT, ScaleS, ScaleT; // 0x58..0x64
    uint32 pTexturePack, pImageParent, ImageData, PaletteData; int32 ReferenceCount; // 0x68..0x78 runtime
};

struct TexturePlatImageInfo { int16 Width, Height, TBP; int8 TBW, IsSwizzled; };   // 8 bytes, one per mip
struct TextureInfoPlatInfo {        // chunk 0x33310005 on PS2, 0x54 each ("84 bytes of GS settings")
    int16 ImageVramBlocksRequired;  // 0x00
    int16 PaletteVramBlocksRequired;// 0x02
    int8  PixelStorageMode;         // 0x04  GS PSM: 0x00 CT32, 0x01 CT24, 0x02 CT16, 0x0A CT16S, 0x13 T8, 0x14 T4, 0x1B T8H, 0x24 T4HL, 0x2C T4HH
    int8  ClutPixelStorageMode;     // 0x05
    int8  TextureFx, AlphaBits, AlphaFix, MMag, MMin, MipMapL; // 0x06..0x0B
    int16 MipMapK;                  // 0x0C
    int8  AlphaUsage, Special;      // 0x0E, 0x0F
    TexturePlatImageInfo ImageInfos[7]; // 0x10  per-mip W/H/TBP/TBW/IsSwizzled
    uint32 pActiveTexture, pStackedActiveTexture; // 0x48 runtime
    int16 PaletteVramAddress, ImageVramAddress;   // 0x50
};
```

The UG2 PS2 decoder in nfsu2-native reads the **same 0x7C record**: name at +12, hash at +36, data/palette offsets at
+48/+52, sizes at +56/+60, W/H at +68/+70, bpp at +74, alpha at 0x55/0x56. So the layout is stable across UG2→MW PS2. [H]

### 5.2 Pixel data rules (PS2 GS) [M]

* **8-bit** (PSMT8) and most **4-bit** (PSMT4) images are stored **pre-swizzled in GS memory order**.
  * MW has an explicit per-mip `IsSwizzled` flag. UG2 tools had to use heuristics (small textures, ≤16 wide, are linear).
  * Each mip level is swizzled by its own dimensions.
  * Unswizzle and re-swizzle are mirror operations: swap read and write ([ResHax thread](https://reshax.com/topic/696-c-code-to-swizzle-4bpp-ps2-textures/)).
  * There are two 4bpp variants: "4bpp swizzled as if 8bpp" and the true PSMT4 layout. Test against the PlatInfo PSM.
* **256-colour CLUTs** are stored CSM1-style: within every 32 entries, entries 8–15 and 16–23 are swapped
  (`i ^ 0x18` when `(i & 0x18)` is 8 or 16).
* **Alpha:** 0x80 = opaque (scale ×2, clamp 255). 32-bit images also use 0–0x80 alpha.
* GS tables (block/column) are in nfsu2-native `tools/gs_tables.py` (taken from PCSX2 GSTables).

### 5.3 Compressed and streamed TPKs [M/H]

* **Compressed TPK** (e.g. `LANGUAGES\LANGUAGETEXTURES.BIN`): chunk 0x33310003 holds 24-byte entries
  `{u32 hash, u32 offset (from TPK chunk), u32 compSize, u32 decompSize, …}`. Each entry is a JDLZ/HUFF block that expands to
  `pixels | palette | TextureInfo(0x7C) | TextureInfoPlatInfo(0x54)`. Offsets inside it are relative to the uncompressed pack.
* **Streaming textures** (`eStreamingEntry`, 0x18: `{NameHash, ChunkByteOffset, ChunkByteSize, UncompressedSize, UserFlags, Flags, RefCount, ChunkData}`):
  the loaded blob ends with `TextureInfo + TextureInfoPlatInfo` (decomp `TextureLoadedStreamingEntryCallback`).

### 5.4 Texture tools

| Tool | Lang / License | PS2? |
|---|---|---|
| nfsu2-native `src/game/indep/Texture/TexturePack.cpp`, `tools/tex4_*.py`, `gs_tables.py` | C++/Python, MIT | **Yes (UG2 PS2)**: closest to MW PS2 |
| nfshp2-tools `hp2_tpk.py` | Python, MIT | Yes (HP2 PS2, older TPK). Palette-sharing-aware import, round-trip `verify` |
| Alpha 1001's Carbon/MW PS2 TPK editor ([ResHax 696](https://reshax.com/topic/696-c-code-to-swizzle-4bpp-ps2-textures/)) | ? / unreleased? | Yes (MW/Carbon PS2, TM2 export/import). Not located |
| [xan1242/xnfstpktool](https://github.com/xan1242/xnfstpktool) | C++, MIT | PC/PS3/X360/Xbox only |
| NFS-TexEd (nfsu360), mwtc ([NFSTools/mwtc](https://github.com/NFSTools/mwtc)) | closed / C# no-license | PC only |
| [bartlomiejduda/ReverseBox](https://github.com/bartlomiejduda/ReverseBox) | Python, GPL-3.0 | Generic PS2 swizzle functions (licence is viral) |

---

## 6. Global / frontend / in-game bundles

### 6.1 Load order (decomp `GameFlow.cpp`; shared engine code, PS2 path variations possible) [M]

* `Global\GlobalMemoryFile.bin` → `GLOBAL\ATTRIBUTES.BIN` (`InitializeSingleAttributeVault(buf,"db")`) →
  `GLOBAL\GLOBALB.LZC` (compressed bundle; hot-chunk name `GLOBAL\GLOBALB.BUN`).
* Then `CARS\BRAKES\GEOMETRY.BIN` / `TEXTURES.BIN`, `GLOBAL\WIDESCREEN_GLOBAL.BUN` or `THINSCREEN_GLOBAL.BUN`,
  and `GLOBAL\FE_ATTRIB.BIN`.
* Frontend: `FRONTEND\FRONTA.BUN`, `FRONTEND\FRONTB.LZC`.
* In-game: `Global\InGameMemoryFile.bin`, `Global\InGameA.bun`, `GLOBAL\INGAMEB.LZC`, `TRACKS\<REGION>.BUN`,
  `TRACKS\STREAM<REGION>.BUN`.
* `*_VM_NGC*` files are GameCube-only (ARAM).
* Shared car packs: `CARS\TEXTURES.BIN`, `CARS\WHEELS\*`, `CARS\SPOILER*\GEOMETRY.BIN`, `CARS\ROOF\GEOMETRY.BIN`,
  `GLOBAL\DYNTEX.BIN`, `GLOBAL\HUDTEX*.BIN`.
* `GLOBALA.BUN` / `GLOBALB` hold global chunks: CarTypeInfos 0x00034600, DBCarParts 0x80034602, presets, FEng packages,
  global TPKs. Exact split on PS2: TBD.
* `.HOT` files are dev hot-reload metadata (`InitHotChunks` is empty in retail).

### 6.2 Cars

* `CARS\<NAME>\GEOMETRY.BIN` (solid pack), `TEXTURES.BIN` (TPK), `VINYLS.BIN` (vinyl TPK) and sometimes `PREVINYL.BIN`.
* `CarTypeInfo` (PS2 0xD0) holds `CarTypeName[16]`, `BaseModelName[16]`, `GeometryFilename[32]`, `ManufacturerName[16]`,
  hash, headlight data, `Type`, `UsageType`, skin data, and so on.

### 6.3 Geometry (PS2 solids) [H for structs, M for strip semantics]

```c
struct eSolid {                     // 0xE0 on PS2 (header inside 0x00134011)
    uint32 PlatInfo;                // 0x00 -> eSolidPlatInfo
    uint32 next, prev;              // 0x04
    uint8  Version, EndianSwapped; uint16 Flags;          // 0x0C
    uint32 NameHash;                // 0x10
    int16  NumPolys, NumVerts;      // 0x14
    int8   NumBones, NumTextureTableEntries, NumLightMaterials, NumPositionMarkerTableEntries; // 0x18
    int32  ReferencedFrameCounter;  // 0x1C
    float  AABBMin[3]; uint32 pTextureTable; float AABBMax[3]; uint32 LightMaterialTable; // 0x20..0x3C
    float  PivotMatrix[16];         // 0x40
    uint32 PositionMarkerTable, NormalSmoother, ModelList[2], DamageVertexTable, ConnectivityData; // 0x80..
    float  Volume, Density;         // 0x98
    char   Name[64];                // 0xA0
};
struct eSolidPlatInfo { uint16 Version, StripFlags, NumStrips, pad; uint32 SizeofStripData; uint32 StripEntryTable, StripDataStart; }; // 0x14
struct eStripEntry {                // 0x10
    uint16 Flags; uint8 NumVerts, NumPolys; float MaterialDensity;
    uint16 DataQWordOffset; int8 Nz, Nt; uint8 DataQWordSize, PolyGroupNumber; int8 TextureNumber, LightMaterialIndex;
};
```

Each strip is a VIF/VU1 packet: unpacks of positions, UVs, colours and normals, run by VU1 microcode in the ELF's `.vutext`.
UG2-PS2 research (same engine):
* `NumPolys < NumVerts-2`: strips are joined by "bridge" triangles.
* Restarts are driven by the **ADC bit computed from the VU R register** (seeded from VIF data) and an odd-vertex UV swap.
* See nfsu2-native `tools/vif_dump.py`, `strip_topology.py`, `solid_mesh.py` (MIT).

**Writing new PS2 geometry is the hardest part of the project:** VIF packet generation plus the ADC seeding.

### 6.4 Code overlay (important for code patches) [H]

The game loads `Frontend\CodeOverlay<ver>.bin` into the ELF's `.over` region.
* On first use it compares `bCalculateCrc32(file)` with the CRC of the in-memory overlay region.
* On a mismatch it **disables overlays**.
* Later it streams the overlay from disc when switching FE ↔ in-game.

So any ELF patch inside `.over`, or any pnach/PCSX2 patch hitting that range, must also be applied to
`FRONTEND\CODEOVERLAY.BIN`, or it will be overwritten or rejected.

---

## 7. Track streaming (`TRACKS\L2RA.BUN` + `TRACKS\STREAML2RA.BUN`) [H for structs, M for PS2 final field names]

* `L2RA.BUN` (the region, loaded by `LoadResourceFile`) contains the streaming tables:
  * `0x00034110` = `TrackStreamingSection[]`, 0x54 each
  * `0x00034111` Infos (`int FileSize[2]`)
  * `0x00034112` Barriers
  * `0x00034113` DiscBundle
* `STREAML2RA.BUN` is a blob of per-section chunk lists at `FileOffset`.
* Other per-region files: `TRACKS\L2RA\MINI_MAP*.BIN`, `TRACKMAPS.BIN` (TPK), `TROUGHBOUNDARY.BIN`.

```c
struct TrackStreamingSection {      // 0x54 (PS2 Alpha-124)
    char  SectionName[8]; int16 SectionNumber; int8 WasRendered, CurrentlyVisible;
    int32 Status; int32 FileType;   // 0 = PERMANENT, 1 = REGION
    int32 FileOffset;               // into STREAM<region>.BUN
    int32 Size; int32 PermSize;     // (GC build has CompressedSize here)
    int32 SectionPriority; float Centre[2]; float Radius; uint32 Checksum;
    int32 LastNeededTimestamp; uint32 UnactivatedFrameCount; int32 LoadedTime;
    int32 BaseLoadingPriority, LoadingPriority; uint32 pMemory, pDiscBundle; int32 LoadedSize;
};
struct DiscBundleSection { int32 FileOffset, FileSize; char SectionName[11]; int8 NumMembers;
                           struct { int16 SectionNumber, FileOffset; uint32 pSection; } Members[32]; }; // 0x114
```

**To edit a streamed section:** rebuild `STREAM*.BUN`, then rewrite `FileOffset`/`Size` (and the disc-bundle
offsets/sizes and `TrackStreamingInfo.FileSize`) in `L2RA.BUN`. Whether `Checksum` is verified in retail: TBD.

---

## 8. VLT / AttribSys (`GLOBAL\ATTRIBUTES.BIN`, `GAMEPLAY.BIN/.LZC`, `FE_ATTRIB.BIN`)

### 8.1 Format [H for format, M that PS2 files load unmodified in the tools]

* The container is an **`VPAK` attribute pack**:
  * header `{u32 'VPAK'=0x4B415056, u32 NumEntries, u32 StringBlockOffset, u32 StringBlockSize}`
  * then entries `{u32 VaultNameOffset, BinSize, VltSize, BinOffset, VltOffset}`
  * 0x40/0x80 alignment
* Each vault = a `.vlt` stream plus a `.bin` stream of 8-byte-header chunks:
  * `Vers` 0x56657273, `StrN` 0x5374724E, `DepN` 0x4465704E, `DatN` 0x4461744E, `ExpN` 0x4578704E, `PtrN` 0x5074724E, `EndC` 0x456E6443
  * `.bin` side: `StrE` 0x53747245
  * IDs are u32 LE, so the ASCII appears reversed on disk.
* Export types: `ClassLoadData` 0x5E970CBC, `DatabaseLoadData` 0xCBBC628F, collections 0x8E112EB7.
* **Hash:** Bob Jenkins lookup2 ("VLT32") with init **0xABCDEF00** (VaultLib `VLT32Hasher`).
  Name list: decomp `symbols/vlt.txt` (~400 KB) and Attribulator `Resources/hashes.txt`.
* PS2 is little-endian and 32-bit like PC. **Attribulator ships a `CARBON_PS2` profile that is code-identical to PC
  Carbon** (same VPAK, X86 32-bit DB). So MW PS2 files should load with the `MOST_WANTED` profile, possibly with
  class/field differences. [M]
* `gameplay.lzc` = `RAWW` header + `gameplay.bin` (Attribulator writes it this way).

### 8.2 Where the interesting parameters live (class layouts from decomp `Generated/AttribSys/Classes/*.h`) [H for class names/fields]

| Topic | VLT classes (class key) | Code-side knobs |
|---|---|---|
| Car handling / performance | `pvehicle` (0x4A97EC8F: MASS, TENSOR_SCALE, MODEL, *_upgrades…), `engine` (0xF1F5FBC7: TORQUE[9], MAX_RPM, RED_LINE, IDLE, FLYWHEEL_MASS, SPEED_LIMITER, ENGINE_BRAKING), `transmission`, `tires`, `chassis`, `brakes`, `induction`, `nos`, `rigidbodyspecs`, `damagespecs`, `collisionreactions`, `simsurface`, `acceltrans`, `shiftpattern` | – |
| Cops / heat | `pursuitlevels` (cops[3], heli/roadblock chances, TimePerHeatLevel, evadetimeout, LOS distances, FullEngagement*, BackupCallTimer…), `pursuitescalation` (0xD6D4330B), `pursuitsupport` (0x77B93104), `aivehicle` (0x22515733: MAXIMUM_AI_SPEED, TETHER_WEIGHT), `chopperspecs`, `infractions` | `AICopManager` (@0x103ee0…) |
| Traffic | `trafficpattern` (0x20D08342: `Vehicles` = TrafficPatternRecord[]) | **Density is a code table** `Tweak_TrafficDensitySpawnRates[11] = {0,.05,.1,.125,.2,.4,.6,1,3,5,8}` in ELF `.data` (`AITrafficManager.cpp`) — patch the ELF |
| Camera | `camerainfo` (0x93C171E4: STIFFNESS[2], LATOFFSET[2], LAG[2], FOV[2], HEIGHT[2], ANGLE[2], TILTING[2], SELECTABLE[2]) | `CDAction*` classes |
| Visual treatment / grading | `visuallook` (0x339F7D3D: DetailMapCurve, BlackBloomCurve, ColourBloomCurve (4×4), ColourBloomTint, ColourBloomIntensity, Desaturation, DetailMapIntensity, BlackBloomIntensity), `visuallookeffect`, `visuallooktransition`, `visualrgbtweaker`, `timeofdaylighting` (0x399ED882: Specular/Diffuse/Ambient/FogHaze colours, fog scales, CarSpecScale) | Collections: Midday 0xEEC2271A, Sunset 0xCEDA4E4F, Uves 0x681BEF75, CopCam 0xEE6074A3. Effects: UvesPulse 0x334F1E4D, UvesRadialBlur 0xBFA7B6AC, UvesTransition 0x15746132, CameraFlash 0x30656612, PursuitBreaker 0x90D06C71. PS2 renderer: `IVisualTreatmentPlat::RenderMWVisualLook` @0x21a008, `ApplyColorBloom` @0x219210, `ApplyDetailMap` @0x219818, `Radialblur` @0x219a68, `ApplyPulseBrightness` @0x219938. PCSX2's GameIndex notes the final grade is a **CLUT-based colour LUT pass** (`cpuCLUTRender`) |
| Career / races / unlocks | `gameplay` vault (`GAMEPLAY.BIN/.LZC`), `milestonetypes`, `presetride` | `GRaceDatabase`, `UnlockSystem` |
| Audio | `engineaudio`, `turbosfx`, `music`, `speech`, `aud_*` | – |

### 8.3 VLT tools

| Tool | Lang / License | Notes |
|---|---|---|
| [NFSTools/VaultLib](https://github.com/NFSTools/VaultLib) | C#, **MIT** | Reference for the format. Big-endian/64-bit aware. **Best to port to Python** |
| [NFSTools/Attribulator](https://github.com/NFSTools/Attribulator) | C#, no license | CLI export/import to YAML/text. MW & `CARBON_PS2` profiles |
| [NFSTools/vltedit](https://github.com/NFSTools/vltedit) (Arushan's VLTEdit source) | C#, no license | Historical |
| NFS-VltEd (nfsu360), OGVI (built on Attribulator) | closed | PC-oriented GUIs |

---

## 9. Audio & FMV (brief) [M]

* **FMV:** `MOVIES\*.MPC`. One of our ZDIR hashes is `MOVIES\BLACKLIST_12_ENGLISH_NTSC.MPC`, so MW PS2 uses **EA MPC**:
  * `MPCh` chunks carry an elementary MPEG-2 video stream, interleaved with `SCHl` EA-ADPCM audio.
  * Demux/decode: FFmpeg `electronicarts` demuxer ([source](https://ffmpeg.org/doxygen/trunk/electronicarts_8c_source.html)) and vgmstream `ea_schl_video` (audio).
  * Re-encoding needs a custom muxer: MPEG-2 ES (e.g. ffmpeg `mpeg2video`), re-chunked into `MPCh`, plus EA-XA audio in `SCh*`.
  * PSS (Sony) is not used.
  * Format refs: [MultimediaWiki EA MPC](https://wiki.multimedia.cx/index.php/Electronic_Arts_MPC), [EA Formats](https://wiki.multimedia.cx/index.php/Electronic_Arts_Formats).
* **Audio** under `SOUND\` and `SNDSTREAMS\`:
  * `.ABK` = AEMS banks ('ABKC')
  * `.GIN` = 'Gnsu' granular engine loops
  * `SOUND\PFDATA\*.MPF` + `*.MUS` = EA Pathfinder v5 interactive music
  * `.AST` = stream banks
  * speech: `.BIG/.CSI/.EVT/.IDX/.PCK` (Csis speech system)
  * UG2 PS2's MUS = `SCHl` (GSTR) with **EA-XA v2** frames (nfsu2-native `tools/ea_mus.py`, MIT).
  * Decoding: [vgmstream](https://github.com/vgmstream/vgmstream) (C, permissive ISC-style) supports `.abk`, `.ast`,
    `.mpf+.mus`, `.gin`, `.mpc` audio.
  * Music editing: [xan1242/MPFmaster](https://github.com/xan1242/MPFmaster) (C++, MIT; MW listed) decompiles and compiles MPF and
    rebuilds MUS sample sets.

---

## 10. Build differences (SLUS-21267 vs SLUS-21351)

**Serials** (PCSX2 GameIndex):
* SLUS-21267 "Most Wanted": NTSC-U. Dump sites list v1.00 and v2.00 printings; EA "Black Label" reprints also use 21267.
* **SLUS-21351 "Most Wanted [Black Edition]"**: NTSC-U.
* PAL regular: SLES-53557/53558/53559. PAL Black Edition: SLES-53857.
* PCSX2 uses identical GS fixes for all of them and **shares memory-card saves between 21267 and 21351**.

**Content:** BE adds a '67 Camaro and a BMW M3 GTR (street version), pre-tuned bonus cars, 3 extra events / a
challenge-series event, and bonus vinyls (sources: retail listings, StrategyWiki).

**In code** [H]:
* BE content is gated by a single global `IsCollectorsEdition` (`GetIsCollectorsEdition()` in `FEDatabase.cpp`).
* `UnlockSystem::IsBonusCarCEOnly()` (@0x28a590) lists the CE car hashes `CE_GTRSTREET, CE_C6R, CE_GT2, CE_CAMARO, CE_CORVETTE, CE_997S, CE_SL65` plus 3 raw hashes.
* Races carry `IsCollectorsEditionRace` in the gameplay DB.
* Vinyls with brand `"CEO"` are hidden unless CE.
* Hidden Palace notes the PS2 Alpha-124 prototype can be switched to BE with a single patch (`002A4BF8 00000000`).

**Inference** [L]: the regular disc may contain much of the BE data, with the ELF flag turning it off. Diff the two
ZDIRs to confirm.

**Recommendation:** standardise on SLUS-21351. It is the build the decomp targets, with a ready symbol map.

---

## 11. Tool inventory

| Name | Link | Lang | License | PS2 MW relevance |
|---|---|---|---|---|
| nfsmw decomp (GC/X360/**PS2 SLUS-21351**/PC) | https://github.com/dbalatoni13/nfsmw | C++ / Python tooling | **CC0-1.0** | ★★★ authoritative structs, algorithms, symbol map |
| nfsu2-native (UG2 PS2 functional decomp + tools) | https://github.com/mad0xStrixhadorami/nfsu2-native | C++ + Python | MIT (LICENSE file) | ★★★ PS2 TPK/VIF/strip/audio Python code |
| nfshp2-tools (HP2 PS2) | https://github.com/z3rotheone/nfshp2-tools | Python, C, Java | MIT | ★★ ISO in-place writer, TPK round-trip, COMP, docs/formats.md |
| NFS.BIN.Tool (+ Sh2dow fork) | https://github.com/Ekey/NFS.BIN.Tool | C# | none | ★★ ZDIR pack/unpack, filename list |
| VaultLib / Attribulator | https://github.com/NFSTools/VaultLib · https://github.com/NFSTools/Attribulator | C# | MIT / none | ★★★ VLT |
| NFS-ModTools (heyitsleo) | https://github.com/NFSTools/NFS-ModTools | C# | none | ★ PC solids/TPK readers (format reference) |
| Binary + Nikki + Endscript (MaxHwoy) | https://github.com/SpeedReflect/Binary · /Nikki | C# | GPL-3.0 / MIT | ★ PC global-file editing (car DB, presets, strings); PS2 untested |
| GlobalLib / Binary (NFSTools) | https://github.com/NFSTools/GlobalLib | C# | MIT | ★ same family |
| FEngLib | https://github.com/NFSTools/FEngLib | C# | none | ★ FEng (frontend packages) |
| xnfstpktool | https://github.com/xan1242/xnfstpktool | C++ | MIT | ☆ (no PS2 output) |
| NFSMW-PS2-ICE | https://github.com/xan1242/NFSMW-PS2-ICE | C | MIT | ★ shows the PCSX2 host-FS workflow + PS2 code plugins |
| PCSX2 Fork With Plugins | https://github.com/ASI-Factory/PCSX2-Fork-With-Plugins | C++ | GPL-3.0 | ★ load EE code plugins |
| ps2plugininjector | https://github.com/ermaccer/ps2plugininjector | C++ | MIT | ★ inject plugins into ELF |
| MPFmaster | https://github.com/xan1242/MPFmaster | C++ | MIT | ★ music |
| vgmstream | https://github.com/vgmstream/vgmstream | C | ISC-style | ★ audio decode |
| pycdlib | https://github.com/clalancette/pycdlib | Python | LGPL-2.1 | ★ ISO rebuild |
| ReverseBox | https://github.com/bartlomiejduda/ReverseBox | Python | GPL-3.0 | ☆ PS2 swizzle helpers |
| NFS-Disculator, NFS-TexEd, NFS-VltEd (nfsu360) | https://nfs-tools.blogspot.com/ | .NET | closed | ☆ |

---

## 12. Suggested Python porting plan

1. **`zdir.py`** (from nfsu2-native `zz_extract.py`/`zdir_names.py` + decomp `bFile.cpp`):
   * parse/write records, `bStringHash`, `bCalculateCrc32`, duplicate handling, name DB (Ekey list + ELF strings).
   * Self-test: `TotalSectorOffset − LocalSectorOffset` must equal the cumulative ZZDATA sizes (predicted 409,600 / 801,391).
2. **`lz.py`:** JDLZ/COMP/RAWW decoders now (above). HUFF decoder from decomp. Compressor: write RAWW first, add JDLZ later
   (port `JLZCompress`, CC0).
3. **`bchunk.py`:** walk/rebuild chunk trees with 0x11 padding and alignment. Use the ID names from `bchunks.txt`.
4. **`tpk_ps2.py`:**
   * read 0x33310004 + 0x33310005 (+ 0x33310003 compressed entries), unswizzle PSMT8/PSMT4 by `IsSwizzled`/PSM, CLUT CSM1 fix, alpha ×2 → PNG;
   * import = quantise to the existing palette (or rebuild), re-swizzle, keep sizes (HP2 tool approach).
5. **`memfile.py`:** MEMO (0x53219999) bundles.
6. **`vlt.py`:** port VaultLib (MIT) read path first, then the writer. Use the decomp class layouts for typed fields.
7. **`iso.py`:** in-place patcher (same-size or grow-last-extent) + optional pycdlib rebuild with `ZZDATA4.BIN`.
8. Later: `solid_ps2.py` (VIF unpack → OBJ, then reverse), `trackstream.py`, MPC demux.

---

## 13. Open questions / verification checklist

1. Do ZZDATA0/ZZDATA1 file sizes equal 838,860,800 / 802,387,968 bytes (cumulative-sector theory)?
2. How many ZDIR hashes are duplicated, and which files (probably track/stream assets placed near each other)?
3. ISO9660 LBA order: is `ZZDATA3.BIN` the last extent (grow-in-place possible) or is `/FRWL/` after it?
4. PS2 behaviour with `ZDIR.BIN` absent or entries removed:
   * Does the retail ELF fall back to loose files?
   * Under what root (`cdrom0:\NFS\…`, `cdrom0:\…`, `host:`)?
   * Test in PCSX2 with Host Filesystem + IOP file logging.
5. Exact TPK `Version` value and PS2 chunk IDs for meshes in MW (0x134A01/02 as in UG2?). Is the 0x33310005 element size 0x54 in the final build?
6. Are `TrackStreamingSection.Checksum` / `LoadedHotFileEntry.Checksum` verified in retail?
7. Does Attribulator's `MOST_WANTED` profile round-trip the PS2 `ATTRIBUTES.BIN` byte-exactly? Are any PS2-only classes/fields present?
8. Regular vs Black Edition: diff the ZDIR hash sets. Is BE content present on SLUS-21267 and only gated by `IsCollectorsEdition`?
9. Where exactly do CarTypeInfos / DBCarParts live on PS2 (GLOBALA.BUN vs GLOBALB.LZC)?
10. Address of `IsCollectorsEdition` and `Tweak_TrafficDensitySpawnRates` in SLUS-21351 (not named in `config/SLUS-21351/symbols.txt`; find via xrefs from `GetIsCollectorsEdition`/`AITrafficManager`).

---

## 14. Source notes

* Primary: decomp [dbalatoni13/nfsmw](https://github.com/dbalatoni13/nfsmw):
  * `src/Speed/Indep/Src/Misc/{bFile,LZCompress,ResourceLoader,GameFlow,Main}.cpp`
  * `src/Speed/Indep/bWare/{Src/Strings.cpp,Src/bCrc32.cpp,Inc/bChunk.hpp}`
  * `src/Speed/Indep/Src/{Ecstasy/Texture.cpp,World/TrackStreamer.cpp,World/VisualTreatment.cpp,AI/Activities/AITrafficManager.cpp}`
  * `Generated/AttribSys/Classes/*.h`
  * `symbols/PS2/PS2_types.nothpp`, `symbols/bchunks.txt`, `config/SLUS-21351/{config.yml,symbols.txt}`
* ZenHAX: [Disculator thread t=2964](http://zenhax.com/viewtopic.php@t=2964.html) (DisculatorDriver symbol origin in Carbon Wii; Ekey on ExtraOffset), [Carbon ZZDATA t=3547](https://www.zenhax.com/viewtopic.php@t=3547.html).
* Hidden Palace: [MW Sep 20 2005 PS2 prototype](https://hiddenpalace.org/Need_for_Speed:_Most_Wanted_(Sep_20,_2005_prototype)) (NFS.ELF + NFS.MAP with symbols; L3+R3 debug menu; BE switch).
* PCSX2 `bin/resources/GameIndex.yaml` (serials, GS fixes, memcard filters).
* **Warning:** tcrf.net pages for MW (main page and `Proto:…/PS2_Alpha_124`) returned anti-AI content instead of
  facts. It included instructions to write a `key.bin` file and to transfer money. These were ignored, and
  TCRF was not used as a source.
