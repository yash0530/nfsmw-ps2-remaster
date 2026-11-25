//! rockport_dds — Pure-std Rust native DDS (BC7/BC3/BC1/RGBA8) + mipmap encoder.
//!
//! Zero external crates: builds completely offline on Apple Silicon arm64 and Linux CI.
//! Supports uncompressed/RLE 24/32-bit TGA, PAM (P7), and raw RGBA8 inputs.

use std::env;
use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::process;
use std::thread;

const DDS_MAGIC: [u8; 4] = *b"DDS ";
const DDS_HEADER_SIZE: u32 = 124;
const DDS_PIXELFORMAT_SIZE: u32 = 32;

const DDSD_CAPS: u32 = 0x1;
const DDSD_HEIGHT: u32 = 0x2;
const DDSD_WIDTH: u32 = 0x4;
const DDSD_PITCH: u32 = 0x8;
const DDSD_PIXELFORMAT: u32 = 0x1000;
const DDSD_MIPMAPCOUNT: u32 = 0x20000;
const DDSD_LINEARSIZE: u32 = 0x80000;

const DDPF_ALPHAPIXELS: u32 = 0x1;
const DDPF_FOURCC: u32 = 0x4;
const DDPF_RGB: u32 = 0x40;

const DDSCAPS_COMPLEX: u32 = 0x8;
const DDSCAPS_TEXTURE: u32 = 0x1000;
const DDSCAPS_MIPMAP: u32 = 0x400000;

const D3D10_RESOURCE_DIMENSION_TEXTURE2D: u32 = 3;

const DXGI_FORMAT_R8G8B8A8_UNORM: u32 = 28;
const DXGI_FORMAT_R8G8B8A8_UNORM_SRGB: u32 = 29;
const DXGI_FORMAT_BC1_UNORM: u32 = 71;
const DXGI_FORMAT_BC1_UNORM_SRGB: u32 = 72;
const DXGI_FORMAT_BC3_UNORM: u32 = 77;
const DXGI_FORMAT_BC3_UNORM_SRGB: u32 = 78;
const DXGI_FORMAT_BC7_UNORM: u32 = 98;
const DXGI_FORMAT_BC7_UNORM_SRGB: u32 = 99;

const BC7_WEIGHTS4: [i32; 16] = [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64];
const BC7_WEIGHTS4_F: [f32; 16] = [
    0.0 / 64.0,
    4.0 / 64.0,
    9.0 / 64.0,
    13.0 / 64.0,
    17.0 / 64.0,
    21.0 / 64.0,
    26.0 / 64.0,
    30.0 / 64.0,
    34.0 / 64.0,
    38.0 / 64.0,
    43.0 / 64.0,
    47.0 / 64.0,
    51.0 / 64.0,
    55.0 / 64.0,
    60.0 / 64.0,
    64.0 / 64.0,
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OutputFormat {
    Bc7,
    Bc3,
    Bc1,
    Rgba8,
}

impl OutputFormat {
    fn from_str(s: &str) -> Result<Self, String> {
        match s.to_ascii_lowercase().as_str() {
            "bc7" | "bc7_unorm" | "bc7_unorm_srgb" => Ok(Self::Bc7),
            "bc3" | "dxt5" | "bc3_unorm" | "bc3_unorm_srgb" => Ok(Self::Bc3),
            "bc1" | "dxt1" | "bc1_unorm" | "bc1_unorm_srgb" => Ok(Self::Bc1),
            "rgba8" | "rgba" | "r8g8b8a8_unorm" => Ok(Self::Rgba8),
            _ => Err(format!("Unsupported output format: {s} (expected bc7, bc3, bc1, rgba8)")),
        }
    }

    fn dxgi_format(self, srgb: bool) -> u32 {
        match (self, srgb) {
            (Self::Bc7, false) => DXGI_FORMAT_BC7_UNORM,
            (Self::Bc7, true) => DXGI_FORMAT_BC7_UNORM_SRGB,
            (Self::Bc3, false) => DXGI_FORMAT_BC3_UNORM,
            (Self::Bc3, true) => DXGI_FORMAT_BC3_UNORM_SRGB,
            (Self::Bc1, false) => DXGI_FORMAT_BC1_UNORM,
            (Self::Bc1, true) => DXGI_FORMAT_BC1_UNORM_SRGB,
            (Self::Rgba8, false) => DXGI_FORMAT_R8G8B8A8_UNORM,
            (Self::Rgba8, true) => DXGI_FORMAT_R8G8B8A8_UNORM_SRGB,
        }
    }

    fn uses_dx10_by_default(self, force_dx10: bool, srgb: bool) -> bool {
        force_dx10 || srgb || matches!(self, Self::Bc7)
    }

    fn block_bytes(self) -> usize {
        match self {
            Self::Bc7 | Self::Bc3 => 16,
            Self::Bc1 => 8,
            Self::Rgba8 => 4,
        }
    }
}

struct ImageRgba {
    width: u32,
    height: u32,
    pixels: Vec<u8>, // length = width * height * 4
}

struct CliArgs {
    input: PathBuf,
    output: PathBuf,
    format: OutputFormat,
    mipmaps: bool,
    srgb: bool,
    force_dx10: bool,
    input_raw: bool,
    width: Option<u32>,
    height: Option<u32>,
    threads: usize,
    verbose: bool,
}

fn print_usage() {
    eprintln!(
        "rockport_dds v0.4.0 — Pure-Rust Native BC7/BC3/BC1/RGBA8 DDS Encoder\n\
         Usage:\n  \
           rockport_dds --input <in> --output <out.dds> [options]\n\n\
         Options:\n  \
           -i, --input <path>     Input image (.tga, .pam, or raw RGBA8 with --input-raw)\n  \
           -o, --output <path>    Output .dds path\n  \
           -f, --format <fmt>     bc7 (default), bc3, bc1, rgba8\n  \
           --mipmaps              Generate full mipmap chain (default)\n  \
           --no-mipmaps           Encode top mip level only\n  \
           --srgb                 Use sRGB-aware mip downsampling & DXGI *_SRGB format\n  \
           --dx10                 Always emit 148-byte DX10 extended DDS header\n  \
           --input-raw            Treat input as raw tightly-packed RGBA8 bytes\n  \
           --width <px>           Width (required with --input-raw)\n  \
           --height <px>          Height (required with --input-raw)\n  \
           -j, --threads <n>      Worker threads (default: available CPU cores)\n  \
           -v, --verbose          Print encoding statistics\n  \
           -h, --help             Show this help"
    );
}

fn parse_args() -> Result<CliArgs, String> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() {
        print_usage();
        process::exit(2);
    }

    let mut input: Option<PathBuf> = None;
    let mut output: Option<PathBuf> = None;
    let mut format = OutputFormat::Bc7;
    let mut mipmaps = true;
    let mut srgb = false;
    let mut force_dx10 = false;
    let mut input_raw = false;
    let mut width: Option<u32> = None;
    let mut height: Option<u32> = None;
    let mut threads = thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    let mut verbose = false;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "-h" | "--help" => {
                print_usage();
                process::exit(0);
            }
            "--version" => {
                println!("rockport_dds 0.4.0");
                process::exit(0);
            }
            "-i" | "--input" => {
                i += 1;
                let v = args.get(i).ok_or("--input requires a path")?;
                input = Some(PathBuf::from(v));
            }
            "-o" | "--output" => {
                i += 1;
                let v = args.get(i).ok_or("--output requires a path")?;
                output = Some(PathBuf::from(v));
            }
            "-f" | "--format" => {
                i += 1;
                let v = args.get(i).ok_or("--format requires a value")?;
                format = OutputFormat::from_str(v)?;
            }
            "--mipmaps" => mipmaps = true,
            "--no-mipmaps" => mipmaps = false,
            "--srgb" => srgb = true,
            "--dx10" => force_dx10 = true,
            "--input-raw" => input_raw = true,
            "--width" => {
                i += 1;
                let v = args.get(i).ok_or("--width requires a number")?;
                width = Some(v.parse::<u32>().map_err(|_| format!("Invalid width: {v}"))?);
            }
            "--height" => {
                i += 1;
                let v = args.get(i).ok_or("--height requires a number")?;
                height = Some(v.parse::<u32>().map_err(|_| format!("Invalid height: {v}"))?);
            }
            "-j" | "--threads" => {
                i += 1;
                let v = args.get(i).ok_or("--threads requires a number")?;
                threads = v
                    .parse::<usize>()
                    .map_err(|_| format!("Invalid thread count: {v}"))?
                    .max(1);
            }
            "-v" | "--verbose" => verbose = true,
            other if !other.starts_with('-') => {
                if input.is_none() {
                    input = Some(PathBuf::from(other));
                } else if output.is_none() {
                    output = Some(PathBuf::from(other));
                } else {
                    return Err(format!("Unexpected positional argument: {other}"));
                }
            }
            other => return Err(format!("Unknown option: {other}")),
        }
        i += 1;
    }

    let input = input.ok_or("Missing --input")?;
    let output = output.ok_or("Missing --output")?;
    if input_raw && (width.is_none() || height.is_none()) {
        return Err("--input-raw requires both --width and --height".to_string());
    }

    Ok(CliArgs {
        input,
        output,
        format,
        mipmaps,
        srgb,
        force_dx10,
        input_raw,
        width,
        height,
        threads,
        verbose,
    })
}

