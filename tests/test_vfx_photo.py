#!/usr/bin/env python3
"""Offline-safe unit & Metal 4 GPU smoke tests for v1.3.0 VFX, Photo Mode, 5-Cam Rig & Post-FI HUD.

Passes cleanly both WITH and WITHOUT an untouched PS2 ISO (`NFSMW_ISO=/nonexistent`).
Covers:
  - Strict ABI layout & hygiene checks for RTVFXSystem.swift, RTHudTypes.swift, and RTHud.metal
    (RTVFXBuffer == 6,736 bytes <= 8 KB, RTPhotoCockpitHudUniforms == 80 bytes at buffer(6),
    buffer(0..5) & texture(0..2) untouched, zero personal paths, zero duplicate RTCameraMode).
  - Strict `swiftc -typecheck -warnings-as-errors` on RTHudTypes.swift + RTVFXSystem.swift.
  - Compiled native Swift + Metal runtime smoke test verifying:
      * RTVFXSystem skidmark / spark / smoke caps (<= 128 / <= 64 / <= 16), -14 m/s^2 spark gravity
        & roadY bounce, 0.35 -> 1.85m smoke expansion, tight AABB + bounding sphere, 4K tile crop.
      * RTPhotoModeController 6-DoF Free-Cam, 24..135mm focal length, f/1.4..f/16 photographic stops,
        exact optical thin-lens aperture radius, full-frame vertical FOV, and car autofocus.
      * RTDrivingCamRig 5-camera cycle (chase -> closeChase -> hood -> bumper -> cockpit) with
        authentic ATTRIBUTES.BIN per-car hood heights (1.17..1.50m, 80 deg FOV) & 0.56m bumper POV.
      * Real Metal MSL 4.0 (with 3.1 fallback) compile of RayTracing.metal + RTHud.metal,
        `hudOverlayKernel` PSO creation on Apple Silicon GPU, and live GPU dispatch verifying
        Widget 8 (Photo Viewfinder ON + zero-leak snap hide) and Widget 9 (Post-FI Cockpit Wheel +
        steering rotation).
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NATIVE_RT_DIR = REPO_ROOT / "native_metal_rt"
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import audit_repo_clean  # noqa: E402


class TestVFXPhotoStructureAndHygiene(unittest.TestCase):
    """Static ABI contract, slot preservation, and repo hygiene verification."""

    def test_owned_files_and_abi_contracts(self) -> None:
        vfx_swift = (NATIVE_RT_DIR / "RTVFXSystem.swift").read_text(encoding="utf-8")
        hud_types = (NATIVE_RT_DIR / "RTHudTypes.swift").read_text(encoding="utf-8")
        hud_metal = (NATIVE_RT_DIR / "RTHud.metal").read_text(encoding="utf-8")

        # 1. RTVFXSystem.swift contracts & no duplicate RTCameraMode collision with RTRenderer.swift:30
        self.assertNotRegex(vfx_swift, r"\benum\s+RTCameraMode\b", "Must not redeclare RTCameraMode")
        for sym in (
            "struct RTVFXBuffer",
            "static let maxSkids = 128",
            "static let maxSparks = 64",
            "static let maxSmoke = 16",
            "static let byteSize = 6736",
            "skidBoundsMinMaxXZ",
            "fxSphere",
            "photoDoF",
            "tileCrop",
            "makeEmptyBuffer",
            "packGPUBuffer",
            "final class RTVFXSystem",
            "final class RTPhotoModeController",
            "enum RTDriveCameraPreset",
            "enum RTDrivingCamRig",
            "cameraEyeOffset",
            "computeApertureRadiusMeters",
            "verticalFOVDegrees",
        ):
            self.assertIn(sym, vfx_swift)

        # 2. RTHudTypes.swift + RTHud.metal buffer(6) ABI & preserved buffer(0..5) / texture(0..2)
        self.assertIn("struct RTPhotoCockpitHudUniforms", hud_types)
        self.assertIn("static let byteSize = 80", hud_types)
        self.assertIn("struct RTPhotoCockpitHudUniforms", hud_metal)
        self.assertIn("sizeof(RTPhotoCockpitHudUniforms) == 80", hud_metal)
        self.assertIn("compositePhotoViewfinder", hud_metal)
        self.assertIn("compositeCockpitSteeringWheel", hud_metal)
        self.assertIn("compositeDigitalDrivingHUD", hud_metal)

        # Verify hudOverlayKernel preserves textures 0..2 and buffers 0..5 while adding buffer(6)
        kernel_idx = hud_metal.index("kernel void hudOverlayKernel(")
        kernel_sig = hud_metal[kernel_idx : kernel_idx + 900]
        for slot in (
            "[[texture(0)]]",
            "[[texture(1)]]",
            "[[texture(2)]]",
            "[[sampler(0)]]",
            "[[buffer(0)]]",
            "[[buffer(1)]]",
            "[[buffer(2)]]",
            "[[buffer(3)]]",
            "[[buffer(4)]]",
            "[[buffer(5)]]",
            "constant RTPhotoCockpitHudUniforms& photoCockpit [[buffer(6)]]",
        ):
            self.assertIn(slot, kernel_sig)

        # Verify #100 P2-1 tight dynamic M spacing helper is active in RTHud.metal
        self.assertIn("uintDigitCount5", hud_metal)
        self.assertIn("gapMX", hud_metal)
        self.assertIn("gateMX", hud_metal)

    def test_no_personal_paths_and_repo_hygiene(self) -> None:
        owned = [
            Path("native_metal_rt/RTVFXSystem.swift"),
            Path("native_metal_rt/RTHudTypes.swift"),
            Path("native_metal_rt/RTHud.metal"),
            Path("tests/test_vfx_photo.py"),
        ]
        for rel in owned:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"/Users/[A-Za-z0-9_.-]+/", text),
                f"Personal home path leaked in {rel}",
            )
        issues = audit_repo_clean.audit_files(REPO_ROOT, owned)
        self.assertEqual(issues, [], f"Hygiene issues: {[i.format_human() for i in issues]}")


class TestVFXPhotoSwiftAndMetalGPU(unittest.TestCase):
    """Strict Swift typecheck, compiled runtime math verification, and real Metal 4 GPU compile/dispatch."""

    def test_swift_typecheck_runtime_smoke_and_metal_hud_pso(self) -> None:
        if sys.platform != "darwin" or platform.system() != "Darwin" or shutil.which("swiftc") is None:
            self.skipTest("macOS swiftc toolchain unavailable")

        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        sdk_ver = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-version"], text=True).strip()
        sdk_major = int(sdk_ver.split(".")[0]) if sdk_ver and sdk_ver[0].isdigit() else 0
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
            str(NATIVE_RT_DIR / "RTHudTypes.swift"),
            str(NATIVE_RT_DIR / "RTVFXSystem.swift"),
        ]

        # 1. Strict typecheck (0 errors, 0 warnings)
        tc_cmd = ["swiftc", "-typecheck", "-warnings-as-errors", *base_flags, *swift_sources]
        tc_res = subprocess.run(tc_cmd, capture_output=True, text=True)
        self.assertEqual(tc_res.returncode, 0, f"swiftc -typecheck failed:\n{tc_res.stderr}")

        # 2. Compiled Swift + Metal GPU smoke binary
        with tempfile.TemporaryDirectory(prefix="rockport_vfx_photo_") as td:
            td_path = Path(td)
            smoke_swift = td_path / "main.swift"
            smoke_swift.write_text(
                r"""
