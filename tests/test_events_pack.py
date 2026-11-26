#!/usr/bin/env python3
"""Unit & integration tests for tools/export_events_pack.py and RTEventManager.swift.

Passes cleanly both WITH and WITHOUT an untouched PS2 ISO (`NFSMW_ISO=/nonexistent`).
Covers:
  - Offline binary chunk parsers (`ENGLISH.BIN` 0x00039000, `GLOBALB.BUN` 0x00030220,
    `L2RA.BUN` 0x00034146) and `WRoadNetwork` snapping + Dijkstra connected routing.
  - Strict `swiftc -typecheck -warnings-as-errors` against `RTWorldLoader.swift` +
    `RTGameWorld.swift` + `RTEventManager.swift`.
  - Compiled native Swift runtime smoke test completing a Sprint, a Speedtrap (win +
    0.45s radar flash decay), a Blacklist 15 Boss Duel (Boss AI slot 0 + rank/gap),
    Interactive World Map controller actions, AND baking the 1024x1024 Metal World Map
    texture (6,538 road segments) in < 25 ms!
  - Live ISO + `rockport_full.rkpt` + `build/gameplay_pack/events.json` catalog audit:
    15/15 Blacklist Bosses, >= 12 Sprints across all 6 districts, >= 6 Speedtraps
    (4..6 traps each including Highway 99), 100% road-graph connectivity, and < 500 KB JSON.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402
import export_events_pack as eep  # noqa: E402
from zdir_tool import bstringhash  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic Binary Fixtures (Zero ISO Required)
# ---------------------------------------------------------------------------


def _make_synthetic_english_bin(entries: Dict[str, str]) -> bytes:
    """Build a valid EA `LANGUAGES\\ENGLISH.BIN` chunk `0x00039000`."""
    str_blob = bytearray()
    hash_entries: List[Tuple[int, int]] = []
    for key, val in entries.items():
        off = len(str_blob)
        str_blob += val.encode("latin-1") + b"\x00"
        hash_entries.append((bstringhash(key), off))

    hash_off = 32
    str_off = hash_off + len(hash_entries) * 8
    body = bytearray(struct.pack("<8I", 16, len(hash_entries), hash_off, str_off, 256, 0, 0, 0))
    for h, off in hash_entries:
        body += struct.pack("<II", h, off)
    body += str_blob
    return struct.pack("<II", eep.CHUNK_ENGLISH_STRINGS, len(body)) + bytes(body)


def _make_synthetic_globalb_preset_cars(records: List[Tuple[str, str]]) -> bytes:
    """Build a valid EA `GLOBALB.BUN` chunk `0x00030220` (`N x 656 B`)."""
    payload = bytearray()
    for idx, (model, preset) in enumerate(records):
        rec = bytearray(eep.PRESET_CAR_STRIDE)
        m_bytes = model.encode("ascii")[:31]
        p_bytes = preset.encode("ascii")[:31]
        rec[8 : 8 + len(m_bytes)] = m_bytes
        rec[40 : 40 + len(p_bytes)] = p_bytes
        struct.pack_into("<3I", rec, 72, 0x1000 + idx, 0, 0x2000 + idx)
        payload += rec
    return struct.pack("<II", eep.CHUNK_PRESET_CAR, len(payload)) + bytes(payload)


def _make_synthetic_l2ra_markers(points_ps2: List[Tuple[float, float, float]]) -> bytes:
    """Build a valid EA `L2RA.BUN` chunk `0x00034146` with 4-byte 0x11111111 header pad."""
    payload = bytearray(b"\x11\x11\x11\x11")
    for idx, (x, y, z) in enumerate(points_ps2):
        rec = bytearray(eep.START_MARKER_STRIDE)
        struct.pack_into("<3I", rec, 0, 0x0B, 0x0B, 0xAA000000 + idx)
        struct.pack_into("<3f", rec, 16, x, y, z)
        payload += rec
    return struct.pack("<II", eep.CHUNK_L2RA_START_MARKERS, len(payload)) + bytes(payload)


def _make_synthetic_road_network() -> eep.RoadNetworkGraph:
    """Build a small connected grid RoadNetworkGraph in Metal Y-up meters (5x5 nodes, 200m spacing)."""
    nodes_list = []
    for rz in range(5):
        for rx in range(5):
            nodes_list.append([rx * 200.0, 20.0, rz * 200.0])
    nodes = np.array(nodes_list, dtype=np.float64)

    segments = []
    adj: Dict[int, List[Tuple[int, float, int]]] = {}
    for rz in range(5):
        for rx in range(5):
            u = rz * 5 + rx
            if rx + 1 < 5:
                v = rz * 5 + (rx + 1)
                sidx = len(segments)
                segments.append((u, v, 16 if rz == 2 else 4, 0, 1, 200.0, 8.5 if rz == 2 else 5.0))
                adj.setdefault(u, []).append((v, 200.0, sidx))
                adj.setdefault(v, []).append((u, 200.0, sidx))
            if rz + 1 < 5:
                v = (rz + 1) * 5 + rx
                sidx = len(segments)
                segments.append((u, v, 4, 0, 2, 200.0, 5.5))
                adj.setdefault(u, []).append((v, 200.0, sidx))
                adj.setdefault(v, []).append((u, 200.0, sidx))

    seg_p0 = np.array([[nodes[s[0], 0], nodes[s[0], 2]] for s in segments], dtype=np.float64)
    seg_ab = np.array([[nodes[s[1], 0] - nodes[s[0], 0], nodes[s[1], 2] - nodes[s[0], 2]] for s in segments], dtype=np.float64)
    seg_len_sq = np.maximum(1e-4, seg_ab[:, 0] ** 2 + seg_ab[:, 1] ** 2)

    landmarks = [
        {"index": 0, "name": "Rosewood Blvd", "spawn_pos": [0.0, 20.0, 0.0], "spawn_yaw_rad": 0.0, "region_id": 0},
        {"index": 1, "name": "Highway 99", "spawn_pos": [400.0, 20.0, 400.0], "spawn_yaw_rad": 1.57, "region_id": 3},
    ]
    return eep.RoadNetworkGraph(
        ps2_origin=(0.0, 0.0, 0.0),
        world_bounds_min=(0.0, 0.0, 0.0),
        world_bounds_max=(800.0, 40.0, 800.0),
        nodes=nodes,
        segments=segments,
        landmarks=landmarks,
        adj=adj,
        seg_p0_xz=seg_p0,
        seg_ab_xz=seg_ab,
        seg_len_sq_xz=seg_len_sq,
        road_bounds_min_xz=(0.0, 0.0),
        road_bounds_max_xz=(800.0, 800.0),
    )


# ---------------------------------------------------------------------------
# 1. Offline Unit & Swift Metal Runtime Smoke Tests (Zero ISO Required)
# ---------------------------------------------------------------------------


class TestEventsPackOffline(unittest.TestCase):
    """Offline-safe parser, routing, hygiene, Makefile, and Swift RTEventManager tests."""

    def test_english_string_table_and_track_name_lookup(self) -> None:
        raw = _make_synthetic_english_bin(
            {
                "BLACKLIST_RIVAL_15_AKA": "Sonny",
                "BLACKLIST_RIVAL_01_AKA": "Razor",
                "TRACKNAME_4_5_1": "Hwy 99",
                "TRACKNAME_15_2_1": "Diamond & Union",
            }
        )
        tbl = eep.parse_english_string_table(raw)
        self.assertEqual(tbl.get(bstringhash("BLACKLIST_RIVAL_15_AKA")), "Sonny")
        self.assertEqual(tbl.get(bstringhash("BLACKLIST_RIVAL_01_AKA")), "Razor")
        self.assertEqual(eep.lookup_track_name(tbl, "4.5.1"), "Hwy 99")
        self.assertEqual(eep.lookup_track_name(tbl, "15.2.1.r"), "Diamond & Union (Reverse)")
        self.assertEqual(eep.lookup_track_name(tbl, "99.9.9", fallback=""), "")

    def test_globalb_preset_car_and_l2ra_start_marker_parsers(self) -> None:
        glob = _make_synthetic_globalb_preset_cars([("GTI", "BL15"), ("MUSTANGGT", "RAZORMUSTANG")])
        presets = eep.parse_globalb_preset_cars(glob)
        self.assertEqual(len(presets), 2)
        self.assertEqual(presets["BL15"]["model"], "gti")
        self.assertEqual(presets["RAZORMUSTANG"]["model"], "mustanggt")

        l2ra = _make_synthetic_l2ra_markers([(100.0, 200.0, 15.0), (500.0, 100.0, 22.0)])
        markers = eep.parse_l2ra_start_markers(l2ra, ps2_origin=(100.0, 200.0, 0.0))
        self.assertEqual(len(markers), 2)
        self.assertEqual(markers[0]["pos"], [0.0, 15.0, 0.0])
        # P(500, 100, 22) - (100, 200, 0) -> (400, 22, +100)
        self.assertEqual(markers[1]["pos"], [400.0, 22.0, 100.0])

    def test_road_network_snapping_and_connected_route_builder(self) -> None:
        rn = _make_synthetic_road_network()
        # Note ps2_origin=(0,0,0) => P(x, y, z) = (x, z, -y); so Metal z=+400 is PS2 y=-400
        route = eep.build_connected_event_route(
            rn,
            start_ps2=(5.0, -3.0, 20.0),
            raw_gates_ps2=[
                (400.0, 0.0, 20.0),
                (400.0, -400.0, 20.0),
                (800.0, -400.0, 20.0),
            ],
            gate_radius_m=15.0,
            is_speedtrap=False,
        )
        self.assertIsNotNone(route)
        assert route is not None
        self.assertGreaterEqual(len(route["checkpoints"]), 3)
        self.assertGreaterEqual(route["routeLengthM"], 1000.0)
        self.assertLessEqual(route["maxSnapDistM"], 10.0)
        # Verify lead-in checkpoint inserted cleanly at ~120m
        cp0 = route["checkpoints"][0]["pos"]
        sp = route["startPos"]
        self.assertLessEqual(math.hypot(cp0[0] - sp[0], cp0[2] - sp[2]), 135.0)
        # Verify every checkpoint node is connected on WRoadNetwork
        prev = route["startRoadNodeIdx"]
        for cp in route["checkpoints"]:
            self.assertTrue(rn.is_connected(prev, cp["roadNodeIdx"]))
            prev = cp["roadNodeIdx"]

    def test_makefile_and_hygiene_clean(self) -> None:
        mk = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("export-events-pack:", mk)
        self.assertIn("tools/export_events_pack.py", mk)
        # Ensure export-events-pack is chained inside export-rockport-full
        full_block = mk.split("export-rockport-full:", 1)[1].split("export-music-pack:", 1)[0]
        self.assertIn("tools/export_events_pack.py", full_block)

        owned = [
            Path("tools/export_events_pack.py"),
            Path("tests/test_events_pack.py"),
            Path("native_metal_rt/RTEventManager.swift"),
            Path("Makefile"),
        ]
        issues = audit_repo_clean.audit_files(REPO_ROOT, owned)
        self.assertEqual(issues, [], f"Hygiene issues: {[i.format_human() for i in issues]}")

    def test_resolve_iso_graceful_when_nonexistent(self) -> None:
        old = os.environ.get("NFSMW_ISO")
        try:
            os.environ["NFSMW_ISO"] = "/nonexistent/nfsmw.iso"
            self.assertIsNone(eep.resolve_iso_path())
        finally:
            if old is None:
                os.environ.pop("NFSMW_ISO", None)
            else:
                os.environ["NFSMW_ISO"] = old

    def test_swift_event_manager_typecheck_and_runtime_smoke(self) -> None:
        """Typecheck + run compiled Swift smoke test (Sprint, Speedtrap, Boss Duel, Map, 1024x1024 bake < 25 ms)."""
        if sys.platform != "darwin" or platform.system() != "Darwin" or shutil.which("swiftc") is None:
            self.skipTest("macOS swiftc toolchain unavailable")

        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        base_flags = [
            "-target",
            "arm64-apple-macosx14.0",
            "-sdk",
            sdk,
            "-framework",
            "Foundation",
            "-framework",
            "Metal",
        ]
        swift_sources = [
            str(REPO_ROOT / "native_metal_rt" / "RTHudTypes.swift"),
            str(REPO_ROOT / "native_metal_rt" / "RTWorldLoader.swift"),
            str(REPO_ROOT / "native_metal_rt" / "RTGameWorld.swift"),
            str(REPO_ROOT / "native_metal_rt" / "RTEventManager.swift"),
        ]

        # 1. Strict typecheck (0 errors, 0 warnings)
        tc_cmd = ["swiftc", "-typecheck", "-warnings-as-errors", *base_flags, *swift_sources]
        tc_res = subprocess.run(tc_cmd, capture_output=True, text=True)
        self.assertEqual(tc_res.returncode, 0, f"swiftc -typecheck failed:\n{tc_res.stderr}")

        # 2. Compiled Swift runtime smoke test (Sprint + Speedtrap + Boss Duel + Map Controller + 1024x1024 Metal bake)
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            synth_json = td_path / "synth_events.json"
            synth_catalog = {
                "schemaVersion": 1,
                "world": {
                    "roadBoundsMinXZ": [-1000.0, -1000.0],
                    "roadBoundsMaxXZ": [1000.0, 1000.0],
                    "landmarks": [
                        {"index": 0, "name": "Rosewood Blvd", "spawn_pos": [0.0, 10.0, 0.0], "spawn_yaw_rad": 0.0, "region_id": 0},
                        {"index": 1, "name": "Highway 99", "spawn_pos": [200.0, 10.0, 300.0], "spawn_yaw_rad": 1.57, "region_id": 3},
                    ],
                },
                "events": [
                    {
                        "id": "sprint_01",
                        "kind": "sprint",
                        "title": "Diamond & Union",
                        "district": "Rosewood Blvd",
                        "startPos": [0.0, 10.0, 0.0],
                        "startYawRad": 0.0,
                        "routeLengthM": 300.0,
                        "parTimeSec": 15.0,
                        "targetScore": 15.0,
                        "checkpoints": [
                            {"pos": [0.0, 10.0, 100.0], "yawRad": 0.0, "radius": 15.0, "roadNodeIdx": 1, "isSpeedtrap": False},
                            {"pos": [0.0, 10.0, 200.0], "yawRad": 0.0, "radius": 15.0, "roadNodeIdx": 2, "isSpeedtrap": False},
                            {"pos": [0.0, 10.0, 300.0], "yawRad": 0.0, "radius": 15.0, "roadNodeIdx": 3, "isSpeedtrap": False},
                        ],
                        "routeWaypoints": [
                            [0.0, 10.0, 0.0, 0.0],
                            [0.0, 10.0, 100.0, 0.0],
                            [0.0, 10.0, 200.0, 0.0],
                            [0.0, 10.0, 300.0, 0.0],
                        ],
                    },
                    {
                        "id": "speedtrap_hwy99",
                        "kind": "speedtrap",
                        "title": "Highway 99 Speedtrap",
                        "district": "Highway 99",
                        "startPos": [0.0, 10.0, 0.0],
                        "startYawRad": 0.0,
                        "routeLengthM": 400.0,
                        "parTimeSec": 20.0,
                        "targetScore": 700.0,
                        "checkpoints": [
                            {"pos": [0.0, 10.0, 100.0], "yawRad": 0.0, "radius": 18.0, "roadNodeIdx": 1, "isSpeedtrap": True},
                            {"pos": [0.0, 10.0, 200.0], "yawRad": 0.0, "radius": 18.0, "roadNodeIdx": 2, "isSpeedtrap": True},
                            {"pos": [0.0, 10.0, 300.0], "yawRad": 0.0, "radius": 18.0, "roadNodeIdx": 3, "isSpeedtrap": True},
                            {"pos": [0.0, 10.0, 400.0], "yawRad": 0.0, "radius": 18.0, "roadNodeIdx": 4, "isSpeedtrap": True},
                        ],
                        "routeWaypoints": [
                            [0.0, 10.0, 0.0, 0.0],
                            [0.0, 10.0, 200.0, 0.0],
                            [0.0, 10.0, 400.0, 0.0],
                        ],
                    },
                    {
                        "id": "boss_15",
                        "kind": "boss",
                        "title": "Blacklist #15 — Sonny",
                        "district": "Rosewood Blvd",
                        "bossRank": 15,
                        "bossName": "Sonny",
                        "bossCarKey": "gti",
                        "bossCarName": "VW Golf GTI",
                        "bossUpgradeMultiplier": 1.04,
                        "bossPaintRGB": [0.88, 0.90, 0.92],
                        "startPos": [0.0, 10.0, 0.0],
                        "startYawRad": 0.0,
                        "routeLengthM": 300.0,
                        "parTimeSec": 18.0,
                        "targetScore": 18.0,
                        "checkpoints": [
                            {"pos": [0.0, 10.0, 100.0], "yawRad": 0.0, "radius": 15.0, "roadNodeIdx": 1, "isSpeedtrap": False},
                            {"pos": [0.0, 10.0, 200.0], "yawRad": 0.0, "radius": 15.0, "roadNodeIdx": 2, "isSpeedtrap": False},
                            {"pos": [0.0, 10.0, 300.0], "yawRad": 0.0, "radius": 15.0, "roadNodeIdx": 3, "isSpeedtrap": False},
                        ],
                        "routeWaypoints": [
                            [0.0, 10.0, 0.0, 0.0],
                            [0.0, 10.0, 100.0, 0.0],
                            [0.0, 10.0, 200.0, 0.0],
                            [0.0, 10.0, 300.0, 0.0],
                        ],
                    },
                ],
            }
            synth_json.write_text(json.dumps(synth_catalog), encoding="utf-8")

            smoke_swift = td_path / "main.swift"
            smoke_swift.write_text(
                """