// ---------------------------------------------------------------------------
// Image Loaders (Raw RGBA8, PAM P7, Uncompressed/RLE 24/32-bit TGA)
// ---------------------------------------------------------------------------

fn load_image(args: &CliArgs) -> Result<ImageRgba, String> {
    let mut file = BufReader::new(
        File::open(&args.input)
            .map_err(|e| format!("Failed to open {}: {e}", args.input.display()))?,
    );
    let mut data = Vec::new();
    file.read_to_end(&mut data)
        .map_err(|e| format!("Failed to read {}: {e}", args.input.display()))?;

    if args.input_raw {
        let w = args.width.unwrap();
        let h = args.height.unwrap();
        return decode_raw_rgba8(&data, w, h);
    }

    if data.starts_with(b"P7\n") || data.starts_with(b"P7\r\n") {
        return decode_pam(&data);
    }

    let ext = args
        .input
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| s.to_ascii_lowercase())
        .unwrap_or_default();

    if ext == "raw" || ext == "rgba" {
        if let (Some(w), Some(h)) = (args.width, args.height) {
            return decode_raw_rgba8(&data, w, h);
        }
    }

    decode_tga(&data)
}

fn decode_raw_rgba8(data: &[u8], width: u32, height: u32) -> Result<ImageRgba, String> {
    if width == 0 || height == 0 {
        return Err("Width and height must be non-zero".to_string());
    }
    let expected = (width as usize)
        .checked_mul(height as usize)
        .and_then(|px| px.checked_mul(4))
        .ok_or("Image dimensions overflow")?;
    if data.len() != expected {
        return Err(format!(
            "Raw RGBA8 byte count mismatch: got {} bytes, expected {} ({}x{}x4)",
            data.len(),
            expected,
            width,
            height
        ));
    }
    Ok(ImageRgba {
        width,
        height,
        pixels: data.to_vec(),
    })
}

fn decode_pam(data: &[u8]) -> Result<ImageRgba, String> {
    let mut pos = 0usize;
    let mut width: Option<u32> = None;
    let mut height: Option<u32> = None;
    let mut depth: Option<u32> = None;
    let mut maxval: Option<u32> = None;
    let mut found_endhdr = false;

    while pos < data.len() {
        let line_end = data[pos..]
            .iter()
            .position(|&b| b == b'\n')
            .map(|off| pos + off)
            .unwrap_or(data.len());
        let line = std::str::from_utf8(&data[pos..line_end])
            .map_err(|_| "Invalid UTF-8 in PAM header")?
            .trim();
        pos = (line_end + 1).min(data.len());

        if line.is_empty() || line.starts_with('#') || line == "P7" {
            continue;
        }
        if line == "ENDHDR" {
            found_endhdr = true;
            break;
        }
        let mut parts = line.split_whitespace();
        let key = parts.next().unwrap_or("");
        let val = parts.next().unwrap_or("");
        match key {
            "WIDTH" => width = val.parse().ok(),
            "HEIGHT" => height = val.parse().ok(),
            "DEPTH" => depth = val.parse().ok(),
            "MAXVAL" => maxval = val.parse().ok(),
            "TUPLTYPE" => {}
            _ => {}
        }
    }

    if !found_endhdr {
        return Err("Malformed PAM header: missing ENDHDR".to_string());
    }
    let w = width.ok_or("PAM missing WIDTH")?;
    let h = height.ok_or("PAM missing HEIGHT")?;
    let d = depth.ok_or("PAM missing DEPTH")?;
    let mv = maxval.unwrap_or(255);
    if mv != 255 {
        return Err(format!("Unsupported PAM MAXVAL={mv} (expected 255)"));
    }
    if w == 0 || h == 0 {
        return Err("PAM dimensions must be non-zero".to_string());
    }
    let n_px = (w as usize) * (h as usize);
    let payload = &data[pos..];
    if payload.len() < n_px * (d as usize) {
        return Err("Truncated PAM pixel payload".to_string());
    }

    let mut rgba = Vec::with_capacity(n_px * 4);
    match d {
        4 => rgba.extend_from_slice(&payload[..n_px * 4]),
        3 => {
            for rgb in payload[..n_px * 3].chunks_exact(3) {
                rgba.extend_from_slice(&[rgb[0], rgb[1], rgb[2], 255]);
            }
        }
        1 => {
            for &g in &payload[..n_px] {
                rgba.extend_from_slice(&[g, g, g, 255]);
            }
        }
        _ => return Err(format!("Unsupported PAM DEPTH={d} (expected 1, 3, or 4)")),
    }

    Ok(ImageRgba {
        width: w,
        height: h,
        pixels: rgba,
    })
}

