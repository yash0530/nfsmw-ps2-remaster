import AVFoundation
import Foundation

// MARK: - EA TRAX Music Metadata & Public Track Info

/// Public immutable descriptor of the currently active EA TRAX or Pursuit Score track.
public struct RTMusicTrackInfo: Equatable {
    public let index: Int
    public let total: Int
    public let artist: String
    public let title: String
    public let isPursuit: Bool

    public init(index: Int, total: Int, artist: String, title: String, isPursuit: Bool) {
        self.index = index
        self.total = total
        self.artist = artist
        self.title = title
        self.isPursuit = isPursuit
    }
}

/// Internal parsed track record from `build/music_pack/manifest.json`.
private struct MusicTrackRecord {
    let globalIndex: Int
    let streamIndex: Int
    let artist: String
    let title: String
    let album: String
    let category: String
    let isPursuit: Bool
    let fileURL: URL
    let durationSec: Double
}

// MARK: - Lock-Free C-Layout Control State & Preloaded Raw PCM Buffers

/// Plain-value struct shared between main thread and CoreAudio real-time render thread.
/// Updated atomically via a double-buffered pair (zero locks, zero ARC, zero heap allocations).
struct RTAudioControlState {
    var rpm: Float = 950.0
    var throttle: Float = 0.0
    var speedKmh: Float = 0.0
    var driftIntensity: Float = 0.0
    var turboBoost: Float = 0.0
    var nosActive: Float = 0.0
    var speedbreakerBlend: Float = 0.0
    var scrapeIntensity: Float = 0.0
    var rainIntensity: Float = 0.0
    var trannyWhineEnabled: Float = 1.0
    var sirenGain: Float = 0.0
    var sirenPan: Float = 0.0           // -1 left .. +1 right
    var sirenDoppler: Float = 1.0       // 0.75 .. 1.35
    var sirenMode: Int32 = 0            // 0 = wail, 1 = yelp, 2 = phaser
    var masterGain: Float = 0.78
    var muted: Int32 = 0

    // Monotonically incrementing event counters for lock-free one-shot triggering
    var shiftPopSeq: UInt32 = 0
    var bovSeq: UInt32 = 0
    var nosIgniteSeq: UInt32 = 0
    var nosPurgeSeq: UInt32 = 0
    var impactSeq: UInt32 = 0
    var impactGain: Float = 0.0
    var speedbreakerSeq: UInt32 = 0
    var radarPingSeq: UInt32 = 0
}

/// Owned raw float32 PCM buffer whose pointer remains valid for the lifetime of the audio engine.
private final class RawPCMBuffer {
    let sampleRate: Float
    let count: Int
    let looped: Bool
    let loopStart: Int
    let loopEnd: Int
    let storage: UnsafeMutablePointer<Float>

    init(samples: [Float], sampleRate: Float, looped: Bool = false, loopStart: Int = 0, loopLength: Int = 0) {
        self.sampleRate = max(8000.0, sampleRate)
        self.count = samples.count
        self.looped = looped
        let ls = max(0, min(max(0, samples.count - 2), loopStart))
        let le = loopLength > 16 ? min(samples.count, ls + loopLength) : samples.count
        self.loopStart = ls
        self.loopEnd = max(ls + 1, le)
        self.storage = UnsafeMutablePointer<Float>.allocate(capacity: max(1, samples.count))
        if !samples.isEmpty {
            samples.withUnsafeBufferPointer { src in
                self.storage.initialize(from: src.baseAddress!, count: samples.count)
            }
        } else {
            self.storage.initialize(to: 0.0)
        }
    }

    deinit {
        storage.deallocate()
    }
}

/// Owned Ginsu v20 granular waveform + RPM & 720-deg crank-cycle grain table (zero Swift collections in RT callback).
private final class RawGinsuBank {
    let pcm: RawPCMBuffer
    let minRPM: Float
    let maxRPM: Float
    let rpmCount: Int
    let rpmTable: UnsafeMutablePointer<Int32>
    let grainCount: Int
    let grainStarts: UnsafeMutablePointer<Int32>
    let grainLengths: UnsafeMutablePointer<Int32>

    init(pcm: RawPCMBuffer, minRPM: Float, maxRPM: Float, rpmTable: [Int], grainTable: [Int]) {
        self.pcm = pcm
        self.minRPM = minRPM
        self.maxRPM = max(minRPM + 500.0, maxRPM)

        let rCount = max(2, rpmTable.count)
        self.rpmCount = rCount
        self.rpmTable = UnsafeMutablePointer<Int32>.allocate(capacity: rCount)
        for i in 0..<rCount {
            self.rpmTable[i] = Int32(i < rpmTable.count ? rpmTable[i] : 0)
        }

        // Precompute valid [start, len] grain pairs
        var starts: [Int32] = []
        var lens: [Int32] = []
        if grainTable.count >= 2 {
            for i in 0..<(grainTable.count - 1) {
                let g0 = grainTable[i]
                let g1 = grainTable[i + 1]
                if g1 - g0 >= 16 && g1 <= pcm.count {
                    starts.append(Int32(g0))
                    lens.append(Int32(g1 - g0))
                }
            }
        }
        if starts.isEmpty {
            starts.append(0)
            lens.append(Int32(max(64, min(pcm.count, 1024))))
        }
        self.grainCount = starts.count
        self.grainStarts = UnsafeMutablePointer<Int32>.allocate(capacity: starts.count)
        self.grainLengths = UnsafeMutablePointer<Int32>.allocate(capacity: lens.count)
        for i in 0..<starts.count {
            self.grainStarts[i] = starts[i]
            self.grainLengths[i] = lens[i]
        }
    }

    deinit {
        rpmTable.deallocate()
        grainStarts.deallocate()
        grainLengths.deallocate()
    }
}

/// Real-time POD snapshot of a single Ginsu layer (no Swift object references inside render callback).
private struct GinsuLayerC {
    var samples: UnsafePointer<Float>? = nil
    var sampleCount: Int = 0
    var sampleRate: Float = 24000.0
    var minRPM: Float = 1200.0
    var maxRPM: Float = 8000.0
    var rpmTable: UnsafePointer<Int32>? = nil
    var rpmCount: Int = 0
    var grainStarts: UnsafePointer<Int32>? = nil
    var grainLengths: UnsafePointer<Int32>? = nil
    var grainCount: Int = 0

    // Two overlapping Hanning-windowed PSOLA voices (50% COLA overlap)
    var v0Start: Int32 = 0
    var v0Len: Int32 = 512
    var v0SrcPos: Float = 0.0
    var v0Phase: Float = 0.0
    var v0WinLen: Float = 1024.0
    var v0Step: Float = 0.5

    var v1Start: Int32 = 0
    var v1Len: Int32 = 512
    var v1SrcPos: Float = 0.0
    var v1Phase: Float = 0.5
    var v1WinLen: Float = 1024.0
    var v1Step: Float = 0.5

    mutating func bind(_ bank: RawGinsuBank?) {
        guard let b = bank else {
            samples = nil
            sampleCount = 0
            grainCount = 0
            return
        }
        samples = UnsafePointer(b.pcm.storage)
        sampleCount = b.pcm.count
        sampleRate = b.pcm.sampleRate
        minRPM = b.minRPM
        maxRPM = b.maxRPM
        rpmTable = UnsafePointer(b.rpmTable)
        rpmCount = b.rpmCount
        grainStarts = UnsafePointer(b.grainStarts)
        grainLengths = UnsafePointer(b.grainLengths)
        grainCount = b.grainCount
        v0Phase = 0.0
        v1Phase = 0.5
        spawnVoice(slot: 0, targetRPM: minRPM + 300.0, outSR: 48000.0, pitchScale: 1.0)
        spawnVoice(slot: 1, targetRPM: minRPM + 300.0, outSR: 48000.0, pitchScale: 1.0)
        v1Phase = 0.5
    }