import Foundation
import Metal
import simd

func check(_ cond: Bool, _ msg: String) {
    if !cond {
        fputs("FAIL: \\(msg)\\n", stderr)
        exit(1)
    }
}

let args = CommandLine.arguments
let synthURL = URL(fileURLWithPath: args[1])
let missingURL = URL(fileURLWithPath: args[1]).deletingLastPathComponent().appendingPathComponent("missing.json")
let rkptPath = args.count > 2 ? args[2] : ""

// 0. Exact Metal ABI layout check
RTEventWorldFX.assertLayout()
check(MemoryLayout<RTEventWorldFX>.stride == 128, "RTEventWorldFX must be 128 bytes")
check(MemoryLayout<RTEventWorldFX>.alignment == 16, "RTEventWorldFX must be 16-byte aligned")

// 1. Graceful missing JSON fallback
do {
    let emptyMgr = RTEventManager(eventsJSONURL: missingURL)
    check(!emptyMgr.isCatalogLoaded, "missing events.json should stay unloaded")
    check(emptyMgr.events.isEmpty, "missing events.json should have 0 events")
    check(emptyMgr.startEvent(id: "boss_15") == nil, "startEvent on empty catalog must return nil")
    let fx = emptyMgr.currentWorldFX(simTime: 1.0)
    check(fx.fxParams.x == 0.0, "inactive WorldFX gateActive must be 0")
}

