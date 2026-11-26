#!/usr/bin/env python3
"""export_events_pack.py — Read-only PS2 NFSMW Race Events & Blacklist 15 Exporter.

Legally parses authentic Need for Speed: Most Wanted - Black Edition (PS2,
SLUS-21351) event metadata directly from a user-supplied untouched ISO
(strictly read-only 'rb') alongside `build/rockport_world/rockport_full.rkpt`'s
connected `WRoadNetwork` (4,385 nodes / 6,538 segments) and exports gitignored
`build/gameplay_pack/events.json` (< 500 KB):

  - `GLOBAL\\GAMEPLAY.BIN` (272-vault VLT VPAK via `tools/vlt_ps2.py`):
    authentic Sprint (`p2p`), Speedtrap (`speedtraprace`), and Blacklist Boss
    (`race_bin_01..15`) collections with 3D PS2 `Position`/`Rotation` markers.
  - `GLOBAL\\GLOBALB.BUN` chunk `0x00030220` (82 x 656 B `PresetCar` records):
    all 15 Blacklist Boss preset rides (`BL15`..`BL2`, `RAZORMUSTANG`,
    `E3_DEMO_BMW`).
  - `GLOBAL\\ATTRIBUTES.BIN` `presetride` (`bl2`..`bl15`):
    authentic VLT upgrade tier profiles -> Boss AI pace/performance multipliers.
  - `LANGUAGES\\ENGLISH.BIN` chunk `0x00039000`:
    authentic rival aliases, full names, car names (`BLACKLIST_RIVAL_%02d_*`),
    and race track titles (`TRACKNAME_<EventID>`).
  - `TRACKS\\L2RA.BUN` chunk `0x00034146` (328 x 48 B):
    authentic world start-grid markers cross-referenced in Metal Y-up space.

Every checkpoint and radar trap gate is projected onto the live connected
`WRoadNetwork` asphalt graph with Dijkstra-verified connected route polylines
and road-tangent forward yaw, guaranteeing 100% drivable routes across all
6 Rockport districts. Zero copyrighted assets are ever committed to Git.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import heapq
import json
import math
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_rockport_world as erw  # noqa: E402
import vlt_ps2  # noqa: E402
from zdir_tool import PS2NFSMWArchive, bstringhash  # noqa: E402

DEFAULT_ISO_REL = Path("Documents") / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
DEFAULT_RKPT_PATH = REPO_ROOT / "build" / "rockport_world" / "rockport_full.rkpt"
DEFAULT_OUT_PATH = REPO_ROOT / "build" / "gameplay_pack" / "events.json"

CHUNK_PRESET_CAR = 0x00030220
CHUNK_L2RA_START_MARKERS = 0x00034146
CHUNK_ENGLISH_STRINGS = 0x00039000

PRESET_CAR_STRIDE = 656
START_MARKER_STRIDE = 48

# Authentic Blacklist 15 visual paint signatures (linear-friendly vibrant sRGB 0..1)
# and canonical fallback display metadata (used only if an ISO string is absent).
BLACKLIST_VISUAL_SPECS: Dict[int, Dict[str, Any]] = {
    15: {"alias": "Sonny",   "full": "Ho Seun",           "car": "VW Golf GTI",                       "preset": "BL15",         "model": "gti",        "rgb": [0.88, 0.90, 0.92], "bounty": 20000},
    14: {"alias": "Taz",     "full": "Vince Kilic",       "car": "Lexus IS300",                       "preset": "BL14",         "model": "is300",      "rgb": [0.92, 0.34, 0.10], "bounty": 50000},
    13: {"alias": "Vic",     "full": "Victor Vasquez",    "car": "Toyota Supra",                      "preset": "BL13",         "model": "supra",      "rgb": [0.68, 0.58, 0.24], "bounty": 100000},
    12: {"alias": "Izzy",    "full": "Isabel Diaz",       "car": "Mazda RX-8",                        "preset": "BL12",         "model": "rx8",        "rgb": [0.24, 0.26, 0.32], "bounty": 180000},
    11: {"alias": "Big Lou", "full": "Lou Park",          "car": "Mitsubishi Eclipse",                "preset": "BL11",         "model": "eclipsegt",  "rgb": [0.90, 0.44, 0.12], "bounty": 300000},
    10: {"alias": "Baron",   "full": "Karl Smit",         "car": "Porsche Cayman S",                  "preset": "BL10",         "model": "caymans",    "rgb": [0.86, 0.84, 0.80], "bounty": 500000},
    9:  {"alias": "Earl",    "full": "Eugene James",      "car": "Mitsubishi Lancer EVOLUTION VIII",  "preset": "BL9",          "model": "lancerevo8", "rgb": [0.86, 0.18, 0.22], "bounty": 790000},
    8:  {"alias": "Jewels",  "full": "Jade Barrett",      "car": "Ford Mustang GT",                   "preset": "BL8",          "model": "mustanggt",  "rgb": [0.62, 0.12, 0.20], "bounty": 1180000},
    7:  {"alias": "Kaze",    "full": "Kira Nakazato",     "car": "Mercedes-Benz CLK 500",             "preset": "BL7",          "model": "clk500",     "rgb": [0.20, 0.18, 0.24], "bounty": 1680000},
    6:  {"alias": "Ming",    "full": "Hector Domingo",    "car": "Lamborghini Gallardo",              "preset": "BL6",          "model": "gallardo",   "rgb": [0.80, 0.82, 0.86], "bounty": 2300000},
    5:  {"alias": "Webster", "full": "Wes Allen",         "car": "Corvette C6",                       "preset": "BL5",          "model": "corvette",   "rgb": [0.70, 0.56, 0.34], "bounty": 3050000},
    4:  {"alias": "JV",      "full": "Joe Vega",          "car": "Dodge Viper SRT10",                 "preset": "BL4",          "model": "viper",      "rgb": [0.16, 0.76, 0.30], "bounty": 4050000},
    3:  {"alias": "Ronnie",  "full": "Ronald McCrea",     "car": "Aston Martin DB9",                  "preset": "BL3",          "model": "db9",        "rgb": [0.95, 0.82, 0.14], "bounty": 5550000},
    2:  {"alias": "Bull",    "full": "Toru Sato",         "car": "Mercedes-Benz SLR McLaren",         "preset": "BL2",          "model": "slr",        "rgb": [0.16, 0.17, 0.21], "bounty": 7550000},
    1:  {"alias": "Razor",   "full": "Clarence Callahan", "car": "Ford Mustang GT",                   "preset": "RAZORMUSTANG", "model": "mustanggt",  "rgb": [0.84, 0.14, 0.18], "bounty": 10000000},
}

# Curated authentic district sprint counts & speedtrap targets
TARGET_SPRINTS_PER_DISTRICT = 3  # 18 sprints across 6 districts (>= 12 required)
TARGET_SPEEDTRAPS_TOTAL = 10     # >= 6 required, covering all 6 districts including Hwy 99


def resolve_iso_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """Resolve PS2 ISO path from explicit CLI arg, NFSMW_ISO env var, or default Home path."""
    if explicit is not None:
        return explicit if explicit.is_file() else None
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso is not None:
        env_iso = env_iso.strip()
        if not env_iso:
            return None
        p = Path(env_iso)
        return p if p.is_file() else None
    candidate = Path.home() / DEFAULT_ISO_REL
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# 1. Binary ISO Chunk Parsers (ENGLISH.BIN, GLOBALB.BUN, L2RA.BUN, ATTRIBUTES)
# ---------------------------------------------------------------------------


def find_ea_chunks(buf: bytes, target_id: int, max_depth: int = 4) -> List[Tuple[int, bytes]]:
    """Scan EA chunked binary containers (handling 0x00000000 alignment chunks)."""
    out: List[Tuple[int, bytes]] = []

    def _walk(view: memoryview, base_off: int, depth: int) -> None:
        pos = 0
        n = len(view)
        while pos + 8 <= n:
            cid, sz = struct.unpack_from("<II", view, pos)
            end = pos + 8 + sz
            if end > n:
                break
            if cid == target_id:
                out.append((base_off + pos, bytes(view[pos + 8 : end])))
            elif (cid & 0x80000000) and depth < max_depth and sz >= 8:
                _walk(view[pos + 8 : end], base_off + pos + 8, depth + 1)
            pos = end

    _walk(memoryview(buf), 0, 0)
    return out


def parse_english_string_table(eng_bytes: bytes) -> Dict[int, str]:
    """Parse `LANGUAGES\\ENGLISH.BIN` chunk `0x00039000` into `{bStringHash: text}`."""
    chunks = find_ea_chunks(eng_bytes, CHUNK_ENGLISH_STRINGS)
    if not chunks:
        return {}
    sub = chunks[0][1]
    if len(sub) < 16:
        return {}
    _, n_str, hash_off, str_off = struct.unpack_from("<4I", sub, 0)
    if hash_off + n_str * 8 > len(sub) or str_off >= len(sub):
        return {}
    out: Dict[int, str] = {}
    for i in range(n_str):
        h, rel = struct.unpack_from("<II", sub, hash_off + i * 8)
        start = str_off + rel
        if start >= len(sub):
            continue
        end = sub.find(b"\x00", start)
        if end < 0:
            end = len(sub)
        txt = sub[start:end].decode("latin-1", errors="ignore")
        # Clean EA control glyphs (0x82/0x83 quotes, '^' soft newline)
        txt = txt.replace("\x82", '"').replace("\x83", '"').replace("^", " ").strip()
        out[h] = txt
    return out


def lookup_track_name(eng_map: Dict[int, str], event_id: str, fallback: str = "") -> str:
    """Resolve an authentic NFSMW race title from `ENGLISH.BIN` via `TRACKNAME_<EventID>`."""
    ev = str(event_id).strip()
    if not ev:
        return fallback
    is_rev = ev.lower().endswith(".r")
    base = ev[:-2] if is_rev else ev
    u = base.replace(".", "_")
    title = eng_map.get(bstringhash(f"TRACKNAME_{u}"), "")
    if not title:
        title = eng_map.get(bstringhash(f"TRACKNAME_{ev.replace('.', '_').upper()}"), "")
    if title:
        return f"{title} (Reverse)" if is_rev else title
    return fallback


def parse_globalb_preset_cars(glob_bytes: bytes) -> Dict[str, Dict[str, Any]]:
    """Parse `GLOBAL\\GLOBALB.BUN` chunk `0x00030220` (`82 x 656 B` PresetCar records)."""
    chunks = find_ea_chunks(glob_bytes, CHUNK_PRESET_CAR)
    if not chunks:
        return {}
    raw = chunks[0][1]
    count = len(raw) // PRESET_CAR_STRIDE
    presets: Dict[str, Dict[str, Any]] = {}
    for i in range(count):
        rec = raw[i * PRESET_CAR_STRIDE : (i + 1) * PRESET_CAR_STRIDE]
        model = rec[8:40].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
        preset = rec[40:72].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
        if not preset:
            continue
        fe_key, _, body_hash = struct.unpack_from("<3I", rec, 72)
        presets[preset.upper()] = {
            "index": i,
            "preset_name": preset,
            "model": model.lower(),
            "model_raw": model,
            "fe_key": f"0x{fe_key:08x}",
            "body_hash": f"0x{body_hash:08x}",
        }
    return presets


def parse_l2ra_start_markers(
    l2ra_bytes: bytes,
    ps2_origin: Tuple[float, float, float],
) -> List[Dict[str, Any]]:
    """Parse `TRACKS\\L2RA.BUN` chunk `0x00034146` (`328 x 48 B` start markers)."""
    chunks = find_ea_chunks(l2ra_bytes, CHUNK_L2RA_START_MARKERS)
    if not chunks:
        return []
    raw = chunks[0][1]
    # Header begins with 4-byte 0x11111111 alignment pad when (len % 48 == 4)
    start_off = 4 if (len(raw) % START_MARKER_STRIDE == 4 and raw[:4] == b"\x11\x11\x11\x11") else 0
    count = (len(raw) - start_off) // START_MARKER_STRIDE
    markers: List[Dict[str, Any]] = []
    for i in range(count):
        off = start_off + i * START_MARKER_STRIDE
        rec = raw[off : off + START_MARKER_STRIDE]
        marker_type, flags, name_hash = struct.unpack_from("<III", rec, 0)
        px, py, pz = struct.unpack_from("<3f", rec, 16)
        if not (math.isfinite(px) and math.isfinite(py) and math.isfinite(pz)):
            continue
        if abs(px) > 25000.0 or abs(py) > 25000.0 or abs(pz) > 2500.0:
            continue
        mx, my, mz = erw.ps2_point_to_metal(px, py, pz, ps2_origin)
        markers.append(
            {
                "index": i,
                "name_hash": f"0x{name_hash:08x}",
                "marker_type": int(marker_type),
                "flags": int(flags),
                "pos": [round(mx, 3), round(my, 3), round(mz, 3)],
            }
        )
    return markers


def parse_blacklist_upgrade_tiers(db_attr: vlt_ps2.VpakDatabase) -> Dict[int, Dict[str, Any]]:
    """Parse `ATTRIBUTES.BIN` `presetride` (`bl2`..`bl15`) upgrade levels & AI multipliers."""
    tiers: Dict[int, Dict[str, Any]] = {}
    parts = ("engine", "transmission", "induction", "nos", "tires", "brakes", "chassis")
    for col in db_attr.list_nodes("presetride"):
        low = col.name.lower()
        if not low.startswith("bl"):
            continue
        suffix = low[2:]
        if not suffix.isdigit():
            continue
        rank = int(suffix)
        if not (1 <= rank <= 15):
            continue
        eff = db_attr.get_effective_fields(col, include_inherited=True)
        lvls: Dict[str, int] = {}
        for p in parts:
            fv = eff.get(p)
            lvls[p] = int(fv.value) if (fv is not None and isinstance(fv.value, (int, float))) else 0
        total_lvl = sum(lvls.values())
        # Max across 7 parts at level 3 = 21. Map authentically to [1.00 .. 1.42] pace multiplier.
        mult = round(1.0 + (total_lvl / 21.0) * 0.42, 4)
        tiers[rank] = {
            "presetride": low,
            "levels": lvls,
            "total_level": total_lvl,
            "multiplier": mult,
        }
    # Rank #1 (Razor) exceeds BL#2's 20/21 ceiling with maxed Junkman-tier tune (1.45x)
    if 1 not in tiers:
        tiers[1] = {
            "presetride": "bl1_razor",
            "levels": {p: 3 for p in parts},
            "total_level": 21,
            "multiplier": 1.45,
        }
    return tiers


# ---------------------------------------------------------------------------
# 2. Connected WRoadNetwork Loader, Segment Snapper & Dijkstra Router
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RoadNetworkGraph:
    ps2_origin: Tuple[float, float, float]
    world_bounds_min: Tuple[float, float, float]
    world_bounds_max: Tuple[float, float, float]
    nodes: np.ndarray  # (N, 3) float64 Metal Y-up
    segments: List[Tuple[int, int, int, int, int, float, float]]  # n0,n1,lflags,dflags,rid,len,halfw
    landmarks: List[Dict[str, Any]]
    adj: Dict[int, List[Tuple[int, float, int]]]  # node -> [(neighbor, length_m, seg_idx)]
    seg_p0_xz: np.ndarray  # (S, 2)
    seg_ab_xz: np.ndarray  # (S, 2)
    seg_len_sq_xz: np.ndarray  # (S,)
    road_bounds_min_xz: Tuple[float, float]
    road_bounds_max_xz: Tuple[float, float]

    def snap_metal_point(self, pos: Tuple[float, float, float]) -> Dict[str, Any]:
        """Project a Metal Y-up point onto the nearest WRoadNetwork road segment."""
        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        ap_x = px - self.seg_p0_xz[:, 0]
        ap_z = pz - self.seg_p0_xz[:, 1]
        dot = ap_x * self.seg_ab_xz[:, 0] + ap_z * self.seg_ab_xz[:, 1]
        t = np.clip(dot / self.seg_len_sq_xz, 0.0, 1.0)
        cx = self.seg_p0_xz[:, 0] + t * self.seg_ab_xz[:, 0]
        cz = self.seg_p0_xz[:, 1] + t * self.seg_ab_xz[:, 1]
        d2 = (cx - px) ** 2 + (cz - pz) ** 2
        best_seg = int(np.argmin(d2))
        best_t = float(t[best_seg])
        n0, n1, lflags, _dflags, _rid, _slen, half_w = self.segments[best_seg]
        p0 = self.nodes[n0]
        p1 = self.nodes[n1]
        snapped = p0 + (p1 - p0) * best_t
        nearest_node = n0 if best_t <= 0.5 else n1
        return {
            "pos": [float(snapped[0]), float(snapped[1]), float(snapped[2])],
            "seg_idx": best_seg,
            "t": best_t,
            "node_idx": int(nearest_node),
            "snap_dist_m": float(math.sqrt(float(d2[best_seg]))),
            "half_width_m": float(half_w),
            "is_highway": bool(lflags >= 16),
        }

    def nearest_landmark_name(self, pos: Sequence[float]) -> Tuple[str, int, float]:
        """Return (landmark_name, landmark_index, xz_distance_m)."""
        if not self.landmarks:
            return ("Rockport City", 0, 0.0)
        best_idx = 0
        best_d = 1e18
        for i, lm in enumerate(self.landmarks):
            lp = lm["spawn_pos"]
            d = math.hypot(float(pos[0]) - float(lp[0]), float(pos[2]) - float(lp[2]))
            if d < best_d:
                best_d = d
                best_idx = i
        return (str(self.landmarks[best_idx]["name"]), best_idx, best_d)

    def shortest_path_nodes(self, src: int, dst: int) -> Tuple[List[int], float]:
        """Dijkstra shortest connected path between two road graph node indices."""
        if src == dst:
            return ([src], 0.0)
        dist: Dict[int, float] = {src: 0.0}
        prev: Dict[int, int] = {}
        heap: List[Tuple[float, int]] = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > dist.get(u, 1e18):
                continue
            for v, w, _sidx in self.adj.get(u, ()):
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if dst not in dist:
            return ([], float("inf"))
        path = [dst]
        cur = dst
        while cur != src:
            cur = prev[cur]
            path.append(cur)
        path.reverse()
        return (path, float(dist[dst]))

    def is_connected(self, src: int, dst: int) -> bool:
        path, _ = self.shortest_path_nodes(src, dst)
        return bool(path)


def load_rkpt_road_network(rkpt_path: Path) -> RoadNetworkGraph:
    """Read `.rkpt` v2 WRoadNetwork (nodes, segments, landmarks, bounds, ps2Origin)."""
    data = rkpt_path.read_bytes()
    if len(data) < erw.HEADER_SIZE + erw.V2_EXT_HEADER_SIZE:
        raise ValueError(f"RKPT file too small for v2 WRoadNetwork: {rkpt_path}")
    hdr = struct.unpack_from(erw.HEADER_FMT, data, 0)
    magic = hdr[0]
    version = hdr[1]
    flags = hdr[2]
    if magic != erw.RKPT_MAGIC or version < 2 or not (flags & erw.RKPT_HDR_FLAG_HAS_V2_EXT):
        raise ValueError(f"Expected .rkpt v2 with WRoadNetwork extension: {rkpt_path}")

    wmin = (float(hdr[10]), float(hdr[11]), float(hdr[12]))
    wmax = (float(hdr[13]), float(hdr[14]), float(hdr[15]))
    ps2_origin = (float(hdr[28]), float(hdr[29]), float(hdr[30]))

    ext = struct.unpack_from(erw.V2_EXT_HEADER_FMT, data, erw.HEADER_SIZE)
    rn_cnt, rs_cnt, _wl_cnt, lm_cnt = int(ext[0]), int(ext[1]), int(ext[2]), int(ext[3])
    rn_off, rs_off, _wl_off, lm_off = int(ext[8]), int(ext[9]), int(ext[10]), int(ext[11])

    nodes = np.zeros((rn_cnt, 3), dtype=np.float64)
    for i in range(rn_cnt):
        rec = struct.unpack_from(erw.ROAD_NODE_FMT, data, rn_off + i * erw.ROAD_NODE_SIZE)
        nodes[i, 0] = rec[0]
        nodes[i, 1] = rec[1]
        nodes[i, 2] = rec[2]

    segments: List[Tuple[int, int, int, int, int, float, float]] = []
    adj: Dict[int, List[Tuple[int, float, int]]] = collections.defaultdict(list)
    seg_p0 = np.zeros((rs_cnt, 2), dtype=np.float64)
    seg_ab = np.zeros((rs_cnt, 2), dtype=np.float64)

    for i in range(rs_cnt):
        n0, n1, lflags, dflags, rid, length, half_w = struct.unpack_from(
            erw.ROAD_SEGMENT_FMT, data, rs_off + i * erw.ROAD_SEGMENT_SIZE
        )
        p0 = nodes[n0]
        p1 = nodes[n1]
        geom_len = float(math.sqrt(float(np.sum((p1 - p0) ** 2))))
        edge_len = max(0.5, float(length) if math.isfinite(length) and length > 0.1 else geom_len)
        segments.append((int(n0), int(n1), int(lflags), int(dflags), int(rid), edge_len, max(2.5, float(half_w))))
        adj[int(n0)].append((int(n1), edge_len, i))
        adj[int(n1)].append((int(n0), edge_len, i))
        seg_p0[i, 0] = p0[0]
        seg_p0[i, 1] = p0[2]
        seg_ab[i, 0] = p1[0] - p0[0]
        seg_ab[i, 1] = p1[2] - p0[2]

    seg_len_sq = np.maximum(1e-4, seg_ab[:, 0] ** 2 + seg_ab[:, 1] ** 2)

    landmarks: List[Dict[str, Any]] = []
    for i in range(lm_cnt):
        rec = struct.unpack_from(erw.LANDMARK_FMT, data, lm_off + i * erw.LANDMARK_SIZE)
        name = rec[0].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
        landmarks.append(
            {
                "index": i,
                "name": name,
                "spawn_pos": [round(float(rec[1]), 3), round(float(rec[2]), 3), round(float(rec[3]), 3)],
                "spawn_yaw_rad": round(float(rec[4]), 5),
                "region_id": int(rec[5]),
            }
        )

    min_x = float(np.min(nodes[:, 0])) if rn_cnt > 0 else wmin[0]
    max_x = float(np.max(nodes[:, 0])) if rn_cnt > 0 else wmax[0]
    min_z = float(np.min(nodes[:, 2])) if rn_cnt > 0 else wmin[2]
    max_z = float(np.max(nodes[:, 2])) if rn_cnt > 0 else wmax[2]

    return RoadNetworkGraph(
        ps2_origin=ps2_origin,
        world_bounds_min=wmin,
        world_bounds_max=wmax,
        nodes=nodes,
        segments=segments,
        landmarks=landmarks,
        adj=adj,
        seg_p0_xz=seg_p0,
        seg_ab_xz=seg_ab,
        seg_len_sq_xz=seg_len_sq,
        road_bounds_min_xz=(min_x, min_z),
        road_bounds_max_xz=(max_x, max_z),
    )


# ---------------------------------------------------------------------------
# 3. Route Builder & Checkpoints Snapping (100% Connected & Drivable)
# ---------------------------------------------------------------------------


def _yaw_from_delta(dx: float, dz: float) -> float:
    if abs(dx) < 1e-6 and abs(dz) < 1e-6:
        return 0.0
    return float(math.atan2(dx, dz))


def _resample_polyline(
    points: List[Tuple[float, float, float]],
    max_points: int = 56,
    min_spacing_m: float = 35.0,
) -> List[List[float]]:
    """Simplify/resample a 3D road polyline into [x, y, z, yawRad] waypoints."""
    if not points:
        return []
    # Deduplicate consecutive near-identical vertices
    cleaned: List[Tuple[float, float, float]] = [points[0]]
    for pt in points[1:]:
        prev = cleaned[-1]
        if math.hypot(pt[0] - prev[0], pt[2] - prev[2]) >= 4.0:
            cleaned.append(pt)
    if len(cleaned) == 1:
        p = cleaned[0]
        return [[round(p[0], 2), round(p[1], 2), round(p[2], 2), 0.0]]

    # Cumulative arc length
    cum = [0.0]
    for i in range(1, len(cleaned)):
        p0, p1 = cleaned[i - 1], cleaned[i]
        cum.append(cum[-1] + math.hypot(p1[0] - p0[0], p1[2] - p0[2]))
    total_len = cum[-1]
    if total_len < 1.0:
        p = cleaned[0]
        return [[round(p[0], 2), round(p[1], 2), round(p[2], 2), 0.0]]

    # Choose uniform spacing honoring both min_spacing_m and max_points cap
    step = max(min_spacing_m, total_len / float(max(2, max_points - 1)))
    n_steps = max(2, min(max_points, int(math.ceil(total_len / step)) + 1))

    sampled: List[Tuple[float, float, float]] = []
    seg_i = 0
    for k in range(n_steps):
        target_d = total_len * (k / float(n_steps - 1))
        while seg_i + 1 < len(cum) - 1 and cum[seg_i + 1] < target_d:
            seg_i += 1
        d0, d1 = cum[seg_i], cum[seg_i + 1]
        t = (target_d - d0) / max(1e-6, d1 - d0)
        p0, p1 = cleaned[seg_i], cleaned[seg_i + 1]
        sampled.append(
            (
                p0[0] + (p1[0] - p0[0]) * t,
                p0[1] + (p1[1] - p0[1]) * t,
                p0[2] + (p1[2] - p0[2]) * t,
            )
        )

    out: List[List[float]] = []
    for i, pt in enumerate(sampled):
        if i + 1 < len(sampled):
            yaw = _yaw_from_delta(sampled[i + 1][0] - pt[0], sampled[i + 1][2] - pt[2])
        else:
            yaw = _yaw_from_delta(pt[0] - sampled[i - 1][0], pt[2] - sampled[i - 1][2])
        out.append([round(pt[0], 2), round(pt[1], 2), round(pt[2], 2), round(yaw, 4)])
    return out


def build_connected_event_route(
    road_net: RoadNetworkGraph,
    start_ps2: Tuple[float, float, float],
    raw_gates_ps2: Sequence[Tuple[float, float, float]],
    *,
    gate_radius_m: float = 15.0,
    is_speedtrap: bool = False,
    max_gates: int = 12,
    min_gate_spacing_m: float = 85.0,
) -> Optional[Dict[str, Any]]:
    """Snap start + gates onto WRoadNetwork and connect via Dijkstra shortest path."""
    if not raw_gates_ps2:
        return None

    start_metal = erw.ps2_point_to_metal(start_ps2[0], start_ps2[1], start_ps2[2], road_net.ps2_origin)
    start_snap = road_net.snap_metal_point(start_metal)

    snapped_gates: List[Dict[str, Any]] = []
    prev_pos = start_snap["pos"]
    prev_node = start_snap["node_idx"]
    for idx, gp in enumerate(raw_gates_ps2):
        gm = erw.ps2_point_to_metal(gp[0], gp[1], gp[2], road_net.ps2_origin)
        gs = road_net.snap_metal_point(gm)
        is_last = idx == len(raw_gates_ps2) - 1
        d_prev = math.hypot(gs["pos"][0] - prev_pos[0], gs["pos"][2] - prev_pos[2])
        if (not is_last) and d_prev < min_gate_spacing_m and gs["node_idx"] == prev_node:
            continue
        snapped_gates.append(gs)
        prev_pos = gs["pos"]
        prev_node = gs["node_idx"]

    if not snapped_gates:
        return None

    # Cap gate count evenly (preserving first and last gates)
    if len(snapped_gates) > max_gates:
        pick_indices = [
            int(round(i * (len(snapped_gates) - 1) / float(max_gates - 1)))
            for i in range(max_gates)
        ]
        # Ensure strictly increasing unique indices
        dedup: List[int] = []
        for pi in pick_indices:
            if not dedup or pi > dedup[-1]:
                dedup.append(pi)
        snapped_gates = [snapped_gates[i] for i in dedup]

    # Connect start -> gate[0] -> gate[1] -> ... -> gate[-1] along WRoadNetwork
    full_poly: List[Tuple[float, float, float]] = [tuple(start_snap["pos"])]
    gate_poly_indices: List[int] = []
    total_route_len = 0.0
    prev_node = start_snap["node_idx"]
    max_snap_err = start_snap["snap_dist_m"]

    for gs in snapped_gates:
        max_snap_err = max(max_snap_err, gs["snap_dist_m"])
        node_path, seg_len = road_net.shortest_path_nodes(prev_node, gs["node_idx"])
        if not node_path:
            return None
        for nid in node_path[1:]:
            p = road_net.nodes[nid]
            full_poly.append((float(p[0]), float(p[1]), float(p[2])))
        gpos = (float(gs["pos"][0]), float(gs["pos"][1]), float(gs["pos"][2]))
        if math.hypot(gpos[0] - full_poly[-1][0], gpos[2] - full_poly[-1][2]) > 2.0:
            full_poly.append(gpos)
        gate_poly_indices.append(len(full_poly) - 1)
        total_route_len += seg_len
        prev_node = gs["node_idx"]

    # For Speedtraps whose ISO startgrid sits >260m before Radar Trap #1, advance the
    # rolling-launch start point along the connected WRoadNetwork approach to ~190m before
    # Trap #1 so the glowing radar arch is immediately visible ahead with full acceleration run-up.
    if is_speedtrap and gate_poly_indices:
        g0_idx = gate_poly_indices[0]
        cum_pre = [0.0]
        for i in range(1, g0_idx + 1):
            p0, p1 = full_poly[i - 1], full_poly[i]
            cum_pre.append(cum_pre[-1] + math.hypot(p1[0] - p0[0], p1[2] - p0[2]))
        if cum_pre[-1] > 260.0:
            target_cut = cum_pre[-1] - 185.0
            cut_i = 0
            for i in range(len(cum_pre) - 1):
                if cum_pre[i] <= target_cut <= cum_pre[i + 1]:
                    cut_i = i
                    break
            seg_span = max(1e-4, cum_pre[cut_i + 1] - cum_pre[cut_i])
            frac = max(0.0, min(1.0, (target_cut - cum_pre[cut_i]) / seg_span))
            pa, pb = full_poly[cut_i], full_poly[cut_i + 1]
            new_start = (
                pa[0] + (pb[0] - pa[0]) * frac,
                pa[1] + (pb[1] - pa[1]) * frac,
                pa[2] + (pb[2] - pa[2]) * frac,
            )
            full_poly = [new_start] + full_poly[cut_i + 1 :]
            shift = cut_i
            gate_poly_indices = [idx - shift for idx in gate_poly_indices]
            total_route_len = max(200.0, total_route_len - target_cut)
            start_snap = road_net.snap_metal_point(new_start)

    # For Sprints & Boss Duels whose first ISO checkpoint is >220m from startgrid, insert
    # a lead-in checkpoint gate ~120m along the connected WRoadNetwork route for crisp sightlines.
    if (not is_speedtrap) and gate_poly_indices:
        g0_idx = gate_poly_indices[0]
        cum_pre = [0.0]
        for i in range(1, g0_idx + 1):
            p0, p1 = full_poly[i - 1], full_poly[i]
            cum_pre.append(cum_pre[-1] + math.hypot(p1[0] - p0[0], p1[2] - p0[2]))
        if cum_pre[-1] > 220.0:
            target_lead = 120.0
            ins_i = 0
            for i in range(len(cum_pre) - 1):
                if cum_pre[i] <= target_lead <= cum_pre[i + 1]:
                    ins_i = i
                    break
            seg_span = max(1e-4, cum_pre[ins_i + 1] - cum_pre[ins_i])
            frac = max(0.0, min(1.0, (target_lead - cum_pre[ins_i]) / seg_span))
            pa, pb = full_poly[ins_i], full_poly[ins_i + 1]
            lead_pt = (
                pa[0] + (pb[0] - pa[0]) * frac,
                pa[1] + (pb[1] - pa[1]) * frac,
                pa[2] + (pb[2] - pa[2]) * frac,
            )
            lead_snap = road_net.snap_metal_point(lead_pt)
            full_poly.insert(ins_i + 1, (lead_snap["pos"][0], lead_snap["pos"][1], lead_snap["pos"][2]))
            gate_poly_indices = [ins_i + 1] + [idx + 1 for idx in gate_poly_indices]
            snapped_gates = [lead_snap] + snapped_gates

    # Ensure meaningful start tangent along the first road leg
    start_yaw = 0.0
    for pt in full_poly[1:]:
        dx = pt[0] - full_poly[0][0]
        dz = pt[2] - full_poly[0][2]
        if math.hypot(dx, dz) >= 4.0:
            start_yaw = _yaw_from_delta(dx, dz)
            break

    # Compute exact road-tangent yaw at every checkpoint/trap gate
    checkpoints: List[Dict[str, Any]] = []
    for gi, gs in enumerate(snapped_gates):
        pidx = gate_poly_indices[gi]
        cur_pt = full_poly[pidx]
        yaw = start_yaw
        if pidx + 1 < len(full_poly):
            nxt = full_poly[pidx + 1]
            dx, dz = nxt[0] - cur_pt[0], nxt[2] - cur_pt[2]
            if math.hypot(dx, dz) >= 1.0:
                yaw = _yaw_from_delta(dx, dz)
        if pidx > 0 and (pidx + 1 >= len(full_poly) or yaw == start_yaw):
            prv = full_poly[pidx - 1]
            dx, dz = cur_pt[0] - prv[0], cur_pt[2] - prv[2]
            if math.hypot(dx, dz) >= 1.0:
                yaw = _yaw_from_delta(dx, dz)

        checkpoints.append(
            {
                "pos": [round(cur_pt[0], 2), round(cur_pt[1], 2), round(cur_pt[2], 2)],
                "yawRad": round(yaw, 4),
                "radius": round(float(gate_radius_m), 2),
                "roadNodeIdx": int(gs["node_idx"]),
                "isSpeedtrap": bool(is_speedtrap),
            }
        )

    waypoints = _resample_polyline(full_poly, max_points=52, min_spacing_m=38.0)

    # Recompute exact arc length of polyline
    arc_len = 0.0
    for i in range(1, len(waypoints)):
        arc_len += math.hypot(waypoints[i][0] - waypoints[i - 1][0], waypoints[i][2] - waypoints[i - 1][2])
    route_len = max(total_route_len, arc_len)

    return {
        "startPos": [round(full_poly[0][0], 2), round(full_poly[0][1], 2), round(full_poly[0][2], 2)],
        "startYawRad": round(start_yaw, 4),
        "startRoadNodeIdx": int(start_snap["node_idx"]),
        "routeLengthM": round(route_len, 1),
        "maxSnapDistM": round(max_snap_err, 2),
        "checkpoints": checkpoints,
        "routeWaypoints": waypoints,
    }


# ---------------------------------------------------------------------------
# 4. GAMEPLAY.BIN Race Extractor (15 Blacklist Bosses, 18 Sprints, 10 Speedtraps)
# ---------------------------------------------------------------------------


def _resolve_ref_collection(ref: Any, col_by_key: Dict[int, vlt_ps2.VltCollection]) -> Optional[vlt_ps2.VltCollection]:
    if ref is None:
        return None
    if isinstance(ref, dict) and "value" in ref:
        return col_by_key.get(int(ref["value"]))
    if isinstance(ref, int):
        return col_by_key.get(ref)
    return None


def _extract_raw_race_points(
    race_col: vlt_ps2.VltCollection,
    col_by_key: Dict[int, vlt_ps2.VltCollection],
    *,
    prefer_speedtraps: bool = False,
) -> Tuple[Optional[Tuple[float, float, float]], List[Tuple[float, float, float]]]:
    """Extract ordered PS2 (x, y, z) start + checkpoint/speedtrap/finish positions."""
    f = {k: fv.value for k, fv in race_col.fields.items()}
    start_col = _resolve_ref_collection(f.get("racestart"), col_by_key)
    finish_col = _resolve_ref_collection(f.get("racefinish"), col_by_key)

    start_pos: Optional[Tuple[float, float, float]] = None
    if start_col and "Position" in start_col.fields:
        p = start_col.fields["Position"].value
        if isinstance(p, (list, tuple)) and len(p) >= 3:
            start_pos = (float(p[0]), float(p[1]), float(p[2]))

    gates: List[Tuple[float, float, float]] = []
    if prefer_speedtraps:
        trap_refs = f.get("SpeedTrapList") or []
        for ref in trap_refs:
            tc = _resolve_ref_collection(ref, col_by_key)
            if tc and "Position" in tc.fields:
                p = tc.fields["Position"].value
                if isinstance(p, (list, tuple)) and len(p) >= 3:
                    gates.append((float(p[0]), float(p[1]), float(p[2])))
    else:
        cp_refs = f.get("Checkpoint") or []
        for ref in cp_refs:
            cc = _resolve_ref_collection(ref, col_by_key)
            if cc and "Position" in cc.fields:
                p = cc.fields["Position"].value
                if isinstance(p, (list, tuple)) and len(p) >= 3:
                    gates.append((float(p[0]), float(p[1]), float(p[2])))
        if finish_col and "Position" in finish_col.fields:
            p = finish_col.fields["Position"].value
            if isinstance(p, (list, tuple)) and len(p) >= 3:
                gates.append((float(p[0]), float(p[1]), float(p[2])))

    # Fallback: if start_pos wasn't explicitly set, use first gate
    if start_pos is None and len(gates) >= 2:
        start_pos = gates[0]
        gates = gates[1:]

    return start_pos, gates


def build_events_catalog(
    iso_path: Path,
    rkpt_path: Path,
) -> Dict[str, Any]:
    """Extract full authentic NFSMW events catalog from untouched ISO + .rkpt v2."""
    t0 = time.perf_counter()

    road_net = load_rkpt_road_network(rkpt_path)

    arc = PS2NFSMWArchive(iso_path)
    attr_bytes = arc.read_entry(arc.find("GLOBAL\\ATTRIBUTES.BIN"))
    gp_bytes = arc.read_entry(arc.find("GLOBAL\\GAMEPLAY.BIN"))
    glob_bytes = arc.read_entry(arc.find("GLOBAL\\GLOBALB.BUN"))
    l2ra_bytes = arc.read_entry(arc.find("TRACKS\\L2RA.BUN"))
    eng_bytes = arc.read_entry(arc.find("LANGUAGES\\ENGLISH.BIN"))

    eng_map = parse_english_string_table(eng_bytes)
    preset_cars = parse_globalb_preset_cars(glob_bytes)
    l2ra_markers = parse_l2ra_start_markers(l2ra_bytes, road_net.ps2_origin)

    db_attr = vlt_ps2.VpakDatabase(attr_bytes)
    upgrade_tiers = parse_blacklist_upgrade_tiers(db_attr)

    db_gp = vlt_ps2.VpakDatabase(gp_bytes, schema_source=db_attr)
    col_by_key: Dict[int, vlt_ps2.VltCollection] = {
        c.key: c for v in db_gp.vaults for c in v.collections
    }
    vault_by_name: Dict[str, vlt_ps2.VltVault] = {v.name: v for v in db_gp.vaults}

    events: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # A. All 15 Blacklist Boss Duels (boss_15 Sonny -> boss_01 Razor)
    # ------------------------------------------------------------------
    for rank in range(15, 0, -1):
        vname = f"race_bin_{rank:02d}"
        vault = vault_by_name.get(vname)
        spec = BLACKLIST_VISUAL_SPECS[rank]

        boss_col = None
        boss_races: List[vlt_ps2.VltCollection] = []
        fallback_races: List[vlt_ps2.VltCollection] = []
        if vault is not None:
            for c in vault.collections:
                if c.parent_name == "boss" and boss_col is None:
                    boss_col = c
                if "EventID" in c.fields and c.parent_name in ("p2p", "circuit", "speedtraprace"):
                    is_boss_race = bool(c.fields.get("BossRace") and c.fields["BossRace"].value)
                    if is_boss_race:
                        boss_races.append(c)
                    else:
                        fallback_races.append(c)

        # Prefer authentic p2p boss sprint, then circuit boss race, then speedtrap boss race
        def _boss_race_priority(rc: vlt_ps2.VltCollection) -> Tuple[int, float]:
            p_rank = {"p2p": 0, "circuit": 1, "speedtraprace": 2}.get(rc.parent_name or "", 3)
            rlen = float(rc.fields["RaceLength"].value) if "RaceLength" in rc.fields else 99999.0
            # Favor crisp 4-10 km routes for punchy Blacklist boss duels
            return (p_rank, abs(rlen - 6500.0))

        candidates = sorted(boss_races, key=_boss_race_priority) + sorted(fallback_races, key=_boss_race_priority)
        chosen_race = None
        built_route = None
        for rc in candidates:
            s_ps2, g_ps2 = _extract_raw_race_points(rc, col_by_key, prefer_speedtraps=False)
            if s_ps2 is None or len(g_ps2) < 3:
                continue
            route = build_connected_event_route(
                road_net,
                s_ps2,
                g_ps2,
                gate_radius_m=15.0,
                is_speedtrap=False,
                max_gates=10,
                min_gate_spacing_m=120.0,
            )
            if route is not None and route["routeLengthM"] >= 600.0:
                chosen_race = rc
                built_route = route
                break

        if built_route is None or chosen_race is None:
            raise RuntimeError(f"Failed to build connected WRoadNetwork route for Blacklist #{rank}")

        # Resolve authentic localized strings from ENGLISH.BIN + PresetCar from GLOBALB.BUN
        boss_alias = eng_map.get(bstringhash(f"BLACKLIST_RIVAL_{rank:02d}_AKA"), spec["alias"]) or spec["alias"]
        boss_full = eng_map.get(bstringhash(f"BLACKLIST_RIVAL_{rank:02d}_NAME"), spec["full"]) or spec["full"]
        iso_car_display = eng_map.get(bstringhash(f"BLACKLIST_RIVAL_{rank:02d}_CAR"), spec["car"]) or spec["car"]

        preset_key = spec["preset"]
        if rank > 1 and boss_col is not None and "PresetRide" in boss_col.fields:
            pr = str(boss_col.fields["PresetRide"].value or "").strip().upper()
            if pr in preset_cars:
                preset_key = pr

        pcar = preset_cars.get(preset_key, {})
        car_key = str(pcar.get("model") or spec["model"]).lower()
        # Rank #1 (Razor) uses RAZORMUSTANG (Ford Mustang GT) per v1.2 spec while preserving storyCarKey
        car_display = spec["car"] if rank == 1 else iso_car_display
        story_car_key = "bmwm3gtre46" if rank == 1 else car_key

        skill_level = 0
        if boss_col is not None and "SkillLevel" in boss_col.fields:
            skill_level = int(boss_col.fields["SkillLevel"].value or 0)
        tier_info = upgrade_tiers.get(rank, {"multiplier": 1.0, "levels": {}, "total_level": 0})
        ev_id_str = str(chosen_race.fields["EventID"].value)
        track_title = lookup_track_name(eng_map, ev_id_str, fallback=f"Blacklist #{rank} Duel")
        district_name, district_idx, _ = road_net.nearest_landmark_name(built_route["startPos"])

        # Par time scales smoothly with route length and boss tier speed (48..72 m/s avg)
        avg_speed_ms = 44.0 + (16 - rank) * 1.65
        par_time_sec = round(built_route["routeLengthM"] / avg_speed_ms, 1)

        events.append(
            {
                "id": f"boss_{rank:02d}",
                "kind": "boss",
                "title": f"Blacklist #{rank} — {boss_alias}",
                "trackName": track_title,
                "district": district_name,
                "districtIndex": district_idx,
                "sourceEventId": ev_id_str,
                "sourceVault": chosen_race.name,
                "bossRank": rank,
                "bossName": boss_alias,
                "bossFullName": boss_full,
                "bossCarKey": car_key,
                "bossStoryCarKey": story_car_key,
                "bossCarName": car_display,
                "bossPresetName": preset_key,
                "bossUpgradeMultiplier": float(tier_info["multiplier"]),
                "bossUpgradeLevels": tier_info["levels"],
                "bossSkillLevel": skill_level,
                "bossPaintRGB": spec["rgb"],
                "bountyReward": int(spec["bounty"]),
                "targetScore": par_time_sec,
                "parTimeSec": par_time_sec,
                "startPos": built_route["startPos"],
                "startYawRad": built_route["startYawRad"],
                "startRoadNodeIdx": built_route["startRoadNodeIdx"],
                "routeLengthM": built_route["routeLengthM"],
                "maxSnapDistM": built_route["maxSnapDistM"],
                "checkpoints": built_route["checkpoints"],
                "routeWaypoints": built_route["routeWaypoints"],
            }
        )

    # ------------------------------------------------------------------
    # B. Curated Authentic Checkpoint Sprints across ALL 6 Districts (18)
    # ------------------------------------------------------------------
    sprint_candidates_by_district: Dict[str, List[Tuple[float, vlt_ps2.VltCollection, str, Dict[str, Any]]]] = (
        collections.defaultdict(list)
    )
    seen_sprint_ev_ids = set()

    for v in db_gp.vaults:
        for c in v.collections:
            if c.parent_name != "p2p" or "EventID" not in c.fields:
                continue
            ev_id = str(c.fields["EventID"].value).strip()
            if not ev_id or ev_id in seen_sprint_ev_ids:
                continue
            tname = lookup_track_name(eng_map, ev_id, fallback="")
            if not tname:
                continue
            s_ps2, g_ps2 = _extract_raw_race_points(c, col_by_key, prefer_speedtraps=False)
            if s_ps2 is None or len(g_ps2) < 4:
                continue
            route = build_connected_event_route(
                road_net,
                s_ps2,
                g_ps2,
                gate_radius_m=15.0,
                is_speedtrap=False,
                max_gates=10,
                min_gate_spacing_m=120.0,
            )
            if route is None or route["routeLengthM"] < 1200.0:
                continue
            seen_sprint_ev_ids.add(ev_id)
            dist_name, dist_idx, lm_dist = road_net.nearest_landmark_name(route["startPos"])
            # Score: favor close to district landmark, low road-snap error, non-reverse first
            rev_pen = 180.0 if ev_id.lower().endswith(".r") else 0.0
            score = lm_dist + route["maxSnapDistM"] * 8.0 + rev_pen
            sprint_candidates_by_district[dist_name].append((score, c, tname, route))

    selected_sprints: List[Tuple[str, vlt_ps2.VltCollection, str, Dict[str, Any]]] = []
    # Guarantee coverage across ALL 6 landmarks in canonical landmark order
    lm_names = [lm["name"] for lm in road_net.landmarks] or sorted(sprint_candidates_by_district.keys())
    for lm_name in lm_names:
        cands = sorted(sprint_candidates_by_district.get(lm_name, []), key=lambda x: x[0])
        for _score, col, tname, route in cands[:TARGET_SPRINTS_PER_DISTRICT]:
            selected_sprints.append((lm_name, col, tname, route))

    # Top up to 18 if any district had fewer than 3 sprints (e.g. Downtown East Park has 2)
    used_sprint_names = {col.name for _, col, _, _ in selected_sprints}
    if len(selected_sprints) < TARGET_SPRINTS_PER_DISTRICT * len(lm_names):
        overflow: List[Tuple[float, str, vlt_ps2.VltCollection, str, Dict[str, Any]]] = []
        for lm_name, cands in sprint_candidates_by_district.items():
            for score, col, tname, route in cands:
                if col.name not in used_sprint_names:
                    overflow.append((score, lm_name, col, tname, route))
        overflow.sort(key=lambda x: x[0])
        for _score, lm_name, col, tname, route in overflow:
            if len(selected_sprints) >= TARGET_SPRINTS_PER_DISTRICT * len(lm_names):
                break
            selected_sprints.append((lm_name, col, tname, route))
            used_sprint_names.add(col.name)

    for idx, (dist_name, col, tname, route) in enumerate(selected_sprints, start=1):
        ev_id = str(col.fields["EventID"].value).strip()
        _, dist_idx, _ = road_net.nearest_landmark_name(route["startPos"])
        cash = int(col.fields["CashValue"].value) if "CashValue" in col.fields and col.fields["CashValue"].value else 5000
        par_time_sec = round(route["routeLengthM"] / 52.0, 1)
        events.append(
            {
                "id": f"sprint_{idx:02d}",
                "kind": "sprint",
                "title": tname,
                "trackName": tname,
                "district": dist_name,
                "districtIndex": dist_idx,
                "sourceEventId": ev_id,
                "sourceVault": col.name,
                "bossRank": 0,
                "bossName": "",
                "bossFullName": "",
                "bossCarKey": "",
                "bossStoryCarKey": "",
                "bossCarName": "",
                "bossPresetName": "",
                "bossUpgradeMultiplier": 1.0,
                "bossUpgradeLevels": {},
                "bossSkillLevel": 0,
                "bossPaintRGB": [1.0, 0.72, 0.14],
                "bountyReward": max(2500, cash),
                "targetScore": par_time_sec,
                "parTimeSec": par_time_sec,
                "startPos": route["startPos"],
                "startYawRad": route["startYawRad"],
                "startRoadNodeIdx": route["startRoadNodeIdx"],
                "routeLengthM": route["routeLengthM"],
                "maxSnapDistM": route["maxSnapDistM"],
                "checkpoints": route["checkpoints"],
                "routeWaypoints": route["routeWaypoints"],
            }
        )

    # ------------------------------------------------------------------
    # C. Curated Authentic Speedtraps (10 events, 4..6 radar traps each)
    # ------------------------------------------------------------------
    speedtrap_candidates: List[Tuple[str, float, vlt_ps2.VltCollection, str, Dict[str, Any]]] = []
    seen_st_ev_ids = set()

    for v in db_gp.vaults:
        for c in v.collections:
            if c.parent_name != "speedtraprace" or "EventID" not in c.fields:
                continue
            ev_id = str(c.fields["EventID"].value).strip()
            if not ev_id or ev_id in seen_st_ev_ids:
                continue
            tname = lookup_track_name(eng_map, ev_id, fallback="")
            if not tname:
                continue
            s_ps2, traps_ps2 = _extract_raw_race_points(c, col_by_key, prefer_speedtraps=True)
            if s_ps2 is None or len(traps_ps2) < 4:
                continue
            route = build_connected_event_route(
                road_net,
                s_ps2,
                traps_ps2,
                gate_radius_m=18.0,
                is_speedtrap=True,
                max_gates=6,
                min_gate_spacing_m=120.0,
            )
            if route is None or not (4 <= len(route["checkpoints"]) <= 6):
                continue
            seen_st_ev_ids.add(ev_id)
            dist_name, _dist_idx, lm_dist = road_net.nearest_landmark_name(route["startPos"])
            rev_pen = 140.0 if ev_id.lower().endswith(".r") else 0.0
            score = lm_dist + route["maxSnapDistM"] * 8.0 + rev_pen
            speedtrap_candidates.append((dist_name, score, c, tname, route))

    # Guarantee flagship Highway 99 speedtrap ("Hwy 99", 4.5.1) comes FIRST,
    # followed by best speedtrap in every district (Rosewood Blvd, Downtown East Park,
    # Camden Beach, Highway 99, Rosewood College, Rockport Waterfront), then top up to 10!
    selected_st: List[Tuple[str, str, vlt_ps2.VltCollection, str, Dict[str, Any]]] = []
    used_st_vaults = set()

    # 1. Pin authentic "Hwy 99" (EventID 4.5.1) as flagship speedtrap_hwy99
    for dist_name, _score, col, tname, route in speedtrap_candidates:
        ev_id = str(col.fields["EventID"].value).strip()
        if ev_id == "4.5.1" or tname.lower() == "hwy 99":
            selected_st.append(("speedtrap_hwy99", "Highway 99", col, "Highway 99 Speedtrap", route))
            used_st_vaults.add(col.name)
            break

    # 2. Pick best remaining speedtrap for each of the 6 Rockport districts
    by_dist_st: Dict[str, List[Tuple[float, vlt_ps2.VltCollection, str, Dict[str, Any]]]] = collections.defaultdict(list)
    for dist_name, score, col, tname, route in speedtrap_candidates:
        if col.name not in used_st_vaults:
            by_dist_st[dist_name].append((score, col, tname, route))

    st_counter = 2
    for lm_name in lm_names:
        cands = sorted(by_dist_st.get(lm_name, []), key=lambda x: x[0])
        if cands:
            _score, col, tname, route = cands[0]
            selected_st.append((f"speedtrap_{st_counter:02d}", lm_name, col, tname, route))
            used_st_vaults.add(col.name)
            st_counter += 1

    # 3. Top up to TARGET_SPEEDTRAPS_TOTAL (10)
    remaining_st = sorted(
        [(score, dist_name, col, tname, route) for dist_name, score, col, tname, route in speedtrap_candidates if col.name not in used_st_vaults],
        key=lambda x: x[0],
    )
    for _score, dist_name, col, tname, route in remaining_st:
        if len(selected_st) >= TARGET_SPEEDTRAPS_TOTAL:
            break
        selected_st.append((f"speedtrap_{st_counter:02d}", dist_name, col, tname, route))
        used_st_vaults.add(col.name)
        st_counter += 1

    for ev_key, dist_name, col, tname, route in selected_st:
        ev_id = str(col.fields["EventID"].value).strip()
        _, dist_idx, _ = road_net.nearest_landmark_name(route["startPos"])
        if dist_name == "Highway 99":
            for lm in road_net.landmarks:
                if lm["name"] == "Highway 99":
                    dist_idx = lm["index"]
                    break
        prog_lvl = int(col.fields["ProgressionLevel"].value) if "ProgressionLevel" in col.fields and col.fields["ProgressionLevel"].value else 8
        n_traps = len(route["checkpoints"])
        # Authentic escalating radar speed target: 160..195 km/h average per trap
        per_trap_target = 160.0 + max(0, 15 - prog_lvl) * 2.5
        target_cum_kmh = round(n_traps * per_trap_target, 0)
        par_time_sec = round(route["routeLengthM"] / 56.0, 1)
        cash = int(col.fields["CashValue"].value) if "CashValue" in col.fields and col.fields["CashValue"].value else 6000

        events.append(
            {
                "id": ev_key,
                "kind": "speedtrap",
                "title": tname,
                "trackName": tname,
                "district": dist_name,
                "districtIndex": dist_idx,
                "sourceEventId": ev_id,
                "sourceVault": col.name,
                "bossRank": 0,
                "bossName": "",
                "bossFullName": "",
                "bossCarKey": "",
                "bossStoryCarKey": "",
                "bossCarName": "",
                "bossPresetName": "",
                "bossUpgradeMultiplier": 1.0,
                "bossUpgradeLevels": {},
                "bossSkillLevel": 0,
                "bossPaintRGB": [0.12, 0.88, 1.0],
                "bountyReward": max(3000, cash),
                "targetScore": target_cum_kmh,
                "parTimeSec": par_time_sec,
                "startPos": route["startPos"],
                "startYawRad": route["startYawRad"],
                "startRoadNodeIdx": route["startRoadNodeIdx"],
                "routeLengthM": route["routeLengthM"],
                "maxSnapDistM": route["maxSnapDistM"],
                "checkpoints": route["checkpoints"],
                "routeWaypoints": route["routeWaypoints"],
            }
        )

    boss_count = sum(1 for e in events if e["kind"] == "boss")
    sprint_count = sum(1 for e in events if e["kind"] == "sprint")
    speedtrap_count = sum(1 for e in events if e["kind"] == "speedtrap")

    elapsed = round(time.perf_counter() - t0, 3)

    return {
        "schemaVersion": 1,
        "generator": "tools/export_events_pack.py",
        "isoName": iso_path.name,
        "rkptName": rkpt_path.name,
        "elapsedSec": elapsed,
        "summary": {
            "totalEvents": len(events),
            "bossCount": boss_count,
            "sprintCount": sprint_count,
            "speedtrapCount": speedtrap_count,
            "districtsCovered": sorted({e["district"] for e in events}),
            "gameplayVaultCount": len(db_gp.vaults),
            "presetCarCount": len(preset_cars),
            "l2raStartMarkerCount": len(l2ra_markers),
            "englishStringCount": len(eng_map),
            "roadNodeCount": int(len(road_net.nodes)),
            "roadSegmentCount": int(len(road_net.segments)),
        },
        "world": {
            "ps2Origin": [round(v, 3) for v in road_net.ps2_origin],
            "roadBoundsMinXZ": [round(road_net.road_bounds_min_xz[0], 2), round(road_net.road_bounds_min_xz[1], 2)],
            "roadBoundsMaxXZ": [round(road_net.road_bounds_max_xz[0], 2), round(road_net.road_bounds_max_xz[1], 2)],
            "landmarks": road_net.landmarks,
        },
        "events": events,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export authentic PS2 NFSMW events (Blacklist 15 Boss Duels, Sprints, Speedtraps) to JSON."
    )
    parser.add_argument("--iso", type=Path, default=None, help="Path to untouched PS2 SLUS-21351 ISO (read-only)")
    parser.add_argument("--rkpt", type=Path, default=DEFAULT_RKPT_PATH, help="Path to rockport_full.rkpt v2")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="Output events.json path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON (larger output)")
    args = parser.parse_args(argv)

    iso_path = resolve_iso_path(args.iso)
    if iso_path is None:
        print(
            "ERROR: Legal PS2 NFSMW Black Edition (SLUS-21351) ISO not found.\n"
            "  Pass --iso /path/to/nfsmw.iso or set NFSMW_ISO=/path/to/nfsmw.iso",
            file=sys.stderr,
        )
        return 2

    if not args.rkpt.is_file():
        print(
            f"ERROR: .rkpt v2 world file not found: {args.rkpt}\n"
            "  Run `make export-rockport-full` or `tools/export_rockport_world.py --district rockport_full --version 2` first.",
            file=sys.stderr,
        )
        return 2

    catalog = build_events_catalog(iso_path, args.rkpt)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.pretty:
        payload = json.dumps(catalog, indent=2)
    else:
        payload = json.dumps(catalog, separators=(",", ":"))
    args.out.write_text(payload, encoding="utf-8")

    byte_size = args.out.stat().st_size
    s = catalog["summary"]
    print(
        f"Exported {s['totalEvents']} authentic NFSMW events "
        f"({s['bossCount']} Blacklist Boss Duels, {s['sprintCount']} Sprints, {s['speedtrapCount']} Speedtraps) "
        f"-> {args.out} ({byte_size:,} bytes = {byte_size / 1024.0:.1f} KB) in {catalog['elapsedSec']:.2f}s"
    )
    if byte_size >= 500 * 1024:
        print(f"WARNING: events.json ({byte_size:,} B) reached 500 KB budget!", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
