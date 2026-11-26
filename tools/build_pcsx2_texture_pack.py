#!/usr/bin/env python3
"""Offline District-by-District Faithful 4x/8x BC7 HD Texture Pack Builder (v2.2).

Builds zero-AI-hallucination linear ``DXGI_FORMAT_BC7_UNORM`` (98) mipmapped
PCSX2 v2.6.3 texture replacement packs directly from the untouched PS2 NFSMW
(SLUS-21351) ISO into gitignored ``build/pcsx2_textures_v22/SLUS-21351/replacements/``.
"""

from __future__ import annotations

import argparse, collections, concurrent.futures, hashlib, json, os, struct, subprocess, sys, tempfile, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
import install_macos_texture_tools as imtt, pcsx2_tex_hash as pth, texture_relief_3d as tr3d  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

DEFAULT_OUT_DIR = _REPO_ROOT / "build" / "pcsx2_textures_v25" / "SLUS-21351" / "replacements"
DEFAULT_FAITHFUL_OUT_DIR = _REPO_ROOT / "build" / "pcsx2_textures_v22" / "SLUS-21351" / "replacements"
RELIEF_MODE_CHOICES: Tuple[str, ...] = tr3d.RELIEF_MODES
DISTRICT_CHOICES: Tuple[str, ...] = ("rosewood", "downtown", "camden", "highway99", "all")
DXGI_FORMAT_BC7_UNORM = 98
DENYLIST_ARCHIVE_TOKENS: Tuple[str, ...] = (
    "HUDTEXRACE", "HUDTEXDRAG", "HUDTEXSPLIT", "HUDTEXDRAGSPLIT", "HUDTEXTURESPHOTOFINISH",
    "HUDS_CUSTOM", "FRONTEND", "LANGUAGETEXTURES", "MINI_MAP", "TRACKMAPS", "VINYLS",
)
DENYLIST_NAME_PREFIXES: Tuple[str, ...] = (
    "HUD", "FE_", "FONT", "MINIMAP", "LOADING", "LOAD_", "FLARE", "SHADOW", "ENV", "UI_",
    "ICON", "CURSOR", "ARROW", "RADAR", "METER", "PDA", "BKGBMP", "DEMO_", "TV_MASK",
    "SUNCENTER", "SUNRING", "SUNHALO", "SUNMAJORRAYS", "REFSKY", "REFLECT", "SPECULAR",
    "CHROME", "GLOW", "LENSFLARE", "SPARK", "PARTICLE", "FX_", "TIP_", "DPAD", "RVM",
    "DRAG_RPM", "BUSTED_", "DEFEATED_", "EVADED_", "IMPOUNDED_", "MILESTONE_", "BOUNTY_",
    "BUTTON_", "LEFT_ANALOG", "RIGHT_ANALOG", "GENERIC_AUTOSAVE", "VISIBILITY_", "MW_LOGO", "COP_LIGHT",
)
DENYLIST_NAME_SUBSTRINGS: Tuple[str, ...] = (
    "HUDTEXRACE", "HUDTEXDRAG", "FRONTEND", "LOADING", "LOAD_", "MINIMAP", "FONT",
    "FLARE", "SHADOW", "ENVMAP", "WINDOWREFLECTION", "REFLECTION", "_ICON", "FX_",
    "TIP_", "DPAD", "ADDITIVE", "_XPOS", "_XNEG", "_YPOS", "_YNEG", "_ZPOS", "_ZNEG",
)
GLOBAL_ALLOWLIST: Dict[str, Tuple[str, ...]] = {
    r"GLOBAL\DYNTEX.BIN": ("SKY_",),
    r"GLOBAL\GLOBALB.LZC": (
        "CARBONFIBRE", "CARBOTTOM", "DAMAGE0", "DOORLINE", "DRIVER_CUTOUT", "GRILL_",
        "INTERCOOLER", "LICENSEPLATE", "METAL_SWATCH", "RADIATOR", "REAR_DEFROSTER",
        "ROTOR", "RUBBERACCORDIAN", "TIRE_BACK", "TRAFFIC_TIRE", "TREAD",
        "WINDOW_DAMAGE0", "WINDOW_FRONT", "WINDOW_MASK",
    ),
    r"GLOBAL\INGAMEA.BUN": (
        "DEBRISA", "DRIVER_COP", "HELICOPTER_BLADES", "LEAF3",
        "OBJ_BARREL_B", "ROADBLOCK_BARREL", "SKID_", "TIREPATCH",
    ),
}
_DISTRICT_LETTERS: Dict[str, Set[str]] = {
    "rosewood": {"A", "F", "G", "H", "I", "J"}, "downtown": {"B", "C", "D", "E", "L", "M", "T"},
    "camden": {"O", "P", "Q", "R", "S"}, "highway99": {"I", "D", "T", "L"},
}
_DISTRICT_HINTS: Dict[str, Tuple[str, ...]] = {
    "rosewood": ("_CT_", "ROSEWOOD", "CAMPUS", "HIGHLANDS", "GOLF", "HERITAGE"),
    "downtown": ("_TC_", "DOWNTOWN", "CITYCENTER", "EASTPARK", "WESTPARK", "STADIUM"),
    "camden": ("_CP_", "CAMDEN", "OILREF", "ASYLUM", "BEACH", "DOCK", "SHIPYARD"),
    "highway99": ("HWY", "FWY", "INTERCHAN", "EXIT"),
}


