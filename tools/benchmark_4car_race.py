#!/usr/bin/env python3
"""
benchmark_4car_race.py — real 4-car (3 AI opponents) Quick Race benchmark + ablation matrix for SLUS-21351.

Measures only during an ACTUAL race (GameFlow RACING, GRaceStatus active, >= 3 AI racers seen via PINE):
  * True host emulation speed: host_speed_pct = 100 * (dWorldLoopCounter / 60) / dWall (75% == the slow-mo bug)
  * True display FPS from eDisplayFrame (0x0054B5E4), sampled at ~250 Hz on the shared PINE socket:
    avg / p50 / p10 / 1%-low / min and frame-time stutters > 25 ms. No F8 snaps (each one stalls the GS).
  * Visual parity vs the v2.4.1 baseline: PINE position-matched video crops (luma, contrast, edge energy).

Variants re-use the MIPS-verified words in patches/elf_patches.json: "restore" writes a baked v2.4.1 patch's stock
words back at runtime (no ISO rebuild), "apply" adds an opt-in patch; "ini" overrides go to the sandbox per-game INI.
Refuses to launch while another PCSX2 runs; ISO is read-only; global PCSX2.ini / memcards are hash-verified.

Usage:
  python3 tools/benchmark_4car_race.py --list-variants
  python3 tools/benchmark_4car_race.py --iso build/v241.iso --variants v241_baseline,ee_rate_2,race_balanced
  python3 tools/benchmark_4car_race.py --analyze-run build/pcsx2_race_bench/v241_baseline/run   # offline
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import benchmark_pcsx2_live as live  # noqa: E402
import pcsx2_pine_driver as pine  # noqa: E402
import pcsx2_profile_2026 as profiles  # noqa: E402

PATCHES_PATH = REPO_ROOT / "patches" / "elf_patches.json"
ADDR_DISPLAY_FRAME = live.ADDR_DISPLAY_FRAME_COUNTER  # 0x0054B5E4 eDisplayFrame swap counter
ADDR_AIRACERS_COUNT = pine.ADDR_IVEHICLE_LIST_BASE + 3 * pine.IVEHICLE_LIST_STRIDE + pine.OFF_LIST_COUNT  # 0x00574BF0
FLOW_RACING = int(pine.GameFlowState.RACING)
SIM_HZ = 60.0
STUTTER_MS = 25.0
BASELINE = "v241_baseline"
RACE_GATE = {"host_speed_avg_pct": 98.0, "host_speed_min_pct": 95.0, "display_fps_avg": 58.0,
             "display_fps_p10": 55.0, "min_opponents": 3}
PARITY_CROP = (0.15, 0.30, 0.85, 0.88)  # road + opponent band (skips HUD minimap/speedo corners)
PARITY_TOL = {"luma_abs": 6.0, "contrast_min": 0.90, "contrast_max": 1.10, "edge_min": 0.92, "edge_max": 1.10}

VARIANTS: Dict[str, Dict[str, Any]] = {
    BASELINE: {"desc": "v2.4.1 as shipped: 6 baked defaults, EE 300%, 0x550D28=0, 60Hz VSync"},
    "stock_reflections": {"desc": "stock 3 envmap faces/frame", "restore": ["full_rate_car_reflections"]},
    "reflections_4face": {"desc": "4 envmap faces/frame", "restore": ["full_rate_car_reflections"],
                          "apply": ["balanced_car_reflections_4_faces"]},
    "stock_draw_distance": {"desc": "stock 18 px / 8.7f scenery cull", "restore": ["extended_scenery_draw_distance"]},
    "draw_distance_2x": {"desc": "2x scenery (9 px / 4.35f)", "restore": ["extended_scenery_draw_distance"],
                         "apply": ["balanced_scenery_draw_distance_2x"]},
    "stock_car_lod": {"desc": "stock distance-based car LOD", "restore": ["high_lod_cars"]},
    "car_lod_3x": {"desc": "LOD A to 3x distance (120 -> 40 px)", "restore": ["high_lod_cars"],
                   "apply": ["balanced_car_lod_3x_distance"]},
    "ee_rate_2": {"desc": "EECycleRate 2 (180%)", "ini": {"EmuCore/Speedhacks": {"EECycleRate": "2"}}},
    "ee_rate_1": {"desc": "EECycleRate 1 (130%)", "ini": {"EmuCore/Speedhacks": {"EECycleRate": "1"}}},
    "ee_rate_0": {"desc": "EECycleRate 0 (100%)", "ini": {"EmuCore/Speedhacks": {"EECycleRate": "0"}}},
    "mtvu_on": {"desc": "force MTVU (vuThread)", "ini": {"EmuCore/Speedhacks": {"vuThread": "true"}}},
    "thread_pinning": {"desc": "EnableThreadPinning", "ini": {"EmuCore": {"EnableThreadPinning": "true"}}},
    "cdvd_precache": {"desc": "CdvdPrecache (ISO into RAM)", "ini": {"EmuCore": {"CdvdPrecache": "true"}}},
    "cpu_clut_off": {"desc": "CPUCLUTRender 0 (its LUT pass is off with 0x550D28=0)",
                     "ini": {"EmuCore/GS": {"UserHacks_CPUCLUTRender": "0"}}},
    "blend_0": {"desc": "accurate_blending_unit 0", "ini": {"EmuCore/GS": {"accurate_blending_unit": "0"}}},
    "trifilter_1": {"desc": "TriFilter 1 (PS2 trilinear)", "ini": {"EmuCore/GS": {"TriFilter": "1"}}},
    "upscale_5": {"desc": "5x upscale", "ini": {"EmuCore/GS": {"upscale_multiplier": "5"}}},
    "upscale_4": {"desc": "4x upscale", "ini": {"EmuCore/GS": {"upscale_multiplier": "4"}}},
    "race_balanced": {"desc": "4-face reflections + 2x scenery + car LOD 3x + CPUCLUT 0",
                      "restore": ["full_rate_car_reflections", "extended_scenery_draw_distance", "high_lod_cars"],
                      "apply": ["balanced_car_reflections_4_faces", "balanced_scenery_draw_distance_2x",
                                "balanced_car_lod_3x_distance"],
                      "ini": {"EmuCore/GS": {"UserHacks_CPUCLUTRender": "0"}}},
}


# ---------------------------------------------------------------------------------------------------------------
# Pure metrics (offline-testable)
# ---------------------------------------------------------------------------------------------------------------

def host_speed_pct(d_world_loop: float, d_wall_sec: float) -> Optional[float]:
    """100% == the 60 Hz sim advances in real time; 75% == the user's slow-motion race."""
    return 100.0 * (d_world_loop / SIM_HZ) / d_wall_sec if d_wall_sec > 0 else None


