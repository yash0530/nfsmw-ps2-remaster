import Foundation
import Metal
import MetalKit
import simd

struct RTFrameUniforms {
    var invViewProjMatrix: simd_float4x4
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
        let y = max(0.22, target.y + distance * sin(el))
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

    func viewProjMatrix(aspect: Float) -> simd_float4x4 {
        let eye = eyePosition()
        let f = simd_normalize(target - eye)
        let s = simd_normalize(simd_cross(f, SIMD3<Float>(0, 1, 0)))
        let u = simd_cross(s, f)
        let view = simd_float4x4(
            SIMD4<Float>(s.x, u.x, -f.x, 0),
            SIMD4<Float>(s.y, u.y, -f.y, 0),
            SIMD4<Float>(s.z, u.z, -f.z, 0),
            SIMD4<Float>(-simd_dot(s, eye), -simd_dot(u, eye), simd_dot(f, eye), 1)
        )
        let fovY = fovDeg * .pi / 180.0
        let yScale = 1.0 / tan(fovY * 0.5)
        let xScale = yScale / max(aspect, 0.01)
        let nearZ: Float = 0.08
        let farZ: Float = 260.0
        let zRange = farZ - nearZ
        let proj = simd_float4x4(
            SIMD4<Float>(xScale, 0, 0, 0),
            SIMD4<Float>(0, yScale, 0, 0),
            SIMD4<Float>(0, 0, -farZ / zRange, -1),
            SIMD4<Float>(0, 0, -(farZ * nearZ) / zRange, 0)
        )
        return proj * view
    }
}

final class RTRenderer: NSObject, MTKViewDelegate {
    let device: MTLDevice
    let commandQueue: MTLCommandQueue
    var scene: RTScenePackage
    var forceSyntheticMode: Bool = false

    private var raytracePSO: MTLComputePipelineState!
    private var denoiseAtrousPSO: MTLComputePipelineState!
    private var remodulatePSO: MTLComputePipelineState!
    private var bloomThresholdPSO: MTLComputePipelineState!
    private var bloomBlurPSO: MTLComputePipelineState!
    private var compositePSO: MTLComputePipelineState!
    private var linearSampler: MTLSamplerState!
    private var repeatSampler: MTLSamplerState!

    private var blasList: [MTLAccelerationStructure] = []
    private var tlas: MTLAccelerationStructure!
    private var tlasScratch: MTLBuffer!
    private var instanceMetaBuffer: MTLBuffer!
    private var instanceDescBuffer: MTLBuffer!
    private var instanceCount: Int = 0
    private var meshToBlasIndex: [Int] = []

    private var accumDiffTex: MTLTexture?
    private var accumSpecTex: MTLTexture?
    private var gAlbedoTex: MTLTexture?
    private var gNormalRoughTex: MTLTexture?
    private var gDepthTex: MTLTexture?
    private var gEmissiveTex: MTLTexture?
    private var denoiseDiffPing: MTLTexture?
    private var denoiseDiffPong: MTLTexture?
    private var denoiseSpecPing: MTLTexture?
    private var denoiseSpecPong: MTLTexture?
    private var hdrResolvedTex: MTLTexture?
    private var bloomTexA: MTLTexture?
    private var bloomTexB: MTLTexture?
    private var targetWidth: Int = 0
    private var targetHeight: Int = 0

    var camera: RTOrbitCamera
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
    var liveryEnabled: Bool = true {
        didSet { if liveryEnabled != oldValue { resetAccumulation() } }
    }
    var hudEnabled: Bool = false {
        didSet { if hudEnabled != oldValue { resetAccumulation() } }
    }

    // Real-time drive state
    var driveDemoMode: Bool = false
    var chaseCamEnabled: Bool = false
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

