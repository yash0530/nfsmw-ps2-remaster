import Foundation
import Metal
import MetalFX
import MetalKit
import simd

enum RTDenoiseMode: String {
    case metalfx = "metalfx"
    case svgf = "svgf"
    case off = "off"

    static func parse(_ raw: String) -> RTDenoiseMode {
        switch raw.lowercased() {
        case "metalfx", "mfx", "neural": return .metalfx
        case "svgf", "atrous", "atrous4": return .svgf
        case "off", "none", "0": return .off
        default: return .metalfx
        }
    }

    func next() -> RTDenoiseMode {
        switch self {
        case .metalfx: return .svgf
        case .svgf: return .off
        case .off: return .metalfx
        }
    }
}

enum RTCameraMode: String, CaseIterable {
    case hero = "hero"
    case chase = "chase"
    case street = "street"
    case overhead = "overhead"

    static func parse(_ raw: String) -> RTCameraMode {
        switch raw.lowercased() {
        case "chase": return .chase
        case "street": return .street
        case "overhead", "top": return .overhead
        default: return .hero
        }
    }

    func next() -> RTCameraMode {
        let all = Self.allCases
        let idx = all.firstIndex(of: self) ?? 0
        return all[(idx + 1) % all.count]
    }
}

struct RTFrameUniforms {
    var invViewProjMatrix: simd_float4x4
    var currViewProjMatrix: simd_float4x4
    var prevViewProjMatrix: simd_float4x4
    var cameraPos: SIMD4<Float>
    var cameraForward: SIMD4<Float>
    var cameraRight: SIMD4<Float>
    var cameraUp: SIMD4<Float>
    var sunDir: SIMD4<Float>
    var sunColor: SIMD4<Float>
    var coronaColor: SIMD4<Float>
    var skyZenith: SIMD4<Float>
    var skyHorizon: SIMD4<Float>
    var skyGround: SIMD4<Float>
    var neonParams: SIMD4<Float>
    var postParams: SIMD4<Float>
    var rtParams: SIMD4<Float>
    var envParams: SIMD4<Float>
    var gradeParams: SIMD4<Float>
    var carDynParams: SIMD4<Float>
    var driveParams: SIMD4<Float>
    var hudParams: SIMD4<Float>
    var jitterParams: SIMD4<Float>   // x = jitterX, y = jitterY, z = temporal1sppMode (0/1), w = worldTexCount
    var carWorldPos: SIMD4<Float>    // xyz = car world pos, w = hasRkptWorld (0/1)
    var lightGridParams: SIMD4<Float>// xy = world lightGrid originXZ, zw = invCellXZ
    var fxParams: SIMD4<Float>       // x = rainIntensity, y = speedbreaker, z = timeOfDayHours, w = worldLightCount
}

struct RTPhysicsProbeInput {
    var carPos: SIMD4<Float>         // xyz = carPos, w = yawRad
    var carForward: SIMD4<Float>     // xyz = forward, w = halfTrackX
    var carRight: SIMD4<Float>       // xyz = right, w = frontAxleZ
    var dims: SIMD4<Float>           // x = rearAxleZ, y = wheelRadius, z = bodyHalfWidth, w = bodyHalfLength
}

struct RTPhysicsProbeOutput {
    var wheelHitY: SIMD4<Float>      // FL, FR, RL, RR world hit Y (-9999 on miss)
    var wallDist: SIMD4<Float>       // front, rear, left, right dist (999 on miss)
    var wallNormalFront: SIMD4<Float>// xyz = front wall normal, w = valid
    var wallNormalSide: SIMD4<Float> // xyz = nearest side wall normal, w = sideSign (-1 left, +1 right, 0 none)
    var skyProbe: SIMD4<Float>       // x = skyOcclusion (0..1), y = hitDist (999 on miss), z = 0, w = valid
}

// Locked Hour 1-3 ABI Contracts matching RayTracing.metal Buffer Slots 11, 12, 13
struct RTWorldLightGPU {
    var posRadius: SIMD4<Float>
    var colorCone: SIMD4<Float>      // rgb = pre-scaled linear radiance, w = coneMinCos (>0 downward lamp, <= -0.9 omni)
}

struct RTDynamicFXGPU {
    var timeWeather: SIMD4<Float>    // x = simTimeSec, y = timeOfDayHours, z = rainIntensity, w = puddleLevel
    var carMetrics: SIMD4<Float>     // x = frontAxleZ, y = rearAxleZ, z = wheelRadiusF, w = wheelRadiusR
    var nosParams: SIMD4<Float>      // x = nosActive, y = nosFlameLen, z = speedbreaker, w = worldLightCount
    var copLightPos: (SIMD4<Float>, SIMD4<Float>, SIMD4<Float>, SIMD4<Float>)
    var copLightCol: (SIMD4<Float>, SIMD4<Float>, SIMD4<Float>, SIMD4<Float>)

    static func zeroed() -> RTDynamicFXGPU {
        return RTDynamicFXGPU(
            timeWeather: .zero,
            carMetrics: SIMD4<Float>(1.595, -1.13, 0.33, 0.33),
            nosParams: .zero,
            copLightPos: (.zero, .zero, .zero, .zero),
            copLightCol: (.zero, .zero, .zero, .zero)
        )
    }
}

struct RTOrbitCamera {
    var target: SIMD3<Float>
    var distance: Float
    var azimuthDeg: Float
    var elevationDeg: Float
    var fovDeg: Float

    let defaultTarget: SIMD3<Float>
    let defaultDistance: Float
    let defaultAzimuthDeg: Float
    let defaultElevationDeg: Float
    let defaultFovDeg: Float

    init(target: SIMD3<Float>, distance: Float, azimuthDeg: Float, elevationDeg: Float, fovDeg: Float) {
        self.target = target
        self.distance = distance
        self.azimuthDeg = azimuthDeg
        self.elevationDeg = elevationDeg
        self.fovDeg = fovDeg
        self.defaultTarget = target
        self.defaultDistance = distance
        self.defaultAzimuthDeg = azimuthDeg
        self.defaultElevationDeg = elevationDeg
        self.defaultFovDeg = fovDeg
    }

    mutating func reset() {
        target = defaultTarget
        distance = defaultDistance
        azimuthDeg = defaultAzimuthDeg
        elevationDeg = defaultElevationDeg
        fovDeg = defaultFovDeg
    }

    func eyePosition() -> SIMD3<Float> {
        let az = azimuthDeg * .pi / 180.0
        let el = elevationDeg * .pi / 180.0
        let x = target.x + distance * cos(el) * sin(az)
        let y = max(target.y - 0.30, target.y + distance * sin(el))
        let z = target.z + distance * cos(el) * cos(az)
        return SIMD3<Float>(x, y, z)
    }

    func basis() -> (eye: SIMD3<Float>, forward: SIMD3<Float>, right: SIMD3<Float>, up: SIMD3<Float>) {
        let eye = eyePosition()
        let f = simd_normalize(target - eye)
        let r = simd_normalize(simd_cross(f, SIMD3<Float>(0, 1, 0)))
        let u = simd_cross(r, f)
        return (eye, f, r, u)
    }

    func viewMatrix() -> simd_float4x4 {
        let eye = eyePosition()
        let f = simd_normalize(target - eye)
        let s = simd_normalize(simd_cross(f, SIMD3<Float>(0, 1, 0)))
        let u = simd_cross(s, f)
        return simd_float4x4(
            SIMD4<Float>(s.x, u.x, -f.x, 0),
            SIMD4<Float>(s.y, u.y, -f.y, 0),
            SIMD4<Float>(s.z, u.z, -f.z, 0),
            SIMD4<Float>(-simd_dot(s, eye), -simd_dot(u, eye), simd_dot(f, eye), 1)
        )
    }

    func projMatrix(aspect: Float) -> simd_float4x4 {
        let fovY = fovDeg * .pi / 180.0
        let yScale = 1.0 / tan(fovY * 0.5)
        let xScale = yScale / max(aspect, 0.01)
        let nearZ: Float = 0.20
        let farZ: Float = 800.0
        let zRange = farZ - nearZ
        return simd_float4x4(
            SIMD4<Float>(xScale, 0, 0, 0),
            SIMD4<Float>(0, yScale, 0, 0),
            SIMD4<Float>(0, 0, -farZ / zRange, -1),
            SIMD4<Float>(0, 0, -(farZ * nearZ) / zRange, 0)
        )
    }

    // Reverse-Z projection (near=1.0, far=0.0) for high-precision MetalFX depth disocclusion
    func projMatrixReverseZ(aspect: Float) -> simd_float4x4 {
        let fovY = fovDeg * .pi / 180.0
        let yScale = 1.0 / tan(fovY * 0.5)
        let xScale = yScale / max(aspect, 0.01)
        let nearZ: Float = 0.20
        let farZ: Float = 800.0
        let zRange = farZ - nearZ
        return simd_float4x4(
            SIMD4<Float>(xScale, 0, 0, 0),
            SIMD4<Float>(0, yScale, 0, 0),
            SIMD4<Float>(0, 0, nearZ / zRange, -1),
            SIMD4<Float>(0, 0, (farZ * nearZ) / zRange, 0)
        )
    }

    func viewProjMatrix(aspect: Float) -> simd_float4x4 {
        return projMatrix(aspect: aspect) * viewMatrix()
    }
}

final class RTRenderer: NSObject, MTKViewDelegate {
    let device: MTLDevice
    let commandQueue: MTLCommandQueue
    var scene: RTScenePackage
    var world: RTWorldPackage?
    private(set) var worldStreamer: RTWorldStreamer?
    private(set) var activeLandmarkIndex: Int = 0
    private(set) var activeLandmarkName: String = "Spawn"
    private(set) var activeWorldInstanceCount: Int = 0
    private(set) var activeAIInstanceCount: Int = 0
    private var maxInstanceCapacity: Int = 0
    private var aiBlasBase: Int = 0
    private var worldBlasBase: Int = 0
    private let aiSlotCount: Int = RTGameWorld.maxAISlots
    private var aiSlotBase: Int { scene.meshes.count }
    private var worldSlotBase: Int { scene.meshes.count + aiSlotCount }
    private var carBlasList: [MTLAccelerationStructure] = []
    private(set) var aiVehicles: RTAIVehiclePack?
    private var aiSceneDataFallbackBuffer: MTLBuffer!
    private var aiRoadProbePSO: MTLComputePipelineState?
    private var aiRoadProbeInBuffer: MTLBuffer!
    private var aiRoadProbeOutBuffer: MTLBuffer!
    private var aiRoadPitches: [Float] = Array(repeating: 0.0, count: RTGameWorld.maxAISlots)
    private var minimapBuffer = RTMinimapBuffer()
    private var smoothedFps: Float = 60.0
    private var smoothedFrameSec: Double = 1.0 / 60.0
    var forceSyntheticMode: Bool = false

    // Authentic NFSMW 2005 Gameplay Subsystems (VLT Physics + Ginsu/ABKC Audio + Traffic/Pursuit AI + Events/WorldMap)
    let physicsEngine: RTPhysicsEngine
    let audioEngine: RTAudioEngine
    let gameWorld: RTGameWorld
    let eventManager: RTEventManager
    var aiEnabled: Bool = true
    var benchmarkNosForced: Bool = false
    var benchmarkSpeedbreakerForced: Bool = false
    var benchmarkPursuitHeat: Int? = nil
    var benchmarkAutoSteerToEvent: Bool = false

    // v1.1.0 EA TRAX Soundtrack Banner & Upward Sky-Ray Acoustic Tunnel Occlusion
    private(set) var rawSkyOcclusion: Float = 0.0
    private(set) var skyCeilingDist: Float = 999.0
    private(set) var smoothedSkyOcclusion: Float = 0.0
    var musicDisabled: Bool = false
    var forceShowMusicBanner: Bool = false
    private var musicBannerTimer: Float = 0.0
    private let musicBannerDuration: Float = 4.5
    private var musicBannerArtist: String = ""
    private var musicBannerTitle: String = ""
    private var musicBannerTrackIndex: Int = 1
    private var musicBannerTotalTracks: Int = 26
    private var musicBannerIsPursuit: Bool = false
    private var musicBannerIsMuted: Bool = false
    private var lastPursuitAudioMode: Bool = false
    private var explicitMusicTrackLocked: Bool = false

    private var raytracePSO: MTLComputePipelineState!
    private var svgfTemporalPSO: MTLComputePipelineState!
    private var denoiseAtrousPSO: MTLComputePipelineState!
    private var remodulatePSO: MTLComputePipelineState!
    private var bloomThresholdPSO: MTLComputePipelineState!
    private var bloomBlurPSO: MTLComputePipelineState!
    private var compositePSO: MTLComputePipelineState!
    private var hudOverlayPSO: MTLComputePipelineState!
    private var physicsProbePSO: MTLComputePipelineState!
    private var linearSampler: MTLSamplerState!
    private var repeatSampler: MTLSamplerState!
    private(set) var compiledMSLVersion: String = "3.1"

    // Dummy fallback world buffers when --world is omitted
    private let dummyWorldVB: MTLBuffer
    private let dummyWorldIB: MTLBuffer
    private let dummyWorldMB: MTLBuffer
    private let dummyWorldTB: MTLBuffer
    private let dummyWorldTex: MTLTexture

    // Reserved Hour 1-3 + v1.2.0 ABI buffers (Buffer Slots 11, 12, 13, 16)
    private var worldLightsBuffer: MTLBuffer!
    private var worldLightGridBuffer: MTLBuffer!
    private var dynamicFXBuffer: MTLBuffer!
    private var eventWorldFXBuffer: MTLBuffer!
    private var fallbackWorldMapTex: MTLTexture!

    // Unified TLAS (car BLASes + optional .rkpt world BLASes)
    private var blasList: [MTLAccelerationStructure] = []
    private var tlas: MTLAccelerationStructure!
    private var tlasScratch: MTLBuffer!
    private var instanceMetaBuffer: MTLBuffer!
    private var instanceDescBuffer: MTLBuffer!
    private var instanceCount: Int = 0
    private var carMeshToBlasIndex: [Int] = []
    private var prevCarModelMatrices: [simd_float4x4] = []
    private var prevCarBodyXform: simd_float4x4 = matrix_identity_float4x4

    // Physics probe shared buffers (64 bytes each)
    private var physicsInBuffer: MTLBuffer!
    private var physicsOutBuffer: MTLBuffer!

    // Render targets (input RT resolution vs output presentation resolution)
    private var accumDiffTex: MTLTexture?
    private var accumSpecTex: MTLTexture?
    private var gAlbedoTex: MTLTexture?
    private var gNormalRoughTex: MTLTexture?
    private var gDepthTex: MTLTexture?
    private var gEmissiveTex: MTLTexture?
    private var gColorTex: MTLTexture?
    private var gMotionTex: MTLTexture?
    private var gSpecAlbedoTex: MTLTexture?
    private var gRoughnessTex: MTLTexture?
    private var gReactiveTex: MTLTexture?

    // SVGF temporal history ping-pong buffers
    private var histDiffTex: MTLTexture?
    private var histSpecTex: MTLTexture?
    private var histNormalRoughTex: MTLTexture?
    private var histDepthTex: MTLTexture?

    private var denoiseDiffPing: MTLTexture?
    private var denoiseDiffPong: MTLTexture?
    private var denoiseSpecPing: MTLTexture?
    private var denoiseSpecPong: MTLTexture?
    private var hdrResolvedTex: MTLTexture?
    private var bloomTexA: MTLTexture?
    private var bloomTexB: MTLTexture?
    private var ldrColorTexA: MTLTexture?
    private var ldrColorTexB: MTLTexture?
    private var ldrInterpTex: MTLTexture?

    private var rtWidth: Int = 0
    private var rtHeight: Int = 0
    private var targetWidth: Int = 0
    private var targetHeight: Int = 0
    private var temporalScaler: AnyObject? = nil // MTLFXTemporalDenoisedScaler on macOS 26+
    private var frameInterpolator: AnyObject? = nil // MTLFXFrameInterpolator on macOS 26+
    private var scalerInputW: Int = 0
    private var scalerInputH: Int = 0
    private var scalerOutputW: Int = 0
    private var scalerOutputH: Int = 0
    private var metalFXActive: Bool = false
    private var frameGenActive: Bool = false
    private var ldrWriteToA: Bool = true
    private var frameGenHistoryValid: Bool = false
    private var frameGenPresentPhase: Int = 0
    private var lastRealFrameTime: CFAbsoluteTime = CFAbsoluteTimeGetCurrent()
    private var lastRealFrameDelta: Float = 1.0 / 60.0

    private var prevViewProj: simd_float4x4 = matrix_identity_float4x4
    private var hasValidPrevFrame: Bool = false
    private var historyResetPending: Bool = true
    private var frameCounter: UInt32 = 0
    private var simTimeSec: Float = 2.40

    var camera: RTOrbitCamera
    var cameraMode: RTCameraMode = .hero
    var lookdevConfig = RTLookdevConfig()
    var activePresetKey: String = "golden_hour" {
        didSet { if activePresetKey != oldValue { resetAccumulation() } }
    }
    var rtEnabled: Bool = true {
        didSet { if rtEnabled != oldValue { resetAccumulation() } }
    }
    var denoiserEnabled: Bool = true {
        didSet { if denoiserEnabled != oldValue { resetAccumulation() } }
    }
    var denoiseMode: RTDenoiseMode = .metalfx {
        didSet {
            denoiserEnabled = (denoiseMode != .off)
            historyResetPending = true
        }
    }
    var frameGenEnabled: Bool = false {
        didSet {
            if frameGenEnabled != oldValue {
                frameGenHistoryValid = false
                frameGenPresentPhase = 0
                historyResetPending = true
            }
        }
    }
    var timeCycleEnabled: Bool = false
    var explicitTimeOfDay: Float? = nil {
        didSet { resetAccumulation() }
    }
    var timeOfDayHours: Float = 17.6
    var rainIntensity: Float = 0.0 {
        didSet { if rainIntensity != oldValue { resetAccumulation() } }
    }
    var speedbreakerStrength: Float = 0.0
    var nosActiveStrength: Float = 0.0
    var liveryEnabled: Bool = true {
        didSet { if liveryEnabled != oldValue { resetAccumulation() } }
    }
    var hudEnabled: Bool = false {
        didSet { if hudEnabled != oldValue { resetAccumulation() } }
    }