    @inline(__always)
    private mutating func spawnVoice(slot: Int, targetRPM: Float, outSR: Float, pitchScale: Float) {
        guard let rTbl = rpmTable, let gStarts = grainStarts, let gLens = grainLengths,
              rpmCount >= 2, grainCount >= 1 else { return }

        let u = max(0.0, min(1.0, (targetRPM - minRPM) / max(1.0, maxRPM - minRPM)))
        let fIdx = u * Float(rpmCount - 1)
        let i0 = Int(fIdx)
        let i1 = min(rpmCount - 1, i0 + 1)
        let frac = fIdx - Float(i0)
        let srcPos = Float(rTbl[i0]) * (1.0 - frac) + Float(rTbl[i1]) * frac

        // Binary search nearest crank-cycle grain start <= srcPos
        var lo = 0
        var hi = grainCount - 1
        while lo < hi {
            let mid = (lo + hi + 1) >> 1
            if Float(gStarts[mid]) <= srcPos {
                lo = mid
            } else {
                hi = mid - 1
            }
        }
        let gStart = gStarts[lo]
        let gLen = max(Int32(16), gLens[lo])
        let nativeRPM = (sampleRate / Float(gLen)) * 120.0
        let pitchRatio = max(0.55, min(1.50, (targetRPM / max(200.0, nativeRPM)) * pitchScale))
        let step = (sampleRate / outSR) * pitchRatio
        let cycleOut = max(24.0, Float(gLen) / max(1e-4, step))
        let winOut = cycleOut * 2.0

        if slot == 0 {
            v0Start = gStart
            v0Len = gLen
            v0SrcPos = 0.0
            v0Phase = 0.0
            v0WinLen = winOut
            v0Step = step
        } else {
            v1Start = gStart
            v1Len = gLen
            v1SrcPos = 0.0
            v1Phase = 0.0
            v1WinLen = winOut
            v1Step = step
        }
    }

    @inline(__always)
    private func readSample(start: Int32, len: Int32, relPos: Float) -> Float {
        guard let ptr = samples, sampleCount > 2 else { return 0.0 }
        let sStart = Int(start)
        let sLen = Int(len)
        var absPos = Float(sStart) + relPos
        if Int(absPos) + 1 >= sampleCount {
            let wrapped = relPos.truncatingRemainder(dividingBy: Float(max(1, sLen)))
            absPos = Float(sStart) + max(0.0, wrapped)
        }
        let i0 = max(0, min(sampleCount - 2, Int(absPos)))
        let f = absPos - Float(i0)
        return ptr[i0] * (1.0 - f) + ptr[i0 + 1] * f
    }

    @inline(__always)
    mutating func nextSample(targetRPM: Float, outSR: Float, pitchScale: Float) -> Float {
        guard samples != nil, grainCount > 0 else { return 0.0 }

        v0Phase += 1.0 / max(24.0, v0WinLen)
        v0SrcPos += v0Step
        if v0Phase >= 1.0 {
            spawnVoice(slot: 0, targetRPM: targetRPM, outSR: outSR, pitchScale: pitchScale)
        }

        v1Phase += 1.0 / max(24.0, v1WinLen)
        v1SrcPos += v1Step
        if v1Phase >= 1.0 {
            spawnVoice(slot: 1, targetRPM: targetRPM, outSR: outSR, pitchScale: pitchScale)
        }

        // Hann windows at 50% phase offset sum cleanly to 1.0 (zero grain boundary clicks!)
        let w0 = 0.5 * (1.0 - cosf(2.0 * Float.pi * v0Phase))
        let w1 = 0.5 * (1.0 - cosf(2.0 * Float.pi * v1Phase))
        let s0 = readSample(start: v0Start, len: v0Len, relPos: v0SrcPos)
        let s1 = readSample(start: v1Start, len: v1Len, relPos: v1SrcPos)
        let wSum = max(0.5, w0 + w1)
        return (s0 * w0 + s1 * w1) / wSum
    }
}

/// Real-time POD looped/one-shot SFX voice (no Swift allocations).
private struct SFXVoiceC {
    var samples: UnsafePointer<Float>? = nil
    var count: Int = 0
    var sampleRate: Float = 24000.0
    var loopStart: Int = 0
    var loopEnd: Int = 0
    var looped: Bool = false
    var active: Bool = false
    var pos: Float = 0.0
    var gain: Float = 1.0

    mutating func bind(_ buf: RawPCMBuffer?, loopedOverride: Bool? = nil) {
        guard let b = buf else {
            samples = nil
            count = 0
            active = false
            return
        }
        samples = UnsafePointer(b.storage)
        count = b.count
        sampleRate = b.sampleRate
        loopStart = b.loopStart
        loopEnd = b.loopEnd
        looped = loopedOverride ?? b.looped
        pos = 0.0
        active = looped
    }

    @inline(__always)
    mutating func trigger(gain g: Float = 1.0) {
        guard samples != nil, count > 2 else { return }
        pos = 0.0
        gain = g
        active = true
    }

    @inline(__always)
    mutating func nextSample(outSR: Float, pitchRatio: Float) -> Float {
        guard active, let ptr = samples, count > 2 else { return 0.0 }
        let i0 = Int(pos)
        if i0 >= count - 1 {
            if looped {
                pos = Float(loopStart)
            } else {
                active = false
                return 0.0
            }
        }
        let idx = max(0, min(count - 2, Int(pos)))
        let f = pos - Float(idx)
        let val = ptr[idx] * (1.0 - f) + ptr[idx + 1] * f
        pos += (sampleRate / outSR) * max(0.25, min(2.6, pitchRatio))
        if looped && Int(pos) >= loopEnd {
            let span = Float(max(1, loopEnd - loopStart))
            pos -= span
        } else if !looped && Int(pos) >= count - 1 {
            active = false
        }
        return val * gain
    }
}

/// Heap-allocated C-safe DSP state box shared exclusively with the audio render callback.
private final class DSPStateBox {
    // Double-buffered control state for lock-free main -> audio handoff
    var ctrl0 = RTAudioControlState()
    var ctrl1 = RTAudioControlState()
    var activeCtrlIndex: Int32 = 0 // 0 or 1

    // Engine switching flag (atomic-style guard so swap never races callback)
    var engineMutedForSwap: Int32 = 0

    // Ginsu accel + decel layers
    var engineAccel = GinsuLayerC()
    var engineDecel = GinsuLayerC()
    var engineAccelVol: Float = 0.80
    var engineDecelVol: Float = 0.65

    // Looped SFX voices
    var whineVoice = SFXVoiceC()
    var turboSpoolVoice = SFXVoiceC()
    var nosLoopVoice = SFXVoiceC()
    var skidVoice = SFXVoiceC()
    var scrapeVoice = SFXVoiceC()
    var rainVoice = SFXVoiceC()
    var windVoice = SFXVoiceC()
    var sirenWailVoice = SFXVoiceC()
    var sirenYelpVoice = SFXVoiceC()
    var sirenPhaserVoice = SFXVoiceC()

    // One-shot SFX voices
    var shiftPop1Voice = SFXVoiceC()
    var shiftPop2Voice = SFXVoiceC()
    var turboBovVoice = SFXVoiceC()
    var nosIgniteVoice = SFXVoiceC()
    var nosPurgeVoice = SFXVoiceC()
    var impactHardVoice = SFXVoiceC()
    var impactMedVoice = SFXVoiceC()
    var speedbreakerVoice = SFXVoiceC()
    var radarPingVoice = SFXVoiceC()

