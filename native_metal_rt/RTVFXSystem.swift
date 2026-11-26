import Foundation
import Metal
import simd

// MARK: - v1.3.0 Analytical RT Drift VFX, Thin-Lens Photo DoF & 4K Tile Crop GPU ABI
// Matches RayTracing.metal buffer(15) byte-for-byte (locked per #104 / #106 / #108):
//   Header (80 bytes, 16B aligned):
//     0..15:  counts             (skidCount <= 128, sparkCount <= 64, smokeCount <= 16, flags)
//     16..31: skidBoundsMinMaxXZ (minX, minZ, maxX, maxZ) — single-instruction AABB reject
//     32..47: fxSphere           (centerX, centerY, centerZ, radius) — single-instruction early-out
//     48..63: photoDoF           (apertureRadiusM, focusDistM, focalLengthMm, shutterVignette)
//     64..79: tileCrop           (offsetU, offsetV, scaleU, scaleV) — default (0, 0, 1, 1)
//   Payload (6,656 bytes):
//     80..4175:   skids[128]  = 128 x (float4 p0Width, float4 p1Alpha)   = 4,096 B
//     4176..6223: sparks[64]  =  64 x (float4 posLife, float4 velTemp)   = 2,048 B
//     6224..6735: smoke[16]   =  16 x (float4 posRadius, float4 velAlpha)=   512 B
//   Total = 6,736 bytes (<= 8 KB, 16B aligned).

enum RTVFXFlagBits {
    /// One-way interior windshield pass-through when driving in Cockpit POV.
    static let isCockpitCam: UInt32               = 1 << 0
    /// Enables 1-ray RIS real streetlamp shadows over RTWorldLight[11] + LightGrid[12].
    static let enableRISStreetlampShadows: UInt32 = 1 << 1
    /// Signals wet asphalt so road skidmarks render darker & glossier.
    static let wetRoad: UInt32                    = 1 << 2
    /// Signals Photo Mode is active.
    static let photoModeActive: UInt32            = 1 << 3
}

/// 32-byte road skidmark segment: p0Width = (x0, y0, z0, halfWidthM), p1Alpha = (x1, y1, z1, alpha).
struct RTVFXSkidGPU {
    var p0Width: SIMD4<Float> = .zero
    var p1Alpha: SIMD4<Float> = .zero
}

/// 32-byte ballistic spark: posLife = (x, y, z, lifeNorm), velTemp = (vx, vy, vz, tempNorm).
struct RTVFXSparkGPU {
    var posLife: SIMD4<Float> = .zero
    var velTemp: SIMD4<Float> = .zero
}

/// 32-byte volumetric tire-smoke puff: posRadius = (x, y, z, radiusM), velAlpha = (vx, vy, vz, alpha).
struct RTVFXSmokeGPU {
    var posRadius: SIMD4<Float> = .zero
    var velAlpha: SIMD4<Float> = .zero
}

/// Fixed 8-element POD blocks (256 bytes each) for fast Swift compilation & exact C array layout.
struct RTVFXSkidBlock8 {
    var v0 = RTVFXSkidGPU(); var v1 = RTVFXSkidGPU(); var v2 = RTVFXSkidGPU(); var v3 = RTVFXSkidGPU()
    var v4 = RTVFXSkidGPU(); var v5 = RTVFXSkidGPU(); var v6 = RTVFXSkidGPU(); var v7 = RTVFXSkidGPU()
}

struct RTVFXSkids128 {
    var b0  = RTVFXSkidBlock8(); var b1  = RTVFXSkidBlock8(); var b2  = RTVFXSkidBlock8(); var b3  = RTVFXSkidBlock8()
    var b4  = RTVFXSkidBlock8(); var b5  = RTVFXSkidBlock8(); var b6  = RTVFXSkidBlock8(); var b7  = RTVFXSkidBlock8()
    var b8  = RTVFXSkidBlock8(); var b9  = RTVFXSkidBlock8(); var b10 = RTVFXSkidBlock8(); var b11 = RTVFXSkidBlock8()
    var b12 = RTVFXSkidBlock8(); var b13 = RTVFXSkidBlock8(); var b14 = RTVFXSkidBlock8(); var b15 = RTVFXSkidBlock8()
}

struct RTVFXSparkBlock8 {
    var v0 = RTVFXSparkGPU(); var v1 = RTVFXSparkGPU(); var v2 = RTVFXSparkGPU(); var v3 = RTVFXSparkGPU()
    var v4 = RTVFXSparkGPU(); var v5 = RTVFXSparkGPU(); var v6 = RTVFXSparkGPU(); var v7 = RTVFXSparkGPU()
}

struct RTVFXSparks64 {
    var b0 = RTVFXSparkBlock8(); var b1 = RTVFXSparkBlock8(); var b2 = RTVFXSparkBlock8(); var b3 = RTVFXSparkBlock8()
    var b4 = RTVFXSparkBlock8(); var b5 = RTVFXSparkBlock8(); var b6 = RTVFXSparkBlock8(); var b7 = RTVFXSparkBlock8()
}

struct RTVFXSmokeBlock8 {
    var v0 = RTVFXSmokeGPU(); var v1 = RTVFXSmokeGPU(); var v2 = RTVFXSmokeGPU(); var v3 = RTVFXSmokeGPU()
    var v4 = RTVFXSmokeGPU(); var v5 = RTVFXSmokeGPU(); var v6 = RTVFXSmokeGPU(); var v7 = RTVFXSmokeGPU()
}

struct RTVFXSmoke16 {
    var b0 = RTVFXSmokeBlock8(); var b1 = RTVFXSmokeBlock8()
}

