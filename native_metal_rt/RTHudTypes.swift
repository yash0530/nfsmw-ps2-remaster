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

// MARK: - v1.2.0 3D Event World FX & Post-FrameGen Race HUD / World Map ABI

/// 128-byte 16B-aligned analytical 3D event gate / chevron payload bound at raytraceKernel buffer(16).
struct RTEventWorldFXGPU {
    var gatePosRadius: SIMD4<Float> = .zero
    var gateDirKind: SIMD4<Float> = SIMD4<Float>(0, 0, 1, 0)
    var nextPosRadius: SIMD4<Float> = .zero
    var fxParams: SIMD4<Float> = .zero
    var chevron0: SIMD4<Float> = SIMD4<Float>(0, 0, 0, -100)
    var chevron1: SIMD4<Float> = SIMD4<Float>(0, 0, 0, -100)
    var chevron2: SIMD4<Float> = SIMD4<Float>(0, 0, 0, -100)
    var chevron3: SIMD4<Float> = SIMD4<Float>(0, 0, 0, -100)

    static let byteSize = 128

    static func assertLayout() {
        precondition(MemoryLayout<RTEventWorldFXGPU>.stride == byteSize,
                     "RTEventWorldFXGPU stride (\(MemoryLayout<RTEventWorldFXGPU>.stride)) must be \(byteSize) bytes")
    }
}

enum RTEventHudFlagBits {
    static let eventActive: UInt32       = 1 << 0
    static let worldMapOpen: UInt32      = 1 << 1
    static let hasSelectedMarker: UInt32 = 1 << 2
    static let radarFlash: UInt32        = 1 << 3
}

/// 16-element SIMD4<Float> GPS route block (256 bytes) for O(1) World Map overlay.
struct RTWorldMapGps16 {
    var b0 = RTMinimapVec4x8()
    var b1 = RTMinimapVec4x8()
}

/// 864-byte (<= 1 KB, 16B-aligned) Race Event HUD & Interactive Full-Island World Map uniform block
/// bound at hudOverlayKernel buffer(5) alongside baked 1024x1024 worldMapTex at texture(2).
struct RTEventHudUniforms {
    // 0..15
    var mode: UInt32 = 0          // 0=none, 1=sprint, 2=speedtrap, 3=boss
    var state: UInt32 = 0         // 0=idle, 1=countdown, 2=active, 3=win, 4=lose
    var cpIndex: UInt32 = 0       // 1-based current checkpoint / trap
    var cpTotal: UInt32 = 0

    // 16..31
    var elapsedSec: Float = 0.0
    var currentScore: Float = 0.0 // speedtrap cumulative km/h
    var targetScore: Float = 0.0  // speedtrap target km/h
    var bossGapM: Float = 0.0     // >0 player ahead, <0 boss ahead

    // 32..47
    var playerRank: UInt32 = 1
    var bossRank: UInt32 = 15
    var flags: UInt32 = 0
    var nextGateDistM: Float = 0.0

    // 48..63
    var mapViewParams: SIMD4<Float> = SIMD4<Float>(0.5, 0.5, 1.0, 0.0) // xy=centerNorm, z=zoom, w=playerHeadingRad

    // 64..79
    var playerMapPos: SIMD4<Float> = SIMD4<Float>(0.5, 0.5, 0.0, 0.0)  // xy=normPos, z=selectedIdx, w=markerCount

    // 80..463: 24 interactive markers (384B): xy=normPos, z=type [0=landmark,1=sprint,2=speedtrap,3=boss], w=bossRank
    var mapMarkers = RTMinimapBlips24()

    // 464..719: 16 GPS route segments (256B): xy=normStart, zw=normEnd
    var gpsRoute = RTWorldMapGps16()

    // 720..735: string lengths
    var eventTitleLen: UInt32 = 0
    var bossNameLen: UInt32 = 0
    var selectedTitleLen: UInt32 = 0
    var selectedSubLen: UInt32 = 0

    // 736..767: eventTitleChars[8] (32 chars)
    var evT0: UInt32 = 0; var evT1: UInt32 = 0; var evT2: UInt32 = 0; var evT3: UInt32 = 0
    var evT4: UInt32 = 0; var evT5: UInt32 = 0; var evT6: UInt32 = 0; var evT7: UInt32 = 0