import Foundation
import Metal
import simd

func check(_ cond: Bool, _ msg: String) {
    if !cond {
        fputs("FAIL: \(msg)\n", stderr)
        exit(1)
    }
}

func approx(_ a: Float, _ b: Float, _ tol: Float = 1e-3) -> Bool {
    return abs(a - b) <= tol
}

let args = CommandLine.arguments
let rtMetalPath = args[1]
let hudMetalPath = args[2]

// 1. Exact ABI byte sizes & alignments
RTVFXBuffer.assertLayout()
RTPhotoCockpitHudUniforms.assertLayout()
RTHudExtUniforms.assertLayout()
RTMinimapBuffer.assertLayout()
RTMusicBannerUniforms.assertLayout()
RTEventHudUniforms.assertLayout()
check(MemoryLayout<RTVFXBuffer>.stride == 6736, "RTVFXBuffer must be 6736 bytes")
check(MemoryLayout<RTVFXBuffer>.stride <= 8192, "RTVFXBuffer must be <= 8 KB")
check(MemoryLayout<RTVFXBuffer>.alignment == 16, "RTVFXBuffer must be 16B aligned")
check(MemoryLayout<RTPhotoCockpitHudUniforms>.stride == 80, "RTPhotoCockpitHudUniforms must be 80 bytes")
check(MemoryLayout<RTPhotoCockpitHudUniforms>.alignment == 16, "RTPhotoCockpitHudUniforms must be 16B aligned")