let mgr = RTEventManager(eventsJSONURL: synthURL)
check(mgr.isCatalogLoaded && mgr.events.count == 3, "synthetic catalog should load 3 events")

// 2. Complete a Checkpoint Sprint (idle -> countdown -> active -> win)
do {
    let start = mgr.startEvent(id: "sprint_01", skipCountdown: false)
    check(start != nil, "sprint_01 should start")
    check(mgr.eventState == .countdown, "sprint should enter countdown first")
    // Tick through 2.5s countdown
    for _ in 0..<60 {
        mgr.step(dt: 0.05, playerPos: SIMD3<Float>(0, 10, 0), playerVel: .zero, playerYaw: 0)
    }
    check(mgr.eventState == .active, "sprint should transition to active after countdown")
    let fxStart = mgr.currentWorldFX(simTime: 3.0)
    check(fxStart.fxParams.x > 0.5 && fxStart.gateDirKind.w == 0.0, "sprint gateActive=1, kind=0")
    check(fxStart.chevron0.w > -90.0, "floating road chevron should be active along route")
    check(fxStart.chevron3.w <= -90.0, "unused trailing chevron should carry <= -100 inactive sentinel")

    // Drive forward at 55 m/s through all 3 checkpoints
    var pos = SIMD3<Float>(0, 10, 0)
    let vel = SIMD3<Float>(0, 0, 55.0)
    for _ in 0..<200 {
        pos.z += vel.z * 0.05
        mgr.step(dt: 0.05, playerPos: pos, playerVel: vel, playerYaw: 0)
        if mgr.eventState == .win { break }
    }
    check(mgr.eventState == .win, "sprint should finish in .win, got \\(mgr.eventState)")
    check(mgr.currentCheckpointIndex == 3, "all 3 checkpoints crossed")
    check(mgr.elapsedSec > 2.0, "elapsed sprint timer > 2s")
}

