# PS2 NFSMW (`SLUS-21351`) Geometry Repacking & VU1 GF(2) LFSR Specification

Clean-room specification of the PlayStation 2 `0x80134000` `GEOMETRY.BIN` encoder, `replace-solid` repacker, exact GF(2) VU1 `RINIT`/`RNEXT` ADC seed solver, and crease-aware car mesh smoother implemented in:

- [`tools/geometry_compiler.py`](../../tools/geometry_compiler.py)
- [`tools/subdivide_car_mesh.py`](../../tools/subdivide_car_mesh.py)
- [`tools/geometry_inspect.py`](../../tools/geometry_inspect.py)

> **Safety & Legal Note:** Keep all extracted `.BIN` files, `.obj` meshes, and test ISOs inside gitignored `build/` directories. Always open retail source ISOs strictly read-only (`'rb'`).

---

## 1. `0x80134000` Container Layout & Alignment Invariants

Verified across all **91** `CARS\*` packs and **6** `FRONTEND\PLATFORMS\*` packs on `SLUS-21351`:

```text
0x80134000  GeometryPack (root chunk; payload spans up to last SolidPack)
├── 0x00000000  Alignment padding chunk (8 B) -> aligns GeometryInfo to +0x10
├── 0x80134001  GeometryInfo
│   ├── 0x00134002  GeometryHeader (144 B)
│   │     +0x08: pack_version (u32, 0x1D), solid_count (u32)
│   │     +0x10: filename (char[0x38]) | +0x48: group_name (char[0x20])
│   │     +0x68: total_file_bytes (u32, equals len(file) on all 91 CARS\* packs)
│   ├── 0x00134003  Sorted solid key table: (name_hash: u32, 0: u32)[solid_count]
│   └── 0x00134004  Streaming solid descriptor table (present on all 91 CARS\* packs)
│         24 B per solid sorted by name_hash:
│         (name_hash: u32, chunk_offset: u32, total_bytes: u32, total_bytes_dup: u32, 0: u32, 0: u32)
└── [0x00000000 pad -> 64-byte aligned] 0x80134010  SolidPack (repeated per solid)
    ├── 0x00134011  eSolid Header
    │     +0x0C: version (u8), flags (u16 @ +0x0E), name_hash (u32 @ +0x10),
    │            num_polys (i16 @ +0x14), num_bones/num_tex/num_mat/num_markers (i8 x 4 @ +0x18)
    │     +0x20: AABBMin (f32[3]) | +0x30: AABBMax (f32[3])
    │     +0x40: PivotMatrix (f32[4x4]) | +0x98: volume, density (f32[2])
    │     +0xA0: NUL-terminated ASCII name padded to 4-byte boundary
    ├── 0x00134012  Texture hash table: (tex_hash: u32, flags: u32)[num_tex]
    ├── 0x00134013  LightMaterial hash table: (mat_hash: u32, flags: u32)[num_mat]
    ├── 0x0013401A  Position markers (optional; 0x11-padded to 16-byte alignment, 80 B/marker)
    └── 0x80134100  PS2 VU1/VIF Mesh Container
        ├── 0x00134A00  eSolidPlatInfo (0x11-padded to 16 B):
        │     plat_version (u16=32), strip_flags (u16), num_strips (u32), strip_data_bytes (u32), pad (8 B)
        ├── 0x00134A01  eStripEntry[num_strips] (0x11-padded to 16 B; 16 B each: <HBBfHbbBBbb)
        └── 0x00134A02  VIF DMA Strip Buffer (0x11-padded to 16 B; 16-byte qwords)
```

Key alignment rules enforced by `tools/geometry_compiler.py`:
- Every `0x80134010` `SolidPack` begins on a **64-byte (`0x40`)** boundary, padded via `0x00000000` filler chunks (if remaining bytes `< 8`, pad `rem + 64`).
- Subchunks `0x0013401A`, `0x00134A00`, `0x00134A01`, and `0x00134A02` insert `0x11` bytes immediately after their 8-byte `(id, size)` chunk header so payload starts on a 16-byte qword boundary.
- When replacing a solid whose vertex/strip topology changed, stale damage/morph tables (`0x00134017`, `0x00134018`, `0x00134019`) are omitted cleanly (matching the 5,504 retail solids without damage morph tables).
- For `CARS\*` packs, trailing `0x00000000` padding aligns file size to 128 bytes and `0x00134002 + 0x68` is updated to the new total file byte length, alongside every `(chunk_offset, total_bytes)` entry in `0x00134004`.

---

## 2. PS2 VIF Strip DMA Packet Format

Across all **190,902** retail strips in `SLUS-21351`, `num_verts` is in **`[3, 30]`**, quantization scale is `0.0002` m (cars) or `1/256` m (frontend), and every strip ends with `MSCAL 0x0` (`0x14000000`) + `NOP`s to a 16-byte qword boundary:

| Sequence | VIF Command | VU1 `addr` | Payload |
| :---: | :--- | :---: | :--- |
| 1 | `0x6E` (`V4-8`, `num=1`, `flg=1`) | `0x000` | `[nv, (nv - 1) // 4, 0x17, 0x04]` |
| 2 | `0x6C` (`V4-32`, `num=1`, `flg=1, usn=1`) | `0x001` | `v1[0..3]`: `(seed0, float32(scale), seed2, seed3)` |
| 3 | `0x68` (`V3-32`, `num=2`, `flg=1, usn=1`) | `0x002` | `vec2 = base + min_q * scale - 0.01`, `vec3 = base + max_q * scale + 0.01` |
| 4 | `0x6C` (`V4-32`, `num=1`, `flg=1, usn=1`) | `0x004` | Normal culling cone `(0, 0, 0, 0)` (disabled so no faces are over-culled) |
| 5 | `0x6D` (`V4-16`) + tail `0x61/0x65/0x69` | `0x005+` | 4-vertex blocked `int16` positions `[X0..b-1, Y0..b-1, Z0..b-1]` |
| 6 | `0x6D` (`V4-16`, `num=2*(nv//4)` + tail `1/2`) | `0x01D+` | `int16` UVs scaled by `4096.0`, `v = 1 - y`, **odd vertices U/V swapped** |
| 7 | `0x6E` (`V4-8`, `num=3*ceil(nv/4)`) | `0x04D+` | 4-vertex blocked `int8` normals (`round(n * 127)`), 4 bytes/axis/block |
| 8 | `0x14000000` + `0x00000000` * k | — | `MSCAL 0x0` + `NOP` padding to 16-byte qword boundary |

---

## 3. Exact GF(2) Linear Solver for VU1 `RINIT` / `RNEXT` ADC Bits

The Sony PS2 VU1 random/LFSR instruction `RNEXT` advances a 23-bit state $r \in \mathbb{F}_2^{23}$ via:

$$r'_{0} = r_{4} \oplus r_{22}, \qquad r'_{k} = r_{k-1} \quad (1 \le k \le 22)$$

and the Black Box VU1 microprogram (`SLUS_213.51` `.vutext` jump table) reads the GS `ADC` (no-draw restart) bit from bit 15 of $r$ after each `RNEXT` step:

$$y_t(r_0) = \text{bit}_{15}\!\left(T^t(r_0)\right), \qquad t = 1, 2, \dots, m.$$

Because $T: \mathbb{F}_2^{23} \to \mathbb{F}_2^{23}$ and projection onto bit 15 are linear over $\mathbb{F}_2$, the map $r_0 \mapsto (y_1, \dots, y_m)$ is an $m \times 23$ matrix $A$ over $\mathbb{F}_2$:
- Each column $j \in \{0, \dots, 22\}$ is obtained by simulating $t = 1 \dots m$ steps from basis seed $r_0 = 2^j$.
- Direct rank computation confirms $\operatorname{rank}_{\mathbb{F}_2}(A_{23 \times 23}) = 23$ (**full rank**).
- Therefore, **every** binary ADC vector of length $m \le 23$ has a **unique exact** 23-bit `RINIT` seed solved in microseconds via Gauss-Jordan elimination over $\mathbb{F}_2$.

Because `decode_vu_adc_bits` splits a strip of length `nv` across `(v1[0], v1[2], v1[3])` based on `h1 = (nv - 1) // 4`:
- `h1 <= 2` (`nv <= 12`): 1 segment of length $\le 12 \le 23$ in `v1[0]`.
- `h1 == 3` (`13 <= nv <= 16`): 1 segment of length $\le 16 \le 23$ in `v1[3]`.
- `4 <= h1 <= 6` (`17 <= nv <= 28`): 2 segments (`<= 15` in `v1[0]`, `<= 13` in `v1[3]`).
- `h1 >= 7` (`nv >= 29`): 2 segments (`19` in `v1[2]`, `nv - 19` in `v1[3]`).

For all VU1 strip lengths `nv <= 42` (including `MAX_STRIP_VERTS = 30`), both segments have length $\le 23$, so **every** possible restart bitvector `[1, 1, 0, ..., 1, 1, 0, ...]` round-trips bit-for-bit through `geometry_inspect.decode_vu_adc_bits()`.

---

## 4. Crease-Aware Car Mesh Smoother & BMW M3 GTR E46 Verification

`tools/subdivide_car_mesh.py` implements 1-to-4 crease-aware **PN-Triangle** (`--mode pn`, default) and **Loop** (`--mode loop`) subdivision:
1. Welds strip/UV-split vertices on a `0.2 mm` (`5000.0` inv-eps) spatial grid to recover true surface manifold adjacency.
2. Marks an edge as **sharp / crease** when it is a boundary/non-manifold edge or its dihedral angle exceeds `--crease-deg` (default `38.0°`).
3. Splits vertex normals per smooth fan bounded by sharp edges so panel gaps, door seams, fender lips, and window borders stay crisp while curved panels smooth seamlessly across material zones without cracking.
4. Interpolates UVs per triangle corner and clamps positions to the original part AABB (`preserve_aabb=True`) so adjacent solids (`FRONT_WINDOW_B`, `LEFT_HEADLIGHT_B`, `DAMAGE0_*_B`, `DECAL_*_A`) remain flush.

### Smoke Test Results (`BMWM3GTRE46_KIT00_BODY_B`)
- **Input:** `3,640` tris, `5,176` strip verts, `243` strips (`181,304` B SolidPack; `626,816` B pack)
- **Subdivided (`pn`, `L=1`, `crease=38.0°`):** `14,560` tris (`11,453` unique corners; `3,759` smooth / `976` sharp / `1,448` boundary edges)
- **Repacked (`tools/geometry_compiler.py`):** `771` strips, `19,716` strip verts, `340,784` B VIF, `353,920` B SolidPack (below retail's `373,360` B largest SolidPack), `789,504` B total `GEOMETRY.BIN`
- **Verification:** `0` errors in `tools/geometry_inspect.py`; `72/72` untouched solids byte-identical; `73/73` `0x00134004` streaming offsets exact; temp ISO injection via `tools/build_modded_iso.py` (`ZZDATA3.BIN` append `+386` sectors) verified with `zdir_tool.py verify-checksums` (`1 passed, 0 failed`).
