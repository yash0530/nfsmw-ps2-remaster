#!/usr/bin/env python3
"""Unit and live Metal GPU tests for v1.4.0 EA PVS (0x80034150) vistas & 6 all-district botanical foliage upgrades."""

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

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
METAL_RT_DIR = REPO_ROOT / "native_metal_rt"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import export_rockport_world as erw  # noqa: E402


PVS_SWIFT_HARNESS_SOURCE = r"""
import Foundation
import Metal
import simd

func runPvsHarness() throws {
    let args = CommandLine.arguments
    guard args.count >= 2 else {
        fputs("Usage: pvs_harness <rkpt_path> [no_pvs_rkpt_path]\n", stderr)
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

    let streamer = try RTWorldStreamer(world: world, device: device)
    let defaultPVSEnabled = streamer.pvsEnabled

    var lmRows: [[String: Any]] = []
    var maxSlotsOn = 0
    var maxSlotsOff = 0
    var maxSpatialOn = 0
    var maxSpatialOff = 0
    var maxVistaOn: Float = 0

    for lm in streamer.landmarks {
        streamer.pvsEnabled = true
        let resOn = streamer.update(playerPos: lm.spawnPos, forceRebuild: true)
        let pvsSecsOn = streamer.activePVSSectionCount
        let vistaOn = streamer.maxActiveVistaDistanceM
        let zoneIdxOn = streamer.activePVSZoneIndex
        let spOn = streamer.activeSpatialSectionCount
        let spInstOn = resOn.activeInstances.filter {
            let s = Int($0.sectionIndex)
            return s < world.sections.count && !world.sections[s].isPanorama
        }.count
        maxSlotsOn = max(maxSlotsOn, resOn.activeInstances.count)
        maxSpatialOn = max(maxSpatialOn, spInstOn)
        maxVistaOn = max(maxVistaOn, vistaOn)

        // Toggle PVS OFF via togglePVS() (J key API)
        let toggledOffState = streamer.togglePVS()
        let resOff = streamer.update(playerPos: lm.spawnPos, forceRebuild: false)
        let pvsSecsOff = streamer.activePVSSectionCount
        let vistaOff = streamer.maxActiveVistaDistanceM
        let spInstOff = resOff.activeInstances.filter {
            let s = Int($0.sectionIndex)
            return s < world.sections.count && !world.sections[s].isPanorama
        }.count
        maxSlotsOff = max(maxSlotsOff, resOff.activeInstances.count)
        maxSpatialOff = max(maxSpatialOff, spInstOff)

        // Toggle PVS back ON via togglePVS()
        let toggledOnState = streamer.togglePVS()
        let resReOn = streamer.update(playerPos: lm.spawnPos, forceRebuild: false)

        lmRows.append([
            "name": lm.name,
            "zoneIdx": zoneIdxOn,
            "activeInstancesOn": resOn.activeInstances.count,
            "spatialInstancesOn": spInstOn,
            "spatialSectionsOn": spOn,
            "pvsSectionsOn": pvsSecsOn,
            "vistaOnM": round(Double(vistaOn) * 10.0) / 10.0,
            "toggledOffState": toggledOffState,
            "toggleOffDirty": resOff.dirty,
            "activeInstancesOff": resOff.activeInstances.count,
            "spatialInstancesOff": spInstOff,
            "pvsSectionsOff": pvsSecsOff,
            "vistaOffM": round(Double(vistaOff) * 10.0) / 10.0,
            "toggledOnState": toggledOnState,
            "toggleOnDirty": resReOn.dirty,
            "vistaReOnM": round(Double(streamer.maxActiveVistaDistanceM) * 10.0) / 10.0,
        ])
    }

    // Stress-test every dense interchange section (>200 insts) with PVS ON & OFF
    var denseMaxSlotsOn = 0
    var denseMaxSpatialOn = 0
    var denseMaxSlotsOff = 0
    var denseMaxSpatialOff = 0
    streamer.pvsEnabled = true
    for sec in world.sections where !sec.isPanorama && sec.instanceCount > 200 {
        let r = streamer.update(playerPos: sec.center, forceRebuild: false)
        denseMaxSlotsOn = max(denseMaxSlotsOn, r.activeInstances.count)
        let sp = r.activeInstances.filter {
            let s = Int($0.sectionIndex)
            return s < world.sections.count && !world.sections[s].isPanorama
        }.count
        denseMaxSpatialOn = max(denseMaxSpatialOn, sp)
    }
    streamer.pvsEnabled = false
    for sec in world.sections where !sec.isPanorama && sec.instanceCount > 200 {
        let r = streamer.update(playerPos: sec.center, forceRebuild: false)
        denseMaxSlotsOff = max(denseMaxSlotsOff, r.activeInstances.count)
        let sp = r.activeInstances.filter {
            let s = Int($0.sectionIndex)
            return s < world.sections.count && !world.sections[s].isPanorama
        }.count
        denseMaxSpatialOff = max(denseMaxSpatialOff, sp)
    }

    // Build a real GPU TLAS with PVS ON at Highway 99 (or first landmark)
    streamer.pvsEnabled = true
    let targetLm = streamer.landmark(namedOrIndex: "hwy99") ?? streamer.landmarks[0]
    let activeRes = streamer.update(playerPos: targetLm.spawnPos, forceRebuild: true)
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

    var noPvsCompat: [String: Any] = [:]
    if args.count >= 3 {
        let w2 = try RTWorldLoader.load(url: URL(fileURLWithPath: args[2]), device: device, commandQueue: queue)
        let s2 = try RTWorldStreamer(world: w2, device: device)
        let r2 = s2.update(playerPos: w2.spawnPos, forceRebuild: true)
        noPvsCompat = [
            "hasPVS": w2.hasPVS,
            "pvsZoneCount": w2.pvsZones.count,
            "activeInstances": r2.activeInstances.count,
            "pvsSections": s2.activePVSSectionCount,
        ]
    }

    let out: [String: Any] = [
        "version": world.header.version,
        "hasPVS": world.hasPVS,
        "defaultPVSEnabled": defaultPVSEnabled,
        "pvsZoneCount": world.pvsZones.count,
        "pvsVertCount": world.pvsVerts.count,
        "pvsEntryCount": world.pvsEntries.count,
        "loadSeconds": round(loadSec * 1000.0) / 1000.0,
        "tlasBuildMs": round(tlasBuildMs * 1000.0) / 1000.0,
        "vramMB": round(Double(streamer.estimatedWorldVRAMBytes) / 1048576.0 * 100.0) / 100.0,
        "maxSlotsOn": maxSlotsOn,
        "maxSlotsOff": maxSlotsOff,
        "maxSpatialOn": maxSpatialOn,
        "maxSpatialOff": maxSpatialOff,
        "maxVistaOnM": round(Double(maxVistaOn) * 10.0) / 10.0,
        "denseMaxSlotsOn": denseMaxSlotsOn,
        "denseMaxSpatialOn": denseMaxSpatialOn,
        "denseMaxSlotsOff": denseMaxSlotsOff,
        "denseMaxSpatialOff": denseMaxSpatialOff,
        "landmarks": lmRows,
        "noPvsCompat": noPvsCompat,
    ]
    let jsonData = try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
    print(String(data: jsonData, encoding: .utf8)!)
}

do {
    try runPvsHarness()
} catch {
    fputs("PVS HARNESS ERROR: \(error)\n", stderr)
    exit(1)
}
"""