    // Smoothed DSP envelopes & last-seen event sequence counters
    var smoothRPM: Float = 950.0
    var smoothThrottle: Float = 0.0
    var smoothNosGain: Float = 0.0
    var smoothSkidGain: Float = 0.0
    var smoothWhineGain: Float = 0.0
    var smoothSirenGain: Float = 0.0
    var lastShiftSeq: UInt32 = 0
    var lastBovSeq: UInt32 = 0
    var lastNosIgniteSeq: UInt32 = 0
    var lastNosPurgeSeq: UInt32 = 0
    var lastImpactSeq: UInt32 = 0
    var lastSBSeq: UInt32 = 0
    var lastRadarSeq: UInt32 = 0
    var shiftAltToggle: Bool = false
}

// MARK: - Native Apple AVAudioEngine Controller (Ginsu PSOLA + PS2 SFX + EA TRAX + Tunnel Reverb)

public final class RTAudioEngine {
    private let audioEngine = AVAudioEngine()
    private let masterEQ = AVAudioUnitEQ(numberOfBands: 1)
    private let musicPlayerNode = AVAudioPlayerNode()
    private let musicMixer = AVAudioMixerNode()
    private let acousticBusMixer = AVAudioMixerNode()
    private let tunnelReverb = AVAudioUnitReverb()
    private let tunnelEQ = AVAudioUnitEQ(numberOfBands: 2)
    private var sourceNode: AVAudioSourceNode?
    private var graphAttached: Bool = false
    private var connectedMusicSampleRate: Double = 0.0
    private var connectedMusicChannels: AVAudioChannelCount = 0
    private let dsp = DSPStateBox()

    // Retained lifetime storage of decoded float PCM buffers
    private var engineAccelBanks: [String: RawGinsuBank] = [:]
    private var engineDecelBanks: [String: RawGinsuBank] = [:]
    private var engineMeta: [String: (trannyWhine: Bool, volAccel: Float, volDecel: Float)] = [:]
    private var sfxBuffers: [String: RawPCMBuffer] = [:]

    private var control = RTAudioControlState()
    private var prevNosActive: Bool = false
    private var prevSpeedbreakerActive: Bool = false
    private var currentEngineNode: String = ""
    private let isHeadless: Bool

    // Thread-safe EA TRAX & Acoustic Tunnel Reverb state (NEVER locked inside CoreAudio RT callback)
    private let musicLock = NSLock()
    private let musicCompletionQueue = DispatchQueue(label: "rockport2026.audio.music", qos: .userInitiated)
    private var musicTracks: [MusicTrackRecord] = []
    private var licensedIndices: [Int] = []
    private var pursuitIndices: [Int] = []
    private var activeTrackGlobalIndex: Int = -1
    private var savedLicensedGlobalIndex: Int = -1
    private var savedPursuitGlobalIndex: Int = -1
    private var activeAudioFile: AVAudioFile?
    private var playbackGeneration: UInt64 = 0
    private var pursuitModeActive: Bool = false
    private var musicMutedFlag: Bool = false
    private var baseMusicVolume: Float = 0.44
    private var currentSpeedbreakerBlend: Float = 0.0
    private var stingerDuckEndTime: CFAbsoluteTime = 0.0
    private var stingerDuckFactor: Float = 1.0
    private var externalStingerDuck: Float = 0.0
    private var currentTunnelOcclusion: Float = 0.0
    private var currentReverbWetDry: Float = 4.0

    public private(set) var isRunning: Bool = false
    public private(set) var isPackLoaded: Bool = false
    public private(set) var loadedEngineCount: Int = 0
    public private(set) var loadedSFXCount: Int = 0
    public private(set) var isMusicPackLoaded: Bool = false
    public private(set) var loadedMusicTrackCount: Int = 0

    public var isMuted: Bool = false {
        didSet {
            control.muted = isMuted ? 1 : 0
            publishControl()
            musicLock.lock()
            applyMusicVolumeLocked()
            musicLock.unlock()
        }
    }

    public var isMusicMuted: Bool {
        get {
            musicLock.lock()
            defer { musicLock.unlock() }
            return musicMutedFlag
        }
        set {
            musicLock.lock()
            musicMutedFlag = newValue
            applyMusicVolumeLocked()
            musicLock.unlock()
        }
    }

    public var currentMusicTrack: RTMusicTrackInfo? {
        musicLock.lock()
        defer { musicLock.unlock() }
        return buildTrackInfoLocked(globalIndex: activeTrackGlobalIndex)
    }

    public var isPursuitMusicMode: Bool {
        musicLock.lock()
        defer { musicLock.unlock() }
        return pursuitModeActive
    }

    public var tunnelOcclusion: Float {
        musicLock.lock()
        defer { musicLock.unlock() }
        return currentTunnelOcclusion
    }

    public var reverbWetDryMix: Float {
        musicLock.lock()
        defer { musicLock.unlock() }
        return currentReverbWetDry
    }

    public init(packDir: URL? = nil, musicPackDir: URL? = nil, headless: Bool = false, musicEnabled: Bool = true) {
        self.isHeadless = headless

        // Load lightweight EA TRAX manifest metadata even in headless mode (zero audio hardware or AVAudioFile opens)
        // so `--show-trax` / `--music-track` HUD metadata works cleanly while audio stays a 100% silent no-op.
        if musicEnabled {
            let musicCandidates: [URL] = musicPackDir.map { [$0] } ?? [
                URL(fileURLWithPath: "build/music_pack"),
                URL(fileURLWithPath: "../build/music_pack")
            ]

            for dir in musicCandidates {
                let mf = dir.appendingPathComponent("manifest.json")
                if FileManager.default.fileExists(atPath: mf.path) {
                    if loadMusicManifest(from: dir) {
                        isMusicPackLoaded = true
                        break
                    }
                }
            }
        }

        if headless {
            return
        }

        let candidates: [URL] = packDir.map { [$0] } ?? [
            URL(fileURLWithPath: "build/gameplay_pack"),
            URL(fileURLWithPath: "../build/gameplay_pack")
        ]

        for dir in candidates {
            let mf = dir.appendingPathComponent("audio/audio_manifest.json")
            if FileManager.default.fileExists(atPath: mf.path) {
                if loadPack(from: dir) {
                    isPackLoaded = true
                    break
                }
            }
        }
    }

    deinit {
        stop()
    }