def is_denylisted_texture(name: str, archive: str = "") -> Tuple[bool, str]:
    """Return ``(blocked, reason)`` under the hard UI/loading-icon/FX denylist & GLOBAL allowlist."""
    arc_u, nm = archive.replace("/", "\\").upper(), name.strip().upper()
    for tok in DENYLIST_ARCHIVE_TOKENS:
        if tok in arc_u: return True, f"archive:{tok}"
    if not nm: return True, "empty_name"
    for pfx in DENYLIST_NAME_PREFIXES:
        if nm.startswith(pfx): return True, f"prefix:{pfx}"
    for sub in DENYLIST_NAME_SUBSTRINGS:
        if sub in nm: return True, f"substr:{sub}"
    if arc_u.startswith("GLOBAL\\"):
        allowed = GLOBAL_ALLOWLIST.get(arc_u)
        if not allowed or not any(nm == a or nm.startswith(a) for a in allowed):
            return True, f"global_not_allowlisted:{arc_u}"
    return False, ""


def section_matches_district(section_name: str, tex_names: Sequence[str], district: str) -> bool:
    if district == "all": return True
    s = section_name.strip().upper()
    if not s: return False
    if s[0] in _DISTRICT_LETTERS.get(district, set()) or s in ("W0", "Y0"): return True
    return s[0] in ("W", "Y", "V") and any(any(h in nm.upper() for h in _DISTRICT_HINTS[district]) for nm in tex_names)


def classify_material(name: str, archive: str = "") -> str:
    nm, arc_u = name.strip().upper(), archive.replace("/", "\\").upper()
    if nm.startswith(("SGN_", "ADS_")) or any(t in nm for t in ("_SIGN", "_PLATE", "LICENSE", "BADGE", "LOGO")): return "signage"
    if nm.startswith(("SKY_", "PAN_")): return "sky"
    return "car" if (arc_u.startswith("CARS\\") or nm.startswith(("CAR_", "WHEEL", "TIRE", "BRAKE", "RIM"))) else "world_surface"


