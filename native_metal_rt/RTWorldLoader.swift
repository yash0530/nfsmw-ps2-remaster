import Foundation
import Metal
import simd

// MARK: - Locked .rkpt v1 Binary Contract (Little-Endian, Metal Y-up Right-Handed Meters)

enum RTTLASMask {
    static let car: UInt32          = 0x01
    static let worldOpaque: UInt32  = 0x02
    static let road: UInt32         = 0x04
    static let wall: UInt32         = 0x08
    static let glass: UInt32        = 0x10
    static let shadowAll: UInt32    = 0x0F // car | worldOpaque | road | wall (skips glass)
    static let primaryAll: UInt32   = 0xFF
}

struct RkptHeader {
    let magic: String
    let version: UInt32
    let flags: UInt32
    let sectionCount: UInt32
    let meshCount: UInt32
    let instanceCount: UInt32
    let materialCount: UInt32
    let textureCount: UInt32
    let totalVertexCount: UInt32
    let totalIndexCount: UInt32
    let worldBoundsMin: SIMD3<Float>
    let worldBoundsMax: SIMD3<Float>
    let spawnPos: SIMD3<Float>
    let spawnYawRad: Float
    let sectionsOffset: UInt64
    let meshesOffset: UInt64
    let instancesOffset: UInt64
    let materialsOffset: UInt64
    let texturesOffset: UInt64
    let verticesOffset: UInt64
    let indicesOffset: UInt64
    let texDataOffset: UInt64
    let ps2Origin: SIMD3<Float>
    let reserved: UInt32

    static let byteSize = 160
}

struct RkptSection {
    let name: String
    let sectionNumber: UInt32
    let instanceStart: UInt32
    let instanceCount: UInt32
    let center: SIMD3<Float>
    let radius: Float
    let aabbMin: SIMD3<Float>
    let aabbMax: SIMD3<Float>
    let reserved: UInt32

    static let byteSize = 64
}

struct RkptMesh {
    let solidHash: UInt32
    let vertexOffset: UInt32
    let vertexCount: UInt32
    let indexOffset: UInt32
    let indexCount: UInt32
    let flags: UInt32 // bit0=HAS_ALPHA_CUTOUT, bit1=DRIVABLE_ROAD, bit2=SOLID_WALL, bit3=TERRAIN_CHOP, bit4=SKY_OR_PANORAMA
    let aabbMin: SIMD3<Float>
    let aabbMax: SIMD3<Float>

    var hasAlphaCutout: Bool { (flags & 1) != 0 }
    var isDrivableRoad: Bool { (flags & 2) != 0 }
    var isSolidWall: Bool    { (flags & 4) != 0 }
    var isTerrainChop: Bool  { (flags & 8) != 0 }
    var isSkyPanorama: Bool  { (flags & 16) != 0 }

    static let byteSize = 48
}

struct RkptInstance {
    let meshIndex: UInt32
    let sectionIndex: UInt32
    let flags: UInt32
    let reserved: UInt32
    let transform4x3: MTLPackedFloat4x3
    let modelMatrix: simd_float4x4

    static let byteSize = 64
}

// 32-byte GPU-ready material struct matching Metal RkptMaterial layout byte-for-byte
struct RkptMaterialGPU {
    var diffuseTexIndex: UInt32
    var surfaceClass: UInt32
    var flags: UInt32
    var roughnessHint: Float
    var metallicHint: Float
    var emissiveHint: Float
    var bumpStrength: Float
    var tpkNameHash: UInt32

    static let byteSize = 32
}

struct RkptTextureRecord {
    let tpkNameHash: UInt32
    let width: UInt32
    let height: UInt32
    let pixelFormat: UInt32 // 0 = RGBA8_UNORM_SRGB
    let dataOffset: UInt64  // relative to header.texDataOffset
    let dataSize: UInt32
    let flags: UInt32       // bit0=HAS_CUTOUT_ALPHA, bit1=PS2_0X80_NORMALIZED, bit2=ENHANCED_2X

    static let byteSize = 32
}

// 36-byte GPU-ready packed vertex struct matching Metal RkptVertex layout byte-for-byte
struct RkptVertexGPU {
    var px: Float
    var py: Float
    var pz: Float
    var nx: Float
    var ny: Float
    var nz: Float
    var u: Float
    var v: Float
    var matIndex: UInt16
    var vertexAO: UInt8
    var flags: UInt8