// 2. RTVFXSystem: Drift skids (<=128), sparks (<=64, gravity -14 + bounce), smoke (<=16, 0.35->1.85m)
do {
    let vfx = RTVFXSystem()
    // Initial empty pack
    let emptyPacked = vfx.packGPUBuffer(photoDoF: SIMD4<Float>(0, 5, 50, 0))
    check(emptyPacked.counts.x == 0 && emptyPacked.counts.y == 0 && emptyPacked.counts.z == 0, "empty counts must be 0")
    check(emptyPacked.fxSphere.w == 0.0, "empty fxSphere radius must be 0")
    check(emptyPacked.skidBoundsMinMaxXZ.x > emptyPacked.skidBoundsMinMaxXZ.z, "empty skid AABB must be inverted")
    check(emptyPacked.tileCrop == SIMD4<Float>(0, 0, 1, 1), "default tileCrop must be (0,0,1,1)")
    check((emptyPacked.counts.w & RTVFXFlagBits.enableRISStreetlampShadows) != 0, "RIS shadows bit should default ON")

    // Drive a sustained heavy drift + wall scrape + demo trigger for 300 frames (~5 seconds)
    var pos = SIMD3<Float>(0.0, 10.0, 0.0)
    var heading: Float = 0.0
    var sawSparkBounce = false
    var maxSeenSkids: UInt32 = 0
    var maxSeenSparks: UInt32 = 0
    var maxSeenSmoke: UInt32 = 0

    for frame in 0..<300 {
        let dt: Float = 1.0 / 60.0
        heading += 0.35 * dt
        let fwd = SIMD3<Float>(sin(heading), 0.0, cos(heading))
        let right = SIMD3<Float>(cos(heading), 0.0, -sin(heading))
        let vel = fwd * 26.0 - right * 8.5
        pos += vel * dt

        vfx.step(RTVFXStepInput(
            dt: dt,
            carPos: pos,
            carVel: vel,
            carHeadingRad: heading,
            roadY: 10.0,
            driftSlipDeg: 18.5,
            driftIntensity: 0.85,
            speedMs: simd_length(vel),
            handbrake: (frame % 40) < 10,
            nosActive: (frame >= 20 && frame < 50),
            throttle: 1.0,
            scrapeIntensity: (frame % 30) < 6 ? 0.75 : 0.0,
            impactIntensity: (frame == 45) ? 0.80 : 0.0,
            wetness: 0.65,
            scrapeSideSign: 1.0,
            demoTrigger: (frame == 0)
        ))

        let gpu = vfx.packGPUBuffer(
            photoDoF: SIMD4<Float>(0.0, 6.0, 50.0, 0.0),
            isCockpitCam: true,
            enableRISStreetlampShadows: true,
            photoModeActive: false,
            wetRoad: true
        )

        // Strict Guardrail 2 cap assertions every single frame
        check(gpu.counts.x <= 128, "skidCount cap violated: \(gpu.counts.x)")
        check(gpu.counts.y <= 64, "sparkCount cap violated: \(gpu.counts.y)")
        check(gpu.counts.z <= 16, "smokeCount cap violated: \(gpu.counts.z)")
        check((gpu.counts.w & RTVFXFlagBits.isCockpitCam) != 0, "cockpit flag missing")
        check((gpu.counts.w & RTVFXFlagBits.wetRoad) != 0, "wetRoad flag missing")

        maxSeenSkids = max(maxSeenSkids, gpu.counts.x)
        maxSeenSparks = max(maxSeenSparks, gpu.counts.y)
        maxSeenSmoke = max(maxSeenSmoke, gpu.counts.z)

        // Verify skidBoundsMinMaxXZ encloses every packed skid
        for i in 0..<Int(gpu.counts.x) {
            let s = gpu.getSkid(i)
            check(s.p0Width.x >= gpu.skidBoundsMinMaxXZ.x && s.p0Width.x <= gpu.skidBoundsMinMaxXZ.z, "skid p0.x outside AABB")
            check(s.p0Width.z >= gpu.skidBoundsMinMaxXZ.y && s.p0Width.z <= gpu.skidBoundsMinMaxXZ.w, "skid p0.z outside AABB")
            check(s.p1Alpha.x >= gpu.skidBoundsMinMaxXZ.x && s.p1Alpha.x <= gpu.skidBoundsMinMaxXZ.z, "skid p1.x outside AABB")
            check(s.p1Alpha.z >= gpu.skidBoundsMinMaxXZ.y && s.p1Alpha.z <= gpu.skidBoundsMinMaxXZ.w, "skid p1.z outside AABB")
            check(s.p1Alpha.w > 0.0 && s.p1Alpha.w <= 1.0, "skid alpha out of range")
        }

        // Verify fxSphere encloses every spark and smoke puff, and check spark roadY floor
        let fc = SIMD3<Float>(gpu.fxSphere.x, gpu.fxSphere.y, gpu.fxSphere.z)
        let fr = gpu.fxSphere.w
        for i in 0..<Int(gpu.counts.y) {
            let sp = gpu.getSpark(i)
            let p = SIMD3<Float>(sp.posLife.x, sp.posLife.y, sp.posLife.z)
            check(p.y >= 10.015, "spark fell below roadY floor: \(p.y)")
            if p.y < 10.12 && sp.velTemp.y > 0.2 {
                sawSparkBounce = true
            }
            check(simd_distance(p, fc) <= fr + 0.05, "spark outside fxSphere")
        }
        for i in 0..<Int(gpu.counts.z) {
            let sm = gpu.getSmoke(i)
            let p = SIMD3<Float>(sm.posRadius.x, sm.posRadius.y, sm.posRadius.z)
            let rad = sm.posRadius.w
            check(rad >= 0.349 && rad <= 1.851, "smoke radius out of [0.35..1.85]: \(rad)")
            check(simd_distance(p, fc) + rad <= fr + 0.05, "smoke puff outside fxSphere")
        }
    }

    check(maxSeenSkids >= 40 && maxSeenSkids <= 128, "expected rich skids within cap, got \(maxSeenSkids)")
    check(maxSeenSparks >= 24 && maxSeenSparks <= 64, "expected rich sparks within cap, got \(maxSeenSparks)")
    check(maxSeenSmoke >= 8 && maxSeenSmoke <= 16, "expected rich smoke within cap, got \(maxSeenSmoke)")
    check(sawSparkBounce, "expected ballistic sparks to bounce off roadY under -14 m/s^2 gravity")

    // Verify 2x2 overlap tile crop seamlessly covers [0..1] x [0..1]
    let t00 = RTVFXSystem.computeTileCrop(tileX: 0, tileY: 0, overlapPx: 32)
    let t11 = RTVFXSystem.computeTileCrop(tileX: 1, tileY: 1, overlapPx: 32)
    check(t00.copyOriginX == 32 && t00.copyOriginY == 32, "tile00 overlap pad should be 32px")
    check(t00.copyWidth == 1856 && t00.copyHeight == 1016, "inner crop should strip 2*32px overlap")
    check(t11.dstOriginX == 1856 && t11.dstOriginY == 1016, "tile11 dst origin must align seamlessly")
    // Verify inner pixel (32, 32) of tile(0,0) maps to full-frame UV (0.0, 0.0)
    let u00 = t00.tileCrop.x + (32.0 / 1920.0) * t00.tileCrop.z
    let v00 = t00.tileCrop.y + (32.0 / 1080.0) * t00.tileCrop.w
    check(approx(u00, 0.0, 1e-4) && approx(v00, 0.0, 1e-4), "tile00 inner top-left UV must map to (0,0), got (\(u00),\(v00))")
    // Verify inner pixel (1920-32, 1080-32) of tile(1,1) maps to full-frame UV (1.0, 1.0)
    let u11 = t11.tileCrop.x + ((1920.0 - 32.0) / 1920.0) * t11.tileCrop.z
    let v11 = t11.tileCrop.y + ((1080.0 - 32.0) / 1080.0) * t11.tileCrop.w
    check(approx(u11, 1.0, 1e-4) && approx(v11, 1.0, 1e-4), "tile11 inner bottom-right UV must map to (1,1), got (\(u11),\(v11))")
}