    init(device: MTLDevice, scene: RTScenePackage, metalFileURL: URL?) throws {
        guard device.supportsRaytracing else {
            throw NSError(domain: "RTRenderer", code: 100, userInfo: [
                NSLocalizedDescriptionKey: "Metal hardware ray tracing is not supported on \(device.name)"
            ])
        }
        guard let queue = device.makeCommandQueue() else {
            throw NSError(domain: "RTRenderer", code: 101, userInfo: [NSLocalizedDescriptionKey: "Failed to create MTLCommandQueue"])
        }
        self.device = device
        self.commandQueue = queue
        self.scene = scene
        self.camera = RTOrbitCamera(
            target: scene.cameraTarget,
            distance: scene.cameraDistance,
            azimuthDeg: scene.cameraAzimuthDeg,
            elevationDeg: scene.cameraElevationDeg,
            fovDeg: scene.cameraFovDeg
        )
        super.init()

        try buildPipelines(metalFileURL: metalFileURL)
        buildSamplers()
        try buildAccelerationStructures()
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

    func configureDrivePose(speedKmh: Float, steerDeg: Float, useChaseCam: Bool) {
        self.speedKmh = max(0.0, min(320.0, speedKmh))
        self.steerDeg = max(-28.0, min(28.0, steerDeg))
        self.chaseCamEnabled = useChaseCam
        let steerRad = self.steerDeg * .pi / 180.0
        let speedFrac = min(self.speedKmh / 240.0, 1.0)
        self.carRollRad = -steerRad * 0.14 * min(self.speedKmh / 180.0, 1.0)
        self.carYawRad = steerRad * 0.22 * min(self.speedKmh / 180.0, 1.0)
        self.carPitchRad = -0.008 * speedFrac // Subtle rear squat under high-speed acceleration
        self.wheelSpinRad = (self.speedKmh / 3.6) * 0.45 / 0.33
        self.roadScrollZ = 14.5
        if useChaseCam {
            camera.azimuthDeg = 180.0 + self.steerDeg * 0.45
            camera.elevationDeg = 7.2
            camera.distance = 4.45 + 0.35 * speedFrac
            // Speed-sensitive dynamic FOV widening (44 deg -> 62 deg at top speed)
            camera.fovDeg = 44.0 + 18.0 * speedFrac
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
            forceSynthetic: forceSyntheticMode
        )
        self.scene = newScene
        self.camera = RTOrbitCamera(
            target: newScene.cameraTarget,
            distance: newScene.cameraDistance,
            azimuthDeg: newScene.cameraAzimuthDeg,
            elevationDeg: newScene.cameraElevationDeg,
            fovDeg: newScene.cameraFovDeg
        )
        if chaseCamEnabled {
            configureDrivePose(speedKmh: speedKmh, steerDeg: steerDeg, useChaseCam: true)
        }
        try buildAccelerationStructures()
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
    }

    private func buildPipelines(metalFileURL: URL?) throws {
        let library: MTLLibrary
        let exeURL = Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])
        let siblingMetallib = exeURL.deletingLastPathComponent().appendingPathComponent("RockportMetalRT.metallib")
        let siblingSource = exeURL.deletingLastPathComponent().appendingPathComponent("RayTracing.metal")

        if FileManager.default.fileExists(atPath: siblingMetallib.path),
           let precompiled = try? device.makeLibrary(URL: siblingMetallib) {
            library = precompiled
        } else {
            let candidates = [
                metalFileURL,
                siblingSource,
                URL(fileURLWithPath: "native_metal_rt/RayTracing.metal"),
                URL(fileURLWithPath: "RayTracing.metal")
            ].compactMap { $0 }

            var loadedSource: String? = nil
            for cand in candidates where FileManager.default.fileExists(atPath: cand.path) {
                loadedSource = try String(contentsOf: cand, encoding: .utf8)
                break
            }
            guard let source = loadedSource else {
                throw NSError(domain: "RTRenderer", code: 102, userInfo: [NSLocalizedDescriptionKey: "RayTracing.metal not found"])
            }
            let opts = MTLCompileOptions()
            opts.fastMathEnabled = true
            library = try device.makeLibrary(source: source, options: opts)
        }