def dilate_alpha_edge_rgb(rgb: np.ndarray, alpha: np.ndarray, threshold: int = 16, iters: int = 6) -> np.ndarray:
    """Outward alpha-edge RGB dilation into transparent pixels to prevent cutout halo fringes."""
    out, valid = rgb.astype(np.float32, copy=True), alpha >= threshold
    if valid.all() or not valid.any():
        return rgb.copy()
    h, w = valid.shape
    for _ in range(iters):
        if valid.all():
            break
        acc, cnt = np.zeros_like(out), np.zeros((h, w), dtype=np.float32)
        prgb, pv = np.pad(out, ((1, 1), (1, 1), (0, 0)), mode="edge"), np.pad(valid, ((1, 1), (1, 1)), mode="edge")
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    nv = pv[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
                    acc += prgb[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w] * nv[:, :, None]
                    cnt += nv.astype(np.float32)
        fill = (~valid) & (cnt > 0.0)
        if not fill.any():
            break
        out[fill] = acc[fill] / cnt[fill, None]
        valid |= fill
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def normalize_ps2_alpha_u8(src_alpha: np.ndarray, target_w: int, target_h: int) -> Tuple[np.ndarray, str]:
    """Upscale and strictly enforce PS2 GS [0..128] alpha fidelity (opaque locked to 128)."""
    src_gs = np.clip((src_alpha.astype(np.uint16) + 1) // 2, 0, 128).astype(np.uint8) if int(src_alpha.max()) > 132 else np.clip(src_alpha, 0, 128).astype(np.uint8)
    a_min, a_max = int(src_gs.min()), int(src_gs.max())
    if a_min >= 120:
        return np.full((target_h, target_w), 128, dtype=np.uint8), "opaque_128"
    up_a = np.asarray(Image.fromarray(src_gs).resize((target_w, target_h), Image.Resampling.BILINEAR), dtype=np.uint8)
    return np.clip(up_a, 0, min(128, max(a_max, 128))).astype(np.uint8), ("cutout_0_128" if (a_min <= 8 and a_max >= 96) else "mask_0_128")


def _box_blur_3x3(arr: np.ndarray) -> np.ndarray:
    p = np.pad(arr, ((1, 1), (1, 1)), mode="edge")
    return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] + p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:] + p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) * (1.0 / 9.0)


def compute_target_size(orig_w: int, orig_h: int, scale: int = 4, small_scale: int = 8, max_size: int = 1024) -> Tuple[int, int]:
    eff = small_scale if (small_scale > scale and max(orig_w, orig_h) <= 32) else scale
    while max(orig_w, orig_h) * eff > max_size and eff > 1:
        eff //= 2
    return min(max_size, max(4, orig_w * eff)), min(max_size, max(4, orig_h * eff))


