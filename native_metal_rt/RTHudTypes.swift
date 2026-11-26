import Foundation
import simd

// MARK: - Locked Hour 2/3 Post-FrameGen HUD & Minimap Binary ABI
// Matches RTHud.metal byte-for-byte:
//   hudOverlayKernel buffer(0) = RTFrameUniforms (unchanged)
//   hudOverlayKernel buffer(1) = uint32 encodeSRGB (unchanged)
//   hudOverlayKernel buffer(2) = RTHudExtUniforms (64 bytes, 16B aligned)
//   hudOverlayKernel buffer(3) = RTMinimapBuffer (1,936 bytes <= 2 KB, 16B aligned)

enum RTHudFlagBits {
    static let visible: UInt32       = 1 << 0
    static let frameGenOn: UInt32    = 1 << 1
    static let nosActive: UInt32     = 1 << 2
    static let sbActive: UInt32      = 1 << 3
    static let pursuitActive: UInt32 = 1 << 4
    static let busted: UInt32        = 1 << 5
    static let evaded: UInt32        = 1 << 6
    static let hideMinimap: UInt32   = 1 << 7
}

/// 64-byte 16B-aligned extended HUD state bound at hudOverlayKernel buffer(2).
/// Zero-initialized default falls back gracefully to RTFrameUniforms.hudParams/driveParams.
struct RTHudExtUniforms {
    // 0..15: core gauges
    var rpmNorm: Float = 0.0
    var nosNorm: Float = 1.0
    var sbNorm: Float = 1.0
    var fps: Float = 0.0

    // 16..31: pursuit telemetry
    var heatLevel: Float = 0.0
    var bounty: Float = 0.0
    var evadeProgress: Float = 0.0
    var bustedProgress: Float = 0.0

    // 32..47: world & dynamics
    var timeOfDayHours: Float = 17.5
    var simTimeSec: Float = 0.0
    var driftIntensity: Float = 0.0
    var speedKmhOverride: Float = 0.0

    // 48..63: bitflags & counts
    var hudFlags: UInt32 = 0
    var copCount: UInt32 = 0
    var districtIndex: UInt32 = 0
    var carIndex: UInt32 = 0

    static let byteSize = 64

    static func assertLayout() {
        precondition(MemoryLayout<RTHudExtUniforms>.stride == byteSize,
                     "RTHudExtUniforms stride (\(MemoryLayout<RTHudExtUniforms>.stride)) must be \(byteSize) bytes")
    }
}

typealias HudParams = RTHudExtUniforms

/// Fixed 8-element SIMD4<Float> block (128 bytes) for fast, compile-light POD storage.
struct RTMinimapVec4x8 {
    var v0: SIMD4<Float> = .zero
    var v1: SIMD4<Float> = .zero
    var v2: SIMD4<Float> = .zero
    var v3: SIMD4<Float> = .zero
    var v4: SIMD4<Float> = .zero
    var v5: SIMD4<Float> = .zero
    var v6: SIMD4<Float> = .zero
    var v7: SIMD4<Float> = .zero
}

/// 96 road segments (1,536 bytes): each float4 = (x0, y0, x1, y1) in heading-up [-1..+1] minimap coords.
struct RTMinimapSegments96 {
    var b0  = RTMinimapVec4x8()
    var b1  = RTMinimapVec4x8()
    var b2  = RTMinimapVec4x8()
    var b3  = RTMinimapVec4x8()
    var b4  = RTMinimapVec4x8()
    var b5  = RTMinimapVec4x8()
    var b6  = RTMinimapVec4x8()
    var b7  = RTMinimapVec4x8()
    var b8  = RTMinimapVec4x8()
    var b9  = RTMinimapVec4x8()
    var b10 = RTMinimapVec4x8()
    var b11 = RTMinimapVec4x8()
}

/// 24 vehicle blips (384 bytes): each float4 = (x, y, typeFlags, flashPhase).
/// typeFlags: 0 = civilian traffic, 1 = patrol cop, 2 = pursuing cop, 3 = landmark/checkpoint.
struct RTMinimapBlips24 {
    var b0 = RTMinimapVec4x8()
    var b1 = RTMinimapVec4x8()
    var b2 = RTMinimapVec4x8()
}

/// 1,936-byte (<= 2 KB) 16B-aligned GPU minimap buffer bound at hudOverlayKernel buffer(3).
/// When segCount == 0, RTHud.metal still renders a clean circular radar bezel + player chevron.
struct RTMinimapBuffer {
    var segCount: UInt32 = 0
    var blipCount: UInt32 = 0
    var mapRangeM: Float = 175.0
    var playerYawRad: Float = 0.0
    var roadSegments = RTMinimapSegments96()
    var blips = RTMinimapBlips24()

    static let maxSegments = 96
    static let maxBlips = 24
    static let byteSize = 1936

    static func assertLayout() {
        precondition(MemoryLayout<RTMinimapBuffer>.stride == byteSize,
                     "RTMinimapBuffer stride (\(MemoryLayout<RTMinimapBuffer>.stride)) must be \(byteSize) bytes")
        precondition(MemoryLayout<RTMinimapBuffer>.stride <= 2048,
                     "RTMinimapBuffer must fit inside 2 KB budget")
    }

    mutating func clear() {
        segCount = 0
        blipCount = 0
    }

    mutating func setSegment(_ index: Int, _ seg: SIMD4<Float>) {
        guard index >= 0 && index < Self.maxSegments else { return }
        withUnsafeMutableBytes(of: &roadSegments) { raw in
            raw.bindMemory(to: SIMD4<Float>.self)[index] = seg
        }
    }

    func getSegment(_ index: Int) -> SIMD4<Float> {
        guard index >= 0 && index < Self.maxSegments else { return .zero }
        var copy = roadSegments
        return withUnsafeBytes(of: &copy) { raw in
            raw.bindMemory(to: SIMD4<Float>.self)[index]
        }
    }

    mutating func setBlip(_ index: Int, _ blip: SIMD4<Float>) {
        guard index >= 0 && index < Self.maxBlips else { return }
        withUnsafeMutableBytes(of: &blips) { raw in
            raw.bindMemory(to: SIMD4<Float>.self)[index] = blip
        }
    }

    func getBlip(_ index: Int) -> SIMD4<Float> {
        guard index >= 0 && index < Self.maxBlips else { return .zero }
        var copy = blips
        return withUnsafeBytes(of: &copy) { raw in
            raw.bindMemory(to: SIMD4<Float>.self)[index]
        }
    }
}