/// Exact 6,736-byte (<= 8 KB, 16B-aligned) GPU VFX + Photo Mode DoF + 4K Tile Crop buffer
/// bound at raytraceKernel buffer(15).
struct RTVFXBuffer {
    // 80-byte 16B-aligned header
    var counts: SIMD4<UInt32> = SIMD4<UInt32>(0, 0, 0, RTVFXFlagBits.enableRISStreetlampShadows)
    var skidBoundsMinMaxXZ: SIMD4<Float> = SIMD4<Float>(1e9, 1e9, -1e9, -1e9)
    var fxSphere: SIMD4<Float> = .zero
    var photoDoF: SIMD4<Float> = SIMD4<Float>(0.0, 5.0, 50.0, 0.0)
    var tileCrop: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 1.0, 1.0)

    // Fixed-cap primitive arrays strictly enforcing Guardrail 2 (<= 128 skids, <= 64 sparks, <= 16 smoke)
    var skids = RTVFXSkids128()
    var sparks = RTVFXSparks64()
    var smoke = RTVFXSmoke16()

    static let maxSkids = 128
    static let maxSparks = 64
    static let maxSmoke = 16
    static let headerByteSize = 80
    static let byteSize = 6736

    static func assertLayout() {
        precondition(MemoryLayout<RTVFXSkidGPU>.stride == 32, "RTVFXSkidGPU must be 32 bytes")
        precondition(MemoryLayout<RTVFXSparkGPU>.stride == 32, "RTVFXSparkGPU must be 32 bytes")
        precondition(MemoryLayout<RTVFXSmokeGPU>.stride == 32, "RTVFXSmokeGPU must be 32 bytes")
        precondition(MemoryLayout<RTVFXBuffer>.stride == byteSize,
                     "RTVFXBuffer stride (\(MemoryLayout<RTVFXBuffer>.stride)) must be \(byteSize) bytes")
        precondition(MemoryLayout<RTVFXBuffer>.alignment == 16,
                     "RTVFXBuffer alignment (\(MemoryLayout<RTVFXBuffer>.alignment)) must be 16 bytes")
        precondition(MemoryLayout<RTVFXBuffer>.stride <= 8192,
                     "RTVFXBuffer must fit inside 8 KB budget")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.counts) == 0, "counts offset must be 0")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.skidBoundsMinMaxXZ) == 16, "skidBoundsMinMaxXZ offset must be 16")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.fxSphere) == 32, "fxSphere offset must be 32")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.photoDoF) == 48, "photoDoF offset must be 48")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.tileCrop) == 64, "tileCrop offset must be 64")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.skids) == 80, "skids offset must be 80")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.sparks) == 4176, "sparks offset must be 4176")
        precondition(MemoryLayout<RTVFXBuffer>.offset(of: \.smoke) == 6224, "smoke offset must be 6224")
    }

    static func makeEmptyBuffer(device: MTLDevice, enableRISShadows: Bool = true) -> MTLBuffer? {
        assertLayout()
        var empty = RTVFXBuffer()
        empty.counts = SIMD4<UInt32>(0, 0, 0, enableRISShadows ? RTVFXFlagBits.enableRISStreetlampShadows : 0)
        empty.skidBoundsMinMaxXZ = SIMD4<Float>(1e9, 1e9, -1e9, -1e9)
        empty.fxSphere = .zero
        empty.photoDoF = SIMD4<Float>(0.0, 5.0, 50.0, 0.0)
        empty.tileCrop = SIMD4<Float>(0.0, 0.0, 1.0, 1.0)
        return device.makeBuffer(bytes: &empty, length: byteSize, options: .storageModeShared)
    }

    mutating func setSkid(_ index: Int, _ val: RTVFXSkidGPU) {
        guard index >= 0 && index < Self.maxSkids else { return }
        withUnsafeMutableBytes(of: &skids) { raw in
            raw.bindMemory(to: RTVFXSkidGPU.self)[index] = val
        }
    }

    func getSkid(_ index: Int) -> RTVFXSkidGPU {
        guard index >= 0 && index < Self.maxSkids else { return RTVFXSkidGPU() }
        var copy = skids
        return withUnsafeBytes(of: &copy) { raw in
            raw.bindMemory(to: RTVFXSkidGPU.self)[index]
        }
    }

    mutating func setSpark(_ index: Int, _ val: RTVFXSparkGPU) {
        guard index >= 0 && index < Self.maxSparks else { return }
        withUnsafeMutableBytes(of: &sparks) { raw in
            raw.bindMemory(to: RTVFXSparkGPU.self)[index] = val
        }
    }

    func getSpark(_ index: Int) -> RTVFXSparkGPU {
        guard index >= 0 && index < Self.maxSparks else { return RTVFXSparkGPU() }
        var copy = sparks
        return withUnsafeBytes(of: &copy) { raw in
            raw.bindMemory(to: RTVFXSparkGPU.self)[index]
        }
    }

    mutating func setSmoke(_ index: Int, _ val: RTVFXSmokeGPU) {
        guard index >= 0 && index < Self.maxSmoke else { return }
        withUnsafeMutableBytes(of: &smoke) { raw in
            raw.bindMemory(to: RTVFXSmokeGPU.self)[index] = val
        }
    }

    func getSmoke(_ index: Int) -> RTVFXSmokeGPU {
        guard index >= 0 && index < Self.maxSmoke else { return RTVFXSmokeGPU() }
        var copy = smoke
        return withUnsafeBytes(of: &copy) { raw in
            raw.bindMemory(to: RTVFXSmokeGPU.self)[index]
        }
    }
}

// MARK: - Analytical Drift VFX Simulation Engine (Persistent Skids, Ballistic Sparks, Tire Smoke)

struct RTVFXStepInput {
    var dt: Float
    var carPos: SIMD3<Float>
    var carVel: SIMD3<Float>
    var carHeadingRad: Float
    var roadY: Float
    var driftSlipDeg: Float
    var driftIntensity: Float = 0.0
    var speedMs: Float = 0.0
    var handbrake: Bool = false
    var nosActive: Bool = false
    var throttle: Float = 0.0
    var scrapeIntensity: Float = 0.0
    var impactIntensity: Float = 0.0
    var wetness: Float = 0.0
    var rearAxleZ: Float = -1.13
    var halfTrackR: Float = 0.80
    /// -1 = left side wall scrape, +1 = right side wall scrape, 0 = auto/rear underbody
    var scrapeSideSign: Float = 0.0
    /// Enables deterministic `--drift-vfx` showcase emission
    var demoTrigger: Bool = false
}

struct RTVFXTileCropSpec {
    /// Passed into `RTVFXBuffer.tileCrop` as `(offsetU, offsetV, scaleU, scaleV)`.
    let tileCrop: SIMD4<Float>
    /// Pixel sub-rectangle `(x, y, width, height)` inside the rendered tile to copy into the stitched 4K image
    /// (strips overlap padding after bloom so zero `+` seams appear between tiles!).
    let copyOriginX: Int
    let copyOriginY: Int
    let copyWidth: Int
    let copyHeight: Int
    /// Destination top-left pixel `(dstX, dstY)` in the stitched 4K output buffer.
    let dstOriginX: Int
    let dstOriginY: Int
}

final class RTVFXSystem {
    static let sparkGravityY: Float = -14.0
    static let maxSkidDistanceM: Float = 45.0
    static let skidLifetimeSec: Float = 26.0
    static let smokeStartRadiusM: Float = 0.35
    static let smokeEndRadiusM: Float = 1.85

    private struct SkidParticle {
        var p0: SIMD3<Float>
        var p1: SIMD3<Float>
        var halfWidth: Float
        var baseAlpha: Float
        var age: Float
        var maxAge: Float
    }

    private struct SparkParticle {
        var pos: SIMD3<Float>
        var vel: SIMD3<Float>
        var age: Float
        var maxLife: Float
        var initialTemp: Float
    }

    private struct SmokeParticle {
        var pos: SIMD3<Float>
        var vel: SIMD3<Float>
        var age: Float
        var maxLife: Float
        var peakAlpha: Float
    }

    private var skids: [SkidParticle] = []
    private var sparks: [SparkParticle] = []
    private var smoke: [SmokeParticle] = []

    private var prevTireRL: SIMD3<Float>? = nil
    private var prevTireRR: SIMD3<Float>? = nil
    private var wasSkidding: Bool = false
    private var sparkAccum: Float = 0.0
    private var smokeAccum: Float = 0.0
    private var lastCarPos: SIMD3<Float> = .zero
    private var lastWetness: Float = 0.0
    private var rngState: UInt64 = 0x9E3779B97F4A7C15

    var activeSkidCount: Int { min(skids.count, RTVFXBuffer.maxSkids) }
    var activeSparkCount: Int { min(sparks.count, RTVFXBuffer.maxSparks) }
    var activeSmokeCount: Int { min(smoke.count, RTVFXBuffer.maxSmoke) }

    init() {
        RTVFXBuffer.assertLayout()
        skids.reserveCapacity(RTVFXBuffer.maxSkids)
        sparks.reserveCapacity(RTVFXBuffer.maxSparks)
        smoke.reserveCapacity(RTVFXBuffer.maxSmoke)
    }

    func reset() {
        skids.removeAll(keepingCapacity: true)
        sparks.removeAll(keepingCapacity: true)
        smoke.removeAll(keepingCapacity: true)
        prevTireRL = nil
        prevTireRR = nil
        wasSkidding = false
        sparkAccum = 0.0
        smokeAccum = 0.0
    }

    static func makeEmptyBuffer(device: MTLDevice, enableRISShadows: Bool = true) -> MTLBuffer? {
        return RTVFXBuffer.makeEmptyBuffer(device: device, enableRISShadows: enableRISShadows)
    }

