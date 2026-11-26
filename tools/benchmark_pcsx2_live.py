#!/usr/bin/env python3
"""
benchmark_pcsx2_live.py — live A/B benchmark: original PS2 vs v2.1 "Metal 2.3" 2026 profile (SLUS-21351).

Each profile runs in an isolated PCSX2 sandbox (HOME override; global PCSX2.ini, memcards and ISO verified
untouched), is driven by PINE autopilot, recorded to MP4, measured for honest world-loop FPS + sim timestep,
and stitched into five letterboxed side-by-side PNGs plus `benchmark_report.json`.

Usage: python3 tools/benchmark_pcsx2_live.py --iso /path/to/NFSMW.iso --scale 6 --modes freeroam,challenge --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import pcsx2_profile_2026 as profiles  # noqa: E402

PNACH_PATH = REPO_ROOT / "patches" / "pnach" / "SLUS-21351_0518D274.pnach"
CHECKPOINT_LABELS = ["01_boot_loading", "02_main_menu", "03_drive_launch", "04_drive_mid", "05_drive_highspeed"]
ADDR_DISPLAY_FRAME_COUNTER = 0x0054B5E4  # SLUS_213.51 eDisplayFrame() hardware swap counter (0x0020DCE0)
FLOW_STATE_RACING = 6
PANEL_BG = (0x0E, 0x12, 0x1A)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_active(r: Dict[str, Any], key: str = "world_loop_counter") -> bool:
    c = r.get(key)
    return (r.get("flow_state") == FLOW_STATE_RACING and not r.get("paused") and r.get("player_present") is True
            and isinstance(c, int) and not isinstance(c, bool) and _num(r.get("timestamp")))


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(pct / 100.0 * len(s)) - 1))] if s else None


def _r2(v: Optional[float]) -> Optional[float]:
    return round(v, 2) if v is not None else None


def _counter_rate(records: Sequence[Dict[str, Any]], key: str, window_sec: float) -> Tuple[int, float, List[float]]:
    total_df, total_dt, acc_df, acc_dt = 0, 0.0, 0, 0.0
    windows: List[float] = []
    for a, b in zip(records, records[1:]):
        ok = _is_active(a, key) and _is_active(b, key)
        df = (b[key] - a[key]) if ok else 0
        dt = (float(b["timestamp"]) - float(a["timestamp"])) if ok else 0.0
        if df <= 0 or not 0.005 <= dt <= 2.0:
            acc_df, acc_dt = 0, 0.0
            continue
        total_df, total_dt = total_df + df, total_dt + dt
        acc_df, acc_dt = acc_df + df, acc_dt + dt
        if acc_dt >= window_sec:
            windows.append(acc_df / acc_dt)
            acc_df, acc_dt = 0, 0.0
    return total_df, total_dt, windows


def compute_pine_fps_metrics(records: Sequence[Dict[str, Any]], *, window_sec: float = 0.5) -> Dict[str, Any]:
    """Honest FPS from eDisplayFrame (0x0054B5E4) when present, plus 60Hz WorldLoopCounter sim rate."""
    has_disp = any(isinstance(r.get("display_frame_counter"), int) and not isinstance(r.get("display_frame_counter"), bool) for r in records)
    key = "display_frame_counter" if has_disp else "world_loop_counter"
    total_df, total_dt, windows = _counter_rate(records, key, window_sec)
    wlc_df, wlc_dt, _ = _counter_rate(records, "world_loop_counter", window_sec) if has_disp else (total_df, total_dt, [])
    distance = 0.0
    for a, b in zip(records, records[1:]):
        if _is_active(a, key) and _is_active(b, key) and 0.005 <= (float(b["timestamp"]) - float(a["timestamp"])) <= 2.0:
            pa, pb = a.get("pos"), b.get("pos")
            if isinstance(pa, list) and isinstance(pb, list) and len(pa) == len(pb) == 3 and all(map(_num, pa + pb)):
                distance += math.dist(pa, pb)
    speeds = [abs(float(r["speed_mph"])) for r in records if _is_active(r, key) and _num(r.get("speed_mph"))]
    dts = [float(r["sim_timestep"]) for r in records if _num(r.get("sim_timestep"))]
    return {
        "fps_source": "eDisplayFrame(0x0054B5E4)" if has_disp else "WorldLoopCounter(0x00574304)",
        "avg_fps": _r2(total_df / total_dt) if total_dt > 0 else None, "p50_fps": _r2(_percentile(windows, 50)),
        "p95_fps": _r2(_percentile(windows, 95)), "min_fps": _r2(min(windows)) if windows else None,
        "max_fps": _r2(max(windows)) if windows else None, "window_count": len(windows),
        "display_frame_delta": total_df if has_disp else None, "world_loop_delta": wlc_df,
        "sim_loop_hz": _r2(wlc_df / wlc_dt) if wlc_dt > 0 else None, "active_racing_sec": round(total_dt, 3),
        "avg_speed_mph": _r2(statistics.fmean(speeds)) if speeds else None,
        "max_speed_mph": _r2(max(speeds)) if speeds else None, "distance_traveled_m": round(distance, 2),
        "sim_timestep_s": statistics.median(dts) if dts else None,
    }


def validate_png_strict(path: Path) -> Tuple[bool, Path, Optional[str]]:
    from video_qa_pipeline import validate_and_quarantine_png

    return validate_and_quarantine_png(Path(path))


def _frame_stats(img: Any) -> Dict[str, Any]:
    hist = img.convert("L").histogram()
    total = max(1, sum(hist))
    return {"width": img.width, "height": img.height,
            "mean_luma": round(sum(i * c for i, c in enumerate(hist)) / total, 2),
            "non_black_coverage": round(sum(hist[17:]) / total, 4)}


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def stitch_side_by_side(left_png: Path, right_png: Path, out_png: Path, *, title: str, left_label: str,
                        right_label: str, left_meta: str = "", right_meta: str = "", panel_width: int = 960,
                        panel_height: int = 540) -> Dict[str, Any]:
    """Letterbox both frames (exact aspect ratio, never stretched) into a labeled side-by-side PNG."""
    from PIL import Image, ImageDraw

    gap, title_h, label_h, footer_h = 16, 52, 34, 40
    canvas = Image.new("RGB", (2 * panel_width + 3 * gap, title_h + label_h + panel_height + footer_h + gap),
                       (0x07, 0x0A, 0x0F))
    draw = ImageDraw.Draw(canvas)
    draw.text((gap, 14), title, fill=(240, 240, 240), font=_font(26))
    metrics: List[Dict[str, Any]] = []
    for idx, (src, label, meta) in enumerate(((left_png, left_label, left_meta), (right_png, right_label, right_meta))):
        ok, _, err = validate_png_strict(Path(src))
        if not ok:
            raise ValueError(f"invalid input PNG {src}: {err}")
        x0, y0 = gap + idx * (panel_width + gap), title_h + label_h
        with Image.open(src) as im:
            im = im.convert("RGB")
            metrics.append(_frame_stats(im))
            scale = min(panel_width / im.width, panel_height / im.height)
            w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
            panel = Image.new("RGB", (panel_width, panel_height), PANEL_BG)
            panel.paste(im.resize((w, h), Image.LANCZOS), ((panel_width - w) // 2, (panel_height - h) // 2))
        canvas.paste(panel, (x0, y0))
        draw.text((x0, title_h + 6), f"{label}  ({metrics[-1]['width']}x{metrics[-1]['height']})",
                  fill=(255, 196, 92) if idx else (170, 190, 220), font=_font(20))
        draw.text((x0, y0 + panel_height + 10), meta, fill=(210, 210, 210), font=_font(16))
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png, format="PNG")
    with Image.open(out_png) as im:
        im.verify()
    with Image.open(out_png) as im:
        im.load()
        out_w, out_h = im.size
    return {"path": str(out_png), "width": out_w, "height": out_h, "valid": True,
            "left_metrics": metrics[0], "right_metrics": metrics[1]}


def _valid_frames(dirs: Sequence[Path]) -> List[Path]:
    from PIL import Image

    out: List[Path] = []
    for p in (p for d in dirs if d.is_dir() for p in sorted(d.glob("*.png"))):
        if validate_png_strict(p)[0]:
            with Image.open(p) as im:
                if _frame_stats(im)["non_black_coverage"] > 0.03:
                    out.append(p)
    return out


def select_five_checkpoints(run_dir: Path) -> List[Tuple[str, Path]]:
    """Pick 5 labeled boot/menu/drive checkpoints from QA video frames + F8 driving snaps."""
    run_dir = Path(run_dir)
    qa_dirs = [run_dir / "qa_frames"] + [d for d in sorted(run_dir.rglob("qa_frames")) if d != run_dir / "qa_frames"]
    qa, snaps = _valid_frames(qa_dirs), _valid_frames([run_dir / "frames"])
    pool = qa + snaps
    if not pool:
        return []

    def pick(seq: Sequence[Path], frac: float) -> Path:
        return seq[min(len(seq) - 1, int(round(frac * (len(seq) - 1))))]

    from PIL import Image
    boot_png = pick(qa or pool, 0.05)
    if len(qa) >= 12:
        menu_band = qa[len(qa) // 6 : len(qa) // 2]
        def _cov(p: Path) -> float:
            with Image.open(p) as im:
                return float(_frame_stats(im)["non_black_coverage"])
        menu_png = max(menu_band, key=_cov)
    else:
        menu_png = pick(qa, 0.3) if qa else snaps[0]
    drive_src = snaps[1:] if len(snaps) >= 4 else (snaps if len(snaps) >= 3 else (qa[len(qa) // 2:] if len(qa) >= 3 else pool))
    chosen = [boot_png, menu_png] + [pick(drive_src, f) for f in (0, 0.5, 1)]
    return list(zip(CHECKPOINT_LABELS, chosen))


def _run_pine_autopilot_inproc(pine_slot: int, pid: int, key_driver_bin: Path, *, duration: float,
                               wait_racing_timeout: float, menu_mode: str, telemetry: Path) -> Dict[str, Any]:
    import pcsx2_pine_driver as pine

    class _DualReader(pine.NfsmwTelemetryReader):
        def read_telemetry(self) -> Any:
            tel = super().read_telemetry()
            disp = self.pine.read_u32(ADDR_DISPLAY_FRAME_COUNTER)
            sim_dt = None
            sim_ptr = self.pine.read_u32(pine.ADDR_SIM_SYSTEM_PTR)
            if pine.is_valid_ee_ptr(sim_ptr):
                v = self.pine.read_f32(sim_ptr + pine.OFF_SIM_TIMESTEP)
                if _num(v) and 0.001 <= v <= 0.2:
                    sim_dt = float(v)
            orig_to_dict = tel.to_dict
            tel.to_dict = lambda: {**orig_to_dict(), "display_frame_counter": disp, "sim_timestep": sim_dt}  # type: ignore[method-assign]
            return tel

    cfg = pine.AutopilotConfig(verify_reverse_demo=False)
    with pine.PineClient(slot=pine_slot, timeout=1.0) as client:
        sink = pine.KeyDriverSession(key_driver_bin, pid=pid, tap_mode="auto")
        sink.start()
        try:
            return pine.run_closed_loop_autopilot(
                _DualReader(client), sink, duration_sec=duration, config=cfg, telemetry_jsonl_path=telemetry,
                f8_interval_sec=5.0, wait_for_racing_timeout=wait_racing_timeout,
                menu_auto_advance=True, menu_mode=menu_mode)
        finally:
            sink.close()


def probe_sim_timestep(pine_slot: int, timeout: float = 1.0) -> Optional[float]:
    import pcsx2_pine_driver as pine

    try:
        with pine.PineClient(slot=pine_slot, timeout=timeout) as client:
            sim_ptr = client.read_u32(pine.ADDR_SIM_SYSTEM_PTR)
            if not pine.is_valid_ee_ptr(sim_ptr):
                return None
            dt = client.read_f32(sim_ptr + pine.OFF_SIM_TIMESTEP)
    except (pine.PineError, OSError):
        return None
    return float(dt) if _num(dt) and 0.001 <= dt <= 0.2 else None


def _integrity_snapshot(iso_path: Path, data_dir: Path, preflight: Dict[str, Any]) -> Dict[str, Any]:
    from pcsx2_live_verify import _sha256_file

    ini = Path((preflight.get("global_pcsx2_ini") or {}).get("path") or data_dir / "inis" / "PCSX2.ini")
    mc, st = data_dir / "memcards", iso_path.stat()
    return {"global_pcsx2_ini": _sha256_file(ini) if ini.is_file() else None,
            "memcards": {p.name: _sha256_file(p) for p in sorted(mc.iterdir()) if p.is_file()} if mc.is_dir() else {},
            "iso": [st.st_size, st.st_mtime_ns]}


def run_benchmark_session(iso_path: Path, session: str, profile_ini: str, *, menu_mode: str, bench_dir: Path,
                          preflight: Dict[str, Any], data_dir: Path, duration: float = 25.0,
                          wait_racing_timeout: float = 50.0, pine_slot: int = 28011) -> Dict[str, Any]:
    """Run one isolated live PCSX2 session with the given per-game INI; returns metrics + artifact paths."""
    import pcsx2_live_verify as lv
    import race_flywheel as rf
    import video_qa_pipeline as vqa

    root = Path(bench_dir) / session
    if root.exists():
        shutil.rmtree(root)
    iter_dir, staged = root / "run", root / "patches_src"
    for d in (iter_dir / "frames", staged):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PNACH_PATH, staged / PNACH_PATH.name)
    info = rf.prepare_flywheel_sandbox(root, iter_dir, data_dir, preflight, patches_dir=staged, record_video=True,
                                       enable_pine=True, pine_slot=pine_slot)
    Path(info["target_gamesettings_ini"]).write_text(profile_ini, encoding="utf-8")

    snaps_dir, emu_log = Path(info["snaps_dir"]), iter_dir / "pcsx2_emulog.txt"
    telemetry = iter_dir / "pine_telemetry.jsonl"
    key_driver_bin = rf.ensure_key_driver_binary()
    before_snaps = {p.name for p in snaps_dir.iterdir()} if snaps_dir.is_dir() else set()
    recorder = vqa.SessionVideoRecorder(mode="auto", iter_dir=iter_dir, videos_dir=Path(info["videos_dir"]),
                                        key_driver_bin=key_driver_bin,
                                        video_tools_bin=vqa.ensure_video_tools_binary())
    cmd = [str(preflight["pcsx2_app"]["binary_path"]), "-batch", "-fastboot", "-logfile", str(emu_log.resolve()),
           "--", str(Path(iso_path).resolve())]
    result: Dict[str, Any] = {"session": session, "menu_mode": menu_mode, "run_dir": str(iter_dir)}
    started = time.monotonic()
    with open(iter_dir / "pcsx2_boot.log", "wb") as boot_fp:
        proc = subprocess.Popen(cmd, stdout=boot_fp, stderr=subprocess.STDOUT,
                                env=dict(os.environ, HOME=str(info["sandbox_home"])))
        try:
            rf._wait_for_gs_window_ready(emu_log, proc, timeout_sec=8.0)
            recorder.start(proc.pid)
            rf._wait_for_emulation_ready(emu_log, proc, timeout_sec=30.0)
            rf._wait_for_pine_socket(pine_slot, proc, timeout_sec=15.0)
            for _ in range(3):
                if recorder.has_active_pcsx2_video_file():
                    break
                recorder.ensure_started_retry(proc.pid)
                time.sleep(0.8)
            result["autopilot_summary"] = _run_pine_autopilot_inproc(
                pine_slot, proc.pid, key_driver_bin, duration=duration,
                wait_racing_timeout=wait_racing_timeout, menu_mode=menu_mode, telemetry=telemetry)
            result["driver_rc"] = 0
            result["probed_sim_timestep_s"] = probe_sim_timestep(pine_slot)
            if recorder.has_active_pcsx2_video_file():
                recorder.stop_before_shutdown(proc.pid, proc_alive=proc.poll() is None)
            rf._wait_for_snaps_flush(snaps_dir, before_snaps, emu_log, expected_count=1, timeout_sec=8.0, proc=proc)
        except Exception as exc:  # noqa: BLE001 — always shut PCSX2 down and report
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            result["terminate"] = lv.terminate_process_cleanly(proc)
    result["wall_sec"] = round(time.monotonic() - started, 2)

    for p in sorted(snaps_dir.glob("*.png")) if snaps_dir.is_dir() else []:
        if p.name not in before_snaps and lv._wait_for_complete_png(p) and validate_png_strict(p)[0]:
            shutil.copy2(p, iter_dir / "frames" / p.name)
    try:
        qa = recorder.finalize_and_extract_qa(interval_sec=1.5, fallback_frames_dir=iter_dir / "frames")
    except Exception as exc:  # noqa: BLE001
        qa = {"error": f"{type(exc).__name__}: {exc}"}
    result.update(session_video_path=qa.get("session_video_path"), qa_summary=(qa.get("qa") or {}).get("summary"))
    log_text = emu_log.read_text(encoding="utf-8", errors="replace") if emu_log.is_file() else ""
    iso_info = preflight.get("iso") or {}
    result["log"] = lv.parse_pcsx2_log(log_text, expected_serial=iso_info.get("serial"),
                                       expected_crc=iso_info.get("elf_crc"))
    records = []
    for line in telemetry.read_text(encoding="utf-8").splitlines() if telemetry.is_file() else []:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    metrics = compute_pine_fps_metrics(records)
    if metrics["sim_timestep_s"] is None:
        metrics["sim_timestep_s"] = result.get("probed_sim_timestep_s")
    result.update(metrics=metrics, telemetry_records=len(records))
    return result


def _meta(res: Dict[str, Any]) -> str:
    m = res.get("metrics") or {}
    dt = m.get("sim_timestep_s")
    dt_s = f"{dt:.4f}s" if isinstance(dt, (int, float)) else "N/A"
    return (f"avg {m.get('avg_fps')} FPS | p50 {m.get('p50_fps')} | p95 {m.get('p95_fps')} | "
            f"sim dt {dt_s} | max {m.get('max_speed_mph')} mph")


def _default_iso_path() -> Optional[Path]:
    env = os.environ.get("NFSMW_ISO", "").strip()
    if env:
        return Path(env).expanduser()
    cand = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    return cand if cand.is_file() else None


def run_full_benchmark(iso_path: Path, *, out_dir: Path, bench_dir: Path, scale: int = 6, rgss: bool = False,
                       ee_cycle_rate: int = 3, duration: float = 25.0, wait_racing_timeout: float = 50.0,
                       modes: Sequence[str] = ("freeroam", "challenge"), pine_slot: int = 28011) -> Dict[str, Any]:
    import pcsx2_live_verify as lv

    iso_path, out_dir, bench_dir = Path(iso_path), Path(out_dir), Path(bench_dir)
    data_dir = lv.default_pcsx2_data_dir()
    preflight = lv.run_preflight(iso_path)
    if not preflight.get("ok"):
        return {"ok": False, "error": "preflight failed", "preflight": preflight}
    before = _integrity_snapshot(iso_path, data_dir, preflight)
    v21 = profiles.build_v21_metal_profile(scale=scale, rgss=rgss, ee_cycle_rate=ee_cycle_rate)
    plan = [("original_ps2", profiles.build_original_ps2_baseline_profile(), "freeroam"),
            ("v21_2026_freeroam", v21, "freeroam")] + ([("v21_2026_challenge", v21, "challenge")]
                                                       if "challenge" in modes else [])
    out_dir.mkdir(parents=True, exist_ok=True)
    sessions: Dict[str, Dict[str, Any]] = {}
    for idx, (name, ini, mode) in enumerate(plan):
        res = run_benchmark_session(iso_path, name, ini, menu_mode=mode, bench_dir=bench_dir, preflight=preflight,
                                    data_dir=data_dir, duration=duration, wait_racing_timeout=wait_racing_timeout,
                                    pine_slot=pine_slot + idx)
        video = res.get("session_video_path")
        if video and Path(video).is_file():
            res["video_out"] = str(shutil.copy2(video, out_dir / f"{name}.mp4"))
        sessions[name] = res

    left, right = sessions["original_ps2"], sessions["v21_2026_freeroam"]
    comparisons: List[Dict[str, Any]] = []
    lcp, rcp = select_five_checkpoints(Path(left["run_dir"])), select_five_checkpoints(Path(right["run_dir"]))
    for (label, lp), (_, rp) in zip(lcp, rcp):
        try:
            comparisons.append(stitch_side_by_side(
                lp, rp, out_dir / f"compare_{label}.png", title=f"NFSMW SLUS-21351 | {label}",
                left_label="Original PS2 (1x, 4:3)", right_label=f"v2.1 Metal 2026 ({scale}x, 16:9)",
                left_meta=_meta(left), right_meta=_meta(right)))
        except (ValueError, OSError) as exc:
            comparisons.append({"label": label, "valid": False, "error": str(exc)})
    after = _integrity_snapshot(iso_path, data_dir, preflight)
    untouched = {k: before[k] == after[k] for k in before}
    report = {"ok": all(untouched.values()) and len(comparisons) == 5 and all(c.get("valid") for c in comparisons),
              "untouched": untouched, "sessions": sessions, "comparisons": comparisons,
              "config": {"scale": scale, "rgss": rgss, "ee_cycle_rate": ee_cycle_rate, "duration": duration,
                         "modes": list(modes)}}
    (out_dir / "benchmark_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live PCSX2 original-vs-v2.1 benchmark for SLUS-21351")
    ap.add_argument("--iso", type=Path, default=_default_iso_path())
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "build" / "benchmarks" / "v2.1")
    ap.add_argument("--bench-dir", type=Path, default=REPO_ROOT / "build" / "pcsx2_bench_v21")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--rgss", action="store_true")
    ap.add_argument("--ee-cycle-rate", type=int, default=3)
    ap.add_argument("--duration", type=float, default=25.0)
    ap.add_argument("--wait-racing-timeout", type=float, default=50.0)
    ap.add_argument("--modes", default="freeroam,challenge")
    ap.add_argument("--pine-slot", type=int, default=28011)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.iso or not Path(args.iso).is_file():
        print("error: --iso (or NFSMW_ISO) must point to an existing SLUS-21351 ISO", file=sys.stderr)
        return 2
    report = run_full_benchmark(
        Path(args.iso), out_dir=args.out_dir, bench_dir=args.bench_dir, scale=args.scale, rgss=args.rgss,
        ee_cycle_rate=args.ee_cycle_rate, duration=args.duration, wait_racing_timeout=args.wait_racing_timeout,
        modes=[m.strip() for m in args.modes.split(",") if m.strip()], pine_slot=args.pine_slot)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for name, res in (report.get("sessions") or {}).items():
            print(f"{name}: {_meta(res)}")
        print(f"untouched={report.get('untouched')} ok={report.get('ok')}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
