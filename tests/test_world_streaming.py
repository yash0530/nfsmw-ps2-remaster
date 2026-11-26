#!/usr/bin/env python3
"""Unit and live Metal GPU verification for RTWorldLoader.swift (.rkpt v1/v2 BC7) & RTWorldStreamer.swift."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
METAL_RT_DIR = REPO_ROOT / "native_metal_rt"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_rockport_world as erw  # noqa: E402

SWIFT_HARNESS_SOURCE = r"""
import Foundation
import Metal
import simd

func runHarness() throws {
    let args = CommandLine.arguments
    guard args.count >= 2 else {
        fputs("Usage: harness <rkpt_path> [v1_rkpt_path]\n", stderr)
        exit(2)
    }
    guard let device = MTLCreateSystemDefaultDevice(),
          let queue = device.makeCommandQueue() else {
        fputs("No Metal GPU device available\n", stderr)
        exit(3)
    }
    guard device.supportsRaytracing else {
        fputs("Metal hardware raytracing unavailable\n", stderr)
        exit(3)
    }

    let url = URL(fileURLWithPath: args[1])
    let t0 = CFAbsoluteTimeGetCurrent()
    let world = try RTWorldLoader.load(url: url, device: device, commandQueue: queue)
    let loadSec = CFAbsoluteTimeGetCurrent() - t0

    let t1 = CFAbsoluteTimeGetCurrent()
    let streamer = try RTWorldStreamer(world: world, device: device)
    let streamerInitMs = (CFAbsoluteTimeGetCurrent() - t1) * 1000.0

    var landmarkResults: [[String: Any]] = []
    var maxActiveInstances = 0
    var maxSpatialInstances = 0
    for (idx, lm) in streamer.landmarks.enumerated() {
        let res = streamer.update(playerPos: lm.spawnPos, forceRebuild: true)
        maxActiveInstances = max(maxActiveInstances, res.activeInstances.count)
        let spCount = res.activeInstances.filter {
            let sIdx = Int($0.sectionIndex)
            return sIdx < world.sections.count && !world.sections[sIdx].isPanorama
        }.count
        maxSpatialInstances = max(maxSpatialInstances, spCount)

        // Hysteresis check: tiny 2m nudge should NOT trigger dirty rebuild
        let nudge = streamer.update(playerPos: lm.spawnPos + SIMD3<Float>(2.0, 0.0, 2.0), forceRebuild: false)
        let cellLights = streamer.lightsAt(worldXZ: SIMD2<Float>(lm.spawnPos.x, lm.spawnPos.z))
        let resolvedAlias = streamer.landmark(namedOrIndex: String(idx))?.name ?? ""

        landmarkResults.append([
            "name": lm.name,
            "resolvedByIndex": resolvedAlias,
            "activeInstances": res.activeInstances.count,
            "spatialInstances": spCount,
            "activeSections": res.activeSectionCount,
            "panoramaSections": res.panoramaCount,
            "nudgeDirty": nudge.dirty,
            "lightsInCell": cellLights.count,
        ])
    }

    // Stress-test worst-case dense highway interchange section center
    var denseMaxInst = 0
    var denseMaxSpatial = 0
    for sec in world.sections where !sec.isPanorama && sec.instanceCount > 200 {
        let res = streamer.update(playerPos: sec.center, forceRebuild: false)
        denseMaxInst = max(denseMaxInst, res.activeInstances.count)
        let spCount = res.activeInstances.filter {
            let sIdx = Int($0.sectionIndex)
            return sIdx < world.sections.count && !world.sections[sIdx].isPanorama
        }.count
        denseMaxSpatial = max(denseMaxSpatial, spCount)
    }

    // Verify real GPU TLAS build on streamed active instances (proves resident BLASes + BC7 textures validate on GPU)
    let firstLm = streamer.landmarks[0]
    let activeRes = streamer.update(playerPos: firstLm.spawnPos, forceRebuild: true)
    let instCnt = max(1, activeRes.activeInstances.count)
    let descBuf = device.makeBuffer(
        length: instCnt * MemoryLayout<MTLAccelerationStructureInstanceDescriptor>.stride,
        options: .storageModeShared
    )!
    let descPtr = descBuf.contents().bindMemory(to: MTLAccelerationStructureInstanceDescriptor.self, capacity: instCnt)
    for (i, wInst) in activeRes.activeInstances.enumerated() {
        var d = MTLAccelerationStructureInstanceDescriptor()
        d.transformationMatrix = wInst.transform4x3
        d.options = world.meshes[Int(wInst.meshIndex)].hasAlphaCutout ? .nonOpaque : .opaque
        d.mask = RTTLASMask.worldOpaque
        d.intersectionFunctionTableOffset = 0
        d.accelerationStructureIndex = UInt32(world.meshToBlasIndex[Int(wInst.meshIndex)])
        descPtr[i] = d
    }
    let tlasDesc = MTLInstanceAccelerationStructureDescriptor()
    tlasDesc.instancedAccelerationStructures = world.blasList
    tlasDesc.instanceCount = activeRes.activeInstances.count
    tlasDesc.instanceDescriptorBuffer = descBuf
    tlasDesc.instanceDescriptorType = .default
    let tlasSizes = device.accelerationStructureSizes(descriptor: tlasDesc)
    let tlas = device.makeAccelerationStructure(size: tlasSizes.accelerationStructureSize)!
    let tlasScratch = device.makeBuffer(length: max(tlasSizes.buildScratchBufferSize, 256), options: .storageModePrivate)!
    let t2 = CFAbsoluteTimeGetCurrent()
    let cb = queue.makeCommandBuffer()!
    let enc = cb.makeAccelerationStructureCommandEncoder()!
    enc.build(accelerationStructure: tlas, descriptor: tlasDesc, scratchBuffer: tlasScratch, scratchBufferOffset: 0)
    enc.endEncoding()
    cb.commit()
    cb.waitUntilCompleted()
    if let err = cb.error { throw err }
    let tlasBuildMs = (CFAbsoluteTimeGetCurrent() - t2) * 1000.0

    // Optional backward-compat v1 load check
    var v1Info: [String: Any] = [:]
    if args.count >= 3 {
        let v1Url = URL(fileURLWithPath: args[2])
        let tv0 = CFAbsoluteTimeGetCurrent()
        let v1World = try RTWorldLoader.load(url: v1Url, device: device, commandQueue: queue)
        let v1Sec = CFAbsoluteTimeGetCurrent() - tv0
        let v1Streamer = try RTWorldStreamer(world: v1World, device: device)
        let v1Res = v1Streamer.update(playerPos: v1World.spawnPos, forceRebuild: true)
        v1Info = [
            "version": v1World.header.version,
            "sections": v1World.sections.count,
            "instances": v1Res.activeInstances.count,
            "loadSeconds": round(v1Sec * 1000.0) / 1000.0,
            "vramMB": round(Double(v1Streamer.estimatedWorldVRAMBytes) / 1048576.0 * 100.0) / 100.0,
        ]
    }

    let bc7TexCount = world.textures.filter { $0.pixelFormat == .bc7_rgbaUnorm_srgb }.count
    let mipmappedBC7Count = world.textures.filter { $0.pixelFormat == .bc7_rgbaUnorm_srgb && $0.mipmapLevelCount > 1 }.count
    let out: [String: Any] = [
        "version": world.header.version,
        "loadSeconds": round(loadSec * 1000.0) / 1000.0,
        "streamerInitMs": round(streamerInitMs * 1000.0) / 1000.0,
        "tlasBuildMs": round(tlasBuildMs * 1000.0) / 1000.0,
        "vramMB": round(Double(streamer.estimatedWorldVRAMBytes) / 1048576.0 * 100.0) / 100.0,
        "sectionCount": world.sections.count,
        "spatialSectionCount": world.spatialSectionCount,
        "panoramaSectionCount": world.panoramaSectionCount,
        "meshCount": world.meshes.count,
        "blasCount": world.blasList.count,
        "hasResidentBLASes": world.hasResidentBLASes,
        "textureCount": world.textures.count,
        "bc7TextureCount": bc7TexCount,
        "mipmappedBC7Count": mipmappedBC7Count,
        "roadNodeCount": world.roadNodes.count,
        "roadSegmentCount": world.roadSegments.count,
        "worldLightCount": world.worldLights.count,
        "landmarkCount": world.landmarks.count,
        "maxActiveInstances": maxActiveInstances,
        "maxSpatialInstances": maxSpatialInstances,
        "denseMaxInstances": denseMaxInst,
        "denseMaxSpatial": denseMaxSpatial,
        "aliasCamden": streamer.landmark(namedOrIndex: "camden")?.name ?? "",
        "aliasHwy99": streamer.landmark(namedOrIndex: "hwy99")?.name ?? "",
        "lightGridOrigin": [streamer.lightGridParams.originXZ.x, streamer.lightGridParams.originXZ.y],
        "lightGridInvCell": [streamer.lightGridParams.invCellXZ.x, streamer.lightGridParams.invCellXZ.y],
        "landmarks": landmarkResults,
        "v1Compat": v1Info,
    ]
    let jsonData = try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
    print(String(data: jsonData, encoding: .utf8)!)
}