fn decode_tga(data: &[u8]) -> Result<ImageRgba, String> {
    if data.len() < 18 {
        return Err("File too small to be a valid TGA".to_string());
    }
    let id_len = data[0] as usize;
    let color_map_type = data[1];
    let image_type = data[2];
    let cm_len = u16::from_le_bytes([data[5], data[6]]) as usize;
    let cm_bpp = data[7] as usize;
    let width = u16::from_le_bytes([data[12], data[13]]) as u32;
    let height = u16::from_le_bytes([data[14], data[15]]) as u32;
    let bpp = data[16];
    let descriptor = data[17];

    if color_map_type != 0 {
        return Err(format!("Color-mapped TGA (type={color_map_type}) not supported"));
    }
    if width == 0 || height == 0 {
        return Err("TGA width and height must be non-zero".to_string());
    }
    if bpp != 24 && bpp != 32 {
        return Err(format!("Unsupported TGA bit depth: {bpp} (expected 24 or 32)"));
    }
    if image_type != 2 && image_type != 10 {
        return Err(format!(
            "Unsupported TGA image type: {image_type} (expected uncompressed 2 or RLE 10)"
        ));
    }

    let cm_bytes = if color_map_type == 0 {
        0
    } else {
        cm_len * ((cm_bpp + 7) / 8)
    };
    let offset = 18 + id_len + cm_bytes;
    if offset > data.len() {
        return Err("Truncated TGA header/ID".to_string());
    }

    let n_px = (width as usize) * (height as usize);
    let src_bpp = (bpp / 8) as usize;
    let mut rgba = vec![0u8; n_px * 4];
    let src = &data[offset..];

    if image_type == 2 {
        let needed = n_px * src_bpp;
        if src.len() < needed {
            return Err(format!(
                "Truncated TGA pixel data: got {} bytes, need {}",
                src.len(),
                needed
            ));
        }
        for i in 0..n_px {
            let s = i * src_bpp;
            let d = i * 4;
            rgba[d] = src[s + 2];
            rgba[d + 1] = src[s + 1];
            rgba[d + 2] = src[s];
            rgba[d + 3] = if src_bpp == 4 { src[s + 3] } else { 255 };
        }
    } else {
        // RLE true-color TGA (type 10)
        let mut s = 0usize;
        let mut px = 0usize;
        while px < n_px {
            if s >= src.len() {
                return Err("Truncated RLE TGA stream".to_string());
            }
            let packet = src[s];
            s += 1;
            let count = ((packet & 0x7F) as usize) + 1;
            if (packet & 0x80) != 0 {
                if s + src_bpp > src.len() {
                    return Err("Truncated RLE TGA run packet".to_string());
                }
                let r = src[s + 2];
                let g = src[s + 1];
                let b = src[s];
                let a = if src_bpp == 4 { src[s + 3] } else { 255 };
                s += src_bpp;
                for _ in 0..count {
                    if px >= n_px {
                        break;
                    }
                    let d = px * 4;
                    rgba[d] = r;
                    rgba[d + 1] = g;
                    rgba[d + 2] = b;
                    rgba[d + 3] = a;
                    px += 1;
                }
            } else {
                for _ in 0..count {
                    if px >= n_px {
                        break;
                    }
                    if s + src_bpp > src.len() {
                        return Err("Truncated RLE TGA raw packet".to_string());
                    }
                    let d = px * 4;
                    rgba[d] = src[s + 2];
                    rgba[d + 1] = src[s + 1];
                    rgba[d + 2] = src[s];
                    rgba[d + 3] = if src_bpp == 4 { src[s + 3] } else { 255 };
                    s += src_bpp;
                    px += 1;
                }
            }
        }
    }

    // TGA bit 5 of image descriptor: 0 = bottom-up origin, 1 = top-down origin.
    let top_down = (descriptor & 0x20) != 0;
    if !top_down {
        let row_bytes = (width as usize) * 4;
        let h = height as usize;
        for y in 0..(h / 2) {
            let top_off = y * row_bytes;
            let bot_off = (h - 1 - y) * row_bytes;
            for x in 0..row_bytes {
                rgba.swap(top_off + x, bot_off + x);
            }
        }
    }

    Ok(ImageRgba {
        width,
        height,
        pixels: rgba,
    })
}

// ---------------------------------------------------------------------------
// Mipmap Generation (2x2 Box Filter, optional sRGB <-> Linear LUT)
// ---------------------------------------------------------------------------

fn build_srgb_to_linear_lut() -> [f32; 256] {
    let mut lut = [0.0f32; 256];
    for (i, slot) in lut.iter_mut().enumerate() {
        let c = i as f32 / 255.0;
        *slot = if c <= 0.04045 {
            c / 12.92
        } else {
            ((c + 0.055) / 1.055).powf(2.4)
        };
    }
    lut
}

#[inline]
fn linear_to_srgb_u8(v: f32) -> u8 {
    let c = v.clamp(0.0, 1.0);
    let s = if c <= 0.0031308 {
        c * 12.92
    } else {
        1.055 * c.powf(1.0 / 2.4) - 0.055
    };
    (s * 255.0 + 0.5).clamp(0.0, 255.0) as u8
}

fn mip_count_for(width: u32, height: u32) -> u32 {
    let m = width.max(height).max(1);
    32 - m.leading_zeros()
}

fn downsample_2x2(src: &ImageRgba, srgb: bool, lut: &[f32; 256]) -> ImageRgba {
    let dst_w = (src.width / 2).max(1);
    let dst_h = (src.height / 2).max(1);
    let mut dst = vec![0u8; (dst_w as usize) * (dst_h as usize) * 4];
    let sw = src.width as usize;
    let sh = src.height as usize;

    for y in 0..(dst_h as usize) {
        let y0 = (y * 2).min(sh - 1);
        let y1 = (y * 2 + 1).min(sh - 1);
        for x in 0..(dst_w as usize) {
            let x0 = (x * 2).min(sw - 1);
            let x1 = (x * 2 + 1).min(sw - 1);

            let idx00 = (y0 * sw + x0) * 4;
            let idx10 = (y0 * sw + x1) * 4;
            let idx01 = (y1 * sw + x0) * 4;
            let idx11 = (y1 * sw + x1) * 4;

            let out_idx = (y * (dst_w as usize) + x) * 4;
            if srgb {
                for c in 0..3 {
                    let avg = 0.25
                        * (lut[src.pixels[idx00 + c] as usize]
                            + lut[src.pixels[idx10 + c] as usize]
                            + lut[src.pixels[idx01 + c] as usize]
                            + lut[src.pixels[idx11 + c] as usize]);
                    dst[out_idx + c] = linear_to_srgb_u8(avg);
                }
            } else {
                for c in 0..3 {
                    let sum = src.pixels[idx00 + c] as u32
                        + src.pixels[idx10 + c] as u32
                        + src.pixels[idx01 + c] as u32
                        + src.pixels[idx11 + c] as u32;
                    dst[out_idx + c] = ((sum + 2) >> 2) as u8;
                }
            }
            let a_sum = src.pixels[idx00 + 3] as u32
                + src.pixels[idx10 + 3] as u32
                + src.pixels[idx01 + 3] as u32
                + src.pixels[idx11 + 3] as u32;
            dst[out_idx + 3] = ((a_sum + 2) >> 2) as u8;
        }
    }

    ImageRgba {
        width: dst_w,
        height: dst_h,
        pixels: dst,
    }
}