// 3. Complete a Speedtrap (radar flash 0.45s decay + cumulative km/h win)
do {
    let start = mgr.startEvent(matching: "speedtrap", skipCountdown: true)
    check(start?.event.id == "speedtrap_hwy99", "speedtrap alias should resolve speedtrap_hwy99")
    let fx0 = mgr.currentWorldFX(simTime: 5.0)
    check(fx0.fxParams.y > 0.5 && fx0.gateDirKind.w == 1.0, "speedtrap FX flags mismatch")

    var pos = SIMD3<Float>(0, 10, 0)
    let vel = SIMD3<Float>(0, 0, 60.0) // 216 km/h -> 4 traps = 864 km/h >= 700 km/h target!
    var sawRadarFlash = false
    for _ in 0..<220 {
        pos.z += vel.z * 0.05
        mgr.step(dt: 0.05, playerPos: pos, playerVel: vel, playerYaw: 0)
        if mgr.radarFlash > 0.8 { sawRadarFlash = true }
        if mgr.eventState == .win || mgr.eventState == .lose { break }
    }
    check(sawRadarFlash, "speedtrap crossing must trigger radarFlash pulse")
    check(mgr.eventState == .win, "speedtrap 864 km/h >= 700 km/h should win")
    check(mgr.currentScore >= 850.0, "cumulative speedtrap score expected ~864, got \\(mgr.currentScore)")
    // Verify 0.45s flash decays back to 0 within 0.6s
    for _ in 0..<15 {
        mgr.step(dt: 0.05, playerPos: pos, playerVel: .zero, playerYaw: 0)
    }
    check(mgr.radarFlash == 0.0, "radarFlash must decay to 0 after 0.45s, got \\(mgr.radarFlash)")
}

