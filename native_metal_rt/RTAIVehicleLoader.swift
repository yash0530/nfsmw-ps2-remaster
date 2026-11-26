import CoreGraphics
import Foundation
import ImageIO
import Metal
import simd

// MARK: - Locked Hour 3 AI Vehicle RT ABI (Matches RayTracing.metal static_asserts 1:1)

/// Compact 36-byte AI vehicle vertex (matches RkptVertex stride; tires rest at local Y = 0).
struct RTAIVertex {
    var px: Float
    var py: Float
    var pz: Float
    var nx: Float
    var ny: Float
    var nz: Float
    var u: Float
    var v: Float
    /// bits 0..3: matClass (0=body, 1=tire, 2=glass, 3=interior, 4=headlight, 5=taillight, 6=lightbar)
    /// bit 4 (0x10): isLightbar
    /// bit 5 (0x20): isLeftSide (local x < 0 -> red left / blue right police strobe)
    var partFlags: UInt16
    var archetypeIdx: UInt16
}

/// 80-byte bindless AI scene argument buffer bound at raytraceKernel [[buffer(14)]].
struct RTAISceneDataGPU {
    var verticesAddress: UInt64
    var indicesAddress: UInt64
    var skinTexID0: MTLResourceID
    var skinTexID1: MTLResourceID
    var skinTexID2: MTLResourceID
    var skinTexID3: MTLResourceID
    var skinTexID4: MTLResourceID
    var skinTexID5: MTLResourceID
    var skinTexID6: MTLResourceID
    var archetypeCount: UInt32
    var flags: UInt32
}

/// 32-byte per-slot input for aiRoadProbeKernel (<= 32 threads, 2 vertical world rays/car).
struct RTAIRoadProbeInput {
    var posActive: SIMD4<Float> // xyz = AI world position, w = active (> 0.5)
    var fwdHalfWB: SIMD4<Float> // xyz = forward unit vector (sin(yaw), 0, cos(yaw)), w = halfWheelbase (m)
}

/// 16-byte per-slot output from aiRoadProbeKernel.
struct RTAIRoadProbeOutput {
    var roadY: Float     // avg road surface elevation (-9999 on miss)
    var pitchRad: Float  // road slope pitch along forward axis (clamped +-0.26 rad)
    var frontY: Float    // front-axle road hit Y (-9999 on miss)
    var rearY: Float     // rear-axle road hit Y (-9999 on miss)
}

/// Metadata for a single deduplicated AI vehicle archetype BLAS.
struct RTAIArchetypeMesh {
    let archetype: RTAIVehicleArchetype
    let vertexOffset: UInt32
    let vertexCount: UInt32
    let indexOffset: UInt32
    let indexCount: UInt32
    let triangleCount: Int
    let aabbMin: SIMD3<Float>
    let aabbMax: SIMD3<Float>
    let lightbarCenter: SIMD3<Float>
}

// MARK: - Loaded AI Vehicle GPU Pack (7 Deduplicated BLASes + Bindless Skins + Residency)

final class RTAIVehiclePack {
    static let maxAISlots = 32
    static let archetypeCount = 7

    /// RTInstanceMeta.flags bit 7 marks an AI vehicle instance (kept distinct from bit 4 = rkptWorld).
    static let metaFlagIsAIVehicle: UInt32 = (1 << 7)
    static let metaFlagIsCop: UInt32       = (1 << 12)
    static let metaFlagSirenOn: UInt32     = (1 << 13)

    let archetypes: [RTAIArchetypeMesh]
    let blasList: [MTLAccelerationStructure]
    let vertexBuffer: MTLBuffer
    let indexBuffer: MTLBuffer
    let skinTextures: [MTLTexture]
    let sceneDataBuffer: MTLBuffer
    let residencySet: AnyObject? // MTLResidencySet on macOS 15+
    let hasResidencySet: Bool
    let totalTriangles: Int
    let estimatedVRAMBytes: Int

    init(
        archetypes: [RTAIArchetypeMesh],
        blasList: [MTLAccelerationStructure],
        vertexBuffer: MTLBuffer,
        indexBuffer: MTLBuffer,
        skinTextures: [MTLTexture],
        sceneDataBuffer: MTLBuffer,
        residencySet: AnyObject?,
        hasResidencySet: Bool,
        totalTriangles: Int,
        estimatedVRAMBytes: Int
    ) {
        self.archetypes = archetypes
        self.blasList = blasList
        self.vertexBuffer = vertexBuffer
        self.indexBuffer = indexBuffer
        self.skinTextures = skinTextures
        self.sceneDataBuffer = sceneDataBuffer
        self.residencySet = residencySet
        self.hasResidencySet = hasResidencySet
        self.totalTriangles = totalTriangles
        self.estimatedVRAMBytes = estimatedVRAMBytes
    }