    static let byteSize = 36
}

struct RTWorldPackage {
    let url: URL
    let header: RkptHeader
    let sections: [RkptSection]
    let meshes: [RkptMesh]
    let instances: [RkptInstance]
    let materials: [RkptMaterialGPU]
    let vertexBuffer: MTLBuffer
    let indexBuffer: MTLBuffer
    let materialBuffer: MTLBuffer
    let blasList: [MTLAccelerationStructure]
    let meshToBlasIndex: [Int]
    let textureHeap: MTLHeap?
    let textures: [MTLTexture]
    let bindlessTexIDBuffer: MTLBuffer
    let residencySet: AnyObject? // MTLResidencySet on macOS 15+
    let totalTriangles: Int

    var spawnPos: SIMD3<Float> { header.spawnPos }
    var spawnYawRad: Float { header.spawnYawRad }
}

private struct BinaryCursor {
    let base: UnsafeRawPointer
    let count: Int

    func readUInt32(_ offset: Int) -> UInt32 {
        var v: UInt32 = 0
        memcpy(&v, base.advanced(by: offset), 4)
        return UInt32(littleEndian: v)
    }

    func readUInt64(_ offset: Int) -> UInt64 {
        var v: UInt64 = 0
        memcpy(&v, base.advanced(by: offset), 8)
        return UInt64(littleEndian: v)
    }

    func readFloat(_ offset: Int) -> Float {
        return Float(bitPattern: readUInt32(offset))
    }

    func readVec3(_ offset: Int) -> SIMD3<Float> {
        return SIMD3<Float>(readFloat(offset), readFloat(offset + 4), readFloat(offset + 8))
    }

    func readASCII(_ offset: Int, length: Int) -> String {
        let ptr = base.advanced(by: offset).assumingMemoryBound(to: UInt8.self)
        var bytes: [UInt8] = []
        bytes.reserveCapacity(length)
        for i in 0..<length {
            let b = ptr[i]
            if b == 0 { break }
            bytes.append(b)
        }
        return String(bytes: bytes, encoding: .ascii) ?? ""
    }
}

enum RTWorldLoader {
    static func assertContractLayout() {
        precondition(MemoryLayout<RkptMaterialGPU>.stride == RkptMaterialGPU.byteSize,
                     "RkptMaterialGPU stride must be 32 bytes")
        precondition(MemoryLayout<RkptVertexGPU>.stride == RkptVertexGPU.byteSize,
                     "RkptVertexGPU stride must be 36 bytes")
    }

    static func load(url: URL, device: MTLDevice, commandQueue: MTLCommandQueue) throws -> RTWorldPackage {
        assertContractLayout()

        let data = try Data(contentsOf: url, options: [.mappedIfSafe])
        guard data.count >= RkptHeader.byteSize else {
            throw NSError(domain: "RTWorldLoader", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "RKPT file too small (\(data.count) bytes): \(url.path)"
            ])
        }