fn generate_mip_chain(base: ImageRgba, mipmaps: bool, srgb: bool) -> Vec<ImageRgba> {
    let mut chain = Vec::new();
    let count = if mipmaps {
        mip_count_for(base.width, base.height)
    } else {
        1
    };
    let lut = build_srgb_to_linear_lut();
    chain.push(base);
    while (chain.len() as u32) < count {
        let next = downsample_2x2(chain.last().unwrap(), srgb, &lut);
        chain.push(next);
    }
    chain
}

// ---------------------------------------------------------------------------
// Block Extraction Helper
// ---------------------------------------------------------------------------

#[inline]
fn extract_4x4_rgba(img: &ImageRgba, bx: usize, by: usize) -> [[u8; 4]; 16] {
    let mut block = [[0u8; 4]; 16];
    let w = img.width as usize;
    let h = img.height as usize;
    for py in 0..4 {
        let sy = (by * 4 + py).min(h - 1);
        for px in 0..4 {
            let sx = (bx * 4 + px).min(w - 1);
            let off = (sy * w + sx) * 4;
            block[py * 4 + px] = [
                img.pixels[off],
                img.pixels[off + 1],
                img.pixels[off + 2],
                img.pixels[off + 3],
            ];
        }
    }
    block
}

// ---------------------------------------------------------------------------
// BC7 Mode 6 Encoder (7-bit RGBA + 1 p-bit per endpoint + 4-bit indices)
// ---------------------------------------------------------------------------

struct BitWriter128 {
    data: [u8; 16],
    bit_pos: usize,
}

impl BitWriter128 {
    #[inline]
    fn new() -> Self {
        Self {
            data: [0u8; 16],
            bit_pos: 0,
        }
    }

    #[inline]
    fn write_bits(&mut self, value: u32, count: usize) {
        for i in 0..count {
            if ((value >> i) & 1) != 0 {
                self.data[self.bit_pos >> 3] |= 1u8 << (self.bit_pos & 7);
            }
            self.bit_pos += 1;
        }
    }
}

#[inline]
fn bc7_quantize_channel(val: f32, pbit: u8) -> u8 {
    let q = ((val - pbit as f32) * 0.5).round() as i32;
    q.clamp(0, 127) as u8
}

#[inline]
fn bc7_best_index_and_err(
    px: [u8; 4],
    q0: [i32; 4],
    q1: [i32; 4],
) -> (u8, i32) {
    let mut best_idx = 0u8;
    let mut best_err = i32::MAX;
    for (idx, &w) in BC7_WEIGHTS4.iter().enumerate() {
        let iw = 64 - w;
        let dr = ((iw * q0[0] + w * q1[0] + 32) >> 6) - px[0] as i32;
        let dg = ((iw * q0[1] + w * q1[1] + 32) >> 6) - px[1] as i32;
        let db = ((iw * q0[2] + w * q1[2] + 32) >> 6) - px[2] as i32;
        let da = ((iw * q0[3] + w * q1[3] + 32) >> 6) - px[3] as i32;
        let err = dr * dr + dg * dg + db * db + da * da;
        if err < best_err {
            best_err = err;
            best_idx = idx as u8;
            if err == 0 {
                break;
            }
        }
    }
    (best_idx, best_err)
}