// 3. RTPhotoModeController: 6-DoF Free-Cam, Focal Length, Photographic Aperture Stops & Optical DoF
do {
    let photo = RTPhotoModeController()
    check(!photo.isActive, "photo mode starts inactive")
    check(photo.computeApertureRadiusMeters() == 0.0, "inactive photo mode must return 0 aperture radius")

    let carPos = SIMD3<Float>(10.0, 2.0, 20.0)
    let camStart = SIMD3<Float>(10.0, 3.65, 14.0)
    photo.enter(fromCamPos: camStart, yaw: 0.0, pitch: -0.10, carPos: carPos)
    check(photo.isActive, "enter must activate photo mode")
    check(approx(photo.focalLengthMm, 50.0, 1e-4), "default focal length must be 50mm")
    check(approx(photo.apertureFStop, 2.0, 1e-4), "default aperture must be f/2.0")
    check(approx(photo.focusDistM, simd_distance(camStart, carPos + SIMD3<Float>(0, 0.65, 0)), 1e-2), "initial AF distance mismatch")

    // Exact thin-lens aperture radius: 0.5 * (50mm / 1000) / 2.0 = 0.0125 m
    check(approx(photo.computeApertureRadiusMeters(), 0.0125, 1e-5), "50mm f/2.0 aperture radius must be 0.0125m")
    // Exact full-frame 35mm vertical FOV: 2 * atan(12 / 50) = 26.991 deg
    check(approx(photo.verticalFOVDegrees(), 26.9915, 0.05), "50mm vFOV mismatch: \(photo.verticalFOVDegrees())")

    // Focal length bounds 24..135mm
    photo.adjustFocalLength(deltaMm: -100.0)
    check(approx(photo.focalLengthMm, 24.0, 1e-4), "focal min clamp 24mm failed")
    check(approx(photo.verticalFOVDegrees(), 53.1301, 0.05), "24mm vFOV mismatch")
    photo.adjustFocalLength(deltaMm: 200.0)
    check(approx(photo.focalLengthMm, 135.0, 1e-4), "focal max clamp 135mm failed")
    check(approx(photo.verticalFOVDegrees(), 10.159, 0.05), "135mm vFOV mismatch")
    photo.setFocalLength(mm: 50.0)

    // Photographic stop math: +2 stops doubles f-stop (2.0 -> 4.0)
    photo.adjustAperture(stops: 2.0)
    check(approx(photo.apertureFStop, 4.0, 1e-3), "+2 stops from f/2.0 must be f/4.0, got \(photo.apertureFStop)")
    photo.adjustAperture(stops: -10.0)
    check(approx(photo.apertureFStop, 1.4, 1e-4), "aperture min clamp f/1.4 failed")
    photo.adjustAperture(stops: 20.0)
    check(approx(photo.apertureFStop, 16.0, 1e-4), "aperture max clamp f/16.0 failed")

    // 6-DoF Free-Cam step (normal + fast + roll + autofocus update)
    let pBefore = photo.position
    photo.step(dt: 0.1, moveForward: 1.0, moveRight: 0.5, moveUp: 0.5, rollDelta: 1.0, fast: false)
    let movedSlow = simd_distance(photo.position, pBefore)
    check(movedSlow > 0.5, "photo free-cam failed to move")
    check(photo.roll > 0.02, "photo roll failed to update")

    let pBeforeFast = photo.position
    photo.step(dt: 0.1, moveForward: 1.0, moveRight: 0.0, moveUp: 0.0, rollDelta: 0.0, fast: true)
    let movedFast = simd_distance(photo.position, pBeforeFast)
    check(movedFast > 1.5, "fast photo free-cam speed mismatch: \(movedFast)")

    let basis = photo.cameraBasis()
    check(approx(simd_length(basis.forward), 1.0, 1e-4), "forward must be unit")
    check(approx(simd_length(basis.right), 1.0, 1e-4), "right must be unit")
    check(approx(simd_length(basis.up), 1.0, 1e-4), "up must be unit")
    check(abs(simd_dot(basis.forward, basis.right)) < 1e-4, "basis not orthogonal (f.r)")
    check(abs(simd_dot(basis.forward, basis.up)) < 1e-4, "basis not orthogonal (f.u)")
    check(abs(simd_dot(basis.right, basis.up)) < 1e-4, "basis not orthogonal (r.u)")

    photo.exit()
    check(!photo.isActive, "exit must deactivate photo mode")
    check(photo.computeApertureRadiusMeters() == 0.0, "exited photo mode must zero apertureRadiusM")
}

