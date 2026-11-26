#!/usr/bin/env python3
"""Automated 6-district full-island Rockport benchmark & road-graph spawn audit.

Verifies:
  1. Offline contract checks (DriveBench stdout parser, canonical 6-landmark specs,
     and RO_* asphalt material classification preventing Bayshore Blvd concrete/sky seams).
  2. Live `rockport_full.rkpt` road-graph audit across all 6 landmarks: single connected
     WRoadNetwork component, asphalt spawn proximity (<= 5m), elevation match (|dy| <= 0.65m),
     >= 10 connected road segments within 150m, and >= 1 forward-aligned segment within 45m.
  3. Live Apple Silicon Metal 4 6-district Frame-Gen benchmark (`RockportMetalRT
     --world build/rockport_world/rockport_full.rkpt --landmark <L> --drive-bench 30
     --frame-gen on`): asserts VRAM <= 600 MB, activeInsts <= 8,192, zero crashes,
     and Frame-Gen FPS >= 95 FPS across Rosewood Blvd, Downtown East Park, Camden Beach,
     Highway 99, Rosewood College, and Rockport Waterfront.

Skips live GPU/asset checks gracefully when `rockport_full.rkpt` or `RockportMetalRT`
is absent (or when `NFSMW_ISO` points to a nonexistent path for offline CI).
"""

from __future__ import annotations

import collections
import dataclasses
import math
import os
import platform
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_rockport_world as erw  # noqa: E402

ROCKPORT_FULL_RKPT = REPO_ROOT / "build" / "rockport_world" / "rockport_full.rkpt"
DEFAULT_METAL_BIN_CANDIDATES: Tuple[Path, ...] = (
    REPO_ROOT / "build" / "metal_rt_v06" / "RockportMetalRT",
    REPO_ROOT / "build" / "metal_rt" / "RockportMetalRT",
)

EXPECTED_LANDMARKS: Tuple[str, ...] = (
    "Rosewood Blvd",
    "Downtown East Park",
    "Camden Beach",
    "Highway 99",
    "Rosewood College",
    "Rockport Waterfront",
)

RO_GENFILL_CRK_HASH = 0xD5A08D5F
RO_DETAILSPACER_HASH = 0xD7ECA6B0

AVG_FPS_RE = re.compile(r"avg=(?P<avg_ms>[0-9.]+)\s*ms/frame\s*\((?P<fps>[0-9.]+)\s*FPS\)")


@dataclasses.dataclass(frozen=True)
class DriveBenchMetrics:
    gpu: str
    msl: str
    car: str
    world: str
    landmark: str
    blas: int
    active_insts: int
    vram_mb: float
    tris: int
    denoise: str
    frame_gen: str
    frames: int
    avg_ms: float
    fps: float
    ai_traffic: int = 0
    ai_cops: int = 0


def parse_drive_bench_output(stdout: str) -> DriveBenchMetrics:
    """Parse the canonical `Metal RT DriveBench ...` summary line from RockportMetalRT stdout.

    Uses resilient key=value field parsing across `|` delimiters so optional telemetry fields
    (e.g. `aiTraffic`, `aiCops`, `heat`, `speed`) never break parsing across releases.
    """
    for line in stdout.splitlines():
        idx = line.find("Metal RT DriveBench ")
        if idx < 0:
            continue
        payload = line[idx + len("Metal RT DriveBench ") :]
        avg_m = AVG_FPS_RE.search(payload)
        if not avg_m:
            continue
        fields: Dict[str, str] = {}
        for part in payload.split("|"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k.strip()] = v.strip()
        required = ("GPU", "msl", "car", "world", "landmark", "blas", "activeInsts", "vram", "tris", "denoise", "frameGen", "frames")
        if not all(k in fields for k in required):
            continue
        vram_str = fields["vram"].upper().removesuffix("MB").strip()
        return DriveBenchMetrics(
            gpu=fields["GPU"],
            msl=fields["msl"],
            car=fields["car"],
            world=fields["world"],
            landmark=fields["landmark"],
            blas=int(fields["blas"]),
            active_insts=int(fields["activeInsts"]),
            vram_mb=float(vram_str),
            tris=int(fields["tris"]),
            denoise=fields["denoise"],
            frame_gen=fields["frameGen"].lower(),
            frames=int(fields["frames"]),
            avg_ms=float(avg_m.group("avg_ms")),
            fps=float(avg_m.group("fps")),
            ai_traffic=int(fields.get("aiTraffic", "0")),
            ai_cops=int(fields.get("aiCops", "0")),
        )
    raise ValueError(f"Could not find Metal RT DriveBench metrics line in output:\n{stdout}")


