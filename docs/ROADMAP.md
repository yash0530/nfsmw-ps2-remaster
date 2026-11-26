# NFSMW: Rockport 2026 — Engineering Roadmap

> **Mission:** Remaster the **real PlayStation 2 *Need for Speed: Most Wanted – Black Edition* (`SLUS-21351`) inside PCSX2** — preserving the authentic story, gameplay, FMVs, and `.ps2` memory-card saves — built with open-source, non-destructive PS2/PCSX2 modding tools; zero copyrighted EA game assets distributed.

**See also:** [Progress Table (`docs/PROGRESS.md`)](PROGRESS.md) · [Player's Guide (`docs/PLAY_GUIDE.md`)](PLAY_GUIDE.md)

**Credits:** `SLUS-21351` decompilation symbol names are credited to **[`dbalatoni13/nfsmw`](https://github.com/dbalatoni13/nfsmw)**.

---

## Active Direction: Pure Real-PS2 PCSX2 Remaster (`v2.1` – `v2.4`)

`main` is strictly a **real PS2/PCSX2 mod** — the external Metal RT engine/bridge is dropped from `main` and archived separately on `archive/standalone-metal-rt-v1.4`. Every milestone is verified against the unmodified retail `SLUS-21351` ISO (opened strictly read-only `'rb'`), automated Ubuntu + macOS CI, and user-scored **Original vs. Modded** side-by-side race benchmarks in PCSX2.

| Milestone | Scope | Verification Gate |
| :--- | :--- | :--- |
| **Stabilization (Current)** | Green Ubuntu + macOS CI, `.pnach` provenance audit, zero-edit ISO round-trip proof | 0 CI errors on `ubuntu-latest` + `macos-latest`; byte-identical SHA-256 round-trip (`tests/test_iso_roundtrip.py`); zero copied unlicensed `.pnach` blocks |
| **`v2.1` — PCSX2 4K/32-bit Profile, 60 FPS Proof & Baseline Video** | Verified PCSX2 Metal 2.3 profile (`dithering_ps2=3`, 4K scaling, 16× AF, trilinear) + measured 60 FPS proof + Original vs. Modded baseline race video | User QA on side-by-side Original vs. Modded race capture |
| **`v2.2` — District-by-District Faithful HD Textures (In-Game Dumps)** | Faithful BC7 DDS texture replacements built directly from real in-game PCSX2 dumps via Rust `rockport_dds`, district by district | User QA; preserves original signage, art direction, and GS `0x80` alpha with zero hash leaks |
| **`v2.3` — 4K HUD/Loading Icons & Opt-In Per-Car VU1 Geometry** | Premultiplied faithful 4K HUD/font/loading-icon replacements + opt-in per-car GF(2) VU1 mesh upgrades | User QA; zero broken loading icons or missing wheels |
| **`v2.4` — Measured VLT Handling/Pursuit Polish & Save Proof** | Measurable VLT handling/pursuit tuning plus end-to-end `BASLUS-21351` `.ps2` memory-card save transfer proof | User QA; Original vs. Modded side-by-side race benchmark & save transfer verified |

---

## Completed Foundation (`v0.1.0` – `v0.5.0`)

- **`v0.1.0` – `v0.4.0` PS2 Modding Foundation (`ef5e714`..`a3dc4de`)**:
  - Verified `SLUS-21351` ISO9660 layout, `ZDIR.BIN` (`1,294/1,294` CRC32), `JDLZ` codec, `VPAK` VLT editor (`56/56` classes) + `GLOBALMEMORYFILE.BIN` sync, MIPS R5900 disassembler & ELF patch suite, `GLOBALB.LZC` lighting editor, `916`-texture TPK decoder, PS2 `GEOMETRY.BIN` + GF(2) VU1 LFSR ADC compiler, native Rust BC7 DDS encoder (`tools/dds_encoder_rs/`), and automated hygiene CI (`tools/audit_repo_clean.py`).
- **`v0.5.0` PS2/PCSX2 Remaster Baseline (`b1bff22`)**:
  - Wheel-safe 84-car VU1 fleet (`2.83M` tris, `0` VU1 errors), `293`-edit `forza_ultra` lighting + planar damp-asphalt road reflections + `2.67×` reflection scenery cull, `170` 4× BC7 world textures, 20 Hz PINE RAM telemetry driver, and automated MP4 capture pipeline.

---

## Archived Experimental Prototype (`archive/standalone-metal-rt-v1.4`)

Between `v0.6.0` and `v1.4.0` (`5fccacb`..`7245b4b`), we built an experimental standalone Apple Silicon Metal RT renderer (`native_metal_rt/`). That external Metal RT engine/bridge work is dropped from `main` and archived separately on branch [`archive/standalone-metal-rt-v1.4`](https://github.com/yash0530/nfsmw-ps2-remaster/tree/archive/standalone-metal-rt-v1.4) so `main` stays strictly focused on remastering the real PS2 game inside PCSX2.