// 4. 5-Camera Rig (`RTDriveCameraPreset` + `RTDrivingCamRig`)
do {
    var cam: RTDriveCameraPreset = .chase
    let expectedOrder: [RTDriveCameraPreset] = [.chase, .closeChase, .hood, .bumper, .cockpit]
    for exp in expectedOrder {
        check(cam == exp, "camera cycle mismatch: got \(cam) expected \(exp)")
        cam = cam.next()
    }
    check(cam == .chase, "5-camera cycle must wrap cleanly back to .chase")
    check(RTDriveCameraPreset.parse("close") == .closeChase, "parse close failed")
    check(RTDriveCameraPreset.parse("hood") == .hood, "parse hood failed")
    check(RTDriveCameraPreset.parse("bumper") == .bumper, "parse bumper failed")
    check(RTDriveCameraPreset.parse("cockpit") == .cockpit, "parse cockpit failed")

    // Authentic ATTRIBUTES.BIN per-car hood heights (1.17..1.50m, 80 deg FOV)
    let fordHood = RTDrivingCamRig.cameraEyeOffset(for: .hood, carKey: "ford_gt", carHalfLength: 2.30, carRoofY: 1.12)
    let m3Hood = RTDrivingCamRig.cameraEyeOffset(for: .hood, carKey: "m3_gtr", carHalfLength: 2.28, carRoofY: 1.32)
    let cgtHood = RTDrivingCamRig.cameraEyeOffset(for: .hood, carKey: "carrera_gt", carHalfLength: 2.30, carRoofY: 1.16)
    let monaroHood = RTDrivingCamRig.cameraEyeOffset(for: .hood, carKey: "monaro", carHalfLength: 2.38, carRoofY: 1.40)
    check(approx(fordHood.eyeOffsetLocal.y, 1.17, 1e-3), "Ford GT hood height must be 1.17m")
    check(approx(m3Hood.eyeOffsetLocal.y, 1.25, 1e-3), "M3 GTR hood height must be 1.25m")
    check(approx(cgtHood.eyeOffsetLocal.y, 1.35, 1e-3), "Carrera GT hood height must be 1.35m")
    check(approx(monaroHood.eyeOffsetLocal.y, 1.50, 1e-3), "Monaro hood height must be 1.50m")
    check(approx(m3Hood.horizontalFovDeg, 80.0, 1e-3), "Hood cam horizontal FOV must be 80 deg")

    // Bumper cam y = 0.56m ahead of front nose
    let bumper = RTDrivingCamRig.cameraEyeOffset(for: .bumper, carKey: "m3_gtr", carHalfLength: 2.28, carRoofY: 1.32)
    check(approx(bumper.eyeOffsetLocal.y, 0.56, 1e-3), "Bumper cam Y must be 0.56m")
    check(bumper.eyeOffsetLocal.z > 2.28, "Bumper cam Z must sit ahead of car nose")

    // Cockpit cam inside windshield below roof
    let cockpit = RTDrivingCamRig.cameraEyeOffset(for: .cockpit, carKey: "m3_gtr", carHalfLength: 2.28, carRoofY: 1.32)
    check(cockpit.eyeOffsetLocal.y > 0.95 && cockpit.eyeOffsetLocal.y < 1.32, "Cockpit eye Y must sit inside cabin")

    let pose = RTDrivingCamRig.worldCameraPose(for: .hood, carKey: "m3_gtr", carPos: SIMD3<Float>(5, 1, 10), carHeadingRad: 0.5)
    check(approx(simd_length(pose.forward), 1.0, 1e-4) && abs(simd_dot(pose.forward, pose.right)) < 1e-4, "worldCameraPose orthonormal failed")
}