        func makeCompute(_ name: String) throws -> MTLComputePipelineState {
            guard let fn = library.makeFunction(name: name) else {
                throw NSError(domain: "RTRenderer", code: 103, userInfo: [NSLocalizedDescriptionKey: "Missing Metal kernel \(name)"])
            }
            return try device.makeComputePipelineState(function: fn)
        }

        raytracePSO = try makeCompute("raytraceKernel")
        denoiseAtrousPSO = try makeCompute("denoiseAtrousKernel")
        remodulatePSO = try makeCompute("remodulateKernel")
        bloomThresholdPSO = try makeCompute("bloomThresholdKernel")
        bloomBlurPSO = try makeCompute("bloomBlurKernel")
        compositePSO = try makeCompute("compositeKernel")
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

    private func dynamicModelMatrix(for mesh: RTBLASMesh) -> simd_float4x4 {
        let steerRad = steerDeg * .pi / 180.0
        let carBodyXform = Self.rotationY(carYawRad) * Self.rotationX(carPitchRad) * Self.rotationZ(carRollRad)

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
        case .carBody:
            return carBodyXform * mesh.modelMatrix
        case .wheelFL, .wheelFR:
            let p = mesh.pivotCenter
            let localWheel = Self.translation(p) * Self.rotationY(steerRad) * Self.rotationX(wheelSpinRad) * Self.translation(-p)
            return carBodyXform * localWheel * mesh.modelMatrix
        case .wheelRL, .wheelRR:
            let p = mesh.pivotCenter
            let localWheel = Self.translation(p) * Self.rotationX(wheelSpinRad) * Self.translation(-p)
            return carBodyXform * localWheel * mesh.modelMatrix
        case .brakeFL, .brakeFR:
            let p = mesh.pivotCenter
            let localBrake = Self.translation(p) * Self.rotationY(steerRad) * Self.translation(-p)
            return carBodyXform * localBrake * mesh.modelMatrix
        case .brakeRL, .brakeRR:
            return carBodyXform * mesh.modelMatrix
        }
    }

    private func buildAccelerationStructures() throws {
        guard let cmdBuf = commandQueue.makeCommandBuffer(),
              let accelEnc = cmdBuf.makeAccelerationStructureCommandEncoder() else {
            throw NSError(domain: "RTRenderer", code: 104, userInfo: [NSLocalizedDescriptionKey: "Failed to create MTLAccelerationStructureCommandEncoder"])
        }

        blasList.removeAll()
        meshToBlasIndex = [Int](repeating: 0, count: scene.meshes.count)

        for (idx, mesh) in scene.meshes.enumerated() {
            if let sharedIdx = mesh.sharedBlasIndex {
                meshToBlasIndex[idx] = meshToBlasIndex[sharedIdx]
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
            meshToBlasIndex[idx] = blasList.count
            blasList.append(blas)
        }

        accelEnc.endEncoding()
        cmdBuf.commit()
        cmdBuf.waitUntilCompleted()
        if let err = cmdBuf.error { throw err }

        instanceCount = scene.meshes.count
        let descByteCount = instanceCount * MemoryLayout<MTLAccelerationStructureInstanceDescriptor>.stride
        let metaByteCount = instanceCount * MemoryLayout<RTInstanceMeta>.stride
        guard let iDescBuf = device.makeBuffer(length: descByteCount, options: .storageModeShared),
              let iMetaBuf = device.makeBuffer(length: metaByteCount, options: .storageModeShared) else {
            throw NSError(domain: "RTRenderer", code: 106, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate TLAS instance buffers"])
        }
        instanceDescBuffer = iDescBuf
        instanceMetaBuffer = iMetaBuf

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

    private func updateInstanceBuffers() {
        let descPtr = instanceDescBuffer.contents().bindMemory(to: MTLAccelerationStructureInstanceDescriptor.self, capacity: instanceCount)
        let metaPtr = instanceMetaBuffer.contents().bindMemory(to: RTInstanceMeta.self, capacity: instanceCount)

        for (idx, mesh) in scene.meshes.enumerated() {
            let mat = dynamicModelMatrix(for: mesh)
            var instDesc = MTLAccelerationStructureInstanceDescriptor()
            instDesc.transformationMatrix = RTRenderer.packed4x3(mat)
            instDesc.options = .opaque
            instDesc.mask = mesh.isTransparent ? 0x02 : 0x01
            instDesc.intersectionFunctionTableOffset = 0
            instDesc.accelerationStructureIndex = UInt32(meshToBlasIndex[idx])
            descPtr[idx] = instDesc

            metaPtr[idx] = RTInstanceMeta(
                vertexOffset: UInt32(mesh.vertexOffset),
                indexOffset: UInt32(mesh.indexOffset),
                triOffset: UInt32(mesh.triOffset),
                flags: mesh.flags,
                modelMatrix: mat,
                invTransposeMatrix: simd_transpose(simd_inverse(mat))
            )
        }
    }

    func rebuildTLAS(commandBuffer externalCmd: MTLCommandBuffer?) throws {
        updateInstanceBuffers()

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

    private func ensureRenderTargets(width: Int, height: Int) {
        if width == targetWidth && height == targetHeight && accumDiffTex != nil { return }
        targetWidth = width
        targetHeight = height

        func makeTex(_ format: MTLPixelFormat, w: Int = width, h: Int = height) -> MTLTexture? {
            let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: format, width: w, height: h, mipmapped: false)
            d.usage = [.shaderRead, .shaderWrite]
            d.storageMode = .private
            return device.makeTexture(descriptor: d)
        }

        accumDiffTex = makeTex(.rgba32Float)
        accumSpecTex = makeTex(.rgba32Float)
        gAlbedoTex = makeTex(.rgba16Float)
        gNormalRoughTex = makeTex(.rgba16Float)
        gDepthTex = makeTex(.r32Float)
        gEmissiveTex = makeTex(.rgba16Float)
        denoiseDiffPing = makeTex(.rgba16Float)
        denoiseDiffPong = makeTex(.rgba16Float)
        denoiseSpecPing = makeTex(.rgba16Float)
        denoiseSpecPong = makeTex(.rgba16Float)
        hdrResolvedTex = makeTex(.rgba16Float)

        let bw = max(1, width / 2)
        let bh = max(1, height / 2)
        bloomTexA = makeTex(.rgba16Float, w: bw, h: bh)
        bloomTexB = makeTex(.rgba16Float, w: bw, h: bh)
        resetAccumulation()
    }

    private func resolvedPreset() -> RTLightingPreset {
        let key = activePresetKey == "night_rain" ? "night_neon" : activePresetKey
        return scene.presets[key] ?? scene.presets["golden_hour"]!
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

    private func makeUniforms(width: Int, height: Int, sampleIdx: UInt32, splitCompare: Bool) -> RTFrameUniforms {
        let p = resolvedPreset()
        let aspect = Float(width) / Float(max(height, 1))
        let vp = camera.viewProjMatrix(aspect: aspect)
        let invVP = simd_inverse(vp)
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
        let denoisePasses = Float(denoiserEnabled ? (lookdevConfig.denoise_passes ?? 4) : 0)

        let (gearVal, rpmNorm) = computeGearAndRPM(speedKmh: speedKmh)
        return RTFrameUniforms(
            invViewProjMatrix: invVP,
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
            carDynParams: SIMD4<Float>(steerDeg * .pi / 180.0, wheelSpinRad, carYawRad, carRollRad),
            driveParams: SIMD4<Float>(speedKmh, steerDeg, scene.hasAuthenticCarTextures ? 1.0 : 0.0, scene.carSupportsLivery ? 1.0 : 0.0),
            hudParams: SIMD4<Float>(hudEnabled ? 1.0 : 0.0, rpmNorm, gearVal, Float(scene.carID))
        )
    }

    private func dispatch2D(encoder: MTLComputeCommandEncoder, pso: MTLComputePipelineState, width: Int, height: Int) {
        let tw = pso.threadExecutionWidth
        let th = max(1, pso.maxTotalThreadsPerThreadgroup / tw)
        let tgSize = MTLSize(width: tw, height: th, depth: 1)
        let gridSize = MTLSize(width: width, height: height, depth: 1)
        encoder.dispatchThreads(gridSize, threadsPerThreadgroup: tgSize)
    }

    func encodeRayTraceSample(commandBuffer: MTLCommandBuffer, width: Int, height: Int, sampleIdx: UInt32, splitCompare: Bool) {
        guard let diff = accumDiffTex,
              let spec = accumSpecTex,
              let gAlb = gAlbedoTex,
              let gNR = gNormalRoughTex,
              let gDep = gDepthTex,
              let gEm = gEmissiveTex,
              let enc = commandBuffer.makeComputeCommandEncoder() else { return }

        var uniforms = makeUniforms(width: width, height: height, sampleIdx: sampleIdx, splitCompare: splitCompare)
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
        enc.setSamplerState(repeatSampler, index: 0)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        enc.setAccelerationStructure(tlas, bufferIndex: 1)
        enc.setBuffer(scene.vertexBuffer, offset: 0, index: 2)
        enc.setBuffer(scene.indexBuffer, offset: 0, index: 3)
        enc.setBuffer(scene.triMaterialBuffer, offset: 0, index: 4)
        enc.setBuffer(instanceMetaBuffer, offset: 0, index: 5)
        enc.setBuffer(scene.materialBuffer, offset: 0, index: 6)
        enc.useResources(blasList, usage: .read)
        dispatch2D(encoder: enc, pso: raytracePSO, width: width, height: height)
        enc.endEncoding()
    }

    func encodePostProcess(commandBuffer: MTLCommandBuffer, outputTexture: MTLTexture, encodeSRGB: Bool, splitCompare: Bool) {
        let w = outputTexture.width
        let h = outputTexture.height
        guard let diffAcc = accumDiffTex,
              let specAcc = accumSpecTex,
              let gAlb = gAlbedoTex,
              let gNR = gNormalRoughTex,
              let gDep = gDepthTex,
              let gEm = gEmissiveTex,
              let dPing = denoiseDiffPing,
              let dPong = denoiseDiffPong,
              let sPing = denoiseSpecPing,
              let sPong = denoiseSpecPong,
              let hdrRes = hdrResolvedTex,
              let bloomA = bloomTexA,
              let bloomB = bloomTexB,
              let enc = commandBuffer.makeComputeCommandEncoder() else { return }

        var uniforms = makeUniforms(width: w, height: h, sampleIdx: sampleIndex, splitCompare: splitCompare)

        let numDenoisePasses = max(0, min(5, Int(uniforms.gradeParams.w.rounded())))
        var finalDiff: MTLTexture = diffAcc
        var finalSpec: MTLTexture = specAcc

        if numDenoisePasses > 0 {
            let steps: [Int32] = [1, 2, 4, 8, 16]
            for passIdx in 0..<numDenoisePasses {
                var stepVal = steps[passIdx]
                let srcD: MTLTexture = (passIdx == 0) ? diffAcc : ((passIdx % 2 == 1) ? dPing : dPong)
                let srcS: MTLTexture = (passIdx == 0) ? specAcc : ((passIdx % 2 == 1) ? sPing : sPong)
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
                dispatch2D(encoder: enc, pso: denoiseAtrousPSO, width: w, height: h)
                finalDiff = dstD
                finalSpec = dstS
            }
        }

        // Remodulate denoised diffuse + albedo + specular + emissive into hdrResolvedTex
        enc.setComputePipelineState(remodulatePSO)
        enc.setTexture(finalDiff, index: 0)
        enc.setTexture(finalSpec, index: 1)
        enc.setTexture(gAlb, index: 2)
        enc.setTexture(gEm, index: 3)
        enc.setTexture(hdrRes, index: 4)
        dispatch2D(encoder: enc, pso: remodulatePSO, width: w, height: h)

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

        var srgbFlag: UInt32 = encodeSRGB ? 1 : 0
        enc.setComputePipelineState(compositePSO)
        enc.setTexture(hdrRes, index: 0)
        enc.setTexture(bloomA, index: 1)
        enc.setTexture(outputTexture, index: 2)
        enc.setSamplerState(linearSampler, index: 0)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        enc.setBytes(&srgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
        dispatch2D(encoder: enc, pso: compositePSO, width: w, height: h)
        enc.endEncoding()
    }

    func renderHeadless(width: Int, height: Int, spp: Int, splitCompare: Bool, outURL: URL) throws {
        ensureRenderTargets(width: width, height: height)
        resetAccumulation()

        let totalSPP = max(1, spp)
        let batchSize = 4
        var s = 0
        while s < totalSPP {
            guard let cmdBuf = commandQueue.makeCommandBuffer() else {
                throw NSError(domain: "RTRenderer", code: 110, userInfo: [NSLocalizedDescriptionKey: "Failed to create command buffer"])
            }
            let end = min(totalSPP, s + batchSize)
            while s < end {
                encodeRayTraceSample(commandBuffer: cmdBuf, width: width, height: height, sampleIdx: UInt32(s), splitCompare: splitCompare)
                s += 1
            }
            cmdBuf.commit()
            cmdBuf.waitUntilCompleted()
            if let err = cmdBuf.error { throw err }
        }
        sampleIndex = UInt32(totalSPP)

        let outDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
        outDesc.usage = [.shaderRead, .shaderWrite]
        outDesc.storageMode = .shared
        guard let outTex = device.makeTexture(descriptor: outDesc),
              let postCmd = commandQueue.makeCommandBuffer() else {
            throw NSError(domain: "RTRenderer", code: 111, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate output texture"])
        }
        encodePostProcess(commandBuffer: postCmd, outputTexture: outTex, encodeSRGB: true, splitCompare: splitCompare)
        postCmd.commit()
        postCmd.waitUntilCompleted()
        if let err = postCmd.error { throw err }

        try RTSceneBuilder.writeBGRA8TextureToPNG(texture: outTex, outURL: outURL)
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
            roadScrollZ += speedMs * dt
            wheelSpinRad += (speedMs / 0.33) * dt
            let steerRad = steerDeg * .pi / 180.0
            let speedFrac = min(speedKmh / 180.0, 1.0)
            carRollRad = -steerRad * 0.14 * speedFrac
            carYawRad = steerRad * 0.20 * speedFrac
            if chaseCamEnabled {
                let fovFrac = min(speedKmh / 240.0, 1.0)
                camera.fovDeg = 44.0 + 18.0 * fovFrac
                camera.distance = 4.45 + 0.35 * fovFrac
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
        resetAccumulation()
    }

    func draw(in view: MTKView) {
        let now = CFAbsoluteTimeGetCurrent()
        let dt = Float(min(0.05, max(0.001, now - lastFrameTime)))
        lastFrameTime = now

        guard let drawable = view.currentDrawable,
              let cmdBuf = commandQueue.makeCommandBuffer() else { return }
        let tex = drawable.texture
        ensureRenderTargets(width: tex.width, height: tex.height)

        let moved = stepDrivePhysics(dt: dt)
        if moved {
            try? rebuildTLAS(commandBuffer: cmdBuf)
            resetAccumulation()
        }

        if sampleIndex < 64 {
            encodeRayTraceSample(commandBuffer: cmdBuf, width: tex.width, height: tex.height, sampleIdx: sampleIndex, splitCompare: false)
            sampleIndex += 1
        }
        let encodeSRGB = (view.colorPixelFormat == .bgra8Unorm)
        encodePostProcess(commandBuffer: cmdBuf, outputTexture: tex, encodeSRGB: encodeSRGB, splitCompare: false)
        cmdBuf.present(drawable)
        cmdBuf.commit()
    }
}