    /// Starts AVAudioEngine when a gameplay audio pack or music pack is loaded (silent no-op if unavailable or headless).
    public func start(initialEngineNode: String = "tvr_cerb") {
        guard !isHeadless, (isPackLoaded || isMusicPackLoaded), !isRunning else { return }

        if isPackLoaded {
            selectEngine(node: initialEngineNode)
            bindSFXVoices()
        }

        // Configure Speedbreaker low-pass filter on engine+SFX bus (20 kHz normal -> 1.4 kHz Speedbreaker)
        let sfxBand = masterEQ.bands[0]
        sfxBand.filterType = .lowPass
        sfxBand.frequency = 20000.0
        sfxBand.bandwidth = 0.7
        sfxBand.bypass = false

        // Configure Acoustic Tunnel Reverb (largeHall preset; 4% open sky -> 42% tunnel ceiling)
        tunnelReverb.loadFactoryPreset(.largeHall)

        // Configure Acoustic Tunnel EQ (band 0: subtle ceiling low-pass; band 1: tunnel bass warmth shelf)
        let lpBand = tunnelEQ.bands[0]
        lpBand.filterType = .lowPass
        lpBand.frequency = 18500.0
        lpBand.bandwidth = 0.8
        lpBand.bypass = false

        let bassBand = tunnelEQ.bands[1]
        bassBand.filterType = .lowShelf
        bassBand.frequency = 160.0
        bassBand.gain = 0.0
        bassBand.bypass = false

        let outSR: Double = 48000.0
        guard let format = AVAudioFormat(standardFormatWithSampleRate: outSR, channels: 2) else { return }

        if !graphAttached {
            audioEngine.attach(masterEQ)
            audioEngine.attach(musicPlayerNode)
            audioEngine.attach(musicMixer)
            audioEngine.attach(acousticBusMixer)
            audioEngine.attach(tunnelReverb)
            audioEngine.attach(tunnelEQ)
            graphAttached = true
        }

        if isPackLoaded {
            let dspRef = self.dsp
            let srF = Float(outSR)

            let node = AVAudioSourceNode(format: format) { _, _, frameCount, audioBufferList -> OSStatus in
                let abl = UnsafeMutableAudioBufferListPointer(audioBufferList)
                guard abl.count >= 2,
                      let outL = abl[0].mData?.assumingMemoryBound(to: Float.self),
                      let outR = abl[1].mData?.assumingMemoryBound(to: Float.self) else {
                    return noErr
                }

                // Lock-free snapshot of latest control block
                let idx = dspRef.activeCtrlIndex
                let c = (idx == 0) ? dspRef.ctrl0 : dspRef.ctrl1
                let nFrames = Int(frameCount)

                if c.muted != 0 || dspRef.engineMutedForSwap != 0 {
                    for i in 0..<nFrames {
                        outL[i] = 0.0
                        outR[i] = 0.0
                    }
                    return noErr
                }

                // Consume one-shot event counters (edge-triggered)
                if c.shiftPopSeq != dspRef.lastShiftSeq {
                    dspRef.lastShiftSeq = c.shiftPopSeq
                    dspRef.shiftAltToggle.toggle()
                    if dspRef.shiftAltToggle {
                        dspRef.shiftPop1Voice.trigger(gain: 0.42)
                    } else {
                        dspRef.shiftPop2Voice.trigger(gain: 0.42)
                    }
                }
                if c.bovSeq != dspRef.lastBovSeq {
                    dspRef.lastBovSeq = c.bovSeq
                    dspRef.turboBovVoice.trigger(gain: 0.38)
                }
                if c.nosIgniteSeq != dspRef.lastNosIgniteSeq {
                    dspRef.lastNosIgniteSeq = c.nosIgniteSeq
                    dspRef.nosIgniteVoice.trigger(gain: 0.46)
                }
                if c.nosPurgeSeq != dspRef.lastNosPurgeSeq {
                    dspRef.lastNosPurgeSeq = c.nosPurgeSeq
                    dspRef.nosPurgeVoice.trigger(gain: 0.34)
                }
                if c.impactSeq != dspRef.lastImpactSeq {
                    dspRef.lastImpactSeq = c.impactSeq
                    if c.impactGain > 0.55 {
                        dspRef.impactHardVoice.trigger(gain: min(0.85, 0.35 + c.impactGain * 0.55))
                    } else {
                        dspRef.impactMedVoice.trigger(gain: min(0.70, 0.25 + c.impactGain * 0.45))
                    }
                }
                if c.speedbreakerSeq != dspRef.lastSBSeq {
                    dspRef.lastSBSeq = c.speedbreakerSeq
                    dspRef.speedbreakerVoice.trigger(gain: 0.50)
                }
                if c.radarPingSeq != dspRef.lastRadarSeq {
                    dspRef.lastRadarSeq = c.radarPingSeq
                    dspRef.radarPingVoice.trigger(gain: 0.35)
                }

                // Speedbreaker pitch bend (1.0 -> 0.58x)
                let sbPitch = 1.0 - 0.42 * c.speedbreakerBlend
                let speedFrac = max(0.0, min(1.0, c.speedKmh / 260.0))

                // Equal-power stereo pan for nearest pursuing police siren
                let panAngle = (max(-1.0, min(1.0, c.sirenPan)) + 1.0) * 0.25 * Float.pi
                let sirenL = cosf(panAngle)
                let sirenR = sinf(panAngle)

                let rpmAlpha: Float = 0.0025
                let envAlpha: Float = 0.0018

                for i in 0..<nFrames {
                    dspRef.smoothRPM += (c.rpm - dspRef.smoothRPM) * rpmAlpha
                    dspRef.smoothThrottle += (c.throttle - dspRef.smoothThrottle) * envAlpha
                    dspRef.smoothNosGain += (c.nosActive - dspRef.smoothNosGain) * envAlpha
                    dspRef.smoothSkidGain += (c.driftIntensity - dspRef.smoothSkidGain) * envAlpha
                    dspRef.smoothSirenGain += (c.sirenGain - dspRef.smoothSirenGain) * envAlpha

                    let targetWhine = c.trannyWhineEnabled * speedFrac * (0.35 + 0.65 * dspRef.smoothThrottle)
                    dspRef.smoothWhineGain += (targetWhine - dspRef.smoothWhineGain) * envAlpha

                    // 1. Ginsu PSOLA Granular Engine (Accel + Decel load crossfade)
                    let accelSamp = dspRef.engineAccel.nextSample(targetRPM: dspRef.smoothRPM, outSR: srF, pitchScale: sbPitch)
                    let decelSamp = dspRef.engineDecel.nextSample(targetRPM: dspRef.smoothRPM, outSR: srF, pitchScale: sbPitch)
                    let load = max(0.0, min(1.0, dspRef.smoothThrottle * 1.15))
                    let engineMix = (accelSamp * (0.28 + 0.72 * load) * dspRef.engineAccelVol
                                   + decelSamp * (1.0 - load) * 0.65 * dspRef.engineDecelVol) * 0.52

                    // 2. BMW M3 GTR Straight-Cut Gearbox Whine (pitch tracks driveshaft speed!)
                    var whineMix: Float = 0.0
                    if dspRef.smoothWhineGain > 0.004 {
                        let whinePitch = (0.52 + 1.38 * speedFrac) * sbPitch
                        whineMix = dspRef.whineVoice.nextSample(outSR: srF, pitchRatio: whinePitch) * dspRef.smoothWhineGain * 0.28
                    }

                    // 3. Turbo Spool + BOV Flutter
                    var turboMix: Float = 0.0
                    if c.turboBoost > 0.05 {
                        let turboPitch = (0.65 + 0.85 * c.turboBoost) * sbPitch
                        turboMix += dspRef.turboSpoolVoice.nextSample(outSR: srF, pitchRatio: turboPitch) * c.turboBoost * 0.16
                    }
                    turboMix += dspRef.turboBovVoice.nextSample(outSR: srF, pitchRatio: sbPitch)

                    // 4. NOS Roar + Ignite/Purge
                    var nosMix: Float = 0.0
                    if dspRef.smoothNosGain > 0.005 {
                        nosMix += dspRef.nosLoopVoice.nextSample(outSR: srF, pitchRatio: (0.95 + 0.20 * speedFrac) * sbPitch) * dspRef.smoothNosGain * 0.42
                    }
                    nosMix += dspRef.nosIgniteVoice.nextSample(outSR: srF, pitchRatio: sbPitch)
                    nosMix += dspRef.nosPurgeVoice.nextSample(outSR: srF, pitchRatio: sbPitch)

                    // 5. Tire Screech / Drift Squeal
                    var skidMix: Float = 0.0
                    if dspRef.smoothSkidGain > 0.01 {
                        let skidPitch = (0.86 + 0.28 * dspRef.smoothSkidGain) * sbPitch
                        skidMix = dspRef.skidVoice.nextSample(outSR: srF, pitchRatio: skidPitch) * dspRef.smoothSkidGain * 0.34
                    }

                    // 6. One-shots (Gearshifts, Wall Impacts, Scrape, Speedbreaker, Radar)
                    var fxMix: Float = 0.0
                    fxMix += dspRef.shiftPop1Voice.nextSample(outSR: srF, pitchRatio: sbPitch)
                    fxMix += dspRef.shiftPop2Voice.nextSample(outSR: srF, pitchRatio: sbPitch)
                    fxMix += dspRef.impactHardVoice.nextSample(outSR: srF, pitchRatio: sbPitch)
                    fxMix += dspRef.impactMedVoice.nextSample(outSR: srF, pitchRatio: sbPitch)
                    fxMix += dspRef.speedbreakerVoice.nextSample(outSR: srF, pitchRatio: 1.0)
                    fxMix += dspRef.radarPingVoice.nextSample(outSR: srF, pitchRatio: 1.0)
                    if c.scrapeIntensity > 0.02 {
                        fxMix += dspRef.scrapeVoice.nextSample(outSR: srF, pitchRatio: (0.85 + 0.35 * speedFrac) * sbPitch) * c.scrapeIntensity * 0.30
                    }

                    // 7. Ambient Weather & High-Speed Wind Bed
                    var envMix: Float = 0.0
                    if c.rainIntensity > 0.05 {
                        envMix += dspRef.rainVoice.nextSample(outSR: srF, pitchRatio: sbPitch) * c.rainIntensity * 0.14
                    }
                    if speedFrac > 0.25 {
                        let windGain = (speedFrac - 0.25) / 0.75
                        envMix += dspRef.windVoice.nextSample(outSR: srF, pitchRatio: (0.8 + 0.45 * speedFrac) * sbPitch) * windGain * 0.18
                    }

                    // 8. 3D Stereo-Panned Doppler Police Siren
                    var sirenVal: Float = 0.0
                    if dspRef.smoothSirenGain > 0.005 {
                        let sPitch = c.sirenDoppler * sbPitch
                        switch c.sirenMode {
                        case 1:  sirenVal = dspRef.sirenYelpVoice.nextSample(outSR: srF, pitchRatio: sPitch)
                        case 2:  sirenVal = dspRef.sirenPhaserVoice.nextSample(outSR: srF, pitchRatio: sPitch)
                        default: sirenVal = dspRef.sirenWailVoice.nextSample(outSR: srF, pitchRatio: sPitch)
                        }
                        sirenVal *= dspRef.smoothSirenGain * 0.42
                    }

                    let mono = engineMix + whineMix + turboMix + nosMix + skidMix + fxMix + envMix
                    let rawL = (mono + sirenVal * sirenL) * c.masterGain
                    let rawR = (mono + sirenVal * sirenR) * c.masterGain

                    // Soft tanh limiter: warm analog saturation, zero digital clipping
                    outL[i] = tanhf(rawL)
                    outR[i] = tanhf(rawR)
                }
                return noErr
            }

            self.sourceNode = node
            audioEngine.attach(node)
            audioEngine.connect(node, to: masterEQ, format: format)
            audioEngine.connect(masterEQ, to: acousticBusMixer, format: format)
        }

        // Probe preferred EA TRAX .m4a format (32 kHz stereo) so AVAudioPlayerNode matches file.processingFormat
        let musicFormat = probeInitialMusicFormat() ?? AVAudioFormat(standardFormatWithSampleRate: 32000.0, channels: 2) ?? format
        connectedMusicSampleRate = musicFormat.sampleRate
        connectedMusicChannels = musicFormat.channelCount

        audioEngine.connect(musicPlayerNode, to: musicMixer, format: musicFormat)
        audioEngine.connect(musicMixer, to: acousticBusMixer, format: format)
        audioEngine.connect(acousticBusMixer, to: tunnelReverb, format: format)
        audioEngine.connect(tunnelReverb, to: tunnelEQ, format: format)
        audioEngine.connect(tunnelEQ, to: audioEngine.mainMixerNode, format: format)

        musicLock.lock()
        applyTunnelAcousticsLocked(occlusion: currentTunnelOcclusion)
        applyMusicVolumeLocked()
        musicLock.unlock()

        do {
            try audioEngine.start()
            isRunning = true

            // Kick off background EA TRAX streaming if music pack is present
            musicLock.lock()
            if !musicTracks.isEmpty && activeTrackGlobalIndex >= 0 {
                scheduleAndPlayTrackLocked(globalIndex: activeTrackGlobalIndex)
            }
            musicLock.unlock()
        } catch {
            // Graceful silent fallback when audio hardware is unavailable (e.g. headless CI)
            isRunning = false
        }
    }