    // 768..791: bossNameChars[6] (24 chars)
    var bn0: UInt32 = 0; var bn1: UInt32 = 0; var bn2: UInt32 = 0
    var bn3: UInt32 = 0; var bn4: UInt32 = 0; var bn5: UInt32 = 0

    // 792..823: selectedTitleChars[8] (32 chars)
    var selT0: UInt32 = 0; var selT1: UInt32 = 0; var selT2: UInt32 = 0; var selT3: UInt32 = 0
    var selT4: UInt32 = 0; var selT5: UInt32 = 0; var selT6: UInt32 = 0; var selT7: UInt32 = 0

    // 824..855: selectedSubChars[8] (32 chars)
    var selS0: UInt32 = 0; var selS1: UInt32 = 0; var selS2: UInt32 = 0; var selS3: UInt32 = 0
    var selS4: UInt32 = 0; var selS5: UInt32 = 0; var selS6: UInt32 = 0; var selS7: UInt32 = 0

    // 856..863
    var gpsSegCount: UInt32 = 0
    var pad0: UInt32 = 0

    static let byteSize = 864
    static let maxMarkers = 24
    static let maxGpsSegments = 16

    static func assertLayout() {
        precondition(MemoryLayout<RTEventHudUniforms>.stride == byteSize,
                     "RTEventHudUniforms stride (\(MemoryLayout<RTEventHudUniforms>.stride)) must be \(byteSize) bytes")
    }

    private static func packAsciiWords(_ text: String, maxLen: Int) -> ([UInt32], UInt32) {
        let upper = text.uppercased()
        var bytes: [UInt8] = []
        bytes.reserveCapacity(maxLen)
        for scalar in upper.unicodeScalars {
            if bytes.count >= maxLen { break }
            let v = scalar.value
            if v >= 0x20 && v <= 0x5F {
                bytes.append(UInt8(v))
            } else if v == 0x2018 || v == 0x2019 {
                bytes.append(0x27)
            } else if v == 0x2013 || v == 0x2014 {
                bytes.append(0x2D)
            } else if v >= 0x61 && v <= 0x7A {
                bytes.append(UInt8(v - 32))
            } else {
                bytes.append(0x20)
            }
        }
        while let last = bytes.last, last == 0x20 { bytes.removeLast() }
        var words = Array(repeating: UInt32(0), count: maxLen / 4)
        for i in 0..<bytes.count {
            words[i >> 2] |= UInt32(bytes[i]) << ((i & 3) * 8)
        }
        return (words, UInt32(bytes.count))
    }

    mutating func setEventTitle(_ s: String) {
        let (w, n) = Self.packAsciiWords(s, maxLen: 32)
        eventTitleLen = n
        evT0 = w[0]; evT1 = w[1]; evT2 = w[2]; evT3 = w[3]; evT4 = w[4]; evT5 = w[5]; evT6 = w[6]; evT7 = w[7]
    }

    mutating func setBossName(_ s: String) {
        let (w, n) = Self.packAsciiWords(s, maxLen: 24)
        bossNameLen = n
        bn0 = w[0]; bn1 = w[1]; bn2 = w[2]; bn3 = w[3]; bn4 = w[4]; bn5 = w[5]
    }

    mutating func setSelectedCard(title: String, subtitle: String) {
        let (tw, tn) = Self.packAsciiWords(title, maxLen: 32)
        selectedTitleLen = tn
        selT0 = tw[0]; selT1 = tw[1]; selT2 = tw[2]; selT3 = tw[3]; selT4 = tw[4]; selT5 = tw[5]; selT6 = tw[6]; selT7 = tw[7]
        let (sw, sn) = Self.packAsciiWords(subtitle, maxLen: 32)
        selectedSubLen = sn
        selS0 = sw[0]; selS1 = sw[1]; selS2 = sw[2]; selS3 = sw[3]; selS4 = sw[4]; selS5 = sw[5]; selS6 = sw[6]; selS7 = sw[7]
    }

    mutating func setMarker(_ index: Int, _ marker: SIMD4<Float>) {
        guard index >= 0 && index < Self.maxMarkers else { return }
        withUnsafeMutableBytes(of: &mapMarkers) { raw in
            raw.bindMemory(to: SIMD4<Float>.self)[index] = marker
        }
    }