    /// Computes zero-seam 2x2 tiled crop parameters with optional overlap border (`overlapPx = 32`)
    /// so `bloomBlurKernel` never produces a center `+` seam when stitching 4K `3840x2160` Photo Mode snaps.
    static func computeTileCrop(
        tileX: Int,
        tileY: Int,
        tilesX: Int = 2,
        tilesY: Int = 2,
        renderWidth: Int = 1920,
        renderHeight: Int = 1080,
        overlapPx: Int = 32
    ) -> RTVFXTileCropSpec {
        let tx = max(0, min(tilesX - 1, tileX))
        let ty = max(0, min(tilesY - 1, tileY))
        let pad = max(0, min(overlapPx, min(renderWidth, renderHeight) / 8))
        let innerW = renderWidth - 2 * pad
        let innerH = renderHeight - 2 * pad
        let fullW = Float(innerW * tilesX)
        let fullH = Float(innerH * tilesY)

        // Map rendered tile pixels [0..renderW) to normalized full-frame UV space so inner [pad..renderW-pad)
        // tiles seamlessly across [0..1] x [0..1] while border pixels supply genuine padded ray context for bloom!
        let offsetU = (Float(tx * innerW) - Float(pad)) / fullW
        let offsetV = (Float(ty * innerH) - Float(pad)) / fullH
        let scaleU = Float(renderWidth) / fullW
        let scaleV = Float(renderHeight) / fullH

        return RTVFXTileCropSpec(
            tileCrop: SIMD4<Float>(offsetU, offsetV, scaleU, scaleV),
            copyOriginX: pad,
            copyOriginY: pad,
            copyWidth: innerW,
            copyHeight: innerH,
            dstOriginX: tx * innerW,
            dstOriginY: ty * innerH
        )
    }

    /// Deterministic fast PRNG in [0, 1).
    @inline(__always)
    private func nextRand01() -> Float {
        rngState &+= 0x9E3779B97F4A7C15
        var z = rngState
        z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
        z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
        z = z ^ (z >> 31)
        return Float(z & 0x00FF_FFFF) / Float(0x0100_0000)
    }

    @inline(__always)
    private func nextRandSigned() -> Float {
        return nextRand01() * 2.0 - 1.0
    }

    /// Seeds a rich, immediate drift VFX burst (twin curved skid arcs + shower of golden sparks + rising rear tire smoke)
    /// for `--drift-vfx` showcase captures or sudden high-impact slides.
    func seedDriftDemoBurst(
        carPos: SIMD3<Float>,
        carHeadingRad: Float,
        carVel: SIMD3<Float>,
        roadY: Float,
        wetness: Float = 0.5,
        rearAxleZ: Float = -1.13,
        halfTrackR: Float = 0.80
    ) {
        let fwd = SIMD3<Float>(sin(carHeadingRad), 0.0, cos(carHeadingRad))
        let right = SIMD3<Float>(cos(carHeadingRad), 0.0, -sin(carHeadingRad))
        let speed = max(18.0, simd_length(SIMD2<Float>(carVel.x, carVel.z)))
        let baseVel = (simd_length(carVel) > 4.0) ? carVel : (fwd * speed - right * (speed * 0.34))

        // 1. Twin curved drift skidmark arcs trailing 18m behind both rear tires
        let segCount = 16
        let stepLen: Float = 1.05
        var prevL = carPos + fwd * rearAxleZ - right * halfTrackR
        var prevR = carPos + fwd * rearAxleZ + right * halfTrackR
        prevL.y = roadY + 0.012
        prevR.y = roadY + 0.012
        for i in 1...segCount {
            let t = Float(i) / Float(segCount)
            let curve = t * t * 3.2
            let back = Float(i) * stepLen
            var pL = carPos + fwd * (rearAxleZ - back) - right * (halfTrackR + curve)
            var pR = carPos + fwd * (rearAxleZ - back) + right * (halfTrackR - curve)
            pL.y = roadY + 0.012
            pR.y = roadY + 0.012
            let alpha = (0.72 + 0.20 * wetness) * (1.0 - 0.55 * t)
            appendSkid(p0: pL, p1: prevL, halfWidth: 0.21, alpha: alpha, age: t * 3.0)
            appendSkid(p0: pR, p1: prevR, halfWidth: 0.21, alpha: alpha, age: t * 3.0)
            prevL = pL
            prevR = pR
        }

        // 2. Shower of ballistic golden-orange drift/rim/scrape sparks trailing from rear tires & diffuser
        let rlOrigin = SIMD3<Float>(carPos.x, roadY + 0.14, carPos.z) + fwd * rearAxleZ - right * (halfTrackR * 0.92)
        let rrOrigin = SIMD3<Float>(carPos.x, roadY + 0.14, carPos.z) + fwd * rearAxleZ + right * (halfTrackR * 0.92)
        for i in 0..<28 {
            let origin = (i & 1 == 0) ? rlOrigin : rrOrigin
            let spreadLat = nextRandSigned() * 3.4
            let backSpeed = 7.0 + nextRand01() * 11.0
            let upSpeed = 1.8 + nextRand01() * 4.6
            let v = -fwd * backSpeed + right * (spreadLat - 2.2) + SIMD3<Float>(0, upSpeed, 0) + baseVel * 0.22
            let life = 0.35 + nextRand01() * 0.40
            let age = nextRand01() * life * 0.45
            let pos = origin + v * age + SIMD3<Float>(0, 0.5 * Self.sparkGravityY * age * age, 0)
            let clampedPos = SIMD3<Float>(pos.x, max(roadY + 0.03, pos.y), pos.z)
            appendSpark(pos: clampedPos, vel: v + SIMD3<Float>(0, Self.sparkGravityY * age, 0), maxLife: life, age: age, temp: 0.78 + nextRand01() * 0.22)
        }

        // 3. Rising expanding rear tire-smoke plume (0.35m -> 1.85m)
        for i in 0..<10 {
            let t = Float(i) / 10.0
            let side: Float = (i & 1 == 0) ? -1.0 : 1.0
            let back = Float(i) * 1.15
            let pos = SIMD3<Float>(carPos.x, roadY + 0.30 + t * 1.05, carPos.z)
                + fwd * (rearAxleZ - 0.35 - back)
                + right * (side * halfTrackR * 0.95 - t * 1.6 + nextRandSigned() * 0.30)
            let vel = -fwd * (1.8 + nextRand01() * 1.4) + SIMD3<Float>(nextRandSigned() * 0.5, 0.75 + nextRand01() * 0.45, nextRandSigned() * 0.5)
            let maxLife: Float = 1.65 + nextRand01() * 0.55
            appendSmoke(pos: pos, vel: vel, maxLife: maxLife, age: t * maxLife * 0.65, peakAlpha: wetness > 0.4 ? 0.50 : 0.62)
        }

        lastCarPos = carPos
        lastWetness = wetness
    }