// 4. Complete a Blacklist 15 Boss Duel (Boss AI TLAS slot 0 + road snap + rank/gap)
do {
    let start = mgr.startEvent(matching: "boss15", skipCountdown: true)
    check(start?.event.id == "boss_15", "boss15 alias should resolve boss_15")
    check(mgr.bossInstanceState != nil, "boss duel must spawn bossInstanceState")
    check(mgr.bossInstanceState?.slot == 0, "boss must claim AI TLAS slot 0")
    check(mgr.bossInstanceState?.active == true && mgr.bossInstanceState?.isCop == false, "boss slot active & non-cop")

    mgr.applyBossRoadProbeHeight(12.25, dt: 0.016, snapImmediate: true)
    check(abs((mgr.bossInstanceState?.worldPosition.y ?? 0) - 12.25) < 0.01, "boss road height snap failed")

    let fxBoss = mgr.currentWorldFX(simTime: 8.0)
    check(fxBoss.gateDirKind.w == 2.0, "boss gate kindFloat must be 2.0")

    // Race faster than Sonny (~72 m/s = 259 km/h) to overtake and win!
    var pos = SIMD3<Float>(0, 10, 0)
    let vel = SIMD3<Float>(0, 0, 72.0)
    var bossMoved = false
    let bossInitZ = mgr.bossInstanceState!.worldPosition.z
    for _ in 0..<160 {
        pos.z += vel.z * 0.05
        mgr.step(dt: 0.05, playerPos: pos, playerVel: vel, playerYaw: 0)
        if let bz = mgr.bossInstanceState?.worldPosition.z, bz > bossInitZ + 10.0 {
            bossMoved = true
        }
        if mgr.eventState == .win || mgr.eventState == .lose { break }
    }
    check(bossMoved, "boss AI must drive along WRoadNetwork waypoints")
    check(mgr.eventState == .win, "player at 72 m/s should beat Sonny")
    check(mgr.playerRank == 1 && mgr.bossGapMeters > 0.0, "winner must be rank 1 with positive gap")
}

