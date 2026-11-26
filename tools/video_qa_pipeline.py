#!/usr/bin/env python3
"""
video_qa_pipeline.py — Full-session video capture, Pillow-validated keyframe extraction,
multi-page timeline contact sheets, and video motion/health telemetry for NFSMW: Rockport 2026.

Recording modes supported:
  - pcsx2         (Primary / Gold Standard): PCSX2 v2.6.3 native internal MP4 video+audio
                  capture (`[EmuCore/GS] CaptureContainer = mp4`, `[Hotkeys] ToggleVideoCapture`).
                  Captures the exact Metal framebuffer cleanly from boot t=0s to shutdown without
                  requiring macOS Screen Recording permissions.
  - screencapture (Companion / Fallback): macOS `screencapture -x -v -V <sec> [-l <windowID>]`
                  automatically targeting PCSX2's CGWindowID via Swift `CGWindowListCopyWindowInfo`.
  - burst         (Headless Fallback): Periodic F8 / screenshot burst mode when video recording
                  is unavailable, assembling frames into identical QA contact sheets + telemetry.
  - auto          Uses `pcsx2` native capture as primary with clean validation & diagnostics.

Post-session QA pipeline (`extract-qa`):
  - Uses compiled native macOS Swift `AVFoundation` binary (`build/flywheel/video_tools`)
    to probe and extract timestamped frames every `interval` seconds (default 1.5s) from boot to end.
  - Strictly validates EVERY SINGLE PNG with Pillow (`Image.open(p).load()` + `verify()`),
    quarantining any corrupt/truncated file as `.corrupt` so no downstream agent ever hits HTTP 400.
  - Builds multi-page labeled 4x4 Timeline Contact Sheets (`qa_sheet_01.png`, `qa_sheet_02.png`, ...)
    showing timestamp, luma, R/B ratio, sharpness, and inter-frame motion delta (Δ MAE).
  - Computes `qa_telemetry.json` with per-frame motion scores, frozen-frame segment detection,
    black-screen duration, highlight clipping spikes, and color-cast anomaly timestamps.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

DEFAULT_FLYWHEEL_DIR = REPO_ROOT / "build" / "flywheel"
DEFAULT_VIDEO_TOOLS_SRC = TOOLS_DIR / "macos_video_tools.swift"
DEFAULT_VIDEO_TOOLS_BIN = DEFAULT_FLYWHEEL_DIR / "video_tools"

VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".avi")

SUPPORTED_VIDEO_HOTKEYS: Dict[str, str] = {
    "f12": "Keyboard/F12",
    "f11": "Keyboard/F11",
    "f10": "Keyboard/F10",
    "f9": "Keyboard/F9",
    "v": "Keyboard/V",
    "o": "Keyboard/O",
    "r": "Keyboard/R",
}


def pcsx2_video_capture_gs_settings(
    *,
    width: int = 1280,
    height: int = 720,
    bitrate_kbps: int = 8000,
    audio_bitrate_kbps: int = 192,
    container: str = "mp4",
    show_osd: bool = False,
) -> Dict[str, str]:
    """Return PCSX2 v2.6.3 [EmuCore/GS] keys for clean native MP4 video+audio capture."""
    if width < 320 or height < 240:
        raise ValueError(f"Invalid video capture dimensions: {width}x{height}")
    if bitrate_kbps < 500:
        raise ValueError(f"Invalid video capture bitrate_kbps: {bitrate_kbps}")
    return {
        "EnableVideoCapture": "true",
        "EnableAudioCapture": "true",
        "EnableVideoCaptureParameters": "false",
        "EnableAudioCaptureParameters": "false",
        "VideoCaptureAutoResolution": "false",
        "CaptureContainer": container,
        "VideoCaptureCodec": "",
        "VideoCaptureFormat": "",
        "VideoCaptureParameters": "",
        "AudioCaptureCodec": "",
        "AudioCaptureParameters": "",
        "VideoCaptureWidth": str(width),
        "VideoCaptureHeight": str(height),
        "VideoCaptureBitrate": str(bitrate_kbps),
        "AudioCaptureBitrate": str(audio_bitrate_kbps),
        "OsdShowVideoCapture": "true" if show_osd else "false",
    }


def resolve_pcsx2_video_hotkey(hotkey: str = "f12") -> Tuple[str, str]:
    """Resolve a user hotkey name ('f12', 'v', etc.) to (key_driver_key, PCSX2_ini_binding)."""
    norm = hotkey.strip().lower()
    if norm not in SUPPORTED_VIDEO_HOTKEYS:
        allowed = ", ".join(sorted(SUPPORTED_VIDEO_HOTKEYS))
        raise ValueError(f"Unsupported video hotkey {hotkey!r}; expected one of: {allowed}")
    return norm, SUPPORTED_VIDEO_HOTKEYS[norm]


def ensure_video_tools_binary(
    src_path: Path = DEFAULT_VIDEO_TOOLS_SRC,
    bin_path: Path = DEFAULT_VIDEO_TOOLS_BIN,
    *,
    force_rebuild: bool = False,
) -> Path:
    """Compile tools/macos_video_tools.swift to build/flywheel/video_tools if needed."""
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    needs_build = force_rebuild or not bin_path.is_file() or not os.access(bin_path, os.X_OK)
    if not needs_build and src_path.is_file():
        needs_build = src_path.stat().st_mtime > bin_path.stat().st_mtime

    if needs_build:
        if not src_path.is_file():
            raise FileNotFoundError(f"video_tools Swift source not found: {src_path}")
        res = subprocess.run(
            ["swiftc", "-O", str(src_path), "-o", str(bin_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to compile {src_path} -> {bin_path} (exit {res.returncode}):\n{res.stderr.strip()}"
            )
    return bin_path


def _run_video_tools_json(
    args: Sequence[str],
    *,
    video_tools_bin: Optional[Path] = None,
    timeout_sec: float = 120.0,
) -> Tuple[int, Dict[str, Any]]:
    tool = video_tools_bin.resolve() if video_tools_bin else ensure_video_tools_binary()
    res = subprocess.run(
        [str(tool), *args],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    payload: Dict[str, Any] = {"returncode": res.returncode}
    if res.stdout.strip():
        try:
            parsed = json.loads(res.stdout.strip())
            if isinstance(parsed, dict):
                payload.update(parsed)
        except json.JSONDecodeError:
            payload["raw_stdout"] = res.stdout.strip()
    if res.stderr.strip():
        payload["stderr"] = res.stderr.strip()
    return res.returncode, payload


def probe_video(video_path: Path, *, video_tools_bin: Optional[Path] = None) -> Dict[str, Any]:
    """Inspect a video file using native AVFoundation and confirm at least one frame decodes."""
    _, payload = _run_video_tools_json(
        ["probe", str(video_path.resolve()), "--json"],
        video_tools_bin=video_tools_bin,
        timeout_sec=30.0,
    )
    payload.setdefault("valid", False)
    payload.setdefault("path", str(video_path.resolve()))
    return payload


def transcode_to_mp4(
    src_path: Path,
    dst_path: Path,
    *,
    preset: str = "1280x720",
    video_tools_bin: Optional[Path] = None,
) -> Dict[str, Any]:
    """Re-encode/remux a video into a clean H.264 MP4 optimized with leading moov atom."""
    _, payload = _run_video_tools_json(
        [
            "transcode",
            "--input",
            str(src_path.resolve()),
            "--output",
            str(dst_path.resolve()),
            "--preset",
            preset,
            "--json",
        ],
        video_tools_bin=video_tools_bin,
        timeout_sec=300.0,
    )
    payload.setdefault("ok", False)
    return payload


def find_window(
    *,
    pid: Optional[int] = None,
    owner: Optional[str] = "PCSX2",
    video_tools_bin: Optional[Path] = None,
) -> Dict[str, Any]:
    """Discover an on-screen macOS window ID (e.g., PCSX2 main display window)."""
    cmd = ["find-window", "--json"]
    if pid is not None:
        cmd.extend(["--pid", str(pid)])
    elif owner:
        cmd.extend(["--owner", owner])
    _, payload = _run_video_tools_json(cmd, video_tools_bin=video_tools_bin, timeout_sec=15.0)
    payload.setdefault("ok", False)
    return payload


def synthesize_test_video(
    out_path: Path,
    *,
    duration_sec: float = 4.0,
    fps: int = 15,
    width: int = 320,
    height: int = 180,
    black_head_sec: float = 0.0,
    static_tail_sec: float = 0.0,
    clip_at_sec: Optional[float] = None,
    amber_at_sec: Optional[float] = None,
    video_tools_bin: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate a real H.264 MP4 via AVAssetWriter for offline QA pipeline testing."""
    cmd = [
        "synth-video",
        "--output",
        str(out_path.resolve()),
        "--duration",
        f"{duration_sec:.3f}",
        "--fps",
        str(fps),
        "--width",
        str(width),
        "--height",
        str(height),
        "--black-head",
        f"{black_head_sec:.3f}",
        "--static-tail",
        f"{static_tail_sec:.3f}",
        "--json",
    ]
    if clip_at_sec is not None:
        cmd.extend(["--clip-at", f"{clip_at_sec:.3f}"])
    if amber_at_sec is not None:
        cmd.extend(["--amber-at", f"{amber_at_sec:.3f}"])
    _, payload = _run_video_tools_json(cmd, video_tools_bin=video_tools_bin, timeout_sec=60.0)
    payload.setdefault("ok", False)
    return payload