    // Real-time drive & world state
    var driveDemoMode: Bool = false
    var chaseCamEnabled: Bool = false
    var carWorldPosition: SIMD3<Float> = .zero
    var carHeadingRad: Float = 0.0
    var terrainPitchRad: Float = 0.0
    var terrainRollRad: Float = 0.0
    var speedKmh: Float = 0.0
    var steerDeg: Float = 0.0
    var throttleHeld: Bool = false
    var brakeHeld: Bool = false
    var steerLeftHeld: Bool = false
    var steerRightHeld: Bool = false
    var handbrakeHeld: Bool = false
    var nosKeyHeld: Bool = false
    var nosShiftHeld: Bool = false
    var speedbreakerHeld: Bool = false
    var nosHeld: Bool { nosKeyHeld || nosShiftHeld || benchmarkNosForced }
    var speedbreakerInputHeld: Bool { speedbreakerHeld || benchmarkSpeedbreakerForced }
    var roadScrollZ: Float = 0.0
    var wheelSpinRad: Float = 0.0
    var carYawRad: Float = 0.0
    var carRollRad: Float = 0.0
    var carPitchRad: Float = 0.0
    private var lastFrameTime: CFAbsoluteTime = CFAbsoluteTimeGetCurrent()
    private(set) var sampleIndex: UInt32 = 0