// 5. Interactive World Map Controller (pan, zoom, cycle, confirm -> startEvent & teleport)
do {
    mgr.abortEvent()
    check(mgr.toggleWorldMap(playerPos: SIMD3<Float>(0, 10, 0)) == true, "toggleWorldMap should open")
    mgr.zoom(delta: 1.5)
    check(mgr.mapZoom >= 2.5 && mgr.mapZoom <= 4.5, "zoom should update within [1.0..4.5]")
    mgr.zoom(delta: 99.0)
    check(abs(mgr.mapZoom - 4.5) < 0.001, "zoom upper clamp must be 4.5")
    mgr.zoom(delta: -99.0)
    check(abs(mgr.mapZoom - 1.0) < 0.001, "zoom lower clamp must be 1.0")
    mgr.zoom(delta: 1.2)
    let c0 = mgr.mapCenterNorm
    mgr.pan(dx: 1.0, dy: -0.5, dt: 0.05)
    check(mgr.mapCenterNorm != c0, "pan should shift mapCenterNorm")

    // Cycle to an event marker and confirm -> .startEvent
    var foundEventAction = false
    var foundTeleportAction = false
    for i in 0..<mgr.mapMarkers.count {
        mgr.openWorldMap()
        mgr.selectMarker(index: i)
        let hud = mgr.currentHudState()
        check(hud.worldMapOpen, "hud.worldMapOpen must be true")
        check(hud.mapMarkers.count <= 24, "mapMarkers capped at 24")
        check(hud.gpsRoutePointsNorm.count <= 16, "gpsRoutePointsNorm capped at 16")
        let act = mgr.confirmSelection()
        if case .startEvent(_) = act { foundEventAction = true }
        if case .teleport(_, _, _) = act { foundTeleportAction = true }
    }
    check(foundEventAction, "World Map confirmSelection must support .startEvent")
    check(foundTeleportAction, "World Map confirmSelection must support .teleport")
}

// 6. O(1) Pre-Baked 1024x1024 Metal World Map Texture Baker (< 25 ms for 6,538 segments!)
guard let device = MTLCreateSystemDefaultDevice() else {
    fputs("SKIP: MTLCreateSystemDefaultDevice() returned nil\\n", stderr)
    exit(3)
}

func verifyTextureHasRoadsAndHighways(_ tex: MTLTexture, label: String) {
    check(tex.width == 1024 && tex.height == 1024, "\\(label): expected 1024x1024")
    check(tex.pixelFormat == .rgba8Unorm, "\\(label): expected .rgba8Unorm")
    var bytes = [UInt8](repeating: 0, count: 1024 * 1024 * 4)
    tex.getBytes(&bytes, bytesPerRow: 1024 * 4, from: MTLRegionMake2D(0, 0, 1024, 1024), mipmapLevel: 0)
    var roadPx = 0
    var hwyPx = 0
    for i in stride(from: 0, to: bytes.count, by: 4) {
        let r = bytes[i], g = bytes[i + 1], b = bytes[i + 2]
        if r > 220 && g > 150 && b < 90 { hwyPx += 1 }
        if b > 140 && r < 140 { roadPx += 1 }
    }
    check(roadPx > 500, "\\(label): expected >500 city road pixels, got \\(roadPx)")
    check(hwyPx > 200, "\\(label): expected >200 amber highway pixels, got \\(hwyPx)")
}

