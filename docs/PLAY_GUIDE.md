# NFSMW: Rockport 2026 — PS2 / PCSX2 Player's Guide (`SLUS-21351`)

Play the **real PlayStation 2 *Need for Speed: Most Wanted – Black Edition* (`SLUS-21351`)** inside **PCSX2** with non-destructive ISO patches, verified `.pnach` visual/display enhancements, upgraded lighting, optional VU1 car geometry, and BC7 texture replacements — preserving the original story, physics, FMVs, and transferable `.ps2` memory-card saves (`BASLUS-21351`).

> Looking for milestone status or engineering plans? See [PROGRESS.md](PROGRESS.md) and [ROADMAP.md](ROADMAP.md).
>
> *Note: Our earlier experimental standalone Apple Silicon Metal RT prototype has been removed from `main` and archived separately on branch [`archive/standalone-metal-rt-v1.4`](https://github.com/yash0530/nfsmw-ps2-remaster/tree/archive/standalone-metal-rt-v1.4).*

---

## 1. Requirements & "Bring Your Own ISO" Policy

- **Emulator**: PCSX2 v2.6.3+ (macOS Metal / Vulkan / Direct3D12) with your own legally dumped PS2 BIOS.
- **Game Disc**: Your own legal copy of *Need for Speed: Most Wanted – Black Edition* (NTSC-U, `SLUS-21351`, stock ELF CRC `0518D274`). All tools open your original ISO strictly read-only (`'rb'`) and never modify it.
- **Toolchain**: Python 3.11+ (`Pillow`, `numpy`) and Rust (`cargo`) for the pure-Rust BC7 DDS texture encoder (`tools/dds_encoder_rs/`).

```bash
export NFSMW_ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
```

*(Zero copyrighted Electronic Arts or Sony assets are distributed in this repository. See [ASSETS_LICENSE.md](../ASSETS_LICENSE.md). `SLUS-21351` decompilation symbol names are credited to [`dbalatoni13/nfsmw`](https://github.com/dbalatoni13/nfsmw).)*

---

## 2. Quick Start: Build Modded ISO & Install Per-Game PCSX2 Profile

```bash
# Build a non-destructive modded ISO into build/ and install per-game PCSX2 settings + companion .pnach
make build-v0.5 ISO="$NFSMW_ISO"
```

Or run the CLI directly with custom renderer/scaling flags:

```bash
python3 tools/rockport_cli.py build \
  --profile rockport2026_v0.5 \
  --source "$NFSMW_ISO" \
  --install-pcsx2 \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```

- **Global PCSX2 safety**: `setup_pcsx2_macos.py` writes only per-game `gamesettings/SLUS-21351_<CRC>.ini` and `patches/SLUS-21351_<CRC>.pnach` files — it never overwrites your global `PCSX2.ini` or existing `.ps2` memory cards.
- **Save compatibility**: `SYSTEM.CNF` (`SLUS_213.51`) stays intact, so standard `BASLUS-21351` saves on `Mcd001.ps2` remain transferable between stock and modded ISOs.

---

## 3. Useful Make Commands

| Command | What It Does |
| :--- | :--- |
| `make status` | Inspect source ISO (read-only), built ISOs, installed PCSX2 profiles, and tool readiness |
| `make profiles` | Validate and list all declarative build profiles in `patches/profiles/` |
| `make build-v0.5` | Build the `rockport2026_v0.5` ISO non-destructively and install per-game PCSX2 settings |
| `make setup-pcsx2` | Install per-game 4K Metal PCSX2 settings & `.pnach` for stock `SLUS-21351` (`0518D274`) |
| `make enhance-world` | Enhance dumped PCSX2 textures to BC7 DDS (preserving PS2 GS `0x80` alpha) |
| `make build-dds` | Build the native Rust `rockport_dds` BC7 encoder (`tools/dds_encoder_rs/`) |
| `make verify` | Run `py_compile`, hygiene audit, `.pnach` linter, profile checks, and offline tests |
| `make test` | Run the full `unittest` suite (`NFSMW_ISO=/nonexistent` for offline CI mode) |

---

## 4. Using `.pnach` Patches Without Building a Modded ISO

If you prefer playing your unmodified retail ISO (`CRC: 0518D274`), copy [`patches/pnach/SLUS-21351_0518D274.pnach`](../patches/pnach/SLUS-21351_0518D274.pnach) into PCSX2's `patches/` folder (or run `make setup-pcsx2`) and toggle individual sections under **Game Properties → Patches**:
- Enable only **one** aspect-ratio section at a time (`[Widescreen 16:9]`, `[Ultrawide 21:9]`, or `[Wider Camera FOV +12%]`).
- For 60 FPS gameplay on capable hardware, set **EE Cycle Rate** to `180%–300%` (`EECycleRate = 1` or `2`) and optionally enable `[60 FPS Companion: Fix ScreenEFX 30Hz Timestep (Experimental)]`.