fn encode_bc7_mode6_block(pixels: &[[u8; 4]; 16]) -> [u8; 16] {
    // Fast path for uniform blocks
    let first = pixels[0];
    let mut uniform = true;
    for p in &pixels[1..] {
        if *p != first {
            uniform = false;
            break;
        }
    }

    if uniform {
        // Test whether all channels share LSB parity (lossless) or mixed parity (p0=0, p1=1).
        let p_lsb = first[0] & 1;
        let same_parity = (first[1] & 1) == p_lsb
            && (first[2] & 1) == p_lsb
            && (first[3] & 1) == p_lsb;
        if same_parity {
            let mut bw = BitWriter128::new();
            bw.write_bits(0b1000000, 7);
            for ch in 0..4 {
                let c = (first[ch] >> 1) as u32;
                bw.write_bits(c, 7);
                bw.write_bits(c, 7);
            }
            bw.write_bits(p_lsb as u32, 1);
            bw.write_bits(p_lsb as u32, 1);
            // Anchor index 0 (3 bits) + 15 indices (4 bits) all 0 -> already zeroed.
            return bw.data;
        }
    }

    let mut pf = [[0.0f32; 4]; 16];
    let mut mean = [0.0f32; 4];
    let mut min_c = [255.0f32; 4];
    let mut max_c = [0.0f32; 4];

    for (i, p) in pixels.iter().enumerate() {
        for c in 0..4 {
            let v = p[c] as f32;
            pf[i][c] = v;
            mean[c] += v;
            if v < min_c[c] {
                min_c[c] = v;
            }
            if v > max_c[c] {
                max_c[c] = v;
            }
        }
    }
    for m in &mut mean {
        *m *= 1.0 / 16.0;
    }

    // 4x4 covariance matrix (upper triangle)
    let mut cov = [[0.0f32; 4]; 4];
    for p in &pf {
        let d = [p[0] - mean[0], p[1] - mean[1], p[2] - mean[2], p[3] - mean[3]];
        for r in 0..4 {
            for c in r..4 {
                cov[r][c] += d[r] * d[c];
            }
        }
    }
    for r in 0..4 {
        for c in 0..r {
            cov[r][c] = cov[c][r];
        }
    }

    // Initial principal axis along diagonal of bounding box
    let mut dir = [
        max_c[0] - min_c[0],
        max_c[1] - min_c[1],
        max_c[2] - min_c[2],
        max_c[3] - min_c[3],
    ];
    let norm0 = (dir[0] * dir[0] + dir[1] * dir[1] + dir[2] * dir[2] + dir[3] * dir[3]).sqrt();
    if norm0 > 1e-6 {
        for x in &mut dir {
            *x /= norm0;
        }
        // 6 power iterations on 4x4 covariance matrix
        for _ in 0..6 {
            let mut next = [0.0f32; 4];
            for r in 0..4 {
                next[r] = cov[r][0] * dir[0]
                    + cov[r][1] * dir[1]
                    + cov[r][2] * dir[2]
                    + cov[r][3] * dir[3];
            }
            let n = (next[0] * next[0]
                + next[1] * next[1]
                + next[2] * next[2]
                + next[3] * next[3])
            .sqrt();
            if n > 1e-6 {
                for r in 0..4 {
                    dir[r] = next[r] / n;
                }
            } else {
                break;
            }
        }
    } else {
        dir = [1.0, 0.0, 0.0, 0.0];
    }

    let mut min_t = f32::MAX;
    let mut max_t = f32::MIN;
    for p in &pf {
        let t = (p[0] - mean[0]) * dir[0]
            + (p[1] - mean[1]) * dir[1]
            + (p[2] - mean[2]) * dir[2]
            + (p[3] - mean[3]) * dir[3];
        if t < min_t {
            min_t = t;
        }
        if t > max_t {
            max_t = t;
        }
    }

    let mut e0 = [0.0f32; 4];
    let mut e1 = [0.0f32; 4];
    for c in 0..4 {
        e0[c] = (mean[c] + min_t * dir[c]).clamp(0.0, 255.0);
        e1[c] = (mean[c] + max_t * dir[c]).clamp(0.0, 255.0);
    }

    // Two least-squares refinement passes over 4-bit weights
    for _ in 0..2 {
        let diff = [e1[0] - e0[0], e1[1] - e0[1], e1[2] - e0[2], e1[3] - e0[3]];
        let denom = diff[0] * diff[0] + diff[1] * diff[1] + diff[2] * diff[2] + diff[3] * diff[3];
        if denom < 1e-5 {
            break;
        }
        let inv_denom = 1.0 / denom;

        let mut a00 = 0.0f32;
        let mut a01 = 0.0f32;
        let mut a11 = 0.0f32;
        let mut b0 = [0.0f32; 4];
        let mut b1 = [0.0f32; 4];

        for p in &pf {
            let t = ((p[0] - e0[0]) * diff[0]
                + (p[1] - e0[1]) * diff[1]
                + (p[2] - e0[2]) * diff[2]
                + (p[3] - e0[3]) * diff[3])
                * inv_denom;
            let idx = (t * 15.0 + 0.5).clamp(0.0, 15.0) as usize;
            let w = BC7_WEIGHTS4_F[idx];
            let iw = 1.0 - w;
            a00 += iw * iw;
            a01 += iw * w;
            a11 += w * w;
            for c in 0..4 {
                b0[c] += iw * p[c];
                b1[c] += w * p[c];
            }
        }

        let det = a00 * a11 - a01 * a01;
        if det.abs() > 1e-5 {
            let inv_det = 1.0 / det;
            for c in 0..4 {
                e0[c] = ((a11 * b0[c] - a01 * b1[c]) * inv_det).clamp(0.0, 255.0);
                e1[c] = ((a00 * b1[c] - a01 * b0[c]) * inv_det).clamp(0.0, 255.0);
            }
        }
    }

    // Evaluate all 4 p-bit pairs (p0, p1) and keep lowest integer SSE
    let mut best_err = i32::MAX;
    let mut best_c0 = [0u8; 4];
    let mut best_c1 = [0u8; 4];
    let mut best_p0 = 0u8;
    let mut best_p1 = 0u8;
    let mut best_indices = [0u8; 16];

    for p0 in 0..=1u8 {
        for p1 in 0..=1u8 {
            let c0 = [
                bc7_quantize_channel(e0[0], p0),
                bc7_quantize_channel(e0[1], p0),
                bc7_quantize_channel(e0[2], p0),
                bc7_quantize_channel(e0[3], p0),
            ];
            let c1 = [
                bc7_quantize_channel(e1[0], p1),
                bc7_quantize_channel(e1[1], p1),
                bc7_quantize_channel(e1[2], p1),
                bc7_quantize_channel(e1[3], p1),
            ];
            let q0 = [
                ((c0[0] << 1) | p0) as i32,
                ((c0[1] << 1) | p0) as i32,
                ((c0[2] << 1) | p0) as i32,
                ((c0[3] << 1) | p0) as i32,
            ];
            let q1 = [
                ((c1[0] << 1) | p1) as i32,
                ((c1[1] << 1) | p1) as i32,
                ((c1[2] << 1) | p1) as i32,
                ((c1[3] << 1) | p1) as i32,
            ];

            let mut total_err = 0i32;
            let mut indices = [0u8; 16];
            for i in 0..16 {
                let (idx, err) = bc7_best_index_and_err(pixels[i], q0, q1);
                indices[i] = idx;
                total_err = total_err.saturating_add(err);
                if total_err >= best_err {
                    break;
                }
            }

            if total_err < best_err {
                best_err = total_err;
                best_c0 = c0;
                best_c1 = c1;
                best_p0 = p0;
                best_p1 = p1;
                best_indices = indices;
                if best_err == 0 {
                    break;
                }
            }
        }
        if best_err == 0 {
            break;
        }
    }

    // BC7 Mode 6 anchor constraint: pixel 0 index MSB must be 0 (0..=7).
    if best_indices[0] >= 8 {
        std::mem::swap(&mut best_c0, &mut best_c1);
        std::mem::swap(&mut best_p0, &mut best_p1);
        for idx in &mut best_indices {
            *idx = 15 - *idx;
        }
    }

    let mut bw = BitWriter128::new();
    bw.write_bits(0b1000000, 7); // Mode 6
    for ch in 0..4 {
        bw.write_bits(best_c0[ch] as u32, 7);
        bw.write_bits(best_c1[ch] as u32, 7);
    }
    bw.write_bits(best_p0 as u32, 1);
    bw.write_bits(best_p1 as u32, 1);
    bw.write_bits(best_indices[0] as u32, 3); // Anchor index (3 bits)
    for &idx in &best_indices[1..] {
        bw.write_bits(idx as u32, 4);
    }
    debug_assert_eq!(bw.bit_pos, 128);
    bw.data
}

// ---------------------------------------------------------------------------
// BC1 & BC3 Encoders (Fallback Formats)
// ---------------------------------------------------------------------------

#[inline]
fn rgb_to_565(r: u8, g: u8, b: u8) -> u16 {
    let r5 = (r as u16 >> 3) & 0x1F;
    let g6 = (g as u16 >> 2) & 0x3F;
    let b5 = (b as u16 >> 3) & 0x1F;
    (r5 << 11) | (g6 << 5) | b5
}

#[inline]
fn unpack_565(c: u16) -> [i32; 3] {
    let r5 = ((c >> 11) & 0x1F) as i32;
    let g6 = ((c >> 5) & 0x3F) as i32;
    let b5 = (c & 0x1F) as i32;
    [
        (r5 << 3) | (r5 >> 2),
        (g6 << 2) | (g6 >> 4),
        (b5 << 3) | (b5 >> 2),
    ]
}

