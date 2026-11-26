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

// MARK: - v1.1.0 Post-FrameGen EA TRAX Music Banner ABI (hudOverlayKernel buffer(4))

enum RTMusicBannerFlagBits {
    static let visible: UInt32       = 1 << 0
    static let muted: UInt32         = 1 << 1
    static let pursuitScore: UInt32  = 1 << 2
    static let tunnelReverb: UInt32  = 1 << 3
}

/// 112-byte (<= 128B, 16B-aligned) sliding EA TRAX corner banner uniform block.
/// Matches `RTMusicBannerUniforms` in `RTHud.metal` byte-for-byte.
struct RTMusicBannerUniforms {
    // 0..15
    var bannerAlpha: Float = 0.0
    var slideT: Float = 0.0
    var trackIndex: UInt32 = 0
    var totalTracks: UInt32 = 0

    // 16..31
    var flags: UInt32 = 0
    var artistLen: UInt32 = 0
    var titleLen: UInt32 = 0
    var skyOcclusion: Float = 0.0

    // 32..63: 32 packed uppercase ASCII artist bytes (8 x UInt32)
    var artist0: UInt32 = 0
    var artist1: UInt32 = 0
    var artist2: UInt32 = 0
    var artist3: UInt32 = 0
    var artist4: UInt32 = 0
    var artist5: UInt32 = 0
    var artist6: UInt32 = 0
    var artist7: UInt32 = 0

    // 64..103: 40 packed uppercase ASCII title bytes (10 x UInt32)
    var title0: UInt32 = 0
    var title1: UInt32 = 0
    var title2: UInt32 = 0
    var title3: UInt32 = 0
    var title4: UInt32 = 0
    var title5: UInt32 = 0
    var title6: UInt32 = 0
    var title7: UInt32 = 0
    var title8: UInt32 = 0
    var title9: UInt32 = 0

    // 104..111: 16B-alignment padding
    var pad0: UInt32 = 0
    var pad1: UInt32 = 0

    static let byteSize = 112
    static let maxArtistChars = 32
    static let maxTitleChars = 40

    static func assertLayout() {
        precondition(MemoryLayout<RTMusicBannerUniforms>.stride == byteSize,
                     "RTMusicBannerUniforms stride (\(MemoryLayout<RTMusicBannerUniforms>.stride)) must be \(byteSize) bytes")
        precondition(MemoryLayout<RTMusicBannerUniforms>.stride <= 128,
                     "RTMusicBannerUniforms must fit inside 128-byte budget")
    }

    private static func packAscii(_ text: String, maxLen: Int) -> ([UInt32], UInt32) {
        let upper = text.uppercased()
        var bytes: [UInt8] = []
        bytes.reserveCapacity(maxLen)
        for scalar in upper.unicodeScalars {
            if bytes.count >= maxLen { break }
            let v = scalar.value
            if v >= 0x20 && v <= 0x5F {
                bytes.append(UInt8(v))
            } else if v == 0x2018 || v == 0x2019 { // curly apostrophe
                bytes.append(0x27)
            } else if v == 0x2013 || v == 0x2014 { // en/em dash
                bytes.append(0x2D)
            } else if v >= 0x61 && v <= 0x7A {
                bytes.append(UInt8(v - 32))
            } else {
                bytes.append(0x20)
            }
        }
        // Trim trailing spaces
        while let last = bytes.last, last == 0x20 {
            bytes.removeLast()
        }
        let count = bytes.count
        let wordCount = maxLen / 4
        var words = Array(repeating: UInt32(0), count: wordCount)
        for i in 0..<count {
            let wIdx = i >> 2
            let shift = (i & 3) * 8
            words[wIdx] |= UInt32(bytes[i]) << shift
        }
        return (words, UInt32(count))
    }

    mutating func setText(artist: String, title: String) {
        let (aw, alen) = Self.packAscii(artist, maxLen: Self.maxArtistChars)
        artistLen = alen
        artist0 = aw[0]; artist1 = aw[1]; artist2 = aw[2]; artist3 = aw[3]
        artist4 = aw[4]; artist5 = aw[5]; artist6 = aw[6]; artist7 = aw[7]

        let (tw, tlen) = Self.packAscii(title, maxLen: Self.maxTitleChars)
        titleLen = tlen
        title0 = tw[0]; title1 = tw[1]; title2 = tw[2]; title3 = tw[3]; title4 = tw[4]
        title5 = tw[5]; title6 = tw[6]; title7 = tw[7]; title8 = tw[8]; title9 = tw[9]
    }
}
