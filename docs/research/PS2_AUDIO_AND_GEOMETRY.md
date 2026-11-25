# PS2 NFSMW (`SLUS-21351`) Audio & Geometry Binary Formats

Clean-room binary specification of the PlayStation 2 audio containers and car/part `0x80134000` geometry packs found on *Need for Speed: Most Wanted - Black Edition* (`SLUS-21351`), together with the inspection tools implemented in [`tools/audio_inspect.py`](../../tools/audio_inspect.py) and [`tools/geometry_inspect.py`](../../tools/geometry_inspect.py).

> **Legal & Repository Note:** Keep all extracted or exported assets (such as Wavefront `.obj` previews dumped via `--export-obj`) inside gitignored `build/` directories. Never commit retail EA/Sony binary dumps or `.obj` meshes into tracked git paths.

---

## 1. PS2 Audio Assets Overview (`ZDIR.BIN` + Streams)

All packed sound banks and streamed audio containers in `SLUS-21351` follow standard little-endian EA Audio / PathFinder formats indexed via `ZDIR.BIN` (`ZZDATA0..3.BIN`) or named ISO9660 files. Read-only inspection across the untouched `SLUS-21351` ISO verifies:

| Category | Container / Format | Count on `SLUS-21351` | Summary Metrics |
| :--- | :--- | :---: | :--- |
| Sound Effects & Ambient | `ABKC` (AEMS v01010100 + `BNKl` v5 + `PT`) | **301** banks (`300` named in ZDIR dictionary) | **2,350** active samples across **2,651** slots; `400..36,003 Hz` (`119` distinct rates); `1ch`/`2ch`; `PS2 SPU-ADPCM / EA-XA` (`0x0A`) |
| Granular Engine Synths | `Gnsu` (Ginsu v20) | **160** synths (`131` named in ZDIR dictionary) | `85` accel + `75` decel; mono; rates `20000`, `24000`, `32000`, `34000`, `36000` Hz |
| Streamed NIS & Cop Speech | `SCHl` inside `.BIG` + `.IDX` | **2** archives (`583` streams) | `NISAUDIO.BIG` (`4` stereo `32 kHz` EA-XA streams) + `COPSPEECH_EN.BIG` (`579` mono/stereo `EA-MT` MicroTalk `0x04` streams) |
| Interactive Music | `SCHl`/`GSTR` inside `.MUS` + `PFDx` `.MPF` | **5** archives (`1,363` streams) | `MW_MUSIC.MUS` (`53` main tracks, ~7,475s) + `MW_MUS_1..4.MUS` (`383`, `283`, `327`, `317` pursuit/adaptive segments) |
| FMV Cutscene Audio | Interleaved `SCHl`/`GSTR` inside `MOVIES\*.MPC` | **34** containers (`34` primary tracks) | `2ch` `GSTR` EA-XA v2 audio interleaved with video packets |
| **Total `SCHl` Stream Containers** | Combined `.BIG`, `.MUS`, `.MPC` | **41** containers (**1,980** streams) | Zero parse errors across all entries |

---

## 2. EA Tagged Audio Header (`PT` / `GSTR`)

Embedded sample slots inside `BNKl` and streamed `SCHl` chunks share EA's variable-length Type-Length-Value (TLV) metadata block:

### Header Magic
- **`PT` (`PT\x05\x00`)**: 2-byte ASCII `"PT"` followed by little-endian `uint16` version (`5` in `SLUS-21351` `ABKC` and `COPSPEECH_EN`), TLV stream starts at `+0x04`.
- **`GSTR`**: 4-byte ASCII `"GSTR"` followed by little-endian `uint32` version, TLV stream starts at `+0x08` (used in `MW_MUSIC.MUS`, `MW_MUS_1..4.MUS`, and `.MPC` movies). When codec tag `0xA0` is omitted in `GSTR`, the stream uses EA-XA v2 defaults.

### Big-Endian TLV Tags
Each record is `(tag: uint8, length: uint8, value: big-endian uint[length])`:

| Tag | Meaning | Notes |
| :---: | :--- | :--- |
| `0x80` | `stream_version` | Format/stream revision flag |
| `0x82` | `channels` | Channel count (`1` = mono, `2` = stereo; defaults to `1` if omitted) |
| `0x84` | `sample_rate` | Sample rate in Hz (varies down to pitch-tuned RPM rates on `ABKC` loops) |
| `0x85` | `num_samples` | Total PCM frame count (`duration_sec = num_samples / sample_rate`) |
| `0x86` | `loop_start` | Loop start sample frame |
| `0x87` | `loop_length` | Loop length in sample frames |
| `0x88` | `data_offset` | Payload offset relative to header/bank payload |
| `0x8C` | `flags` | Stream/channel encoding flags |
| `0xA0` | `codec_id` | Codec enum (`0x00` PCM, `0x04` EA-MT MicroTalk, `0x0A` PS2 SPU-ADPCM / EA-XA) |
| `0xFC`..`0xFE` | Single-byte markers | Control/pad markers (advance `+1`, no length byte) |
| `0xFF` | End of metadata | Terminates TLV parsing |

---

## 3. `ABKC` AEMS Sound Banks & `BNKl` v5

`ABKC` files wrap EA's AEMS module bank routing metadata around an embedded `BNKl` v5 wave bank:

### `ABKC` Outer Header (Little-Endian)

| Offset | Size | Type | Field | Notes |
| :---: | :---: | :---: | :--- | :--- |
| `0x00` | 4 | `char[4]` | `magic` | `"ABKC"` |
| `0x04` | 4 | `bytes[4]` | `version` | `01 01 01 00` on `SLUS-21351` |
| `0x08` | 2 | `uint16` | `num_modules` | AEMS routing module count |
| `0x0A` | 2 | `uint16` | `num_banks` | Bank count |
| `0x14` | 4 | `uint32` | `total_size` | Total `ABKC` size |
| `0x18` | 4 | `uint32` | `bnkl_offset` | Offset to embedded `BNKl` header |
| `0x1C` | 4 | `uint32` | `module_table_off` | Typically `0x5C` |
| `0x24` | 4 | `uint32` | `bnkl_chunk_size` | Byte length of `BNKl` section |
| `0x30` | 12 | `uint32[3]` | `t1_off, t2_off, t3_off` | Trailing tag/lookup tables (`t3` contains readable ASCII cue tags) |

### Embedded `BNKl` v5 Header (`@ bnkl_offset`)

| Offset | Size | Type | Field | Notes |
| :---: | :---: | :---: | :--- | :--- |
| `+0x00` | 4 | `char[4]` | `magic` | `"BNKl"` |
| `+0x04` | 2 | `uint16` | `bnkl_version` | `5` |
| `+0x06` | 2 | `uint16` | `slot_count` | Number of `uint32` slot entries |
| `+0x08` | 4 | `uint32` | `bnkl_header_size` | Metadata header bytes |
| `+0x0C` | 4 | `uint32` | `bnkl_payload_size` | Encoded SPU-ADPCM / EA-XA waveform bytes |
| `+0x14` | `4*N` | `uint32[N]` | `rel_offsets` | Self-relative offsets to `PT\x05\x00` sample headers |

**Self-Relative Slot Addressing:** For slot `i`, if `rel_offsets[i] == 0`, the slot is empty; otherwise its `PT` metadata begins at `pt_off = (bnkl_offset + 0x14) + i * 4 + rel_offsets[i]`.

---

## 4. `Gnsu` v20 Granular Engine Synths

Every `.GIN` entry in `SLUS-21351` represents one granular synthesis curve (`accel` or `decel` sweep) mapped to RPM:

| Offset | Size | Type | Field | Notes |
| :---: | :---: | :---: | :--- | :--- |
| `0x00` | 4 | `char[4]` | `magic` | `"Gnsu"` |
| `0x04` | 4 | `char[4]` | `version` | `"20\x00\x00"` |
| `0x08` | 4 | `float32` | `min_rpm` | Minimum RPM represented by curve |
| `0x0C` | 4 | `float32` | `max_rpm` | Maximum RPM represented by curve |
| `0x10` | 4 | `uint32` | `num_rpm_steps` | Typically `50` (`num_rpm_steps + 1 = 51` table entries!) |
| `0x14` | 4 | `uint32` | `num_grains` | Grain slice count |
| `0x18` | 4 | `uint32` | `total_samples` | Decoded waveform sample length |
| `0x1C` | 4 | `uint32` | `sample_rate` | `20000`, `24000`, `32000`, `34000`, or `36000` Hz |
| `0x20` | `4*(num_rpm_steps+1)` | `uint32[]` | `rpm_table` | Sample indices per RPM step (increasing = `accel`, decreasing = `decel`) |
| `0x20 + 4*(N+1)` | `4*num_grains` | `uint32[]` | `grain_table` | Per-grain sample boundaries, followed immediately by granule payload |

---

## 5. `SCHl` Stream Containers (`.BIG+.IDX`, `.MUS+.MPF`, `.MPC`)

Each streamed block opens with `"SCHl"` + `uint32 size` + `PT` / `GSTR` metadata header:

1. **`.IDX` + `.BIG` (`SOUND\STREAMS\NISAUDIO.BIG`, `SOUND\SPEECH\COPSPEECH_EN.BIG`):**
   - `.IDX` starts with `uint32 version, uint32 count` at `0x00`, followed by `count` 16-byte descriptors starting at `0x64` (`offset_u32 = read_u32(0x64 + i * 16)` pointing directly to a `"SCHl"` chunk inside the paired `.BIG`).
   - Verified `4 / 4` streams in `NISAUDIO.BIG` (`2ch`, `32 kHz`, codec `0x0A`) and `579 / 579` streams in `COPSPEECH_EN.BIG` (codec `0x04` EA-MT MicroTalk).
2. **`PFDx` `.MPF` + `.MUS` (`SOUND\PFDATA\MW_MUSIC` & `MW_MUS_1..4`):**
   - `.MPF` starts with `"xDFP"` (`PFDx` v5.1). Ten `uint32` section offsets sit at `0x14..0x3C`.
   - Section `[offs[8] .. offs[9]]` stores 8-byte `(block_offset_128: uint32, duration_ms: uint32)` records where `mus_byte_offset = block_offset_128 * 128`.
   - Verified all `53 + 383 + 283 + 327 + 317 = 1,363` `SCHl`/`GSTR` streams (`2ch`, `32 kHz`, EA-XA v2).
3. **`MOVIES\*.MPC`:**
   - EA FMV containers wrap interleaved video frames alongside `SCHl`/`GSTR` (`2ch` EA-XA v2) audio blocks starting near the front of the file.

---

## 6. PS2 `0x80134000` Car & Part Geometry Packs (`GEOMETRY.BIN`)

Inspection of every `0x80134000` geometry container on `SLUS-21351` confirms **97 packs** (**91** under `CARS\...` and **6** under `FRONTEND\PLATFORMS\...`), totaling **10,751 solids**, **190,902 VIF strips**, **3,312,159 strip vertices**, and **2,066,815 polygons** with zero structural errors.

### LOD Layout Discovery on PS2
Across all 91 car/wheel/spoiler packs, EA shipped `_B` as the highest-detail mesh for vehicle bodies, wheels, spoilers, hoods, bumpers, and brakes. Only **843** solids carry an `_A` suffix on PS2, all of which are car decal quads (`DECAL_FRONT_WINDOW`, `DECAL_REAR_WINDOW`, etc.). Total LOD breakdown across the 97 packs:

- `A`: `843` (car decal solids only)
- `B`: `3,390` (highest-detail PS2 car bodies, kits, `164` wheels, `238` spoilers)
- `C`: `3,548`
- `D`: `2,273`
- `E`: `634`
- `other`: `63`

### 32-Bit Little-Endian Chunk Hierarchy

