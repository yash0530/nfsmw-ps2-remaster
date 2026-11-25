# rockport_dds (Native Pure-Rust BC7 DDS & Mipmap Encoder)

Zero-dependency (`std`-only) Rust CLI that compiles offline in <2s on Apple Silicon (`arm64`) and Linux/x86_64 CI, generating standard 2D `.dds` textures with full mipmap chains for PCSX2 texture replacement (`SLUS-21351`).

## Supported Formats

- **Input**:
  - Uncompressed / RLE **TGA** (24-bit BGR and 32-bit BGRA, top-down or bottom-up)
  - **PAM** (`P7`, `RGB` or `RGB_ALPHA`, `MAXVAL 255`)
  - Raw **RGBA8** (`--input-raw --width W --height H`)
- **Output DDS**:
  - `bc7` (default): `DXGI_FORMAT_BC7_UNORM` (`98`, or `99` with `--srgb`), 148-byte `DDS ` + `DDS_HEADER_DXT10` header, Mode 6 high-precision RGBA block encoder with PCA principal-axis seeding, least-squares endpoint refinement, and optimal p-bit selection.
  - `bc3`: `DXGI_FORMAT_BC3_UNORM` (`77` / `DXT5`), full alpha + RGB.
  - `bc1`: `DXGI_FORMAT_BC1_UNORM` (`71` / `DXT1`), opaque or 1-bit punch-through alpha.
  - `rgba8`: `DXGI_FORMAT_R8G8B8A8_UNORM` (`28`), uncompressed RGBA fallback.

## Build & Test

Always direct Cargo's build directory into `build/dds_wave3/target` so `tools/dds_encoder_rs/` stays clean:

```bash
CARGO_TARGET_DIR=build/dds_wave3/target cargo build --release --manifest-path tools/dds_encoder_rs/Cargo.toml
CARGO_TARGET_DIR=build/dds_wave3/target cargo test --manifest-path tools/dds_encoder_rs/Cargo.toml
```

## Usage

```bash
build/dds_wave3/target/release/rockport_dds \
  --input path/to/texture.tga \
  --output build/dds_wave3/pcsx2_dds_pack/texture.dds \
  --format bc7
```