    /// Advances drift skidmarks, ballistic sparks (gravity -14 m/s^2 + road bounce), and rising tire smoke.
    func step(_ input: RTVFXStepInput) {
        let dt = max(0.0005, min(0.05, input.dt))
        lastCarPos = input.carPos
        lastWetness = max(0.0, min(1.0, input.wetness))

        let fwd = SIMD3<Float>(sin(input.carHeadingRad), 0.0, cos(input.carHeadingRad))
        let right = SIMD3<Float>(cos(input.carHeadingRad), 0.0, -sin(input.carHeadingRad))
        let absSlipDeg = abs(input.driftSlipDeg)
        let speedMs = max(input.speedMs, simd_length(SIMD2<Float>(input.carVel.x, input.carVel.z)))

        // 1. Age existing skidmarks and prune expired / distant (> 45m)
        var sWrite = 0
        for i in 0..<skids.count {
            var s = skids[i]
            s.age += dt
            let mid = (s.p0 + s.p1) * 0.5
            let dXZ = hypot(mid.x - input.carPos.x, mid.z - input.carPos.z)
            if s.age < s.maxAge && dXZ <= Self.maxSkidDistanceM * 1.15 {
                skids[sWrite] = s
                sWrite += 1
            }
        }
        if sWrite < skids.count {
            skids.removeLast(skids.count - sWrite)
        }

        // 2. Emit persistent rear-tire skidmarks when |driftSlipDeg| > 10 deg, handbrake, burnout/NOS launch, or demoTrigger
        let isBurnoutOrNosLaunch = (input.nosActive && input.throttle > 0.65 && speedMs > 1.2 && speedMs < 42.0)
            || (input.throttle > 0.85 && input.driftIntensity > 0.35 && speedMs > 2.5)
        let isSkidding = (absSlipDeg > 10.0 && speedMs > 2.5)
            || (input.handbrake && speedMs > 2.5)
            || isBurnoutOrNosLaunch
            || input.demoTrigger

        let currRL = SIMD3<Float>(input.carPos.x, input.roadY + 0.012, input.carPos.z)
            + fwd * input.rearAxleZ - right * input.halfTrackR
        let currRR = SIMD3<Float>(input.carPos.x, input.roadY + 0.012, input.carPos.z)
            + fwd * input.rearAxleZ + right * input.halfTrackR

        if isSkidding {
            if let pRL = prevTireRL, let pRR = prevTireRR, wasSkidding {
                let distL = simd_distance(currRL, pRL)
                let distR = simd_distance(currRR, pRR)
                if distL >= 0.55 && distL <= 6.5 {
                    let slipFactor = max(0.0, min(1.0, (absSlipDeg - 8.0) / 22.0))
                    let intensity = max(slipFactor, max(input.driftIntensity, input.handbrake ? 0.85 : (input.demoTrigger ? 0.88 : 0.65)))
                    let halfW = 0.165 + 0.065 * intensity
                    let alpha = min(0.94, (0.50 + 0.30 * intensity) * (1.0 + 0.22 * lastWetness))
                    appendSkid(p0: pRL, p1: currRL, halfWidth: halfW, alpha: alpha, age: 0.0)
                    prevTireRL = currRL
                } else if distL > 6.5 {
                    prevTireRL = currRL
                }
                if distR >= 0.55 && distR <= 6.5 {
                    let slipFactor = max(0.0, min(1.0, (absSlipDeg - 8.0) / 22.0))
                    let intensity = max(slipFactor, max(input.driftIntensity, input.handbrake ? 0.85 : (input.demoTrigger ? 0.88 : 0.65)))
                    let halfW = 0.165 + 0.065 * intensity
                    let alpha = min(0.94, (0.50 + 0.30 * intensity) * (1.0 + 0.22 * lastWetness))
                    appendSkid(p0: pRR, p1: currRR, halfWidth: halfW, alpha: alpha, age: 0.0)
                    prevTireRR = currRR
                } else if distR > 6.5 {
                    prevTireRR = currRR
                }
            } else {
                prevTireRL = currRL
                prevTireRR = currRR
                if input.demoTrigger && skids.isEmpty {
                    seedDriftDemoBurst(
                        carPos: input.carPos,
                        carHeadingRad: input.carHeadingRad,
                        carVel: input.carVel,
                        roadY: input.roadY,
                        wetness: input.wetness,
                        rearAxleZ: input.rearAxleZ,
                        halfTrackR: input.halfTrackR
                    )
                }
            }
            wasSkidding = true
        } else {
            wasSkidding = false
            prevTireRL = nil
            prevTireRR = nil
        }

        // 3. Integrate ballistic sparks under gravity (-14 m/s^2) + roadY bounce
        var spWrite = 0
        for i in 0..<sparks.count {
            var sp = sparks[i]
            sp.age += dt
            if sp.age >= sp.maxLife { continue }
            sp.vel.y += Self.sparkGravityY * dt
            let drag = max(0.0, 1.0 - 0.65 * dt)
            sp.vel.x *= drag
            sp.vel.z *= drag
            sp.pos += sp.vel * dt
            let floorY = input.roadY + 0.02
            if sp.pos.y < floorY && sp.vel.y < 0.0 {
                sp.pos.y = floorY
                sp.vel.y = -sp.vel.y * 0.42
                sp.vel.x *= 0.84
                sp.vel.z *= 0.84
            }
            sparks[spWrite] = sp
            spWrite += 1
        }
        if spWrite < sparks.count {
            sparks.removeLast(sparks.count - spWrite)
        }

        // 4. Emit new ballistic sparks (wall scrape, curb/impact strike, heavy drift, or demoTrigger)
        let heavyDriftSparks: Float = (absSlipDeg > 16.0 && speedMs > 12.0) ? min(1.0, (absSlipDeg - 14.0) / 18.0) : 0.0
        var sparkRate: Float = 0.0
        sparkRate += max(0.0, input.scrapeIntensity) * 95.0
        sparkRate += max(0.0, input.impactIntensity) * 140.0
        sparkRate += heavyDriftSparks * 32.0
        if input.demoTrigger { sparkRate = max(sparkRate, 58.0) }

        sparkAccum += sparkRate * dt
        var burstEmit = Int(sparkAccum)
        sparkAccum -= Float(burstEmit)
        if input.impactIntensity > 0.25 {
            burstEmit = max(burstEmit, Int(10.0 + input.impactIntensity * 14.0))
        }
        burstEmit = min(burstEmit, 20)

        for e in 0..<burstEmit {
            let sideSign: Float
            if input.scrapeIntensity > 0.05 && abs(input.scrapeSideSign) > 0.1 {
                sideSign = input.scrapeSideSign > 0 ? 1.0 : -1.0
            } else {
                sideSign = (e & 1 == 0) ? -1.0 : 1.0
            }
            let zLocal: Float = (input.scrapeIntensity > 0.05) ? (nextRandSigned() * 1.45) : (input.rearAxleZ - 0.12)
            let xLocal: Float = sideSign * (input.halfTrackR + (input.scrapeIntensity > 0.05 ? 0.16 : 0.02))
            let origin = SIMD3<Float>(input.carPos.x, input.roadY + 0.12 + nextRand01() * 0.26, input.carPos.z)
                + fwd * zLocal + right * xLocal
            let backV = 6.5 + nextRand01() * 11.5 + speedMs * 0.18
            let latV = -sideSign * (1.2 + nextRand01() * 3.4) + nextRandSigned() * 2.2
            let upV = 1.6 + nextRand01() * 4.8
            let vel = -fwd * backV + right * latV + SIMD3<Float>(0, upV, 0) + input.carVel * 0.28
            let maxLife: Float = 0.26 + nextRand01() * 0.46
            appendSpark(pos: origin, vel: vel, maxLife: maxLife, age: 0.0, temp: 0.80 + nextRand01() * 0.20)
        }

        // 5. Integrate rising expanding tire-smoke puffs (radius 0.35m -> 1.85m)
        var smWrite = 0
        for i in 0..<smoke.count {
            var sm = smoke[i]
            sm.age += dt
            if sm.age >= sm.maxLife { continue }
            let damp = exp(-1.75 * dt)
            sm.vel.x *= damp
            sm.vel.z *= damp
            sm.vel.y = max(0.35, sm.vel.y * (1.0 - 0.35 * dt))
            sm.pos += sm.vel * dt
            smoke[smWrite] = sm
            smWrite += 1
        }
        if smWrite < smoke.count {
            smoke.removeLast(smoke.count - smWrite)
        }

        // 6. Emit new tire-smoke puffs from spinning rear tires
        let smokeIntensity: Float = {
            if input.demoTrigger { return 0.95 }
            if input.handbrake && speedMs > 3.0 { return 0.85 }
            if absSlipDeg > 10.0 && speedMs > 4.0 { return min(1.0, (absSlipDeg - 9.0) / 18.0) }
            if isBurnoutOrNosLaunch { return 0.72 }
            return 0.0
        }()

        if smokeIntensity > 0.05 {
            smokeAccum += (8.0 + 7.0 * smokeIntensity) * dt
            var emitN = Int(smokeAccum)
            smokeAccum -= Float(emitN)
            emitN = min(emitN, 4)
            for e in 0..<emitN {
                let side: Float = (e & 1 == 0) ? -1.0 : 1.0
                let origin = SIMD3<Float>(input.carPos.x, input.roadY + 0.28, input.carPos.z)
                    + fwd * (input.rearAxleZ - 0.22 + nextRandSigned() * 0.15)
                    + right * (side * input.halfTrackR + nextRandSigned() * 0.12)
                let vel = input.carVel * 0.16 - fwd * (1.4 + nextRand01() * 1.4)
                    + right * (nextRandSigned() * 1.1)
                    + SIMD3<Float>(0, 0.72 + nextRand01() * 0.52, 0)
                let maxLife: Float = 1.35 + nextRand01() * 0.75
                let peakAlpha: Float = (0.44 + 0.20 * smokeIntensity) * (1.0 - 0.22 * lastWetness)
                appendSmoke(pos: origin, vel: vel, maxLife: maxLife, age: 0.0, peakAlpha: peakAlpha)
            }
        }
    }

