# NFSMW: Rockport 2026 — Open-Source PS2 / PCSX2 Remaster Toolkit (`SLUS-21351`)

> **Open-source, non-destructive PS2/PCSX2 modding tools for remastering the real PlayStation 2 *Need for Speed: Most Wanted – Black Edition* (`SLUS-21351`) inside PCSX2 — zero copyrighted EA game assets distributed.**

[![License: MIT](https://img.shields.io/badge/Code-MIT-blue.svg)](LICENSE)
[![Assets: CC0 / OFL](https://img.shields.io/badge/Assets-CC0%20%2F%20SIL%20OFL-green.svg)](ASSETS_LICENSE.md)
[![Target: SLUS-21351](https://img.shields.io/badge/PS2%20Target-SLUS--21351%20Black%20Edition-orange.svg)](docs/research/PS2_ARCHITECTURE.md)
[![CI](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml/badge.svg)](https://github.com/yash0530/nfsmw-ps2-remaster/actions/workflows/ci.yml)

**Quick Links:** [Player's Guide (`docs/PLAY_GUIDE.md`)](docs/PLAY_GUIDE.md) · [Progress Table (`docs/PROGRESS.md`)](docs/PROGRESS.md) · [Engineering Roadmap (`docs/ROADMAP.md`)](docs/ROADMAP.md)

---

## Why This Exists

Most *Need for Speed: Most Wanted (2005)* modding tools (`NFS-VltEd`, `OGVI`, `NFS-CarToolkit`, `Extra Options`) target the **Windows PC** release and do not handle PlayStation 2 archives (`ZDIR.BIN`/`ZZDATA0..3.BIN`, `JDLZ`, `VPAK` VLT, PS2 `GEOMETRY.BIN` VU1 packets, `GLOBALMEMORYFILE.BIN`).

**NFSMW: Rockport 2026** focuses on remastering the **real PS2 *Need for Speed: Most Wanted – Black Edition* (`SLUS-21351`) running inside PCSX2** — preserving the original story, world, physics, FMVs, and transferable `.ps2` memory-card saves (`BASLUS-21351`):
- **Non-Destructive Modded ISO Builder (`tools/build_modded_iso.py`)**: Clones the user's untouched `SLUS-21351` ISO strictly read-only (`'rb'`), applies binary-verified MIPS R5900 ELF patches (`patches/elf_patches.json`), updates `ZDIR.BIN` (`bCalculateCrc32`) / `ZZDATA3.BIN` / ISO9660 PVD, and proves byte-identical zero-edit round-trips (`tests/test_iso_roundtrip.py`).
- **Independently Verified `.pnach` & MIPS Patch Suite (`patches/pnach/SLUS-21351_0518D274.pnach`)**: Native 16:9 / 21:9 widescreen, wider FOV, sepia/smear/bloom toggles, high-LOD cars & scenery draw distance, 5-face car reflections, planar damp-asphalt road reflections, and 60 Hz ScreenEFX timing — every address verified against unmodified `SLUS_213.51` ELF disassembly (`tools/elf_disasm.py`).
- **PS2 Data, Lighting, Audio & Texture Pipelines (`tools/`)**: Pure-Python `VPAK` VLT editor (`vlt_ps2.py`), `GLOBALB.LZC` lighting editor (`lighting_ps2.py`), 84-car GF(2) VU1 ADC mesh upgrader (`geometry_compiler.py`, `upgrade_car_fleet.py`), `TPK` texture extractor (`tpk_extract.py`), pure-Rust BC7 DDS encoder (`tools/dds_encoder_rs/`), PCSX2 per-game configurator (`setup_pcsx2_macos.py`), and PINE RAM telemetry + MP4 capture harness (`pcsx2_pine_driver.py`, `race_flywheel.py`, `video_qa_pipeline.py`).

> **Note on Experimental Metal RT Prototype:** Our earlier external Metal RT engine/bridge work (`native_metal_rt/`) has been dropped from `main` and archived separately on branch [`archive/standalone-metal-rt-v1.4`](https://github.com/yash0530/nfsmw-ps2-remaster/tree/archive/standalone-metal-rt-v1.4). Active development on `main` is strictly a **real PS2/PCSX2 mod** verified through user-scored Original vs. Modded side-by-side race benchmarks.

---

## Credits & Acknowledgments

- **[`dbalatoni13/nfsmw`](https://github.com/dbalatoni13/nfsmw)** — `SLUS-21351` decompilation symbol names used to identify and cross-check PS2 engine functions and globals in `SLUS_213.51`.
- **PCSX2 Team** — PCSX2 emulator, PINE IPC protocol, Metal/Vulkan GS renderers, and texture replacement pipeline.

---

## Legal & "Bring Your Own ISO" Policy

**This repository contains ZERO copyrighted Electronic Arts or Sony files.**
- No `.iso`, `.bin`, `.bun`, `.elf`, `.rkpt`, `.m4a`, `.aac`, `.caf`, `.mp3`, `.wav`, `.pcm`, `.obj`, BIOS, audio banks, or original game textures are hosted here.
- Automated CI (`tools/audit_repo_clean.py`) checks every commit so zero prohibited binaries, oversized files, or hardcoded personal paths enter Git.
- See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for our strict CC0 / SIL OFL / MIT policy.

---

## Quick Start

### 1. Build a Non-Destructive Modded ISO + Install PCSX2 Profile (`SLUS-21351`)
```bash
python3 tools/rockport_cli.py build \
  --profile rockport2026_v0.5 \
  --source "/path/to/Need for Speed - Most Wanted - Black Edition.iso" \
  --install-pcsx2 \
  --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```
*(Or `make build-v0.5 ISO="/path/to/iso"`.)*

### 2. Run Offline Verification & Tests
```bash
python3 tools/rockport_cli.py verify --include-untracked
CARGO_TARGET_DIR=build/rust_target cargo test --manifest-path tools/dds_encoder_rs/Cargo.toml
NFSMW_ISO=/nonexistent python3 -m unittest discover -s tests -v
```

---

## Repository Structure

```text
nfsmw-ps2-remaster/
├── .github/workflows/ci.yml        # Automated Ubuntu (Py 3.11/3.12) + macOS CI & Rust BC7 tests
├── assets_cc0/                     # CC0/OFL audio, livery, & material rules
├── docs/
│   ├── PLAY_GUIDE.md               # Player guide & PCSX2 configuration notes
│   ├── PROGRESS.md                 # Milestone progress tracker
│   ├── ROADMAP.md                  # PS2/PCSX2 engineering roadmap
│   └── research/                   # SLUS-21351 binary/format research notes
├── native_metal_rt/                # Archived experimental Metal RT prototype (see archive/standalone-metal-rt-v1.4)
├── patches/                        # ELF caves, 20 MIPS patches, lighting, .pnach, profiles, VLT
├── shaders/                        # Post-processing shader experiments
├── src/ps2_mod/                    # Reproducible ps2dev/ps2sdk C plugin blueprint
├── tests/                          # Automated offline & live unit/integration test suites
├── tools/                          # Pure-Python PS2 archive/ISO/ELF/VLT/texture CLI + Rust BC7 encoder
├── ASSETS_LICENSE.md
├── LICENSE
└── Makefile
```