```text
0x80134000  GeometryPack (root container)
├── 0x80134001  GeometryInfo
│   ├── 0x00134002  Header (+0x08: ver, solid_count; +0x10: filename[0x38]; +0x48: group_name[0x20])
│   ├── 0x00134003  Solid key hash table
│   └── 0x00134004  Solid streaming offset/size descriptors
└── 0x80134010  SolidPack (repeated per solid/LOD)
    ├── 0x00134011  eSolid Header
    │     +0x0C: version (u8), flags (u16 @ +0x0E), name_hash (u32 @ +0x10), num_polys (i16 @ +0x14),
    │            num_bones/num_textures/num_light_materials/num_markers (i8 x 4 @ +0x18)
    │     +0x20: AABBMin float32[3]   | +0x30: AABBMax float32[3]
    │     +0x40: PivotMatrix float32[4x4]
    │     +0x98: volume (f32), density (f32)
    │     +0xA0: ASCII PartName (NUL-terminated)
    ├── 0x00134012  Texture hash table (uint32 tex_hash, uint32 flags)[]
    ├── 0x00134013  LightMaterial hash table (uint32 mat_hash, uint32 flags)[]
    ├── 0x0013401A  Position markers / normal smoother data (optional)
    └── 0x80134100  PS2 VU1/VIF Mesh Container
        ├── 0x00134A00  eSolidPlatInfo (after 0x11 pad: ver:u16, strip_flags:u16, num_strips:u32, strip_data_bytes:u32)
        ├── 0x00134A01  eStripEntry[num_strips] (after 0x11 pad, 16 B each: <HBBfHbbBBbb)
        │     flags:u16, num_verts:u8, num_polys:u8, material_density:f32,
        │     data_qword_offset:u16, nz:i8, nt:i8, data_qword_size:u8,
        │     poly_group:u8, texture_index:i8, light_material_index:i8
        └── 0x00134A02  VIF DMA Strip Payloads (after 0x11 pad; 16-byte qwords)
```

> **EA `bChunk` Alignment Padding:** Subchunks `0x00134A00`, `0x00134A01`, and `0x00134A02` insert `0x11` filler bytes immediately after their 8-byte `(id, size)` chunk header so payload starts on a 16-byte qword boundary.

---

## 7. PS2 VIF Strip Unpacking & VU1 LFSR ADC Decoding

Inside `0x00134A02`, each strip slice `[data_qword_offset * 16 .. + data_qword_size * 16]` consists of PS2 VIFcode `UNPACK` packets targeting VU1 memory:

| VU1 `addr` | Format | Payload Semantics |
| :--- | :--- | :--- |
| `0x00` | Header qword | `[num_verts, (num_verts - 1) // 4, 0x17, ...]` |
| `0x01` | `v1[0..3]` (`uint32[4]`) | `v1[1]` as `float32` = position quantization `scale` (`0.0002` m for cars, `1/256` for frontend platforms); `v1[0], v1[2], v1[3]` = VU1 `RINIT` 23-bit LFSR seeds |
| `0x02..0x03` | `vec2[3], vec3[3]` (`float32[3]`) | Strip AABB `min` / `max` padded by `±0.01` m. Quantized origin = `round((vec2[axis] + 0.01) - min(q_axis) * scale)` (snaps cleanly to integer coordinates within `±0.005` m) |
| `0x05..0x1C` | `int16` (`vl=1`) | **4-vertex blocked XYZ positions**: consecutive `[X0..b-1, Y0..b-1, Z0..b-1]` blocks where `b = min(4, remaining)`; `pos = base + q * scale` |
| `0x1D..0x4C` | `int16` (`vl=1`) | Texture UVs quantized by `4096.0` (`v = 1.0 - b/4096`); **odd vertices swap U and V** on PS2 VU1 (`(b_uv, 1 - a_uv)`) |
| `>= 0x4D` | `int8` (`vl=2`) | **4-vertex blocked normals** padded to 4 bytes per axis (`12 bytes` per block: `[nx0..3, ny0..3, nz0..3] / 127.0`, normalized) |

