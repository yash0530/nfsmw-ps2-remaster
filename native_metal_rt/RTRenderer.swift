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
    var forceSyntheticMode: Bool = false

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

    // Reserved Hour 1-3 ABI buffers (Buffer Slots 11, 12, 13)
    private var worldLightsBuffer: MTLBuffer!
    private var worldLightGridBuffer: MTLBuffer!
    private var dynamicFXBuffer: MTLBuffer!

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
    var timeOfDayHours: Float = 17.6
    var rainIntensity: Float = 0.0
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

    init(device: MTLDevice, commandQueue: MTLCommandQueue, scene: RTScenePackage, world: RTWorldPackage? = nil, metalFileURL: URL?) throws {
        guard device.supportsRaytracing else {
            throw NSError(domain: "RTRenderer", code: 100, userInfo: [
                NSLocalizedDescriptionKey: "Metal hardware ray tracing is not supported on \(device.name)"
            ])
        }
        self.device = device
        self.commandQueue = commandQueue
        self.scene = scene
        self.world = world
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
              let wlBuf = device.makeBuffer(length: MemoryLayout<RTWorldLightGPU>.stride, options: .storageModeShared),
              let wlgBuf = device.makeBuffer(length: 256, options: .storageModeShared),
              let dfxBuf = device.makeBuffer(length: MemoryLayout<RTDynamicFXGPU>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTRenderer", code: 102, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate physics/ABI buffers"])
        }
        precondition(MemoryLayout<RTWorldLightGPU>.stride == 32, "RTWorldLightGPU stride must be 32 bytes")
        precondition(MemoryLayout<RTDynamicFXGPU>.stride == 176, "RTDynamicFXGPU stride must be 176 bytes")
        self.physicsInBuffer = pin
        self.physicsOutBuffer = pout
        self.worldLightsBuffer = wlBuf
        self.worldLightGridBuffer = wlgBuf
        self.dynamicFXBuffer = dfxBuf
        memset(wlBuf.contents(), 0, MemoryLayout<RTWorldLightGPU>.stride)
        memset(wlgBuf.contents(), 0, 256)
        var initFX = RTDynamicFXGPU.zeroed()
        memcpy(dfxBuf.contents(), &initFX, MemoryLayout<RTDynamicFXGPU>.stride)

        try buildPipelines(metalFileURL: metalFileURL)
        buildSamplers()

        if let w = world {
            self.carWorldPosition = w.spawnPos
            self.carHeadingRad = w.spawnYawRad
        }
        try buildAccelerationStructures()

        if world != nil {
            // Snap car tires cleanly onto the exact .rkpt asphalt height via GPU BVH probe
            snapCarToWorldRoad()
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
        if useChaseCam {
            applyCameraMode(.chase)
        }
        try? rebuildTLAS(commandBuffer: nil)
        resetAccumulation()
    }

    func switchCar(to profile: RTCarProfile) throws {
        let newScene = try RTSceneBuilder.buildScene(
            device: device,
            commandQueue: commandQueue,
            scenePath: nil,
            carKey: profile.rawValue,
            forceSynthetic: forceSyntheticMode,
            skipProceduralWorld: (world != nil)
        )
        self.scene = newScene
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
        }

        accelEnc.endEncoding()
        cmdBuf.commit()
        cmdBuf.waitUntilCompleted()
        if let err = cmdBuf.error { throw err }

        // Append deduplicated .rkpt world BLASes into unified TLAS BLAS array
        let worldBlasBase = blasList.count
        var validWorldInstances: [RkptInstance] = []
        if let w = world {
            blasList.append(contentsOf: w.blasList)
            validWorldInstances.reserveCapacity(w.instances.count)
            for inst in w.instances {
                let mIdx = Int(inst.meshIndex)
                if mIdx >= 0 && mIdx < w.meshToBlasIndex.count && w.meshToBlasIndex[mIdx] >= 0 {
                    validWorldInstances.append(inst)
                }
            }
        }

        instanceCount = scene.meshes.count + validWorldInstances.count
        prevCarBodyXform = currentCarBodyXform()
        prevCarModelMatrices = scene.meshes.map { dynamicModelMatrix(for: $0) }

        let descByteCount = max(1, instanceCount) * MemoryLayout<MTLAccelerationStructureInstanceDescriptor>.stride
        let metaByteCount = max(1, instanceCount) * MemoryLayout<RTInstanceMeta>.stride
        guard let iDescBuf = device.makeBuffer(length: descByteCount, options: .storageModeShared),
              let iMetaBuf = device.makeBuffer(length: metaByteCount, options: .storageModeShared) else {
            throw NSError(domain: "RTRenderer", code: 106, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate TLAS instance buffers"])
        }
        instanceDescBuffer = iDescBuf
        instanceMetaBuffer = iMetaBuf

        // Populate static .rkpt world TLAS descriptors & instance metadata ONCE
        if let w = world, !validWorldInstances.isEmpty {
            let descPtr = instanceDescBuffer.contents().bindMemory(to: MTLAccelerationStructureInstanceDescriptor.self, capacity: instanceCount)
            let metaPtr = instanceMetaBuffer.contents().bindMemory(to: RTInstanceMeta.self, capacity: instanceCount)
            let baseSlot = scene.meshes.count

            for (i, wInst) in validWorldInstances.enumerated() {
                let slot = baseSlot + i
                let mIdx = Int(wInst.meshIndex)
                let mesh = w.meshes[mIdx]
                let blasIdx = worldBlasBase + w.meshToBlasIndex[mIdx]

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
            }
        }

        let tlasDesc = MTLInstanceAccelerationStructureDescriptor()
        tlasDesc.instancedAccelerationStructures = blasList
        tlasDesc.instanceCount = instanceCount
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

    func rebuildTLAS(commandBuffer externalCmd: MTLCommandBuffer?) throws {
        updateCarInstanceBuffers()

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

    private func fillPhysicsProbeInput() {
        let fwd = SIMD3<Float>(sin(carHeadingRad), 0.0, cos(carHeadingRad))
        let right = SIMD3<Float>(cos(carHeadingRad), 0.0, -sin(carHeadingRad))
        let frontZ: Float = (scene.carID == 1) ? 1.205 : ((scene.carID == 2) ? 1.16 : 1.595)
        let rearZ: Float  = (scene.carID == 1) ? -1.48 : ((scene.carID == 2) ? -1.39 : -1.13)
        var inp = RTPhysicsProbeInput(
            carPos: SIMD4<Float>(carWorldPosition, carHeadingRad),
            carForward: SIMD4<Float>(fwd, 0.84),
            carRight: SIMD4<Float>(right, frontZ),
            dims: SIMD4<Float>(rearZ, 0.33, 0.96, 2.28)
        )
        memcpy(physicsInBuffer.contents(), &inp, MemoryLayout<RTPhysicsProbeInput>.stride)
    }

    func encodePhysicsProbe(commandBuffer: MTLCommandBuffer) {
        guard world != nil, let enc = commandBuffer.makeComputeCommandEncoder() else { return }
        fillPhysicsProbeInput()
        enc.setComputePipelineState(physicsProbePSO)
        enc.setAccelerationStructure(tlas, bufferIndex: 0)
        enc.setBuffer(physicsInBuffer, offset: 0, index: 1)
        enc.setBuffer(physicsOutBuffer, offset: 0, index: 2)
        enc.useResources(blasList, usage: .read)
        enc.dispatchThreads(MTLSize(width: 1, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 1, height: 1, depth: 1))
        enc.endEncoding()
    }

    private func readPhysicsProbeOutput() -> RTPhysicsProbeOutput {
        var out = RTPhysicsProbeOutput(wheelHitY: .zero, wallDist: .zero, wallNormalFront: .zero, wallNormalSide: .zero)
        memcpy(&out, physicsOutBuffer.contents(), MemoryLayout<RTPhysicsProbeOutput>.stride)
        return out
    }

    func snapCarToWorldRoad() {
        guard world != nil, let cb = commandQueue.makeCommandBuffer() else { return }
        encodePhysicsProbe(commandBuffer: cb)
        cb.commit()
        cb.waitUntilCompleted()
        let probe = readPhysicsProbeOutput()
        let hits = [probe.wheelHitY.x, probe.wheelHitY.y, probe.wheelHitY.z, probe.wheelHitY.w].filter { $0 > -5000.0 }
        if !hits.isEmpty {
            let avgY = hits.reduce(0.0, +) / Float(hits.count)
            carWorldPosition.y = avgY
            if probe.wheelHitY.x > -5000.0 && probe.wheelHitY.y > -5000.0 &&
               probe.wheelHitY.z > -5000.0 && probe.wheelHitY.w > -5000.0 {
                let frontY = 0.5 * (probe.wheelHitY.x + probe.wheelHitY.y)
                let rearY  = 0.5 * (probe.wheelHitY.z + probe.wheelHitY.w)
                let leftY  = 0.5 * (probe.wheelHitY.x + probe.wheelHitY.z)
                let rightY = 0.5 * (probe.wheelHitY.y + probe.wheelHitY.w)
                terrainPitchRad = max(-0.22, min(0.22, -atan2(frontY - rearY, 2.7)))
                terrainRollRad  = max(-0.18, min(0.18, atan2(leftY - rightY, 1.7)))
            }
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

    private func resolvedPreset() -> RTLightingPreset {
        let key = activePresetKey == "night_rain" ? "night_neon" : activePresetKey
        return scene.presets[key] ?? scene.presets["golden_hour"]!
    }

    private func resolvedSunDirection(preset: RTLightingPreset) -> SIMD3<Float> {
        if timeCycleEnabled {
            let phase = (timeOfDayHours / 24.0) * 2.0 * .pi
            let el = max(0.08, sin(phase - .pi * 0.5) * 0.72 + 0.25)
            let az = phase - 0.65
            return simd_normalize(SIMD3<Float>(cos(el) * sin(az), sin(el), cos(el) * cos(az)))
        }
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
        let frontZ: Float = (scene.carID == 1) ? 1.205 : ((scene.carID == 2) ? 1.16 : 1.595)
        let rearZ: Float  = (scene.carID == 1) ? -1.48 : ((scene.carID == 2) ? -1.39 : -1.13)
        let effectiveRain: Float = (activePresetKey == "night_neon" || activePresetKey == "night_rain")
            ? max(rainIntensity, 0.85)
            : rainIntensity
        var fx = RTDynamicFXGPU.zeroed()
        fx.timeWeather = SIMD4<Float>(simTimeSec, timeOfDayHours, effectiveRain, lookdevConfig.puddle_wetness ?? p.wetness)
        fx.carMetrics = SIMD4<Float>(frontZ, rearZ, 0.32, (scene.carID == 1) ? 0.34 : 0.33)
        memcpy(dynamicFXBuffer.contents(), &fx, MemoryLayout<RTDynamicFXGPU>.stride)
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
        let headsOn = lookdevConfig.headlights_on ?? p.headlightsOn
        let vignette = lookdevConfig.vignette ?? 0.12
        let contrast = lookdevConfig.contrast ?? 1.03
        let saturation = lookdevConfig.saturation ?? 1.08
        let denoisePasses = Float(denoiserEnabled ? (lookdevConfig.denoise_passes ?? (temporal1SPP ? 3 : 4)) : 0)

        let (gearVal, rpmNorm) = computeGearAndRPM(speedKmh: speedKmh)
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
            carWorldPos: SIMD4<Float>(carWorldPosition, world != nil ? 1.0 : 0.0)
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
        enc.setBuffer(world?.vertexBuffer ?? dummyWorldVB, offset: 0, index: 7)
        enc.setBuffer(world?.indexBuffer ?? dummyWorldIB, offset: 0, index: 8)
        enc.setBuffer(world?.materialBuffer ?? dummyWorldMB, offset: 0, index: 9)
        enc.setBuffer(world?.bindlessTexIDBuffer ?? dummyWorldTB, offset: 0, index: 10)
        enc.setBuffer(worldLightsBuffer, offset: 0, index: 11)
        enc.setBuffer(worldLightGridBuffer, offset: 0, index: 12)
        enc.setBuffer(dynamicFXBuffer, offset: 0, index: 13)
        enc.useResources(blasList, usage: .read)
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
        enc.setComputePipelineState(hudOverlayPSO)
        enc.setTexture(inputTexture, index: 0)
        enc.setTexture(outputTexture, index: 1)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        enc.setBytes(&srgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
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
            enc.setComputePipelineState(hudOverlayPSO)
            enc.setTexture(ldrScratch, index: 0)
            enc.setTexture(outputTexture, index: 1)
            enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
            enc.setBytes(&hudSrgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
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

        throttleHeld = true
        speedKmh = max(speedKmh, 110.0)
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
                    simTimeSec += dt
                    if timeCycleEnabled {
                        timeOfDayHours = (timeOfDayHours + dt * 0.25).truncatingRemainder(dividingBy: 24.0)
                    }
                    guard let cb = commandQueue.makeCommandBuffer() else { continue }
                    try rebuildTLAS(commandBuffer: cb)
                    if world != nil {
                        encodePhysicsProbe(commandBuffer: cb)
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
                simTimeSec += dt
                if timeCycleEnabled {
                    timeOfDayHours = (timeOfDayHours + dt * 0.25).truncatingRemainder(dividingBy: 24.0)
                }

                guard let cb = commandQueue.makeCommandBuffer() else { continue }
                try rebuildTLAS(commandBuffer: cb)
                if world != nil {
                    encodePhysicsProbe(commandBuffer: cb)
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
        }
        throttleHeld = false

        try RTSceneBuilder.writeBGRA8TextureToPNG(texture: outTex, outURL: outURL)
        let avgMs = (timedFrames > 0) ? (totalWallSec / Double(timedFrames)) * 1000.0 : 0.0
        let fps = avgMs > 0.001 ? 1000.0 / avgMs : 0.0
        return (avgMs, fps)
    }

    func stepDrivePhysics(dt: Float) -> Bool {
        var moved = false
        let targetPitch: Float
        if throttleHeld {
            speedKmh = min(310.0, speedKmh + 68.0 * dt)
            targetPitch = -0.011
        } else if brakeHeld {
            speedKmh = max(0.0, speedKmh - 120.0 * dt)
            targetPitch = speedKmh > 2.0 ? 0.014 : 0.0
        } else {
            if speedKmh > 0.1 {
                speedKmh = max(0.0, speedKmh - 14.0 * dt)
            }
            targetPitch = 0.0
        }
        if abs(targetPitch - carPitchRad) > 0.0004 {
            carPitchRad += (targetPitch - carPitchRad) * min(1.0, dt * 7.0)
            moved = true
        }

        let targetSteer: Float = (steerLeftHeld ? 22.0 : 0.0) + (steerRightHeld ? -22.0 : 0.0)
        if abs(targetSteer - steerDeg) > 0.05 {
            steerDeg += (targetSteer - steerDeg) * min(1.0, dt * 8.5)
            moved = true
        } else if abs(steerDeg) > 0.05 {
            steerDeg *= max(0.0, 1.0 - dt * 6.0)
            moved = true
        }

        if speedKmh > 0.2 {
            let speedMs = speedKmh / 3.6
            wheelSpinRad += (speedMs / 0.33) * dt
            let steerRad = steerDeg * .pi / 180.0
            let speedFrac = min(speedKmh / 180.0, 1.0)
            carRollRad = -steerRad * 0.14 * speedFrac

            if world != nil {
                // Real 3D Rockport City driving using GPU BVH road/wall physics probe!
                let probe = readPhysicsProbeOutput()
                let turnRate = steerRad * min(speedMs / 10.0, 1.8) * 0.65
                carHeadingRad += turnRate * dt
                let fwd = SIMD3<Float>(sin(carHeadingRad), 0.0, cos(carHeadingRad))
                var step = fwd * (speedMs * dt)

                // Wall collision projection (smooth NFSMW barrier sliding!)
                if probe.wallDist.x < 2.35 && probe.wallNormalFront.w > 0.5 {
                    let wn = simd_normalize(SIMD3<Float>(probe.wallNormalFront.x, 0, probe.wallNormalFront.z))
                    let intoWall = simd_dot(step, wn)
                    if intoWall < 0.0 {
                        step -= wn * intoWall
                        speedKmh *= 0.85
                    }
                }
                if probe.wallNormalSide.w != 0.0 {
                    let sideDist = (probe.wallNormalSide.w < 0) ? probe.wallDist.z : probe.wallDist.w
                    if sideDist < 1.15 {
                        let sn = simd_normalize(SIMD3<Float>(probe.wallNormalSide.x, 0, probe.wallNormalSide.z))
                        carWorldPosition += sn * (1.15 - sideDist) * 0.5
                    }
                }

                carWorldPosition += step

                // Smoothly seat car Y + slope pitch/roll onto 4 wheel drop ray hits
                let hits = [probe.wheelHitY.x, probe.wheelHitY.y, probe.wheelHitY.z, probe.wheelHitY.w].filter { $0 > -5000.0 }
                if !hits.isEmpty {
                    let avgY = hits.reduce(0.0, +) / Float(hits.count)
                    carWorldPosition.y += (avgY - carWorldPosition.y) * min(1.0, dt * 18.0)
                    if probe.wheelHitY.x > -5000.0 && probe.wheelHitY.y > -5000.0 &&
                       probe.wheelHitY.z > -5000.0 && probe.wheelHitY.w > -5000.0 {
                        let frontY = 0.5 * (probe.wheelHitY.x + probe.wheelHitY.y)
                        let rearY  = 0.5 * (probe.wheelHitY.z + probe.wheelHitY.w)
                        let leftY  = 0.5 * (probe.wheelHitY.x + probe.wheelHitY.z)
                        let rightY = 0.5 * (probe.wheelHitY.y + probe.wheelHitY.w)
                        let targetTPitch = max(-0.22, min(0.22, -atan2(frontY - rearY, 2.7)))
                        let targetTRoll  = max(-0.18, min(0.18, atan2(leftY - rightY, 1.7)))
                        terrainPitchRad += (targetTPitch - terrainPitchRad) * min(1.0, dt * 12.0)
                        terrainRollRad  += (targetTRoll - terrainRollRad) * min(1.0, dt * 12.0)
                    }
                }

                // Follow car cleanly with current camera mode
                let headingDeg = carHeadingRad * 180.0 / .pi
                let fovFrac = min(speedKmh / 240.0, 1.0)
                if cameraMode == .chase || chaseCamEnabled {
                    camera.target = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.72, carWorldPosition.z)
                    camera.azimuthDeg = headingDeg + 180.0 + steerDeg * 0.42
                    camera.fovDeg = 44.0 + 18.0 * fovFrac
                    camera.distance = 4.55 + 0.35 * fovFrac
                } else {
                    camera.target = SIMD3<Float>(carWorldPosition.x, carWorldPosition.y + 0.52, carWorldPosition.z)
                }
            } else {
                // Procedural overpass scroll mode
                roadScrollZ += speedMs * dt
                carYawRad = steerRad * 0.20 * speedFrac
                if chaseCamEnabled {
                    let fovFrac = min(speedKmh / 240.0, 1.0)
                    camera.fovDeg = 44.0 + 18.0 * fovFrac
                    camera.distance = 4.45 + 0.35 * fovFrac
                }
            }
            moved = true
        } else if abs(carRollRad) > 0.001 || abs(carYawRad) > 0.001 {
            carRollRad *= max(0.0, 1.0 - dt * 6.0)
            carYawRad *= max(0.0, 1.0 - dt * 6.0)
            moved = true
        }
        return moved
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {
        historyResetPending = true
        resetAccumulation()
    }

    func draw(in view: MTKView) {
        let now = CFAbsoluteTimeGetCurrent()
        let dt = Float(min(0.05, max(0.001, now - lastFrameTime)))
        lastFrameTime = now

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
            simTimeSec += realDt
            if timeCycleEnabled {
                timeOfDayHours = (timeOfDayHours + realDt * 0.25).truncatingRemainder(dividingBy: 24.0)
            }

            let moved = stepDrivePhysics(dt: realDt)
            if moved {
                try? rebuildTLAS(commandBuffer: cmdBuf)
                if world != nil {
                    encodePhysicsProbe(commandBuffer: cmdBuf)
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

        simTimeSec += dt
        if timeCycleEnabled {
            timeOfDayHours = (timeOfDayHours + dt * 0.25).truncatingRemainder(dividingBy: 24.0)
        }
        let moved = stepDrivePhysics(dt: dt)
        if moved {
            try? rebuildTLAS(commandBuffer: cmdBuf)
            if world != nil {
                encodePhysicsProbe(commandBuffer: cmdBuf)
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