def _racing(s: Dict[str, Any]) -> bool:
    return s.get("flow") == FLOW_RACING and all(isinstance(s.get(k), (int, float)) for k in ("t", "wlc"))


def host_speed_metrics(samples: Sequence[Dict[str, Any]], *, window_sec: float = 1.0) -> Dict[str, Any]:
    """samples: [{t (wall s), flow, wlc (WorldLoopCounter)}]; windows break on non-racing, counter resets or
    glitch reads (a jump implying the sim ran > 2x real time)."""
    tot_w, tot_t, acc_w, acc_t = 0, 0.0, 0, 0.0
    windows: List[float] = []
    for a, b in zip(samples, samples[1:]):
        dw, dt = (b.get("wlc") or 0) - (a.get("wlc") or 0), float(b.get("t", 0)) - float(a.get("t", 0))
        if not (_racing(a) and _racing(b)) or dw < 0 or dw > 2 + 2 * SIM_HZ * dt or not 0 < dt <= 10.0:
            acc_w, acc_t = 0, 0.0
            continue
        tot_w, tot_t, acc_w, acc_t = tot_w + dw, tot_t + dt, acc_w + dw, acc_t + dt
        if acc_t >= window_sec:
            windows.append(host_speed_pct(acc_w, acc_t) or 0.0)
            acc_w, acc_t = 0, 0.0
    avg = host_speed_pct(tot_w, tot_t)
    return {"avg_pct": live._r2(avg), "p10_pct": live._r2(live._percentile(windows, 10)),
            "min_pct": live._r2(min(windows)) if windows else None,
            "pct_of_race_ge_98": round(100.0 * sum(w >= 98.0 for w in windows) / len(windows), 1) if windows else None,
            "window_count": len(windows), "racing_sec": round(tot_t, 2), "world_loop_delta": tot_w}