do {
    try runHarness()
} catch {
    fputs("HARNESS ERROR: \(error)\n", stderr)
    exit(1)
}
"""


def _can_compile_swift_metal() -> bool:
    return sys.platform == "darwin" and platform.system() == "Darwin" and shutil.which("swiftc") is not None


def _compile_harness(tmp_dir: Path) -> Path:
    harness_swift = tmp_dir / "main.swift"
    harness_swift.write_text(SWIFT_HARNESS_SOURCE, encoding="utf-8")
    out_bin = tmp_dir / "world_stream_harness"
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    cmd = [
        "swiftc",
        "-O",
        "-target", "arm64-apple-macosx14.0",
        "-sdk", sdk,
        "-framework", "Foundation",
        "-framework", "Metal",
        str(METAL_RT_DIR / "RTWorldLoader.swift"),
        str(METAL_RT_DIR / "RTWorldStreamer.swift"),
        str(harness_swift),
        "-o", str(out_bin),
    ]
    subprocess.run(cmd, check=True)
    return out_bin


class TestWorldStreamingMetal(unittest.TestCase):
    _harness_dir: tempfile.TemporaryDirectory[str] | None = None
    _harness_bin: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if _can_compile_swift_metal():
            cls._harness_dir = tempfile.TemporaryDirectory(prefix="rkpt_stream_test_")
            cls._harness_bin = _compile_harness(Path(cls._harness_dir.name))

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._harness_dir is not None:
            cls._harness_dir.cleanup()

    def test_swift_typecheck_loader_and_streamer(self) -> None:
        if not _can_compile_swift_metal():
            self.skipTest("macOS swiftc required for Swift Metal typecheck")
        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        res = subprocess.run(
            [
                "swiftc",
                "-typecheck",
                "-target", "arm64-apple-macosx14.0",
                "-sdk", sdk,
                "-framework", "Foundation",
                "-framework", "Metal",
                str(METAL_RT_DIR / "RTWorldLoader.swift"),
                str(METAL_RT_DIR / "RTWorldStreamer.swift"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"swiftc errors:\n{res.stderr}")
        self.assertEqual(res.stderr.strip(), "", f"Unexpected swiftc warnings:\n{res.stderr}")

    def test_synthetic_v2_bc7_and_v1_streaming_on_metal_gpu(self) -> None:
        if self._harness_bin is None:
            self.skipTest("macOS Metal GPU + swiftc required")

        solid_road = erw.DecodedWorldSolid(
            name="TRN_SYNTH_ROAD",
            name_hash=0x01020304,
            texture_hashes=[0x11223344],
            positions_metal=np.array([[0, 0, 0], [20, 0, 0], [0, 0, -20]], dtype=np.float32),
            normals_metal=np.array([[0, 1, 0], [0, 1, 0], [0, 1, 0]], dtype=np.float32),
            uvs_metal=np.array([[0, 0], [1, 0], [0, 1]], dtype=np.float32),
            vertex_ao=np.array([255, 255, 255], dtype=np.uint8),
            vertex_tex_slot=np.array([0, 0, 0], dtype=np.int32),
            indices=np.array([0, 1, 2], dtype=np.uint32),
            aabb_min_metal=(0.0, 0.0, -20.0),
            aabb_max_metal=(20.0, 0.0, 0.0),
        )
        mat = erw.classify_world_material("TRNS_ALL_LA_PAVEMENTA_Y", "TRN_SYNTH_ROAD", False, 0, 0x11223344)
        mips, bc7_len = erw.expected_bc7_mip_chain_bytes(16, 8, mipmaps=True)
        tex_v2 = erw.DecodedRkptTexture(
            "TRNS_ALL_LA_PAVEMENTA_Y", 0x11223344, 16, 8,
            bytes([128] * (16 * 8 * 4)), erw.TEX_FLAG_PS2_0X80_NORMALIZED, "synth",
            pixel_format=erw.PIXEL_FORMAT_BC7_SRGB, mip_count=mips,
            encoded_bytes=bytes([0x40] * bc7_len),
        )
        tex_v1 = erw.DecodedRkptTexture(
            "TRNS_ALL_LA_PAVEMENTA_Y", 0x11223344, 16, 8,
            bytes([128] * (16 * 8 * 4)), erw.TEX_FLAG_PS2_0X80_NORMALIZED, "synth",
        )
        xf0 = erw.ps2_instance_to_metal_4x3([8192, 0, 0, 0, 8192, 0, 0, 0, 8192], (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        xf_far = erw.ps2_instance_to_metal_4x3([8192, 0, 0, 0, 8192, 0, 0, 0, 8192], (800.0, 0.0, 0.0), (0.0, 0.0, 0.0))

        with tempfile.TemporaryDirectory() as td:
            v2_path = Path(td) / "synth_v2.rkpt"
            v1_path = Path(td) / "synth_v1.rkpt"
            erw.write_rkpt_v2(
                out_path=v2_path,
                sections_meta=[
                    ("A47", 147, 0, 1, (10.0, 0.0, -10.0), 30.0, (0.0, 0.0, -20.0), (20.0, 1.0, 0.0), 0),
                    ("A80", 180, 1, 1, (810.0, 0.0, -10.0), 100.0, (800.0, 0.0, -20.0), (820.0, 1.0, 0.0), erw.SEC_FLAG_PANORAMA),
                ],
                meshes=[solid_road],
                mesh_vert_mats=[np.array([0, 0, 0], dtype=np.int32)],
                mesh_flags=[erw.MESH_FLAG_DRIVABLE_ROAD],
                instances=[(0, 0, 0, xf0), (0, 1, 0, xf_far)],
                materials=[mat],
                textures=[tex_v2],
                world_bounds_min=(0.0, 0.0, -20.0),
                world_bounds_max=(820.0, 1.0, 0.0),
                spawn_pos=(5.0, 0.0, -5.0),
                spawn_yaw_rad=0.0,
                ps2_origin=(0.0, 0.0, 0.0),
                spatial_section_count=1,
                panorama_section_count=1,
                road_nodes=[
                    erw.RkptRoadNode((0.0, 0.0, 0.0), 0, 0, 1, (0, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF)),
                    erw.RkptRoadNode((20.0, 0.0, 0.0), 1, 0, 1, (0, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF)),
                ],
                road_segments=[erw.RkptRoadSegment(0, 1, 9, 1, 0xFFFF, 20.0, 5.5)],
                world_lights=[erw.RTWorldLight((8.0, 7.0, -8.0), 24.0, (19.0, 12.0, 5.0), 0.12)],
                landmarks=[erw.RkptLandmark("Rosewood Blvd", (5.0, 0.0, -5.0), 0.0, 0)],
            )
            erw.write_rkpt_v1(
                out_path=v1_path,
                sections_meta=[("A47", 147, 0, 1, (10.0, 0.0, -10.0), 30.0, (0.0, 0.0, -20.0), (20.0, 1.0, 0.0))],
                meshes=[solid_road],
                mesh_vert_mats=[np.array([0, 0, 0], dtype=np.int32)],
                mesh_flags=[erw.MESH_FLAG_DRIVABLE_ROAD],
                instances=[(0, 0, 0, xf0)],
                materials=[mat],
                textures=[tex_v1],
                world_bounds_min=(0.0, 0.0, -20.0),
                world_bounds_max=(20.0, 1.0, 0.0),
                spawn_pos=(5.0, 0.0, -5.0),
                spawn_yaw_rad=0.0,
                ps2_origin=(0.0, 0.0, 0.0),
            )
            proc = subprocess.run([str(self._harness_bin), str(v2_path), str(v1_path)], capture_output=True, text=True, check=False)
            if proc.returncode == 3:
                self.skipTest(proc.stderr.strip() or "Metal hardware ray tracing unavailable")
            self.assertEqual(proc.returncode, 0, f"Harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}")
            data = json.loads(proc.stdout)
            self.assertEqual(data["version"], 2)
            self.assertEqual(data["bc7TextureCount"], 1)
            self.assertEqual(data["mipmappedBC7Count"], 1)
            self.assertEqual(data["roadNodeCount"], 2)
            self.assertEqual(data["roadSegmentCount"], 1)
            self.assertEqual(data["worldLightCount"], 1)
            self.assertEqual(data["landmarks"][0]["lightsInCell"], 1)
            self.assertEqual(data["landmarks"][0]["activeInstances"], 2)  # 1 spatial + 1 distant panorama
            self.assertEqual(data["landmarks"][0]["panoramaSections"], 1)
            self.assertEqual(data["v1Compat"]["version"], 1)
            self.assertEqual(data["v1Compat"]["instances"], 1)

    def test_live_rockport_full_v2_streaming_and_vram_gate(self) -> None:
        if self._harness_bin is None:
            self.skipTest("macOS Metal GPU + swiftc required")
        full_rkpt = REPO_ROOT / "build" / "rockport_world" / "rockport_full.rkpt"
        rw_rkpt = REPO_ROOT / "build" / "rockport_world" / "rosewood.rkpt"
        if not full_rkpt.is_file() or os.environ.get("NFSMW_ISO", "").strip() == "/nonexistent":
            self.skipTest("build/rockport_world/rockport_full.rkpt not present (or offline CI mode)")

        cmd = [str(self._harness_bin), str(full_rkpt)]
        if rw_rkpt.is_file():
            cmd.append(str(rw_rkpt))
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 3:
            self.skipTest(proc.stderr.strip() or "Metal hardware ray tracing unavailable")
        self.assertEqual(proc.returncode, 0, f"Harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}")
        data = json.loads(proc.stdout)

        # Hard Hour 2 Gates
        self.assertEqual(data["version"], 2)
        self.assertLess(data["loadSeconds"], 3.0, f"Load time {data['loadSeconds']}s >= 3.0s")
        self.assertLessEqual(data["vramMB"], 550.0, f"World VRAM {data['vramMB']} MB > 550 MB")
        self.assertEqual(data["sectionCount"], 718)
        self.assertEqual(data["spatialSectionCount"], 638)
        self.assertEqual(data["panoramaSectionCount"], 80)
        self.assertEqual(data["bc7TextureCount"], 1497)
        self.assertGreater(data["mipmappedBC7Count"], 1450)
        self.assertTrue(data["hasResidentBLASes"])
        self.assertEqual(data["roadNodeCount"], 4385)
        self.assertEqual(data["roadSegmentCount"], 6538)
        self.assertGreaterEqual(data["worldLightCount"], 4000)
        self.assertEqual(data["landmarkCount"], 6)
        self.assertLessEqual(data["maxActiveInstances"], 8192)
        self.assertLessEqual(data["maxSpatialInstances"], 6000)
        self.assertLessEqual(data["denseMaxInstances"], 8192)
        self.assertLessEqual(data["denseMaxSpatial"], 6000)
        self.assertEqual(data["aliasCamden"], "Camden Beach")
        self.assertEqual(data["aliasHwy99"], "Highway 99")

        # Verify real streetlamps populate lightGrid at Rosewood, Downtown, and Camden Beach
        by_name = {row["name"]: row for row in data["landmarks"]}
        for required in ("Rosewood Blvd", "Downtown East Park", "Camden Beach"):
            self.assertIn(required, by_name)
            self.assertGreater(by_name[required]["lightsInCell"], 0, f"No lights in lightGrid at {required}")
            self.assertGreater(by_name[required]["activeInstances"], 500)
            self.assertGreater(by_name[required]["panoramaSections"], 50)
            self.assertFalse(by_name[required]["nudgeDirty"], f"2m nudge caused unnecessary rebuild at {required}")

        if rw_rkpt.is_file():
            self.assertEqual(data["v1Compat"]["version"], 1)
            self.assertEqual(data["v1Compat"]["sections"], 25)


if __name__ == "__main__":
    unittest.main()
