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
    }

    mutating func reset() {
        target = defaultTarget
        distance = defaultDistance
        azimuthDeg = defaultAzimuthDeg
        elevationDeg = defaultElevationDeg
    }

    func eyePosition() -> SIMD3<Float> {
        let az = azimuthDeg * .pi / 180.0
        let el = elevationDeg * .pi / 180.0
        let x = target.x + distance * cos(el) * sin(az)
        let y = max(0.22, target.y + distance * sin(el))
        let z = target.z + distance * cos(el) * cos(az)
        return SIMD3<Float>(x, y, z)
    }

    func basis() -> (eye: SIMD3<Float>, forward: SIMD3<Float>, right: SIMD3<Float>, up: SIMD3<Float>, invViewProj: simd_float4x4) {
        let eye = eyePosition()
        let f = simd_normalize(target - eye)
        let r = simd_normalize(simd_cross(f, SIMD3<Float>(0, 1, 0)))
        let u = simd_cross(r, f)
        return (eye, f, r, u, matrix_identity_float4x4)
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
        let farZ: Float = 220.0
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
    let scene: RTScenePackage

    private var raytracePSO: MTLComputePipelineState!
    private var bloomThresholdPSO: MTLComputePipelineState!
    private var bloomBlurPSO: MTLComputePipelineState!
    private var compositePSO: MTLComputePipelineState!
    private var linearSampler: MTLSamplerState!
    private var repeatSampler: MTLSamplerState!

    private var blasList: [MTLAccelerationStructure] = []
    private var tlas: MTLAccelerationStructure!
    private var instanceMetaBuffer: MTLBuffer!
    private var instanceDescBuffer: MTLBuffer!

    private var accumTex: MTLTexture?
    private var bloomTexA: MTLTexture?
    private var bloomTexB: MTLTexture?
    private var targetWidth: Int = 0
    private var targetHeight: Int = 0

    var camera: RTOrbitCamera
    var activePresetKey: String = "golden_hour" {
        didSet {
            if activePresetKey != oldValue { resetAccumulation() }
        }
    }
    var rtEnabled: Bool = true {
        didSet {
            if rtEnabled != oldValue { resetAccumulation() }
        }
    }
    var liveryEnabled: Bool = true {
        didSet {
            if liveryEnabled != oldValue { resetAccumulation() }
        }
    }
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

    private func buildAccelerationStructures() throws {
        guard let cmdBuf = commandQueue.makeCommandBuffer(),
              let accelEnc = cmdBuf.makeAccelerationStructureCommandEncoder() else {
            throw NSError(domain: "RTRenderer", code: 104, userInfo: [NSLocalizedDescriptionKey: "Failed to create MTLAccelerationStructureCommandEncoder"])
        }

        blasList.removeAll()
        var instanceDescs: [MTLAccelerationStructureInstanceDescriptor] = []
        var instanceMetas: [RTInstanceMeta] = []

        for (idx, mesh) in scene.meshes.enumerated() {
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
            blasList.append(blas)

            var instDesc = MTLAccelerationStructureInstanceDescriptor()
            instDesc.transformationMatrix = RTRenderer.packed4x3(mesh.modelMatrix)
            instDesc.options = .opaque
            instDesc.mask = mesh.isTransparent ? 0x02 : 0x01
            instDesc.intersectionFunctionTableOffset = 0
            instDesc.accelerationStructureIndex = UInt32(idx)
            instanceDescs.append(instDesc)

            let invT = simd_transpose(simd_inverse(mesh.modelMatrix))
            instanceMetas.append(RTInstanceMeta(
                vertexOffset: UInt32(mesh.vertexOffset),
                indexOffset: UInt32(mesh.indexOffset),
                triOffset: UInt32(mesh.triOffset),
                flags: mesh.flags,
                modelMatrix: mesh.modelMatrix,
                invTransposeMatrix: invT
            ))
        }

        let descByteCount = instanceDescs.count * MemoryLayout<MTLAccelerationStructureInstanceDescriptor>.stride
        let metaByteCount = instanceMetas.count * MemoryLayout<RTInstanceMeta>.stride
        guard let iDescBuf = device.makeBuffer(bytes: instanceDescs, length: descByteCount, options: .storageModeShared),
              let iMetaBuf = device.makeBuffer(bytes: instanceMetas, length: metaByteCount, options: .storageModeShared) else {
            throw NSError(domain: "RTRenderer", code: 106, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate TLAS instance buffers"])
        }
        instanceDescBuffer = iDescBuf
        instanceMetaBuffer = iMetaBuf

        let tlasDesc = MTLInstanceAccelerationStructureDescriptor()
        tlasDesc.instancedAccelerationStructures = blasList
        tlasDesc.instanceCount = instanceDescs.count
        tlasDesc.instanceDescriptorBuffer = instanceDescBuffer
        tlasDesc.instanceDescriptorType = .default

        let tlasSizes = device.accelerationStructureSizes(descriptor: tlasDesc)
        guard let tlasAS = device.makeAccelerationStructure(size: tlasSizes.accelerationStructureSize),
              let tlasScratch = device.makeBuffer(length: max(tlasSizes.buildScratchBufferSize, 256), options: .storageModePrivate) else {
            throw NSError(domain: "RTRenderer", code: 107, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate TLAS"])
        }
        tlas = tlasAS
        accelEnc.build(accelerationStructure: tlas, descriptor: tlasDesc, scratchBuffer: tlasScratch, scratchBufferOffset: 0)
        accelEnc.endEncoding()
        cmdBuf.commit()
        cmdBuf.waitUntilCompleted()
        if let err = cmdBuf.error {
            throw err
        }
    }

    private func ensureRenderTargets(width: Int, height: Int) {
        if width == targetWidth && height == targetHeight && accumTex != nil { return }
        targetWidth = width
        targetHeight = height

        let accDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba32Float, width: width, height: height, mipmapped: false)
        accDesc.usage = [.shaderRead, .shaderWrite]
        accDesc.storageMode = .private
        accumTex = device.makeTexture(descriptor: accDesc)

        let bw = max(1, width / 2)
        let bh = max(1, height / 2)
        let bloomDesc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba16Float, width: bw, height: bh, mipmapped: false)
        bloomDesc.usage = [.shaderRead, .shaderWrite]
        bloomDesc.storageMode = .private
        bloomTexA = device.makeTexture(descriptor: bloomDesc)
        bloomTexB = device.makeTexture(descriptor: bloomDesc)
        resetAccumulation()
    }

    private func resolvedPreset() -> RTLightingPreset {
        let key = activePresetKey == "night_rain" ? "night_neon" : activePresetKey
        return scene.presets[key] ?? scene.presets["golden_hour"]!
    }

    private func makeUniforms(width: Int, height: Int, sampleIdx: UInt32, splitCompare: Bool) -> RTFrameUniforms {
        let p = resolvedPreset()
        let aspect = Float(width) / Float(max(height, 1))
        let vp = camera.viewProjMatrix(aspect: aspect)
        let invVP = simd_inverse(vp)
        let b = camera.basis()
        return RTFrameUniforms(
            invViewProjMatrix: invVP,
            cameraPos: SIMD4<Float>(b.eye, camera.fovDeg),
            cameraForward: SIMD4<Float>(b.forward, aspect),
            cameraRight: SIMD4<Float>(b.right, tan(camera.fovDeg * .pi / 360.0)),
            cameraUp: SIMD4<Float>(b.up, p.exposure),
            sunDir: SIMD4<Float>(simd_normalize(p.sunDirection), p.sunIntensity),
            sunColor: SIMD4<Float>(p.sunColor, p.sunConeRadius),
            coronaColor: SIMD4<Float>(p.coronaColor, p.iblIntensity),
            skyZenith: SIMD4<Float>(p.skyZenith, p.wetness),
            skyHorizon: SIMD4<Float>(p.skyHorizon, p.fogDensity),
            skyGround: SIMD4<Float>(p.skyGround, p.headlightsOn),
            neonParams: SIMD4<Float>(p.neonTint, p.neonIntensity),
            postParams: SIMD4<Float>(p.bloomThreshold, p.bloomIntensity, liveryEnabled ? 1.0 : 0.0, rtEnabled ? 1.0 : 0.0),
            rtParams: SIMD4<Float>(Float(width), Float(height), Float(sampleIdx), splitCompare ? 1.0 : 0.0)
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
        guard let acc = accumTex,
              let enc = commandBuffer.makeComputeCommandEncoder() else { return }
        var uniforms = makeUniforms(width: width, height: height, sampleIdx: sampleIdx, splitCompare: splitCompare)
        enc.setComputePipelineState(raytracePSO)
        enc.setTexture(acc, index: 0)
        enc.setTexture(scene.asphaltTexture, index: 1)
        enc.setTexture(scene.carbonTexture, index: 2)
        enc.setSamplerState(repeatSampler, index: 0)
        enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
        enc.setAccelerationStructure(tlas, bufferIndex: 1)
        enc.setBuffer(scene.vertexBuffer, offset: 0, index: 2)
        enc.setBuffer(scene.indexBuffer, offset: 0, index: 3)
        enc.setBuffer(scene.triMaterialBuffer, offset: 0, index: 4)
        enc.setBuffer(instanceMetaBuffer, offset: 0, index: 5)
        enc.setBuffer(scene.materialBuffer, offset: 0, index: 6)
        // Ensure Metal driver keeps all BLAS acceleration structures resident during TLAS traversal
        enc.useResources(blasList, usage: .read)
        dispatch2D(encoder: enc, pso: raytracePSO, width: width, height: height)
        enc.endEncoding()
    }

    func encodePostProcess(commandBuffer: MTLCommandBuffer, outputTexture: MTLTexture, encodeSRGB: Bool, splitCompare: Bool) {
        let w = outputTexture.width
        let h = outputTexture.height
        guard let acc = accumTex, let bloomA = bloomTexA, let bloomB = bloomTexB else { return }
        var uniforms = makeUniforms(width: w, height: h, sampleIdx: sampleIndex, splitCompare: splitCompare)

        if let enc = commandBuffer.makeComputeCommandEncoder() {
            enc.setComputePipelineState(bloomThresholdPSO)
            enc.setTexture(acc, index: 0)
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
            enc.setTexture(acc, index: 0)
            enc.setTexture(bloomA, index: 1)
            enc.setTexture(outputTexture, index: 2)
            enc.setSamplerState(linearSampler, index: 0)
            enc.setBytes(&uniforms, length: MemoryLayout<RTFrameUniforms>.stride, index: 0)
            enc.setBytes(&srgbFlag, length: MemoryLayout<UInt32>.stride, index: 1)
            dispatch2D(encoder: enc, pso: compositePSO, width: w, height: h)
            enc.endEncoding()
        }
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

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {
        resetAccumulation()
    }

    func draw(in view: MTKView) {
        guard let drawable = view.currentDrawable,
              let cmdBuf = commandQueue.makeCommandBuffer() else { return }
        let tex = drawable.texture
        ensureRenderTargets(width: tex.width, height: tex.height)

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