    private func appendSkid(p0: SIMD3<Float>, p1: SIMD3<Float>, halfWidth: Float, alpha: Float, age: Float) {
        let item = SkidParticle(p0: p0, p1: p1, halfWidth: halfWidth, baseAlpha: alpha, age: age, maxAge: Self.skidLifetimeSec)
        if skids.count >= RTVFXBuffer.maxSkids {
            skids.removeFirst()
        }
        skids.append(item)
    }

    private func appendSpark(pos: SIMD3<Float>, vel: SIMD3<Float>, maxLife: Float, age: Float, temp: Float) {
        let item = SparkParticle(pos: pos, vel: vel, age: age, maxLife: maxLife, initialTemp: temp)
        if sparks.count >= RTVFXBuffer.maxSparks {
            // Evict oldest spark so fresh sparks always stay bright
            var oldestIdx = 0
            var oldestFrac: Float = -1.0
            for i in 0..<sparks.count {
                let f = sparks[i].age / max(0.01, sparks[i].maxLife)
                if f > oldestFrac {
                    oldestFrac = f
                    oldestIdx = i
                }
            }
            sparks[oldestIdx] = item
        } else {
            sparks.append(item)
        }
    }

    private func appendSmoke(pos: SIMD3<Float>, vel: SIMD3<Float>, maxLife: Float, age: Float, peakAlpha: Float) {
        let item = SmokeParticle(pos: pos, vel: vel, age: age, maxLife: maxLife, peakAlpha: peakAlpha)
        if smoke.count >= RTVFXBuffer.maxSmoke {
            smoke.removeFirst()
        }
        smoke.append(item)
    }

    /// Packs current skids (<=128), sparks (<=64), smoke (<=16), tight AABB/bounding sphere,
    /// Photo Mode DoF params, and 4K tileCrop into exact 6,736-byte `RTVFXBuffer`.
    func packGPUBuffer(
        photoDoF: SIMD4<Float> = SIMD4<Float>(0.0, 5.0, 50.0, 0.0),
        tileCrop: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 1.0, 1.0),
        isCockpitCam: Bool = false,
        enableRISStreetlampShadows: Bool = true,
        photoModeActive: Bool = false,
        wetRoad: Bool? = nil,
        referencePos: SIMD3<Float>? = nil
    ) -> RTVFXBuffer {
        var buf = RTVFXBuffer()
        let refPos = referencePos ?? lastCarPos
        let isWet = wetRoad ?? (lastWetness > 0.25)

        // 1. Pack skidmarks (<= 128) + compute tight XZ AABB for single-instruction reject
        var skidCount = 0
        var minX = Float.greatestFiniteMagnitude
        var minZ = Float.greatestFiniteMagnitude
        var maxX = -Float.greatestFiniteMagnitude
        var maxZ = -Float.greatestFiniteMagnitude

        for s in skids {
            if skidCount >= RTVFXBuffer.maxSkids { break }
            let mid = (s.p0 + s.p1) * 0.5
            let distXZ = hypot(mid.x - refPos.x, mid.z - refPos.z)
            if distXZ > Self.maxSkidDistanceM { continue }
            let ageFade = max(0.0, 1.0 - (s.age / max(1.0, s.maxAge)))
            let distFade = max(0.0, min(1.0, (Self.maxSkidDistanceM - distXZ) / 10.0))
            let alpha = s.baseAlpha * ageFade * distFade
            if alpha <= 0.015 { continue }

            buf.setSkid(skidCount, RTVFXSkidGPU(
                p0Width: SIMD4<Float>(s.p0.x, s.p0.y, s.p0.z, s.halfWidth),
                p1Alpha: SIMD4<Float>(s.p1.x, s.p1.y, s.p1.z, alpha)
            ))
            let pad = s.halfWidth + 0.15
            minX = min(minX, min(s.p0.x, s.p1.x) - pad)
            minZ = min(minZ, min(s.p0.z, s.p1.z) - pad)
            maxX = max(maxX, max(s.p0.x, s.p1.x) + pad)
            maxZ = max(maxZ, max(s.p0.z, s.p1.z) + pad)
            skidCount += 1
        }
        buf.skidBoundsMinMaxXZ = (skidCount > 0)
            ? SIMD4<Float>(minX, minZ, maxX, maxZ)
            : SIMD4<Float>(1e9, 1e9, -1e9, -1e9)

        // 2. Pack ballistic sparks (<= 64) and volumetric smoke (<= 16) + enclosing bounding sphere
        var fxMin = SIMD3<Float>(repeating: Float.greatestFiniteMagnitude)
        var fxMax = SIMD3<Float>(repeating: -Float.greatestFiniteMagnitude)

        var sparkCount = 0
        for sp in sparks {
            if sparkCount >= RTVFXBuffer.maxSparks { break }
            let lifeNorm = max(0.0, min(1.0, 1.0 - (sp.age / max(0.01, sp.maxLife))))
            if lifeNorm <= 0.01 { continue }
            let tempNorm = max(0.12, min(1.0, sp.initialTemp * (0.35 + 0.65 * lifeNorm)))
            buf.setSpark(sparkCount, RTVFXSparkGPU(
                posLife: SIMD4<Float>(sp.pos.x, sp.pos.y, sp.pos.z, lifeNorm),
                velTemp: SIMD4<Float>(sp.vel.x, sp.vel.y, sp.vel.z, tempNorm)
            ))
            // Account for velocity streak extent in bounding sphere
            let tail = sp.pos - sp.vel * 0.045
            let lo = simd_min(sp.pos, tail) - SIMD3<Float>(repeating: 0.12)
            let hi = simd_max(sp.pos, tail) + SIMD3<Float>(repeating: 0.12)
            fxMin = simd_min(fxMin, lo)
            fxMax = simd_max(fxMax, hi)
            sparkCount += 1
        }

        var smokeCount = 0
        for sm in smoke {
            if smokeCount >= RTVFXBuffer.maxSmoke { break }
            let u = max(0.0, min(1.0, sm.age / max(0.05, sm.maxLife)))
            if u >= 1.0 { continue }
            let radius = Self.smokeStartRadiusM + (Self.smokeEndRadiusM - Self.smokeStartRadiusM) * pow(u, 0.72)
            let fadeIn = min(1.0, u / 0.12)
            let fadeOut = pow(max(0.0, 1.0 - u), 1.35)
            let alpha = sm.peakAlpha * fadeIn * fadeOut
            if alpha <= 0.01 { continue }

            buf.setSmoke(smokeCount, RTVFXSmokeGPU(
                posRadius: SIMD4<Float>(sm.pos.x, sm.pos.y, sm.pos.z, radius),
                velAlpha: SIMD4<Float>(sm.vel.x, sm.vel.y, sm.vel.z, alpha)
            ))
            let rVec = SIMD3<Float>(repeating: radius)
            fxMin = simd_min(fxMin, sm.pos - rVec)
            fxMax = simd_max(fxMax, sm.pos + rVec)
            smokeCount += 1
        }

        if sparkCount > 0 || smokeCount > 0 {
            let center = (fxMin + fxMax) * 0.5
            let radius = simd_length(fxMax - center) + 0.25
            buf.fxSphere = SIMD4<Float>(center.x, center.y, center.z, radius)
        } else {
            buf.fxSphere = .zero
        }

        var flags: UInt32 = 0
        if isCockpitCam { flags |= RTVFXFlagBits.isCockpitCam }
        if enableRISStreetlampShadows { flags |= RTVFXFlagBits.enableRISStreetlampShadows }
        if isWet { flags |= RTVFXFlagBits.wetRoad }
        if photoModeActive { flags |= RTVFXFlagBits.photoModeActive }

        buf.counts = SIMD4<UInt32>(UInt32(skidCount), UInt32(sparkCount), UInt32(smokeCount), flags)
        buf.photoDoF = photoDoF
        buf.tileCrop = tileCrop
        return buf
    }

    /// Convenience helper writing packed `RTVFXBuffer` directly into an existing shared `MTLBuffer`.
    @discardableResult
    func write(
        to buffer: MTLBuffer,
        photoDoF: SIMD4<Float> = SIMD4<Float>(0.0, 5.0, 50.0, 0.0),
        tileCrop: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 1.0, 1.0),
        isCockpitCam: Bool = false,
        enableRISStreetlampShadows: Bool = true,
        photoModeActive: Bool = false,
        wetRoad: Bool? = nil,
        referencePos: SIMD3<Float>? = nil
    ) -> RTVFXBuffer {
        var packed = packGPUBuffer(
            photoDoF: photoDoF,
            tileCrop: tileCrop,
            isCockpitCam: isCockpitCam,
            enableRISStreetlampShadows: enableRISStreetlampShadows,
            photoModeActive: photoModeActive,
            wetRoad: wetRoad,
            referencePos: referencePos
        )
        precondition(buffer.length >= RTVFXBuffer.byteSize, "MTLBuffer too small for RTVFXBuffer")
        memcpy(buffer.contents(), &packed, RTVFXBuffer.byteSize)
        return packed
    }
}