    mutating func setGpsSegment(_ index: Int, _ seg: SIMD4<Float>) {
        guard index >= 0 && index < Self.maxGpsSegments else { return }
        withUnsafeMutableBytes(of: &gpsRoute) { raw in
            raw.bindMemory(to: SIMD4<Float>.self)[index] = seg
        }
    }
}

// MARK: - v1.3.0 Post-FrameGen GT7 Photo Mode Viewfinder & Cockpit Steering Wheel ABI (hudOverlayKernel buffer(6))

enum RTPhotoCockpitHudFlagBits {
    static let photoModeActive: UInt32       = 1 << 0
    static let hideViewfinderForSnap: UInt32 = 1 << 1
    static let cockpitActive: UInt32         = 1 << 2
    static let showRuleOfThirds: UInt32      = 1 << 3
}

/// 80-byte 16B-aligned Post-FI Photo Mode Viewfinder & GT7 Cockpit Wheel uniform block
/// bound at `hudOverlayKernel buffer(6)` (slots `buffer(0..5)` & `texture(0..2)` untouched).
/// Zero-initialized default disables both widgets with zero overhead.
struct RTPhotoCockpitHudUniforms {
    // 0..15: photo mode viewfinder telemetry
    var photoFlags: UInt32 = 0       // bit0=photoModeActive, bit1=hideViewfinderForSnap, bit2=cockpitActive, bit3=showRuleOfThirds
    var focalMm: Float = 50.0        // 24..135 mm
    var fStop: Float = 2.0           // 1.4..16.0
    var focusDistM: Float = 5.0      // 1.5..80.0 m

    // 16..31: cockpit steering wheel & hub OLED telemetry
    var steeringAngleRad: Float = 0.0 // + = counter-clockwise (left turn), - = clockwise (right turn)
    var rpmNorm: Float = 0.0          // 0..1 progressive shift-LED bar
    var speedKmh: Float = 0.0         // hub OLED digital speed
    var gearInt: Int32 = 1            // -1=R, 0=N, 1..6

    // 32..47: active camera & drift VFX telemetry
    var cameraMode: UInt32 = 0        // 0=chase, 1=closeChase, 2=hood, 3=bumper, 4=cockpit
    var skidActiveCount: UInt32 = 0
    var sparkActiveCount: UInt32 = 0
    var smokeActiveCount: UInt32 = 0

    // 48..63: clock & reserved scalars
    var simTimeSec: Float = 0.0
    var rsv0: Float = 0.0
    var rsv1: Float = 0.0
    var rsv2: Float = 0.0

    // 64..79: 16B-aligned padding vector
    var pad0: SIMD4<Float> = .zero

    static let byteSize = 80

    static func assertLayout() {
        precondition(MemoryLayout<RTPhotoCockpitHudUniforms>.stride == byteSize,
                     "RTPhotoCockpitHudUniforms stride (\(MemoryLayout<RTPhotoCockpitHudUniforms>.stride)) must be \(byteSize) bytes")
        precondition(MemoryLayout<RTPhotoCockpitHudUniforms>.alignment == 16,
                     "RTPhotoCockpitHudUniforms alignment (\(MemoryLayout<RTPhotoCockpitHudUniforms>.alignment)) must be 16 bytes")
        precondition(MemoryLayout<RTPhotoCockpitHudUniforms>.offset(of: \.photoFlags) == 0, "photoFlags offset must be 0")
        precondition(MemoryLayout<RTPhotoCockpitHudUniforms>.offset(of: \.steeringAngleRad) == 16, "steeringAngleRad offset must be 16")
        precondition(MemoryLayout<RTPhotoCockpitHudUniforms>.offset(of: \.cameraMode) == 32, "cameraMode offset must be 32")
        precondition(MemoryLayout<RTPhotoCockpitHudUniforms>.offset(of: \.simTimeSec) == 48, "simTimeSec offset must be 48")
        precondition(MemoryLayout<RTPhotoCockpitHudUniforms>.offset(of: \.pad0) == 64, "pad0 offset must be 64")
    }
}