// 5. Real Metal MSL Compile of RayTracing.metal + RTHud.metal & Live GPU Dispatch of hudOverlayKernel!
guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
    fputs("SKIP: No default Metal device available\n", stderr)
    exit(3)
}
guard device.supportsRaytracing else {
    fputs("SKIP: Metal hardware ray tracing unavailable on host GPU\n", stderr)
    exit(3)
}
guard let emptyBuf = RTVFXSystem.makeEmptyBuffer(device: device) else {
    fputs("FAIL: makeEmptyBuffer failed\n", stderr)
    exit(1)
}
check(emptyBuf.length == 6736, "empty MTLBuffer length mismatch")

let rtSrc = try String(contentsOfFile: rtMetalPath, encoding: .utf8)
let hudSrc = try String(contentsOfFile: hudMetalPath, encoding: .utf8)
let combinedSrc = rtSrc + "\n// --- RTHud.metal ---\n" + hudSrc

let opts = MTLCompileOptions()
opts.fastMathEnabled = true
var compiledMSL = "3.1"
var lib: MTLLibrary? = nil
""" + (
                    """if #available(macOS 26.0, *) {
    opts.languageVersion = .version4_0
    if let l4 = try? device.makeLibrary(source: combinedSrc, options: opts) {
        lib = l4
        compiledMSL = "4.0"
    }
}
"""
                    if sdk_major >= 26
                    else ""
                ) + r"""if lib == nil {
    opts.languageVersion = .version3_1
    lib = try device.makeLibrary(source: combinedSrc, options: opts)
    compiledMSL = "3.1"
}
guard let metalLib = lib, let hudFn = metalLib.makeFunction(name: "hudOverlayKernel") else {
    fputs("FAIL: hudOverlayKernel missing from compiled Metal library\n", stderr)
    exit(1)
}
let hudPSO = try device.makeComputePipelineState(function: hudFn)