def _can_compile_swift_metal() -> bool:
    return sys.platform == "darwin" and platform.system() == "Darwin" and shutil.which("swiftc") is not None


def _compile_pvs_harness(tmp_dir: Path) -> Path:
    harness_swift = tmp_dir / "main.swift"
    harness_swift.write_text(PVS_SWIFT_HARNESS_SOURCE, encoding="utf-8")
    out_bin = tmp_dir / "pvs_ultra_harness"
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    cmd = [
        "swiftc",
        "-O",
        "-warnings-as-errors",
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


class TestPvsUltraWorldOffline(unittest.TestCase):
    _harness_dir: tempfile.TemporaryDirectory[str] | None = None
    _harness_bin: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if _can_compile_swift_metal():
            cls._harness_dir = tempfile.TemporaryDirectory(prefix="rkpt_pvs_test_")
            cls._harness_bin = _compile_pvs_harness(Path(cls._harness_dir.name))

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._harness_dir is not None:
            cls._harness_dir.cleanup()

    def test_pvs_binary_abi_and_roundtrip(self) -> None:
        self.assertEqual(erw.RKPT_HDR_FLAG_HAS_PVS, 1 << 3)
        self.assertEqual(erw.PVS_ZONE_FMT, "<4f4I")
        self.assertEqual(erw.PVS_ZONE_SIZE, 32)
        self.assertEqual(erw.PVS_VERT_FMT, "<2f")
        self.assertEqual(erw.PVS_VERT_SIZE, 8)

        solid = erw.DecodedWorldSolid(
            name="TRN_SYNTH_PVS",
            name_hash=0x10203040,
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
        mat = erw.classify_world_material("TRNS_ALL_LA_PAVEMENTA_Y", "TRN_SYNTH_PVS", False, 0, 0x11223344)
        mips, bc7_len = erw.expected_bc7_mip_chain_bytes(8, 8, mipmaps=True)
        tex = erw.DecodedRkptTexture(
            "TRNS_ALL_LA_PAVEMENTA_Y", 0x11223344, 8, 8,
            bytes([128] * (8 * 8 * 4)), erw.TEX_FLAG_PS2_0X80_NORMALIZED, "synth",
            pixel_format=erw.PIXEL_FORMAT_BC7_SRGB, mip_count=mips, encoded_bytes=bytes([0x40] * bc7_len),
        )
        xf0 = erw.ps2_instance_to_metal_4x3([8192, 0, 0, 0, 8192, 0, 0, 0, 8192], (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        xf1 = erw.ps2_instance_to_metal_4x3([8192, 0, 0, 0, 8192, 0, 0, 0, 8192], (2600.0, 0.0, 0.0), (0.0, 0.0, 0.0))

        pvs_verts = [(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)]
        pvs_entries = [0, 1]
        pvs_zones = [erw.RkptPvsZone(-50.0, -50.0, 50.0, 50.0, 0, 4, 0, 2)]

        with tempfile.TemporaryDirectory() as td:
            pvs_rkpt = Path(td) / "synth_pvs.rkpt"
            no_pvs_rkpt = Path(td) / "synth_nopvs.rkpt"
            common_kwargs = dict(
                sections_meta=[
                    ("A41", 141, 0, 1, (10.0, 0.0, -10.0), 40.0, (0.0, 0.0, -20.0), (20.0, 1.0, 0.0), 0),
                    ("B41", 241, 1, 1, (2610.0, 0.0, -10.0), 40.0, (2600.0, 0.0, -20.0), (2620.0, 1.0, 0.0), 0),
                ],
                meshes=[solid],
                mesh_vert_mats=[np.array([0, 0, 0], dtype=np.int32)],
                mesh_flags=[erw.MESH_FLAG_DRIVABLE_ROAD],
                instances=[(0, 0, 0, xf0), (0, 1, 0, xf1)],
                materials=[mat],
                textures=[tex],
                world_bounds_min=(0.0, 0.0, -20.0),
                world_bounds_max=(2620.0, 1.0, 0.0),
                spawn_pos=(5.0, 0.0, -5.0),
                spawn_yaw_rad=0.0,
                ps2_origin=(0.0, 0.0, 0.0),
                spatial_section_count=2,
                panorama_section_count=0,
                landmarks=[erw.RkptLandmark("Synthetic Vista", (5.0, 0.0, -5.0), 0.0, 0)],
            )
            erw.write_rkpt_v2(
                out_path=pvs_rkpt,
                pvs_zones=pvs_zones,
                pvs_verts=pvs_verts,
                pvs_entries=pvs_entries,
                **common_kwargs,
            )
            erw.write_rkpt_v2(out_path=no_pvs_rkpt, **common_kwargs)

            hdr = erw.read_rkpt_header(pvs_rkpt)
            self.assertEqual(hdr["version"], 2)
            self.assertTrue(hdr["hasPVS"])
            self.assertTrue(hdr["flags"] & erw.RKPT_HDR_FLAG_HAS_PVS)
            self.assertEqual(hdr["pvsZoneCount"], 1)
            self.assertEqual(hdr["pvsVertCount"], 4)
            self.assertEqual(hdr["pvsEntryCount"], 2)
            self.assertEqual(hdr["pvsZonesOffset"] & 15, 0)
            self.assertEqual(hdr["pvsVertsOffset"] & 15, 0)
            self.assertEqual(hdr["pvsEntriesOffset"] & 15, 0)

            raw = pvs_rkpt.read_bytes()
            z0 = struct.unpack_from(erw.PVS_ZONE_FMT, raw, hdr["pvsZonesOffset"])
            self.assertEqual(z0[4:], (0, 4, 0, 2))
            self.assertAlmostEqual(z0[0], -50.0, places=4)
            self.assertAlmostEqual(z0[3], 50.0, places=4)
            unpacked_entries = struct.unpack_from("<2H", raw, hdr["pvsEntriesOffset"])
            self.assertEqual(unpacked_entries, (0, 1))

            hdr_no = erw.read_rkpt_header(no_pvs_rkpt)
            self.assertFalse(hdr_no["hasPVS"])
            self.assertEqual(hdr_no["pvsZoneCount"], 0)
            self.assertEqual(hdr_no["pvsZonesOffset"], 0)

            # Real Metal GPU test of synthetic 2.6km PVS vista + togglePVS() ON/OFF + backward-compat no-PVS load
            if self._harness_bin is not None:
                proc = subprocess.run(
                    [str(self._harness_bin), str(pvs_rkpt), str(no_pvs_rkpt)],
                    capture_output=True, text=True, check=False,
                )
                if proc.returncode == 3:
                    return
                self.assertEqual(proc.returncode, 0, f"PVS harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}")
                data = json.loads(proc.stdout)
                self.assertTrue(data["hasPVS"])
                self.assertTrue(data["defaultPVSEnabled"])
                self.assertEqual(data["pvsZoneCount"], 1)
                self.assertEqual(data["pvsVertCount"], 4)
                self.assertEqual(data["pvsEntryCount"], 2)
                lm0 = data["landmarks"][0]
                self.assertEqual(lm0["activeInstancesOn"], 2)
                self.assertEqual(lm0["pvsSectionsOn"], 2)
                self.assertGreaterEqual(lm0["vistaOnM"], 2500.0)
                self.assertFalse(lm0["toggledOffState"])
                self.assertTrue(lm0["toggleOffDirty"])
                self.assertEqual(lm0["activeInstancesOff"], 1)
                self.assertEqual(lm0["pvsSectionsOff"], 0)
                self.assertLess(lm0["vistaOffM"], 100.0)
                self.assertTrue(lm0["toggledOnState"])
                self.assertTrue(lm0["toggleOnDirty"])
                self.assertGreaterEqual(lm0["vistaReOnM"], 2500.0)
                self.assertFalse(data["noPvsCompat"]["hasPVS"])
                self.assertEqual(data["noPvsCompat"]["pvsZoneCount"], 0)

    def test_all_6_botanical_archetypes_geometry_and_edge_dilated_textures(self) -> None:
        sample_solids = [
            ("XT_JPNMAPLE_1B_JG_00", "autumn_maple", (-8.5, 0.0, -8.5), (7.7, 22.3, 8.5), [0xAF05FD10, erw.BOTANICAL_TEX_MAPLE_CRIMSON]),
            ("XT_CT_FALLTREES_1B_JG_00", "autumn_maple", (-12.7, 0.0, -11.2), (15.7, 29.9, 10.9), [0x89BE1EE6, erw.BOTANICAL_TEX_FALL_AMBER]),
            ("XT_CT_FALLTREEFILLERB_1B_00", "autumn_maple", (-10.5, -0.6, -8.2), (8.9, 16.7, 8.0), [erw.BOTANICAL_TEX_FALL_AMBER, erw.BOTANICAL_TEX_POPLAR_GOLD]),
            ("XT_CT_POPLARD_1B_JG_00", "golden_poplar", (-7.1, 0.0, -7.3), (7.0, 21.4, 5.9), [0x6714390A, erw.BOTANICAL_TEX_POPLAR_GOLD]),
            ("XT_CEDARM_1B_JG_00", "pnw_cedar", (-9.0, 0.0, -9.9), (8.0, 49.5, 8.8), [erw.BOTANICAL_TEX_CEDAR_BOUGH, 0x0EE5A928]),
            ("XT_CT_BUSH_1B_JG_00", "roadside_bush", (-6.2, -0.9, -5.9), (6.3, 10.7, 5.9), [erw.BOTANICAL_TEX_ROADSIDE_BUSH]),
            ("XT_BUSH_FOREST_1A_JG_00", "roadside_bush", (-6.1, -1.1, -4.5), (5.9, 9.2, 6.7), [erw.BOTANICAL_TEX_FOREST_BUSH]),
            ("XW_HEDGESTADIUM_1B_00", "stadium_hedge", (-0.01, 0.0, -0.92), (10.92, 2.70, 0.74), [erw.BOTANICAL_TEX_STADIUM_HEDGE, 0xC2975107]),
            ("XT_BEACHGRASS_1B_00", "coastal_beachgrass", (-2.95, -0.03, -0.36), (2.95, 1.76, 1.48), [erw.BOTANICAL_TEX_BEACHGRASS]),
        ]
        seen_archetypes = set()
        for nm, exp_arch, bmin, bmax, ths in sample_solids:
            got_arch = erw.match_botanical_foliage_archetype(nm)
            self.assertEqual(got_arch, exp_arch, nm)
            seen_archetypes.add(got_arch)
            dummy = erw.DecodedWorldSolid(
                name=nm,
                name_hash=0x55AA1234,
                texture_hashes=ths,
                positions_metal=np.array([bmin, bmax, [0.0, bmax[1] * 0.5, 0.0]], dtype=np.float32),
                normals_metal=np.array([[0, 1, 0]] * 3, dtype=np.float32),
                uvs_metal=np.zeros((3, 2), dtype=np.float32),
                vertex_ao=np.full(3, 255, dtype=np.uint8),
                vertex_tex_slot=np.zeros(3, dtype=np.int32),
                indices=np.array([0, 1, 2], dtype=np.uint32),
                aabb_min_metal=bmin,
                aabb_max_metal=bmax,
            )
            built = erw.build_botanical_foliage_solid(dummy, got_arch)
            n_tris = len(built.indices) // 3
            self.assertGreaterEqual(n_tris, 100, f"{nm} too low-poly ({n_tris} tris)")
            self.assertLessEqual(n_tris, 650, f"{nm} exceeded budget ({n_tris} tris)")
            self.assertTrue(np.all(np.isfinite(built.positions_metal)))
            self.assertTrue(np.all(np.isfinite(built.normals_metal)))
            self.assertLess(int(built.indices.max()), len(built.positions_metal))
            # Strictly bounded inside original solid AABB so zero road/building encroachment occurs
            for axis in range(3):
                self.assertGreaterEqual(built.aabb_min_metal[axis], bmin[axis] - 1e-3, f"{nm} min axis {axis}")
                self.assertLessEqual(built.aabb_max_metal[axis], bmax[axis] + 1e-3, f"{nm} max axis {axis}")
        self.assertEqual(len(seen_archetypes), 6)

        # Verify all 8 procedural botanical textures (128px fast offline test + check alpha cutout & edge dilation)
        all_hashes = (
            erw.BOTANICAL_TEX_MAPLE_CRIMSON,
            erw.BOTANICAL_TEX_FALL_AMBER,
            erw.BOTANICAL_TEX_POPLAR_GOLD,
            erw.BOTANICAL_TEX_CEDAR_BOUGH,
            erw.BOTANICAL_TEX_ROADSIDE_BUSH,
            erw.BOTANICAL_TEX_FOREST_BUSH,
            erw.BOTANICAL_TEX_STADIUM_HEDGE,
            erw.BOTANICAL_TEX_BEACHGRASS,
        )
        for th in all_hashes:
            res = erw.generate_botanical_foliage_texture_rgba(th, size=128)
            self.assertIsNotNone(res)
            tname, _src, rgba = res  # type: ignore[misc]
            self.assertEqual(rgba.shape, (128, 128, 4))
            self.assertTrue(erw._is_foliage_texture_name(tname), f"{tname} must classify as SURFACE_FOLIAGE_CUTOUT")
            cutout_frac = float(np.mean(rgba[:, :, 3] < 128))
            self.assertGreater(cutout_frac, 0.05, f"{tname} has no cutout alpha ({cutout_frac:.3f})")
            self.assertLess(cutout_frac, 0.78, f"{tname} overly sparse ({cutout_frac:.3f})")
            # Verify edge dilation populated transparent pixels with non-white, non-black foliage RGB (no pale fringes!)
            trans = rgba[:, :, 3] < 32
            if np.any(trans):
                t_rgb = rgba[trans][:, :3].astype(np.float32)
                mean_luma = float(t_rgb.mean())
                self.assertGreater(mean_luma, 10.0, f"{tname} transparent RGB looks undilated black")
                self.assertLess(mean_luma, 185.0, f"{tname} transparent RGB looks washed-out white fringe")


class TestPvsUltraWorldLive(unittest.TestCase):
    def _live_rkpt_and_manifest(self) -> tuple[Path, Path]:
        if os.environ.get("NFSMW_ISO", "").strip() == "/nonexistent":
            self.skipTest("Offline CI mode (NFSMW_ISO=/nonexistent); skipping live rockport_full PVS check")
        rkpt_path = REPO_ROOT / "build" / "rockport_world" / "rockport_full.rkpt"
        manifest_path = REPO_ROOT / "build" / "rockport_world" / "rockport_full_manifest.json"
        if not rkpt_path.is_file() or not manifest_path.is_file():
            self.skipTest("build/rockport_world/rockport_full.rkpt not present")
        return rkpt_path, manifest_path

    def test_live_pvs_binary_vista_reach_and_botanical_coverage(self) -> None:
        rkpt_path, manifest_path = self._live_rkpt_and_manifest()
        hdr = erw.read_rkpt_header(rkpt_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # File size & header gates
        self.assertEqual(hdr["version"], 2)
        self.assertTrue(hdr["hasPVS"])
        self.assertTrue(hdr["flags"] & erw.RKPT_HDR_FLAG_HAS_PVS)
        self.assertLessEqual(hdr["fileSize"], 245 * 1024 * 1024)
        self.assertGreaterEqual(hdr["pvsZoneCount"], 400)
        self.assertGreaterEqual(hdr["pvsVertCount"], 3000)
        self.assertGreaterEqual(hdr["pvsEntryCount"], 5000)

        pvs_m = manifest["pvs"]
        self.assertGreaterEqual(pvs_m["pvs_polygon_count"], 460)
        self.assertGreaterEqual(pvs_m["pvs_matched_vis_count"], 430)
        self.assertEqual(pvs_m["pvs_zone_count"], hdr["pvsZoneCount"])

        # Independently unpack PVS binary tables from .rkpt and verify bounds + vista reach
        raw = rkpt_path.read_bytes()
        sp_cnt = hdr["spatialSectionCount"]
        sec_centers = []
        for i in range(sp_cnt):
            o = hdr["sectionsOffset"] + i * erw.SECTION_SIZE
            cx, cy, cz = struct.unpack_from("<3f", raw, o + 20)
            sec_centers.append((cx, cz))

        verts = [
            struct.unpack_from(erw.PVS_VERT_FMT, raw, hdr["pvsVertsOffset"] + i * erw.PVS_VERT_SIZE)
            for i in range(hdr["pvsVertCount"])
        ]
        entries = struct.unpack_from(f"<{hdr['pvsEntryCount']}H", raw, hdr["pvsEntriesOffset"])

        zone_vistas = []
        for zi in range(hdr["pvsZoneCount"]):
            min_x, min_z, max_x, max_z, fv, vc, fe, ec = struct.unpack_from(
                erw.PVS_ZONE_FMT, raw, hdr["pvsZonesOffset"] + zi * erw.PVS_ZONE_SIZE
            )
            self.assertLessEqual(min_x, max_x)
            self.assertLessEqual(min_z, max_z)
            self.assertGreaterEqual(vc, 3)
            self.assertLessEqual(fv + vc, hdr["pvsVertCount"])
            self.assertLessEqual(fe + ec, hdr["pvsEntryCount"])
            cx_z = 0.5 * (min_x + max_x)
            cz_z = 0.5 * (min_z + max_z)
            if ec > 0:
                md = 0.0
                for s_idx in entries[fe : fe + ec]:
                    self.assertLess(s_idx, sp_cnt)
                    sx, sz = sec_centers[s_idx]
                    md = max(md, math.hypot(sx - cx_z, sz - cz_z))
                zone_vistas.append(md)

        self.assertGreaterEqual(len(zone_vistas), 400)
        med_vista = float(np.median(zone_vistas))
        p90_vista = float(np.percentile(zone_vistas, 90))
        self.assertGreaterEqual(med_vista, 1500.0, f"Median PVS vista {med_vista:.1f}m < 1500m")
        self.assertGreaterEqual(p90_vista, 3000.0, f"p90 PVS vista {p90_vista:.1f}m < 3000m")

        # Verify all 6 botanical upgrades active across >= 7,500 instances with 512x512 BC7 procedural textures
        by_arch = manifest["botanical_foliage_instances_by_archetype"]
        for req_arch in (
            "autumn_maple", "golden_poplar", "pnw_cedar",
            "roadside_bush", "stadium_hedge", "coastal_beachgrass",
        ):
            self.assertIn(req_arch, by_arch, f"Missing botanical archetype {req_arch}")
            self.assertGreater(by_arch[req_arch], 250, f"Too few instances for {req_arch}")
        self.assertGreaterEqual(manifest["botanical_foliage_instance_count"], 7500)
        self.assertEqual(len(manifest["botanical_foliage_textures_replaced"]), 8)

        # Verify all 8 procedural botanical texture records in binary .rkpt are 512x512 BC7 with PROCEDURAL_BOTANICAL flag
        expected_hashes = {
            erw.BOTANICAL_TEX_MAPLE_CRIMSON,
            erw.BOTANICAL_TEX_FALL_AMBER,
            erw.BOTANICAL_TEX_POPLAR_GOLD,
            erw.BOTANICAL_TEX_CEDAR_BOUGH,
            erw.BOTANICAL_TEX_ROADSIDE_BUSH,
            erw.BOTANICAL_TEX_FOREST_BUSH,
            erw.BOTANICAL_TEX_STADIUM_HEDGE,
            erw.BOTANICAL_TEX_BEACHGRASS,
        }
        found_bot_tex = 0
        for i in range(hdr["textureCount"]):
            th, tw, th_, pfmt, _doff, _dsz, tfl = struct.unpack_from(
                erw.TEXTURE_FMT, raw, hdr["texturesOffset"] + i * erw.TEXTURE_SIZE
            )
            if th in expected_hashes:
                found_bot_tex += 1
                self.assertEqual((tw, th_), (512, 512))
                self.assertEqual(pfmt, erw.PIXEL_FORMAT_BC7_SRGB)
                self.assertTrue(tfl & erw.TEX_FLAG_PROCEDURAL_BOTANICAL)
                self.assertTrue(tfl & erw.TEX_FLAG_HAS_CUTOUT_ALPHA)
        self.assertEqual(found_bot_tex, 8)

    def test_live_metal_gpu_pvs_vistas_toggle_and_vram_gate(self) -> None:
        if not _can_compile_swift_metal():
            self.skipTest("macOS Metal GPU + swiftc required")
        rkpt_path, _ = self._live_rkpt_and_manifest()
        with tempfile.TemporaryDirectory(prefix="rkpt_pvs_live_") as td:
            harness_bin = _compile_pvs_harness(Path(td))
            proc = subprocess.run([str(harness_bin), str(rkpt_path)], capture_output=True, text=True, check=False)
            if proc.returncode == 3:
                self.skipTest(proc.stderr.strip() or "Metal hardware ray tracing unavailable")
            self.assertEqual(proc.returncode, 0, f"PVS harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}")
        data = json.loads(proc.stdout)

        self.assertEqual(data["version"], 2)
        self.assertTrue(data["hasPVS"])
        self.assertTrue(data["defaultPVSEnabled"])
        self.assertGreaterEqual(data["pvsZoneCount"], 400)
        self.assertLess(data["loadSeconds"], 3.0)
        self.assertLessEqual(data["vramMB"], 380.0, f"World VRAM {data['vramMB']} MB > 380 MB")
        self.assertLessEqual(data["maxSlotsOn"], 8192)
        self.assertLessEqual(data["maxSlotsOff"], 8192)
        self.assertLessEqual(data["denseMaxSlotsOn"], 8192)
        self.assertLessEqual(data["denseMaxSlotsOff"], 8192)
        self.assertGreaterEqual(data["maxVistaOnM"], 3000.0)

        by_lm = {row["name"]: row for row in data["landmarks"]}
        for lm_name in ("Highway 99", "Rockport Waterfront", "Downtown East Park", "Camden Beach", "Rosewood College", "Rosewood Blvd"):
            self.assertIn(lm_name, by_lm)
            row = by_lm[lm_name]
            self.assertGreaterEqual(row["zoneIdx"], 0, f"{lm_name} failed to match a PVS zone")
            self.assertGreaterEqual(row["pvsSectionsOn"], 10, f"{lm_name} has too few PVS sections")
            self.assertGreater(row["vistaOnM"], row["vistaOffM"] + 500.0, f"PVS ON did not extend vista at {lm_name}")
            self.assertGreaterEqual(row["vistaOnM"], 1500.0, f"{lm_name} PVS vista {row['vistaOnM']}m < 1500m")
            self.assertFalse(row["toggledOffState"])
            self.assertTrue(row["toggleOffDirty"])
            self.assertEqual(row["pvsSectionsOff"], 0)
            self.assertTrue(row["toggledOnState"])
            self.assertTrue(row["toggleOnDirty"])
            self.assertAlmostEqual(row["vistaReOnM"], row["vistaOnM"], delta=1.0)


if __name__ == "__main__":
    unittest.main()