// MARK: - GT7 6-DoF Photo Mode Controller

final class RTPhotoModeController {
    static let minFocalLengthMm: Float = 24.0
    static let maxFocalLengthMm: Float = 135.0
    static let minApertureFStop: Float = 1.4
    static let maxApertureFStop: Float = 16.0
    static let minFocusDistM: Float = 1.5
    static let maxFocusDistM: Float = 80.0
    static let fullFrameSensorHeightMm: Float = 24.0

    var isActive: Bool = false
    var position: SIMD3<Float> = .zero
    var yaw: Float = 0.0
    var pitch: Float = 0.0
    var roll: Float = 0.0
    var focalLengthMm: Float = 50.0
    var apertureFStop: Float = 2.0
    var focusDistM: Float = 5.0
    var autoFocusOnCar: Bool = true
    var showRuleOfThirds: Bool = true
    var hideViewfinderForSnap: Bool = false
    var dofEnabled: Bool = true
    var shutterVignette: Float = 0.16
    var carFocusAnchor: SIMD3<Float> = .zero

    /// Enters Photo Mode from an existing camera position & Euler yaw/pitch, locking autofocus onto `carPos`.
    func enter(fromCamPos: SIMD3<Float>, yaw: Float, pitch: Float, carPos: SIMD3<Float>) {
        self.isActive = true
        self.position = fromCamPos
        self.yaw = yaw
        self.pitch = Self.clampPitch(pitch)
        self.roll = 0.0
        self.carFocusAnchor = carPos
        self.autoFocusOnCar = true
        self.hideViewfinderForSnap = false
        self.focusDistM = Self.clampFocusDist(simd_distance(fromCamPos, carPos + SIMD3<Float>(0.0, 0.65, 0.0)))
    }

    /// Convenience entry point computing `(yaw, pitch)` directly from `(lookTarget - fromCamPos)`.
    func enter(fromCamPos: SIMD3<Float>, lookTarget: SIMD3<Float>, carPos: SIMD3<Float>) {
        let delta = lookTarget - fromCamPos
        let len = max(1e-4, simd_length(delta))
        let dir = delta / len
        let derivedYaw = atan2(dir.x, dir.z)
        let derivedPitch = asin(max(-0.98, min(0.98, dir.y)))
        enter(fromCamPos: fromCamPos, yaw: derivedYaw, pitch: derivedPitch, carPos: carPos)
    }

    func exit() {
        self.isActive = false
        self.hideViewfinderForSnap = false
        self.roll = 0.0
    }

    /// 6-DoF Free-Cam step: moves in camera basis `(forward, right, worldUp)`, adjusts Dutch roll, updates AF.
    func step(
        dt rawDt: Float,
        moveForward: Float,
        moveRight: Float,
        moveUp: Float,
        rollDelta: Float,
        fast: Bool
    ) {
        guard isActive else { return }
        let dt = max(0.0, min(0.1, rawDt))
        let speed: Float = fast ? 18.0 : 6.5
        let rollRate: Float = fast ? 1.10 : 0.55

        let basis = cameraBasis()
        let moveVec = basis.forward * moveForward + basis.right * moveRight + SIMD3<Float>(0.0, 1.0, 0.0) * moveUp
        position += moveVec * (speed * dt)
        roll = Self.clampRoll(roll + rollDelta * rollRate * dt)

        if autoFocusOnCar {
            let target = carFocusAnchor + SIMD3<Float>(0.0, 0.65, 0.0)
            focusDistM = Self.clampFocusDist(simd_distance(position, target))
        }
    }

    func rotate(deltaYaw: Float, deltaPitch: Float) {
        guard isActive else { return }
        yaw += deltaYaw
        pitch = Self.clampPitch(pitch + deltaPitch)
    }

    func adjustFocalLength(deltaMm: Float) {
        focalLengthMm = max(Self.minFocalLengthMm, min(Self.maxFocalLengthMm, focalLengthMm + deltaMm))
    }

    func setFocalLength(mm: Float) {
        focalLengthMm = max(Self.minFocalLengthMm, min(Self.maxFocalLengthMm, mm))
    }

    /// Adjusts aperture in photographic stops (`+1.0` stop = `f * sqrt(2)` smaller opening; `-1.0` stop = wider opening).
    func adjustAperture(stops: Float) {
        let factor = pow(Float(2.0).squareRoot(), stops)
        apertureFStop = max(Self.minApertureFStop, min(Self.maxApertureFStop, apertureFStop * factor))
    }

    func setAperture(fStop: Float) {
        apertureFStop = max(Self.minApertureFStop, min(Self.maxApertureFStop, fStop))
    }