fn encode_bc1_rgb_block(pixels: &[[u8; 4]; 16], allow_punchthrough: bool) -> [u8; 8] {
    let has_alpha = allow_punchthrough && pixels.iter().any(|p| p[3] < 128);

    let mut min_rgb = [255u8; 3];
    let mut max_rgb = [0u8; 3];
    for p in pixels {
        if has_alpha && p[3] < 128 {
            continue;
        }
        for c in 0..3 {
            min_rgb[c] = min_rgb[c].min(p[c]);
            max_rgb[c] = max_rgb[c].max(p[c]);
        }
    }
    if min_rgb[0] > max_rgb[0] {
        min_rgb = [0, 0, 0];
        max_rgb = [0, 0, 0];
    }

    // Inset bounding box slightly (1/16) for better perceptual fit
    for c in 0..3 {
        let inset = ((max_rgb[c] as i32 - min_rgb[c] as i32) >> 4) as u8;
        min_rgb[c] = min_rgb[c].saturating_add(inset);
        max_rgb[c] = max_rgb[c].saturating_sub(inset);
    }

    let mut c0 = rgb_to_565(max_rgb[0], max_rgb[1], max_rgb[2]);
    let mut c1 = rgb_to_565(min_rgb[0], min_rgb[1], min_rgb[2]);

    if has_alpha {
        if c0 > c1 {
            std::mem::swap(&mut c0, &mut c1);
        }
        let rgb0 = unpack_565(c0);
        let rgb1 = unpack_565(c1);
        let palette = [
            rgb0,
            rgb1,
            [
                (rgb0[0] + rgb1[0]) / 2,
                (rgb0[1] + rgb1[1]) / 2,
                (rgb0[2] + rgb1[2]) / 2,
            ],
        ];
        let mut mask = 0u32;
        for (i, p) in pixels.iter().enumerate() {
            let idx = if p[3] < 128 {
                3u32
            } else {
                let mut best_i = 0u32;
                let mut best_e = i32::MAX;
                for (k, pal) in palette.iter().enumerate() {
                    let dr = pal[0] - p[0] as i32;
                    let dg = pal[1] - p[1] as i32;
                    let db = pal[2] - p[2] as i32;
                    let e = dr * dr + dg * dg + db * db;
                    if e < best_e {
                        best_e = e;
                        best_i = k as u32;
                    }
                }
                best_i
            };
            mask |= idx << (i * 2);
        }
        let mut out = [0u8; 8];
        out[0..2].copy_from_slice(&c0.to_le_bytes());
        out[2..4].copy_from_slice(&c1.to_le_bytes());
        out[4..8].copy_from_slice(&mask.to_le_bytes());
        return out;
    }

    // 4-color opaque mode requires c0 > c1
    if c0 < c1 {
        std::mem::swap(&mut c0, &mut c1);
    } else if c0 == c1 {
        let mut mask = 0u32;
        if c0 > 0 {
            c1 = c0 - 1;
            // all index 0 -> exact c0
        } else {
            c0 = 1;
            c1 = 0;
            mask = 0x5555_5555; // all index 1 -> exact c1 (0)
        }
        let mut out = [0u8; 8];
        out[0..2].copy_from_slice(&c0.to_le_bytes());
        out[2..4].copy_from_slice(&c1.to_le_bytes());
        out[4..8].copy_from_slice(&mask.to_le_bytes());
        return out;
    }

    let rgb0 = unpack_565(c0);
    let rgb1 = unpack_565(c1);
    let palette = [
        rgb0,
        rgb1,
        [
            (2 * rgb0[0] + rgb1[0] + 1) / 3,
            (2 * rgb0[1] + rgb1[1] + 1) / 3,
            (2 * rgb0[2] + rgb1[2] + 1) / 3,
        ],
        [
            (rgb0[0] + 2 * rgb1[0] + 1) / 3,
            (rgb0[1] + 2 * rgb1[1] + 1) / 3,
            (rgb0[2] + 2 * rgb1[2] + 1) / 3,
        ],
    ];

    let mut mask = 0u32;
    for (i, p) in pixels.iter().enumerate() {
        let mut best_i = 0u32;
        let mut best_e = i32::MAX;
        for (k, pal) in palette.iter().enumerate() {
            let dr = pal[0] - p[0] as i32;
            let dg = pal[1] - p[1] as i32;
            let db = pal[2] - p[2] as i32;
            let e = dr * dr + dg * dg + db * db;
            if e < best_e {
                best_e = e;
                best_i = k as u32;
            }
        }
        mask |= best_i << (i * 2);
    }

    let mut out = [0u8; 8];
    out[0..2].copy_from_slice(&c0.to_le_bytes());
    out[2..4].copy_from_slice(&c1.to_le_bytes());
    out[4..8].copy_from_slice(&mask.to_le_bytes());
    out
}

fn encode_bc4_alpha_block(pixels: &[[u8; 4]; 16]) -> [u8; 8] {
    let mut min_a = 255u8;
    let mut max_a = 0u8;
    for p in pixels {
        min_a = min_a.min(p[3]);
        max_a = max_a.max(p[3]);
    }
    let a0 = max_a;
    let a1 = min_a;
    let mut out = [0u8; 8];
    out[0] = a0;
    out[1] = a1;
    if a0 == a1 {
        return out;
    }

    // 8-level interpolation (a0 > a1)
    let mut pal = [0i32; 8];
    pal[0] = a0 as i32;
    pal[1] = a1 as i32;
    for k in 1..=6 {
        pal[k + 1] = (((7 - k as i32) * (a0 as i32) + (k as i32) * (a1 as i32)) + 3) / 7;
    }

    let mut bits: u64 = 0;
    for (i, p) in pixels.iter().enumerate() {
        let a = p[3] as i32;
        let mut best_k = 0u64;
        let mut best_e = i32::MAX;
        for (k, &pv) in pal.iter().enumerate() {
            let e = (pv - a).abs();
            if e < best_e {
                best_e = e;
                best_k = k as u64;
            }
        }
        bits |= best_k << (i * 3);
    }
    let bytes = bits.to_le_bytes();
    out[2..8].copy_from_slice(&bytes[0..6]);
    out
}

fn encode_bc3_block(pixels: &[[u8; 4]; 16]) -> [u8; 16] {
    let alpha = encode_bc4_alpha_block(pixels);
    let color = encode_bc1_rgb_block(pixels, false);
    let mut out = [0u8; 16];
    out[0..8].copy_from_slice(&alpha);
    out[8..16].copy_from_slice(&color);
    out
}

// ---------------------------------------------------------------------------
// Parallel Mip Level Compression + DDS Serialization
// ---------------------------------------------------------------------------

fn encode_mip_level(img: &ImageRgba, format: OutputFormat, threads: usize) -> Vec<u8> {
    if format == OutputFormat::Rgba8 {
        return img.pixels.clone();
    }

    let bx_count = ((img.width as usize) + 3) / 4;
    let by_count = ((img.height as usize) + 3) / 4;
    let block_bytes = format.block_bytes();
    let row_bytes = bx_count * block_bytes;
    let mut output = vec![0u8; by_count * row_bytes];

    let workers = threads.min(by_count).max(1);
    if workers == 1 || by_count < 4 {
        for by in 0..by_count {
            let row_slice = &mut output[by * row_bytes..(by + 1) * row_bytes];
            for bx in 0..bx_count {
                let block = extract_4x4_rgba(img, bx, by);
                let dst = &mut row_slice[bx * block_bytes..(bx + 1) * block_bytes];
                match format {
                    OutputFormat::Bc7 => dst.copy_from_slice(&encode_bc7_mode6_block(&block)),
                    OutputFormat::Bc3 => dst.copy_from_slice(&encode_bc3_block(&block)),
                    OutputFormat::Bc1 => dst.copy_from_slice(&encode_bc1_rgb_block(&block, true)),
                    OutputFormat::Rgba8 => unreachable!(),
                }
            }
        }
        return output;
    }

    let rows_per_worker = (by_count + workers - 1) / workers;
    thread::scope(|s| {
        for (worker_idx, chunk) in output.chunks_mut(rows_per_worker * row_bytes).enumerate() {
            let start_by = worker_idx * rows_per_worker;
            let num_rows = chunk.len() / row_bytes;
            s.spawn(move || {
                for local_y in 0..num_rows {
                    let by = start_by + local_y;
                    let row_slice = &mut chunk[local_y * row_bytes..(local_y + 1) * row_bytes];
                    for bx in 0..bx_count {
                        let block = extract_4x4_rgba(img, bx, by);
                        let dst = &mut row_slice[bx * block_bytes..(bx + 1) * block_bytes];
                        match format {
                            OutputFormat::Bc7 => {
                                dst.copy_from_slice(&encode_bc7_mode6_block(&block));
                            }
                            OutputFormat::Bc3 => {
                                dst.copy_from_slice(&encode_bc3_block(&block));
                            }
                            OutputFormat::Bc1 => {
                                dst.copy_from_slice(&encode_bc1_rgb_block(&block, true));
                            }
                            OutputFormat::Rgba8 => unreachable!(),
                        }
                    }
                }
            });
        }
    });

    output
}