// Allocate 320x180 RGBA8 textures to actually execute Widgets 8 & 9 on GPU!
let W = 320
let H = 180
let td = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: W, height: H, mipmapped: false)
td.storageMode = .shared
td.usage = [.shaderRead, .shaderWrite]
guard let inTex = device.makeTexture(descriptor: td),
      let outTex = device.makeTexture(descriptor: td) else {
    fputs("FAIL: texture creation failed\n", stderr)
    exit(1)
}
let mapDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: 1, height: 1, mipmapped: false)
mapDesc.storageMode = .shared
mapDesc.usage = [.shaderRead]
guard let mapTex = device.makeTexture(descriptor: mapDesc) else {
    fputs("FAIL: fallback map texture creation failed\n", stderr)
    exit(1)
}
let sampDesc = MTLSamplerDescriptor()
sampDesc.minFilter = .linear
sampDesc.magFilter = .linear
guard let linSamp = device.makeSamplerState(descriptor: sampDesc) else {
    fputs("FAIL: sampler creation failed\n", stderr)
    exit(1)
}

// Fill input texture with uniform mid-dark grey (40, 44, 52, 255)
var inPixels = [UInt8](repeating: 0, count: W * H * 4)
for i in 0..<(W * H) {
    inPixels[i * 4 + 0] = 40
    inPixels[i * 4 + 1] = 44
    inPixels[i * 4 + 2] = 52
    inPixels[i * 4 + 3] = 255
}
inTex.replace(region: MTLRegionMake2D(0, 0, W, H), mipmapLevel: 0, withBytes: inPixels, bytesPerRow: W * 4)
var mapPx: [UInt8] = [10, 20, 30, 255]
mapTex.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: &mapPx, bytesPerRow: 4)

// Zeroed RTFrameUniforms (544 bytes: 3 x float4x4 [192B] + 22 x float4 [352B])
var frameUniformBytes = [UInt8](repeating: 0, count: 544)
var hudExt = RTHudExtUniforms()
var minimap = RTMinimapBuffer()
var music = RTMusicBannerUniforms()
var evHud = RTEventHudUniforms()