def _is_offline_ci_mode() -> bool:
    iso_env = os.environ.get("NFSMW_ISO")
    if iso_env is None:
        return False
    return iso_env.strip() == "" or not Path(iso_env).expanduser().is_file()


def find_metal_rt_binary() -> Optional[Path]:
    env_bin = os.environ.get("ROCKPORT_METAL_RT_BIN")
    if env_bin:
        p = Path(env_bin).expanduser()
        return p if p.is_file() and os.access(p, os.X_OK) else None
    for cand in DEFAULT_METAL_BIN_CANDIDATES:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def audit_rkpt_landmarks_and_road_graph(rkpt_path: Path) -> Dict[str, object]:
    """Decode v2 WRoadNetwork + landmarks + RO_* materials from an exported `.rkpt` v2 file."""
    data = rkpt_path.read_bytes()
    hdr = struct.unpack_from(erw.HEADER_FMT, data, 0)
    magic, version, flags = hdr[0], hdr[1], hdr[2]
    if magic != erw.RKPT_MAGIC or version < 2 or (flags & erw.RKPT_HDR_FLAG_HAS_V2_EXT) == 0:
        raise ValueError(f"{rkpt_path} is not a .rkpt v2 package with extension tables")

    mat_cnt = hdr[6]
    mat_off = hdr[23]
    ext = struct.unpack_from(erw.V2_EXT_HEADER_FMT, data, erw.HEADER_SIZE)
    rn_cnt, rs_cnt, wl_cnt, lm_cnt = ext[0], ext[1], ext[2], ext[3]
    rn_off, rs_off, _wl_off, lm_off = ext[8], ext[9], ext[10], ext[11]

    mat_sc_by_hash: Dict[int, int] = {}
    for mi in range(mat_cnt):
        _dt, sc, _mf, _rh, _mh, _eh, _bs, th = struct.unpack_from(erw.MATERIAL_FMT, data, mat_off + mi * erw.MATERIAL_SIZE)
        mat_sc_by_hash[th] = sc

    nodes: List[Tuple[float, float, float]] = []
    adj: List[List[int]] = [[] for _ in range(rn_cnt)]
    for i in range(rn_cnt):
        px, py, pz = struct.unpack_from("<3f", data, rn_off + i * erw.ROAD_NODE_SIZE)
        nodes.append((px, py, pz))

    segs: List[Tuple[int, int, int, float]] = []
    for i in range(rs_cnt):
        n0, n1, lfl, _dfl, _rid, _slen, hw = struct.unpack_from(erw.ROAD_SEGMENT_FMT, data, rs_off + i * erw.ROAD_SEGMENT_SIZE)
        segs.append((n0, n1, lfl, hw))
        if n0 < rn_cnt and n1 < rn_cnt:
            adj[n0].append(n1)
            adj[n1].append(n0)

    comp_id = [-1] * rn_cnt
    comp_sizes: List[int] = []
    for i in range(rn_cnt):
        if comp_id[i] != -1:
            continue
        cid = len(comp_sizes)
        q: collections.deque[int] = collections.deque([i])
        comp_id[i] = cid
        cnt = 0
        while q:
            u = q.popleft()
            cnt += 1
            for v in adj[u]:
                if comp_id[v] == -1:
                    comp_id[v] = cid
                    q.append(v)
        comp_sizes.append(cnt)

    landmark_reports: List[Dict[str, object]] = []
    for i in range(lm_cnt):
        nm_b, sx, sy, sz, yaw, reg, lfl, _r0, _r1 = struct.unpack_from(
            erw.LANDMARK_FMT, data, lm_off + i * erw.LANDMARK_SIZE
        )
        nm = nm_b.split(b"\x00", 1)[0].decode("ascii", "ignore")
        fx, fz = math.sin(yaw), math.cos(yaw)

        best_node = -1
        best_node_d = 1e9
        for ni, (nx, _ny, nz) in enumerate(nodes):
            d = math.hypot(nx - sx, nz - sz)
            if d < best_node_d:
                best_node_d = d
                best_node = ni

        segs_150 = 0
        fwd_45 = 0
        fwd_150 = 0
        best_seg_d = 1e9
        best_seg_dy = 0.0
        for n0, n1, _lfl, _hw in segs:
            x0, y0, z0 = nodes[n0]
            x1, y1, z1 = nodes[n1]
            dx, dz = x1 - x0, z1 - z0
            l2 = dx * dx + dz * dz
            if l2 < 1e-4:
                continue
            t = max(0.0, min(1.0, ((sx - x0) * dx + (sz - z0) * dz) / l2))
            cx, cy, cz = x0 + t * dx, y0 + t * (y1 - y0), z0 + t * dz
            sd = math.hypot(cx - sx, cz - sz)
            ln = math.sqrt(l2)
            dot = abs((dx / ln) * fx + (dz / ln) * fz)
            if sd < best_seg_d:
                best_seg_d = sd
                best_seg_dy = sy - cy
            if sd <= 150.0:
                segs_150 += 1
                if dot >= 0.70:
                    fwd_150 += 1
                    if sd <= 45.0:
                        fwd_45 += 1

        landmark_reports.append(
            {
                "index": i,
                "name": nm,
                "spawn_pos": (round(sx, 2), round(sy, 2), round(sz, 2)),
                "yaw_deg": round(math.degrees(yaw), 1),
                "region_id": reg,
                "nearest_seg_dist_m": round(best_seg_d, 2),
                "elevation_delta_m": round(best_seg_dy, 2),
                "segs_within_150m": segs_150,
                "fwd_aligned_within_45m": fwd_45,
                "fwd_aligned_within_150m": fwd_150,
                "component_nodes": comp_sizes[comp_id[best_node]] if best_node >= 0 else 0,
            }
        )

    return {
        "road_node_count": rn_cnt,
        "road_segment_count": rs_cnt,
        "world_light_count": wl_cnt,
        "component_count": len(comp_sizes),
        "largest_component": max(comp_sizes) if comp_sizes else 0,
        "ro_genfill_sc": mat_sc_by_hash.get(RO_GENFILL_CRK_HASH),
        "ro_detailspacer_sc": mat_sc_by_hash.get(RO_DETAILSPACER_HASH),
        "landmarks": landmark_reports,
    }