// 6a. Offline synthetic 4,385 nodes / 6,538 segments stress benchmark (< 25 ms!)
do {
    var nodes: [RkptRoadNode] = []
    nodes.reserveCapacity(4385)
    for i in 0..<4385 {
        let gx = Float(i % 67) * 85.0 - 2800.0
        let gz = Float(i / 67) * 85.0 - 2800.0
        nodes.append(RkptRoadNode(
            pos: SIMD3<Float>(gx, 20.0, gz),
            nodeIndex: UInt16(i),
            flags: 0,
            degree: 2,
            adjSegments: (0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF)
        ))
    }
    var segs: [RkptRoadSegment] = []
    segs.reserveCapacity(6538)
    for i in 0..<6538 {
        let n0 = UInt16(i % 4384)
        let n1 = UInt16((i % 2 == 0) ? min(4384, Int(n0) + 1) : min(4384, Int(n0) + 67))
        let isHwy = (i % 7 == 0)
        segs.append(RkptRoadSegment(
            node0: n0,
            node1: n1,
            laneFlags: isHwy ? 16 : 4,
            dirFlags: 0,
            roadId: UInt16(i & 0xFF),
            lengthMeters: 85.0,
            halfWidthMeters: isHwy ? 9.0 : 5.0
        ))
    }
    let lms = [
        RkptLandmark(name: "Rosewood Blvd", spawnPos: SIMD3<Float>(-800, 20, -800), spawnYawRad: 0, regionId: 0, flags: 0),
        RkptLandmark(name: "Highway 99", spawnPos: SIMD3<Float>(0, 20, 0), spawnYawRad: 0, regionId: 3, flags: 0),
    ]
    let tex = mgr.bakeWorldMapTexture(device: device, roadNodes: nodes, roadSegments: segs, landmarks: lms)
    check(tex != nil, "synthetic 6,538-seg bake returned nil")
    check(mgr.lastBakeDurationMs < 25.0, "synthetic 6,538-seg bake took \\(String(format: "%.2f", mgr.lastBakeDurationMs)) ms (limit < 25 ms)")
    verifyTextureHasRoadsAndHighways(tex!, label: "synthetic_6538")
    print(String(format: "SYNTH_BAKE_MS=%.2f", mgr.lastBakeDurationMs))
}

// 6b. Real rockport_full.rkpt 6,538-segment bake when present on disk (< 25 ms!)
if !rkptPath.isEmpty && FileManager.default.fileExists(atPath: rkptPath) {
    let graph = try RTRoadGraph.loadFromRkpt(url: URL(fileURLWithPath: rkptPath))
    let rNodes: [RkptRoadNode] = graph.nodes.map { n in
        RkptRoadNode(
            pos: n.pos,
            nodeIndex: n.nodeIdx,
            flags: n.flags,
            degree: n.degree,
            adjSegments: (0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF)
        )
    }
    let rSegs: [RkptRoadSegment] = graph.segments.map { s in
        RkptRoadSegment(
            node0: s.node0,
            node1: s.node1,
            laneFlags: s.laneFlags,
            dirFlags: s.dirFlags,
            roadId: s.roadId,
            lengthMeters: s.lengthM,
            halfWidthMeters: s.halfWidthM
        )
    }
    let tex = mgr.bakeWorldMapTexture(device: device, roadNodes: rNodes, roadSegments: rSegs, landmarks: mgr.landmarks)
    check(tex != nil, "real rockport_full bake returned nil")
    check(mgr.lastBakeDurationMs < 25.0, "real rockport_full bake took \\(String(format: "%.2f", mgr.lastBakeDurationMs)) ms (limit < 25 ms)")
    verifyTextureHasRoadsAndHighways(tex!, label: "rockport_full_6538")
    print(String(format: "REAL_RKPT_BAKE_MS=%.2f nodes=%d segs=%d", mgr.lastBakeDurationMs, rNodes.count, rSegs.count))
}

// 6c. Fallback 1x1 texture helper
let fb = RTEventManager.makeFallbackWorldMapTexture(device: device)
check(fb != nil && fb!.width == 1 && fb!.height == 1, "fallback 1x1 world map texture failed")