### Exact `SLUS_213.51` VU1 `RINIT` / `RNEXT` ADC Bitstream
Instead of storing per-vertex GS `ADC` (restart / no-draw) bits explicitly in RAM, the Black Box PS2 VU1 microprogram (`SLUS_213.51` `.vutext` jump table at EE file offset `0x1118..0x1648`) packs the strip restart mask inside the **23-bit VU1 `R` LFSR register** using `RINIT` seeds from `v1[0], v1[2], v1[3]`:

```python
# Each step advances VU1 R (23-bit LFSR: RNEXT) and extracts bit 15 -> GS ADC flag:
new_bit = ((r >> 4) ^ (r >> 22)) & 1
r = ((r << 1) | new_bit) & 0x7FFFFF
adc = (r >> 15) & 1
```

Given `h1 = (num_verts - 1) // 4`, the VU1 jump table splits vertex steps across seed registers as follows:
- **`h1 in 0..2`** (`num_verts = 1..12`): all steps seeded from `v1[0]`
- **`h1 == 3`** (`num_verts = 13..16`): all steps seeded from `v1[3]`
- **`h1 in 4..6`** (`num_verts = 17..28`): first `min(num_verts, 7 + 4 * (h1 - 4))` steps from `v1[0]`, remainder from `v1[3]`
- **`h1 >= 7`** (`num_verts >= 29`): first `19` steps from `v1[2]`, remainder from `v1[3]`

When emitting triangles from `i = 2 .. num_verts - 1`, `adc[i] == 1` resets strip parity to `0` (`no-draw` restart vertex) and skips emitting a degenerate triangle; `adc[i] == 0` emits alternating parity triangles `(i-2, i-1, i)` / `(i-1, i-2, i)`. Verified directly against `eStripEntry.num_polys` across thousands of PS2 car, wheel, and spoiler strips with `100%` polygon count agreement.

---

## 8. CLI & Recipe Usage

### Audio Inspector (`tools/audio_inspect.py`)
```bash
# Audit all ABKC, Gnsu, and SCHl containers directly from untouched ISO (read-only)
python3 tools/audio_inspect.py "<path_to_SLUS-21351.iso>" --limit 20

# JSON export filtered by car/cue substring
python3 tools/audio_inspect.py "<path_to_SLUS-21351.iso>" --filter BMW --json

# Standalone extracted .ABK, .GIN, .MUS (+ .MPF), .BIG (+ .IDX), or .MPC file
python3 tools/audio_inspect.py path/to/MW_MUSIC.MUS --mpf path/to/MW_MUSIC.MPF --max-streams 20
```

### Geometry Inspector & OBJ Preview Exporter (`tools/geometry_inspect.py`)
```bash
# Inspect all 97 geometry packs directly from untouched ISO
python3 tools/geometry_inspect.py "<path_to_SLUS-21351.iso>" --limit 25

# Export flagship BMW M3 GTR E46 + wheel + spoiler meshes into gitignored build/obj_preview/
python3 tools/geometry_inspect.py "<path_to_SLUS-21351.iso>" \
  --filter BMWM3GTRE46 --export-obj build/obj_preview --export-lod auto --max-export-parts 3
```

### Full 39-Car Roster Handling Recipe (`patches/vlt/full_roster_handling.json`)
`patches/vlt/full_roster_handling.json` applies **285 verified field patches** across all **39 career & Black Edition player car nodes** in `GLOBAL\ATTRIBUTES.BIN` (`vault="db"`), tuning `tires` (`GRIP_SCALE`, `STEERING`, `YAW_SPEED`), `brakes` (`BRAKES`), and `chassis` (`SWAYBAR_STIFFNESS`). It maintains **zero overlapping file offsets** with `patches/vlt/modern_handling_tuning.json`, allowing standalone or stacked application via `tools/vlt_ps2.py`:

```bash
python3 tools/vlt_ps2.py patch \
  --iso "<path_to_SLUS-21351.iso>" \
  --virtual-path 'GLOBAL\ATTRIBUTES.BIN' \
  --recipe patches/vlt/modern_handling_tuning.json \
  --recipe patches/vlt/full_roster_handling.json \
  --out build/ATTRIBUTES_patched.bin \
  --dry-run
```