def enhance_faithful_rgba(
    rgba_u8: np.ndarray, *, name: str, archive: str = "", scale: int = 4, small_scale: int = 8, max_size: int = 1024,
) -> Tuple[np.ndarray, str, str]:
    """Deterministically enhance RGBA while preserving 2005 palette, signage & [0..128] alpha."""
    src_h, src_w = rgba_u8.shape[:2]
    tw, th = compute_target_size(src_w, src_h, scale=scale, small_scale=small_scale, max_size=max_size)
    mat, src_rgb, src_a = classify_material(name, archive), rgba_u8[:, :, :3], rgba_u8[:, :, 3]
    if int(src_a.min()) <= 8 and int(src_a.max()) >= 96:
        src_rgb = dilate_alpha_edge_rgb(src_rgb, src_a, threshold=16, iters=6)
    up_rgb = np.asarray(Image.fromarray(src_rgb).resize((tw, th), Image.Resampling.LANCZOS), dtype=np.float32) * (1.0 / 255.0)
    up_a, alpha_mode = normalize_ps2_alpha_u8(src_a, tw, th)
    luma = 0.299 * up_rgb[:, :, 0] + 0.587 * up_rgb[:, :, 1] + 0.114 * up_rgb[:, :, 2]

    if mat == "sky":
        blur = _box_blur_3x3(_box_blur_3x3(luma))
        grad = np.abs(np.diff(luma, axis=1, prepend=luma[:, :1])) + np.abs(np.diff(luma, axis=0, prepend=luma[:1, :]))
        delta = (blur - luma) * np.clip(1.0 - grad * 18.0, 0.0, 1.0) * 0.45
    elif mat in ("signage", "car"):
        gain = 0.42 if mat == "signage" else 0.35
        delta = np.clip(gain * 0.055 * np.tanh((luma - _box_blur_3x3(luma)) / 0.055), -0.045, 0.045)
    else:
        blur = _box_blur_3x3(luma)
        gx, gy = np.diff(blur, axis=1, prepend=blur[:, :1]), np.diff(blur, axis=0, prepend=blur[:1, :])
        edge_prot = np.clip(1.0 - np.hypot(gx, gy) * 6.0, 0.25, 1.0)
        seed = int.from_bytes(hashlib.blake2b(name.encode("utf-8"), digest_size=4).digest(), "little")
        tile_h, tile_w = min(th, 128), min(tw, 128)
        g0 = np.random.default_rng(seed).normal(0.0, 1.0, size=(tile_h, tile_w)).astype(np.float32)
        grain = np.tile(0.65 * g0 + 0.35 * _box_blur_3x3(g0), ((th + tile_h - 1) // tile_h, (tw + tile_w - 1) // tile_w))[:th, :tw]
        delta = np.clip(0.48 * (0.05 * np.tanh((luma - blur) / 0.05)) + (-gx * 0.16 - gy * 0.16 + grain * 0.014) * edge_prot, -0.038, 0.038)

    out_rgb = np.clip(np.round((up_rgb + delta[:, :, None]) * 255.0), 0, 255).astype(np.uint8)
    if alpha_mode == "cutout_0_128":
        out_rgb = dilate_alpha_edge_rgb(out_rgb, up_a, threshold=16, iters=4)
    return np.dstack([out_rgb, up_a]), mat, alpha_mode


def enhance_texture_rgba(
    rgba_u8: np.ndarray, *, name: str, archive: str = "", scale: int = 4, small_scale: int = 8,
    max_size: int = 1024, relief_mode: str = "sculpted",
) -> Tuple[np.ndarray, str, str]:
    """Dispatch texture enhancement between v2.2 faithful mode and v2.5 sculpted 3D relief mode."""
    if relief_mode == "faithful":
        return enhance_faithful_rgba(rgba_u8, name=name, archive=archive, scale=scale, small_scale=small_scale, max_size=max_size)
    if relief_mode == "sculpted":
        return tr3d.sculpt_texture_relief_rgba(rgba_u8, name=name, archive=archive, scale=scale, small_scale=small_scale, max_size=max_size)
    raise ValueError(f"Unsupported relief_mode: {relief_mode!r}")


def build_bc7_dds_header(width: int, height: int, mip_count: int) -> bytes:
    """Build exact 148-byte DDS + DDS_HEADER_DXT10 header for DXGI_FORMAT_BC7_UNORM (98)."""
    flags = 0x81007 | (0x20000 if mip_count > 1 else 0)
    top_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    caps = 0x1000 | (0x400008 if mip_count > 1 else 0)
    return (
        b"DDS " + struct.pack("<7I", 124, flags, height, width, top_size, 0, mip_count) + b"\x00" * 44
        + struct.pack("<II4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
        + struct.pack("<5I", caps, 0, 0, 0, 0) + struct.pack("<5I", DXGI_FORMAT_BC7_UNORM, 3, 0, 1, 0)
    )


def encode_batch_to_dds(
    items: Sequence[Tuple[List[Tuple[str, int]], np.ndarray]], out_dir: Path, encoder_bin: Path, threads: int = 0,
) -> Tuple[int, int]:
    """Batch-encode RGBA arrays via ``rockport_dds --batch`` and emit DX10 BC7_UNORM DDS files."""
    if not items:
        return 0, 0
    out_dir.mkdir(parents=True, exist_ok=True)
    fw, bw = 0, 0
    with tempfile.TemporaryDirectory(prefix="rp_v22_") as tmp:
        in_p, out_p = Path(tmp) / "batch.rkb1", Path(tmp) / "batch.rko1"
        buf = bytearray(b"RKB1") + bytearray(struct.pack("<I", len(items)))
        for _targets, rgba in items:
            h, w = rgba.shape[:2]
            raw = np.ascontiguousarray(rgba, dtype=np.uint8).tobytes()
            buf += struct.pack("<4I", w, h, 1, len(raw)) + raw
        in_p.write_bytes(buf)
        cmd = [str(encoder_bin), "--batch", str(in_p), "--output", str(out_p), "--format", "bc7", "--mipmaps"] + (["-j", str(threads)] if threads > 0 else [])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"rockport_dds batch failed: {proc.stderr.strip() or proc.stdout.strip()}")
        out_data = out_p.read_bytes()
        if len(out_data) < 8 or out_data[:4] != b"RKO1" or struct.unpack_from("<I", out_data, 4)[0] != len(items):
            raise RuntimeError("Invalid RKO1 batch output")
        pos = 8
        for targets, _rgba in items:
            w, h, mips, plen = struct.unpack_from("<4I", out_data, pos)
            payload, pos = out_data[pos + 16 : pos + 16 + plen], pos + 16 + plen
            for stem, shift in targets:
                tw, th, tm, off = w, h, mips, 0
                for _ in range(shift):
                    if tm <= 1:
                        break
                    off += max(1, (tw + 3) // 4) * max(1, (th + 3) // 4) * 16
                    tw, th, tm = max(1, tw // 2), max(1, th // 2), tm - 1
                dds = build_bc7_dds_header(tw, th, tm) + payload[off:]
                (out_dir / f"{stem}.dds").write_bytes(dds)
                fw += 1; bw += len(dds)
    return fw, bw


def collect_blocked_archive_stems(arc: PS2NFSMWArchive, blocked_stems: Set[str]) -> None:
    """Hash all protected UI/FRONTEND/HUD/TRACKMAP/GLOBAL-non-allowlisted textures into blocked_stems."""
    protected_paths = sorted({
        e.resolved_path for e in arc.entries
        if e.resolved_path and (
            e.resolved_path.startswith(("FRONTEND\\", "LANGUAGES\\"))
            or any(tok in e.resolved_path for tok in ("HUDTEX", "HUDS_CUSTOM", "TRACKMAPS", "MINI_MAP"))
            or e.resolved_path in (r"GLOBAL\GLOBALA.BUN", r"GLOBAL\INGAMEB.LZC", r"GLOBAL\DEMO_MARKETING.BUN", r"GLOBAL\WIDESCREEN_GLOBAL.BUN", r"GLOBAL\THINSCREEN_GLOBAL.BUN")
        )
    })
    for vp in protected_paths:
        try:
            for tex in pth.iter_tpk_raw_textures(arc.read_entry(arc.find(vp)), vp):
                blocked_stems.update(pth.compute_pcsx2_stems(tex, include_sub_mips=True))
        except Exception:
            continue


def filter_stage2_stem(stem: str, seen_stems: Set[str], blocked_stems: Set[str]) -> Tuple[bool, str]:
    """Gate a Stage-2 dump stem against Stage-1 seen stems and denylisted blocked_stems."""
    parts = stem.split("-")
    if len(parts) not in (2, 3) or len(parts[-1]) != 8: return False, "stage2_invalid_stem"
    if stem in seen_stems: return False, "stage2_already_covered"
    if stem in blocked_stems: return False, "stage2_blocked_stem"
    return True, ""


def _default_iso() -> Optional[Path]:
    env = os.environ.get("NFSMW_ISO", "").strip()
    p = Path(env).expanduser() if env else (Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso")
    return p if p.is_file() else None


def build_texture_pack(
    *, iso_path: Path, out_dir: Path, district: str = "all", include_global: bool = True,
    include_cars: bool = True, scale: int = 4, small_scale: int = 8, max_size: int = 1024,
    emit_mip1_tails: bool = True, supplement_dumps: Optional[Path] = None,
    batch_size: int = 384, threads: int = 0, relief_mode: str = "sculpted",
) -> Dict[str, Any]:
    """Build a 4x/8x BC7 DDS replacement pack (faithful v2.2 or sculpted 3D v2.5) offline from PS2 ISO."""
    if relief_mode not in RELIEF_MODE_CHOICES:
        raise ValueError(f"Unsupported relief_mode: {relief_mode!r}")
    t0 = time.perf_counter()
    encoder_bin = imtt.find_rockport_dds() or imtt.build_rockport_dds(release=True)
    arc = PS2NFSMWArchive(iso_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    workers = threads if threads > 0 else max(1, os.cpu_count() or 4)
    seen_stems, blocked_stems = set(), set()
    track_blocked = supplement_dumps is not None and supplement_dumps.is_dir()
    skipped, by_mat, by_alpha, by_src = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    jobs: List[Tuple[List[Tuple[str, int]], np.ndarray, str, str, str]] = []
    files_written, bytes_written, unique_tex = 0, 0, 0

    def _enhance_one(j: Tuple[List[Tuple[str, int]], np.ndarray, str, str, str]) -> Tuple[List[Tuple[str, int]], np.ndarray, str, str, str]:
        enh, mat, amode = enhance_texture_rgba(
            j[1], name=j[2], archive=j[3], scale=scale, small_scale=small_scale,
            max_size=max_size, relief_mode=relief_mode,
        )
        return j[0], enh, mat, amode, j[4]

    def _flush(pool: concurrent.futures.ThreadPoolExecutor) -> None:
        nonlocal files_written, bytes_written, unique_tex
        if not jobs:
            return
        res = list(pool.map(_enhance_one, jobs)); jobs.clear()
        for _t, _e, mat, amode, bkt in res:
            unique_tex += 1; by_mat[mat] += 1; by_alpha[amode] += 1; by_src[bkt] += 1
        fw, bw = encode_batch_to_dds([(r[0], r[1]) for r in res], out_dir, encoder_bin, threads=threads)
        files_written += fw; bytes_written += bw

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        def _queue(tex: pth.RawTPKTexture, bucket: str) -> None:
            if tex.psm not in (pth.PSM_T4, pth.PSM_T8, pth.PSM_CT32):
                skipped["unsupported_psm"] += 1; return
            blocked, reason = is_denylisted_texture(tex.name, tex.archive)
            if blocked:
                skipped[reason] += 1
                if track_blocked:
                    blocked_stems.update(pth.compute_pcsx2_stems(tex, include_sub_mips=True))
                return
            stems = pth.compute_pcsx2_stems(tex, include_sub_mips=emit_mip1_tails)
            if not stems or stems[0] in seen_stems:
                return
            targets: List[Tuple[str, int]] = [(stems[0], 0)]
            seen_stems.add(stems[0])
            if emit_mip1_tails and len(stems) > 1:
                mips = pth.parse_plat_mips(tex.plat_bytes, tex.width, tex.height)
                fl0 = f"-{pth.pcsx2_flags(tex.psm, mips[0][0], mips[0][1]):08x}"
                for st in stems[1:]:
                    if st.endswith(fl0) and st not in seen_stems:
                        targets.append((st, 0)); seen_stems.add(st); break
                if len(mips) >= 2 and mips[1][0] >= 32 and mips[1][1] >= 16:
                    fl1 = f"-{pth.pcsx2_flags(tex.psm, mips[1][0], mips[1][1]):08x}"
                    for st in stems[1:]:
                        if st.endswith(fl1) and st not in seen_stems:
                            targets.append((st, 1)); seen_stems.add(st); break
            jobs.append((targets, pth.decode_raw_tpk_rgba(tex, scale_alpha=False), tex.name, tex.archive, bucket))
            if len(jobs) >= batch_size:
                _flush(pool)

        sections_matched = 0
        for sname, _, sbytes in pth.iter_streaml2ra_sections(arc.read_entry(arc.find(r"TRACKS\L2RA.BUN")), arc.read_entry(arc.find(r"TRACKS\STREAML2RA.BUN"))):
            sec_tex = list(pth.iter_tpk_raw_textures(sbytes, r"TRACKS\STREAML2RA.BUN", section=sname))
            if sec_tex and section_matches_district(sname, [t.name for t in sec_tex], district):
                sections_matched += 1
                for tex in sec_tex:
                    _queue(tex, f"world:{district}")

        extra_archives: List[Tuple[str, str]] = []
        if include_global:
            extra_archives.extend((vp, "global") for vp in GLOBAL_ALLOWLIST)
        if include_cars:
            extra_archives.extend((vp, "cars") for vp in sorted({e.resolved_path for e in arc.entries if e.resolved_path and e.resolved_path.startswith("CARS\\") and e.resolved_path.endswith("TEXTURES.BIN")}))
        for vp, bkt in extra_archives:
            try:
                for tex in pth.iter_tpk_raw_textures(arc.read_entry(arc.find(vp)), vp):
                    _queue(tex, bkt)
            except KeyError:
                continue

        stage2_added = 0
        if track_blocked and supplement_dumps is not None:
            collect_blocked_archive_stems(arc, blocked_stems)
            for png in sorted(supplement_dumps.glob("*.png")):
                stem = png.stem.lower()
                ok, rej = filter_stage2_stem(stem, seen_stems, blocked_stems)
                if not ok:
                    if rej != "stage2_already_covered":
                        skipped[rej] += 1
                    continue
                try:
                    with Image.open(png) as im:
                        arr = np.asarray(im.convert("RGBA"), dtype=np.uint8)
                except Exception:
                    skipped["stage2_unreadable"] += 1; continue
                h, w = arr.shape[:2]
                if min(w, h) < 32 or max(w, h) / max(1, min(w, h)) > 4.0:
                    skipped["stage2_tiny_or_strip"] += 1; continue
                if int(np.unique(arr[:, :, :3].reshape(-1, 3), axis=0).shape[0]) <= 4:
                    skipped["stage2_binary_mask"] += 1; continue
                seen_stems.add(stem); stage2_added += 1
                jobs.append(([(stem, 0)], arr, f"STAGE2_{stem}", "STAGE2", "stage2_dumps"))
                if len(jobs) >= batch_size:
                    _flush(pool)
        _flush(pool)

    manifest = {
        "schema_version": "2.5" if relief_mode == "sculpted" else "2.2",
        "relief_mode": relief_mode, "district": district, "include_global": include_global, "include_cars": include_cars,
        "scale": scale, "small_scale": small_scale, "max_size": max_size, "dxgi_format": DXGI_FORMAT_BC7_UNORM,
        "sections_matched": sections_matched, "unique_textures_enhanced": unique_tex, "dds_files_written": files_written,
        "total_dds_bytes": bytes_written, "total_dds_mb": round(bytes_written / (1024 * 1024), 2),
        "stage2_supplement_added": stage2_added, "stage2_blocked_stems": len(blocked_stems),
        "elapsed_seconds": round(time.perf_counter() - t0, 2),
        "by_source": dict(by_src), "by_material": dict(by_mat), "by_alpha_mode": dict(by_alpha),
        "skipped_denylist_total": sum(skipped.values()), "skipped_top": dict(skipped.most_common(12)),
    }
    (out_dir.parent / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build 4x/8x BC7 DDS PCSX2 texture packs (v2.2 faithful or v2.5 sculpted 3D) offline from PS2 ISO.")
    ap.add_argument("--iso", type=Path, default=None); ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--district", choices=DISTRICT_CHOICES, default="all")
    ap.add_argument("--relief-mode", choices=RELIEF_MODE_CHOICES, default="sculpted")
    ap.add_argument("--include-global", action="store_true"); ap.add_argument("--include-cars", action="store_true")
    ap.add_argument("--scale", type=int, default=4, choices=(2, 4, 8)); ap.add_argument("--small-scale", type=int, default=8, choices=(4, 8))
    ap.add_argument("--max-size", type=int, default=1024); ap.add_argument("--no-sub-mips", action="store_true")
    ap.add_argument("--supplement-dumps", type=Path, default=None); ap.add_argument("-j", "--threads", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    iso_path = args.iso.expanduser().resolve() if args.iso else _default_iso()
    if iso_path is None or not iso_path.is_file():
        print("ERROR: PS2 NFSMW ISO not found (pass --iso or set NFSMW_ISO).", file=sys.stderr)
        return 1
    out_dir = args.out.expanduser().resolve() if args.out else (DEFAULT_OUT_DIR if args.relief_mode == "sculpted" else DEFAULT_FAITHFUL_OUT_DIR)
    m = build_texture_pack(
        iso_path=iso_path, out_dir=out_dir, district=args.district,
        include_global=args.include_global, include_cars=args.include_cars, scale=args.scale,
        small_scale=args.small_scale, max_size=args.max_size, emit_mip1_tails=not args.no_sub_mips,
        supplement_dumps=args.supplement_dumps.expanduser().resolve() if args.supplement_dumps else None,
        threads=args.threads, relief_mode=args.relief_mode,
    )
    ver = "v2.5" if args.relief_mode == "sculpted" else "v2.2"
    print(json.dumps(m, indent=2) if args.json else f"Built {ver} ({m['district']}, {m['relief_mode']}): {m['dds_files_written']} BC7 DDS ({m['total_dds_mb']} MB) in {m['elapsed_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