    public func stop() {
        musicLock.lock()
        playbackGeneration &+= 1
        if isRunning {
            musicPlayerNode.stop()
        }
        activeAudioFile = nil
        musicLock.unlock()

        guard isRunning else { return }
        audioEngine.stop()
        if let n = sourceNode {
            audioEngine.detach(n)
            sourceNode = nil
        }
        if graphAttached {
            audioEngine.detach(masterEQ)
            audioEngine.detach(musicPlayerNode)
            audioEngine.detach(musicMixer)
            audioEngine.detach(acousticBusMixer)
            audioEngine.detach(tunnelReverb)
            audioEngine.detach(tunnelEQ)
            graphAttached = false
        }
        isRunning = false
    }

    // MARK: - EA TRAX Music Playback & Acoustic Tunnel Reverb API

    @discardableResult
    public func nextMusicTrack() -> RTMusicTrackInfo? {
        musicLock.lock()
        defer { musicLock.unlock() }
        return stepMusicTrackLocked(delta: 1)
    }

    @discardableResult
    public func prevMusicTrack() -> RTMusicTrackInfo? {
        musicLock.lock()
        defer { musicLock.unlock() }
        return stepMusicTrackLocked(delta: -1)
    }

    @discardableResult
    public func toggleMusicMute() -> Bool {
        musicLock.lock()
        defer { musicLock.unlock() }
        musicMutedFlag.toggle()
        applyMusicVolumeLocked()
        return musicMutedFlag
    }

    @discardableResult
    public func selectMusicTrack(index: Int) -> RTMusicTrackInfo? {
        musicLock.lock()
        defer { musicLock.unlock() }
        guard !musicTracks.isEmpty else { return nil }
        let count = musicTracks.count
        let wrapped = ((index % count) + count) % count
        let rec = musicTracks[wrapped]
        activeTrackGlobalIndex = wrapped
        if rec.isPursuit {
            pursuitModeActive = true
            savedPursuitGlobalIndex = wrapped
        } else {
            pursuitModeActive = false
            savedLicensedGlobalIndex = wrapped
        }
        if isRunning && !isHeadless {
            scheduleAndPlayTrackLocked(globalIndex: wrapped)
        }
        return buildTrackInfoLocked(globalIndex: wrapped)
    }

    /// Smoothly modulates tunnel/overpass acoustic reverb (4% open sky -> 42% tunnel)
    /// and subtle ceiling low-pass + bass warmth EQ.
    public func setTunnelOcclusion(_ occlusion: Float) {
        let clamped = max(0.0, min(1.0, occlusion.isFinite ? occlusion : 0.0))
        musicLock.lock()
        currentTunnelOcclusion = clamped
        applyTunnelAcousticsLocked(occlusion: clamped)
        musicLock.unlock()
    }