def frame_times_ms(samples: Sequence[Dict[str, Any]]) -> List[float]:
    """Frame times between eDisplayFrame increments (k swaps in one poll are split evenly)."""
    out: List[float] = []
    anchor: Optional[float] = None
    last: Optional[int] = None
    for s in samples:
        d = s.get("disp")
        if not _racing(s) or not isinstance(d, int):
            anchor, last = None, None
            continue
        if last is not None and d != last:
            dd = d - last
            if anchor is not None and 0 < dd <= 120:
                out.extend([(float(s["t"]) - anchor) * 1000.0 / dd] * dd)
            anchor = float(s["t"])
        last = d
    return out


def display_metrics(frame_ms: Sequence[float], *, window_sec: float = 1.0) -> Dict[str, Any]:
    """avg = frames / time; p50/p10/min over 1 s windows; 1%-low = 1000 / p99 frame time."""
    ft = [f for f in frame_ms if f > 0]
    windows: List[float] = []
    n, acc = 0, 0.0
    for f in ft:
        n, acc = n + 1, acc + f
        if acc >= window_sec * 1000.0:
            windows.append(n * 1000.0 / acc)
            n, acc = 0, 0.0
    p99 = live._percentile(ft, 99)
    return {"avg_fps": live._r2(len(ft) * 1000.0 / sum(ft)) if ft else None,
            "p50_fps": live._r2(live._percentile(windows, 50)), "p10_fps": live._r2(live._percentile(windows, 10)),
            "p1_low_fps": live._r2(1000.0 / p99) if p99 else None, "min_fps": live._r2(min(windows)) if windows else None,
            "stutters_gt_25ms": sum(f > STUTTER_MS for f in ft), "max_frame_ms": live._r2(max(ft)) if ft else None,
            "frames": len(ft), "window_count": len(windows)}