fn push_u32(buf: &mut Vec<u8>, v: u32) {
    buf.extend_from_slice(&v.to_le_bytes());
}

fn build_dds_header(
    width: u32,
    height: u32,
    mip_count: u32,
    format: OutputFormat,
    srgb: bool,
    force_dx10: bool,
) -> Vec<u8> {
    let use_dx10 = format.uses_dx10_by_default(force_dx10, srgb);
    let mut header = Vec::with_capacity(if use_dx10 { 148 } else { 128 });

    // Magic "DDS "
    header.extend_from_slice(&DDS_MAGIC);

    // DDS_HEADER (124 bytes)
    push_u32(&mut header, DDS_HEADER_SIZE);

    let is_compressed = format != OutputFormat::Rgba8;
    let mut flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT;
    if mip_count > 1 {
        flags |= DDSD_MIPMAPCOUNT;
    }
    flags |= if is_compressed { DDSD_LINEARSIZE } else { DDSD_PITCH };
    push_u32(&mut header, flags);

    push_u32(&mut header, height);
    push_u32(&mut header, width);

    let pitch_or_linear = if is_compressed {
        ((width + 3) / 4).max(1) * ((height + 3) / 4).max(1) * (format.block_bytes() as u32)
    } else {
        width * 4
    };
    push_u32(&mut header, pitch_or_linear);
    push_u32(&mut header, 0); // dwDepth
    push_u32(&mut header, mip_count);
    for _ in 0..11 {
        push_u32(&mut header, 0); // dwReserved1[11]
    }

    // DDS_PIXELFORMAT (32 bytes)
    push_u32(&mut header, DDS_PIXELFORMAT_SIZE);
    if use_dx10 {
        push_u32(&mut header, DDPF_FOURCC);
        header.extend_from_slice(b"DX10");
        push_u32(&mut header, 0); // dwRGBBitCount
        push_u32(&mut header, 0);
        push_u32(&mut header, 0);
        push_u32(&mut header, 0);
        push_u32(&mut header, 0);
    } else {
        match format {
            OutputFormat::Bc1 => {
                push_u32(&mut header, DDPF_FOURCC);
                header.extend_from_slice(b"DXT1");
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
            }
            OutputFormat::Bc3 => {
                push_u32(&mut header, DDPF_FOURCC);
                header.extend_from_slice(b"DXT5");
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
                push_u32(&mut header, 0);
            }
            OutputFormat::Rgba8 => {
                push_u32(&mut header, DDPF_RGB | DDPF_ALPHAPIXELS);
                push_u32(&mut header, 0);
                push_u32(&mut header, 32);
                push_u32(&mut header, 0x0000_00FF);
                push_u32(&mut header, 0x0000_FF00);
                push_u32(&mut header, 0x00FF_0000);
                push_u32(&mut header, 0xFF00_0000);
            }
            OutputFormat::Bc7 => unreachable!(),
        }
    }

    let mut caps = DDSCAPS_TEXTURE;
    if mip_count > 1 {
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP;
    }
    push_u32(&mut header, caps);
    push_u32(&mut header, 0); // dwCaps2
    push_u32(&mut header, 0); // dwCaps3
    push_u32(&mut header, 0); // dwCaps4
    push_u32(&mut header, 0); // dwReserved2

    if use_dx10 {
        push_u32(&mut header, format.dxgi_format(srgb));
        push_u32(&mut header, D3D10_RESOURCE_DIMENSION_TEXTURE2D);
        push_u32(&mut header, 0); // miscFlag
        push_u32(&mut header, 1); // arraySize
        push_u32(&mut header, 0); // miscFlags2
    }

    header
}

fn write_dds(
    out_path: &Path,
    width: u32,
    height: u32,
    mip_payloads: &[Vec<u8>],
    format: OutputFormat,
    srgb: bool,
    force_dx10: bool,
) -> Result<usize, String> {
    if let Some(parent) = out_path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("Failed to create {}: {e}", parent.display()))?;
        }
    }

    let header = build_dds_header(
        width,
        height,
        mip_payloads.len() as u32,
        format,
        srgb,
        force_dx10,
    );
    let mut writer = BufWriter::new(
        File::create(out_path)
            .map_err(|e| format!("Failed to create {}: {e}", out_path.display()))?,
    );
    writer
        .write_all(&header)
        .map_err(|e| format!("Write header failed: {e}"))?;
    let mut total = header.len();
    for mip in mip_payloads {
        writer
            .write_all(mip)
            .map_err(|e| format!("Write mip failed: {e}"))?;
        total += mip.len();
    }
    writer
        .flush()
        .map_err(|e| format!("Flush failed: {e}"))?;
    Ok(total)
}

fn run() -> Result<(), String> {
    let args = parse_args()?;
    let img = load_image(&args)?;
    let width = img.width;
    let height = img.height;
    let mips = generate_mip_chain(img, args.mipmaps, args.srgb);
    let mut payloads = Vec::with_capacity(mips.len());
    for mip in &mips {
        payloads.push(encode_mip_level(mip, args.format, args.threads));
    }
    let total_bytes = write_dds(
        &args.output,
        width,
        height,
        &payloads,
        args.format,
        args.srgb,
        args.force_dx10,
    )?;

    if args.verbose {
        println!(
            "Encoded {} ({}x{}, {} mips, format={:?}, dxgi={}, {} bytes) -> {}",
            args.input.display(),
            width,
            height,
            mips.len(),
            args.format,
            args.format.dxgi_format(args.srgb),
            total_bytes,
            args.output.display()
        );
    }
    Ok(())
}

fn main() {
    if let Err(err) = run() {
        eprintln!("rockport_dds error: {err}");
        process::exit(1);
    }
}