    /// Switches between licensed EA TRAX (free-roam/race) and Paul Linford & Chris Vrenna
    /// interactive pursuit score tracks when entering or escaping a police pursuit.
    public func setPursuitMusicMode(_ active: Bool) {
        musicLock.lock()
        defer { musicLock.unlock() }
        guard active != pursuitModeActive else { return }
        pursuitModeActive = active
        guard !musicTracks.isEmpty else { return }

        let targetPool = active ? pursuitIndices : licensedIndices
        guard !targetPool.isEmpty else { return }

        let chosenGlobal: Int
        if active {
            if savedPursuitGlobalIndex >= 0 && targetPool.contains(savedPursuitGlobalIndex) {
                chosenGlobal = savedPursuitGlobalIndex
            } else {
                chosenGlobal = targetPool[0]
            }
            savedPursuitGlobalIndex = chosenGlobal
        } else {
            if savedLicensedGlobalIndex >= 0 && targetPool.contains(savedLicensedGlobalIndex) {
                chosenGlobal = savedLicensedGlobalIndex
            } else {
                chosenGlobal = targetPool[0]
            }
            savedLicensedGlobalIndex = chosenGlobal
        }

        if activeTrackGlobalIndex != chosenGlobal {
            activeTrackGlobalIndex = chosenGlobal
            if isRunning && !isHeadless {
                scheduleAndPlayTrackLocked(globalIndex: chosenGlobal)
            }
        }
    }

    /// Temporarily ducks background EA TRAX during Busted/Evaded/Event finish stingers.
    public func triggerStingerDuck(durationSeconds: Float = 3.5, duckFactor: Float = 0.25) {
        musicLock.lock()
        let dur = Double(max(0.1, min(15.0, durationSeconds)))
        stingerDuckFactor = max(0.05, min(1.0, duckFactor))
        stingerDuckEndTime = CFAbsoluteTimeGetCurrent() + dur
        applyMusicVolumeLocked()
        musicLock.unlock()
    }

    private func activePoolLocked() -> [Int] {
        if pursuitModeActive && !pursuitIndices.isEmpty {
            return pursuitIndices
        }
        if !licensedIndices.isEmpty {
            return licensedIndices
        }
        return Array(0..<musicTracks.count)
    }

    private func buildTrackInfoLocked(globalIndex: Int) -> RTMusicTrackInfo? {
        guard globalIndex >= 0 && globalIndex < musicTracks.count else { return nil }
        let rec = musicTracks[globalIndex]
        let pool = rec.isPursuit ? (pursuitIndices.isEmpty ? Array(0..<musicTracks.count) : pursuitIndices)
                                 : (licensedIndices.isEmpty ? Array(0..<musicTracks.count) : licensedIndices)
        let poolPos = pool.firstIndex(of: globalIndex) ?? globalIndex
        return RTMusicTrackInfo(
            index: poolPos,
            total: pool.count,
            artist: rec.artist,
            title: rec.title,
            isPursuit: rec.isPursuit
        )
    }

    private func stepMusicTrackLocked(delta: Int) -> RTMusicTrackInfo? {
        let pool = activePoolLocked()
        guard !pool.isEmpty else { return nil }
        let curPos = pool.firstIndex(of: activeTrackGlobalIndex) ?? 0
        let nextPos = ((curPos + delta) % pool.count + pool.count) % pool.count
        let nextGlobal = pool[nextPos]
        activeTrackGlobalIndex = nextGlobal
        if pursuitModeActive {
            savedPursuitGlobalIndex = nextGlobal
        } else {
            savedLicensedGlobalIndex = nextGlobal
        }
        if isRunning && !isHeadless {
            scheduleAndPlayTrackLocked(globalIndex: nextGlobal)
        }
        return buildTrackInfoLocked(globalIndex: nextGlobal)
    }

    private func applyTunnelAcousticsLocked(occlusion: Float) {
        let clamped = max(0.0, min(1.0, occlusion))
        let wetDry: Float = 4.0 + 38.0 * clamped        // 4% open sky -> 42% inside tunnel/overpass
        let lpFreq: Float = 18500.0 - 14000.0 * clamped // 18.5 kHz -> 4.5 kHz ceiling dampening
        let bassGainDb: Float = 3.5 * clamped           // 0 dB -> +3.5 dB tunnel bass warmth @ 160 Hz
        currentReverbWetDry = wetDry

        if !isHeadless {
            tunnelReverb.wetDryMix = wetDry
            if tunnelEQ.bands.count >= 2 {
                tunnelEQ.bands[0].frequency = max(2500.0, min(20000.0, lpFreq))
                tunnelEQ.bands[1].gain = bassGainDb
            }
        }
    }

    private func applyMusicVolumeLocked() {
        guard !isHeadless else { return }
        if isMuted || musicMutedFlag {
            musicMixer.outputVolume = 0.0
            return
        }
        // Auto-duck to 0.45x during Speedbreaker
        let sbDuck: Float = 1.0 - 0.55 * max(0.0, min(1.0, currentSpeedbreakerBlend))

        // Auto-duck during Busted/Evaded stingers (timed or frame-driven)
        let now = CFAbsoluteTimeGetCurrent()
        var timedDuck: Float = 1.0
        if now < stingerDuckEndTime {
            let rem = Float(stingerDuckEndTime - now)
            if rem < 0.6 {
                let t = max(0.0, min(1.0, rem / 0.6))
                timedDuck = 1.0 - (1.0 - stingerDuckFactor) * t
            } else {
                timedDuck = stingerDuckFactor
            }
        }
        let extDuck: Float = 1.0 - 0.75 * max(0.0, min(1.0, externalStingerDuck))
        let effectiveDuck = min(sbDuck, min(timedDuck, extDuck))
        musicMixer.outputVolume = max(0.0, min(1.0, baseMusicVolume * effectiveDuck))
    }

    private func probeInitialMusicFormat() -> AVAudioFormat? {
        musicLock.lock()
        let firstURL = (activeTrackGlobalIndex >= 0 && activeTrackGlobalIndex < musicTracks.count)
            ? musicTracks[activeTrackGlobalIndex].fileURL
            : musicTracks.first?.fileURL
        musicLock.unlock()

        guard let url = firstURL, let file = try? AVAudioFile(forReading: url) else {
            return nil
        }
        return file.processingFormat
    }

    /// Must be called while holding `musicLock` and only when `isRunning && !isHeadless`.
    private func scheduleAndPlayTrackLocked(globalIndex: Int) {
        guard isRunning, !isHeadless, globalIndex >= 0, globalIndex < musicTracks.count else { return }
        let rec = musicTracks[globalIndex]
        guard FileManager.default.fileExists(atPath: rec.fileURL.path),
              let audioFile = try? AVAudioFile(forReading: rec.fileURL) else {
            return
        }

        playbackGeneration &+= 1
        let expectedGen = playbackGeneration

        musicPlayerNode.stop()

        // Ensure player node connection format matches file.processingFormat (handles any non-32kHz files safely)
        let fileFmt = audioFile.processingFormat
        if abs(fileFmt.sampleRate - connectedMusicSampleRate) > 1.0 || fileFmt.channelCount != connectedMusicChannels {
            audioEngine.disconnectNodeOutput(musicPlayerNode)
            audioEngine.connect(musicPlayerNode, to: musicMixer, format: fileFmt)
            connectedMusicSampleRate = fileFmt.sampleRate
            connectedMusicChannels = fileFmt.channelCount
        }

        activeAudioFile = audioFile
        applyMusicVolumeLocked()

        musicPlayerNode.scheduleFile(audioFile, at: nil, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self = self else { return }
            self.musicCompletionQueue.async {
                self.musicLock.lock()
                defer { self.musicLock.unlock() }
                guard self.isRunning, !self.isHeadless, self.playbackGeneration == expectedGen else { return }
                _ = self.stepMusicTrackLocked(delta: 1)
            }
        }
        musicPlayerNode.play()
    }

    // MARK: - Engine Bank Selection & Frame Telemetry Update

