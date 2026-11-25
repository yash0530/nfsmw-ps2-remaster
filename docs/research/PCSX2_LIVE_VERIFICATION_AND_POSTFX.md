# PCSX2 v2.6.3 Live Verification & Post-FX Architecture (macOS Metal)

> **Project:** NFSMW: Rockport 2026 (`v0.4.0` — Wave 3)  
> **Verifier Tool:** [`tools/pcsx2_live_verify.py`](../../tools/pcsx2_live_verify.py)  
> **Shaders:** [`shaders/rockport2026_ssgi.slang`](../../shaders/rockport2026_ssgi.slang), [`shaders/rockport2026_aces_grade.glsl`](../../shaders/rockport2026_aces_grade.glsl)  
> **Target Binary:** `/Applications/PCSX2-v2.6.3.app/Contents/MacOS/PCSX2` (`v2.6.3`, Rosetta 2 `x86_64` -> Apple M5 Pro Metal 2.3)

---

## 1. Live Boot Verification Results (`NFSMW_BlackEdition_Rockport2026_v0.3.iso`)

`tools/pcsx2_live_verify.py` was executed live against `build/NFSMW_BlackEdition_Rockport2026_v0.3.iso` using an isolated `HOME` sandbox under `build/pcsx2_verify_wave3/sandbox_home` so that:
- The original retail ISO was never touched.
- Global `~/Library/Application Support/PCSX2/inis/PCSX2.ini` remained byte-for-byte untouched (`global_pcsx2_ini_untouched: true`, verified by pre/post SHA-256).
- BIOS ROMs were referenced read-only via `[Folders] Bios` without copying any copyrighted files into the repository.
- All logs, snaps, and JSON reports were written strictly inside `build/pcsx2_verify_wave3/`.

### Verified Runtime Telemetry (`build/pcsx2_verify_wave3/verify_report.json`)

| Check | Verified Observed Result |
| :--- | :--- |
| **PCSX2 Version & Host** | `v2.6.3`, `Darwin 27.2.0 x86_64`, `Apple M5 Pro (Rosetta)` |
| **Target ISO & ELF CRC** | `SLUS_213.51` (`SLUS-21351`), ELF CRC **`2DAD2310`**, `1808064` sectors |
| **BIOS** | Found & loaded (`USA v01.60(07/02/2002) Console 20020207-164243`) |
| **Per-Game INI Loaded** | `SLUS-21351_2DAD2310.ini` (`Renderer = 17`, `upscale_multiplier = 6`, `CASMode = 2`, `fxaa = true`, `ShadeBoost = true`) |
| **Per-CRC `.pnach` Applied** | `SLUS-21351_2DAD2310.pnach`: 4 patch groups found, **1 active** (`Widescreen 16:9 (Runtime Confirm)` -> `1,EE,word,00aafe54,1`) |
| **Metal GS Device** | `AGXG17SDevice` — `name = Apple M5 Pro`, `Shader Version: Metal 2.3`, `Unified Memory: Supported`, `Framebuffer Fetch: Supported`, `Max Texture Size: 16384` |
| **Active Execution** | `ELF cdrom0:\SLUS_213.51;1` executing at `0x00436128`, `NTSC 640x448 @ 59.940`, FMV playback active |
| **Fatal Errors / Crashes** | **`0`** (`fatal_errors: []`) |
| **Window Capture (`screencapture -l`)** | Quartz located PCSX2 window (`1496x939`, `window_id: 3893`); `screencapture` honestly reported `permission_denied: true` (`could not create image from window` under macOS Screen Recording TCC) |
| **PCSX2 Internal F8 Capture (`snaps/`)** | Succeeded (`2992x1683` PNG, `2,318,478` bytes, `non_black_coverage = 0.4304`, `mean_luma = 43.52`, `std_luma = 70.61`, `is_valid_render = true`) |
| **Process Termination** | Clean `SIGTERM` (`returncode: 0`, `orphan_remains: false`, uptime `29.14s`) |

---

## 2. PCSX2 v2.6.3 CLI Flags & INI Post-FX Schema (Binary-Verified)

Audited directly against `/Applications/PCSX2-v2.6.3.app/Contents/MacOS/PCSX2` and `Contents/Resources/Metal23.metallib` + `shaders/vulkan/`:

### 2.1 CLI Launch Flags (`PCSX2 -batch -fastboot -logfile ... -- <iso>`)

Verified from `PCSX2` binary CLI usage table:
- `-batch`: Enables batch mode (exits cleanly after shutting down).
- `-fastboot`: Skips slow PS2 BIOS splash animation and boots the ELF directly.
- `-logfile <path>`: Redirects `emulog.txt` output to a dedicated log path (`build/pcsx2_verify_wave3/pcsx2_emulog.txt`).
- `-nogui`: Hides the main window while running (intentionally omitted when attempting window/F8 visual capture).
- `-- <iso>`: Target disc image path.

### 2.2 Built-In Metal Post-FX Controls (`[EmuCore/GS]`)

PCSX2 v2.6.3 compiles its Metal shaders ahead-of-time into `Metal22.metallib` / `Metal23.metallib` / `default.metallib` (derived from `shaders/vulkan/*.glsl` and `shaders/common/fxaa.fx` in `GSDeviceMTL.mm`). Every `[EmuCore/GS]` post-FX key verified in both `PCSX2` binary strings and `Metal23.metallib`:

| INI Key | Schema / Valid Values | `Metal23.metallib` Kernel | Role in Rockport 2026 |
| :--- | :--- | :--- | :--- |
| `Renderer` | `-1` Auto, `11` Null, `13` SW, `14` Vulkan, `17` Metal | `GSDeviceMTL` | `17` selects native Apple Metal (`Metal 2.3` on M5 Pro) |
| `fxaa` | `bool` (`true` / `false`) | `ps_fxaa` (`shaders/common/fxaa.fx`) | Smooths fence/spoiler sub-pixel specular aliasing |
| `CASMode` | `int` `0..2` (`0` None, `1` Sharpen Only IR, `2` Sharpen & Resize Display) | `CASFloat` / `CASHalf` (`CAS_SHARPEN_ONLY`) | `2` sharpens 6× IR output cleanly to Retina display resolution |
| `CASSharpness` | `int` `0..100` (default `50`; Rockport `45`) | `CASFloat` / `CASHalf` (`const0`/`const1`) | Restores asphalt & car body-line crispness without halo ringing |
| `ShadeBoost` | `bool` (`true` / `false`) | `ps_shadeboost` | Enables final brightness/contrast/saturation/gamma pass |
| `ShadeBoost_Brightness` | `int` `0..100` (`50` = `1.0` neutral) | `ps_shadeboost` (`params.x`) | Neutral `50` |
| `ShadeBoost_Contrast` | `int` `0..100` (`50` = `1.0` neutral; Rockport `54`) | `ps_shadeboost` (`params.y`) | Gentle punch (`54`) after brown-filter removal |
| `ShadeBoost_Saturation` | `int` `0..100` (`50` = `1.0` neutral; Rockport `56`) | `ps_shadeboost` (`params.z`) | Clean cobalt/amber vibrancy (`56`) |
| `ShadeBoost_Gamma` | `int` `0..100` (`50` = `1.0` neutral; Rockport `48`) | `ps_shadeboost` (`params.w`) | Slight shadow anchor (`48`) |
| `TVShader` | `int` `0..5` (None, Scanlines, Diagonal, Triangular, Complex, Lottes CRT) | `ps_filter_*` | `0` (disabled for clean modern remaster) |

---

## 3. Three-Tier Rendering Capability Breakdown

A key engineering principle of **NFSMW: Rockport 2026** is being exact about what each rendering tier can actually execute:

### Tier 1 — PCSX2 v2.6.3 Native Metal Today (Zero Upstream Changes)
- **What runs natively:** `GSDeviceMTL` (`Metal 2.3`), up to 6×+ IR upscaling, 16× anisotropic filtering, 4K BC7 replacement textures precached into 64 GB Unified Memory, `GameIndex.yaml` / `UserHacks` fixes, ELF/`.pnach` brown-filter & motion-blur removal, and the fixed built-in post-FX chain (`ps_fxaa`, `CASFloat`/`CASHalf`, `ps_shadeboost`).
- **Hard limits:** Binary inspection of `PCSX2 v2.6.3` confirms `librashader` / `.slangp` support is **not compiled in**, and legacy standalone `.fx`/`.glsl` post-processing shaders were removed in PCSX2 2.0. ReShade does not run natively on macOS Metal.

### Tier 2 — PCSX2 `librashader` Metal (PR `#14433`) / Vulkan Multi-Pass Shaders (`shaders/`)
- **Target files:** [`shaders/rockport2026_ssgi.slang`](../../shaders/rockport2026_ssgi.slang) and [`shaders/rockport2026_aces_grade.glsl`](../../shaders/rockport2026_aces_grade.glsl).
- **What this unlocks:** Arbitrary user `.slang` / `.slangp` post-processing chains compiled cross-platform to Metal/SPIR-V via `librashader`, including:
  - **Screen-Space GI & Wet-Road SSR** (`rockport2026_ssgi.slang`): 8-direction golden-angle indirect color bounce, horizon-perspective wet asphalt reflections with Schlick Fresnel + micro-ripple roughness, and 3×3 bilateral stabilization.
  - **ACES Filmic Grading & Dehaze** (`rockport2026_aces_grade.glsl`): dark-channel atmospheric veil dehaze, edge-clamped micro-contrast, 8-tap Karis-averaged HDR bloom, and Stephen Hill AP1 ACES RRT+ODT tone mapping.
- **Architectural boundary:** Post-FX shaders only see the final 2D RGBA frame (`Source`), without PS2 GS world-space depth, G-buffer normals, or scene BVH geometry.

### Tier 3 — Native M5 Pro Metal 4 Hardware Ray Tracing (`native_metal/` & Track A/B Recomp)
- **Target codebase:** `native_metal/` (macOS Metal 4 `MTLAccelerationStructure` / `intersect()` ray tracing + Cook-Torrance GGX dual-lobe clearcoat PBR + `tools/decomp_bridge.py` `Rockport2026Lighting.h`).
- **What only Tier 3 can do:** True world-space ray-traced 1-bounce GI, sharp curved-fender ray-traced reflections on the BMW M3 GTR E46 clearcoat, physically accurate wet-puddle reflection rays hitting Rockport skyline & buildings, ray-traced soft solar shadows, and native ARM64 120 Hz frame pacing without Rosetta 2 or PS2 30 Hz coupling.

---

## 4. Safe Usage & CI Testing

```bash
# Dry-run preflight only (checks ISO CRC, BIOS dir, per-game .ini/.pnach without launching PCSX2)
python3 tools/pcsx2_live_verify.py --dry-run --json

# Live sandboxed PCSX2 v2.6.3 Metal boot verification (writes only under build/pcsx2_verify_wave3/)
python3 tools/pcsx2_live_verify.py build/NFSMW_BlackEdition_Rockport2026_v0.3.iso --boot-wait 24 --json

# Offline unit tests (CI-safe; skips live PCSX2 launch unless NFSMW_LIVE_PCSX2=1)
python3 -m unittest discover -s tests -p "test_pcsx2_verify.py" -v
```