def race_detection(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Confirms an ACTUAL race: RACING flow, GRaceStatus active, and >= 3 AI racers (not solo free-roam)."""
    racing = [r for r in records if r.get("flow_state") == FLOW_RACING]

    def _cnt(r: Dict[str, Any], k: str) -> int:
        v = r.get(k)
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 16 else 0

    ai = max((_cnt(r, "ai_racer_count") for r in racing), default=0)
    racers = max((_cnt(r, "racer_count") for r in racing), default=0)
    active = any(r.get("race_active") is True for r in racing)
    opponents = max(ai, racers - 1 if racers else 0)
    return {"racing_samples": len(racing), "race_active_seen": active, "max_ai_racers": ai,
            "max_racer_count": racers, "opponents": opponents,
            "four_car_race": active and opponents >= RACE_GATE["min_opponents"]}


def evaluate_gate(host: Dict[str, Any], disp: Dict[str, Any], race: Dict[str, Any],
                  gate: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    g = {**RACE_GATE, **(gate or {})}
    checks = {"four_car_race": (race.get("opponents"), g["min_opponents"], bool(race.get("four_car_race"))),
              "host_speed_avg_pct": (host.get("avg_pct"), g["host_speed_avg_pct"], None),
              "host_speed_min_pct": (host.get("min_pct"), g["host_speed_min_pct"], None),
              "display_fps_avg": (disp.get("avg_fps"), g["display_fps_avg"], None),
              "display_fps_p10": (disp.get("p10_fps"), g["display_fps_p10"], None)}
    out = {k: {"value": v, "threshold": thr, "ok": ok if ok is not None else (isinstance(v, (int, float)) and v >= thr)}
           for k, (v, thr, ok) in checks.items()}
    failures = [k for k, c in out.items() if not c["ok"]]
    return {"pass": not failures, "checks": out, "failures": failures}


def compare_parity(base: Dict[str, float], cand: Dict[str, float],
                   tol: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Statistical crop parity (AI/traffic differ per run, so no pixel diff). Edge > max flags double-lining."""
    t = {**PARITY_TOL, **(tol or {})}
    dl = cand["luma"] - base["luma"]
    cr = cand["contrast"] / base["contrast"] if base["contrast"] else float("inf")
    er = cand["edge"] / base["edge"] if base["edge"] else float("inf")
    ok = abs(dl) <= t["luma_abs"] and t["contrast_min"] <= cr <= t["contrast_max"] and t["edge_min"] <= er <= t["edge_max"]
    return {"luma_delta": round(dl, 2), "contrast_ratio": round(cr, 3), "edge_ratio": round(er, 3), "ok": ok}


def crop_stats(png: Path, box: Tuple[float, float, float, float] = PARITY_CROP) -> Dict[str, float]:
    from PIL import Image, ImageFilter, ImageStat

    with Image.open(png) as im:
        w, h = im.size
        c = im.convert("L").crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
        c = c.resize((640, max(1, round(640 * c.height / max(1, c.width)))))
        # PIL kernels copy the 1 px border unfiltered; trim it so edge energy does not scale with luma
        st, edge = ImageStat.Stat(c), ImageStat.Stat(c.filter(ImageFilter.FIND_EDGES).crop((1, 1, c.width - 1, c.height - 1)))
    return {"luma": st.mean[0], "contrast": st.stddev[0], "edge": edge.mean[0]}


# ---------------------------------------------------------------------------------------------------------------
# Variants -> runtime pnach + per-game INI
# ---------------------------------------------------------------------------------------------------------------

def load_patch_spec(path: Path = PATCHES_PATH) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def variant_writes(name: str, spec: Optional[Dict[str, Any]] = None) -> List[Tuple[int, int]]:
    """Ordered (vaddr, word): restores first (stock words), then applies (patched words) override them."""
    v, spec = VARIANTS[name], spec or load_patch_spec()
    by_id = {p["id"]: p for p in spec["patches"]}
    words: Dict[int, int] = {}
    for key, field in (("restore", "expected_orig"), ("apply", "patched")):
        for pid in v.get(key, []):
            for w in by_id[pid]["writes"]:
                words[int(w["vaddr"], 16)] = int(w[field], 16)
    return sorted(words.items())


def variant_pnach_section(name: str, writes: Sequence[Tuple[int, int]]) -> Tuple[Optional[str], str]:
    if not writes:
        return None, ""
    sec = f"Race Bench Variant {name} (Optional)"
    lines = ["", f"[{sec}]", "author=NFSMW: Rockport 2026 (tools/benchmark_4car_race.py)",
             f"description={VARIANTS[name]['desc']} (bytes from patches/elf_patches.json)"]
    lines += [f"patch=0,EE,{a:08X},word,{w:08X}" for a, w in writes]
    return sec, "\n".join(lines) + "\n"


def baked_default_ids(spec: Optional[Dict[str, Any]] = None) -> List[str]:
    return [p["id"] for p in (spec or load_patch_spec())["patches"] if p["enabled_by_default"]]


def build_variant_profile(name: str, *, scale: int = 6, ee_cycle_rate: int = 3, section: Optional[str] = None) -> str:
    from race_flywheel import upsert_ini_sections

    patches = list(profiles.V241_CLEAN_PATCHES) + ([section] if section else [])
    ini = profiles.build_v241_clean_profile(scale=scale, ee_cycle_rate=ee_cycle_rate, enabled_patches=patches)
    if VARIANTS[name].get("ini"):
        ini = upsert_ini_sections(ini, VARIANTS[name]["ini"])
    errors = profiles.validate_profile_ini(ini)
    if errors:
        raise ValueError(f"variant {name}: invalid INI: " + "; ".join(errors))
    return ini


def parse_menu_keys(text: str) -> Tuple[Tuple[float, str, int, str], ...]:
    """'2.0:k,5.0:right,...' -> FrontendMenuNavigator schedule (for live calibration without code changes)."""
    steps: List[Tuple[float, str, int, str]] = []
    for i, tok in enumerate(t.strip() for t in text.split(",") if t.strip()):
        at, _, key = tok.partition(":")
        if not key.strip().isalnum() or (steps and float(at) <= steps[-1][0]):
            raise ValueError(f"bad --menu-keys step {tok!r} (need ascending 'sec:key')")
        steps.append((float(at), key.strip(), 250, f"custom_{i}_{key.strip()}"))
    if not steps:
        raise ValueError("--menu-keys is empty")
    return tuple(steps)


# ---------------------------------------------------------------------------------------------------------------
# Live PINE capture (shared socket, 250 Hz frame sampler + 20 Hz autopilot telemetry)
# ---------------------------------------------------------------------------------------------------------------

class LockedPineClient(pine.PineClient):
    """One PINE socket shared by the autopilot and the frame sampler (every request is one locked round-trip)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def _send_and_recv(self, payload_body: bytes) -> bytes:
        with self._lock:
            return super()._send_and_recv(payload_body)


class FrameSampler:
    """Polls (flow, WorldLoopCounter, eDisplayFrame) in one batch every `interval`; keeps change points only."""

    def __init__(self, client: Any, *, interval: float = 0.004, clock: Callable[[], float] = time.time) -> None:
        self.client, self.interval, self.clock = client, interval, clock
        self.samples: List[Dict[str, Any]] = []
        self.errors = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="frame-sampler", daemon=True)

    def poll_once(self) -> None:
        R = pine.PineOpcode.MSG_READ32
        raw = self.client.read_batch_raw([(R, pine.ADDR_GAMEFLOW_STATE), (R, pine.ADDR_WORLD_LOOP_COUNTER),
                                          (R, ADDR_DISPLAY_FRAME)])
        flow, wlc, disp = (struct.unpack("<I", b)[0] for b in raw)
        last = self.samples[-1] if self.samples else None
        if last is None or (last["flow"], last["wlc"], last["disp"]) != (flow, wlc, disp):
            self.samples.append({"t": round(self.clock(), 5), "flow": flow, "wlc": wlc, "disp": disp})

    def _run(self) -> None:
        while not self._stop.is_set() and self.errors < 50:
            try:
                self.poll_once()
            except pine.PineError:
                self.errors += 1
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self, out_path: Optional[Path] = None) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if out_path is not None:
            Path(out_path).write_text("".join(json.dumps(s) + "\n" for s in self.samples), encoding="utf-8")


class RaceTelemetryReader(pine.NfsmwTelemetryReader):
    """Adds eDisplayFrame, IVehicle::mList[AIRACERS].Count and GRaceStatus racer count to every record."""

    def read_telemetry(self) -> Any:
        tel = super().read_telemetry()
        R = pine.PineOpcode.MSG_READ32
        disp, ai, rs = (struct.unpack("<I", b)[0] for b in self.pine.read_batch_raw(
            [(R, ADDR_DISPLAY_FRAME), (R, ADDR_AIRACERS_COUNT), (R, pine.ADDR_GRACESTATUS_PTR)]))
        racers = self.pine.read_u32(rs + pine.OFF_GRACESTATUS_RACER_COUNT) if pine.is_valid_ee_ptr(rs) else 0
        extra = {"display_frame_counter": disp, "ai_racer_count": ai if ai <= 16 else 0,
                 "racer_count": racers if racers <= 16 else 0}
        base = tel.to_dict
        tel.to_dict = lambda: {**base(), **extra}  # type: ignore[method-assign]
        return tel


def pcsx2_already_running() -> bool:
    """Never run two PCSX2 instances (a live lab worker may own one). Unknown == running (refuse)."""
    try:
        res = subprocess.run(["pgrep", "-x", "PCSX2"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    return res.returncode == 0 and bool(res.stdout.strip())


def _drive_race(pine_slot: int, pid: int, key_driver_bin: Path, run_dir: Path, *, duration: float,
                wait_racing_timeout: float, menu_mode: str, sample_interval: float) -> Dict[str, Any]:
    client = LockedPineClient(slot=pine_slot, timeout=1.0)
    client.connect()
    sampler = FrameSampler(client, interval=sample_interval)
    sink = pine.KeyDriverSession(key_driver_bin, pid=pid, tap_mode="auto")
    sampler.start()
    sink.start()
    try:
        return pine.run_closed_loop_autopilot(
            RaceTelemetryReader(client), sink, duration_sec=duration, config=pine.AutopilotConfig(verify_reverse_demo=False),
            telemetry_jsonl_path=run_dir / "pine_telemetry.jsonl", f8_interval_sec=0.0,
            wait_for_racing_timeout=wait_racing_timeout, menu_auto_advance=True, menu_mode=menu_mode)
    finally:
        sink.close()
        sampler.stop(run_dir / "frame_samples.jsonl")
        client.close()


def analyze_run(run_dir: Path, *, warmup_sec: float = 2.0) -> Dict[str, Any]:
    """Offline metrics from pine_telemetry.jsonl (+ frame_samples.jsonl when the 250 Hz sampler ran)."""
    run_dir = Path(run_dir)
    records = live._read_jsonl(run_dir / "pine_telemetry.jsonl")
    samples, source = live._read_jsonl(run_dir / "frame_samples.jsonl"), "frame_sampler_250hz"
    if not samples:
        source = "telemetry_20hz"
        samples = [{"t": r.get("timestamp"), "flow": r.get("flow_state"), "wlc": r.get("world_loop_counter"),
                    "disp": r.get("display_frame_counter")} for r in records]
    t_race = next((float(s["t"]) for s in samples if _racing(s)), None)
    if t_race is not None:
        samples = [s for s in samples if isinstance(s.get("t"), (int, float)) and s["t"] >= t_race + warmup_sec]
    race = race_detection(records)
    host, disp = host_speed_metrics(samples), display_metrics(frame_times_ms(samples))
    return {"sample_source": source, "race": race, "host_speed": host, "display": disp,
            "gate": evaluate_gate(host, disp, race)}


def visual_parity(base_run: Path, cand_run: Path, *, n: int = 5) -> Dict[str, Any]:
    """Match n race frames by PINE world position and compare HUD-free crops against the baseline."""
    ann = []
    for run in (Path(base_run), Path(cand_run)):
        frames = json.loads((run / "video_frames.json").read_text()) if (run / "video_frames.json").is_file() else []
        track = live.load_pine_track(live._read_jsonl(run / "pine_telemetry.jsonl"))
        ann.append(live.annotate_frames([Path(f["path"]) for f in frames], track, frame_times=[f["t"] for f in frames]))
    pairs = []
    for m in live.match_checkpoints_by_position(ann[0], ann[1], n=n):
        cmp = compare_parity(crop_stats(m["left"]), crop_stats(m["right"]))
        pairs.append({"baseline": str(m["left"]), "candidate": str(m["right"]), "method": m["method"],
                      "position_gap_m": m["position_gap_m"], **cmp})
    return {"pairs": pairs, "matched": len(pairs), "pass": len(pairs) >= 3 and all(p["ok"] for p in pairs)}


def run_race_session(iso: Path, variant: str, *, bench_dir: Path, preflight: Dict[str, Any], data_dir: Path,
                     scale: int, ee_cycle_rate: int, duration: float, wait_racing_timeout: float, pine_slot: int,
                     menu_mode: str, video_mode: str, sample_interval: float, warmup_sec: float,
                     replacements_dir: Optional[Path] = None) -> Dict[str, Any]:
    import build_modded_iso
    import pcsx2_live_verify as lv
    import race_flywheel as rf
    import video_qa_pipeline as vqa

    root = Path(bench_dir) / variant
    if root.exists():
        shutil.rmtree(root)
    run_dir, staged = root / "run", root / "patches_src"
    for d in (run_dir, staged):
        d.mkdir(parents=True, exist_ok=True)
    serial, crc = preflight["iso"].get("serial") or "SLUS-21351", preflight["iso"]["elf_crc"]
    pnach = staged / f"{serial}_{crc}.pnach"
    build_modded_iso.emit_companion_pnach(crc, pnach, baked_default_ids())
    section, text = variant_pnach_section(variant, variant_writes(variant))
    pnach.write_text(pnach.read_text(encoding="utf-8") + text, encoding="utf-8")
    info = rf.prepare_flywheel_sandbox(root, run_dir, data_dir, preflight, patches_dir=staged, record_video=video_mode != "off",
                                       video_mode=video_mode, enable_pine=True, pine_slot=pine_slot,
                                       replacements_dir=replacements_dir)
    Path(info["target_gamesettings_ini"]).write_text(
        build_variant_profile(variant, scale=scale, ee_cycle_rate=ee_cycle_rate, section=section), encoding="utf-8")
    emu_log, key_bin = run_dir / "pcsx2_emulog.txt", rf.ensure_key_driver_binary()
    recorder = vqa.SessionVideoRecorder(mode=video_mode, iter_dir=run_dir, videos_dir=Path(info["videos_dir"]),
                                        key_driver_bin=key_bin, video_tools_bin=vqa.ensure_video_tools_binary())
    cmd = [str(preflight["pcsx2_app"]["binary_path"]), "-batch", "-fastboot", "-logfile", str(emu_log.resolve()),
           "--", str(Path(iso).resolve())]
    result: Dict[str, Any] = {"variant": variant, "desc": VARIANTS[variant]["desc"], "run_dir": str(run_dir),
                              "pnach_section": section}
    video_t0 = time.time()
    with open(run_dir / "pcsx2_boot.log", "wb") as boot_fp:
        proc = subprocess.Popen(cmd, stdout=boot_fp, stderr=subprocess.STDOUT,
                                env=dict(os.environ, HOME=str(info["sandbox_home"])))
        try:
            rf._wait_for_gs_window_ready(emu_log, proc, timeout_sec=8.0)
            recorder.start(proc.pid)
            video_t0 = time.time()
            rf._wait_for_emulation_ready(emu_log, proc, timeout_sec=30.0)
            rf._wait_for_pine_socket(pine_slot, proc, timeout_sec=15.0)
            recorder.ensure_started_retry(proc.pid)
            result["autopilot_summary"] = _drive_race(pine_slot, proc.pid, key_bin, run_dir, duration=duration,
                                                      wait_racing_timeout=wait_racing_timeout, menu_mode=menu_mode,
                                                      sample_interval=sample_interval)
            recorder.stop_before_shutdown(proc.pid, proc_alive=proc.poll() is None)
        except Exception as exc:  # noqa: BLE001 — always shut PCSX2 down and report
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            result["terminate"] = lv.terminate_process_cleanly(proc)
    try:
        qa = recorder.finalize_and_extract_qa(interval_sec=1.0) if video_mode != "off" else {}
    except Exception as exc:  # noqa: BLE001
        qa = {"error": f"{type(exc).__name__}: {exc}"}
    timeline = (qa.get("qa") or {}).get("timeline") or []
    (run_dir / "video_frames.json").write_text(json.dumps(
        [{"path": e["path"], "t": video_t0 + float(e["timestamp_sec"])} for e in timeline if e.get("is_valid_render")]))
    result["session_video_path"] = qa.get("session_video_path")
    result.update(analyze_run(run_dir, warmup_sec=warmup_sec))
    return result


def run_matrix(iso: Path, variants: Sequence[str], *, out_dir: Path, bench_dir: Path, **kw: Any) -> Dict[str, Any]:
    import pcsx2_live_verify as lv

    data_dir = lv.default_pcsx2_data_dir()
    preflight = lv.run_preflight(Path(iso))
    if not preflight.get("ok"):
        return {"ok": False, "error": "preflight failed", "preflight": preflight}
    before = live._integrity_snapshot(Path(iso), data_dir, preflight)
    sessions: Dict[str, Dict[str, Any]] = {}
    base_slot = int(kw.pop("pine_slot", 28031))
    for idx, name in enumerate(variants):
        if pcsx2_already_running():
            sessions[name] = {"variant": name, "error": "another PCSX2 instance is running; refusing to launch"}
            break
        sessions[name] = run_race_session(Path(iso), name, bench_dir=bench_dir, preflight=preflight, data_dir=data_dir,
                                          pine_slot=base_slot + idx, **kw)
    if BASELINE in sessions:
        for name, res in sessions.items():
            if name != BASELINE and "run_dir" in res:
                try:
                    res["visual_parity"] = visual_parity(Path(sessions[BASELINE]["run_dir"]), Path(res["run_dir"]))
                except (OSError, ValueError, ImportError) as exc:
                    res["visual_parity"] = {"pass": False, "error": str(exc)}
    after = live._integrity_snapshot(Path(iso), data_dir, preflight)
    untouched = {k: before[k] == after[k] for k in before}
    report = {"ok": all(untouched.values()) and all("error" not in s for s in sessions.values()),
              "gates_pass": bool(sessions) and all((s.get("gate") or {}).get("pass") for s in sessions.values()),
              "untouched": untouched, "elf_crc": preflight["iso"].get("elf_crc"), "gate": RACE_GATE,
              "sessions": sessions, "config": {**{k: str(v) for k, v in kw.items()}, "pine_slot": base_slot}}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "race_benchmark_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def summary_line(name: str, res: Dict[str, Any]) -> str:
    h, d, r = res.get("host_speed") or {}, res.get("display") or {}, res.get("race") or {}
    g = (res.get("gate") or {})
    vp = (res.get("visual_parity") or {}).get("pass")
    return (f"{name:20s} speed avg {h.get('avg_pct')}% p10 {h.get('p10_pct')} min {h.get('min_pct')} "
            f">=98% {h.get('pct_of_race_ge_98')}% | fps avg {d.get('avg_fps')} p50 {d.get('p50_fps')} "
            f"p10 {d.get('p10_fps')} 1%low {d.get('p1_low_fps')} min {d.get('min_fps')} stutters {d.get('stutters_gt_25ms')} "
            f"| opponents {r.get('opponents')} | gate {'PASS' if g.get('pass') else 'FAIL ' + ','.join(g.get('failures', []))}"
            + (f" | parity {'OK' if vp else 'FAIL'}" if vp is not None else "") + (f" | error {res['error']}" if res.get("error") else ""))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Real 4-car Quick Race benchmark + ablation matrix (SLUS-21351)")
    ap.add_argument("--iso", type=Path, default=live._default_iso_path(), help="v2.4.1 modded ISO (read-only)")
    ap.add_argument("--variants", default=BASELINE, help=f"comma list (see --list-variants); first should be {BASELINE}")
    ap.add_argument("--list-variants", action="store_true")
    ap.add_argument("--analyze-run", type=Path, default=None, help="offline: recompute metrics for an existing run dir")
    ap.add_argument("--duration", type=float, default=90.0, help="seconds of racing per variant")
    ap.add_argument("--wait-racing-timeout", type=float, default=120.0)
    ap.add_argument("--warmup-sec", type=float, default=2.0, help="skip the race fade-in")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--ee-cycle-rate", type=int, default=3)
    ap.add_argument("--pine-slot", type=int, default=28031)
    ap.add_argument("--menu-mode", default="quickrace", choices=sorted(pine._MENU_SCHEDULES))
    ap.add_argument("--menu-keys", default="", help="override menu route, e.g. '2:k,5:right,5.8:right,6.6:k,...'")
    ap.add_argument("--video-mode", default="screencapture", choices=["screencapture", "pcsx2", "auto", "off"],
                    help="screencapture records outside the emulator (no in-process encode skewing speed)")
    ap.add_argument("--sample-ms", type=float, default=4.0, help="eDisplayFrame poll period")
    ap.add_argument("--replacements-dir", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "build" / "benchmarks" / "race4")
    ap.add_argument("--bench-dir", type=Path, default=REPO_ROOT / "build" / "pcsx2_race_bench")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.list_variants:
        for name, v in VARIANTS.items():
            print(f"{name:20s} {v['desc']}")
        return 0
    if args.analyze_run is not None:
        res = analyze_run(args.analyze_run, warmup_sec=args.warmup_sec)
        print(json.dumps(res, indent=2) if args.json else summary_line(args.analyze_run.parent.name, res))
        return 0 if res["gate"]["pass"] else 3
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown or not args.iso or not Path(args.iso).is_file():
        print(f"error: unknown variants {unknown}" if unknown else "error: --iso (or NFSMW_ISO) must exist", file=sys.stderr)
        return 2
    menu_mode = args.menu_mode
    if args.menu_keys:
        pine._MENU_SCHEDULES["quickrace_custom"] = parse_menu_keys(args.menu_keys)
        menu_mode = "quickrace_custom"
    if pcsx2_already_running():
        print("error: PCSX2 is already running — refusing to start a second instance", file=sys.stderr)
        return 1
    report = run_matrix(args.iso, variants, out_dir=args.out_dir, bench_dir=args.bench_dir, scale=args.scale,
                        ee_cycle_rate=args.ee_cycle_rate, duration=args.duration,
                        wait_racing_timeout=args.wait_racing_timeout, pine_slot=args.pine_slot, menu_mode=menu_mode,
                        video_mode=args.video_mode, sample_interval=args.sample_ms / 1000.0,
                        warmup_sec=args.warmup_sec, replacements_dir=args.replacements_dir)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for name, res in (report.get("sessions") or {}).items():
            print(summary_line(name, res))
        print(f"untouched={report.get('untouched')} ok={report.get('ok')} gates_pass={report.get('gates_pass')}")
    if not report.get("ok"):
        return 1
    return 0 if report.get("gates_pass") else 3


if __name__ == "__main__":
    raise SystemExit(main())