    var supportsMetalFXDenoiser: Bool {
        if #available(macOS 26.0, *) {
            return MTLFXTemporalDenoisedScalerDescriptor.supportsDevice(device)
        }
        return false
    }

    var supportsFrameInterpolator: Bool {
        if #available(macOS 26.0, *) {
            return MTLFXFrameInterpolatorDescriptor.supportsDevice(device)
        }
        return false
    }

    var effectiveFrameGenActive: Bool {
        return frameGenEnabled && supportsFrameInterpolator && denoiseMode == .metalfx && supportsMetalFXDenoiser
    }

    var effectiveDenoiseLabel: String {
        switch denoiseMode {
        case .metalfx:
            return supportsMetalFXDenoiser ? "metalfx" : "svgf"
        case .svgf:
            return "svgf"
        case .off:
            return "off"
        }
    }

    init(
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        scene: RTScenePackage,
        world: RTWorldPackage? = nil,
        metalFileURL: URL?,
        headless: Bool = false,
        aiEnabled: Bool = true
    ) throws {
        guard device.supportsRaytracing else {
            throw NSError(domain: "RTRenderer", code: 100, userInfo: [
                NSLocalizedDescriptionKey: "Metal hardware ray tracing is not supported on \(device.name)"
            ])
        }
        self.device = device
        self.commandQueue = commandQueue
        self.scene = scene
        self.world = world
        self.aiEnabled = aiEnabled
        self.physicsEngine = RTPhysicsEngine(carKey: scene.canonicalCarName)
        self.audioEngine = RTAudioEngine(headless: headless)
        self.gameWorld = RTGameWorld()
        self.eventManager = RTEventManager(landmarks: world?.landmarks ?? [])
        let dummies = RTWorldLoader.makeDummyWorldBuffers(device: device)
        self.dummyWorldVB = dummies.0
        self.dummyWorldIB = dummies.1
        self.dummyWorldMB = dummies.2
        self.dummyWorldTB = dummies.3
        self.dummyWorldTex = dummies.4

        self.camera = RTOrbitCamera(
            target: scene.cameraTarget,
            distance: scene.cameraDistance,
            azimuthDeg: scene.cameraAzimuthDeg,
            elevationDeg: scene.cameraElevationDeg,
            fovDeg: scene.cameraFovDeg
        )
        super.init()

        guard let pin = device.makeBuffer(length: MemoryLayout<RTPhysicsProbeInput>.stride, options: .storageModeShared),
              let pout = device.makeBuffer(length: MemoryLayout<RTPhysicsProbeOutput>.stride, options: .storageModeShared),
              let aiPin = device.makeBuffer(length: RTGameWorld.maxAISlots * MemoryLayout<RTAIRoadProbeInput>.stride, options: .storageModeShared),
              let aiPout = device.makeBuffer(length: RTGameWorld.maxAISlots * MemoryLayout<RTAIRoadProbeOutput>.stride, options: .storageModeShared),
              let wlBuf = device.makeBuffer(length: MemoryLayout<RTWorldLightGPU>.stride, options: .storageModeShared),
              let wlgBuf = device.makeBuffer(length: 256, options: .storageModeShared),
              let dfxBuf = device.makeBuffer(length: MemoryLayout<RTDynamicFXGPU>.stride, options: .storageModeShared),
              let evFxBuf = device.makeBuffer(length: RTEventWorldFX.byteSize, options: .storageModeShared),
              let fbMapTex = RTEventManager.makeFallbackWorldMapTexture(device: device) else {
            throw NSError(domain: "RTRenderer", code: 102, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate physics/ABI buffers"])
        }
        precondition(MemoryLayout<RTWorldLightGPU>.stride == 32, "RTWorldLightGPU stride must be 32 bytes")
        precondition(MemoryLayout<RTDynamicFXGPU>.stride == 176, "RTDynamicFXGPU stride must be 176 bytes")
        precondition(MemoryLayout<RTPhysicsProbeOutput>.stride == 80, "RTPhysicsProbeOutput stride must be 80 bytes")
        RTHudExtUniforms.assertLayout()
        RTMinimapBuffer.assertLayout()
        RTMusicBannerUniforms.assertLayout()
        RTEventWorldFX.assertLayout()
        RTEventHudUniforms.assertLayout()
        self.physicsInBuffer = pin
        self.physicsOutBuffer = pout
        self.aiRoadProbeInBuffer = aiPin
        self.aiRoadProbeOutBuffer = aiPout
        self.aiSceneDataFallbackBuffer = RTAIVehicleLoader.makeEmptySceneDataBuffer(device: device)
        self.worldLightsBuffer = wlBuf
        self.worldLightGridBuffer = wlgBuf
        self.dynamicFXBuffer = dfxBuf
        self.eventWorldFXBuffer = evFxBuf
        self.fallbackWorldMapTex = fbMapTex
        var initEvFX = RTEventWorldFX.inactive()
        memcpy(evFxBuf.contents(), &initEvFX, RTEventWorldFX.byteSize)
        memset(aiPin.contents(), 0, RTGameWorld.maxAISlots * MemoryLayout<RTAIRoadProbeInput>.stride)
        let aiOutInit = aiPout.contents().bindMemory(to: RTAIRoadProbeOutput.self, capacity: RTGameWorld.maxAISlots)
        for i in 0..<RTGameWorld.maxAISlots {
            aiOutInit[i] = RTAIRoadProbeOutput(roadY: -9999.0, pitchRad: 0.0, frontY: -9999.0, rearY: -9999.0)
        }
        memset(wlBuf.contents(), 0, MemoryLayout<RTWorldLightGPU>.stride)
        memset(wlgBuf.contents(), 0, 256)
        var initFX = RTDynamicFXGPU.zeroed()
        memcpy(dfxBuf.contents(), &initFX, MemoryLayout<RTDynamicFXGPU>.stride)

        try buildPipelines(metalFileURL: metalFileURL)
        buildSamplers()

        if aiEnabled && world != nil {
            self.aiVehicles = try? RTAIVehicleLoader.load(device: device, commandQueue: commandQueue)
        }

        if let w = world {
            self.carWorldPosition = w.spawnPos
            self.carHeadingRad = w.spawnYawRad
            if !w.landmarks.isEmpty {
                self.activeLandmarkName = w.landmarks[0].name
            }
            if w.instances.count > 4500 || !w.worldLights.isEmpty || w.panoramaSectionCount > 0 {
                self.worldStreamer = try? RTWorldStreamer(world: w, device: device)
            }
            if !w.roadNodes.isEmpty && !w.roadSegments.isEmpty {
                let nodes: [RTRoadNodeSpec] = w.roadNodes.map { n in
                    RTRoadNodeSpec(
                        pos: n.pos,
                        nodeIdx: n.nodeIndex,
                        flags: n.flags,
                        degree: n.degree,
                        adjSegs: n.adjacentSegmentIndices.map { UInt16($0) }
                    )
                }
                let segs: [RTRoadSegmentSpec] = w.roadSegments.map { s in
                    RTRoadSegmentSpec(
                        node0: s.node0,
                        node1: s.node1,
                        laneFlags: s.laneFlags,
                        dirFlags: s.dirFlags,
                        roadId: s.roadId,
                        lengthM: max(0.5, s.lengthMeters),
                        halfWidthM: max(2.5, s.halfWidthMeters)
                    )
                }
                self.gameWorld.setRoadGraph(nodes: nodes, segments: segs)
                _ = self.eventManager.bakeWorldMapTexture(
                    device: device,
                    roadNodes: w.roadNodes,
                    roadSegments: w.roadSegments,
                    landmarks: w.landmarks
                )
            }
        }
        self.physicsEngine.reset(position: carWorldPosition, headingRad: carHeadingRad)

        if !headless && audioEngine.isPackLoaded {
            audioEngine.start(initialEngineNode: physicsEngine.spec.engineAudioNode)
            audioEngine.selectEngine(node: physicsEngine.spec.engineAudioNode, trannyWhineOverride: physicsEngine.spec.trannyWhine)
        }
        syncMusicBannerFromAudio(triggerSlide: !headless && audioEngine.currentMusicTrack != nil)

        try buildAccelerationStructures()

        if world != nil {
            // Snap car tires cleanly onto the exact .rkpt asphalt height via GPU BVH probe
            snapCarToWorldRoad()
            if aiEnabled && !gameWorld.roadGraph.isEmpty {
                gameWorld.resetNearPlayer(playerPos: carWorldPosition, playerYaw: carHeadingRad, keepPursuit: false)
                gameWorld.step(dt: 1.0 / 60.0, playerPos: carWorldPosition, playerVel: .zero, playerYaw: carHeadingRad)
                snapAIVehiclesToRoad()
            }
            updateMinimapBuffer()
            applyCameraMode(cameraMode)
        }
    }

    func applyLookdevConfig(_ cfg: RTLookdevConfig) {
        self.lookdevConfig = cfg
        if let preset = cfg.preset { activePresetKey = preset }
        if let fov = cfg.camera_fov { camera.fovDeg = fov }
        if let az = cfg.camera_azimuth { camera.azimuthDeg = az }
        if let el = cfg.camera_elevation { camera.elevationDeg = el }
        if let dist = cfg.camera_distance { camera.distance = dist }
        if let spd = cfg.speed_kmh { speedKmh = spd }
        if let str = cfg.steer_deg { steerDeg = str }
        resetAccumulation()
    }

    func applyCameraMode(_ mode: RTCameraMode) {
        self.cameraMode = mode
        let headingDeg = carHeadingRad * 180.0 / .pi
        let baseTarget = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.52, carWorldPosition.z)
        let speedFrac = min(speedKmh / 240.0, 1.0)
        switch mode {
        case .hero:
            chaseCamEnabled = false
            if world != nil {
                // Autovista / GT7 Scapes parked stance: angle front wheels +12 deg toward camera & offset azimuth +10 deg to clear roof-line signs/poles!
                if speedKmh < 1.0 && !driveDemoMode {
                    steerDeg = 12.0
                    try? rebuildTLAS(commandBuffer: nil)
                }
                camera.target = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.58, carWorldPosition.z)
                camera.azimuthDeg = headingDeg + 49.0
                camera.elevationDeg = 7.0
                camera.distance = 4.95
                camera.fovDeg = 35.0
            } else {
                camera.target = baseTarget + SIMD3<Float>(0, 0, scene.cameraTarget.z)
                camera.azimuthDeg = headingDeg + 36.0
                camera.elevationDeg = 7.6
                camera.distance = min(scene.cameraDistance, 4.75)
                camera.fovDeg = 36.0
            }
        case .chase:
            chaseCamEnabled = true
            camera.target = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.72, carWorldPosition.z)
            camera.azimuthDeg = headingDeg + 180.0 + steerDeg * 0.42
            camera.elevationDeg = 8.2
            camera.distance = 4.55 + 0.35 * speedFrac
            camera.fovDeg = 44.0 + 18.0 * speedFrac
        case .street:
            chaseCamEnabled = false
            if world != nil && speedKmh < 1.0 && !driveDemoMode {
                steerDeg = 12.0
                try? rebuildTLAS(commandBuffer: nil)
            }
            camera.target = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.62, carWorldPosition.z)
            camera.azimuthDeg = headingDeg + 41.0
            camera.elevationDeg = 4.8
            camera.distance = 5.75
            camera.fovDeg = 33.0
        case .overhead:
            chaseCamEnabled = false
            camera.target = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.50, carWorldPosition.z)
            camera.azimuthDeg = headingDeg + 145.0
            camera.elevationDeg = 42.0
            camera.distance = 22.0
            camera.fovDeg = 48.0
        }
        resetAccumulation()
    }

    func configureDrivePose(speedKmh: Float, steerDeg: Float, useChaseCam: Bool) {
        self.speedKmh = max(0.0, min(320.0, speedKmh))
        self.steerDeg = max(-28.0, min(28.0, steerDeg))
        self.chaseCamEnabled = useChaseCam
        let steerRad = self.steerDeg * .pi / 180.0
        let speedFrac = min(self.speedKmh / 240.0, 1.0)
        self.carRollRad = -steerRad * 0.14 * min(self.speedKmh / 180.0, 1.0)
        self.carYawRad = steerRad * 0.22 * min(self.speedKmh / 180.0, 1.0)
        self.carPitchRad = -0.008 * speedFrac
        self.wheelSpinRad = (self.speedKmh / 3.6) * 0.45 / 0.33
        self.roadScrollZ = 14.5
        physicsEngine.configurePose(
            speedKmh: self.speedKmh,
            steerDeg: self.steerDeg,
            position: carWorldPosition,
            headingRad: carHeadingRad
        )
        if useChaseCam {
            applyCameraMode(.chase)
        }
        try? rebuildTLAS(commandBuffer: nil)
        resetAccumulation()
    }

    func switchCar(to profile: RTCarProfile) throws {
        try switchCar(toKey: profile.rawValue)
    }

    @discardableResult
    func switchCar(toKey carKey: String) throws -> RTScenePackage {
        let newScene = try RTSceneBuilder.buildScene(
            device: device,
            commandQueue: commandQueue,
            scenePath: nil,
            carKey: carKey,
            forceSynthetic: forceSyntheticMode,
            skipProceduralWorld: (world != nil)
        )
        self.scene = newScene
        physicsEngine.setCar(newScene.canonicalCarName)
        audioEngine.selectEngine(node: physicsEngine.spec.engineAudioNode, trannyWhineOverride: physicsEngine.spec.trannyWhine)
        self.camera = RTOrbitCamera(
            target: newScene.cameraTarget,
            distance: newScene.cameraDistance,
            azimuthDeg: newScene.cameraAzimuthDeg,
            elevationDeg: newScene.cameraElevationDeg,
            fovDeg: newScene.cameraFovDeg
        )
        try buildAccelerationStructures()
        if world != nil {
            snapCarToWorldRoad()
        }
        applyCameraMode(cameraMode)
        historyResetPending = true
        frameGenHistoryValid = false
        resetAccumulation()
        return newScene
    }

    func triggerInstantPursuit(setHeat explicitHeat: Int? = nil, showcaseFraming: Bool = false) {
        guard aiEnabled && world != nil && !gameWorld.roadGraph.isEmpty else { return }
        gameWorld.triggerInstantPursuit(
            near: carWorldPosition,
            playerYaw: carHeadingRad,
            setHeat: explicitHeat,
            showcaseFraming: showcaseFraming
        )
        gameWorld.step(
            dt: 1.0 / 60.0,
            playerPos: carWorldPosition,
            playerVel: physicsEngine.state.velocityWorld,
            playerYaw: carHeadingRad
        )
        snapAIVehiclesToRoad()
        updateMinimapBuffer()
        updateDynamicFXBuffer()
        if !musicDisabled && !explicitMusicTrackLocked {
            audioEngine.setPursuitMusicMode(true)
            lastPursuitAudioMode = true
            syncMusicBannerFromAudio(triggerSlide: true)
        }
    }

    // MARK: - v1.1.0 EA TRAX Soundtrack & Tunnel Reverb Controls

    func syncMusicBannerFromAudio(triggerSlide: Bool) {
        musicBannerIsMuted = musicDisabled || audioEngine.isMusicMuted
        if let info = audioEngine.currentMusicTrack {
            musicBannerArtist = info.artist
            musicBannerTitle = info.title
            musicBannerTrackIndex = info.index + 1
            musicBannerTotalTracks = max(1, info.total)
            musicBannerIsPursuit = info.isPursuit
            if triggerSlide {
                musicBannerTimer = musicBannerDuration
            }
        } else if forceShowMusicBanner && musicBannerTitle.isEmpty {
            // Clean offline/synthetic fallback when --show-trax is explicitly requested without music_pack
            musicBannerArtist = "STYLES OF BEYOND"
            musicBannerTitle = "NINE THOU (SUPERSTARS REMIX)"
            musicBannerTrackIndex = 16
            musicBannerTotalTracks = 26
            musicBannerIsPursuit = false
            if triggerSlide {
                musicBannerTimer = musicBannerDuration
            }
        }
    }

    @discardableResult
    func nextMusicTrack() -> RTMusicTrackInfo? {
        guard !musicDisabled else { return nil }
        explicitMusicTrackLocked = true
        let info = audioEngine.nextMusicTrack()
        syncMusicBannerFromAudio(triggerSlide: true)
        return info
    }

    @discardableResult
    func prevMusicTrack() -> RTMusicTrackInfo? {
        guard !musicDisabled else { return nil }
        explicitMusicTrackLocked = true
        let info = audioEngine.prevMusicTrack()
        syncMusicBannerFromAudio(triggerSlide: true)
        return info
    }

    @discardableResult
    func selectMusicTrack(index: Int) -> RTMusicTrackInfo? {
        explicitMusicTrackLocked = true
        let info = audioEngine.selectMusicTrack(index: index)
        syncMusicBannerFromAudio(triggerSlide: true)
        return info
    }

    @discardableResult
    func toggleMusicMute() -> Bool {
        let muted = audioEngine.toggleMusicMute()
        musicBannerIsMuted = muted
        musicBannerTimer = musicBannerDuration
        return muted
    }

    func disableMusicPlayback() {
        musicDisabled = true
        if !audioEngine.isMusicMuted {
            _ = audioEngine.toggleMusicMute()
        }
        musicBannerIsMuted = true
    }

    func teleportToPosition(_ pos: SIMD3<Float>, yawRad: Float, label: String = "Custom") {
        activeLandmarkName = label
        let lm = RkptLandmark(name: label, spawnPos: pos, spawnYawRad: yawRad, regionId: 0, flags: 0)
        applyLandmarkPose(lm)
    }

    // MARK: - v1.2.0 NFSMW Race Events (Sprint / Speedtrap / Blacklist 15 Boss) & World Map

    @discardableResult
    func startEvent(matching query: String, skipCountdown: Bool = false) -> RTEventStartResult? {
        guard let res = eventManager.startEvent(matching: query, skipCountdown: skipCountdown) else {
            return nil
        }
        applyEventStartPose(res)
        return res
    }

    @discardableResult
    func startNearestEvent(skipCountdown: Bool = false) -> RTEventStartResult? {
        guard let res = eventManager.startNearestEvent(to: carWorldPosition, kind: nil, skipCountdown: skipCountdown) else {
            return nil
        }
        applyEventStartPose(res)
        return res
    }

    func abortActiveEvent() {
        eventManager.abortEvent()
        updateEventWorldFXBuffer()
        updateMinimapBuffer()
    }

    private func applyEventStartPose(_ res: RTEventStartResult) {
        eventManager.closeWorldMap()
        activeLandmarkName = res.event.district.isEmpty ? res.event.title : res.event.district
        carWorldPosition = res.startPos
        carHeadingRad = res.startYawRad
        speedKmh = 0.0
        steerDeg = 0.0
        carYawRad = 0.0
        carPitchRad = 0.0
        carRollRad = 0.0
        physicsEngine.reset(position: carWorldPosition, headingRad: carHeadingRad)
        try? rebuildTLAS(commandBuffer: nil, forceWorldStream: true)
        snapCarToWorldRoad()
        if aiEnabled && !gameWorld.roadGraph.isEmpty {
            gameWorld.resetNearPlayer(playerPos: carWorldPosition, playerYaw: carHeadingRad, keepPursuit: false)
            gameWorld.step(dt: 1.0 / 60.0, playerPos: carWorldPosition, playerVel: .zero, playerYaw: carHeadingRad)
        }
        eventManager.step(dt: 1.0 / 60.0, playerPos: carWorldPosition, playerVel: .zero, playerYaw: carHeadingRad)
        snapAIVehiclesToRoad()
        updateMinimapBuffer()
        updateDynamicFXBuffer()
        updateEventWorldFXBuffer()
        applyCameraMode(.chase)
        historyResetPending = true
        resetAccumulation()
    }

    @discardableResult
    func toggleWorldMap() -> Bool {
        let open = eventManager.toggleWorldMap(playerPos: carWorldPosition)
        if open {
            throttleHeld = false
            brakeHeld = false
            steerLeftHeld = false
            steerRightHeld = false
            handbrakeHeld = false
            nosKeyHeld = false
            nosShiftHeld = false
            speedbreakerHeld = false
        }
        return open
    }

    func confirmWorldMapSelection() {
        let action = eventManager.confirmSelection()
        switch action {
        case .none:
            break
        case .teleport(let pos, let yaw, let label):
            teleportToPosition(pos, yawRad: yaw, label: label)
            print("World Map fast-travel -> \(label)")
        case .startEvent(let id):
            if let res = startEvent(matching: id, skipCountdown: false) {
                print("World Map launched event -> \(res.event.title) [\(res.event.kind.rawValue)]")
            }
        }
    }

    func resetPlayer() {
        speedKmh = 0.0
        steerDeg = 0.0
        carYawRad = 0.0
        carRollRad = 0.0
        carPitchRad = 0.0
        terrainPitchRad = 0.0
        terrainRollRad = 0.0
        nosActiveStrength = 0.0
        speedbreakerStrength = 0.0
        if let w = world {
            carWorldPosition = w.spawnPos
            carHeadingRad = w.spawnYawRad
            physicsEngine.reset(position: carWorldPosition, headingRad: carHeadingRad)
            try? rebuildTLAS(commandBuffer: nil, forceWorldStream: true)
            snapCarToWorldRoad()
            if aiEnabled && !gameWorld.roadGraph.isEmpty {
                gameWorld.resetNearPlayer(playerPos: carWorldPosition, playerYaw: carHeadingRad, keepPursuit: false)
                gameWorld.step(dt: 1.0 / 60.0, playerPos: carWorldPosition, playerVel: .zero, playerYaw: carHeadingRad)
                snapAIVehiclesToRoad()
            }
            updateMinimapBuffer()
        } else {
            carWorldPosition = .zero
            carHeadingRad = 0.0
            physicsEngine.reset(position: .zero, headingRad: 0.0)
            try? rebuildTLAS(commandBuffer: nil)
        }
        applyCameraMode(.hero)
        resetAccumulation()
    }

    @discardableResult
    func teleportToLandmark(_ query: String) -> RkptLandmark? {
        guard let streamer = worldStreamer, let lm = streamer.landmark(namedOrIndex: query) else {
            // Fallback check directly on world.landmarks if non-streamed
            if let w = world, !w.landmarks.isEmpty {
                let q = query.lowercased()
                if let idx = Int(q), idx >= 0 && idx < w.landmarks.count {
                    let lm = w.landmarks[idx]
                    activeLandmarkIndex = idx
                    activeLandmarkName = lm.name
                    applyLandmarkPose(lm)
                    return lm
                }
                if let idx = w.landmarks.firstIndex(where: { $0.name.lowercased().contains(q) }) {
                    let lm = w.landmarks[idx]
                    activeLandmarkIndex = idx
                    activeLandmarkName = lm.name
                    applyLandmarkPose(lm)
                    return lm
                }
            }
            return nil
        }
        if let idx = streamer.landmarks.firstIndex(where: { $0.name == lm.name }) {
            activeLandmarkIndex = idx
        }
        activeLandmarkName = lm.name
        applyLandmarkPose(lm)
        return lm
    }

    @discardableResult
    func cycleLandmark() -> RkptLandmark? {
        guard let streamer = worldStreamer, !streamer.landmarks.isEmpty else { return nil }
        let next = streamer.nextLandmark(after: activeLandmarkIndex)
        activeLandmarkIndex = next.index
        activeLandmarkName = next.landmark.name
        applyLandmarkPose(next.landmark)
        return next.landmark
    }

    private func applyLandmarkPose(_ lm: RkptLandmark) {
        carWorldPosition = lm.spawnPos
        carHeadingRad = lm.spawnYawRad
        speedKmh = 0.0
        steerDeg = 0.0
        carYawRad = 0.0
        carRollRad = 0.0
        carPitchRad = 0.0
        terrainPitchRad = 0.0
        terrainRollRad = 0.0
        nosActiveStrength = 0.0
        speedbreakerStrength = 0.0
        physicsEngine.reset(position: carWorldPosition, headingRad: carHeadingRad)
        try? rebuildTLAS(commandBuffer: nil, forceWorldStream: true)
        snapCarToWorldRoad()
        if aiEnabled && !gameWorld.roadGraph.isEmpty {
            let hadPursuit = (gameWorld.heatLevel > 0)
            gameWorld.resetNearPlayer(playerPos: carWorldPosition, playerYaw: carHeadingRad, keepPursuit: hadPursuit)
            gameWorld.step(dt: 1.0 / 60.0, playerPos: carWorldPosition, playerVel: .zero, playerYaw: carHeadingRad)
            snapAIVehiclesToRoad()
        }
        updateMinimapBuffer()
        applyCameraMode(cameraMode)
        historyResetPending = true
        frameGenHistoryValid = false
        resetAccumulation()
    }

    private func computeGearAndRPM(speedKmh: Float) -> (gear: Float, rpmNorm: Float) {
        if speedKmh < 1.0 { return (0.0, 0.14) }
        let thresholds: [Float] = [0.0, 62.0, 108.0, 152.0, 198.0, 246.0, 325.0]
        for g in 1...6 {
            if speedKmh <= thresholds[g] || g == 6 {
                let lo = thresholds[g - 1]
                let hi = thresholds[g]
                let frac = max(0.0, min(1.0, (speedKmh - lo) / max(hi - lo, 1.0)))
                let baseRpm: Float = (g == 1) ? 0.24 : 0.50
                let rpm = min(0.98, baseRpm + (0.95 - baseRpm) * frac)
                return (Float(g), rpm)
            }
        }
        return (6.0, 0.92)
    }

    func resetAccumulation() {
        sampleIndex = 0
        historyResetPending = true
        frameGenHistoryValid = false
        frameGenPresentPhase = 0
    }

    private func buildPipelines(metalFileURL: URL?) throws {
        let library: MTLLibrary
        let exeURL = Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])
        let siblingMetallib = exeURL.deletingLastPathComponent().appendingPathComponent("RockportMetalRT.metallib")
        let siblingSource = exeURL.deletingLastPathComponent().appendingPathComponent("RayTracing.metal")
        let siblingHud = exeURL.deletingLastPathComponent().appendingPathComponent("RTHud.metal")

        if FileManager.default.fileExists(atPath: siblingMetallib.path),
           let precompiled = try? device.makeLibrary(URL: siblingMetallib),
           precompiled.makeFunction(name: "hudOverlayKernel") != nil {
            library = precompiled
            compiledMSLVersion = "precompiled"
        } else {
            let candidates = [
                metalFileURL,
                siblingSource,
                URL(fileURLWithPath: "native_metal_rt/RayTracing.metal"),
                URL(fileURLWithPath: "RayTracing.metal")
            ].compactMap { $0 }

            var loadedSource: String? = nil
            var loadedDir: URL? = nil
            for cand in candidates where FileManager.default.fileExists(atPath: cand.path) {
                loadedSource = try String(contentsOf: cand, encoding: .utf8)
                loadedDir = cand.deletingLastPathComponent()
                break
            }
            guard let baseSource = loadedSource else {
                throw NSError(domain: "RTRenderer", code: 102, userInfo: [NSLocalizedDescriptionKey: "RayTracing.metal not found"])
            }

            // Concatenate RTHud.metal after RayTracing.metal before runtime compilation (zero #include dependency!)
            let hudCandidates = [
                loadedDir?.appendingPathComponent("RTHud.metal"),
                siblingHud,
                URL(fileURLWithPath: "native_metal_rt/RTHud.metal"),
                URL(fileURLWithPath: "RTHud.metal")
            ].compactMap { $0 }
            var hudSource: String? = nil
            for cand in hudCandidates where FileManager.default.fileExists(atPath: cand.path) {
                hudSource = try String(contentsOf: cand, encoding: .utf8)
                break
            }
            guard let resolvedHudSource = hudSource else {
                throw NSError(domain: "RTRenderer", code: 102, userInfo: [NSLocalizedDescriptionKey: "RTHud.metal not found"])
            }
            let source = baseSource + "\n\n// === Concatenated RTHud.metal ===\n" + resolvedHudSource

            // Compile runtime MSL 4.0 on macOS 26+, falling back cleanly to 3.2 / 3.1
            var builtLib: MTLLibrary? = nil
            var lastErr: Error? = nil
            var candidateVersions: [(String, MTLLanguageVersion)] = []
            if #available(macOS 26.0, *) {
                candidateVersions.append(("4.0", .version4_0))
            }
            if #available(macOS 15.0, *) {
                candidateVersions.append(("3.2", .version3_2))
            }
            candidateVersions.append(("3.1", .version3_1))

            for (label, ver) in candidateVersions {
                let opts = MTLCompileOptions()
                opts.languageVersion = ver
                if #available(macOS 15.0, *) {
                    opts.mathMode = .fast
                } else {
                    opts.fastMathEnabled = true
                }
                do {
                    builtLib = try device.makeLibrary(source: source, options: opts)
                    compiledMSLVersion = label
                    break
                } catch {
                    lastErr = error
                }
            }
            guard let finalLib = builtLib else {
                throw lastErr ?? NSError(domain: "RTRenderer", code: 103, userInfo: [NSLocalizedDescriptionKey: "Failed to compile RayTracing.metal + RTHud.metal"])
            }
            library = finalLib
        }

        func makeCompute(_ name: String) throws -> MTLComputePipelineState {
            guard let fn = library.makeFunction(name: name) else {
                throw NSError(domain: "RTRenderer", code: 103, userInfo: [NSLocalizedDescriptionKey: "Missing Metal kernel \(name)"])
            }
            return try device.makeComputePipelineState(function: fn)
        }

        raytracePSO = try makeCompute("raytraceKernel")
        svgfTemporalPSO = try makeCompute("svgfTemporalKernel")
        denoiseAtrousPSO = try makeCompute("denoiseAtrousKernel")
        remodulatePSO = try makeCompute("remodulateKernel")
        bloomThresholdPSO = try makeCompute("bloomThresholdKernel")
        bloomBlurPSO = try makeCompute("bloomBlurKernel")
        compositePSO = try makeCompute("compositeKernel")
        hudOverlayPSO = try makeCompute("hudOverlayKernel")
        physicsProbePSO = try makeCompute("physicsProbeKernel")
        aiRoadProbePSO = try? makeCompute("aiRoadProbeKernel")
    }

    private func buildSamplers() {
        let linDesc = MTLSamplerDescriptor()
        linDesc.minFilter = .linear
        linDesc.magFilter = .linear
        linDesc.sAddressMode = .clampToEdge
        linDesc.tAddressMode = .clampToEdge
        linearSampler = device.makeSamplerState(descriptor: linDesc)

        let repDesc = MTLSamplerDescriptor()
        repDesc.minFilter = .linear
        repDesc.magFilter = .linear
        repDesc.mipFilter = .linear
        repDesc.maxAnisotropy = 8
        repDesc.sAddressMode = .repeat
        repDesc.tAddressMode = .repeat
        repeatSampler = device.makeSamplerState(descriptor: repDesc)
    }

    private static func packed4x3(_ m: simd_float4x4) -> MTLPackedFloat4x3 {
        let c0 = MTLPackedFloat3Make(m.columns.0.x, m.columns.0.y, m.columns.0.z)
        let c1 = MTLPackedFloat3Make(m.columns.1.x, m.columns.1.y, m.columns.1.z)
        let c2 = MTLPackedFloat3Make(m.columns.2.x, m.columns.2.y, m.columns.2.z)
        let c3 = MTLPackedFloat3Make(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        return MTLPackedFloat4x3(columns: (c0, c1, c2, c3))
    }

    private static func translation(_ v: SIMD3<Float>) -> simd_float4x4 {
        var m = matrix_identity_float4x4
        m.columns.3 = SIMD4<Float>(v.x, v.y, v.z, 1.0)
        return m
    }

    private static func rotationX(_ rad: Float) -> simd_float4x4 {
        let c = cos(rad), s = sin(rad)
        return simd_float4x4(
            SIMD4<Float>(1, 0, 0, 0),
            SIMD4<Float>(0, c, s, 0),
            SIMD4<Float>(0, -s, c, 0),
            SIMD4<Float>(0, 0, 0, 1)
        )
    }

    private static func rotationY(_ rad: Float) -> simd_float4x4 {
        let c = cos(rad), s = sin(rad)
        return simd_float4x4(
            SIMD4<Float>(c, 0, -s, 0),
            SIMD4<Float>(0, 1, 0, 0),
            SIMD4<Float>(s, 0, c, 0),
            SIMD4<Float>(0, 0, 0, 1)
        )
    }

    private static func rotationZ(_ rad: Float) -> simd_float4x4 {
        let c = cos(rad), s = sin(rad)
        return simd_float4x4(
            SIMD4<Float>(c, s, 0, 0),
            SIMD4<Float>(-s, c, 0, 0),
            SIMD4<Float>(0, 0, 1, 0),
            SIMD4<Float>(0, 0, 0, 1)
        )
    }

    private func currentCarBodyXform() -> simd_float4x4 {
        let totalYaw = (world != nil) ? carHeadingRad : carYawRad
        let totalPitch = carPitchRad + terrainPitchRad
        let totalRoll = carRollRad + terrainRollRad
        return Self.translation(carWorldPosition) * Self.rotationY(totalYaw) * Self.rotationX(totalPitch) * Self.rotationZ(totalRoll)
    }

    private func localPartMatrix(for mesh: RTBLASMesh) -> simd_float4x4 {
        let steerRad = steerDeg * .pi / 180.0
        switch mesh.role {
        case .wheelFL, .wheelFR:
            let p = mesh.pivotCenter
            return Self.translation(p) * Self.rotationY(steerRad) * Self.rotationX(wheelSpinRad) * Self.translation(-p) * mesh.modelMatrix
        case .wheelRL, .wheelRR:
            let p = mesh.pivotCenter
            return Self.translation(p) * Self.rotationX(wheelSpinRad) * Self.translation(-p) * mesh.modelMatrix
        case .brakeFL, .brakeFR:
            let p = mesh.pivotCenter
            return Self.translation(p) * Self.rotationY(steerRad) * Self.translation(-p) * mesh.modelMatrix
        default:
            return mesh.modelMatrix
        }
    }

    private func dynamicModelMatrix(for mesh: RTBLASMesh) -> simd_float4x4 {
        switch mesh.role {
        case .worldStatic:
            return mesh.modelMatrix
        case .streetlamp(let basePos):
            let totalSpan: Float = 192.0
            let halfSpan: Float = 96.0
            var z = basePos.z - roadScrollZ.truncatingRemainder(dividingBy: totalSpan)
            while z < -halfSpan { z += totalSpan }
            while z >= halfSpan { z -= totalSpan }
            return Self.translation(SIMD3<Float>(basePos.x, basePos.y, z))
        default:
            return currentCarBodyXform() * localPartMatrix(for: mesh)
        }
    }

    private func buildAccelerationStructures() throws {
        guard let cmdBuf = commandQueue.makeCommandBuffer(),
              let accelEnc = cmdBuf.makeAccelerationStructureCommandEncoder() else {
            throw NSError(domain: "RTRenderer", code: 104, userInfo: [NSLocalizedDescriptionKey: "Failed to create MTLAccelerationStructureCommandEncoder"])
        }

        blasList.removeAll()
        carBlasList.removeAll()
        carMeshToBlasIndex = [Int](repeating: 0, count: scene.meshes.count)

        for (idx, mesh) in scene.meshes.enumerated() {
            if let sharedIdx = mesh.sharedBlasIndex {
                carMeshToBlasIndex[idx] = carMeshToBlasIndex[sharedIdx]
                continue
            }

            let geomDesc = MTLAccelerationStructureTriangleGeometryDescriptor()
            geomDesc.vertexBuffer = scene.vertexBuffer
            geomDesc.vertexBufferOffset = mesh.vertexOffset * MemoryLayout<RTVertex>.stride
            geomDesc.vertexStride = MemoryLayout<RTVertex>.stride
            geomDesc.vertexFormat = .float3
            geomDesc.indexBuffer = scene.indexBuffer
            geomDesc.indexBufferOffset = mesh.indexOffset * MemoryLayout<UInt32>.stride
            geomDesc.indexType = .uint32
            geomDesc.triangleCount = mesh.triangleCount
            geomDesc.opaque = true

            let blasDesc = MTLPrimitiveAccelerationStructureDescriptor()
            blasDesc.geometryDescriptors = [geomDesc]

            let sizes = device.accelerationStructureSizes(descriptor: blasDesc)
            guard let blas = device.makeAccelerationStructure(size: sizes.accelerationStructureSize),
                  let scratch = device.makeBuffer(length: max(sizes.buildScratchBufferSize, 256), options: .storageModePrivate) else {
                throw NSError(domain: "RTRenderer", code: 105, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate BLAS for \(mesh.name)"])
            }
            accelEnc.build(accelerationStructure: blas, descriptor: blasDesc, scratchBuffer: scratch, scratchBufferOffset: 0)
            carMeshToBlasIndex[idx] = blasList.count
            blasList.append(blas)
            carBlasList.append(blas)
        }

        accelEnc.endEncoding()
        cmdBuf.commit()
        cmdBuf.waitUntilCompleted()
        if let err = cmdBuf.error { throw err }

        // Append deduplicated AI vehicle archetype BLASes (7 BLASes) before world BLASes
        aiBlasBase = blasList.count
        if let ai = aiVehicles {
            blasList.append(contentsOf: ai.blasList)
        }

        // Append deduplicated .rkpt world BLASes into unified TLAS BLAS array
        worldBlasBase = blasList.count
        var initialWorldInstances: [RkptInstance] = []
        if let w = world {
            blasList.append(contentsOf: w.blasList)
            if let streamer = worldStreamer {
                let res = streamer.update(playerPos: carWorldPosition, forceRebuild: true)
                initialWorldInstances = res.activeInstances
            } else {
                initialWorldInstances.reserveCapacity(w.instances.count)
                for inst in w.instances {
                    let mIdx = Int(inst.meshIndex)
                    if mIdx >= 0 && mIdx < w.meshToBlasIndex.count && w.meshToBlasIndex[mIdx] >= 0 {
                        initialWorldInstances.append(inst)
                    }
                }
            }
        }

        let maxWorldSlots = (worldStreamer != nil) ? 8192 : initialWorldInstances.count
        maxInstanceCapacity = max(1, worldSlotBase + maxWorldSlots)
        activeWorldInstanceCount = min(initialWorldInstances.count, maxWorldSlots)
        instanceCount = worldSlotBase + activeWorldInstanceCount
        prevCarBodyXform = currentCarBodyXform()
        prevCarModelMatrices = scene.meshes.map { dynamicModelMatrix(for: $0) }

        let descByteCount = maxInstanceCapacity * MemoryLayout<MTLAccelerationStructureInstanceDescriptor>.stride
        let metaByteCount = maxInstanceCapacity * MemoryLayout<RTInstanceMeta>.stride
        guard let iDescBuf = device.makeBuffer(length: descByteCount, options: .storageModeShared),
              let iMetaBuf = device.makeBuffer(length: metaByteCount, options: .storageModeShared) else {
            throw NSError(domain: "RTRenderer", code: 106, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate TLAS instance buffers"])
        }
        memset(iDescBuf.contents(), 0, descByteCount)
        memset(iMetaBuf.contents(), 0, metaByteCount)
        instanceDescBuffer = iDescBuf
        instanceMetaBuffer = iMetaBuf

        clearAIInstanceSlots()
        writeWorldInstancesToTLASBuffers(initialWorldInstances)

        let tlasDesc = MTLInstanceAccelerationStructureDescriptor()
        tlasDesc.instancedAccelerationStructures = blasList
        tlasDesc.instanceCount = maxInstanceCapacity
        tlasDesc.instanceDescriptorBuffer = instanceDescBuffer
        tlasDesc.instanceDescriptorType = .default

        let tlasSizes = device.accelerationStructureSizes(descriptor: tlasDesc)
        guard let tlasAS = device.makeAccelerationStructure(size: tlasSizes.accelerationStructureSize),
              let tlasScr = device.makeBuffer(length: max(tlasSizes.buildScratchBufferSize, 256), options: .storageModePrivate) else {
            throw NSError(domain: "RTRenderer", code: 107, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate TLAS"])
        }
        tlas = tlasAS
        tlasScratch = tlasScr

        try rebuildTLAS(commandBuffer: nil)
    }

    private func clearAIInstanceSlots() {
        let descPtr = instanceDescBuffer.contents().bindMemory(to: MTLAccelerationStructureInstanceDescriptor.self, capacity: maxInstanceCapacity)
        let metaPtr = instanceMetaBuffer.contents().bindMemory(to: RTInstanceMeta.self, capacity: maxInstanceCapacity)
        for i in 0..<aiSlotCount {
            let slot = aiSlotBase + i
            guard slot < maxInstanceCapacity else { break }
            var instDesc = MTLAccelerationStructureInstanceDescriptor()
            instDesc.mask = 0
            instDesc.accelerationStructureIndex = 0
            descPtr[slot] = instDesc
            metaPtr[slot] = RTInstanceMeta(
                vertexOffset: 0,
                indexOffset: 0,
                triOffset: UInt32(i),
                flags: 0,
                modelMatrix: matrix_identity_float4x4,
                invTransposeMatrix: matrix_identity_float4x4,
                prevModelMatrix: matrix_identity_float4x4
            )
        }
    }

    private func writeWorldInstancesToTLASBuffers(_ activeWorldInstances: [RkptInstance]) {
        guard let w = world else {
            activeWorldInstanceCount = 0
            instanceCount = worldSlotBase
            return
        }
        let baseSlot = worldSlotBase
        let maxWorld = max(0, maxInstanceCapacity - baseSlot)
        let writeCount = min(activeWorldInstances.count, maxWorld)
        let descPtr = instanceDescBuffer.contents().bindMemory(to: MTLAccelerationStructureInstanceDescriptor.self, capacity: maxInstanceCapacity)
        let metaPtr = instanceMetaBuffer.contents().bindMemory(to: RTInstanceMeta.self, capacity: maxInstanceCapacity)

        var written = 0
        for i in 0..<writeCount {
            let wInst = activeWorldInstances[i]
            let mIdx = Int(wInst.meshIndex)
            guard mIdx >= 0 && mIdx < w.meshToBlasIndex.count else { continue }
            let localBlas = w.meshToBlasIndex[mIdx]
            guard localBlas >= 0 else { continue }
            let mesh = w.meshes[mIdx]
            // Skip obsolete PS2 floating single/few-triangle skybox cards (e.g. A81 sky triangle)
            // because our volumetric 24h Rayleigh/Mie/cloud sky dome replaces PS2 sky cards cleanly.
            if mesh.isSkyPanorama && mesh.indexCount <= 24 { continue }
            let slot = baseSlot + written
            let blasIdx = worldBlasBase + localBlas

            var mask: UInt32 = RTTLASMask.worldOpaque
            if mesh.isDrivableRoad { mask |= RTTLASMask.road }
            if mesh.isSolidWall    { mask |= RTTLASMask.wall }

            var instDesc = MTLAccelerationStructureInstanceDescriptor()
            instDesc.transformationMatrix = wInst.transform4x3
            instDesc.options = mesh.hasAlphaCutout ? .nonOpaque : .opaque
            instDesc.mask = mask
            instDesc.intersectionFunctionTableOffset = 0
            instDesc.accelerationStructureIndex = UInt32(blasIdx)
            descPtr[slot] = instDesc

            let invT = simd_transpose(simd_inverse(wInst.modelMatrix))
            let instFlags: UInt32 = (1 << 4) | (mesh.hasAlphaCutout ? (1 << 5) : 0) | (mesh.isSkyPanorama ? (1 << 6) : 0)
            metaPtr[slot] = RTInstanceMeta(
                vertexOffset: mesh.vertexOffset,
                indexOffset: mesh.indexOffset,
                triOffset: 0,
                flags: instFlags,
                modelMatrix: wInst.modelMatrix,
                invTransposeMatrix: invT,
                prevModelMatrix: wInst.modelMatrix
            )
            written += 1
        }
        activeWorldInstanceCount = written
        instanceCount = baseSlot + written
    }

    private func updateCarInstanceBuffers() {
        let descPtr = instanceDescBuffer.contents().bindMemory(to: MTLAccelerationStructureInstanceDescriptor.self, capacity: instanceCount)
        let metaPtr = instanceMetaBuffer.contents().bindMemory(to: RTInstanceMeta.self, capacity: instanceCount)
        let bodyNow = currentCarBodyXform()

        for (idx, mesh) in scene.meshes.enumerated() {
            let mat = dynamicModelMatrix(for: mesh)
            // For fast-spinning wheels, use car-body translation/heading delta (NOT tangential wheel spin!)
            // so MetalFX tracks the wheel silhouette cleanly with ZERO swirling brown wheel clouds!
            let prevMat: simd_float4x4
            switch mesh.role {
            case .wheelFL, .wheelFR, .wheelRL, .wheelRR:
                prevMat = prevCarBodyXform * localPartMatrix(for: mesh)
            default:
                prevMat = (idx < prevCarModelMatrices.count) ? prevCarModelMatrices[idx] : mat
            }

            var instDesc = MTLAccelerationStructureInstanceDescriptor()
            instDesc.transformationMatrix = RTRenderer.packed4x3(mat)
            instDesc.options = .opaque
            if mesh.isTransparent {
                instDesc.mask = RTTLASMask.glass
            } else {
                switch mesh.role {
                case .worldStatic, .streetlamp:
                    instDesc.mask = RTTLASMask.worldOpaque | RTTLASMask.road
                default:
                    instDesc.mask = RTTLASMask.car
                }
            }
            instDesc.intersectionFunctionTableOffset = 0
            instDesc.accelerationStructureIndex = UInt32(carMeshToBlasIndex[idx])
            descPtr[idx] = instDesc

            metaPtr[idx] = RTInstanceMeta(
                vertexOffset: UInt32(mesh.vertexOffset),
                indexOffset: UInt32(mesh.indexOffset),
                triOffset: UInt32(mesh.triOffset),
                flags: mesh.flags,
                modelMatrix: mat,
                invTransposeMatrix: simd_transpose(simd_inverse(mat)),
                prevModelMatrix: prevMat
            )
            if idx < prevCarModelMatrices.count {
                prevCarModelMatrices[idx] = mat
            }
        }
        prevCarBodyXform = bodyNow
    }

    private func effectiveAISlots() -> [RTAIInstanceState] {
        var slots = gameWorld.aiSlots
        if eventManager.isBossDuelActive, let boss = eventManager.bossInstanceState, !slots.isEmpty {
            slots[0] = boss
        }
        return slots
    }

    private func updateAIInstanceBuffers() {
        guard aiEnabled, world != nil, let ai = aiVehicles else {
            clearAIInstanceSlots()
            activeAIInstanceCount = 0
            return
        }
        let descPtr = instanceDescBuffer.contents().bindMemory(to: MTLAccelerationStructureInstanceDescriptor.self, capacity: maxInstanceCapacity)
        let metaPtr = instanceMetaBuffer.contents().bindMemory(to: RTInstanceMeta.self, capacity: maxInstanceCapacity)
        activeAIInstanceCount = ai.writeAITLASInstances(
            states: effectiveAISlots(),
            baseSlot: aiSlotBase,
            maxSlots: aiSlotCount,
            descriptors: descPtr,
            meta: metaPtr,
            blasBaseIndex: aiBlasBase,
            roadPitches: aiRoadPitches
        )
    }

    func rebuildTLAS(commandBuffer externalCmd: MTLCommandBuffer?, forceWorldStream: Bool = false) throws {
        if let streamer = worldStreamer {
            let res = streamer.update(playerPos: carWorldPosition, forceRebuild: forceWorldStream)
            if res.dirty {
                writeWorldInstancesToTLASBuffers(res.activeInstances)
            }
        }
        updateCarInstanceBuffers()
        updateAIInstanceBuffers()

        let tlasDesc = MTLInstanceAccelerationStructureDescriptor()
        tlasDesc.instancedAccelerationStructures = blasList
        tlasDesc.instanceCount = instanceCount
        tlasDesc.instanceDescriptorBuffer = instanceDescBuffer
        tlasDesc.instanceDescriptorType = .default

        let cmdBuf = externalCmd ?? commandQueue.makeCommandBuffer()
        guard let cb = cmdBuf,
              let accelEnc = cb.makeAccelerationStructureCommandEncoder() else {
            throw NSError(domain: "RTRenderer", code: 108, userInfo: [NSLocalizedDescriptionKey: "Failed to encode TLAS rebuild"])
        }
        accelEnc.build(accelerationStructure: tlas, descriptor: tlasDesc, scratchBuffer: tlasScratch, scratchBufferOffset: 0)
        accelEnc.endEncoding()
        if externalCmd == nil {
            cb.commit()
            cb.waitUntilCompleted()
            if let err = cb.error { throw err }
        }
    }

    // MARK: - 8-Ray GPU BVH Physics Probe (Road Elevation + Slope + Wall Collision)

    private func fillPhysicsProbeInput(predictedStep: SIMD3<Float> = .zero) {
        let fwd = SIMD3<Float>(sin(carHeadingRad), 0.0, cos(carHeadingRad))
        let right = SIMD3<Float>(cos(carHeadingRad), 0.0, -sin(carHeadingRad))
        let wb = scene.wheelBounds
        let probePos = carWorldPosition + predictedStep
        var inp = RTPhysicsProbeInput(
            carPos: SIMD4<Float>(probePos, carHeadingRad),
            carForward: SIMD4<Float>(fwd, max(wb.halfTrackF, 0.72)),
            carRight: SIMD4<Float>(right, wb.frontAxleZ),
            dims: SIMD4<Float>(wb.rearAxleZ, wb.wheelRadiusF, 0.96, 2.28)
        )
        memcpy(physicsInBuffer.contents(), &inp, MemoryLayout<RTPhysicsProbeInput>.stride)
    }

    private func predictedNextStep(dt: Float) -> SIMD3<Float> {
        let simDt = dt * physicsEngine.state.effectiveTimeScale
        let vel = physicsEngine.state.velocityWorld
        let vXZ = SIMD3<Float>(vel.x, 0.0, vel.z)
        if simd_length(vXZ) > 0.25 {
            return vXZ * simDt
        }
        let nextMs = speedKmh / 3.6
        let fwd = SIMD3<Float>(sin(carHeadingRad), 0.0, cos(carHeadingRad))
        return fwd * (nextMs * simDt)
    }

    func encodePhysicsProbe(commandBuffer: MTLCommandBuffer, predictedStep: SIMD3<Float> = .zero) {
        guard world != nil, let enc = commandBuffer.makeComputeCommandEncoder() else { return }
        fillPhysicsProbeInput(predictedStep: predictedStep)
        enc.setComputePipelineState(physicsProbePSO)
        enc.setAccelerationStructure(tlas, bufferIndex: 0)
        enc.setBuffer(physicsInBuffer, offset: 0, index: 1)
        enc.setBuffer(physicsOutBuffer, offset: 0, index: 2)
        if world?.hasResidentBLASes == true {
            enc.useResources(carBlasList, usage: .read)
        } else {
            enc.useResources(blasList, usage: .read)
        }
        enc.dispatchThreads(MTLSize(width: 1, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 1, height: 1, depth: 1))
        enc.endEncoding()
    }

    private func readPhysicsProbeOutput() -> RTPhysicsProbeOutput {
        var out = RTPhysicsProbeOutput(
            wheelHitY: .zero,
            wallDist: .zero,
            wallNormalFront: .zero,
            wallNormalSide: .zero,
            skyProbe: SIMD4<Float>(0.0, 999.0, 0.0, 0.0)
        )
        memcpy(&out, physicsOutBuffer.contents(), MemoryLayout<RTPhysicsProbeOutput>.stride)
        return out
    }

    // MARK: - 32-Slot GPU BVH AI Road Elevation & Slope Probe (aiRoadProbeKernel)

    func encodeAIRoadProbe(commandBuffer: MTLCommandBuffer) {
        guard aiEnabled, world != nil, let pso = aiRoadProbePSO,
              let enc = commandBuffer.makeComputeCommandEncoder() else { return }
        let inPtr = aiRoadProbeInBuffer.contents().bindMemory(to: RTAIRoadProbeInput.self, capacity: RTGameWorld.maxAISlots)
        let slots = effectiveAISlots()
        let n = min(slots.count, RTGameWorld.maxAISlots)
        for i in 0..<n {
            let u = slots[i]
            let fwd = SIMD3<Float>(sin(u.yawRad), 0.0, cos(u.yawRad))
            inPtr[i] = RTAIRoadProbeInput(
                posActive: SIMD4<Float>(u.worldPosition, u.active ? 1.0 : 0.0),
                fwdHalfWB: SIMD4<Float>(fwd, max(1.0, u.archetype.halfLength * 0.62))
            )
        }
        var countU32 = UInt32(n)
        enc.setComputePipelineState(pso)
        enc.setAccelerationStructure(tlas, bufferIndex: 0)
        enc.setBuffer(aiRoadProbeInBuffer, offset: 0, index: 1)
        enc.setBuffer(aiRoadProbeOutBuffer, offset: 0, index: 2)
        enc.setBytes(&countU32, length: MemoryLayout<UInt32>.stride, index: 3)
        if world?.hasResidentBLASes == true {
            enc.useResources(carBlasList, usage: .read)
            aiVehicles?.useResources(on: enc)
        } else {
            enc.useResources(blasList, usage: .read)
        }
        let tg = min(pso.maxTotalThreadsPerThreadgroup, max(1, n))
        enc.dispatchThreads(MTLSize(width: n, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: tg, height: 1, depth: 1))
        enc.endEncoding()
    }

    private func ingestAIRoadProbeOutput(dt: Float, snapImmediate: Bool = false) {
        guard aiEnabled, world != nil else { return }
        let outPtr = aiRoadProbeOutBuffer.contents().bindMemory(to: RTAIRoadProbeOutput.self, capacity: RTGameWorld.maxAISlots)
        var heights = [Float](repeating: -9999.0, count: RTGameWorld.maxAISlots)
        let alpha: Float = snapImmediate ? 1.0 : min(1.0, max(0.05, dt * 14.0))
        for i in 0..<RTGameWorld.maxAISlots {
            let o = outPtr[i]
            heights[i] = o.roadY
            if o.roadY > -5000.0 {
                aiRoadPitches[i] += (o.pitchRad - aiRoadPitches[i]) * alpha
            } else if snapImmediate {
                aiRoadPitches[i] = 0.0
            }
        }
        gameWorld.applyRoadProbeHeights(heights, dt: dt, snapImmediate: snapImmediate)
        if eventManager.isBossDuelActive && !heights.isEmpty {
            eventManager.applyBossRoadProbeHeight(heights[0], dt: dt, snapImmediate: snapImmediate)
        }
    }

    func snapAIVehiclesToRoad() {
        guard aiEnabled, world != nil, aiRoadProbePSO != nil,
              let cb = commandQueue.makeCommandBuffer() else { return }
        encodeAIRoadProbe(commandBuffer: cb)
        cb.commit()
        cb.waitUntilCompleted()
        ingestAIRoadProbeOutput(dt: 1.0, snapImmediate: true)
        try? rebuildTLAS(commandBuffer: nil)
    }

    func snapCarToWorldRoad() {
        guard world != nil, let cb = commandQueue.makeCommandBuffer() else { return }
        encodePhysicsProbe(commandBuffer: cb)
        cb.commit()
        cb.waitUntilCompleted()
        let probe = readPhysicsProbeOutput()
        rawSkyOcclusion = max(0.0, min(1.0, probe.skyProbe.x))
        skyCeilingDist = probe.skyProbe.y
        smoothedSkyOcclusion = rawSkyOcclusion
        audioEngine.setTunnelOcclusion(smoothedSkyOcclusion)
        let hits = [probe.wheelHitY.x, probe.wheelHitY.y, probe.wheelHitY.z, probe.wheelHitY.w].filter { $0 > -5000.0 }
        if !hits.isEmpty {
            let avgY = hits.reduce(0.0, +) / Float(hits.count)
            carWorldPosition.y = avgY
            if probe.wheelHitY.x > -5000.0 && probe.wheelHitY.y > -5000.0 &&
               probe.wheelHitY.z > -5000.0 && probe.wheelHitY.w > -5000.0 {
                let wb = scene.wheelBounds
                let wheelbase = max(1.8, wb.frontAxleZ - wb.rearAxleZ)
                let trackWidth = max(1.2, wb.halfTrackF + wb.halfTrackR)
                let frontY = 0.5 * (probe.wheelHitY.x + probe.wheelHitY.y)
                let rearY  = 0.5 * (probe.wheelHitY.z + probe.wheelHitY.w)
                let leftY  = 0.5 * (probe.wheelHitY.x + probe.wheelHitY.z)
                let rightY = 0.5 * (probe.wheelHitY.y + probe.wheelHitY.w)
                terrainPitchRad = max(-0.22, min(0.22, -atan2(frontY - rearY, wheelbase)))
                // rotationZ(+r) lifts local +X (right wheel) upward by +x*sin(r)
                terrainRollRad  = max(-0.18, min(0.18, atan2(rightY - leftY, trackWidth)))
            }
            physicsEngine.snapGround(y: carWorldPosition.y, terrainPitch: terrainPitchRad, terrainRoll: terrainRollRad)
            try? rebuildTLAS(commandBuffer: nil)
        }
    }

    // MARK: - Render Targets & MetalFX Temporal Denoised Scaler

    private func computeRTResolution(outW: Int, outH: Int, useMetalFX: Bool) -> (Int, Int) {
        if useMetalFX && outW >= 1280 {
            // 2.0x neural temporal upscale (960x540 -> 1920x1080, 1.75ms on M5 Pro MXU!)
            return (max(64, outW / 2), max(64, outH / 2))
        }
        return (outW, outH)
    }

    private func ensureRenderTargets(width: Int, height: Int, useMetalFX: Bool, useFrameGen: Bool = false) {
        let wantsMFX = useMetalFX && supportsMetalFXDenoiser
        let wantsFG = useFrameGen && wantsMFX && supportsFrameInterpolator
        let (inW, inH) = computeRTResolution(outW: width, outH: height, useMetalFX: wantsMFX)

        if width == targetWidth && height == targetHeight &&
           inW == rtWidth && inH == rtHeight &&
           metalFXActive == wantsMFX &&
           frameGenActive == wantsFG &&
           accumDiffTex != nil {
            return
        }

        targetWidth = width
        targetHeight = height
        rtWidth = inW
        rtHeight = inH
        metalFXActive = wantsMFX
        frameGenActive = false
        frameInterpolator = nil

        var colorExtraUsage: MTLTextureUsage = []
        var depthExtraUsage: MTLTextureUsage = []
        var motionExtraUsage: MTLTextureUsage = []
        var diffAlbExtraUsage: MTLTextureUsage = []
        var specAlbExtraUsage: MTLTextureUsage = []
        var normalExtraUsage: MTLTextureUsage = []
        var roughExtraUsage: MTLTextureUsage = []
        var reactiveExtraUsage: MTLTextureUsage = []
        var outExtraUsage: MTLTextureUsage = []
        var fiColorExtraUsage: MTLTextureUsage = []
        var fiOutExtraUsage: MTLTextureUsage = []

        if wantsMFX {
            if #available(macOS 26.0, *) {
                let desc = MTLFXTemporalDenoisedScalerDescriptor()
                desc.colorTextureFormat = .rgba16Float
                desc.depthTextureFormat = .r32Float
                desc.motionTextureFormat = .rg16Float
                desc.diffuseAlbedoTextureFormat = .rgba16Float
                desc.specularAlbedoTextureFormat = .rgba16Float
                desc.normalTextureFormat = .rgba16Float
                desc.roughnessTextureFormat = .r16Float
                desc.isReactiveMaskTextureEnabled = true
                desc.reactiveMaskTextureFormat = .r8Unorm
                desc.outputTextureFormat = .rgba16Float
                desc.inputWidth = inW
                desc.inputHeight = inH
                desc.outputWidth = width
                desc.outputHeight = height
                desc.isAutoExposureEnabled = false
                desc.requiresSynchronousInitialization = true

                if let scaler = desc.makeTemporalDenoisedScaler(device: device) {
                    temporalScaler = scaler
                    scalerInputW = inW
                    scalerInputH = inH
                    scalerOutputW = width
                    scalerOutputH = height
                    colorExtraUsage = scaler.colorTextureUsage
                    depthExtraUsage = scaler.depthTextureUsage
                    motionExtraUsage = scaler.motionTextureUsage
                    diffAlbExtraUsage = scaler.diffuseAlbedoTextureUsage
                    specAlbExtraUsage = scaler.specularAlbedoTextureUsage
                    normalExtraUsage = scaler.normalTextureUsage
                    roughExtraUsage = scaler.roughnessTextureUsage
                    if #available(macOS 27.0, *) {
                        reactiveExtraUsage = scaler.reactiveMaskTextureUsage
                    } else {
                        reactiveExtraUsage = .shaderRead
                    }
                    outExtraUsage = scaler.outputTextureUsage
                } else {
                    temporalScaler = nil
                    metalFXActive = false
                }

                if wantsFG && metalFXActive {
                    let fiDesc = MTLFXFrameInterpolatorDescriptor()
                    fiDesc.colorTextureFormat = .bgra8Unorm
                    fiDesc.outputTextureFormat = .bgra8Unorm
                    fiDesc.depthTextureFormat = .r32Float
                    fiDesc.motionTextureFormat = .rg16Float
                    fiDesc.inputWidth = inW
                    fiDesc.inputHeight = inH
                    fiDesc.outputWidth = width
                    fiDesc.outputHeight = height
                    fiDesc.scaler = nil // Standalone post-ACES pre-HUD mode (1.80ms verified on M5 Pro, zero HUD warp!)
                    if #available(macOS 27.0, *) {
                        fiDesc.requiresPrevColorTexture = true
                    }
                    if let fi = fiDesc.makeFrameInterpolator(device: device) {
                        frameInterpolator = fi
                        frameGenActive = true
                        fiColorExtraUsage = fi.colorTextureUsage
                        fiOutExtraUsage = fi.outputTextureUsage
                        depthExtraUsage.formUnion(fi.depthTextureUsage)
                        motionExtraUsage.formUnion(fi.motionTextureUsage)
                    }
                }
            }
        } else {
            temporalScaler = nil
        }

        func makeTex(_ format: MTLPixelFormat, w: Int, h: Int, extra: MTLTextureUsage = []) -> MTLTexture? {
            let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: format, width: w, height: h, mipmapped: false)
            d.usage = extra.union([.shaderRead, .shaderWrite])
            d.storageMode = .private
            return device.makeTexture(descriptor: d)
        }

        accumDiffTex = makeTex(.rgba32Float, w: inW, h: inH)
        accumSpecTex = makeTex(.rgba32Float, w: inW, h: inH)
        gAlbedoTex = makeTex(.rgba16Float, w: inW, h: inH, extra: diffAlbExtraUsage)
        gNormalRoughTex = makeTex(.rgba16Float, w: inW, h: inH, extra: normalExtraUsage)
        gDepthTex = makeTex(.r32Float, w: inW, h: inH, extra: depthExtraUsage)
        gEmissiveTex = makeTex(.rgba16Float, w: inW, h: inH)
        gColorTex = makeTex(.rgba16Float, w: inW, h: inH, extra: colorExtraUsage)
        gMotionTex = makeTex(.rg16Float, w: inW, h: inH, extra: motionExtraUsage)
        gSpecAlbedoTex = makeTex(.rgba16Float, w: inW, h: inH, extra: specAlbExtraUsage)
        gRoughnessTex = makeTex(.r16Float, w: inW, h: inH, extra: roughExtraUsage)
        gReactiveTex = makeTex(.r8Unorm, w: inW, h: inH, extra: reactiveExtraUsage)

        histDiffTex = makeTex(.rgba16Float, w: inW, h: inH)
        histSpecTex = makeTex(.rgba16Float, w: inW, h: inH)
        histNormalRoughTex = makeTex(.rgba16Float, w: inW, h: inH)
        histDepthTex = makeTex(.r32Float, w: inW, h: inH)

        denoiseDiffPing = makeTex(.rgba16Float, w: inW, h: inH)
        denoiseDiffPong = makeTex(.rgba16Float, w: inW, h: inH)
        denoiseSpecPing = makeTex(.rgba16Float, w: inW, h: inH)
        denoiseSpecPong = makeTex(.rgba16Float, w: inW, h: inH)

        hdrResolvedTex = makeTex(.rgba16Float, w: width, h: height, extra: outExtraUsage)

        let bw = max(1, width / 2)
        let bh = max(1, height / 2)
        bloomTexA = makeTex(.rgba16Float, w: bw, h: bh)
        bloomTexB = makeTex(.rgba16Float, w: bw, h: bh)

        ldrColorTexA = makeTex(.bgra8Unorm, w: width, h: height, extra: fiColorExtraUsage)
        ldrColorTexB = makeTex(.bgra8Unorm, w: width, h: height, extra: fiColorExtraUsage)
        ldrInterpTex = makeTex(.bgra8Unorm, w: width, h: height, extra: fiOutExtraUsage)
        ldrWriteToA = true

        hasValidPrevFrame = false
        historyResetPending = true
        frameGenHistoryValid = false
        frameGenPresentPhase = 0
        resetAccumulation()
    }

    private func effectiveRainAmount() -> Float {
        if activePresetKey == "night_neon" || activePresetKey == "night_rain" {
            return max(rainIntensity, 0.85)
        }
        return rainIntensity
    }

    private func resolvedPreset() -> RTLightingPreset {
        if timeCycleEnabled || explicitTimeOfDay != nil {
            let h = explicitTimeOfDay ?? timeOfDayHours
            return RTSceneBuilder.lightingPreset(forTimeOfDayHours: h, rainIntensity: rainIntensity)
        }
        let key = activePresetKey == "night_rain" ? "night_neon" : activePresetKey
        let base = scene.presets[key] ?? scene.presets["golden_hour"]!
        if rainIntensity <= 0.01 || key == "night_neon" {
            return base
        }
        // Live rain weather modulation on top of static presets (B toggle)
        let r = max(0.0, min(1.0, rainIntensity))
        return RTLightingPreset(
            key: base.key,
            name: base.name,
            sunDirection: base.sunDirection,
            sunColor: simd_mix(base.sunColor, SIMD3<Float>(0.62, 0.70, 0.84), SIMD3<Float>(repeating: r * 0.45)),
            sunIntensity: base.sunIntensity * (1.0 - r * 0.38),
            sunConeRadius: base.sunConeRadius + r * 0.016,
            coronaColor: base.coronaColor,
            skyZenith: simd_mix(base.skyZenith, SIMD3<Float>(0.022, 0.032, 0.058), SIMD3<Float>(repeating: r * 0.45)),
            skyHorizon: simd_mix(base.skyHorizon, SIMD3<Float>(0.22, 0.26, 0.34), SIMD3<Float>(repeating: r * 0.45)),
            skyGround: base.skyGround,
            iblIntensity: base.iblIntensity * (1.0 - r * 0.12),
            wetness: min(0.95, base.wetness + r * (0.94 - base.wetness)),
            fogDensity: base.fogDensity + r * 0.0042,
            exposure: base.exposure,
            bloomThreshold: base.bloomThreshold,
            bloomIntensity: base.bloomIntensity + r * 0.12,
            headlightsOn: max(base.headlightsOn, r * 1.15),
            neonTint: base.neonTint,
            neonIntensity: max(base.neonIntensity, r * 0.75),
            cloudCoverage: min(0.92, base.cloudCoverage + r * 0.35)
        )
    }

    private func resolvedSunDirection(preset: RTLightingPreset) -> SIMD3<Float> {
        if lookdevConfig.sun_elevation_deg == nil && lookdevConfig.sun_azimuth_deg == nil {
            return simd_normalize(preset.sunDirection)
        }
        let defaultDir = simd_normalize(preset.sunDirection)
        let defaultEl = asin(max(-1.0, min(1.0, defaultDir.y))) * 180.0 / .pi
        let defaultAz = atan2(defaultDir.x, defaultDir.z) * 180.0 / .pi
        let el = (lookdevConfig.sun_elevation_deg ?? defaultEl) * .pi / 180.0
        let az = (lookdevConfig.sun_azimuth_deg ?? defaultAz) * .pi / 180.0
        return simd_normalize(SIMD3<Float>(cos(el) * sin(az), sin(el), cos(el) * cos(az)))
    }

    private func updateDynamicFXBuffer() {
        let p = resolvedPreset()
        let wb = scene.wheelBounds
        let effRain = effectiveRainAmount()
        let lightCount = Float(worldStreamer?.lightGridParams.lightCount ?? 0)
        var fx = RTDynamicFXGPU.zeroed()
        fx.timeWeather = SIMD4<Float>(simTimeSec, explicitTimeOfDay ?? timeOfDayHours, effRain, lookdevConfig.puddle_wetness ?? p.wetness)
        fx.carMetrics = SIMD4<Float>(wb.frontAxleZ, wb.rearAxleZ, wb.wheelRadiusF, wb.wheelRadiusR)
        fx.nosParams = SIMD4<Float>(nosActiveStrength, 1.05 + 0.35 * nosActiveStrength, speedbreakerStrength, lightCount)
        if aiEnabled && world != nil {
            let cl = gameWorld.copLights
            if cl.count >= 4 {
                fx.copLightPos = (
                    SIMD4<Float>(cl[0].worldPos, cl[0].strobePhase),
                    SIMD4<Float>(cl[1].worldPos, cl[1].strobePhase),
                    SIMD4<Float>(cl[2].worldPos, cl[2].strobePhase),
                    SIMD4<Float>(cl[3].worldPos, cl[3].strobePhase)
                )
                fx.copLightCol = (
                    SIMD4<Float>(cl[0].colorHDR, cl[0].intensity),
                    SIMD4<Float>(cl[1].colorHDR, cl[1].intensity),
                    SIMD4<Float>(cl[2].colorHDR, cl[2].intensity),
                    SIMD4<Float>(cl[3].colorHDR, cl[3].intensity)
                )
            }
        }
        memcpy(dynamicFXBuffer.contents(), &fx, MemoryLayout<RTDynamicFXGPU>.stride)
    }

    private func updateEventWorldFXBuffer() {
        var evFX = eventManager.currentWorldFX(simTime: simTimeSec)
        memcpy(eventWorldFXBuffer.contents(), &evFX, RTEventWorldFX.byteSize)
    }

    private func updateMinimapBuffer() {
        if world != nil && !gameWorld.roadGraph.isEmpty {
            minimapBuffer = gameWorld.packMinimap(
                playerPos: carWorldPosition,
                playerYaw: carHeadingRad,
                mapRangeM: 175.0
            )
            if eventManager.isEventActiveOrCountdown {
                let evFX = eventManager.currentWorldFX(simTime: simTimeSec)
                let range: Float = max(1.0, minimapBuffer.mapRangeM)
                let cy = cos(carHeadingRad)
                let sy = sin(carHeadingRad)
                func project(_ wp: SIMD3<Float>) -> SIMD2<Float> {
                    let dx = wp.x - carWorldPosition.x
                    let dz = wp.z - carWorldPosition.z
                    let rx = (dx * cy - dz * sy) / range
                    let fy = (dx * sy + dz * cy) / range
                    let len = sqrt(rx * rx + fy * fy)
                    if len > 0.94 {
                        return SIMD2<Float>(rx / len * 0.94, fy / len * 0.94)
                    }
                    return SIMD2<Float>(rx, fy)
                }
                var bCount = Int(minimapBuffer.blipCount)
                if evFX.fxParams.x > 0.5 {
                    let gp = project(SIMD3<Float>(evFX.gatePosRadius.x, evFX.gatePosRadius.y, evFX.gatePosRadius.z))
                    let idx = min(bCount, RTMinimapBuffer.maxBlips - 1)
                    minimapBuffer.setBlip(idx, SIMD4<Float>(gp.x, gp.y, 3.0, simTimeSec))
                    bCount = min(RTMinimapBuffer.maxBlips, idx + 1)
                }
                if eventManager.isBossDuelActive, let boss = eventManager.bossInstanceState {
                    let bp = project(boss.worldPosition)
                    let idx = min(bCount, RTMinimapBuffer.maxBlips - 1)
                    minimapBuffer.setBlip(idx, SIMD4<Float>(bp.x, bp.y, 2.0, simTimeSec))
                    bCount = min(RTMinimapBuffer.maxBlips, idx + 1)
                }
                minimapBuffer.blipCount = UInt32(bCount)
            }
            return
        }
        minimapBuffer.clear()
        minimapBuffer.mapRangeM = 175.0
        minimapBuffer.playerYawRad = carHeadingRad
    }

    private func makeHudExtUniforms() -> RTHudExtUniforms {
        let st = physicsEngine.state
        var ext = RTHudExtUniforms()
        ext.rpmNorm = st.rpmNorm
        ext.nosNorm = st.nosCharge
        ext.sbNorm = st.speedbreakerCharge
        ext.fps = smoothedFps
        ext.timeOfDayHours = explicitTimeOfDay ?? timeOfDayHours
        ext.simTimeSec = simTimeSec
        var flags: UInt32 = 0
        if hudEnabled { flags |= RTHudFlagBits.visible }
        if effectiveFrameGenActive { flags |= RTHudFlagBits.frameGenOn }
        if st.nosActive || nosActiveStrength > 0.12 { flags |= RTHudFlagBits.nosActive }
        if st.speedbreakerActive || speedbreakerStrength > 0.12 { flags |= RTHudFlagBits.sbActive }
        ext.hudFlags = flags
        if aiEnabled && world != nil {
            gameWorld.fillHudExt(&ext)
        }
        ext.simTimeSec = simTimeSec
        ext.districtIndex = UInt32(activeLandmarkIndex)
        ext.carIndex = scene.carID
        return ext
    }

    private func makeMusicBannerUniforms() -> RTMusicBannerUniforms {
        var banner = RTMusicBannerUniforms()
        if forceShowMusicBanner && musicBannerTitle.isEmpty {
            syncMusicBannerFromAudio(triggerSlide: false)
        }
        guard !musicBannerTitle.isEmpty || !musicBannerArtist.isEmpty else {
            return banner
        }
        let alpha: Float
        let slideT: Float
        if forceShowMusicBanner {
            alpha = 1.0
            slideT = 1.0
        } else if musicBannerTimer > 0.001 {
            let elapsed = musicBannerDuration - musicBannerTimer
            slideT = min(1.0, max(0.0, elapsed / 0.38))
            let fadeIn = min(1.0, max(0.0, elapsed / 0.25))
            let fadeOut = min(1.0, max(0.0, musicBannerTimer / 0.75))
            alpha = min(fadeIn, fadeOut)
        } else {
            return banner
        }

        banner.bannerAlpha = alpha
        banner.slideT = slideT
        banner.trackIndex = UInt32(max(1, musicBannerTrackIndex))
        banner.totalTracks = UInt32(max(1, musicBannerTotalTracks))
        var mFlags: UInt32 = RTMusicBannerFlagBits.visible
        if musicBannerIsMuted { mFlags |= RTMusicBannerFlagBits.muted }
        if musicBannerIsPursuit { mFlags |= RTMusicBannerFlagBits.pursuitScore }
        if smoothedSkyOcclusion > 0.35 { mFlags |= RTMusicBannerFlagBits.tunnelReverb }
        banner.flags = mFlags
        banner.skyOcclusion = smoothedSkyOcclusion
        banner.setText(artist: musicBannerArtist, title: musicBannerTitle)
        return banner
    }

    private func makeEventHudUniforms() -> RTEventHudUniforms {
        var u = RTEventHudUniforms()
        let hs = eventManager.currentHudState(playerPos: carWorldPosition)
        u.mode = hs.mode
        u.state = hs.state
        u.cpIndex = hs.cpTotal > 0 ? UInt32(min(Int(hs.cpIndex) + 1, Int(hs.cpTotal))) : 0
        u.cpTotal = hs.cpTotal
        u.elapsedSec = hs.elapsedSec
        u.currentScore = hs.currentScore
        u.targetScore = hs.targetScore
        u.bossGapM = hs.bossGapM
        u.playerRank = max(1, hs.rank)
        u.bossRank = max(1, hs.bossRank)
        u.nextGateDistM = hs.nextGateDistM

        var flags: UInt32 = 0
        if hs.mode != 0 && hs.state != 0 { flags |= RTEventHudFlagBits.eventActive }
        if hs.worldMapOpen { flags |= RTEventHudFlagBits.worldMapOpen }
        if !hs.mapMarkers.isEmpty { flags |= RTEventHudFlagBits.hasSelectedMarker }
        if hs.radarFlash > 0.05 { flags |= RTEventHudFlagBits.radarFlash }
        u.flags = flags

        let pUV = eventManager.worldXZToMapUV(SIMD2<Float>(carWorldPosition.x, carWorldPosition.z))
        let mCount = min(hs.mapMarkers.count, RTEventHudUniforms.maxMarkers)
        u.mapViewParams = SIMD4<Float>(hs.mapCenterNorm.x, hs.mapCenterNorm.y, hs.mapZoom, carHeadingRad)
        u.playerMapPos = SIMD4<Float>(pUV.x, pUV.y, Float(hs.selectedMarkerIndex), Float(mCount))

        for i in 0..<mCount {
            let mk = hs.mapMarkers[i]
            u.setMarker(i, SIMD4<Float>(mk.normUV.x, mk.normUV.y, Float(mk.kind), Float(mk.rank)))
        }
        let pts = hs.gpsRoutePointsNorm
        let segs = min(max(0, pts.count - 1), RTEventHudUniforms.maxGpsSegments)
        for i in 0..<segs {
            u.setGpsSegment(i, SIMD4<Float>(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y))
        }
        u.gpsSegCount = UInt32(segs)

        u.setEventTitle(hs.eventTitle)
        let bossBanner = hs.bossCarName.isEmpty ? hs.bossName : "\(hs.bossName) - \(hs.bossCarName)"
        u.setBossName(bossBanner)
        u.setSelectedCard(title: hs.selectedMarkerTitle, subtitle: hs.selectedMarkerSubtitle)
        return u
    }

    private static func halton(_ index: UInt32, base: UInt32) -> Float {
        var f: Float = 1.0
        var r: Float = 0.0
        var i = index
        while i > 0 {
            f /= Float(base)
            r += f * Float(i % base)
            i /= base
        }
        return r
    }

    private func makeUniforms(
        width: Int,
        height: Int,
        sampleIdx: UInt32,
        splitCompare: Bool,
        temporal1SPP: Bool,
        jitter: SIMD2<Float>
    ) -> RTFrameUniforms {
        let p = resolvedPreset()
        let aspect = Float(width) / Float(max(height, 1))
        let vp = camera.viewProjMatrix(aspect: aspect)
        let invVP = simd_inverse(vp)
        let prevVP = hasValidPrevFrame ? prevViewProj : vp
        let b = camera.basis()

        let exposure = lookdevConfig.exposure ?? p.exposure
        let wetness = lookdevConfig.puddle_wetness ?? p.wetness
        let cloudCov = lookdevConfig.cloud_coverage ?? p.cloudCoverage
        let ccRough = lookdevConfig.clearcoat_roughness ?? 0.013
        let flakeStrength = lookdevConfig.flake_strength ?? 0.0052
        let bloomThresh = lookdevConfig.bloom_threshold ?? p.bloomThreshold
        let bloomInt = lookdevConfig.bloom_intensity ?? p.bloomIntensity
        var headsOn = lookdevConfig.headlights_on ?? p.headlightsOn
        // #91 P2-1 Bonus: Auto-enable player xenon headlight beams whenever skyOcclusion > 0.50 (underground tunnels!)
        if smoothedSkyOcclusion > 0.50 {
            headsOn = max(headsOn, 1.0 + smoothedSkyOcclusion)
        }
        let vignette = lookdevConfig.vignette ?? 0.12
        let contrast = lookdevConfig.contrast ?? 1.03
        let saturation = lookdevConfig.saturation ?? 1.08
        let denoisePasses = Float(denoiserEnabled ? (lookdevConfig.denoise_passes ?? (temporal1SPP ? 3 : 4)) : 0)

        let gearVal = Float(physicsEngine.state.gear)
        let rpmNorm = physicsEngine.state.rpmNorm
        let worldTexCount = Float(world?.textures.count ?? 1)
        let yawForLights = (world != nil) ? carHeadingRad : carYawRad

        return RTFrameUniforms(
            invViewProjMatrix: invVP,
            currViewProjMatrix: vp,
            prevViewProjMatrix: prevVP,
            cameraPos: SIMD4<Float>(b.eye, camera.fovDeg),
            cameraForward: SIMD4<Float>(b.forward, aspect),
            cameraRight: SIMD4<Float>(b.right, tan(camera.fovDeg * .pi / 360.0)),
            cameraUp: SIMD4<Float>(b.up, exposure),
            sunDir: SIMD4<Float>(resolvedSunDirection(preset: p), p.sunIntensity),
            sunColor: SIMD4<Float>(p.sunColor, p.sunConeRadius),
            coronaColor: SIMD4<Float>(p.coronaColor, p.iblIntensity),
            skyZenith: SIMD4<Float>(p.skyZenith, wetness),
            skyHorizon: SIMD4<Float>(p.skyHorizon, p.fogDensity),
            skyGround: SIMD4<Float>(p.skyGround, headsOn),
            neonParams: SIMD4<Float>(p.neonTint, p.neonIntensity),
            postParams: SIMD4<Float>(bloomThresh, bloomInt, (liveryEnabled && scene.carSupportsLivery) ? 1.0 : 0.0, rtEnabled ? 1.0 : 0.0),
            rtParams: SIMD4<Float>(Float(width), Float(height), Float(sampleIdx), splitCompare ? 1.0 : 0.0),
            envParams: SIMD4<Float>(cloudCov, roadScrollZ, ccRough, flakeStrength),
            gradeParams: SIMD4<Float>(vignette, contrast, saturation, denoisePasses),
            carDynParams: SIMD4<Float>(steerDeg * .pi / 180.0, wheelSpinRad, yawForLights, carRollRad),
            driveParams: SIMD4<Float>(speedKmh, steerDeg, scene.hasAuthenticCarTextures ? 1.0 : 0.0, scene.carSupportsLivery ? 1.0 : 0.0),
            hudParams: SIMD4<Float>(hudEnabled ? 1.0 : 0.0, rpmNorm, gearVal, Float(scene.carID)),
            jitterParams: SIMD4<Float>(jitter.x, jitter.y, temporal1SPP ? 1.0 : 0.0, worldTexCount),
            carWorldPos: SIMD4<Float>(carWorldPosition, world != nil ? 1.0 : 0.0),
            lightGridParams: worldStreamer?.lightGridParams.originInvCellVec4 ?? .zero,
            fxParams: SIMD4<Float>(effectiveRainAmount(), speedbreakerStrength, explicitTimeOfDay ?? timeOfDayHours, Float(worldStreamer?.lightGridParams.lightCount ?? 0))
        )
    }

    private func dispatch2D(encoder: MTLComputeCommandEncoder, pso: MTLComputePipelineState, width: Int, height: Int) {
        let tw = pso.threadExecutionWidth
        let th = max(1, pso.maxTotalThreadsPerThreadgroup / tw)
        let tgSize = MTLSize(width: tw, height: th, depth: 1)
        let gridSize = MTLSize(width: width, height: height, depth: 1)
        encoder.dispatchThreads(gridSize, threadsPerThreadgroup: tgSize)
    }

    func encodeRayTraceSample(
        commandBuffer: MTLCommandBuffer,
        width: Int,
        height: Int,
        sampleIdx: UInt32,
        splitCompare: Bool,
        temporal1SPP: Bool,
        jitter: SIMD2<Float>
    ) {
        guard let diff = accumDiffTex,
              let spec = accumSpecTex,
              let gAlb = gAlbedoTex,
              let gNR = gNormalRoughTex,
              let gDep = gDepthTex,
              let gEm = gEmissiveTex,
              let gCol = gColorTex,
              let gMot = gMotionTex,
              let gSpecAlb = gSpecAlbedoTex,
              let gRough = gRoughnessTex,
              let gReact = gReactiveTex,
              let enc = commandBuffer.makeComputeCommandEncoder() else { return }

        var uniforms = makeUniforms(
            width: width,
            height: height,
            sampleIdx: sampleIdx,
            splitCompare: splitCompare,
            temporal1SPP: temporal1SPP,
            jitter: jitter
        )

        enc.setComputePipelineState(raytracePSO)
        enc.setTexture(diff, index: 0)
        enc.setTexture(spec, index: 1)
        enc.setTexture(gAlb, index: 2)
        enc.setTexture(gNR, index: 3)
        enc.setTexture(gDep, index: 4)
        enc.setTexture(gEm, index: 5)
        enc.setTexture(scene.asphaltDiffTexture, index: 6)
        enc.setTexture(scene.asphaltNormalTexture, index: 7)
        enc.setTexture(scene.asphaltRoughTexture, index: 8)
        enc.setTexture(scene.concreteDiffTexture, index: 9)
        enc.setTexture(scene.concreteNormalTexture, index: 10)
        enc.setTexture(scene.carbonDiffTexture, index: 11)
        enc.setTexture(scene.carbonNormalTexture, index: 12)
        enc.setTexture(scene.carLiveryMaskTexture, index: 13)
        enc.setTexture(scene.carMiscTexture, index: 14)
        enc.setTexture(scene.carBadgeTexture, index: 15)
        enc.setTexture(scene.carHeadlightTexture, index: 16)
        enc.setTexture(scene.carTaillightTexture, index: 17)
        enc.setTexture(scene.carTireTexture, index: 18)
        enc.setTexture(scene.carInteriorTexture, index: 19)
        enc.setTexture(gCol, index: 20)
        enc.setTexture(gMot, index: 21)
        enc.setTexture(gSpecAlb, index: 22)
        enc.setTexture(gRough, index: 23)
        enc.setTexture(gReact, index: 24)
        enc.setSamplerState(repeatSampler, index: 0)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        enc.setAccelerationStructure(tlas, bufferIndex: 1)
        enc.setBuffer(scene.vertexBuffer, offset: 0, index: 2)
        enc.setBuffer(scene.indexBuffer, offset: 0, index: 3)
        enc.setBuffer(scene.triMaterialBuffer, offset: 0, index: 4)
        enc.setBuffer(instanceMetaBuffer, offset: 0, index: 5)
        enc.setBuffer(scene.materialBuffer, offset: 0, index: 6)
        updateDynamicFXBuffer()
        updateEventWorldFXBuffer()
        enc.setBuffer(world?.vertexBuffer ?? dummyWorldVB, offset: 0, index: 7)
        enc.setBuffer(world?.indexBuffer ?? dummyWorldIB, offset: 0, index: 8)
        enc.setBuffer(world?.materialBuffer ?? dummyWorldMB, offset: 0, index: 9)
        enc.setBuffer(world?.bindlessTexIDBuffer ?? dummyWorldTB, offset: 0, index: 10)
        enc.setBuffer(worldStreamer?.worldLightsBuffer ?? worldLightsBuffer, offset: 0, index: 11)
        enc.setBuffer(worldStreamer?.lightGridBuffer ?? worldLightGridBuffer, offset: 0, index: 12)
        enc.setBuffer(dynamicFXBuffer, offset: 0, index: 13)
        enc.setBuffer(aiVehicles?.sceneDataBuffer ?? aiSceneDataFallbackBuffer, offset: 0, index: 14)
        enc.setBuffer(eventWorldFXBuffer, offset: 0, index: 16)
        if world?.hasResidentBLASes == true {
            enc.useResources(carBlasList, usage: .read)
            aiVehicles?.useResources(on: enc)
        } else {
            enc.useResources(blasList, usage: .read)
            aiVehicles?.useResources(on: enc)
        }
        if let wHeap = world?.textureHeap {
            enc.useHeap(wHeap)
        } else if let wTex = world?.textures, !wTex.isEmpty {
            enc.useResources(wTex, usage: .read)
        } else {
            enc.useResource(dummyWorldTex, usage: .read)
        }
        dispatch2D(encoder: enc, pso: raytracePSO, width: width, height: height)
        enc.endEncoding()
    }

    func encodeHudOverlay(
        commandBuffer: MTLCommandBuffer,
        inputTexture: MTLTexture,
        outputTexture: MTLTexture,
        encodeSRGB: Bool,
        splitCompare: Bool = false,
        temporal1SPP: Bool = false,
        jitter: SIMD2<Float> = .zero
    ) {
        guard let enc = commandBuffer.makeComputeCommandEncoder() else { return }
        let outW = outputTexture.width
        let outH = outputTexture.height
        var uniforms = makeUniforms(
            width: outW,
            height: outH,
            sampleIdx: sampleIndex,
            splitCompare: splitCompare,
            temporal1SPP: temporal1SPP,
            jitter: jitter
        )
        var srgbFlag: UInt32 = encodeSRGB ? 1 : 0
        var hudExt = makeHudExtUniforms()
        var mm = minimapBuffer
        var musicBanner = makeMusicBannerUniforms()
        var evHud = makeEventHudUniforms()
        enc.setComputePipelineState(hudOverlayPSO)
        enc.setTexture(inputTexture, index: 0)
        enc.setTexture(outputTexture, index: 1)
        enc.setTexture(eventManager.worldMapTexture ?? fallbackWorldMapTex, index: 2)
        enc.setSamplerState(linearSampler, index: 0)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        enc.setBytes(&srgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
        enc.setBytes(&hudExt, length: RTHudExtUniforms.byteSize, index: 2)
        enc.setBytes(&mm, length: RTMinimapBuffer.byteSize, index: 3)
        enc.setBytes(&musicBanner, length: RTMusicBannerUniforms.byteSize, index: 4)
        enc.setBytes(&evHud, length: RTEventHudUniforms.byteSize, index: 5)
        dispatch2D(encoder: enc, pso: hudOverlayPSO, width: outW, height: outH)
        enc.endEncoding()
    }

    @discardableResult
    func encodeFrameInterpolator(
        commandBuffer: MTLCommandBuffer,
        currTexture: MTLTexture,
        prevTexture: MTLTexture,
        outputTexture: MTLTexture,
        dt: Float,
        jitter: SIMD2<Float>,
        resetHistory: Bool
    ) -> Bool {
        guard frameGenActive,
              let gDep = gDepthTex,
              let gMot = gMotionTex else { return false }
        if #available(macOS 26.0, *), let fi = frameInterpolator as? MTLFXFrameInterpolator {
            let outW = outputTexture.width
            let outH = outputTexture.height
            let aspect = Float(outW) / Float(max(outH, 1))
            fi.colorTexture = currTexture
            fi.prevColorTexture = prevTexture
            fi.depthTexture = gDep
            fi.motionTexture = gMot
            fi.outputTexture = outputTexture
            fi.motionVectorScaleX = Float(outW) / Float(max(rtWidth, 1))
            fi.motionVectorScaleY = Float(outH) / Float(max(rtHeight, 1))
            fi.jitterOffsetX = -jitter.x
            fi.jitterOffsetY = -jitter.y
            fi.deltaTime = max(0.001, dt)
            fi.nearPlane = 0.20
            fi.farPlane = 800.0
            fi.fieldOfView = camera.fovDeg
            fi.aspectRatio = aspect
            fi.isDepthReversed = true
            if #available(macOS 27.0, *) {
                fi.worldToViewMatrix = camera.viewMatrix()
                fi.viewToClipMatrix = camera.projMatrixReverseZ(aspect: aspect)
            }
            fi.shouldResetHistory = resetHistory
            fi.encode(commandBuffer: commandBuffer)
            return true
        }
        return false
    }

    func encodePostProcess(
        commandBuffer: MTLCommandBuffer,
        outputTexture: MTLTexture,
        encodeSRGB: Bool,
        splitCompare: Bool,
        temporal1SPP: Bool,
        jitter: SIMD2<Float>,
        runHudOverlay: Bool = true
    ) {
        let outW = outputTexture.width
        let outH = outputTexture.height
        guard let diffAcc = accumDiffTex,
              let specAcc = accumSpecTex,
              let gAlb = gAlbedoTex,
              let gNR = gNormalRoughTex,
              let gDep = gDepthTex,
              let gEm = gEmissiveTex,
              let gCol = gColorTex,
              let gMot = gMotionTex,
              let gSpecAlb = gSpecAlbedoTex,
              let gRough = gRoughnessTex,
              let gReact = gReactiveTex,
              let dPing = denoiseDiffPing,
              let dPong = denoiseDiffPong,
              let sPing = denoiseSpecPing,
              let sPong = denoiseSpecPong,
              let hdrRes = hdrResolvedTex,
              let bloomA = bloomTexA,
              let bloomB = bloomTexB,
              let ldrScratch = ldrColorTexA else { return }

        var uniforms = makeUniforms(
            width: outW,
            height: outH,
            sampleIdx: sampleIndex,
            splitCompare: splitCompare,
            temporal1SPP: temporal1SPP,
            jitter: jitter
        )

        var usedMetalFX = false
        if temporal1SPP && metalFXActive && !splitCompare {
            if #available(macOS 26.0, *), let scaler = temporalScaler as? MTLFXTemporalDenoisedScaler {
                let aspect = Float(outW) / Float(max(outH, 1))
                scaler.colorTexture = gCol
                scaler.depthTexture = gDep
                scaler.motionTexture = gMot
                scaler.diffuseAlbedoTexture = gAlb
                scaler.specularAlbedoTexture = gSpecAlb
                scaler.normalTexture = gNR
                scaler.roughnessTexture = gRough
                scaler.reactiveMaskTexture = gReact
                scaler.outputTexture = hdrRes
                scaler.jitterOffsetX = -jitter.x
                scaler.jitterOffsetY = -jitter.y
                scaler.motionVectorScaleX = 1.0
                scaler.motionVectorScaleY = 1.0
                scaler.isDepthReversed = true
                scaler.worldToViewMatrix = camera.viewMatrix()
                scaler.viewToClipMatrix = camera.projMatrixReverseZ(aspect: aspect)
                scaler.shouldResetHistory = historyResetPending
                scaler.encode(commandBuffer: commandBuffer)
                historyResetPending = false
                usedMetalFX = true
            }
        }

        guard let enc = commandBuffer.makeComputeCommandEncoder() else { return }

        if !usedMetalFX {
            var srcDiffBase: MTLTexture = diffAcc
            var srcSpecBase: MTLTexture = specAcc

            // SVGF temporal reprojection when running real-time 1-SPP without MetalFX
            if temporal1SPP && denoiseMode == .svgf,
               let hDiff = histDiffTex, let hSpec = histSpecTex,
               let hNR = histNormalRoughTex, let hDep = histDepthTex {
                var resetFlag: UInt32 = historyResetPending ? 1 : 0
                enc.setComputePipelineState(svgfTemporalPSO)
                enc.setTexture(diffAcc, index: 0)
                enc.setTexture(specAcc, index: 1)
                enc.setTexture(hDiff, index: 2)
                enc.setTexture(hSpec, index: 3)
                enc.setTexture(gMot, index: 4)
                enc.setTexture(gNR, index: 5)
                enc.setTexture(gDep, index: 6)
                enc.setTexture(hNR, index: 7)
                enc.setTexture(hDep, index: 8)
                enc.setTexture(dPong, index: 9)
                enc.setTexture(sPong, index: 10)
                enc.setBytes(&resetFlag, length: MemoryLayout<UInt32>.stride, index: 0)
                dispatch2D(encoder: enc, pso: svgfTemporalPSO, width: rtWidth, height: rtHeight)
                srcDiffBase = dPong
                srcSpecBase = sPong
                historyResetPending = false
            }

            let numDenoisePasses = max(0, min(5, Int(uniforms.gradeParams.w.rounded())))
            var finalDiff: MTLTexture = srcDiffBase
            var finalSpec: MTLTexture = srcSpecBase

            if numDenoisePasses > 0 {
                let steps: [Int32] = [1, 2, 4, 8, 16]
                for passIdx in 0..<numDenoisePasses {
                    var stepVal = steps[passIdx]
                    let srcD: MTLTexture = (passIdx == 0) ? srcDiffBase : ((passIdx % 2 == 1) ? dPing : dPong)
                    let srcS: MTLTexture = (passIdx == 0) ? srcSpecBase : ((passIdx % 2 == 1) ? sPing : sPong)
                    let dstD: MTLTexture = (passIdx % 2 == 0) ? dPing : dPong
                    let dstS: MTLTexture = (passIdx % 2 == 0) ? sPing : sPong

                    enc.setComputePipelineState(denoiseAtrousPSO)
                    enc.setTexture(srcD, index: 0)
                    enc.setTexture(srcS, index: 1)
                    enc.setTexture(dstD, index: 2)
                    enc.setTexture(dstS, index: 3)
                    enc.setTexture(gNR, index: 4)
                    enc.setTexture(gDep, index: 5)
                    enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
                    enc.setBytes(&stepVal, length: MemoryLayout<Int32>.stride, index: 1)
                    dispatch2D(encoder: enc, pso: denoiseAtrousPSO, width: rtWidth, height: rtHeight)
                    finalDiff = dstD
                    finalSpec = dstS
                }
            }

            enc.setComputePipelineState(remodulatePSO)
            enc.setTexture(finalDiff, index: 0)
            enc.setTexture(finalSpec, index: 1)
            enc.setTexture(gAlb, index: 2)
            enc.setTexture(gEm, index: 3)
            enc.setTexture(hdrRes, index: 4)
            dispatch2D(encoder: enc, pso: remodulatePSO, width: outW, height: outH)
        }

        // Bright-pass bloom
        enc.setComputePipelineState(bloomThresholdPSO)
        enc.setTexture(hdrRes, index: 0)
        enc.setTexture(bloomA, index: 1)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        dispatch2D(encoder: enc, pso: bloomThresholdPSO, width: bloomA.width, height: bloomA.height)

        for pass in 0..<2 {
            let stepSize = Int32(pass == 0 ? 1 : 2)
            var dirH = SIMD2<Int32>(stepSize, 0)
            enc.setComputePipelineState(bloomBlurPSO)
            enc.setTexture(bloomA, index: 0)
            enc.setTexture(bloomB, index: 1)
            enc.setBytes(&dirH, length: MemoryLayout<SIMD2<Int32>>.stride, index: 0)
            dispatch2D(encoder: enc, pso: bloomBlurPSO, width: bloomA.width, height: bloomA.height)

            var dirV = SIMD2<Int32>(0, stepSize)
            enc.setTexture(bloomB, index: 0)
            enc.setTexture(bloomA, index: 1)
            enc.setBytes(&dirV, length: MemoryLayout<SIMD2<Int32>>.stride, index: 0)
            dispatch2D(encoder: enc, pso: bloomBlurPSO, width: bloomA.width, height: bloomA.height)
        }

        // Composite post-ACES perceptual sRGB (pre-HUD)
        let compositeDest: MTLTexture = runHudOverlay ? ldrScratch : outputTexture
        var compSrgbFlag: UInt32 = runHudOverlay ? 1 : (encodeSRGB ? 1 : 0)
        enc.setComputePipelineState(compositePSO)
        enc.setTexture(hdrRes, index: 0)
        enc.setTexture(bloomA, index: 1)
        enc.setTexture(compositeDest, index: 2)
        enc.setSamplerState(linearSampler, index: 0)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        enc.setBytes(&compSrgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
        dispatch2D(encoder: enc, pso: compositePSO, width: outW, height: outH)

        if runHudOverlay {
            var hudSrgbFlag: UInt32 = encodeSRGB ? 1 : 0
            var hudExt = makeHudExtUniforms()
            var mm = minimapBuffer
            var musicBanner = makeMusicBannerUniforms()
            var evHud = makeEventHudUniforms()
            enc.setComputePipelineState(hudOverlayPSO)
            enc.setTexture(ldrScratch, index: 0)
            enc.setTexture(outputTexture, index: 1)
            enc.setTexture(eventManager.worldMapTexture ?? fallbackWorldMapTex, index: 2)
            enc.setSamplerState(linearSampler, index: 0)
            enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
            enc.setBytes(&hudSrgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
            enc.setBytes(&hudExt, length: RTHudExtUniforms.byteSize, index: 2)
            enc.setBytes(&mm, length: RTMinimapBuffer.byteSize, index: 3)
            enc.setBytes(&musicBanner, length: RTMusicBannerUniforms.byteSize, index: 4)
            enc.setBytes(&evHud, length: RTEventHudUniforms.byteSize, index: 5)
            dispatch2D(encoder: enc, pso: hudOverlayPSO, width: outW, height: outH)
        }
        enc.endEncoding()

        // Copy SVGF temporal history if SVGF is active
        if temporal1SPP && !usedMetalFX && denoiseMode == .svgf,
           let hDiff = histDiffTex, let hSpec = histSpecTex,
           let hNR = histNormalRoughTex, let hDep = histDepthTex,
           let blit = commandBuffer.makeBlitCommandEncoder() {
            let sz = MTLSize(width: rtWidth, height: rtHeight, depth: 1)
            let zero = MTLOrigin(x: 0, y: 0, z: 0)
            blit.copy(from: dPong, sourceSlice: 0, sourceLevel: 0, sourceOrigin: zero, sourceSize: sz, to: hDiff, destinationSlice: 0, destinationLevel: 0, destinationOrigin: zero)
            blit.copy(from: sPong, sourceSlice: 0, sourceLevel: 0, sourceOrigin: zero, sourceSize: sz, to: hSpec, destinationSlice: 0, destinationLevel: 0, destinationOrigin: zero)
            blit.copy(from: gNR, sourceSlice: 0, sourceLevel: 0, sourceOrigin: zero, sourceSize: sz, to: hNR, destinationSlice: 0, destinationLevel: 0, destinationOrigin: zero)
            blit.copy(from: gDep, sourceSlice: 0, sourceLevel: 0, sourceOrigin: zero, sourceSize: sz, to: hDep, destinationSlice: 0, destinationLevel: 0, destinationOrigin: zero)
            blit.endEncoding()
        }

        let aspect = Float(outW) / Float(max(outH, 1))
        prevViewProj = camera.viewProjMatrix(aspect: aspect)
        hasValidPrevFrame = true
    }

    func renderHeadless(
        width: Int,
        height: Int,
        spp: Int,
        splitCompare: Bool,
        outURL: URL,
        forceTemporalDenoise: Bool = false
    ) throws {
        let useTemporalMFX = forceTemporalDenoise && !splitCompare && (denoiseMode == .metalfx) && supportsMetalFXDenoiser
        ensureRenderTargets(width: width, height: height, useMetalFX: useTemporalMFX)
        resetAccumulation()

        let outDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
        outDesc.usage = [.shaderRead, .shaderWrite]
        outDesc.storageMode = .shared
        guard let outTex = device.makeTexture(descriptor: outDesc) else {
            throw NSError(domain: "RTRenderer", code: 111, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate output texture"])
        }

        if forceTemporalDenoise && !splitCompare {
            let numFrames = max(4, min(spp, 16))
            for f in 0..<numFrames {
                guard let cb = commandQueue.makeCommandBuffer() else { continue }
                let jx = Self.halton(UInt32((f % 16) + 1), base: 2) - 0.5
                let jy = Self.halton(UInt32((f % 16) + 1), base: 3) - 0.5
                let jitter = SIMD2<Float>(jx, jy)
                encodeRayTraceSample(
                    commandBuffer: cb,
                    width: rtWidth,
                    height: rtHeight,
                    sampleIdx: UInt32(f),
                    splitCompare: false,
                    temporal1SPP: true,
                    jitter: jitter
                )
                encodePostProcess(
                    commandBuffer: cb,
                    outputTexture: outTex,
                    encodeSRGB: true,
                    splitCompare: false,
                    temporal1SPP: true,
                    jitter: jitter
                )
                cb.commit()
                cb.waitUntilCompleted()
                if let err = cb.error { throw err }
            }
        } else {
            let totalSPP = max(1, spp)
            let batchSize = 4
            var s = 0
            while s < totalSPP {
                guard let cmdBuf = commandQueue.makeCommandBuffer() else {
                    throw NSError(domain: "RTRenderer", code: 110, userInfo: [NSLocalizedDescriptionKey: "Failed to create command buffer"])
                }
                let end = min(totalSPP, s + batchSize)
                while s < end {
                    encodeRayTraceSample(
                        commandBuffer: cmdBuf,
                        width: rtWidth,
                        height: rtHeight,
                        sampleIdx: UInt32(s),
                        splitCompare: splitCompare,
                        temporal1SPP: false,
                        jitter: .zero
                    )
                    s += 1
                }
                cmdBuf.commit()
                cmdBuf.waitUntilCompleted()
                if let err = cmdBuf.error { throw err }
            }
            sampleIndex = UInt32(totalSPP)

            guard let postCmd = commandQueue.makeCommandBuffer() else {
                throw NSError(domain: "RTRenderer", code: 111, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate post command buffer"])
            }
            encodePostProcess(
                commandBuffer: postCmd,
                outputTexture: outTex,
                encodeSRGB: true,
                splitCompare: splitCompare,
                temporal1SPP: false,
                jitter: .zero
            )
            postCmd.commit()
            postCmd.waitUntilCompleted()
            if let err = postCmd.error { throw err }
        }

        try RTSceneBuilder.writeBGRA8TextureToPNG(texture: outTex, outURL: outURL)
    }

    // Drive forward N real-time frames with MetalFX/SVGF (+ optional 2x MTLFXFrameInterpolator), measuring GPU ms/frame & FPS and saving final frame
    func runDriveBenchmark(frames: Int, width: Int, height: Int, outURL: URL) throws -> (avgMs: Double, fps: Double) {
        let totalFrames = max(1, frames)
        let useMFX = (denoiseMode == .metalfx) && supportsMetalFXDenoiser
        let useFG = frameGenEnabled && useMFX && supportsFrameInterpolator
        ensureRenderTargets(width: width, height: height, useMetalFX: useMFX, useFrameGen: useFG)

        let outDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
        outDesc.usage = [.shaderRead, .shaderWrite]
        outDesc.storageMode = .shared
        guard let outTex = device.makeTexture(descriptor: outDesc) else {
            throw NSError(domain: "RTRenderer", code: 112, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate drive-bench output texture"])
        }

        if eventManager.isEventActiveOrCountdown {
            benchmarkAutoSteerToEvent = true
        }
        throttleHeld = !eventManager.isWorldMapOpen
        let initialSpeed = eventManager.isWorldMapOpen ? 0.0 : max(speedKmh, benchmarkNosForced ? 165.0 : 115.0)
        physicsEngine.configurePose(
            speedKmh: initialSpeed,
            steerDeg: 0.0,
            position: carWorldPosition,
            headingRad: carHeadingRad
        )
        speedKmh = initialSpeed
        if let pHeat = benchmarkPursuitHeat {
            triggerInstantPursuit(setHeat: pHeat, showcaseFraming: true)
        }
        if cameraMode == .hero {
            applyCameraMode(.chase)
        }
        historyResetPending = true
        hasValidPrevFrame = false
        frameGenHistoryValid = false
        ldrWriteToA = true

        var totalWallSec = 0.0
        var timedFrames = 0
        // Use even warmup (4) when Frame-Gen is active so timedFrames covers exact (real+FI, real) pairs
        let warmup = useFG ? min(4, max(0, totalFrames - 2)) : min(3, max(0, totalFrames - 1))
        var realFrameIdx: UInt32 = 0
        var lastRealLdr: MTLTexture? = nil

        for f in 0..<totalFrames {
            let t0 = CFAbsoluteTimeGetCurrent()
            let dt: Float = 1.0 / 60.0

            if frameGenActive,
               let ldrA = ldrColorTexA, let ldrB = ldrColorTexB, let ldrInterp = ldrInterpTex {
                // Monotonic 2x Frame-Gen schedule:
                //   f == 0: Render R0 -> present R0
                //   f odd : Render R_k + encode FI(R_{k-1}, R_k) -> present FI (half-frame t = k - 0.5)
                //   f even: Present R_k directly via post-FI hudOverlayKernel (t = k)
                if f == 0 || (f % 2 == 1) {
                    _ = stepDrivePhysics(dt: dt)
                    simTimeSec += dt * physicsEngine.state.effectiveTimeScale
                    if timeCycleEnabled {
                        timeOfDayHours = (timeOfDayHours + dt * 0.25).truncatingRemainder(dividingBy: 24.0)
                    }
                    guard let cb = commandQueue.makeCommandBuffer() else { continue }
                    try rebuildTLAS(commandBuffer: cb)
                    if world != nil {
                        encodePhysicsProbe(commandBuffer: cb, predictedStep: predictedNextStep(dt: dt))
                        encodeAIRoadProbe(commandBuffer: cb)
                    }

                    let jIdx = UInt32((realFrameIdx % 16) + 1)
                    let jitter = SIMD2<Float>(Self.halton(jIdx, base: 2) - 0.5, Self.halton(jIdx, base: 3) - 0.5)
                    let currLdr = ldrWriteToA ? ldrA : ldrB
                    let prevLdr = ldrWriteToA ? ldrB : ldrA

                    encodeRayTraceSample(
                        commandBuffer: cb,
                        width: rtWidth,
                        height: rtHeight,
                        sampleIdx: realFrameIdx,
                        splitCompare: false,
                        temporal1SPP: true,
                        jitter: jitter
                    )
                    encodePostProcess(
                        commandBuffer: cb,
                        outputTexture: currLdr,
                        encodeSRGB: true,
                        splitCompare: false,
                        temporal1SPP: true,
                        jitter: jitter,
                        runHudOverlay: false
                    )
                    if frameGenHistoryValid {
                        _ = encodeFrameInterpolator(
                            commandBuffer: cb,
                            currTexture: currLdr,
                            prevTexture: prevLdr,
                            outputTexture: ldrInterp,
                            dt: dt,
                            jitter: jitter,
                            resetHistory: (realFrameIdx <= 1)
                        )
                        encodeHudOverlay(
                            commandBuffer: cb,
                            inputTexture: ldrInterp,
                            outputTexture: outTex,
                            encodeSRGB: true,
                            temporal1SPP: true,
                            jitter: jitter
                        )
                    } else {
                        encodeHudOverlay(
                            commandBuffer: cb,
                            inputTexture: currLdr,
                            outputTexture: outTex,
                            encodeSRGB: true,
                            temporal1SPP: true,
                            jitter: jitter
                        )
                        frameGenHistoryValid = true
                    }
                    cb.commit()
                    cb.waitUntilCompleted()
                    if let err = cb.error { throw err }

                    lastRealLdr = currLdr
                    ldrWriteToA.toggle()
                    realFrameIdx &+= 1
                } else if let readyLdr = lastRealLdr, let cb = commandQueue.makeCommandBuffer() {
                    encodeHudOverlay(
                        commandBuffer: cb,
                        inputTexture: readyLdr,
                        outputTexture: outTex,
                        encodeSRGB: true,
                        temporal1SPP: true,
                        jitter: .zero
                    )
                    cb.commit()
                    cb.waitUntilCompleted()
                    if let err = cb.error { throw err }
                }
            } else {
                _ = stepDrivePhysics(dt: dt)
                simTimeSec += dt * physicsEngine.state.effectiveTimeScale
                if timeCycleEnabled {
                    timeOfDayHours = (timeOfDayHours + dt * 0.25).truncatingRemainder(dividingBy: 24.0)
                }

                guard let cb = commandQueue.makeCommandBuffer() else { continue }
                try rebuildTLAS(commandBuffer: cb)
                if world != nil {
                    encodePhysicsProbe(commandBuffer: cb, predictedStep: predictedNextStep(dt: dt))
                    encodeAIRoadProbe(commandBuffer: cb)
                }

                let jIdx = UInt32((f % 16) + 1)
                let jitter = SIMD2<Float>(Self.halton(jIdx, base: 2) - 0.5, Self.halton(jIdx, base: 3) - 0.5)
                encodeRayTraceSample(
                    commandBuffer: cb,
                    width: rtWidth,
                    height: rtHeight,
                    sampleIdx: UInt32(f),
                    splitCompare: false,
                    temporal1SPP: true,
                    jitter: jitter
                )
                encodePostProcess(
                    commandBuffer: cb,
                    outputTexture: outTex,
                    encodeSRGB: true,
                    splitCompare: false,
                    temporal1SPP: true,
                    jitter: jitter,
                    runHudOverlay: true
                )
                cb.commit()
                cb.waitUntilCompleted()
                if let err = cb.error { throw err }
            }

            let dtFrame = CFAbsoluteTimeGetCurrent() - t0
            if f >= warmup {
                totalWallSec += dtFrame
                timedFrames += 1
            }
            // Use harmonic average frame time across BOTH real+FI and fast present frames so
            // the HUD honestly displays true displayed FPS (e.g. 118-132 FG, not half-rate 57-68).
            if timedFrames >= 2 && totalWallSec > 1e-4 {
                smoothedFps = Float(Double(timedFrames) / totalWallSec)
            } else if dtFrame > 0.00005 {
                smoothedFrameSec = (f == 0) ? dtFrame : (smoothedFrameSec * 0.80 + dtFrame * 0.20)
                smoothedFps = Float(1.0 / max(smoothedFrameSec, 1e-4))
            }
            if world != nil && (f % 2 == 0) {
                updateMinimapBuffer()
            }
        }
        throttleHeld = false

        try RTSceneBuilder.writeBGRA8TextureToPNG(texture: outTex, outURL: outURL)
        let avgMs = (timedFrames > 0) ? (totalWallSec / Double(timedFrames)) * 1000.0 : 0.0
        let fps = avgMs > 0.001 ? 1000.0 / avgMs : 0.0
        return (avgMs, fps)
    }

    func stepDrivePhysics(dt: Float) -> Bool {
        if eventManager.isWorldMapOpen {
            return false
        }
        var steerInput: Float = (steerLeftHeld ? 1.0 : 0.0) + (steerRightHeld ? -1.0 : 0.0)
        if benchmarkAutoSteerToEvent && eventManager.isEventActiveOrCountdown && steerInput == 0.0 {
            // Follow event road route with continuous segment interpolation ~26m ahead so curves stay cleanly centered!
            var aimPos: SIMD3<Float>? = nil
            if let ev = eventManager.activeEvent, ev.routeWaypoints.count >= 2 {
                var bestSeg = 0
                var bestT: Float = 0.0
                var bestD2: Float = .greatestFiniteMagnitude
                let pXZ = SIMD2<Float>(carWorldPosition.x, carWorldPosition.z)
                for wi in 0..<(ev.routeWaypoints.count - 1) {
                    let a = SIMD2<Float>(ev.routeWaypoints[wi].pos.x, ev.routeWaypoints[wi].pos.z)
                    let b = SIMD2<Float>(ev.routeWaypoints[wi + 1].pos.x, ev.routeWaypoints[wi + 1].pos.z)
                    let ab = b - a
                    let lenSq = max(1e-4, simd_dot(ab, ab))
                    let t = max(0.0, min(1.0, simd_dot(pXZ - a, ab) / lenSq))
                    let proj = a + ab * t
                    let d2 = simd_length_squared(pXZ - proj)
                    if d2 < bestD2 { bestD2 = d2; bestSeg = wi; bestT = t }
                }
                let segDist = max(0.01, ev.routeWaypoints[bestSeg + 1].cumDistM - ev.routeWaypoints[bestSeg].cumDistM)
                let curCum = ev.routeWaypoints[bestSeg].cumDistM + segDist * bestT
                let targetCum = curCum + 26.0
                var lookSeg = bestSeg
                while lookSeg + 1 < ev.routeWaypoints.count - 1 && ev.routeWaypoints[lookSeg + 1].cumDistM < targetCum {
                    lookSeg += 1
                }
                let l0 = ev.routeWaypoints[lookSeg]
                let l1 = ev.routeWaypoints[min(lookSeg + 1, ev.routeWaypoints.count - 1)]
                let span = max(0.01, l1.cumDistM - l0.cumDistM)
                let frac = max(0.0, min(1.0, (targetCum - l0.cumDistM) / span))
                aimPos = simd_mix(l0.pos, l1.pos, SIMD3<Float>(repeating: frac))
            } else {
                let fx = eventManager.currentWorldFX(simTime: simTimeSec)
                if fx.fxParams.x > 0.5 {
                    aimPos = SIMD3<Float>(fx.gatePosRadius.x, fx.gatePosRadius.y, fx.gatePosRadius.z)
                }
            }
            if let tgt = aimPos {
                let targetXZ = SIMD2<Float>(tgt.x - carWorldPosition.x, tgt.z - carWorldPosition.z)
                if simd_length(targetXZ) > 3.0 {
                    let targetYaw = atan2(targetXZ.x, targetXZ.y)
                    var dYaw = targetYaw - carHeadingRad
                    while dYaw > .pi { dYaw -= 2.0 * .pi }
                    while dYaw < -.pi { dYaw += 2.0 * .pi }
                    // RTPhysics: positive steerInput yields positive yawRate (increases headingRad)
                    steerInput = max(-0.75, min(0.75, dYaw * 1.45))
                }
            }
        }
        let input = RTDriveInput(
            throttle: throttleHeld ? 1.0 : 0.0,
            brake: brakeHeld ? 1.0 : 0.0,
            steer: steerInput,
            handbrake: handbrakeHeld,
            nos: nosHeld,
            speedbreaker: speedbreakerInputHeld
        )

        var probeSample = RTRoadProbeSample()
        probeSample.hasWorld = (world != nil)
        if world != nil {
            let gpuProbe = readPhysicsProbeOutput()
            probeSample.wheelHitY = gpuProbe.wheelHitY
            probeSample.wallDist = gpuProbe.wallDist
            probeSample.wallNormalFront = gpuProbe.wallNormalFront
            probeSample.wallNormalSide = gpuProbe.wallNormalSide
            rawSkyOcclusion = max(0.0, min(1.0, gpuProbe.skyProbe.x))
            skyCeilingDist = gpuProbe.skyProbe.y
            let tau: Float = (rawSkyOcclusion > smoothedSkyOcclusion) ? 0.14 : 0.45
            let alpha = 1.0 - exp(-max(0.001, dt) / tau)
            smoothedSkyOcclusion = smoothedSkyOcclusion + (rawSkyOcclusion - smoothedSkyOcclusion) * alpha
        } else {
            rawSkyOcclusion = 0.0
            skyCeilingDist = 999.0
            smoothedSkyOcclusion = 0.0
        }
        if musicBannerTimer > 0.0 {
            musicBannerTimer = max(0.0, musicBannerTimer - dt)
        }

        let prevPos = carWorldPosition
        let prevHead = carHeadingRad
        let prevSteer = steerDeg
        let prevPitch = carPitchRad
        let prevRoll = carRollRad

        _ = physicsEngine.step(realDt: dt, input: input, probe: probeSample)
        let simDt = dt * physicsEngine.state.effectiveTimeScale

        // Step live AI traffic + Heat 1-5 police pursuit along Rockport's 4,385-node road network
        if aiEnabled && world != nil && !gameWorld.roadGraph.isEmpty {
            gameWorld.step(
                dt: simDt,
                playerPos: physicsEngine.state.worldPosition,
                playerVel: physicsEngine.state.velocityWorld,
                playerYaw: physicsEngine.state.headingRad
            )
            ingestAIRoadProbeOutput(dt: simDt)
            if gameWorld.playerCollisionIntensity > 0.01 {
                physicsEngine.applyExternalImpulse(
                    gameWorld.playerBumpImpulse,
                    impactIntensity: gameWorld.playerCollisionIntensity
                )
            }
        }
        if world != nil && eventManager.isEventActiveOrCountdown {
            eventManager.step(
                dt: simDt,
                playerPos: physicsEngine.state.worldPosition,
                playerVel: physicsEngine.state.velocityWorld,
                playerYaw: physicsEngine.state.headingRad
            )
        }

        let st = physicsEngine.state
        carWorldPosition = st.worldPosition
        carHeadingRad = st.headingRad
        carYawRad = (world != nil) ? 0.0 : st.visualYawRad
        carPitchRad = st.pitchRad
        carRollRad = st.rollRad
        terrainPitchRad = st.terrainPitchRad
        terrainRollRad = st.terrainRollRad
        steerDeg = st.steerDeg
        wheelSpinRad = st.wheelSpinRad
        roadScrollZ = st.roadScrollZ
        speedKmh = st.speedKmh
        nosActiveStrength = st.nosFlameIntensity
        speedbreakerStrength = st.speedbreakerBlend

        // Authentic NFSMW chase camera with drift slip lag, NOS FOV kick & Speedbreaker zoom
        if world != nil {
            if cameraMode == .chase || chaseCamEnabled {
                let cam = physicsEngine.chaseCameraTarget()
                camera.target = cam.target
                camera.azimuthDeg = cam.azimuthDeg
                camera.distance = cam.distance
                camera.fovDeg = cam.fovDeg
            } else {
                camera.target = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.52, carWorldPosition.z)
            }
        } else if chaseCamEnabled {
            let cam = physicsEngine.chaseCameraTarget()
            camera.fovDeg = cam.fovDeg
            camera.distance = max(4.35, cam.distance - 0.10)
        }

        // Real-time Ginsu PSOLA engine + SFX + 3D Doppler siren + Tunnel Reverb audio (silent no-op in headless)
        audioEngine.setTunnelOcclusion(smoothedSkyOcclusion)
        if !musicDisabled && !explicitMusicTrackLocked {
            let pursuitActive = aiEnabled && world != nil && gameWorld.heatLevel > 0
            if pursuitActive != lastPursuitAudioMode {
                lastPursuitAudioMode = pursuitActive
                audioEngine.setPursuitMusicMode(pursuitActive)
                syncMusicBannerFromAudio(triggerSlide: true)
            }
        }
        audioEngine.update(
            telemetry: st,
            rainIntensity: effectiveRainAmount(),
            sirenGain: gameWorld.sirenAudio.gain,
            sirenPan: gameWorld.sirenAudio.pan,
            sirenDoppler: gameWorld.sirenAudio.doppler,
            sirenMode: gameWorld.sirenAudio.mode,
            radarPing: gameWorld.sirenAudio.radarPing
        )

        let playerMoved = simd_distance(carWorldPosition, prevPos) > 1e-4
            || abs(carHeadingRad - prevHead) > 1e-4
            || abs(steerDeg - prevSteer) > 0.03
            || abs(carPitchRad - prevPitch) > 0.0003
            || abs(carRollRad - prevRoll) > 0.0005
            || st.speedKmh > 0.2
            || st.nosFlameIntensity > 0.01
            || st.speedbreakerBlend > 0.01
        let aiActive = aiEnabled && world != nil && (gameWorld.activeTrafficCount > 0 || gameWorld.activeCopCount > 0)
        return playerMoved || aiActive
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {
        historyResetPending = true
        resetAccumulation()
    }

    func draw(in view: MTKView) {
        let now = CFAbsoluteTimeGetCurrent()
        let rawDt = max(0.0005, min(0.1, now - lastFrameTime))
        let dt = Float(min(0.05, max(0.001, rawDt)))
        lastFrameTime = now
        smoothedFrameSec = smoothedFrameSec * 0.88 + rawDt * 0.12
        smoothedFps = Float(1.0 / max(smoothedFrameSec, 1e-4))
        if world != nil && (frameCounter % 2 == 0) {
            updateMinimapBuffer()
        }

        guard let drawable = view.currentDrawable,
              let cmdBuf = commandQueue.makeCommandBuffer() else { return }
        let tex = drawable.texture
        let useTemporalMFX = (denoiseMode == .metalfx) && supportsMetalFXDenoiser
        let useFG = frameGenEnabled && useTemporalMFX && supportsFrameInterpolator
        ensureRenderTargets(width: tex.width, height: tex.height, useMetalFX: useTemporalMFX, useFrameGen: useFG)

        let encodeSRGB = (view.colorPixelFormat == .bgra8Unorm)

        // Interactive 120 Hz 2x Frame-Gen pacing:
        //   Phase 0: Step physics, RT + MetalFX TDS -> currLdr, interpolate FI(prevLdr, currLdr) -> HUD -> present
        //   Phase 1: Present currLdr via post-FI HUD overlay (~0.2ms, zero RT work!)
        if frameGenActive && denoiseMode == .metalfx,
           let ldrA = ldrColorTexA, let ldrB = ldrColorTexB, let ldrInterp = ldrInterpTex {
            if frameGenPresentPhase == 1 && frameGenHistoryValid {
                let readyLdr = ldrWriteToA ? ldrB : ldrA
                encodeHudOverlay(
                    commandBuffer: cmdBuf,
                    inputTexture: readyLdr,
                    outputTexture: tex,
                    encodeSRGB: encodeSRGB,
                    temporal1SPP: true,
                    jitter: .zero
                )
                frameGenPresentPhase = 0
                cmdBuf.present(drawable)
                cmdBuf.commit()
                return
            }

            let realDt = Float(min(0.05, max(0.002, now - lastRealFrameTime)))
            lastRealFrameTime = now
            lastRealFrameDelta = realDt
            let moved = stepDrivePhysics(dt: realDt)
            simTimeSec += realDt * physicsEngine.state.effectiveTimeScale
            if timeCycleEnabled {
                timeOfDayHours = (timeOfDayHours + realDt * 0.25).truncatingRemainder(dividingBy: 24.0)
            }
            if moved {
                try? rebuildTLAS(commandBuffer: cmdBuf)
                if world != nil {
                    encodePhysicsProbe(commandBuffer: cmdBuf, predictedStep: predictedNextStep(dt: realDt))
                    encodeAIRoadProbe(commandBuffer: cmdBuf)
                }
            }

            let jIdx = UInt32((frameCounter % 16) + 1)
            let jitter = SIMD2<Float>(Self.halton(jIdx, base: 2) - 0.5, Self.halton(jIdx, base: 3) - 0.5)
            let currLdr = ldrWriteToA ? ldrA : ldrB
            let prevLdr = ldrWriteToA ? ldrB : ldrA

            encodeRayTraceSample(
                commandBuffer: cmdBuf,
                width: rtWidth,
                height: rtHeight,
                sampleIdx: frameCounter,
                splitCompare: false,
                temporal1SPP: true,
                jitter: jitter
            )
            encodePostProcess(
                commandBuffer: cmdBuf,
                outputTexture: currLdr,
                encodeSRGB: true,
                splitCompare: false,
                temporal1SPP: true,
                jitter: jitter,
                runHudOverlay: false
            )
            if frameGenHistoryValid {
                _ = encodeFrameInterpolator(
                    commandBuffer: cmdBuf,
                    currTexture: currLdr,
                    prevTexture: prevLdr,
                    outputTexture: ldrInterp,
                    dt: realDt,
                    jitter: jitter,
                    resetHistory: (frameCounter <= 1)
                )
                encodeHudOverlay(
                    commandBuffer: cmdBuf,
                    inputTexture: ldrInterp,
                    outputTexture: tex,
                    encodeSRGB: encodeSRGB,
                    temporal1SPP: true,
                    jitter: jitter
                )
                frameGenPresentPhase = 1
            } else {
                encodeHudOverlay(
                    commandBuffer: cmdBuf,
                    inputTexture: currLdr,
                    outputTexture: tex,
                    encodeSRGB: encodeSRGB,
                    temporal1SPP: true,
                    jitter: jitter
                )
                frameGenHistoryValid = true
                frameGenPresentPhase = 0
            }
            ldrWriteToA.toggle()
            frameCounter &+= 1
            cmdBuf.present(drawable)
            cmdBuf.commit()
            return
        }

        let moved = stepDrivePhysics(dt: dt)
        simTimeSec += dt * physicsEngine.state.effectiveTimeScale
        if timeCycleEnabled {
            timeOfDayHours = (timeOfDayHours + dt * 0.25).truncatingRemainder(dividingBy: 24.0)
        }
        if moved {
            try? rebuildTLAS(commandBuffer: cmdBuf)
            if world != nil {
                encodePhysicsProbe(commandBuffer: cmdBuf, predictedStep: predictedNextStep(dt: dt))
                encodeAIRoadProbe(commandBuffer: cmdBuf)
            }
        }

        // Real-time temporal driving pipeline: NEVER wipe history on movement!
        let useTemporalMode = (denoiseMode != .off)
        let jIdx = UInt32((frameCounter % 16) + 1)
        let jitter = useTemporalMode
            ? SIMD2<Float>(Self.halton(jIdx, base: 2) - 0.5, Self.halton(jIdx, base: 3) - 0.5)
            : .zero

        if useTemporalMode {
            encodeRayTraceSample(
                commandBuffer: cmdBuf,
                width: rtWidth,
                height: rtHeight,
                sampleIdx: frameCounter,
                splitCompare: false,
                temporal1SPP: true,
                jitter: jitter
            )
            frameCounter &+= 1
        } else {
            if moved { resetAccumulation() }
            if sampleIndex < 64 {
                encodeRayTraceSample(
                    commandBuffer: cmdBuf,
                    width: rtWidth,
                    height: rtHeight,
                    sampleIdx: sampleIndex,
                    splitCompare: false,
                    temporal1SPP: false,
                    jitter: .zero
                )
                sampleIndex += 1
            }
        }

        encodePostProcess(
            commandBuffer: cmdBuf,
            outputTexture: tex,
            encodeSRGB: encodeSRGB,
            splitCompare: false,
            temporal1SPP: useTemporalMode,
            jitter: jitter,
            runHudOverlay: true
        )
        cmdBuf.present(drawable)
        cmdBuf.commit()
    }
}