def run_six_district_bench(
    binary: Path,
    rkpt_path: Path,
    frames: int = 30,
    landmarks: Sequence[str] = EXPECTED_LANDMARKS,
) -> List[DriveBenchMetrics]:
    """Run headless MetalFX Frame-Gen DriveBench across every Rockport district landmark."""
    results: List[DriveBenchMetrics] = []
    with tempfile.TemporaryDirectory(prefix="rkpt_6dist_bench_") as td:
        tmp_dir = Path(td)
        for idx, lm in enumerate(landmarks):
            out_png = tmp_dir / f"bench_{idx}.png"
            cmd = [
                str(binary),
                "--world", str(rkpt_path),
                "--landmark", lm,
                "--drive-bench", str(frames),
                "--frame-gen", "on",
                "--out", str(out_png),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"RockportMetalRT failed on landmark {lm!r} (rc={proc.returncode}):\n"
                    f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                )
            results.append(parse_drive_bench_output(proc.stdout))
    return results


class TestMultiDistrictBenchmark(unittest.TestCase):
    def test_drive_bench_stdout_parser_contract(self) -> None:
        sample = (
            "Loaded world rockport_full.rkpt in 0.14s\n"
            "Metal RT DriveBench GPU=Apple M5 Pro | msl=4.0 | supportsRaytracing=true | "
            "car=m3_gtr | world=rockport_full.rkpt | landmark=Camden Beach | blas=10256 | "
            "activeInsts=6768 | aiTraffic=16 | aiCops=3 | heat=3 | speed=148.2kmh | "
            "vram=301.5MB | tris=5264120 | denoise=MetalFX-TDS | "
            "frameGen=on | frames=30 | avg=7.91 ms/frame (126.4 FPS) -> /tmp/out.png\n"
        )
        m = parse_drive_bench_output(sample)
        self.assertEqual(m.landmark, "Camden Beach")
        self.assertEqual(m.active_insts, 6768)
        self.assertEqual(m.ai_traffic, 16)
        self.assertEqual(m.ai_cops, 3)
        self.assertAlmostEqual(m.vram_mb, 301.5, places=1)
        self.assertEqual(m.frame_gen, "on")
        self.assertAlmostEqual(m.fps, 126.4, places=1)
        with self.assertRaises(ValueError):
            parse_drive_bench_output("unrelated log output")

    def test_canonical_six_landmarks_and_ro_road_classification(self) -> None:
        spec_names = tuple(spec[0] for spec in erw.ROCKPORT_LANDMARK_SPECS)
        self.assertEqual(spec_names, EXPECTED_LANDMARKS)

        for tex_name, thash in (
            ("RO_GENFILL_CRK", RO_GENFILL_CRK_HASH),
            ("RO_DETAILSPACER", RO_DETAILSPACER_HASH),
        ):
            mat = erw.classify_world_material(
                tex_name=tex_name,
                solid_name="TRN_CP_IT_ROAD_A_09_CHOP_P23_R0",
                tex_has_cutout_alpha=False,
                diffuse_tex_index=0,
                tpk_name_hash=thash,
            )
            self.assertEqual(
                mat.surface_class,
                erw.SURFACE_ROAD_ASPHALT,
                f"{tex_name} must classify as SURFACE_ROAD_ASPHALT (1), got {mat.surface_class}",
            )
            self.assertTrue(mat.flags & erw.MAT_FLAG_DRIVABLE_ROAD)

    def test_live_all_six_landmarks_road_graph_connectivity(self) -> None:
        if _is_offline_ci_mode() or not ROCKPORT_FULL_RKPT.is_file():
            self.skipTest("build/rockport_world/rockport_full.rkpt not present (or offline CI mode)")

        audit = audit_rkpt_landmarks_and_road_graph(ROCKPORT_FULL_RKPT)
        self.assertEqual(audit["component_count"], 1, "Rockport WRoadNetwork must be a single connected component")
        self.assertEqual(audit["largest_component"], audit["road_node_count"])
        self.assertEqual(
            audit["ro_genfill_sc"],
            erw.SURFACE_ROAD_ASPHALT,
            "Bayshore Blvd RO_GENFILL_CRK must be SURFACE_ROAD_ASPHALT (1) in rockport_full.rkpt",
        )
        self.assertEqual(
            audit["ro_detailspacer_sc"],
            erw.SURFACE_ROAD_ASPHALT,
            "RO_DETAILSPACER must be SURFACE_ROAD_ASPHALT (1) in rockport_full.rkpt",
        )

        lm_list = audit["landmarks"]
        self.assertEqual(len(lm_list), 6)
        for lm in lm_list:
            nm = lm["name"]
            self.assertLessEqual(lm["nearest_seg_dist_m"], 5.0, f"{nm} too far from road graph: {lm}")
            self.assertLessEqual(abs(lm["elevation_delta_m"]), 0.65, f"{nm} road elevation mismatch: {lm}")
            self.assertGreaterEqual(lm["segs_within_150m"], 10, f"{nm} needs >=10 connected segments within 150m: {lm}")
            self.assertGreaterEqual(lm["fwd_aligned_within_45m"], 1, f"{nm} needs >=1 forward-aligned segment within 45m: {lm}")
            self.assertEqual(lm["component_nodes"], audit["road_node_count"], f"{nm} not on main road component")

    def test_live_six_district_framegen_benchmark(self) -> None:
        if platform.system() != "Darwin" or _is_offline_ci_mode():
            self.skipTest("macOS Apple Silicon live GPU environment required")
        if not ROCKPORT_FULL_RKPT.is_file():
            self.skipTest("build/rockport_world/rockport_full.rkpt not present")
        binary = find_metal_rt_binary()
        if binary is None:
            self.skipTest("RockportMetalRT binary not built in build/metal_rt_v06/")

        frames = int(os.environ.get("ROCKPORT_BENCH_FRAMES", "30"))
        results = run_six_district_bench(binary=binary, rkpt_path=ROCKPORT_FULL_RKPT, frames=frames)
        self.assertEqual(len(results), 6)
        for expected_lm, res in zip(EXPECTED_LANDMARKS, results):
            self.assertEqual(res.landmark, expected_lm)
            self.assertLessEqual(res.vram_mb, 600.0, f"{expected_lm} VRAM exceeded 600 MB: {res}")
            self.assertGreaterEqual(res.active_insts, 100, f"{expected_lm} too few streamed instances: {res}")
            self.assertLessEqual(res.active_insts, 8192, f"{expected_lm} activeInsts exceeded 8192: {res}")
            self.assertEqual(res.frame_gen, "on", f"{expected_lm} Frame-Gen was not active: {res}")
            self.assertGreaterEqual(res.fps, 95.0, f"{expected_lm} Frame-Gen FPS < 95: {res}")


def _cli_main() -> int:
    if "--bench" in sys.argv:
        binary = find_metal_rt_binary()
        if binary is None or not ROCKPORT_FULL_RKPT.is_file():
            print("Missing RockportMetalRT binary or build/rockport_world/rockport_full.rkpt", file=sys.stderr)
            return 2
        frames = int(os.environ.get("ROCKPORT_BENCH_FRAMES", "30"))
        rows = run_six_district_bench(binary=binary, rkpt_path=ROCKPORT_FULL_RKPT, frames=frames)
        print("| Landmark | Active Insts | VRAM (MB) | Frame-Gen | Avg (ms) | FPS |")
        print("|---|---:|---:|:---:|---:|---:|")
        for r in rows:
            print(f"| {r.landmark} | {r.active_insts} | {r.vram_mb:.1f} | {r.frame_gen} | {r.avg_ms:.2f} | {r.fps:.1f} |")
        return 0
    unittest.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