def validate_and_quarantine_png(png_path: Path) -> Tuple[bool, Path, Optional[str]]:
    """Strictly validate a PNG with Pillow (verify + full pixel load).

    If truncated, empty, or corrupt, immediately rename it with a `.corrupt` suffix so no
    agent or tool can ever ingest an invalid `.png` file.
    """
    if not png_path.is_file():
        return False, png_path, "file does not exist"

    err_msg: Optional[str] = None
    if png_path.stat().st_size <= 0:
        err_msg = "empty 0-byte PNG file"
    else:
        try:
            with Image.open(png_path) as im:
                im.verify()
            with Image.open(png_path) as im2:
                im2.load()
                w, h = im2.size
                if w <= 0 or h <= 0:
                    err_msg = f"invalid image dimensions {w}x{h}"
        except Exception as exc:
            err_msg = f"corrupt or truncated PNG: {exc}"

    if err_msg is None:
        return True, png_path, None

    candidate = png_path.with_name(png_path.name + ".corrupt")
    suffix_idx = 1
    while candidate.exists():
        candidate = png_path.with_name(f"{png_path.name}.corrupt.{suffix_idx}")
        suffix_idx += 1
    png_path.rename(candidate)
    return False, candidate, err_msg


def analyze_png_frame(image_path: Path) -> Dict[str, Any]:
    """Compute objective exposure, contrast, color cast, and sharpness metrics on a validated PNG."""
    info: Dict[str, Any] = {
        "path": str(image_path),
        "exists": image_path.is_file(),
        "size_bytes": image_path.stat().st_size if image_path.is_file() else 0,
        "width": 0,
        "height": 0,
        "mean_luma": 0.0,
        "std_luma": 0.0,
        "p5": 0.0,
        "p99": 0.0,
        "highlight_clip_pct": 0.0,
        "shadow_crush_pct": 0.0,
        "amber_rb_ratio": 0.0,
        "laplacian_sharpness": 0.0,
        "non_black_coverage": 0.0,
        "is_valid_render": False,
    }
    if not image_path.is_file() or info["size_bytes"] == 0:
        return info

    try:
        with Image.open(image_path) as img:
            rgb_img = img.convert("RGB")
            w, h = rgb_img.size
            info["width"] = w
            info["height"] = h
            if w == 0 or h == 0:
                return info
            arr = np.asarray(rgb_img, dtype=np.float32)
    except Exception as exc:
        info["error"] = f"failed to decode image: {exc}"
        return info

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b

    mean_luma = float(np.mean(luma))
    std_luma = float(np.std(luma))
    p5 = float(np.percentile(luma, 5.0))
    p99 = float(np.percentile(luma, 99.0))
    highlight_clip_pct = float(np.mean(luma >= 250.0) * 100.0)
    shadow_crush_pct = float(np.mean(luma <= 5.0) * 100.0)

    mean_r = float(np.mean(r))
    mean_b = float(np.mean(b))
    amber_rb_ratio = float(mean_r / max(mean_b, 1e-6))

    max_rgb = np.max(arr, axis=2)
    non_black_coverage = float(np.mean(max_rgb > 8.0))

    if w >= 3 and h >= 3:
        lap = (
            luma[:-2, 1:-1]
            + luma[2:, 1:-1]
            + luma[1:-1, :-2]
            + luma[1:-1, 2:]
            - 4.0 * luma[1:-1, 1:-1]
        )
        laplacian_sharpness = float(np.var(lap))
    else:
        laplacian_sharpness = 0.0

    info.update(
        {
            "mean_luma": round(mean_luma, 2),
            "std_luma": round(std_luma, 2),
            "p5": round(p5, 2),
            "p99": round(p99, 2),
            "highlight_clip_pct": round(highlight_clip_pct, 3),
            "shadow_crush_pct": round(shadow_crush_pct, 3),
            "amber_rb_ratio": round(amber_rb_ratio, 4),
            "laplacian_sharpness": round(laplacian_sharpness, 2),
            "non_black_coverage": round(non_black_coverage, 4),
            "is_valid_render": bool(non_black_coverage > 0.05 and std_luma > 2.0),
        }
    )
    return info