    func adjustFocusDistance(deltaM: Float) {
        autoFocusOnCar = false
        focusDistM = Self.clampFocusDist(focusDistM + deltaM)
    }

    func refocusOnCar(_ carPos: SIMD3<Float>) {
        carFocusAnchor = carPos
        autoFocusOnCar = true
        focusDistM = Self.clampFocusDist(simd_distance(position, carPos + SIMD3<Float>(0.0, 0.65, 0.0)))
    }

    /// Physical thin-lens aperture radius in meters: `0.5 * (focalLengthMm / 1000) / fStop` (`0` when inactive/DoF off).
    func computeApertureRadiusMeters() -> Float {
        guard isActive && dofEnabled else { return 0.0 }
        let focalM = focalLengthMm * 0.001
        let clampedF = max(Self.minApertureFStop, min(Self.maxApertureFStop, apertureFStop))
        return 0.5 * (focalM / clampedF)
    }

    /// Full-frame 35mm vertical FOV in degrees (`24mm` sensor height): `2 * atan(12 / focalLengthMm)`.
    func verticalFOVDegrees() -> Float {
        let clampedMm = max(Self.minFocalLengthMm, min(Self.maxFocalLengthMm, focalLengthMm))
        return 2.0 * atan((Self.fullFrameSensorHeightMm * 0.5) / clampedMm) * 180.0 / .pi
    }

    /// Orthonormal 6-DoF camera basis matching RockportMetalRT right-handed Y-up convention (`r = cross(f, up)`).
    func cameraBasis() -> (eye: SIMD3<Float>, forward: SIMD3<Float>, right: SIMD3<Float>, up: SIMD3<Float>) {
        let cp = cos(pitch), sp = sin(pitch)
        let sy = sin(yaw), cy = cos(yaw)
        let fwd = simd_normalize(SIMD3<Float>(sy * cp, sp, cy * cp))
        let worldUp = SIMD3<Float>(0.0, 1.0, 0.0)
        var right = simd_cross(fwd, worldUp)
        if simd_length_squared(right) < 1e-6 {
            right = SIMD3<Float>(1.0, 0.0, 0.0)
        } else {
            right = simd_normalize(right)
        }
        var up = simd_normalize(simd_cross(right, fwd))

        if abs(roll) > 1e-5 {
            let cr = cos(roll), sr = sin(roll)
            let rolledRight = right * cr + up * sr
            let rolledUp = -right * sr + up * cr
            right = simd_normalize(rolledRight)
            up = simd_normalize(rolledUp)
        }
        return (position, fwd, right, up)
    }

    func viewMatrix() -> simd_float4x4 {
        let b = cameraBasis()
        let s = b.right
        let u = b.up
        let f = b.forward
        let eye = b.eye
        return simd_float4x4(
            SIMD4<Float>(s.x, u.x, -f.x, 0),
            SIMD4<Float>(s.y, u.y, -f.y, 0),
            SIMD4<Float>(s.z, u.z, -f.z, 0),
            SIMD4<Float>(-simd_dot(s, eye), -simd_dot(u, eye), simd_dot(f, eye), 1)
        )
    }

    func photoDoFUniform() -> SIMD4<Float> {
        return SIMD4<Float>(
            computeApertureRadiusMeters(),
            focusDistM,
            focalLengthMm,
            isActive ? shutterVignette : 0.0
        )
    }

    private static func clampPitch(_ p: Float) -> Float {
        let limit: Float = 82.0 * .pi / 180.0
        return max(-limit, min(limit, p))
    }

    private static func clampRoll(_ r: Float) -> Float {
        let limit: Float = 45.0 * .pi / 180.0
        return max(-limit, min(limit, r))
    }

    private static func clampFocusDist(_ d: Float) -> Float {
        return max(minFocusDistM, min(maxFocusDistM, d))
    }
}

// MARK: - 5-Camera Driving Rig (`RTDrivingCamRig`, avoiding `RTCameraMode` collision at RTRenderer.swift:30)

/// 5-camera live `C`-key driving cycle: Chase -> Close Chase -> Hood -> Bumper -> Cockpit.
enum RTDriveCameraPreset: UInt32, CaseIterable {
    case chase      = 0
    case closeChase = 1
    case hood       = 2
    case bumper     = 3
    case cockpit    = 4

    var displayName: String {
        switch self {
        case .chase:      return "CHASE"
        case .closeChase: return "CLOSE CHASE"
        case .hood:       return "HOOD CAM"
        case .bumper:     return "BUMPER CAM"
        case .cockpit:    return "COCKPIT"
        }
    }

    var cliName: String {
        switch self {
        case .chase:      return "chase"
        case .closeChase: return "close"
        case .hood:       return "hood"
        case .bumper:     return "bumper"
        case .cockpit:    return "cockpit"
        }
    }

    var isCarAttachedPOV: Bool {
        switch self {
        case .hood, .bumper, .cockpit: return true
        case .chase, .closeChase:      return false
        }
    }

    var isCockpit: Bool { self == .cockpit }

    func next() -> RTDriveCameraPreset {
        let all = Self.allCases
        let nextRaw = (Int(rawValue) + 1) % all.count
        return all[nextRaw]
    }

    static func parse(_ raw: String) -> RTDriveCameraPreset? {
        switch raw.lowercased().trimmingCharacters(in: .whitespacesAndNewlines) {
        case "chase", "far", "0":                       return .chase
        case "close", "closechase", "close_chase", "1": return .closeChase
        case "hood", "bonnet", "2":                     return .hood
        case "bumper", "nose", "3":                     return .bumper
        case "cockpit", "interior", "driver", "4":      return .cockpit
        default:                                        return nil
        }
    }

    func cameraEyeOffset(forCarKey carKey: String, carHalfLength: Float = 2.28, carRoofY: Float = 1.32) -> RTDrivingCameraOffset {
        return RTDrivingCamRig.cameraEyeOffset(for: self, carKey: carKey, carHalfLength: carHalfLength, carRoofY: carRoofY)
    }
}

struct RTDrivingCameraOffset {
    /// Car-local eye offset `(xLocal, yUp, zForward)` in meters.
    let eyeOffsetLocal: SIMD3<Float>
    /// Car-local look target `(xLocal, yUp, zForward)` in meters.
    let lookOffsetLocal: SIMD3<Float>
    /// Authentic `ATTRIBUTES.BIN` horizontal FOV in degrees (e.g. `80°` hood, `83°` bumper).
    let horizontalFovDeg: Float
    /// Downward camera pitch angle in degrees (`>0` pitches slightly down over the hood).
    let pitchDownDeg: Float

    /// Converts horizontal FOV to vertical FOV for `RTOrbitCamera.projMatrix(aspect:)`.
    func verticalFovDeg(aspect: Float = 16.0 / 9.0) -> Float {
        let safeAspect = max(0.5, aspect)
        let hRad = horizontalFovDeg * .pi / 180.0
        return 2.0 * atan(tan(hRad * 0.5) / safeAspect) * 180.0 / .pi
    }
}