        return try data.withUnsafeBytes { rawBuf in
            guard let base = rawBuf.baseAddress else {
                throw NSError(domain: "RTWorldLoader", code: 2, userInfo: [
                    NSLocalizedDescriptionKey: "Empty RKPT mapping: \(url.path)"
                ])
            }
            let cur = BinaryCursor(base: base, count: data.count)

            let magic = cur.readASCII(0, length: 4)
            guard magic == "RKPT" else {
                throw NSError(domain: "RTWorldLoader", code: 3, userInfo: [
                    NSLocalizedDescriptionKey: "Invalid RKPT magic '\(magic)' in \(url.path)"
                ])
            }
            let version = cur.readUInt32(4)
            guard version == 1 else {
                throw NSError(domain: "RTWorldLoader", code: 4, userInfo: [
                    NSLocalizedDescriptionKey: "Unsupported RKPT version \(version) (expected 1)"
                ])
            }

            let header = RkptHeader(
                magic: magic,
                version: version,
                flags: cur.readUInt32(8),
                sectionCount: cur.readUInt32(12),
                meshCount: cur.readUInt32(16),
                instanceCount: cur.readUInt32(20),
                materialCount: cur.readUInt32(24),
                textureCount: cur.readUInt32(28),
                totalVertexCount: cur.readUInt32(32),
                totalIndexCount: cur.readUInt32(36),
                worldBoundsMin: cur.readVec3(40),
                worldBoundsMax: cur.readVec3(52),
                spawnPos: cur.readVec3(64),
                spawnYawRad: cur.readFloat(76),
                sectionsOffset: cur.readUInt64(80),
                meshesOffset: cur.readUInt64(88),
                instancesOffset: cur.readUInt64(96),
                materialsOffset: cur.readUInt64(104),
                texturesOffset: cur.readUInt64(112),
                verticesOffset: cur.readUInt64(120),
                indicesOffset: cur.readUInt64(128),
                texDataOffset: cur.readUInt64(136),
                ps2Origin: cur.readVec3(144),
                reserved: cur.readUInt32(156)
            )

            func validateRange(_ off: UInt64, _ byteCount: Int, _ label: String) throws {
                let end = Int(off) + byteCount
                if Int(off) < 0 || byteCount < 0 || end > data.count {
                    throw NSError(domain: "RTWorldLoader", code: 5, userInfo: [
                        NSLocalizedDescriptionKey: "RKPT section '\(label)' out of bounds (off=\(off), size=\(byteCount), file=\(data.count))"
                    ])
                }
            }

            // 1. Parse Sections (64 bytes each)
            let secBytes = Int(header.sectionCount) * RkptSection.byteSize
            try validateRange(header.sectionsOffset, secBytes, "sections")
            var sections: [RkptSection] = []
            sections.reserveCapacity(Int(header.sectionCount))
            for i in 0..<Int(header.sectionCount) {
                let o = Int(header.sectionsOffset) + i * RkptSection.byteSize
                sections.append(RkptSection(
                    name: cur.readASCII(o, length: 8),
                    sectionNumber: cur.readUInt32(o + 8),
                    instanceStart: cur.readUInt32(o + 12),
                    instanceCount: cur.readUInt32(o + 16),
                    center: cur.readVec3(o + 20),
                    radius: cur.readFloat(o + 32),
                    aabbMin: cur.readVec3(o + 36),
                    aabbMax: cur.readVec3(o + 48),
                    reserved: cur.readUInt32(o + 60)
                ))
            }

            // 2. Parse Meshes (48 bytes each)
            let meshBytes = Int(header.meshCount) * RkptMesh.byteSize
            try validateRange(header.meshesOffset, meshBytes, "meshes")
            var meshes: [RkptMesh] = []
            meshes.reserveCapacity(Int(header.meshCount))
            for i in 0..<Int(header.meshCount) {
                let o = Int(header.meshesOffset) + i * RkptMesh.byteSize
                meshes.append(RkptMesh(
                    solidHash: cur.readUInt32(o),
                    vertexOffset: cur.readUInt32(o + 4),
                    vertexCount: cur.readUInt32(o + 8),
                    indexOffset: cur.readUInt32(o + 12),
                    indexCount: cur.readUInt32(o + 16),
                    flags: cur.readUInt32(o + 20),
                    aabbMin: cur.readVec3(o + 24),
                    aabbMax: cur.readVec3(o + 36)
                ))
            }

            // 3. Parse Instances (64 bytes each)
            let instBytes = Int(header.instanceCount) * RkptInstance.byteSize
            try validateRange(header.instancesOffset, instBytes, "instances")
            var instances: [RkptInstance] = []
            instances.reserveCapacity(Int(header.instanceCount))
            for i in 0..<Int(header.instanceCount) {
                let o = Int(header.instancesOffset) + i * RkptInstance.byteSize
                let c0 = cur.readVec3(o + 16)
                let c1 = cur.readVec3(o + 28)
                let c2 = cur.readVec3(o + 40)
                let c3 = cur.readVec3(o + 52)
                let packed = MTLPackedFloat4x3(columns: (
                    MTLPackedFloat3Make(c0.x, c0.y, c0.z),
                    MTLPackedFloat3Make(c1.x, c1.y, c1.z),
                    MTLPackedFloat3Make(c2.x, c2.y, c2.z),
                    MTLPackedFloat3Make(c3.x, c3.y, c3.z)
                ))
                let mat4 = simd_float4x4(
                    SIMD4<Float>(c0.x, c0.y, c0.z, 0),
                    SIMD4<Float>(c1.x, c1.y, c1.z, 0),
                    SIMD4<Float>(c2.x, c2.y, c2.z, 0),
                    SIMD4<Float>(c3.x, c3.y, c3.z, 1)
                )
                instances.append(RkptInstance(
                    meshIndex: cur.readUInt32(o),
                    sectionIndex: cur.readUInt32(o + 4),
                    flags: cur.readUInt32(o + 8),
                    reserved: cur.readUInt32(o + 12),
                    transform4x3: packed,
                    modelMatrix: mat4
                ))
            }

            // 4. Parse Materials (32 bytes each)
            let matBytes = Int(header.materialCount) * RkptMaterialGPU.byteSize
            try validateRange(header.materialsOffset, matBytes, "materials")
            var materials: [RkptMaterialGPU] = []
            materials.reserveCapacity(max(1, Int(header.materialCount)))
            for i in 0..<Int(header.materialCount) {
                let o = Int(header.materialsOffset) + i * RkptMaterialGPU.byteSize
                materials.append(RkptMaterialGPU(
                    diffuseTexIndex: cur.readUInt32(o),
                    surfaceClass: cur.readUInt32(o + 4),
                    flags: cur.readUInt32(o + 8),
                    roughnessHint: cur.readFloat(o + 12),
                    metallicHint: cur.readFloat(o + 16),
                    emissiveHint: cur.readFloat(o + 20),
                    bumpStrength: cur.readFloat(o + 24),
                    tpkNameHash: cur.readUInt32(o + 28)
                ))
            }
            if materials.isEmpty {
                materials.append(RkptMaterialGPU(
                    diffuseTexIndex: 0xFFFFFFFF,
                    surfaceClass: 0,
                    flags: 0,
                    roughnessHint: 0.72,
                    metallicHint: 0.0,
                    emissiveHint: 0.0,
                    bumpStrength: 0.3,
                    tpkNameHash: 0
                ))
            }

            // 5. Parse Texture Records (32 bytes each)
            let texRecBytes = Int(header.textureCount) * RkptTextureRecord.byteSize
            try validateRange(header.texturesOffset, texRecBytes, "textures")
            var texRecords: [RkptTextureRecord] = []
            texRecords.reserveCapacity(Int(header.textureCount))
            for i in 0..<Int(header.textureCount) {
                let o = Int(header.texturesOffset) + i * RkptTextureRecord.byteSize
                texRecords.append(RkptTextureRecord(
                    tpkNameHash: cur.readUInt32(o),
                    width: cur.readUInt32(o + 4),
                    height: cur.readUInt32(o + 8),
                    pixelFormat: cur.readUInt32(o + 12),
                    dataOffset: cur.readUInt64(o + 16),
                    dataSize: cur.readUInt32(o + 24),
                    flags: cur.readUInt32(o + 28)
                ))
            }

            // 6. Upload Packed Vertices (36 bytes each), Indices (4 bytes each), Materials (32 bytes each)
            let vBytes = Int(header.totalVertexCount) * RkptVertexGPU.byteSize
            let iBytes = Int(header.totalIndexCount) * MemoryLayout<UInt32>.stride
            try validateRange(header.verticesOffset, vBytes, "vertices")
            try validateRange(header.indicesOffset, iBytes, "indices")

            let vertBufLen = max(vBytes, RkptVertexGPU.byteSize)
            let idxBufLen = max(iBytes, 12)
            guard let vertexBuffer = device.makeBuffer(length: vertBufLen, options: .storageModeShared),
                  let indexBuffer = device.makeBuffer(length: idxBufLen, options: .storageModeShared),
                  let materialBuffer = device.makeBuffer(bytes: materials,
                                                         length: materials.count * RkptMaterialGPU.byteSize,
                                                         options: .storageModeShared) else {
                throw NSError(domain: "RTWorldLoader", code: 6, userInfo: [
                    NSLocalizedDescriptionKey: "Failed to allocate RKPT geometry/material Metal buffers"
                ])
            }
            if vBytes > 0 {
                memcpy(vertexBuffer.contents(), base.advanced(by: Int(header.verticesOffset)), vBytes)
            }
            if iBytes > 0 {
                memcpy(indexBuffer.contents(), base.advanced(by: Int(header.indicesOffset)), iBytes)
            }

            // 7. Pack All World Textures into a Single Private Mipmapped MTLHeap + Bindless MTLResourceID Table
            let (texHeap, textures, bindlessBuf, residencySet) = try buildBindlessTextureHeap(
                device: device,
                commandQueue: commandQueue,
                records: texRecords,
                base: base,
                fileSize: data.count,
                texDataOffset: header.texDataOffset
            )

            // 8. Build Deduplicated Per-Mesh Hardware RT BLASes
            let (blasList, meshToBlasIndex) = try buildWorldBLASes(
                device: device,
                commandQueue: commandQueue,
                meshes: meshes,
                vertexBuffer: vertexBuffer,
                indexBuffer: indexBuffer
            )

            var totalWorldTris = 0
            for inst in instances {
                let mIdx = Int(inst.meshIndex)
                if mIdx >= 0 && mIdx < meshes.count {
                    totalWorldTris += Int(meshes[mIdx].indexCount) / 3
                }
            }

            return RTWorldPackage(
                url: url,
                header: header,
                sections: sections,
                meshes: meshes,
                instances: instances,
                materials: materials,
                vertexBuffer: vertexBuffer,
                indexBuffer: indexBuffer,
                materialBuffer: materialBuffer,
                blasList: blasList,
                meshToBlasIndex: meshToBlasIndex,
                textureHeap: texHeap,
                textures: textures,
                bindlessTexIDBuffer: bindlessBuf,
                residencySet: residencySet,
                totalTriangles: totalWorldTris
            )
        }
    }

    private static func buildBindlessTextureHeap(
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        records: [RkptTextureRecord],
        base: UnsafeRawPointer,
        fileSize: Int,
        texDataOffset: UInt64
    ) throws -> (MTLHeap?, [MTLTexture], MTLBuffer, AnyObject?) {
        // Always include at least 1 valid fallback texture so the bindless table is non-empty.
        if records.isEmpty {
            let td = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: 1, height: 1, mipmapped: false)
            td.usage = [.shaderRead]
            td.storageMode = .shared
            let fallback = device.makeTexture(descriptor: td)!
            var white: [UInt8] = [180, 180, 180, 255]
            fallback.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: &white, bytesPerRow: 4)
            var rid = fallback.gpuResourceID
            let buf = device.makeBuffer(bytes: &rid, length: MemoryLayout<MTLResourceID>.stride, options: .storageModeShared)!
            return (nil, [fallback], buf, nil)
        }

        // Compute total private heap size with alignment padding
        var descriptors: [MTLTextureDescriptor] = []
        descriptors.reserveCapacity(records.count)
        var totalHeapBytes = 0
        var maxStageBytes = 0

        for rec in records {
            let w = max(1, Int(rec.width))
            let h = max(1, Int(rec.height))
            let canMip = (w > 1 || h > 1)
            let td = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: .rgba8Unorm_srgb,
                width: w,
                height: h,
                mipmapped: canMip
            )
            td.usage = [.shaderRead]
            td.storageMode = .private
            let sa = device.heapTextureSizeAndAlign(descriptor: td)
            let aligned = ((sa.size + sa.align - 1) / sa.align) * sa.align
            totalHeapBytes += aligned
            maxStageBytes = max(maxStageBytes, w * h * 4)
            descriptors.append(td)
        }

        let heapDesc = MTLHeapDescriptor()
        heapDesc.size = max(totalHeapBytes + 4 * 1024 * 1024, 1024 * 1024)
        heapDesc.storageMode = .private
        heapDesc.type = .automatic
        guard let heap = device.makeHeap(descriptor: heapDesc) else {
            throw NSError(domain: "RTWorldLoader", code: 10, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate RKPT bindless MTLHeap (\(heapDesc.size) bytes)"
            ])
        }

        // Upload textures in batched blit command buffers (keeping staging memory bounded <= 32 MB)
        let batchCapBytes = max(maxStageBytes, 16 * 1024 * 1024)
        guard let stagingBuf = device.makeBuffer(length: batchCapBytes, options: .storageModeShared),
              let idBuf = device.makeBuffer(length: records.count * MemoryLayout<MTLResourceID>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTWorldLoader", code: 11, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate RKPT texture staging or bindless ID buffer"
            ])
        }
        let idPtr = idBuf.contents().bindMemory(to: MTLResourceID.self, capacity: records.count)

        var textures: [MTLTexture] = []
        textures.reserveCapacity(records.count)

        var stageOffset = 0
        var activeCB = commandQueue.makeCommandBuffer()
        var activeBlit = activeCB?.makeBlitCommandEncoder()

        func flushBlitBatch() throws {
            activeBlit?.endEncoding()
            if let cb = activeCB {
                cb.commit()
                cb.waitUntilCompleted()
                if let err = cb.error { throw err }
            }
            stageOffset = 0
            activeCB = commandQueue.makeCommandBuffer()
            activeBlit = activeCB?.makeBlitCommandEncoder()
        }

        for (i, rec) in records.enumerated() {
            let td = descriptors[i]
            let tex: MTLTexture
            if let heapTex = heap.makeTexture(descriptor: td) {
                tex = heapTex
            } else {
                // Rare overflow guard: fall back to device-allocated private texture
                guard let devTex = device.makeTexture(descriptor: td) else {
                    throw NSError(domain: "RTWorldLoader", code: 12, userInfo: [
                        NSLocalizedDescriptionKey: "Failed to allocate RKPT texture #\(i) (\(rec.width)x\(rec.height))"
                    ])
                }
                tex = devTex
            }

            let w = max(1, Int(rec.width))
            let h = max(1, Int(rec.height))
            let expectedBytes = w * h * 4
            let alignedBytes = (expectedBytes + 255) & ~255
            if stageOffset + alignedBytes > batchCapBytes {
                try flushBlitBatch()
            }

            let srcOff = Int(texDataOffset) + Int(rec.dataOffset)
            let dstPtr = stagingBuf.contents().advanced(by: stageOffset)
            if srcOff >= 0 && srcOff + expectedBytes <= fileSize {
                memcpy(dstPtr, base.advanced(by: srcOff), expectedBytes)
            } else {
                memset(dstPtr, 160, expectedBytes)
            }

            activeBlit?.copy(
                from: stagingBuf,
                sourceOffset: stageOffset,
                sourceBytesPerRow: w * 4,
                sourceBytesPerImage: expectedBytes,
                sourceSize: MTLSize(width: w, height: h, depth: 1),
                to: tex,
                destinationSlice: 0,
                destinationLevel: 0,
                destinationOrigin: MTLOrigin(x: 0, y: 0, z: 0)
            )
            if tex.mipmapLevelCount > 1 {
                activeBlit?.generateMipmaps(for: tex)
            }

            stageOffset += alignedBytes
            idPtr[i] = tex.gpuResourceID
            textures.append(tex)
        }

        activeBlit?.endEncoding()
        if let cb = activeCB {
            cb.commit()
            cb.waitUntilCompleted()
            if let err = cb.error { throw err }
        }

        var residencyObj: AnyObject? = nil
        if #available(macOS 15.0, *) {
            let rDesc = MTLResidencySetDescriptor()
            rDesc.label = "RockportBindlessWorldHeap"
            rDesc.initialCapacity = 4
            if let rSet = try? device.makeResidencySet(descriptor: rDesc) {
                rSet.addAllocation(heap)
                for t in textures where t.heap == nil {
                    rSet.addAllocation(t)
                }
                rSet.commit()
                commandQueue.addResidencySet(rSet)
                residencyObj = rSet
            }
        }

        return (heap, textures, idBuf, residencyObj)
    }

    private static func buildWorldBLASes(
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        meshes: [RkptMesh],
        vertexBuffer: MTLBuffer,
        indexBuffer: MTLBuffer
    ) throws -> ([MTLAccelerationStructure], [Int]) {
        var blasList: [MTLAccelerationStructure] = []
        blasList.reserveCapacity(meshes.count)
        var meshToBlas = [Int](repeating: -1, count: meshes.count)

        // Build in batches of 128 BLASes per command buffer to keep scratch memory lean
        let batchSize = 128
        var startIdx = 0
        while startIdx < meshes.count {
            let endIdx = min(meshes.count, startIdx + batchSize)
            guard let cmdBuf = commandQueue.makeCommandBuffer(),
                  let accelEnc = cmdBuf.makeAccelerationStructureCommandEncoder() else {
                throw NSError(domain: "RTWorldLoader", code: 20, userInfo: [
                    NSLocalizedDescriptionKey: "Failed to create BLAS encoder for RKPT world"
                ])
            }
            var scratchRetention: [MTLBuffer] = []
            scratchRetention.reserveCapacity(endIdx - startIdx)

            for mIdx in startIdx..<endIdx {
                let m = meshes[mIdx]
                let triCount = Int(m.indexCount) / 3
                if triCount <= 0 || m.vertexCount == 0 { continue }

                let geomDesc = MTLAccelerationStructureTriangleGeometryDescriptor()
                geomDesc.vertexBuffer = vertexBuffer
                geomDesc.vertexBufferOffset = Int(m.vertexOffset) * RkptVertexGPU.byteSize
                geomDesc.vertexStride = RkptVertexGPU.byteSize
                geomDesc.vertexFormat = .float3
                geomDesc.indexBuffer = indexBuffer
                geomDesc.indexBufferOffset = Int(m.indexOffset) * MemoryLayout<UInt32>.stride
                geomDesc.indexType = .uint32
                geomDesc.triangleCount = triCount
                // Opaque meshes bypass inline any-hit completely; alpha-cutout trees/fences invoke intersection_query!
                geomDesc.opaque = !m.hasAlphaCutout

                let blasDesc = MTLPrimitiveAccelerationStructureDescriptor()
                blasDesc.geometryDescriptors = [geomDesc]

                let sizes = device.accelerationStructureSizes(descriptor: blasDesc)
                guard let blas = device.makeAccelerationStructure(size: sizes.accelerationStructureSize),
                      let scratch = device.makeBuffer(length: max(sizes.buildScratchBufferSize, 256), options: .storageModePrivate) else {
                    throw NSError(domain: "RTWorldLoader", code: 21, userInfo: [
                        NSLocalizedDescriptionKey: "Failed to allocate RKPT BLAS for mesh #\(mIdx)"
                    ])
                }
                accelEnc.build(accelerationStructure: blas, descriptor: blasDesc, scratchBuffer: scratch, scratchBufferOffset: 0)
                scratchRetention.append(scratch)
                meshToBlas[mIdx] = blasList.count
                blasList.append(blas)
            }

            accelEnc.endEncoding()
            cmdBuf.commit()
            cmdBuf.waitUntilCompleted()
            if let err = cmdBuf.error { throw err }
            startIdx = endIdx
        }

        return (blasList, meshToBlas)
    }

    static func makeDummyWorldBuffers(device: MTLDevice) -> (MTLBuffer, MTLBuffer, MTLBuffer, MTLBuffer, MTLTexture) {
        var v = RkptVertexGPU(px: 0, py: 0, pz: 0, nx: 0, ny: 1, nz: 0, u: 0, v: 0, matIndex: 0, vertexAO: 255, flags: 0)
        var idx: [UInt32] = [0, 0, 0]
        var mat = RkptMaterialGPU(diffuseTexIndex: 0, surfaceClass: 0, flags: 0, roughnessHint: 0.7, metallicHint: 0.0, emissiveHint: 0.0, bumpStrength: 0.2, tpkNameHash: 0)
        let td = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: 1, height: 1, mipmapped: false)
        td.usage = [.shaderRead]
        td.storageMode = .shared
        let tex = device.makeTexture(descriptor: td)!
        var px: [UInt8] = [160, 160, 160, 255]
        tex.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: &px, bytesPerRow: 4)
        var rid = tex.gpuResourceID

        let vb = device.makeBuffer(bytes: &v, length: RkptVertexGPU.byteSize, options: .storageModeShared)!
        let ib = device.makeBuffer(bytes: &idx, length: 12, options: .storageModeShared)!
        let mb = device.makeBuffer(bytes: &mat, length: RkptMaterialGPU.byteSize, options: .storageModeShared)!
        let tb = device.makeBuffer(bytes: &rid, length: MemoryLayout<MTLResourceID>.stride, options: .storageModeShared)!
        return (vb, ib, mb, tb, tex)
    }
}