func runHudKernel(photoCockpit: RTPhotoCockpitHudUniforms) -> [UInt8] {
    var srgbFlag: UInt32 = 1
    var extCopy = hudExt
    var mmCopy = minimap
    var musCopy = music
    var evCopy = evHud
    var pcCopy = photoCockpit
    guard let cmd = queue.makeCommandBuffer(), let enc = cmd.makeComputeCommandEncoder() else {
        fputs("FAIL: encoder creation failed\n", stderr)
        exit(1)
    }
    enc.setComputePipelineState(hudPSO)
    enc.setTexture(inTex, index: 0)
    enc.setTexture(outTex, index: 1)
    enc.setTexture(mapTex, index: 2)
    enc.setSamplerState(linSamp, index: 0)
    enc.setBytes(&frameUniformBytes, length: frameUniformBytes.count, index: 0)
    enc.setBytes(&srgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
    enc.setBytes(&extCopy, length: RTHudExtUniforms.byteSize, index: 2)
    enc.setBytes(&mmCopy, length: RTMinimapBuffer.byteSize, index: 3)
    enc.setBytes(&musCopy, length: RTMusicBannerUniforms.byteSize, index: 4)
    enc.setBytes(&evCopy, length: RTEventHudUniforms.byteSize, index: 5)
    enc.setBytes(&pcCopy, length: RTPhotoCockpitHudUniforms.byteSize, index: 6)
    let tg = MTLSize(width: 16, height: 16, depth: 1)
    let grid = MTLSize(width: W, height: H, depth: 1)
    enc.dispatchThreads(grid, threadsPerThreadgroup: tg)
    enc.endEncoding()
    cmd.commit()
    cmd.waitUntilCompleted()
    var outBytes = [UInt8](repeating: 0, count: W * H * 4)
    outTex.getBytes(&outBytes, bytesPerRow: W * 4, from: MTLRegionMake2D(0, 0, W, H), mipmapLevel: 0)
    return outBytes
}

func countChangedPixels(_ a: [UInt8], _ b: [UInt8], yMin: Int = 0, yMax: Int = H) -> Int {
    var diff = 0
    for y in yMin..<yMax {
        for x in 0..<W {
            let idx = (y * W + x) * 4
            let d = abs(Int(a[idx]) - Int(b[idx])) + abs(Int(a[idx + 1]) - Int(b[idx + 1])) + abs(Int(a[idx + 2]) - Int(b[idx + 2]))
            if d > 6 { diff += 1 }
        }
    }
    return diff
}

// GPU Scenario A: Photo Mode Viewfinder ON (rule-of-thirds + AF reticle + bottom OLED bar)
var pcViewfinder = RTPhotoCockpitHudUniforms()
pcViewfinder.photoFlags = RTPhotoCockpitHudFlagBits.photoModeActive | RTPhotoCockpitHudFlagBits.showRuleOfThirds
pcViewfinder.focalMm = 85.0
pcViewfinder.fStop = 1.8
pcViewfinder.focusDistM = 4.2
let outViewfinder = runHudKernel(photoCockpit: pcViewfinder)
let vfDiff = countChangedPixels(inPixels, outViewfinder)
check(vfDiff > 1200, "Widget 8 Photo Viewfinder should visibly render on GPU, changed=\(vfDiff)")

// GPU Scenario B: Photo Mode 4K Snap Hide (MUST leak ZERO HUD pixels!)
var pcSnapHide = pcViewfinder
pcSnapHide.photoFlags |= RTPhotoCockpitHudFlagBits.hideViewfinderForSnap
let outSnapHide = runHudKernel(photoCockpit: pcSnapHide)
let hideDiff = countChangedPixels(inPixels, outSnapHide)
check(hideDiff == 0, "hideViewfinderForSnap must draw ZERO pixels, changed=\(hideDiff)")

// GPU Scenario C: Post-FI GT7 Cockpit Steering Wheel + Steering Rotation Verification
var pcCockpit0 = RTPhotoCockpitHudUniforms()
pcCockpit0.photoFlags = RTPhotoCockpitHudFlagBits.cockpitActive
pcCockpit0.steeringAngleRad = 0.0
pcCockpit0.rpmNorm = 0.93
pcCockpit0.speedKmh = 218.0
pcCockpit0.gearInt = 5
pcCockpit0.simTimeSec = 0.05
let outCockpit0 = runHudKernel(photoCockpit: pcCockpit0)
let topUnchanged = countChangedPixels(inPixels, outCockpit0, yMin: 0, yMax: 100)
let wheelChanged = countChangedPixels(inPixels, outCockpit0, yMin: 110, yMax: 180)
check(topUnchanged == 0, "Cockpit wheel must not touch upper windshield sky region, changed=\(topUnchanged)")
check(wheelChanged > 1000, "Widget 9 GT7 Cockpit Wheel should visibly render in bottom-center, changed=\(wheelChanged)")

var pcCockpitTurn = pcCockpit0
pcCockpitTurn.steeringAngleRad = 0.85
let outCockpitTurn = runHudKernel(photoCockpit: pcCockpitTurn)
let rotDiff = countChangedPixels(outCockpit0, outCockpitTurn, yMin: 110, yMax: 180)
check(rotDiff > 500, "Cockpit steering wheel must rotate cleanly with steeringAngleRad, diff=\(rotDiff)")

print("VFX_PHOTO_GPU_PASS device=\(device.name) msl=\(compiledMSL) vfDiff=\(vfDiff) wheelDiff=\(wheelChanged) rotDiff=\(rotDiff)")
""",
                encoding="utf-8",
            )

            smoke_bin = td_path / "vfx_photo_smoke"
            build_cmd = [
                "swiftc",
                "-warnings-as-errors",
                *base_flags,
                *swift_sources,
                str(smoke_swift),
                "-o",
                str(smoke_bin),
            ]
            build_res = subprocess.run(build_cmd, capture_output=True, text=True)
            self.assertEqual(build_res.returncode, 0, f"swiftc smoke build failed:\n{build_res.stderr}")

            run_res = subprocess.run(
                [
                    str(smoke_bin),
                    str(NATIVE_RT_DIR / "RayTracing.metal"),
                    str(NATIVE_RT_DIR / "RTHud.metal"),
                ],
                capture_output=True,
                text=True,
            )
            if run_res.returncode == 3:
                self.skipTest(run_res.stderr.strip() or "Metal hardware ray tracing unavailable on host GPU")
            self.assertEqual(
                run_res.returncode,
                0,
                f"Swift/Metal GPU smoke failed:\nstdout={run_res.stdout}\nstderr={run_res.stderr}",
            )
            self.assertIn("VFX_PHOTO_GPU_PASS", run_res.stdout)


if __name__ == "__main__":
    unittest.main()