/// Authentic `GLOBAL\ATTRIBUTES.BIN` `camerainfo` 5-camera driving rig helper.
enum RTDrivingCamRig {
    /// Verified per-car `ATTRIBUTES.BIN` `camerainfo` `*_hood` HEIGHT[0] values (1.17m .. 1.50m).
    static let authenticHoodHeightByCar: [String: Float] = [
        "fordgt":       1.17,
        "slr":          1.17,
        "elise":        1.18,
        "rx7":          1.18,
        "supra":        1.20,
        "eclipsegt":    1.22,
        "corvette":     1.23,
        "corvettec6r":  1.23,
        "tt":           1.24,
        "bmwm3":        1.25,
        "bmwm3gtr":     1.25,
        "bmwm3gtre46":  1.25,
        "sl65":         1.25,
        "db9":          1.26,
        "gallardo":     1.30,
        "camaro":       1.33,
        "clk500":       1.33,
        "sl500":        1.33,
        "clio":         1.34,
        "997s":         1.35,
        "a3":           1.35,
        "carreragt":    1.35,
        "caymans":      1.35,
        "cts":          1.37,
        "imprezawrx":   1.375,
        "911turbo":     1.38,
        "viper":        1.38,
        "350z":         1.397,
        "911gt2":       1.397,
        "is300":        1.397,
        "lancerevo8":   1.397,
        "punto":        1.397,
        "rx8":          1.397,
        "rx8speedt":    1.397,
        "mustanggt":    1.40,
        "gti":          1.41,
        "murcielago":   1.41,
        "a4":           1.42,
        "gto":          1.425,
        "cobaltss":     1.44,
        "monaro":       1.50,
    ]

    private static let carKeyAliases: [String: String] = [
        "m3_gtr":          "bmwm3gtre46",
        "m3":              "bmwm3gtre46",
        "bmw_m3":          "bmwm3",
        "bmw_m3_gtr_race": "bmwm3gtr",
        "carrera_gt":      "carreragt",
        "cgt":             "carreragt",
        "porsche":         "carreragt",
        "lambo":           "gallardo",
        "corvette_c6r":    "corvettec6r",
        "ford_gt":         "fordgt",
        "911_gt2":         "911gt2",
        "911_turbo":       "911turbo",
        "cayman_s":        "caymans",
        "mustang_gt":      "mustanggt",
        "evo8":            "lancerevo8",
        "wrx_sti":         "imprezawrx",
        "rx8_speedt":      "rx8speedt",
        "eclipse_gt":      "eclipsegt",
        "cobalt_ss":       "cobaltss",
    ]

    /// Returns authentic `ATTRIBUTES.BIN` hood camera height in meters (`1.17...1.50m`).
    static func authenticHoodHeight(forCarKey carKey: String) -> Float {
        let lower = carKey.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        let norm = lower.replacingOccurrences(of: "-", with: "_")
        if let h = authenticHoodHeightByCar[norm] { return h }
        if let mapped = carKeyAliases[norm], let h = authenticHoodHeightByCar[mapped] { return h }
        let compact = norm.replacingOccurrences(of: "_", with: "")
        if let h = authenticHoodHeightByCar[compact] { return h }
        return 1.32
    }

    /// Computes car-local eye offset, look target, and FOV for any of the 5 driving camera presets.
    static func cameraEyeOffset(
        for preset: RTDriveCameraPreset,
        carKey: String,
        carHalfLength: Float = 2.28,
        carRoofY: Float = 1.32
    ) -> RTDrivingCameraOffset {
        let halfLen = max(1.75, min(2.75, carHalfLength))
        let roofY = max(1.05, min(1.65, carRoofY))
        switch preset {
        case .chase:
            return RTDrivingCameraOffset(
                eyeOffsetLocal: SIMD3<Float>(0.0, 1.56, -4.65),
                lookOffsetLocal: SIMD3<Float>(0.0, 0.72, 2.60),
                horizontalFovDeg: 78.0,
                pitchDownDeg: 6.5
            )
        case .closeChase:
            return RTDrivingCameraOffset(
                eyeOffsetLocal: SIMD3<Float>(0.0, 1.38, -3.55),
                lookOffsetLocal: SIMD3<Float>(0.0, 0.74, 2.80),
                horizontalFovDeg: 78.0,
                pitchDownDeg: 5.5
            )
        case .hood:
            // Authentic ATTRIBUTES.BIN hood height (1.17..1.50m), FOV 80 deg, cowl mount showcasing real RT hood reflections!
            let hoodY = authenticHoodHeight(forCarKey: carKey)
            let hoodZ = halfLen * 0.16
            return RTDrivingCameraOffset(
                eyeOffsetLocal: SIMD3<Float>(0.0, hoodY, hoodZ),
                lookOffsetLocal: SIMD3<Float>(0.0, hoodY - 0.88, hoodZ + 18.0),
                horizontalFovDeg: 80.0,
                pitchDownDeg: 2.8
            )
        case .bumper:
            // Authentic low nose POV (y = 0.56m) right ahead of front bumper with zero self-intersection
            let noseZ = halfLen + 0.10
            return RTDrivingCameraOffset(
                eyeOffsetLocal: SIMD3<Float>(0.0, 0.56, noseZ),
                lookOffsetLocal: SIMD3<Float>(0.0, 0.54, noseZ + 20.0),
                horizontalFovDeg: 83.0,
                pitchDownDeg: 0.4
            )
        case .cockpit:
            // Driver eye POV inside cabin looking cleanly through one-way windshield over hood + post-FI GT3 wheel
            let eyeY = max(0.96, min(1.24, roofY - 0.21))
            let eyeZ = -halfLen * 0.04
            return RTDrivingCameraOffset(
                eyeOffsetLocal: SIMD3<Float>(0.0, eyeY, eyeZ),
                lookOffsetLocal: SIMD3<Float>(0.0, eyeY - 0.45, eyeZ + 18.0),
                horizontalFovDeg: 76.0,
                pitchDownDeg: 1.5
            )
        }
    }

    /// Computes world-space camera basis for car-attached POV modes (`.hood`, `.bumper`, `.cockpit`)
    /// honoring car heading, suspension/terrain pitch, and roll.
    static func worldCameraPose(
        for preset: RTDriveCameraPreset,
        carKey: String,
        carPos: SIMD3<Float>,
        carHeadingRad: Float,
        carPitchRad: Float = 0.0,
        carRollRad: Float = 0.0,
        carHalfLength: Float = 2.28,
        carRoofY: Float = 1.32,
        aspect: Float = 16.0 / 9.0
    ) -> (eye: SIMD3<Float>, target: SIMD3<Float>, forward: SIMD3<Float>, right: SIMD3<Float>, up: SIMD3<Float>, vFovDeg: Float) {
        let off = cameraEyeOffset(for: preset, carKey: carKey, carHalfLength: carHalfLength, carRoofY: carRoofY)
        let cy = cos(carHeadingRad), sy = sin(carHeadingRad)
        let cp = cos(carPitchRad), sp = sin(carPitchRad)
        let cr = cos(carRollRad), sr = sin(carRollRad)

        // Matches RTRenderer.currentCarBodyXform(): R_y(yaw) * R_x(pitch) * R_z(roll)
        let rotY = simd_float3x3(
            SIMD3<Float>(cy, 0, -sy),
            SIMD3<Float>(0, 1, 0),
            SIMD3<Float>(sy, 0, cy)
        )
        let rotX = simd_float3x3(
            SIMD3<Float>(1, 0, 0),
            SIMD3<Float>(0, cp, sp),
            SIMD3<Float>(0, -sp, cp)
        )
        let rotZ = simd_float3x3(
            SIMD3<Float>(cr, sr, 0),
            SIMD3<Float>(-sr, cr, 0),
            SIMD3<Float>(0, 0, 1)
        )
        let bodyRot = rotY * rotX * rotZ

        let eye = carPos + bodyRot * off.eyeOffsetLocal
        let target = carPos + bodyRot * off.lookOffsetLocal
        let fwd = simd_normalize(target - eye)
        let upHint = simd_normalize(bodyRot * SIMD3<Float>(0, 1, 0))
        let right = simd_normalize(simd_cross(fwd, upHint))
        let up = simd_normalize(simd_cross(right, fwd))
        return (eye, target, fwd, right, up, off.verticalFovDeg(aspect: aspect))
    }
}