    /// Hot-swaps the active Ginsu engine accel/decel bank for a newly selected garage car.
    public func selectEngine(node rawNode: String, trannyWhineOverride: Bool? = nil) {
        let key = engineAccelBanks[rawNode] != nil ? rawNode : fallbackEngineNode(for: rawNode)
        guard key != currentEngineNode || dsp.engineAccel.sampleCount == 0 else { return }
        currentEngineNode = key

        dsp.engineMutedForSwap = 1
        let acc = engineAccelBanks[key]
        let dec = engineDecelBanks[key] ?? acc
        let meta = engineMeta[key] ?? (trannyWhine: false, volAccel: 0.80, volDecel: 0.65)
        dsp.engineAccel.bind(acc)
        dsp.engineDecel.bind(dec)
        dsp.engineAccelVol = meta.volAccel
        dsp.engineDecelVol = meta.volDecel
        control.trannyWhineEnabled = (trannyWhineOverride ?? meta.trannyWhine) ? 1.0 : 0.0
        publishControl()
        dsp.engineMutedForSwap = 0
    }

    /// Updates real-time engine, SFX, weather, 3D police siren, and music auto-ducking once per display frame.
    func update(
        telemetry: RTDriveTelemetry,
        rainIntensity: Float = 0.0,
        sirenGain: Float = 0.0,
        sirenPan: Float = 0.0,
        sirenDoppler: Float = 1.0,
        sirenMode: Int32 = 0,
        radarPing: Bool = false,
        stingerDuck: Float = 0.0
    ) {
        guard isRunning else { return }

        control.rpm = telemetry.rpm
        control.throttle = telemetry.speedKmh > 1.0 ? max(0.0, min(1.0, telemetry.rpmNorm * 0.9 + (telemetry.nosActive ? 0.3 : 0.0))) : 0.0
        control.speedKmh = telemetry.speedKmh
        control.driftIntensity = telemetry.driftIntensity
        control.turboBoost = telemetry.turboBoostNorm
        control.nosActive = telemetry.nosActive ? 1.0 : 0.0
        control.speedbreakerBlend = telemetry.speedbreakerBlend
        control.scrapeIntensity = telemetry.scrapeIntensity
        control.rainIntensity = rainIntensity
        control.sirenGain = sirenGain
        control.sirenPan = sirenPan
        control.sirenDoppler = sirenDoppler
        control.sirenMode = sirenMode

        if telemetry.shiftPopTrigger { control.shiftPopSeq &+= 1 }
        if telemetry.bovTrigger { control.bovSeq &+= 1 }
        if telemetry.nosActive && !prevNosActive { control.nosIgniteSeq &+= 1 }
        if !telemetry.nosActive && prevNosActive { control.nosPurgeSeq &+= 1 }
        if telemetry.impactIntensity > 0.08 {
            control.impactGain = telemetry.impactIntensity
            control.impactSeq &+= 1
        }
        if telemetry.speedbreakerActive && !prevSpeedbreakerActive {
            control.speedbreakerSeq &+= 1
        }
        if radarPing { control.radarPingSeq &+= 1 }

        prevNosActive = telemetry.nosActive
        prevSpeedbreakerActive = telemetry.speedbreakerActive

        // Sweep engine/SFX Speedbreaker low-pass filter on main thread (20 kHz -> 1.4 kHz)
        let targetCutoff = 20000.0 - 18600.0 * telemetry.speedbreakerBlend
        masterEQ.bands[0].frequency = max(1200.0, min(20000.0, targetCutoff))

        // Update Speedbreaker (0.45x) + Busted/Evaded stinger music ducking smoothly
        musicLock.lock()
        currentSpeedbreakerBlend = telemetry.speedbreakerBlend
        externalStingerDuck = stingerDuck
        applyMusicVolumeLocked()
        musicLock.unlock()

        publishControl()
    }

    private func publishControl() {
        let nextIdx: Int32 = (dsp.activeCtrlIndex == 0) ? 1 : 0
        if nextIdx == 0 {
            dsp.ctrl0 = control
        } else {
            dsp.ctrl1 = control
        }
        dsp.activeCtrlIndex = nextIdx
    }

    private func fallbackEngineNode(for requested: String) -> String {
        let lc = requested.lowercased()
        if lc.contains("por") || lc.contains("911") || lc.contains("997") || lc.contains("cayman") {
            if engineAccelBanks["por_996"] != nil { return "por_996" }
        }
        if lc.contains("corv") || lc.contains("viper") || lc.contains("gto") || lc.contains("mus") {
            if engineAccelBanks["corvette_z06_v2"] != nil { return "corvette_z06_v2" }
        }
        if lc.contains("rx") || lc.contains("maz") {
            if engineAccelBanks["maz_rx7"] != nil { return "maz_rx7" }
        }
        if engineAccelBanks["tvr_cerb"] != nil { return "tvr_cerb" }
        return engineAccelBanks.keys.sorted().first ?? ""
    }

    private func bindSFXVoices() {
        dsp.whineVoice.bind(sfxBuffers["gearbox_whine"] ?? sfxBuffers["tranny_whine"], loopedOverride: true)
        dsp.turboSpoolVoice.bind(sfxBuffers["turbo_spool"], loopedOverride: true)
        dsp.turboBovVoice.bind(sfxBuffers["turbo_bov"], loopedOverride: false)
        dsp.nosLoopVoice.bind(sfxBuffers["nos_loop"], loopedOverride: true)
        dsp.nosIgniteVoice.bind(sfxBuffers["nos_ignite"], loopedOverride: false)
        dsp.nosPurgeVoice.bind(sfxBuffers["nos_purge"], loopedOverride: false)
        dsp.skidVoice.bind(sfxBuffers["skid_loop"], loopedOverride: true)
        dsp.scrapeVoice.bind(sfxBuffers["scrape_loop"], loopedOverride: true)
        dsp.rainVoice.bind(sfxBuffers["rain_loop"], loopedOverride: true)
        dsp.windVoice.bind(sfxBuffers["wind_loop"], loopedOverride: true)
        dsp.sirenWailVoice.bind(sfxBuffers["siren_wail"], loopedOverride: true)
        dsp.sirenYelpVoice.bind(sfxBuffers["siren_yelp"], loopedOverride: true)
        dsp.sirenPhaserVoice.bind(sfxBuffers["siren_phaser"], loopedOverride: true)
        dsp.shiftPop1Voice.bind(sfxBuffers["shift_pop_1"], loopedOverride: false)
        dsp.shiftPop2Voice.bind(sfxBuffers["shift_pop_2"], loopedOverride: false)
        dsp.impactHardVoice.bind(sfxBuffers["impact_hard"], loopedOverride: false)
        dsp.impactMedVoice.bind(sfxBuffers["impact_med"], loopedOverride: false)
        dsp.speedbreakerVoice.bind(sfxBuffers["speedbreaker_fx"], loopedOverride: false)
        dsp.radarPingVoice.bind(sfxBuffers["radar_ping"], loopedOverride: false)
    }

    // MARK: - EA TRAX Music Pack Manifest Loading