    /// Attach the AI residency set (vertex/index buffers, 7 skin textures, 7 BLASes) to the queue.
    func attachResidencySet(to commandQueue: MTLCommandQueue) {
        if #available(macOS 15.0, *), let rs = residencySet as? MTLResidencySet {
            commandQueue.addResidencySet(rs)
        }
    }

    /// Bind resource usage explicitly on compute encoders (fallback if residency sets are unavailable).
    func useResources(on encoder: MTLComputeCommandEncoder) {
        if hasResidencySet { return }
        encoder.useResource(vertexBuffer, usage: .read)
        encoder.useResource(indexBuffer, usage: .read)
        encoder.useResources(skinTextures, usage: .read)
        encoder.useResources(blasList, usage: .read)
    }

    /// Writes up to `maxSlots` (32) AI vehicle descriptors + RTInstanceMeta entries into the shared TLAS buffers.
    /// Keeps `.default` MTLAccelerationStructureInstanceDescriptor (zero stride churn) and writes exact
    /// per-slot `prevModelMatrix` so MetalFX TDS & Frame Interpolator track moving traffic/cops cleanly.
    @discardableResult
    func writeAITLASInstances(
        states: [RTAIInstanceState],
        baseSlot: Int,
        maxSlots: Int,
        descriptors: UnsafeMutablePointer<MTLAccelerationStructureInstanceDescriptor>,
        meta: UnsafeMutablePointer<RTInstanceMeta>,
        blasBaseIndex: Int,
        roadPitches: [Float]? = nil
    ) -> Int {
        guard !archetypes.isEmpty, !blasList.isEmpty else { return 0 }
        let slotCount = min(maxSlots, states.count)
        var activeCount = 0

        for i in 0..<slotCount {
            let st = states[i]
            let dstSlot = baseSlot + i
            let archIdx = max(0, min(archetypes.count - 1, st.archetype.rawValue))
            let arch = archetypes[archIdx]

            var instDesc = MTLAccelerationStructureInstanceDescriptor()
            instDesc.options = .opaque
            instDesc.intersectionFunctionTableOffset = 0
            instDesc.accelerationStructureIndex = UInt32(blasBaseIndex + archIdx)

            if !st.active {
                instDesc.mask = 0
                instDesc.transformationMatrix = Self.packed4x3(matrix_identity_float4x4)
                descriptors[dstSlot] = instDesc
                meta[dstSlot] = RTInstanceMeta(
                    vertexOffset: arch.vertexOffset,
                    indexOffset: arch.indexOffset,
                    triOffset: UInt32(st.slot),
                    flags: Self.metaFlagIsAIVehicle | (UInt32(archIdx & 0xF) << 8),
                    modelMatrix: matrix_identity_float4x4,
                    invTransposeMatrix: matrix_identity_float4x4,
                    prevModelMatrix: matrix_identity_float4x4
                )
                continue
            }

            let pitchRad: Float = (roadPitches != nil && i < roadPitches!.count) ? roadPitches![i] : 0.0
            let modelMat = Self.translation(st.worldPosition) * Self.rotationY(st.yawRad) * Self.rotationX(pitchRad)
            let prevMat  = Self.translation(st.prevWorldPosition) * Self.rotationY(st.prevYawRad) * Self.rotationX(pitchRad)
            let invT     = simd_transpose(simd_inverse(modelMat))

            instDesc.mask = RTTLASMask.car
            instDesc.transformationMatrix = Self.packed4x3(modelMat)
            descriptors[dstSlot] = instDesc

            let phaseNorm = st.lightbarPhase.truncatingRemainder(dividingBy: 2.0 * .pi)
            let posPhase  = phaseNorm < 0 ? (phaseNorm + 2.0 * .pi) : phaseNorm
            let phaseByte = UInt32(max(0, min(255, Int((posPhase / (2.0 * .pi)) * 255.0))))
            let paintVar  = UInt32((st.slot * 7 + archIdx * 3) & 0xF)

            var flags = Self.metaFlagIsAIVehicle
            flags |= UInt32(archIdx & 0xF) << 8
            if st.isCop   { flags |= Self.metaFlagIsCop }
            if st.sirenOn { flags |= Self.metaFlagSirenOn }
            flags |= (phaseByte & 0xFF) << 16
            flags |= (paintVar & 0xF) << 24

            meta[dstSlot] = RTInstanceMeta(
                vertexOffset: arch.vertexOffset,
                indexOffset: arch.indexOffset,
                triOffset: UInt32(st.slot),
                flags: flags,
                modelMatrix: modelMat,
                invTransposeMatrix: invT,
                prevModelMatrix: prevMat
            )
            activeCount += 1
        }

        // Zero-mask any trailing reserved AI slots beyond states.count
        if slotCount < maxSlots {
            let arch0 = archetypes[0]
            for i in slotCount..<maxSlots {
                let dstSlot = baseSlot + i
                var d = MTLAccelerationStructureInstanceDescriptor()
                d.mask = 0
                d.options = .opaque
                d.accelerationStructureIndex = UInt32(blasBaseIndex)
                d.transformationMatrix = Self.packed4x3(matrix_identity_float4x4)
                descriptors[dstSlot] = d
                meta[dstSlot] = RTInstanceMeta(
                    vertexOffset: arch0.vertexOffset,
                    indexOffset: arch0.indexOffset,
                    triOffset: UInt32(i),
                    flags: Self.metaFlagIsAIVehicle,
                    modelMatrix: matrix_identity_float4x4,
                    invTransposeMatrix: matrix_identity_float4x4,
                    prevModelMatrix: matrix_identity_float4x4
                )
            }
        }
        return activeCount
    }

    private static func packed4x3(_ m: simd_float4x4) -> MTLPackedFloat4x3 {
        return MTLPackedFloat4x3(columns: (
            MTLPackedFloat3Make(m.columns.0.x, m.columns.0.y, m.columns.0.z),
            MTLPackedFloat3Make(m.columns.1.x, m.columns.1.y, m.columns.1.z),
            MTLPackedFloat3Make(m.columns.2.x, m.columns.2.y, m.columns.2.z),
            MTLPackedFloat3Make(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        ))
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
}

// MARK: - AI Vehicle Pack Loader

enum RTAIVehicleLoader {

    static func assertContractLayout() {
        precondition(MemoryLayout<RTAIVertex>.stride == 36, "RTAIVertex stride must be 36 bytes")
        precondition(MemoryLayout<RTAISceneDataGPU>.stride == 80, "RTAISceneDataGPU stride must be 80 bytes")
        precondition(MemoryLayout<RTAIRoadProbeInput>.stride == 32, "RTAIRoadProbeInput stride must be 32 bytes")
        precondition(MemoryLayout<RTAIRoadProbeOutput>.stride == 16, "RTAIRoadProbeOutput stride must be 16 bytes")
    }

    /// Creates a safe 80-byte zeroed RTAISceneData buffer for binding at buffer(14) when no AI pack is loaded.
    static func makeEmptySceneDataBuffer(device: MTLDevice) -> MTLBuffer {
        assertContractLayout()
        guard let buf = device.makeBuffer(length: MemoryLayout<RTAISceneDataGPU>.stride, options: .storageModeShared) else {
            fatalError("Failed to allocate empty RTAISceneDataGPU buffer")
        }
        memset(buf.contents(), 0, MemoryLayout<RTAISceneDataGPU>.stride)
        return buf
    }

    /// Loads all 7 deduplicated AI vehicle archetypes from `build/gameplay_pack/ai_vehicles/`.
    /// Returns `nil` gracefully if the pack directory or `manifest.json` does not exist.
    static func load(
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        directory: URL? = nil
    ) throws -> RTAIVehiclePack? {
        assertContractLayout()
        let fm = FileManager.default
        let packDir: URL
        if let dir = directory {
            packDir = dir
        } else {
            let candidates = [
                "build/gameplay_pack/ai_vehicles",
                "../build/gameplay_pack/ai_vehicles"
            ]
            guard let found = candidates.first(where: { fm.fileExists(atPath: "\($0)/manifest.json") }) else {
                return nil
            }
            packDir = URL(fileURLWithPath: found)
        }

        let manifestURL = packDir.appendingPathComponent("manifest.json")
        guard fm.fileExists(atPath: manifestURL.path) else {
            return nil
        }

        let manifestData = try Data(contentsOf: manifestURL)
        let manifestRoot = (try JSONSerialization.jsonObject(with: manifestData) as? [String: Any]) ?? [:]
        let archMap = (manifestRoot["archetypes"] as? [String: [String: Any]]) ?? [:]

        var allVertices: [RTAIVertex] = []
        var allIndices: [UInt32] = []
        allVertices.reserveCapacity(36000)
        allIndices.reserveCapacity(60000)

        var archetypes: [RTAIArchetypeMesh] = []
        var skinTextures: [MTLTexture] = []
        var totalTexBytes = 0

        for arch in RTAIVehicleArchetype.allCases {
            let key = arch.manifestKey
            let archIdx = UInt16(arch.rawValue)
            let archDir = packDir.appendingPathComponent(key)
            let bodyURL = archDir.appendingPathComponent("body.obj")
            let lightbarURL = archDir.appendingPathComponent("lightbar.obj")
            let skinURL = archDir.appendingPathComponent("skin.png")

            let vOff = UInt32(allVertices.count)
            let iOff = UInt32(allIndices.count)
            var minB = SIMD3<Float>(repeating:  Float.greatestFiniteMagnitude)
            var maxB = SIMD3<Float>(repeating: -Float.greatestFiniteMagnitude)

            if fm.fileExists(atPath: bodyURL.path) {
                let wheelBoxes = try parseOBJ(
                    url: bodyURL,
                    archetypeIdx: archIdx,
                    forceLightbar: false,
                    stripRawWheelQuads: true,
                    vertexBase: Int(vOff),
                    vertices: &allVertices,
                    indices: &allIndices,
                    minB: &minB,
                    maxB: &maxB
                )
                // Synthesize 4 crisp 3D cylindrical tires + recessed metallic 5-spoke/police rims at each archetype's
                // exact wheel arches — completely eliminating EA PS2 flat square checkered wheel billboard quads!
                appendProcedural3DWheels(
                    wheelBoxes: wheelBoxes,
                    bodyMin: minB,
                    bodyMax: maxB,
                    archetypeIdx: archIdx,
                    vertexBase: Int(vOff),
                    vertices: &allVertices,
                    indices: &allIndices,
                    minB: &minB,
                    maxB: &maxB
                )
            }
            if fm.fileExists(atPath: lightbarURL.path) {
                _ = try parseOBJ(
                    url: lightbarURL,
                    archetypeIdx: archIdx,
                    forceLightbar: true,
                    stripRawWheelQuads: false,
                    vertexBase: Int(vOff),
                    vertices: &allVertices,
                    indices: &allIndices,
                    minB: &minB,
                    maxB: &maxB
                )
            }

            let vCount = UInt32(allVertices.count) - vOff
            let iCount = UInt32(allIndices.count) - iOff
            if vCount == 0 || iCount == 0 {
                // Degenerate tiny fallback quad inside origin so archetype slot index stays dense 0..6
                let baseLocal = UInt32(allVertices.count) - vOff
                let stub = RTAIVertex(px: 0, py: 0, pz: 0, nx: 0, ny: 1, nz: 0, u: 0, v: 0, partFlags: 0, archetypeIdx: archIdx)
                allVertices.append(contentsOf: [stub, stub, stub])
                allIndices.append(contentsOf: [baseLocal, baseLocal + 1, baseLocal + 2])
                minB = .zero
                maxB = .zero
            }

            var lbCenter = arch.localLightbarOffset
            if let metaEntry = archMap[key],
               let lbArr = metaEntry["lightbar_center_yup"] as? [Double], lbArr.count == 3 {
                lbCenter = SIMD3<Float>(Float(lbArr[0]), Float(lbArr[1]), Float(lbArr[2]))
            }

            let finalVCount = UInt32(allVertices.count) - vOff
            let finalICount = UInt32(allIndices.count) - iOff
            archetypes.append(RTAIArchetypeMesh(
                archetype: arch,
                vertexOffset: vOff,
                vertexCount: finalVCount,
                indexOffset: iOff,
                indexCount: finalICount,
                triangleCount: Int(finalICount / 3),
                aabbMin: minB,
                aabbMax: maxB,
                lightbarCenter: lbCenter
            ))

            let (tex, texBytes) = loadSkinTexture(url: skinURL, device: device, commandQueue: commandQueue)
            skinTextures.append(tex)
            totalTexBytes += texBytes
        }

        let vBytes = max(allVertices.count * MemoryLayout<RTAIVertex>.stride, 64)
        let iBytes = max(allIndices.count * MemoryLayout<UInt32>.stride, 64)
        guard let vBuf = device.makeBuffer(bytes: allVertices, length: vBytes, options: .storageModeShared),
              let iBuf = device.makeBuffer(bytes: allIndices, length: iBytes, options: .storageModeShared) else {
            throw NSError(domain: "RTAIVehicleLoader", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate AI vehicle vertex/index buffers"
            ])
        }

        // Build 7 compact hardware-RT BLASes in a single command buffer
        guard let cmdBuf = commandQueue.makeCommandBuffer(),
              let accelEnc = cmdBuf.makeAccelerationStructureCommandEncoder() else {
            throw NSError(domain: "RTAIVehicleLoader", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "Failed to create acceleration encoder for AI BLASes"
            ])
        }

        var blasList: [MTLAccelerationStructure] = []
        blasList.reserveCapacity(archetypes.count)
        var blasBytes = 0
        var scratchHolders: [MTLBuffer] = []

        for arch in archetypes {
            let geomDesc = MTLAccelerationStructureTriangleGeometryDescriptor()
            geomDesc.vertexBuffer = vBuf
            geomDesc.vertexBufferOffset = Int(arch.vertexOffset) * MemoryLayout<RTAIVertex>.stride
            geomDesc.vertexStride = MemoryLayout<RTAIVertex>.stride
            geomDesc.vertexFormat = .float3
            geomDesc.indexBuffer = iBuf
            geomDesc.indexBufferOffset = Int(arch.indexOffset) * MemoryLayout<UInt32>.stride
            geomDesc.indexType = .uint32
            geomDesc.triangleCount = arch.triangleCount
            geomDesc.opaque = true

            let blasDesc = MTLPrimitiveAccelerationStructureDescriptor()
            blasDesc.geometryDescriptors = [geomDesc]

            let sizes = device.accelerationStructureSizes(descriptor: blasDesc)
            guard let blas = device.makeAccelerationStructure(size: sizes.accelerationStructureSize),
                  let scratch = device.makeBuffer(length: max(sizes.buildScratchBufferSize, 256), options: .storageModePrivate) else {
                throw NSError(domain: "RTAIVehicleLoader", code: 3, userInfo: [
                    NSLocalizedDescriptionKey: "Failed to allocate AI BLAS for \(arch.archetype.manifestKey)"
                ])
            }
            accelEnc.build(accelerationStructure: blas, descriptor: blasDesc, scratchBuffer: scratch, scratchBufferOffset: 0)
            blasList.append(blas)
            scratchHolders.append(scratch)
            blasBytes += sizes.accelerationStructureSize
        }

        accelEnc.endEncoding()
        cmdBuf.commit()
        cmdBuf.waitUntilCompleted()
        if let err = cmdBuf.error { throw err }
        scratchHolders.removeAll()

        // Pack 80-byte RTAISceneDataGPU argument buffer (buffer(14))
        guard let sceneDataBuf = device.makeBuffer(length: MemoryLayout<RTAISceneDataGPU>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTAIVehicleLoader", code: 4, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate RTAISceneDataGPU buffer"
            ])
        }
        var sceneData = RTAISceneDataGPU(
            verticesAddress: vBuf.gpuAddress,
            indicesAddress: iBuf.gpuAddress,
            skinTexID0: skinTextures[0].gpuResourceID,
            skinTexID1: skinTextures[1].gpuResourceID,
            skinTexID2: skinTextures[2].gpuResourceID,
            skinTexID3: skinTextures[3].gpuResourceID,
            skinTexID4: skinTextures[4].gpuResourceID,
            skinTexID5: skinTextures[5].gpuResourceID,
            skinTexID6: skinTextures[6].gpuResourceID,
            archetypeCount: UInt32(archetypes.count),
            flags: 1
        )
        memcpy(sceneDataBuf.contents(), &sceneData, MemoryLayout<RTAISceneDataGPU>.stride)

        // Register all AI buffers, textures & BLASes in an MTLResidencySet (macOS 15+)
        var resSetObj: AnyObject? = nil
        var hasRes = false
        if #available(macOS 15.0, *) {
            let rsDesc = MTLResidencySetDescriptor()
            rsDesc.label = "RockportAIVehicles"
            rsDesc.initialCapacity = 2 + skinTextures.count + blasList.count
            if let rs = try? device.makeResidencySet(descriptor: rsDesc) {
                rs.addAllocation(vBuf)
                rs.addAllocation(iBuf)
                for tex in skinTextures { rs.addAllocation(tex) }
                for blas in blasList { rs.addAllocation(blas) }
                rs.commit()
                commandQueue.addResidencySet(rs)
                resSetObj = rs
                hasRes = true
            }
        }

        let totalTris = archetypes.reduce(0) { $0 + $1.triangleCount }
        let totalVRAM = vBytes + iBytes + totalTexBytes + blasBytes + MemoryLayout<RTAISceneDataGPU>.stride
        return RTAIVehiclePack(
            archetypes: archetypes,
            blasList: blasList,
            vertexBuffer: vBuf,
            indexBuffer: iBuf,
            skinTextures: skinTextures,
            sceneDataBuffer: sceneDataBuf,
            residencySet: resSetObj,
            hasResidencySet: hasRes,
            totalTriangles: totalTris,
            estimatedVRAMBytes: totalVRAM
        )
    }

    // MARK: - Private OBJ & Skin Helpers

    private static func matClassForName(_ mtl: String, forceLightbar: Bool) -> UInt16 {
        if forceLightbar { return 6 }
        let low = mtl.lowercased()
        if low.contains("lightbar")  { return 6 }
        if low.contains("taillight") || low.contains("brakelight") { return 5 }
        if low.contains("headlight") { return 4 }
        if low.contains("interior")  { return 3 }
        if low.contains("glass") || low.contains("window") { return 2 }
        if low.contains("tire") || low.contains("rubber") || low.contains("wheel") { return 1 }
        return 0
    }

    private struct WheelQuadBounds {
        var minP = SIMD3<Float>(repeating:  Float.greatestFiniteMagnitude)
        var maxP = SIMD3<Float>(repeating: -Float.greatestFiniteMagnitude)
        var count: Int = 0
        mutating func include(_ p: SIMD3<Float>) {
            minP = simd_min(minP, p)
            maxP = simd_max(maxP, p)
            count += 1
        }
    }

    private static func parseOBJ(
        url: URL,
        archetypeIdx: UInt16,
        forceLightbar: Bool,
        stripRawWheelQuads: Bool,
        vertexBase: Int,
        vertices: inout [RTAIVertex],
        indices: inout [UInt32],
        minB: inout SIMD3<Float>,
        maxB: inout SIMD3<Float>
    ) throws -> [WheelQuadBounds] {
        let raw = try String(contentsOf: url, encoding: .utf8)
        var positions: [SIMD3<Float>] = []
        var texCoords: [SIMD2<Float>] = []
        var normals: [SIMD3<Float>] = []
        positions.reserveCapacity(4096)
        texCoords.reserveCapacity(4096)
        normals.reserveCapacity(4096)

        var wheelBoxes = Array(repeating: WheelQuadBounds(), count: 4) // 0:FL, 1:FR, 2:RL, 3:RR
        var currentMatClass: UInt16 = forceLightbar ? 6 : 0
        var vertCache: [SIMD4<Int32>: UInt32] = [:]

        func parseIdx(_ tok: Substring, count: Int) -> Int {
            guard !tok.isEmpty, let v = Int(tok) else { return -1 }
            return v > 0 ? (v - 1) : (count + v)
        }

        func addVertex(vi: Int, vti: Int, vni: Int, matClass: UInt16) -> UInt32 {
            let p = (vi >= 0 && vi < positions.count) ? positions[vi] : .zero
            let isLB: UInt16 = (matClass == 6 || forceLightbar) ? 0x10 : 0
            let isLeft: UInt16 = (p.x < 0.0) ? 0x20 : 0
            let pFlags: UInt16 = (matClass & 0xF) | isLB | isLeft
            let key = SIMD4<Int32>(Int32(vi), Int32(vti), Int32(vni), Int32(pFlags))
            if let existing = vertCache[key] {
                return existing
            }
            let n = (vni >= 0 && vni < normals.count) ? normals[vni] : SIMD3<Float>(0, 1, 0)
            let t = (vti >= 0 && vti < texCoords.count) ? texCoords[vti] : .zero
            minB = simd_min(minB, p)
            maxB = simd_max(maxB, p)
            let localIdx = UInt32(vertices.count - vertexBase)
            vertices.append(RTVertexAIHelper.make(p: p, n: n, t: t, flags: pFlags, arch: archetypeIdx))
            vertCache[key] = localIdx
            return localIdx
        }

        for rawLine in raw.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            let parts = line.split(whereSeparator: { $0 == " " || $0 == "\t" })
            guard let head = parts.first else { continue }
            switch head {
            case "v":
                if parts.count >= 4, let x = Float(parts[1]), let y = Float(parts[2]), let z = Float(parts[3]) {
                    positions.append(SIMD3<Float>(x, y, z))
                }
            case "vt":
                if parts.count >= 3, let u = Float(parts[1]), let v = Float(parts[2]) {
                    texCoords.append(SIMD2<Float>(u, v))
                }
            case "vn":
                if parts.count >= 4, let x = Float(parts[1]), let y = Float(parts[2]), let z = Float(parts[3]) {
                    normals.append(SIMD3<Float>(x, y, z))
                }
            case "usemtl":
                if parts.count >= 2 {
                    currentMatClass = matClassForName(String(parts[1]), forceLightbar: forceLightbar)
                }
            case "f":
                guard parts.count >= 4 else { continue }
                if stripRawWheelQuads && currentMatClass == 1 {
                    // Record wheel quadrant AABBs from raw PS2 wheel geometry, then skip raw square billboard quads!
                    for tok in parts.dropFirst() {
                        let comps = tok.split(separator: "/", omittingEmptySubsequences: false)
                        let vi = comps.count > 0 ? parseIdx(comps[0], count: positions.count) : -1
                        if vi >= 0 && vi < positions.count {
                            let p = positions[vi]
                            if abs(p.x) > 0.25 && abs(p.z) > 0.25 {
                                let q = (p.z >= 0 ? 0 : 2) + (p.x >= 0 ? 1 : 0)
                                wheelBoxes[q].include(p)
                            }
                        }
                    }
                    continue
                }
                var faceVerts: [UInt32] = []
                faceVerts.reserveCapacity(parts.count - 1)
                for tok in parts.dropFirst() {
                    let comps = tok.split(separator: "/", omittingEmptySubsequences: false)
                    let vi  = comps.count > 0 ? parseIdx(comps[0], count: positions.count) : -1
                    let vti = comps.count > 1 ? parseIdx(comps[1], count: texCoords.count) : -1
                    let vni = comps.count > 2 ? parseIdx(comps[2], count: normals.count) : -1
                    faceVerts.append(addVertex(vi: vi, vti: vti, vni: vni, matClass: currentMatClass))
                }
                for k in 1..<(faceVerts.count - 1) {
                    indices.append(faceVerts[0])
                    indices.append(faceVerts[k])
                    indices.append(faceVerts[k + 1])
                }
            default:
                break
            }
        }
        return wheelBoxes
    }

    private enum RTVertexAIHelper {
        static func make(p: SIMD3<Float>, n: SIMD3<Float>, t: SIMD2<Float>, flags: UInt16, arch: UInt16) -> RTAIVertex {
            RTAIVertex(px: p.x, py: p.y, pz: p.z, nx: n.x, ny: n.y, nz: n.z, u: t.x, v: t.y, partFlags: flags, archetypeIdx: arch)
        }
    }

    /// Generates 4 crisp 20-segment 3D cylindrical vulcanized rubber tires (matClass=1) + recessed metallic
    /// 5-spoke/police-steelie rims (matClass=7) seated cleanly at Y = 0.000 inside each archetype's wheel arches.
    private static func appendProcedural3DWheels(
        wheelBoxes: [WheelQuadBounds],
        bodyMin: SIMD3<Float>,
        bodyMax: SIMD3<Float>,
        archetypeIdx: UInt16,
        vertexBase: Int,
        vertices: inout [RTAIVertex],
        indices: inout [UInt32],
        minB: inout SIMD3<Float>,
        maxB: inout SIMD3<Float>
    ) {
        let halfW = max(abs(bodyMin.x), abs(bodyMax.x), 0.90)
        let defaultCenters: [SIMD3<Float>] = [
            SIMD3<Float>(-(halfW - 0.12), 0.33,  1.42),
            SIMD3<Float>( (halfW - 0.12), 0.33,  1.42),
            SIMD3<Float>(-(halfW - 0.12), 0.33, -1.35),
            SIMD3<Float>( (halfW - 0.12), 0.33, -1.35)
        ]

        let segs = 20
        let tireFlags: UInt16 = 1
        let rimFlags: UInt16 = 7

        func pushVert(_ p: SIMD3<Float>, _ n: SIMD3<Float>, _ uv: SIMD2<Float>, _ flags: UInt16) -> UInt32 {
            minB = simd_min(minB, p)
            maxB = simd_max(maxB, p)
            let localIdx = UInt32(vertices.count - vertexBase)
            vertices.append(RTAIVertex(
                px: p.x, py: p.y, pz: p.z,
                nx: n.x, ny: n.y, nz: n.z,
                u: uv.x, v: uv.y,
                partFlags: flags,
                archetypeIdx: archetypeIdx
            ))
            return localIdx
        }

        for q in 0..<4 {
            let box = wheelBoxes[q]
            let isRight = (q % 2 == 1)
            let signX: Float = isRight ? 1.0 : -1.0
            let cx: Float
            let cz: Float
            let radius: Float
            let halfThick: Float
            if box.count >= 4 {
                let rawR = 0.5 * max(box.maxP.y - box.minP.y, box.maxP.z - box.minP.z)
                radius = max(0.27, min(0.41, rawR))
                let rawW = max(0.18, min(0.28, box.maxP.x - box.minP.x))
                halfThick = 0.5 * rawW
                // Keep outer tire sidewall slightly tucked inside outer body flare for realistic stance
                let outerX = isRight ? min(box.maxP.x, halfW + 0.01) : max(box.minP.x, -(halfW + 0.01))
                cx = outerX - signX * halfThick
                cz = 0.5 * (box.minP.z + box.maxP.z)
            } else {
                cx = defaultCenters[q].x
                cz = defaultCenters[q].z
                radius = 0.33
                halfThick = 0.115
            }
            let cy = radius // Bottom of tire rests strictly at local Y = 0.000
            let rimR = radius * 0.64
            let xOut = cx + signX * halfThick
            let xIn  = cx - signX * halfThick
            let xRimRecess = cx + signX * (halfThick * 0.72)

            // Center hub vertices for outer recessed rim face & inner backing disc
            let hubOut = pushVert(SIMD3<Float>(xRimRecess, cy, cz), SIMD3<Float>(signX, 0, 0), SIMD2<Float>(0, 0), rimFlags)
            let hubIn  = pushVert(SIMD3<Float>(xIn, cy, cz), SIMD3<Float>(-signX, 0, 0), SIMD2<Float>(0, 0), tireFlags)

            var treadOut: [UInt32] = []
            var treadIn: [UInt32] = []
            var swOutOuter: [UInt32] = []
            var swOutInner: [UInt32] = []
            var swInOuter: [UInt32] = []
            var rimEdge: [UInt32] = []
            treadOut.reserveCapacity(segs)
            treadIn.reserveCapacity(segs)
            swOutOuter.reserveCapacity(segs)
            swOutInner.reserveCapacity(segs)
            swInOuter.reserveCapacity(segs)
            rimEdge.reserveCapacity(segs)

            for i in 0..<segs {
                let a = Float(i) / Float(segs) * 2.0 * .pi
                let ca = cos(a), sa = sin(a)
                let radN = SIMD3<Float>(0, ca, sa)
                let pOuter = SIMD3<Float>(xOut, cy + ca * radius, cz + sa * radius)
                let pInner = SIMD3<Float>(xIn,  cy + ca * radius, cz + sa * radius)
                let pRimSw = SIMD3<Float>(xOut, cy + ca * rimR,   cz + sa * rimR)
                let pRimIn = SIMD3<Float>(xRimRecess, cy + ca * rimR, cz + sa * rimR)

                let uTread = Float(i) / Float(segs)
                treadOut.append(pushVert(pOuter, radN, SIMD2<Float>(uTread, 1.0), tireFlags))
                treadIn.append(pushVert(pInner,  radN, SIMD2<Float>(uTread, 0.0), tireFlags))
                swOutOuter.append(pushVert(pOuter, SIMD3<Float>(signX, 0, 0), SIMD2<Float>(ca, sa), tireFlags))
                swOutInner.append(pushVert(pRimSw, SIMD3<Float>(signX, 0, 0), SIMD2<Float>(ca * 0.64, sa * 0.64), tireFlags))
                swInOuter.append(pushVert(pInner,  SIMD3<Float>(-signX, 0, 0), SIMD2<Float>(ca, sa), tireFlags))
                rimEdge.append(pushVert(pRimIn,    SIMD3<Float>(signX, 0, 0), SIMD2<Float>(ca, sa), rimFlags))
            }

            for i in 0..<segs {
                let j = (i + 1) % segs
                // 1. Cylindrical tire tread band
                indices.append(contentsOf: [treadOut[i], treadIn[i], treadIn[j], treadOut[i], treadIn[j], treadOut[j]])
                // 2. Outer rubber sidewall annulus (radius -> rimR)
                indices.append(contentsOf: [swOutOuter[i], swOutOuter[j], swOutInner[j], swOutOuter[i], swOutInner[j], swOutInner[i]])
                // 3. Recessed rim lip (outer rim edge -> recessed rim face)
                indices.append(contentsOf: [swOutInner[i], swOutInner[j], rimEdge[j], swOutInner[i], rimEdge[j], rimEdge[i]])
                // 4. Recessed metallic 5-spoke / police interceptor rim disc (UVs encode polar (cos, sin) in [-1..1]!)
                indices.append(contentsOf: [hubOut, rimEdge[i], rimEdge[j]])
                // 5. Inner wheel backing disc
                indices.append(contentsOf: [hubIn, swInOuter[j], swInOuter[i]])
            }
        }
    }

    private static func loadSkinTexture(
        url: URL,
        device: MTLDevice,
        commandQueue: MTLCommandQueue
    ) -> (MTLTexture, Int) {
        if FileManager.default.fileExists(atPath: url.path),
           let src = CGImageSourceCreateWithURL(url as CFURL, nil),
           let cgImg = CGImageSourceCreateImageAtIndex(src, 0, nil) {
            let w = max(1, min(256, cgImg.width))
            let h = max(1, min(256, cgImg.height))
            let bpr = w * 4
            var raw = [UInt8](repeating: 255, count: bpr * h)
            if let cs = CGColorSpace(name: CGColorSpace.sRGB),
               let ctx = CGContext(
                   data: &raw,
                   width: w,
                   height: h,
                   bitsPerComponent: 8,
                   bytesPerRow: bpr,
                   space: cs,
                   // Use noneSkipLast so opaque PS2 skins with alpha==0 (e.g. copsport) never premultiply to black!
                   bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
               ) {
                ctx.interpolationQuality = .high
                ctx.draw(cgImg, in: CGRect(x: 0, y: 0, width: w, height: h))
                let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: w, height: h, mipmapped: true)
                desc.usage = [.shaderRead]
                desc.storageMode = .shared
                if let tex = device.makeTexture(descriptor: desc) {
                    tex.replace(region: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0, withBytes: raw, bytesPerRow: bpr)
                    if tex.mipmapLevelCount > 1, let cb = commandQueue.makeCommandBuffer(), let blit = cb.makeBlitCommandEncoder() {
                        blit.generateMipmaps(for: tex)
                        blit.endEncoding()
                        cb.commit()
                        cb.waitUntilCompleted()
                    }
                    let estBytes = (w * h * 4 * 4) / 3
                    return (tex, estBytes)
                }
            }
        }

        // Neutral 4x4 fallback skin texture
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: 4, height: 4, mipmapped: false)
        desc.usage = [.shaderRead]
        desc.storageMode = .shared
        let tex = device.makeTexture(descriptor: desc)!
        let px = [UInt8](repeating: 140, count: 4 * 4 * 4)
        tex.replace(region: MTLRegionMake2D(0, 0, 4, 4), mipmapLevel: 0, withBytes: px, bytesPerRow: 16)
        return (tex, 64)
    }
}