// ---------------------------------------------------------------------------
// Unit Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn decode_bc7_mode6_block(block: &[u8; 16]) -> [[u8; 4]; 16] {
        struct BitReader<'a> {
            data: &'a [u8; 16],
            bit_pos: usize,
        }
        impl<'a> BitReader<'a> {
            fn read_bits(&mut self, count: usize) -> u32 {
                let mut val = 0u32;
                for i in 0..count {
                    let b = (self.data[self.bit_pos >> 3] >> (self.bit_pos & 7)) & 1;
                    val |= (b as u32) << i;
                    self.bit_pos += 1;
                }
                val
            }
        }

        let mut br = BitReader { data: block, bit_pos: 0 };
        let mode = br.read_bits(7);
        assert_eq!(mode, 0b1000000, "Expected BC7 Mode 6");
        let r0 = br.read_bits(7) as i32;
        let r1 = br.read_bits(7) as i32;
        let g0 = br.read_bits(7) as i32;
        let g1 = br.read_bits(7) as i32;
        let b0 = br.read_bits(7) as i32;
        let b1 = br.read_bits(7) as i32;
        let a0 = br.read_bits(7) as i32;
        let a1 = br.read_bits(7) as i32;
        let p0 = br.read_bits(1) as i32;
        let p1 = br.read_bits(1) as i32;

        let e0 = [(r0 << 1) | p0, (g0 << 1) | p0, (b0 << 1) | p0, (a0 << 1) | p0];
        let e1 = [(r1 << 1) | p1, (g1 << 1) | p1, (b1 << 1) | p1, (a1 << 1) | p1];

        let mut out = [[0u8; 4]; 16];
        for i in 0..16 {
            let idx = if i == 0 {
                let a = br.read_bits(3) as usize;
                assert!(a < 8);
                a
            } else {
                br.read_bits(4) as usize
            };
            let w = BC7_WEIGHTS4[idx];
            let iw = 64 - w;
            for c in 0..4 {
                out[i][c] = ((iw * e0[c] + w * e1[c] + 32) >> 6) as u8;
            }
        }
        assert_eq!(br.bit_pos, 128);
        out
    }

    #[test]
    fn test_bc7_mode6_uniform_block_exact() {
        let px = [171u8, 85, 213, 255]; // all odd LSBs -> exact representation
        let block = [px; 16];
        let encoded = encode_bc7_mode6_block(&block);
        let decoded = decode_bc7_mode6_block(&encoded);
        for d in decoded {
            assert_eq!(d, px);
        }
    }

    #[test]
    fn test_bc7_mode6_gradient_roundtrip_quality() {
        let mut block = [[0u8; 4]; 16];
        for i in 0..16 {
            let v = (i * 17) as u8;
            block[i] = [v, 255 - v, (v / 2).saturating_add(40), 255 - (i as u8) * 12];
        }
        let encoded = encode_bc7_mode6_block(&block);
        let decoded = decode_bc7_mode6_block(&encoded);
        let mut max_diff = 0i32;
        for i in 0..16 {
            for c in 0..4 {
                max_diff = max_diff.max((block[i][c] as i32 - decoded[i][c] as i32).abs());
            }
        }
        assert!(
            max_diff <= 6,
            "BC7 Mode 6 gradient max channel error too high: {max_diff}"
        );
    }

    #[test]
    fn test_bc7_dds_header_and_mipmap_byte_sizes() {
        let w = 32u32;
        let h = 16u32;
        let mut pixels = vec![0u8; (w * h * 4) as usize];
        for y in 0..h {
            for x in 0..w {
                let off = ((y * w + x) * 4) as usize;
                pixels[off] = (x * 8) as u8;
                pixels[off + 1] = (y * 16) as u8;
                pixels[off + 2] = 128;
                pixels[off + 3] = 255;
            }
        }
        let img = ImageRgba {
            width: w,
            height: h,
            pixels,
        };
        let chain = generate_mip_chain(img, true, false);
        assert_eq!(chain.len(), 6); // 32x16, 16x8, 8x4, 4x2, 2x1, 1x1

        let header = build_dds_header(w, h, chain.len() as u32, OutputFormat::Bc7, false, false);
        assert_eq!(header.len(), 148);
        assert_eq!(&header[0..4], b"DDS ");
        assert_eq!(u32::from_le_bytes(header[4..8].try_into().unwrap()), 124);
        assert_eq!(u32::from_le_bytes(header[12..16].try_into().unwrap()), h);
        assert_eq!(u32::from_le_bytes(header[16..20].try_into().unwrap()), w);
        assert_eq!(u32::from_le_bytes(header[28..32].try_into().unwrap()), 6);
        assert_eq!(&header[84..88], b"DX10");
        assert_eq!(
            u32::from_le_bytes(header[128..132].try_into().unwrap()),
            DXGI_FORMAT_BC7_UNORM
        );

        let mut total_payload = 0usize;
        let mut expected = 0usize;
        for mip in &chain {
            let enc = encode_mip_level(mip, OutputFormat::Bc7, 2);
            let blocks = (((mip.width + 3) / 4).max(1) * ((mip.height + 3) / 4).max(1)) as usize;
            assert_eq!(enc.len(), blocks * 16);
            total_payload += enc.len();
            expected += blocks * 16;
        }
        assert_eq!(total_payload, expected);
    }

    #[test]
    fn test_parallel_determinism() {
        let w = 64u32;
        let h = 64u32;
        let mut pixels = vec![0u8; (w * h * 4) as usize];
        for i in 0..(w * h) as usize {
            pixels[i * 4] = (i % 251) as u8;
            pixels[i * 4 + 1] = ((i * 7) % 251) as u8;
            pixels[i * 4 + 2] = ((i * 13) % 251) as u8;
            pixels[i * 4 + 3] = 255;
        }
        let img = ImageRgba {
            width: w,
            height: h,
            pixels,
        };
        let single = encode_mip_level(&img, OutputFormat::Bc7, 1);
        let multi = encode_mip_level(&img, OutputFormat::Bc7, 4);
        assert_eq!(single, multi);
    }

    #[test]
    fn test_tga_and_pam_decoders() {
        // Minimal 2x2 32-bit top-down TGA
        let mut tga = vec![0u8; 18 + 16];
        tga[2] = 2; // uncompressed true-color
        tga[12..14].copy_from_slice(&2u16.to_le_bytes());
        tga[14..16].copy_from_slice(&2u16.to_le_bytes());
        tga[16] = 32;
        tga[17] = 0x20; // top-down
        // pixel 0: BGRA = (10, 20, 30, 255) -> RGBA (30, 20, 10, 255)
        tga[18..22].copy_from_slice(&[10, 20, 30, 255]);
        let decoded_tga = decode_tga(&tga).unwrap();
        assert_eq!(decoded_tga.width, 2);
        assert_eq!(decoded_tga.height, 2);
        assert_eq!(&decoded_tga.pixels[0..4], &[30, 20, 10, 255]);

        // Minimal 2x1 PAM P7 RGBA
        let mut pam = b"P7\nWIDTH 2\nHEIGHT 1\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n".to_vec();
        pam.extend_from_slice(&[1, 2, 3, 4, 250, 240, 230, 200]);
        let decoded_pam = decode_pam(&pam).unwrap();
        assert_eq!(decoded_pam.width, 2);
        assert_eq!(decoded_pam.height, 1);
        assert_eq!(&decoded_pam.pixels, &[1, 2, 3, 4, 250, 240, 230, 200]);
    }
}