    private func loadMusicManifest(from musicDir: URL) -> Bool {
        let mfURL = musicDir.appendingPathComponent("manifest.json")
        guard let data = try? Data(contentsOf: mfURL),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let tracksArr = root["tracks"] as? [[String: Any]] else {
            return false
        }

        var loaded: [MusicTrackRecord] = []
        var licIdx: [Int] = []
        var purIdx: [Int] = []

        for item in tracksArr {
            guard let relFile = item["file"] as? String else { continue }
            let fileURL = musicDir.appendingPathComponent(relFile)
            // Keep tracks whose .m4a exists on disk (or accept metadata entries if manifest is synthetic in headless)
            if !isHeadless && !FileManager.default.fileExists(atPath: fileURL.path) {
                continue
            }
            let artist = (item["artist"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "Unknown Artist"
            let title = (item["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "Unknown Track"
            let album = (item["album"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let category = (item["category"] as? String) ?? "ea_trax"
            let isPursuit = (item["is_pursuit"] as? Bool) ?? (category == "pursuit" || category == "score")
            let streamIndex = (item["stream_index"] as? NSNumber)?.intValue ?? loaded.count
            let durationSec = (item["duration_sec"] as? NSNumber)?.doubleValue ?? 0.0

            let globalIdx = loaded.count
            loaded.append(
                MusicTrackRecord(
                    globalIndex: globalIdx,
                    streamIndex: streamIndex,
                    artist: artist,
                    title: title,
                    album: album,
                    category: category,
                    isPursuit: isPursuit,
                    fileURL: fileURL,
                    durationSec: durationSec
                )
            )
            if isPursuit {
                purIdx.append(globalIdx)
            } else {
                licIdx.append(globalIdx)
            }
        }

        guard !loaded.isEmpty else { return false }

        musicLock.lock()
        musicTracks = loaded
        licensedIndices = licIdx
        pursuitIndices = purIdx
        loadedMusicTrackCount = loaded.count
        if let firstLic = licIdx.first {
            activeTrackGlobalIndex = firstLic
            savedLicensedGlobalIndex = firstLic
            pursuitModeActive = false
        } else {
            activeTrackGlobalIndex = 0
            savedPursuitGlobalIndex = 0
            pursuitModeActive = loaded[0].isPursuit
        }
        if let firstPur = purIdx.first {
            savedPursuitGlobalIndex = firstPur
        }
        musicLock.unlock()
        return true
    }

    // MARK: - Pack & WAV Loading (Runs Once on Main Thread)

    private func loadPack(from packDir: URL) -> Bool {
        let mfURL = packDir.appendingPathComponent("audio/audio_manifest.json")
        guard let data = try? Data(contentsOf: mfURL),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return false
        }

        if let engines = root["engines"] as? [String: [String: Any]] {
            for (eaKey, info) in engines {
                let tranny = (info["tranny_whine"] as? Bool) ?? false
                let volA = (info["vol_accel"] as? NSNumber)?.floatValue ?? 0.80
                let volD = (info["vol_decel"] as? NSNumber)?.floatValue ?? 0.65
                engineMeta[eaKey] = (tranny, volA, volD)

                if let acc = info["accel"] as? [String: Any],
                   let bank = loadGinsuBank(dict: acc, packDir: packDir) {
                    engineAccelBanks[eaKey] = bank
                }
                if let dec = info["decel"] as? [String: Any],
                   let bank = loadGinsuBank(dict: dec, packDir: packDir) {
                    engineDecelBanks[eaKey] = bank
                }
            }
        }

        if let sfx = root["sfx"] as? [String: [String: Any]] {
            for (sfxKey, info) in sfx {
                guard let rel = info["wav"] as? String else { continue }
                let wavURL = packDir.appendingPathComponent(rel)
                let looped = (info["looped"] as? Bool) ?? false
                let ls = (info["loop_start"] as? NSNumber)?.intValue ?? 0
                let ll = (info["loop_length"] as? NSNumber)?.intValue ?? 0
                if let buf = Self.loadMonoWAV(url: wavURL, looped: looped, loopStart: ls, loopLength: ll) {
                    sfxBuffers[sfxKey] = buf
                }
            }
        }

        loadedEngineCount = engineAccelBanks.count
        loadedSFXCount = sfxBuffers.count
        return loadedEngineCount > 0 || loadedSFXCount > 0
    }

    private func loadGinsuBank(dict: [String: Any], packDir: URL) -> RawGinsuBank? {
        guard let rel = dict["wav"] as? String else { return nil }
        let wavURL = packDir.appendingPathComponent(rel)
        guard let pcm = Self.loadMonoWAV(url: wavURL, looped: false, loopStart: 0, loopLength: 0) else {
            return nil
        }
        let minRPM = (dict["min_rpm"] as? NSNumber)?.floatValue ?? 1200.0
        let maxRPM = (dict["max_rpm"] as? NSNumber)?.floatValue ?? 8000.0
        let rpmTbl = (dict["rpm_table"] as? [NSNumber])?.map { $0.intValue } ?? []
        let grainTbl = (dict["grain_table"] as? [NSNumber])?.map { $0.intValue } ?? []
        return RawGinsuBank(pcm: pcm, minRPM: minRPM, maxRPM: maxRPM, rpmTable: rpmTbl, grainTable: grainTbl)
    }

    /// Minimal, allocation-light PCM16 mono RIFF/WAVE reader (zero AudioFile overhead).
    private static func loadMonoWAV(url: URL, looped: Bool, loopStart: Int, loopLength: Int) -> RawPCMBuffer? {
        guard let data = try? Data(contentsOf: url), data.count >= 44 else { return nil }
        return data.withUnsafeBytes { rawBuf -> RawPCMBuffer? in
            guard let base = rawBuf.baseAddress else { return nil }
            let bytes = rawBuf.bindMemory(to: UInt8.self)
            guard bytes[0] == 0x52, bytes[1] == 0x49, bytes[2] == 0x46, bytes[3] == 0x46, // RIFF
                  bytes[8] == 0x57, bytes[9] == 0x41, bytes[10] == 0x56, bytes[11] == 0x45 else { // WAVE
                return nil
            }

            var sr: UInt32 = 24000
            var bitsPerSample: UInt16 = 16
            var channels: UInt16 = 1
            var dataOff = 0
            var dataLen = 0
            var off = 12

            while off + 8 <= data.count {
                let c0 = bytes[off], c1 = bytes[off + 1], c2 = bytes[off + 2], c3 = bytes[off + 3]
                var chunkSz: UInt32 = 0
                memcpy(&chunkSz, base.advanced(by: off + 4), 4)
                let sz = Int(UInt32(littleEndian: chunkSz))
                let body = off + 8
                if c0 == 0x66 && c1 == 0x6D && c2 == 0x74 && c3 == 0x20 && body + 16 <= data.count { // "fmt "
                    var ch: UInt16 = 1
                    var rate: UInt32 = 24000
                    var bps: UInt16 = 16
                    memcpy(&ch, base.advanced(by: body + 2), 2)
                    memcpy(&rate, base.advanced(by: body + 4), 4)
                    memcpy(&bps, base.advanced(by: body + 14), 2)
                    channels = UInt16(littleEndian: ch)
                    sr = UInt32(littleEndian: rate)
                    bitsPerSample = UInt16(littleEndian: bps)
                } else if c0 == 0x64 && c1 == 0x61 && c2 == 0x74 && c3 == 0x61 { // "data"
                    dataOff = body
                    dataLen = min(sz, data.count - body)
                    break
                }
                off = body + sz + (sz & 1)
            }

            guard dataOff > 0, dataLen >= 2, bitsPerSample == 16 else { return nil }
            let frameStride = max(1, Int(channels))
            let totalInt16 = dataLen / 2
            let frameCount = totalInt16 / frameStride
            var floats = [Float](repeating: 0.0, count: frameCount)
            let i16Ptr = base.advanced(by: dataOff).assumingMemoryBound(to: Int16.self)
            let invScale: Float = 1.0 / 32768.0
            for i in 0..<frameCount {
                let s = Int16(littleEndian: i16Ptr[i * frameStride])
                floats[i] = Float(s) * invScale
            }
            return RawPCMBuffer(samples: floats, sampleRate: Float(sr), looped: looped, loopStart: loopStart, loopLength: loopLength)
        }
    }
}