print("SWIFT_EVENTS_SMOKE_PASS")
""",
                encoding="utf-8",
            )

            smoke_bin = td_path / "events_smoke"
            build_cmd = [
                "swiftc",
                "-O",
                "-warnings-as-errors",
                *base_flags,
                *swift_sources,
                str(smoke_swift),
                "-o",
                str(smoke_bin),
            ]
            b_res = subprocess.run(build_cmd, capture_output=True, text=True)
            self.assertEqual(b_res.returncode, 0, f"swiftc smoke build failed:\n{b_res.stderr}")

            rkpt_arg = str(eep.DEFAULT_RKPT_PATH) if eep.DEFAULT_RKPT_PATH.is_file() else ""
            run_cmd = [str(smoke_bin), str(synth_json)]
            if rkpt_arg:
                run_cmd.append(rkpt_arg)
            r_res = subprocess.run(run_cmd, capture_output=True, text=True)
            if r_res.returncode == 3:
                self.skipTest(r_res.stderr.strip() or "No Metal GPU device available on this host")
            self.assertEqual(
                r_res.returncode,
                0,
                f"Swift events smoke test failed:\nSTDOUT:\n{r_res.stdout}\nSTDERR:\n{r_res.stderr}",
            )
            self.assertIn("SWIFT_EVENTS_SMOKE_PASS", r_res.stdout)


# ---------------------------------------------------------------------------
# 2. Live ISO & Exported events.json Catalog Tests (Skipped if ISO Absent)
# ---------------------------------------------------------------------------


class TestEventsPackLiveISO(unittest.TestCase):
    """End-to-end validation of `build/gameplay_pack/events.json` + live ISO export."""

    def setUp(self) -> None:
        self.iso_path = eep.resolve_iso_path()
        self.rkpt_path = eep.DEFAULT_RKPT_PATH
        self.events_path = eep.DEFAULT_OUT_PATH

    def test_exported_events_json_catalog_and_connectivity(self) -> None:
        if self.iso_path is None or not self.rkpt_path.is_file():
            self.skipTest("Untouched PS2 ISO or rockport_full.rkpt not present")

        if not self.events_path.is_file():
            rc = eep.main(["--iso", str(self.iso_path), "--rkpt", str(self.rkpt_path), "--out", str(self.events_path)])
            self.assertEqual(rc, 0)

        byte_size = self.events_path.stat().st_size
        self.assertGreater(byte_size, 10_000)
        self.assertLess(byte_size, 500 * 1024, f"events.json ({byte_size:,} B) exceeded 500 KB budget")

        data = json.loads(self.events_path.read_text(encoding="utf-8"))
        summary = data["summary"]
        events = data["events"]

        self.assertEqual(summary["gameplayVaultCount"], 272)
        self.assertEqual(summary["presetCarCount"], 82)
        self.assertEqual(summary["l2raStartMarkerCount"], 328)
        self.assertEqual(summary["roadNodeCount"], 4385)
        self.assertEqual(summary["roadSegmentCount"], 6538)

        # 1. All 15 Blacklist Bosses (boss_15 Sonny -> boss_01 Razor)
        bosses = [e for e in events if e["kind"] == "boss"]
        self.assertEqual(len(bosses), 15, f"Expected 15 Blacklist bosses, got {len(bosses)}")
        ranks = [b["bossRank"] for b in bosses]
        self.assertEqual(ranks, list(range(15, 0, -1)))

        by_rank = {b["bossRank"]: b for b in bosses}
        self.assertEqual(by_rank[15]["id"], "boss_15")
        self.assertEqual(by_rank[15]["bossName"], "Sonny")
        self.assertEqual(by_rank[15]["bossCarKey"], "gti")
        self.assertEqual(by_rank[1]["id"], "boss_01")
        self.assertEqual(by_rank[1]["bossName"], "Razor")
        self.assertEqual(by_rank[1]["bossCarKey"], "mustanggt")
        self.assertGreater(by_rank[1]["bossUpgradeMultiplier"], by_rank[15]["bossUpgradeMultiplier"])

        for b in bosses:
            self.assertEqual(len(b["bossPaintRGB"]), 3)
            self.assertGreaterEqual(len(b["checkpoints"]), 4)
            self.assertGreaterEqual(len(b["routeWaypoints"]), 10)

        # 2. >= 12 Authentic Checkpoint Sprints across all 6 Rockport districts
        sprints = [e for e in events if e["kind"] == "sprint"]
        self.assertGreaterEqual(len(sprints), 12)
        sprint_districts = {s["district"] for s in sprints}
        expected_districts = {
            "Rosewood Blvd",
            "Downtown East Park",
            "Camden Beach",
            "Highway 99",
            "Rosewood College",
            "Rockport Waterfront",
        }
        self.assertEqual(sprint_districts, expected_districts)
        for sp in sprints:
            self.assertTrue(sp["title"] and not sp["title"].startswith("Event "))
            self.assertGreaterEqual(len(sp["checkpoints"]), 5)

        # 3. >= 6 Authentic Speedtraps (4..6 radar traps each + target cumulative km/h)
        speedtraps = [e for e in events if e["kind"] == "speedtrap"]
        self.assertGreaterEqual(len(speedtraps), 6)
        st_districts = {st["district"] for st in speedtraps}
        for req_dist in ("Highway 99", "Rosewood Blvd", "Downtown East Park", "Camden Beach"):
            self.assertIn(req_dist, st_districts, f"Missing required Speedtrap district: {req_dist}")
        for st in speedtraps:
            n_traps = len(st["checkpoints"])
            self.assertGreaterEqual(n_traps, 4, f"{st['id']} has < 4 traps")
            self.assertLessEqual(n_traps, 6, f"{st['id']} has > 6 traps")
            self.assertGreater(st["targetScore"], 500.0)
            for cp in st["checkpoints"]:
                self.assertTrue(cp["isSpeedtrap"])

        # 4. Full WRoadNetwork road-graph connectivity audit across EVERY exported event
        rn = eep.load_rkpt_road_network(self.rkpt_path)
        n_nodes = len(rn.nodes)
        for ev in events:
            s_node = ev["startRoadNodeIdx"]
            self.assertTrue(0 <= s_node < n_nodes, f"{ev['id']} invalid startRoadNodeIdx={s_node}")
            prev = s_node
            for cp in ev["checkpoints"]:
                cn = cp["roadNodeIdx"]
                self.assertTrue(0 <= cn < n_nodes, f"{ev['id']} invalid cp node={cn}")
                self.assertTrue(rn.is_connected(prev, cn), f"{ev['id']} disconnected edge {prev} -> {cn}")
                prev = cn
                for v in cp["pos"]:
                    self.assertTrue(math.isfinite(v))


if __name__ == "__main__":
    unittest.main()