def _load_thumb_array(image_path: Path, size: Tuple[int, int] = (160, 90)) -> Optional[np.ndarray]:
    try:
        with Image.open(image_path) as im:
            thumb = im.convert("RGB").resize(size, Image.Resampling.BILINEAR)
            return np.asarray(thumb, dtype=np.float32)
    except Exception:
        return None


def _load_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_qa_contact_sheets(
    timeline_entries: Sequence[Dict[str, Any]],
    out_dir: Path,
    *,
    source_label: str = "session_video.mp4",
    cols: int = 4,
    rows: int = 4,
    tile_width: int = 480,
    sheet_prefix: str = "qa_sheet",
) -> List[Dict[str, Any]]:
    """Build multi-page chronological labeled QA contact sheets (`qa_sheet_01.png`, ...).

    Validates every generated sheet with Pillow before returning.
    """
    valid_entries = [
        e
        for e in timeline_entries
        if not e.get("quarantined_corrupt") and e.get("path") and Path(e["path"]).is_file()
    ]
    if not valid_entries:
        return []

    cols = max(1, cols)
    rows = max(1, rows)
    per_sheet = cols * rows
    total_sheets = math.ceil(len(valid_entries) / per_sheet)

    # Determine median aspect ratio across frames
    aspects: List[float] = []
    for e in valid_entries[:32]:
        w = int(e.get("width") or 0)
        h = int(e.get("height") or 0)
        if w > 0 and h > 0:
            aspects.append(h / w)
    median_aspect = sorted(aspects)[len(aspects) // 2] if aspects else (9.0 / 16.0)
    tile_height = max(120, int(round(tile_width * median_aspect)))

    banner_h = 46
    header_h = 44
    pad = 12
    title_font = _load_font(18)
    tile_font = _load_font(14)
    meta_font = _load_font(12)

    out_dir.mkdir(parents=True, exist_ok=True)
    sheet_records: List[Dict[str, Any]] = []

    for sheet_idx in range(total_sheets):
        chunk = valid_entries[sheet_idx * per_sheet : (sheet_idx + 1) * per_sheet]
        eff_rows = math.ceil(len(chunk) / cols)
        eff_cols = min(cols, len(chunk)) if sheet_idx == 0 and total_sheets == 1 else cols

        canvas_w = pad + eff_cols * (tile_width + pad)
        canvas_h = banner_h + pad + eff_rows * (header_h + tile_height + pad)
        canvas = Image.new("RGB", (canvas_w, canvas_h), (12, 16, 24))
        draw = ImageDraw.Draw(canvas)

        t_start = float(chunk[0].get("timestamp_sec", 0.0))
        t_end = float(chunk[-1].get("timestamp_sec", 0.0))
        banner_text = (
            f"QA Timeline Sheet {sheet_idx + 1}/{total_sheets}  |  "
            f"t={t_start:05.1f}s .. {t_end:05.1f}s  |  "
            f"{len(chunk)} frames  |  {source_label}"
        )
        draw.rectangle([0, 0, canvas_w, banner_h], fill=(20, 28, 42))
        draw.line([(0, banner_h - 1), (canvas_w, banner_h - 1)], fill=(64, 86, 120), width=1)
        draw.text((pad, 13), banner_text, fill=(242, 246, 252), font=title_font)

        for local_idx, entry in enumerate(chunk):
            r_idx = local_idx // eff_cols
            c_idx = local_idx % eff_cols
            x0 = pad + c_idx * (tile_width + pad)
            y0 = banner_h + pad + r_idx * (header_h + tile_height + pad)

            flags = list(entry.get("flags") or [])
            if "BLACK_SCREEN" in flags or "FROZEN" in flags:
                accent = (210, 66, 66)
                header_bg = (48, 22, 26)
            elif "HIGHLIGHT_CLIP" in flags or "COLOR_CAST" in flags:
                accent = (224, 162, 54)
                header_bg = (46, 36, 20)
            else:
                accent = (62, 184, 118)
                header_bg = (22, 32, 46)

            draw.rectangle(
                [x0, y0, x0 + tile_width, y0 + header_h],
                fill=header_bg,
                outline=(56, 72, 98),
                width=1,
            )
            # Left status accent bar
            draw.rectangle([x0, y0, x0 + 5, y0 + header_h], fill=accent)

            frame_num = int(entry.get("index", local_idx + 1))
            ts = float(entry.get("timestamp_sec", 0.0))
            delta_mae = entry.get("frame_diff_mae")
            delta_str = f"d={delta_mae:.1f}" if delta_mae is not None else "d=--"
            flag_tag = f" [{','.join(flags[:2])}]" if flags else ""

            line1 = f"#{frame_num:03d}  t={ts:05.1f}s{flag_tag}"
            line2 = (
                f"luma={entry.get('mean_luma', 0.0):.1f}  "
                f"R/B={entry.get('amber_rb_ratio', 0.0):.2f}  "
                f"sharp={entry.get('laplacian_sharpness', 0.0):.0f}  "
                f"{delta_str}"
            )
            draw.text((x0 + 11, y0 + 5), line1, fill=(242, 246, 252), font=tile_font)
            draw.text((x0 + 11, y0 + 24), line2, fill=(166, 194, 232), font=meta_font)

            img_path = Path(entry["path"])
            try:
                with Image.open(img_path) as im:
                    rgb = im.convert("RGB").resize((tile_width, tile_height), Image.Resampling.LANCZOS)
                    canvas.paste(rgb, (x0, y0 + header_h))
            except Exception as exc:
                draw.rectangle(
                    [x0, y0 + header_h, x0 + tile_width, y0 + header_h + tile_height],
                    fill=(44, 18, 22),
                )
                draw.text(
                    (x0 + 14, y0 + header_h + tile_height // 2 - 8),
                    f"[UNREADABLE: {str(exc)[:48]}]",
                    fill=(255, 160, 160),
                    font=tile_font,
                )

            draw.rectangle(
                [x0, y0 + header_h, x0 + tile_width, y0 + header_h + tile_height],
                outline=accent if flags else (56, 72, 98),
                width=1,
            )

        out_path = out_dir / f"{sheet_prefix}_{sheet_idx + 1:02d}.png"
        tmp_path = out_dir / f".{out_path.name}.tmp"
        canvas.save(tmp_path, format="PNG")
        ok, checked_path, err = validate_and_quarantine_png(tmp_path)
        if not ok:
            raise RuntimeError(f"Generated QA sheet failed Pillow validation ({checked_path}): {err}")
        tmp_path.replace(out_path)

        sheet_records.append(
            {
                "sheet_index": sheet_idx + 1,
                "path": str(out_path.resolve()),
                "width": canvas_w,
                "height": canvas_h,
                "frame_count": len(chunk),
                "t_start_sec": round(t_start, 3),
                "t_end_sec": round(t_end, 3),
            }
        )

    return sheet_records


def _compute_timeline_and_telemetry(
    frame_records: Sequence[Dict[str, Any]],
    *,
    video_info: Optional[Dict[str, Any]] = None,
    interval_sec: float = 1.5,
    frozen_mae_threshold: float = 0.85,
    active_motion_mae_threshold: float = 2.0,
    highlight_clip_threshold_pct: float = 5.0,
    amber_high_threshold: float = 1.55,
    amber_low_threshold: float = 0.60,
) -> Dict[str, Any]:
    """Validate every PNG, compute per-frame metrics & inter-frame motion/health telemetry."""
    timeline: List[Dict[str, Any]] = []
    quarantined_corrupt: List[Dict[str, Any]] = []

    prev_thumb: Optional[np.ndarray] = None
    prev_ts: Optional[float] = None
    prev_is_black = False

    black_frames = 0
    black_duration_sec = 0.0
    cur_black_sec = 0.0
    max_consecutive_black_sec = 0.0

    frozen_pairs = 0
    cur_frozen_start: Optional[float] = None
    cur_frozen_end: Optional[float] = None
    frozen_segments: List[Dict[str, Any]] = []

    motion_maes: List[float] = []
    active_motion_frames = 0
    highlight_clip_spikes: List[Dict[str, Any]] = []
    color_cast_anomalies: List[Dict[str, Any]] = []

    def _close_frozen_segment() -> None:
        nonlocal cur_frozen_start, cur_frozen_end
        if cur_frozen_start is not None and cur_frozen_end is not None:
            dur = round(max(0.0, cur_frozen_end - cur_frozen_start), 3)
            frozen_segments.append(
                {
                    "start_sec": round(cur_frozen_start, 3),
                    "end_sec": round(cur_frozen_end, 3),
                    "duration_sec": dur,
                }
            )
        cur_frozen_start = None
        cur_frozen_end = None

    for idx, rec in enumerate(frame_records, start=1):
        src_path = Path(rec["path"])
        ts = float(rec.get("timestamp_sec", (idx - 1) * interval_sec))

        ok, final_path, err = validate_and_quarantine_png(src_path)
        if not ok:
            q_entry = {
                "index": idx,
                "timestamp_sec": round(ts, 3),
                "path": str(final_path),
                "quarantined_corrupt": True,
                "error": err,
                "is_valid_render": False,
                "flags": ["CORRUPT_QUARANTINED"],
            }
            quarantined_corrupt.append(q_entry)
            timeline.append(q_entry)
            _close_frozen_segment()
            prev_thumb = None
            prev_ts = ts
            continue

        metrics = analyze_png_frame(final_path)
        thumb = _load_thumb_array(final_path)

        dt = (ts - prev_ts) if (prev_ts is not None and ts > prev_ts) else interval_sec
        is_black = bool(metrics["non_black_coverage"] <= 0.05 or metrics["mean_luma"] <= 4.0)

        frame_diff_mae: Optional[float] = None
        frame_diff_pct_changed: Optional[float] = None
        is_frozen_vs_prev = False

        if thumb is not None and prev_thumb is not None:
            diff = np.abs(thumb - prev_thumb)
            frame_diff_mae = round(float(np.mean(diff)), 3)
            pixel_luma_diff = np.mean(diff, axis=2)
            frame_diff_pct_changed = round(float(np.mean(pixel_luma_diff > 8.0) * 100.0), 2)

            if not is_black and not prev_is_black:
                motion_maes.append(frame_diff_mae)
                if frame_diff_mae >= active_motion_mae_threshold:
                    active_motion_frames += 1
                if frame_diff_mae < frozen_mae_threshold:
                    is_frozen_vs_prev = True
                    frozen_pairs += 1
                    if cur_frozen_start is None:
                        cur_frozen_start = prev_ts if prev_ts is not None else ts
                    cur_frozen_end = ts
                else:
                    _close_frozen_segment()
            else:
                _close_frozen_segment()
        else:
            _close_frozen_segment()

        if is_black:
            black_frames += 1
            step_dur = dt if prev_ts is not None else interval_sec
            black_duration_sec += step_dur
            cur_black_sec += step_dur
            if cur_black_sec > max_consecutive_black_sec:
                max_consecutive_black_sec = cur_black_sec
        else:
            cur_black_sec = 0.0

        flags: List[str] = []
        if is_black:
            flags.append("BLACK_SCREEN")
        if is_frozen_vs_prev:
            flags.append("FROZEN")
        if not is_black and metrics["highlight_clip_pct"] >= highlight_clip_threshold_pct:
            flags.append("HIGHLIGHT_CLIP")
            highlight_clip_spikes.append(
                {
                    "index": idx,
                    "timestamp_sec": round(ts, 3),
                    "highlight_clip_pct": metrics["highlight_clip_pct"],
                    "path": str(final_path),
                }
            )
        if not is_black and (
            metrics["amber_rb_ratio"] >= amber_high_threshold
            or metrics["amber_rb_ratio"] <= amber_low_threshold
        ):
            flags.append("COLOR_CAST")
            color_cast_anomalies.append(
                {
                    "index": idx,
                    "timestamp_sec": round(ts, 3),
                    "amber_rb_ratio": metrics["amber_rb_ratio"],
                    "path": str(final_path),
                }
            )

        entry = dict(metrics)
        entry.update(
            {
                "index": idx,
                "timestamp_sec": round(ts, 3),
                "path": str(final_path.resolve()),
                "quarantined_corrupt": False,
                "frame_diff_mae": frame_diff_mae,
                "frame_diff_pct_changed": frame_diff_pct_changed,
                "is_black_frame": is_black,
                "is_frozen_vs_prev": is_frozen_vs_prev,
                "flags": flags,
            }
        )
        timeline.append(entry)

        prev_thumb = thumb
        prev_ts = ts
        prev_is_black = is_black

    _close_frozen_segment()

    valid_timeline = [e for e in timeline if not e.get("quarantined_corrupt")]
    max_frozen_sec = max((seg["duration_sec"] for seg in frozen_segments), default=0.0)
    mean_motion_mae = round(sum(motion_maes) / len(motion_maes), 3) if motion_maes else 0.0
    max_motion_mae = round(max(motion_maes), 3) if motion_maes else 0.0
    motion_detected = bool(active_motion_frames > 0 or max_motion_mae >= active_motion_mae_threshold)

    duration_sec = float((video_info or {}).get("duration_sec") or 0.0)
    if duration_sec <= 0.0 and valid_timeline:
        duration_sec = float(valid_timeline[-1]["timestamp_sec"])

    health_flags: List[str] = []
    if quarantined_corrupt:
        health_flags.append("CORRUPT_FRAMES_QUARANTINED")
    if max_consecutive_black_sec >= 6.0:
        health_flags.append("EXCESSIVE_BLACK_SCREEN")
    if max_frozen_sec >= 4.5:
        health_flags.append("LONG_FROZEN_SEGMENT")
    if len(valid_timeline) >= 3 and not motion_detected:
        health_flags.append("NO_MOTION_DETECTED")
    if highlight_clip_spikes:
        health_flags.append("HIGHLIGHT_CLIPPING_SPIKES")
    if color_cast_anomalies:
        health_flags.append("COLOR_CAST_ANOMALIES")

    summary: Dict[str, Any] = {
        "duration_sec": round(duration_sec, 3),
        "total_frames_extracted": len(timeline),
        "valid_frames": len(valid_timeline),
        "corrupt_frames_quarantined": len(quarantined_corrupt),
        "black_screen_frames": black_frames,
        "black_screen_duration_sec": round(black_duration_sec, 3),
        "max_consecutive_black_sec": round(max_consecutive_black_sec, 3),
        "frozen_frame_pairs": frozen_pairs,
        "max_consecutive_frozen_sec": round(max_frozen_sec, 3),
        "frozen_segments": frozen_segments,
        "active_motion_frames": active_motion_frames,
        "mean_motion_mae": mean_motion_mae,
        "max_motion_mae": max_motion_mae,
        "motion_detected": motion_detected,
        "highlight_clip_spike_count": len(highlight_clip_spikes),
        "highlight_clip_spikes": highlight_clip_spikes,
        "color_cast_anomaly_count": len(color_cast_anomalies),
        "color_cast_anomalies": color_cast_anomalies,
        "health_flags": health_flags,
    }

    return {
        "tool": "video_qa_pipeline.py",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "video_info": video_info or {},
        "interval_sec": interval_sec,
        "summary": summary,
        "quarantined_corrupt": quarantined_corrupt,
        "timeline": timeline,
    }


def extract_qa(
    input_path: Path,
    out_dir: Path,
    *,
    interval_sec: float = 1.5,
    max_frames: int = 400,
    max_width: int = 0,
    sheet_cols: int = 4,
    sheet_rows: int = 4,
    tile_width: int = 480,
    video_tools_bin: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run full video/frames QA extraction: Pillow validation, contact sheets, and telemetry."""
    input_path = input_path.resolve()
    out_dir = out_dir.resolve()
    qa_frames_dir = out_dir / "qa_frames"
    qa_frames_dir.mkdir(parents=True, exist_ok=True)

    video_info: Dict[str, Any] = {}
    raw_frames: List[Dict[str, Any]] = []
    extract_result: Optional[Dict[str, Any]] = None

    if input_path.is_dir():
        # Burst / PNG directory mode
        pngs = sorted(
            (p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() == ".png"),
            key=lambda p: p.name,
        )
        for idx, p in enumerate(pngs, start=1):
            m = re.search(r"_t(\d+(?:\.\d+)?)s", p.name)
            ts = float(m.group(1)) if m else (idx - 1) * interval_sec
            dst = qa_frames_dir / p.name
            if dst.resolve() != p.resolve():
                shutil.copy2(p, dst)
            raw_frames.append({"index": idx, "timestamp_sec": ts, "path": str(dst)})
        video_info = {
            "source_type": "frame_directory",
            "path": str(input_path),
            "valid": bool(raw_frames),
            "duration_sec": raw_frames[-1]["timestamp_sec"] if raw_frames else 0.0,
        }
    else:
        if not input_path.is_file():
            raise FileNotFoundError(f"Video input not found: {input_path}")
        video_info = probe_video(input_path, video_tools_bin=video_tools_bin)
        if not video_info.get("valid"):
            telemetry = {
                "tool": "video_qa_pipeline.py",
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "ok": False,
                "video_info": video_info,
                "error": video_info.get("error") or "Video failed AVFoundation probe",
                "qa_frames_dir": str(qa_frames_dir),
                "qa_sheets": [],
                "summary": {
                    "total_frames_extracted": 0,
                    "valid_frames": 0,
                    "corrupt_frames_quarantined": 0,
                    "motion_detected": False,
                    "health_flags": ["INVALID_VIDEO"],
                },
                "timeline": [],
            }
            telemetry_path = out_dir / "qa_telemetry.json"
            telemetry_path.write_text(json.dumps(telemetry, indent=2), encoding="utf-8")
            telemetry["telemetry_path"] = str(telemetry_path)
            return telemetry

        # Clean old extracted frames in qa_frames_dir before extracting fresh ones
        for old in qa_frames_dir.glob("frame_*.png"):
            old.unlink()

        cmd = [
            "extract",
            "--input",
            str(input_path),
            "--out-dir",
            str(qa_frames_dir),
            "--interval",
            f"{interval_sec:.3f}",
            "--max-frames",
            str(max_frames),
            "--json",
        ]
        if max_width > 0:
            cmd.extend(["--max-width", str(max_width)])
        _, extract_result = _run_video_tools_json(
            cmd, video_tools_bin=video_tools_bin, timeout_sec=300.0
        )
        raw_frames = list(extract_result.get("frames") or [])

    telemetry = _compute_timeline_and_telemetry(
        raw_frames,
        video_info=video_info,
        interval_sec=interval_sec,
    )
    sheets = build_qa_contact_sheets(
        telemetry["timeline"],
        out_dir,
        source_label=input_path.name,
        cols=sheet_cols,
        rows=sheet_rows,
        tile_width=tile_width,
    )

    telemetry["ok"] = bool(telemetry["summary"]["valid_frames"] > 0)
    telemetry["qa_frames_dir"] = str(qa_frames_dir)
    telemetry["qa_sheets"] = sheets
    if extract_result is not None:
        telemetry["extract_failure_count"] = int(extract_result.get("failure_count", 0))

    telemetry_path = out_dir / "qa_telemetry.json"
    telemetry["telemetry_path"] = str(telemetry_path)
    telemetry_path.write_text(json.dumps(telemetry, indent=2), encoding="utf-8")
    return telemetry


def _list_video_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda p: p.stat().st_mtime,
    )


def wait_for_video_file_ready(
    videos_dir: Path,
    before_names: set[str],
    *,
    timeout_sec: float = 10.0,
    video_tools_bin: Optional[Path] = None,
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Wait until a newly written video file stabilizes in size and passes AVFoundation probe."""
    deadline = time.monotonic() + max(1.5, timeout_sec)
    last_sizes: Dict[str, int] = {}
    stable_since: Dict[str, float] = {}
    latest_candidate: Optional[Path] = None
    latest_probe: Optional[Dict[str, Any]] = None

    while time.monotonic() < deadline:
        new_videos = [p for p in _list_video_files(videos_dir) if p.name not in before_names]
        if new_videos:
            cand = new_videos[-1]
            latest_candidate = cand
            sz = cand.stat().st_size
            now = time.monotonic()
            if sz > 0 and last_sizes.get(cand.name) == sz:
                if (now - stable_since.get(cand.name, now)) >= 0.45:
                    probe = probe_video(cand, video_tools_bin=video_tools_bin)
                    latest_probe = probe
                    if probe.get("valid"):
                        return cand, probe
            else:
                last_sizes[cand.name] = sz
                stable_since[cand.name] = now
        time.sleep(0.25)

    if latest_candidate is not None and latest_probe is None:
        latest_probe = probe_video(latest_candidate, video_tools_bin=video_tools_bin)
    return latest_candidate, latest_probe


class SessionVideoRecorder:
    """Manages full-session video capture for a sandboxed PCSX2 run.

    Supports:
      - `pcsx2` / `auto`: PCSX2 v2.6.3 native internal MP4 capture toggled via `[Hotkeys] ToggleVideoCapture`
      - `screencapture`: macOS `screencapture -x -v -V <sec> [-l <windowID>]`
      - `burst`: Screenshot-only QA fallback (post-processes captured snaps as timeline)
    """

    def __init__(
        self,
        *,
        mode: str,
        iter_dir: Path,
        videos_dir: Path,
        key_driver_bin: Optional[Path] = None,
        video_tools_bin: Optional[Path] = None,
        tap_mode: str = "auto",
        video_hotkey: str = "f12",
        max_duration_sec: int = 180,
    ) -> None:
        self.requested_mode = mode.strip().lower()
        self.effective_mode = "pcsx2" if self.requested_mode == "auto" else self.requested_mode
        self.iter_dir = iter_dir.resolve()
        self.videos_dir = videos_dir.resolve()
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.key_driver_bin = key_driver_bin.resolve() if key_driver_bin else None
        self.video_tools_bin = video_tools_bin.resolve() if video_tools_bin else None
        self.tap_mode = tap_mode
        self.video_hotkey, self.pcsx2_hotkey_binding = resolve_pcsx2_video_hotkey(video_hotkey)
        self.max_duration_sec = max(15, int(max_duration_sec))

        self.before_video_names: set[str] = {p.name for p in _list_video_files(self.videos_dir)}
        self.started = False
        self.stopped = False
        self.pcsx2_toggle_sent_start = False
        self.pcsx2_toggle_sent_stop = False
        self._screencapture_proc: Optional[subprocess.Popen[bytes]] = None
        self._screencapture_path: Optional[Path] = None
        self.events: List[Dict[str, Any]] = []
        self.window_info: Optional[Dict[str, Any]] = None

    def _send_hotkey_toggle(self, pid: int, reason: str) -> Dict[str, Any]:
        if self.key_driver_bin is None or not self.key_driver_bin.is_file():
            ev = {"event": reason, "ok": False, "error": "key_driver binary unavailable"}
            self.events.append(ev)
            return ev
        action = f"tap:{self.video_hotkey}:120,wait:250"
        res = subprocess.run(
            [
                str(self.key_driver_bin),
                "--pid",
                str(pid),
                "--tap-mode",
                self.tap_mode,
                "--json",
                "--actions",
                action,
            ],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        ev: Dict[str, Any] = {
            "event": reason,
            "hotkey": self.video_hotkey,
            "returncode": res.returncode,
            "ok": res.returncode == 0,
            "timestamp_monotonic": round(time.monotonic(), 3),
        }
        if res.stderr.strip():
            ev["stderr"] = res.stderr.strip()
        self.events.append(ev)
        return ev

    def has_active_pcsx2_video_file(self) -> bool:
        return any(p.name not in self.before_video_names for p in _list_video_files(self.videos_dir))

    def start(self, pid: int) -> Dict[str, Any]:
        """Begin recording immediately after PCSX2 GS/VM initializes."""
        if self.effective_mode == "off" or self.effective_mode == "burst":
            self.started = True
            return {"mode": self.effective_mode, "started": True}

        if self.effective_mode == "pcsx2":
            ev = self._send_hotkey_toggle(pid, "pcsx2_start_capture")
            self.pcsx2_toggle_sent_start = bool(ev.get("ok"))
            self.started = self.pcsx2_toggle_sent_start
            # Wait briefly to confirm PCSX2 created the video output file
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if self.has_active_pcsx2_video_file():
                    ev["video_file_detected"] = True
                    break
                time.sleep(0.15)
            return {"mode": "pcsx2", "started": self.started, "event": ev}

        if self.effective_mode == "screencapture":
            win = find_window(pid=pid, video_tools_bin=self.video_tools_bin)
            self.window_info = win
            out_mov = self.videos_dir / "screencapture_session.mov"
            if out_mov.exists():
                out_mov.unlink()
            cmd = ["screencapture", "-x", "-v", "-V", str(self.max_duration_sec)]
            wid = win.get("window_id")
            if wid:
                cmd.extend(["-l", str(wid)])
            cmd.append(str(out_mov))
            self._screencapture_path = out_mov
            self._screencapture_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.started = True
            ev = {
                "event": "screencapture_start",
                "ok": True,
                "command": cmd,
                "window_id": wid,
                "output": str(out_mov),
            }
            self.events.append(ev)
            return {"mode": "screencapture", "started": True, "event": ev}

        raise ValueError(f"Unsupported video mode: {self.effective_mode!r}")

    def ensure_started_retry(self, pid: int) -> Optional[Dict[str, Any]]:
        """If PCSX2 native capture did not create a file right at early GS init, retry once."""
        if self.effective_mode != "pcsx2":
            return None
        if self.has_active_pcsx2_video_file():
            return None
        ev = self._send_hotkey_toggle(pid, "pcsx2_start_capture_retry")
        if ev.get("ok"):
            self.pcsx2_toggle_sent_start = True
            self.started = True
        return ev

    def stop_before_shutdown(self, pid: int, *, proc_alive: bool = True) -> Dict[str, Any]:
        """Cleanly stop recording so MP4/MOV moov atoms flush BEFORE PCSX2 terminates."""
        if self.stopped:
            return {"already_stopped": True}
        self.stopped = True

        if self.effective_mode == "pcsx2":
            stop_ev: Optional[Dict[str, Any]] = None
            if proc_alive and (self.pcsx2_toggle_sent_start or self.has_active_pcsx2_video_file()):
                stop_ev = self._send_hotkey_toggle(pid, "pcsx2_stop_capture")
                self.pcsx2_toggle_sent_stop = bool(stop_ev.get("ok"))
                # Give PCSX2 FFmpeg muxer a brief moment to finalize moov atom before SIGTERM
                wait_for_video_file_ready(
                    self.videos_dir,
                    self.before_video_names,
                    timeout_sec=6.0,
                    video_tools_bin=self.video_tools_bin,
                )
            return {"mode": "pcsx2", "stop_event": stop_ev}

        if self.effective_mode == "screencapture" and self._screencapture_proc is not None:
            sc_proc = self._screencapture_proc
            stderr_text = ""
            if sc_proc.poll() is None:
                try:
                    sc_proc.send_signal(signal.SIGINT)
                    _, err = sc_proc.communicate(timeout=8.0)
                    stderr_text = (err or b"").decode("utf-8", errors="replace").strip()
                except subprocess.TimeoutExpired:
                    sc_proc.terminate()
                    try:
                        _, err = sc_proc.communicate(timeout=3.0)
                        stderr_text = (err or b"").decode("utf-8", errors="replace").strip()
                    except subprocess.TimeoutExpired:
                        sc_proc.kill()
                        sc_proc.wait(timeout=2.0)
            ev = {
                "event": "screencapture_stop",
                "returncode": sc_proc.returncode,
                "stderr": stderr_text,
            }
            self.events.append(ev)
            return {"mode": "screencapture", "stop_event": ev}

        return {"mode": self.effective_mode}

    def finalize_and_extract_qa(
        self,
        *,
        interval_sec: float = 1.5,
        fallback_frames_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Validate/publish `iter_dir/session_video.mp4` and run `extract_qa`."""
        result: Dict[str, Any] = {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "video_hotkey": self.video_hotkey,
            "pcsx2_hotkey_binding": self.pcsx2_hotkey_binding,
            "videos_dir": str(self.videos_dir),
            "events": self.events,
            "raw_video_path": None,
            "session_video_path": None,
            "video_valid": False,
            "probe": None,
            "qa": None,
        }

        if self.effective_mode == "off":
            return result

        if self.effective_mode == "burst":
            if fallback_frames_dir and fallback_frames_dir.is_dir():
                qa = extract_qa(
                    fallback_frames_dir,
                    self.iter_dir,
                    interval_sec=interval_sec,
                    video_tools_bin=self.video_tools_bin,
                )
                result["qa"] = qa
            return result

        # After PCSX2 exits (or screencapture exits), wait/probe for finalized video
        cand, probe = wait_for_video_file_ready(
            self.videos_dir,
            self.before_video_names,
            timeout_sec=6.0,
            video_tools_bin=self.video_tools_bin,
        )
        result["raw_video_path"] = str(cand) if cand else None
        result["probe"] = probe

        session_mp4 = self.iter_dir / "session_video.mp4"
        if cand is not None and probe and probe.get("valid"):
            if cand.suffix.lower() == ".mp4":
                shutil.copy2(cand, session_mp4)
                final_probe = probe_video(session_mp4, video_tools_bin=self.video_tools_bin)
            else:
                tc = transcode_to_mp4(
                    cand,
                    session_mp4,
                    preset="1280x720",
                    video_tools_bin=self.video_tools_bin,
                )
                result["transcode"] = tc
                final_probe = probe_video(session_mp4, video_tools_bin=self.video_tools_bin)

            result["probe"] = final_probe
            if final_probe.get("valid"):
                result["session_video_path"] = str(session_mp4.resolve())
                result["video_valid"] = True
                result["qa"] = extract_qa(
                    session_mp4,
                    self.iter_dir,
                    interval_sec=interval_sec,
                    video_tools_bin=self.video_tools_bin,
                )
                return result

        # Fallback: if video was not produced/valid, still build QA sheets from any captured frames
        if fallback_frames_dir and fallback_frames_dir.is_dir():
            pngs = list(fallback_frames_dir.glob("*.png"))
            if pngs:
                result["fallback_used"] = "captured_frames"
                result["qa"] = extract_qa(
                    fallback_frames_dir,
                    self.iter_dir,
                    interval_sec=interval_sec,
                    video_tools_bin=self.video_tools_bin,
                )
        return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Full-session video QA pipeline (AVFoundation extract, Pillow validation, contact sheets, telemetry)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # extract-qa
    p_ext = sub.add_parser(
        "extract-qa",
        help="Extract Pillow-validated timeline frames, multi-page contact sheets, and motion/health telemetry",
    )
    p_ext.add_argument("input", type=Path, help="Input video (.mp4/.mov/.m4v) or PNG frames directory")
    p_ext.add_argument("--out-dir", type=Path, required=True, help="Output directory for qa_frames/, qa_sheet_*.png, qa_telemetry.json")
    p_ext.add_argument("--interval", type=float, default=1.5, help="Keyframe extraction interval in seconds (default: 1.5)")
    p_ext.add_argument("--max-frames", type=int, default=400, help="Maximum frames to extract (default: 400)")
    p_ext.add_argument("--max-width", type=int, default=0, help="Optional max extracted frame width (0 = native)")
    p_ext.add_argument("--cols", type=int, default=4, help="Contact sheet columns (default: 4)")
    p_ext.add_argument("--rows", type=int, default=4, help="Contact sheet rows per page (default: 4)")
    p_ext.add_argument("--tile-width", type=int, default=480, help="Contact sheet tile width in px (default: 480)")
    p_ext.add_argument("--video-tools", type=Path, default=None, help="Optional precompiled video_tools binary path")
    p_ext.add_argument("--json", action="store_true", help="Emit full JSON telemetry to stdout")

    # probe
    p_probe = sub.add_parser("probe", help="Inspect a video file using native AVFoundation")
    p_probe.add_argument("video", type=Path, help="Video file to probe")
    p_probe.add_argument("--video-tools", type=Path, default=None)
    p_probe.add_argument("--json", action="store_true")

    # transcode
    p_tc = sub.add_parser("transcode", help="Transcode/remux a video to clean H.264 MP4")
    p_tc.add_argument("--input", type=Path, required=True)
    p_tc.add_argument("--output", type=Path, required=True)
    p_tc.add_argument("--preset", choices=["1280x720", "1920x1080", "high", "medium", "passthrough"], default="1280x720")
    p_tc.add_argument("--video-tools", type=Path, default=None)
    p_tc.add_argument("--json", action="store_true")

    # find-window
    p_fw = sub.add_parser("find-window", help="Find on-screen CGWindowID for PCSX2 or a PID")
    p_fw.add_argument("--pid", type=int, default=None)
    p_fw.add_argument("--owner", type=str, default="PCSX2")
    p_fw.add_argument("--video-tools", type=Path, default=None)
    p_fw.add_argument("--json", action="store_true")

    # synth-video
    p_syn = sub.add_parser("synth-video", help="Synthesize a deterministic H.264 MP4 for offline testing")
    p_syn.add_argument("--output", type=Path, required=True)
    p_syn.add_argument("--duration", type=float, default=4.0)
    p_syn.add_argument("--fps", type=int, default=15)
    p_syn.add_argument("--width", type=int, default=320)
    p_syn.add_argument("--height", type=int, default=180)
    p_syn.add_argument("--black-head", type=float, default=0.0)
    p_syn.add_argument("--static-tail", type=float, default=0.0)
    p_syn.add_argument("--clip-at", type=float, default=None)
    p_syn.add_argument("--amber-at", type=float, default=None)
    p_syn.add_argument("--video-tools", type=Path, default=None)
    p_syn.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "probe":
        info = probe_video(args.video, video_tools_bin=args.video_tools)
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print(
                f"{info.get('path')}: valid={info.get('valid')} "
                f"{info.get('width', 0)}x{info.get('height', 0)} "
                f"dur={info.get('duration_sec', 0)}s fps={info.get('nominal_fps', 0)} "
                f"codec={info.get('video_codec', '')}"
            )
        return 0 if info.get("valid") else 1

    if args.command == "transcode":
        res = transcode_to_mp4(
            args.input,
            args.output,
            preset=args.preset,
            video_tools_bin=args.video_tools,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if args.command == "find-window":
        res = find_window(pid=args.pid, owner=args.owner, video_tools_bin=args.video_tools)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if args.command == "synth-video":
        res = synthesize_test_video(
            args.output,
            duration_sec=args.duration,
            fps=args.fps,
            width=args.width,
            height=args.height,
            black_head_sec=args.black_head,
            static_tail_sec=args.static_tail,
            clip_at_sec=args.clip_at,
            amber_at_sec=args.amber_at,
            video_tools_bin=args.video_tools,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if args.command == "extract-qa":
        res = extract_qa(
            args.input,
            args.out_dir,
            interval_sec=args.interval,
            max_frames=args.max_frames,
            max_width=args.max_width,
            sheet_cols=args.cols,
            sheet_rows=args.rows,
            tile_width=args.tile_width,
            video_tools_bin=args.video_tools,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            s = res.get("summary") or {}
            print(
                f"QA extract ok={res.get('ok')} frames={s.get('valid_frames', 0)}/{s.get('total_frames_extracted', 0)} "
                f"corrupt={s.get('corrupt_frames_quarantined', 0)} sheets={len(res.get('qa_sheets') or [])} "
                f"motion={s.get('motion_detected')} flags={s.get('health_flags')}"
            )
            for sh in res.get("qa_sheets") or []:
                print(f"  Sheet {sh['sheet_index']:02d}: {sh['path']} ({sh['frame_count']} frames, t={sh['t_start_sec']}s..{sh['t_end_sec']}s)")
            print(f"  Telemetry: {res.get('telemetry_path')}")
        return 0 if res.get("ok") else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
