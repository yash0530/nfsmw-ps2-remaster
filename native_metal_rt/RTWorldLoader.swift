import Foundation
import Metal
import simd

// MARK: - Locked .rkpt v1 & v2 Binary Contract (Little-Endian, Metal Y-up Right-Handed Meters)

enum RTTLASMask {
    static let car: UInt32          = 0x01
    static let worldOpaque: UInt32  = 0x02
    static let road: UInt32         = 0x04
    static let wall: UInt32         = 0x08
    static let glass: UInt32        = 0x10
    static let shadowAll: UInt32    = 0x0F // car | worldOpaque | road | wall (skips glass)
    static let primaryAll: UInt32   = 0xFF
}

enum RkptHeaderFlags {
    static let hasV2Ext: UInt32     = 1 << 0
    static let bc7Textures: UInt32  = 1 << 1
    static let hasPanorama: UInt32  = 1 << 2
    static let hasPVS: UInt32       = 1 << 3
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

    var hasV2Ext: Bool    { version >= 2 && (flags & RkptHeaderFlags.hasV2Ext) != 0 }
    var hasBC7: Bool      { (flags & RkptHeaderFlags.bc7Textures) != 0 }
    var hasPanorama: Bool { (flags & RkptHeaderFlags.hasPanorama) != 0 }
    var hasPVS: Bool      { version >= 2 && (flags & RkptHeaderFlags.hasPVS) != 0 }

    static let byteSize = 160
}

// 96-byte v2 extension header immediately following 160-byte RkptHeader when version >= 2 && hasV2Ext
struct RkptV2ExtHeader {
    let roadNodeCount: UInt32
    let roadSegmentCount: UInt32
    let worldLightCount: UInt32
    let landmarkCount: UInt32
    let spatialSectionCount: UInt32
    let panoramaSectionCount: UInt32
    let pvsZoneCount: UInt32       // reserved0 when HAS_PVS == 0
    let pvsEntryCount: UInt32      // reserved1 when HAS_PVS == 0
    let roadNodesOffset: UInt64
    let roadSegmentsOffset: UInt64
    let worldLightsOffset: UInt64
    let landmarksOffset: UInt64
    let pvsZonesOffset: UInt64     // reservedOff0 when HAS_PVS == 0
    let pvsVertsOffset: UInt64     // reservedOff1 when HAS_PVS == 0
    let pvsEntriesOffset: UInt64   // reservedOff2 when HAS_PVS == 0
    let pvsVertCount: UInt64       // reservedOff3 when HAS_PVS == 0

    static let byteSize = 96
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
    let reserved: UInt32 // bit0 = SEC_FLAG_PANORAMA

    var isPanorama: Bool { (reserved & 1) != 0 }

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
    let pixelFormat: UInt32 // 0 = RGBA8_UNORM_SRGB, 1 = BC7_RGBA_UNORM_SRGB
    let dataOffset: UInt64  // relative to header.texDataOffset
    let dataSize: UInt32
    let flags: UInt32       // bits0..7=flags, bits8..15=mipCount

    var isBC7: Bool { pixelFormat == 1 }
    var mipCount: Int {
        let m = Int((flags >> 8) & 0xFF)
        return max(1, m)
    }

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

// 32-byte road network node decoded from EA L2RA.BUN 0x0003B800 (CARP/RNnd)
struct RkptRoadNode {
    let pos: SIMD3<Float>
    let nodeIndex: UInt16
    let flags: UInt16
    let degree: UInt8
    let adjSegments: (UInt16, UInt16, UInt16, UInt16, UInt16, UInt16, UInt16)

    var adjacentSegmentIndices: [Int] {
        let raw = [adjSegments.0, adjSegments.1, adjSegments.2, adjSegments.3, adjSegments.4, adjSegments.5, adjSegments.6]
        let lim = min(Int(degree), 7)
        var out: [Int] = []
        out.reserveCapacity(lim)
        for i in 0..<lim where raw[i] != 0xFFFF {
            out.append(Int(raw[i]))
        }
        return out
    }

    static let byteSize = 32
}

// 16-byte road network segment decoded from EA L2RA.BUN 0x0003B800 (CARP/RNsg)
struct RkptRoadSegment {
    let node0: UInt16
    let node1: UInt16
    let laneFlags: UInt8
    let dirFlags: UInt8
    let roadId: UInt16
    let lengthMeters: Float
    let halfWidthMeters: Float

    var isHighway: Bool { laneFlags >= 16 }

    static let byteSize = 16
}

// 32-byte GPU-ready world light matching Metal RTWorldLight / RTWorldLightGPU byte-for-byte
struct RTWorldLight {
    var posRadius: SIMD4<Float> // xyz = Metal Y-up luminaire bulb pos, w = effective radius (m)
    var colorCone: SIMD4<Float> // rgb = pre-scaled linear radiance, w = coneMinCos (>0 downward lamp, <= -0.9 omni)

    var isDownwardStreetlamp: Bool { colorCone.w > 0.0 }

    static let byteSize = 32
}

// 64-byte verified road spawn landmark
struct RkptLandmark {
    let name: String
    let spawnPos: SIMD3<Float>
    let spawnYawRad: Float
    let regionId: UInt32
    let flags: UInt32

    static let byteSize = 64
}

// 32-byte EA PVS visibility zone decoded from L2RA.BUN 0x80034150 (0x00034152 + 0x00034153)
struct RkptPvsZone {
    let minX: Float
    let minZ: Float
    let maxX: Float
    let maxZ: Float
    let firstVert: UInt32
    let vertCount: UInt32
    let firstSecEntry: UInt32
    let secEntryCount: UInt32

    static let byteSize = 32
}

struct RTWorldPackage {
    let url: URL
    let header: RkptHeader
    let extHeader: RkptV2ExtHeader?
    let sections: [RkptSection]
    let meshes: [RkptMesh]
    let instances: [RkptInstance]
    let materials: [RkptMaterialGPU]
    let vertexBuffer: MTLBuffer
    let indexBuffer: MTLBuffer
    let materialBuffer: MTLBuffer
    let blasHeap: MTLHeap?
    let blasList: [MTLAccelerationStructure]
    let meshToBlasIndex: [Int]
    let textureHeap: MTLHeap?
    let textures: [MTLTexture]
    let bindlessTexIDBuffer: MTLBuffer
    let residencySet: AnyObject?     // MTLResidencySet on macOS 15+ (textures + BLASes)
    let blasResidencySet: AnyObject? // Alias/dedicated residency set when BLASes are GPU-resident
    let hasResidentBLASes: Bool
    let totalTriangles: Int
    let roadNodes: [RkptRoadNode]
    let roadSegments: [RkptRoadSegment]
    let worldLights: [RTWorldLight]
    let landmarks: [RkptLandmark]
    let pvsZones: [RkptPvsZone]
    let pvsVerts: [SIMD2<Float>]
    let pvsEntries: [UInt16]
    let spatialSectionCount: Int
    let panoramaSectionCount: Int
    let estimatedVRAMBytes: Int

    var hasPVS: Bool { header.hasPVS && !pvsZones.isEmpty }
    var spawnPos: SIMD3<Float> { header.spawnPos }
    var spawnYawRad: Float { header.spawnYawRad }
}

private struct BinaryCursor {
    let base: UnsafeRawPointer
    let count: Int

    func readUInt8(_ offset: Int) -> UInt8 {
        return base.load(fromByteOffset: offset, as: UInt8.self)
    }

    func readUInt16(_ offset: Int) -> UInt16 {
        var v: UInt16 = 0
        memcpy(&v, base.advanced(by: offset), 2)
        return UInt16(littleEndian: v)
    }

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

    func readVec4(_ offset: Int) -> SIMD4<Float> {
        return SIMD4<Float>(readFloat(offset), readFloat(offset + 4), readFloat(offset + 8), readFloat(offset + 12))
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
        precondition(MemoryLayout<RTWorldLight>.stride == RTWorldLight.byteSize,
                     "RTWorldLight stride must be 32 bytes")
    }

    static func expectedBC7MipBytes(width: Int, height: Int, mipCount: Int) -> Int {
        var cw = max(1, width)
        var ch = max(1, height)
        var total = 0
        for _ in 0..<max(1, mipCount) {
            total += ((cw + 3) / 4) * ((ch + 3) / 4) * 16
            cw = max(1, cw >> 1)
            ch = max(1, ch >> 1)
        }
        return total
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
            guard version == 1 || version == 2 else {
                throw NSError(domain: "RTWorldLoader", code: 4, userInfo: [
                    NSLocalizedDescriptionKey: "Unsupported RKPT version \(version) (expected 1 or 2)"
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
                if byteCount == 0 { return }
                let end = Int(off) + byteCount
                if Int(off) < 0 || byteCount < 0 || end > data.count {
                    throw NSError(domain: "RTWorldLoader", code: 5, userInfo: [
                        NSLocalizedDescriptionKey: "RKPT section '\(label)' out of bounds (off=\(off), size=\(byteCount), file=\(data.count))"
                    ])
                }
            }

            // Optional 96-byte v2 Extension Header (read ONLY when version >= 2 && (flags & 1) != 0)
            var extHeader: RkptV2ExtHeader? = nil
            if header.hasV2Ext {
                try validateRange(UInt64(RkptHeader.byteSize), RkptV2ExtHeader.byteSize, "v2ExtHeader")
                let e = RkptHeader.byteSize
                extHeader = RkptV2ExtHeader(
                    roadNodeCount: cur.readUInt32(e + 0),
                    roadSegmentCount: cur.readUInt32(e + 4),
                    worldLightCount: cur.readUInt32(e + 8),
                    landmarkCount: cur.readUInt32(e + 12),
                    spatialSectionCount: cur.readUInt32(e + 16),
                    panoramaSectionCount: cur.readUInt32(e + 20),
                    pvsZoneCount: cur.readUInt32(e + 24),
                    pvsEntryCount: cur.readUInt32(e + 28),
                    roadNodesOffset: cur.readUInt64(e + 32),
                    roadSegmentsOffset: cur.readUInt64(e + 40),
                    worldLightsOffset: cur.readUInt64(e + 48),
                    landmarksOffset: cur.readUInt64(e + 56),
                    pvsZonesOffset: cur.readUInt64(e + 64),
                    pvsVertsOffset: cur.readUInt64(e + 72),
                    pvsEntriesOffset: cur.readUInt64(e + 80),
                    pvsVertCount: cur.readUInt64(e + 88)
                )
            }

            // 1. Parse Sections (64 bytes each)
            let secBytes = Int(header.sectionCount) * RkptSection.byteSize
            try validateRange(header.sectionsOffset, secBytes, "sections")
            var sections: [RkptSection] = []
            sections.reserveCapacity(Int(header.sectionCount))
            var panCount = 0
            for i in 0..<Int(header.sectionCount) {
                let o = Int(header.sectionsOffset) + i * RkptSection.byteSize
                let sec = RkptSection(
                    name: cur.readASCII(o, length: 8),
                    sectionNumber: cur.readUInt32(o + 8),
                    instanceStart: cur.readUInt32(o + 12),
                    instanceCount: cur.readUInt32(o + 16),
                    center: cur.readVec3(o + 20),
                    radius: cur.readFloat(o + 32),
                    aabbMin: cur.readVec3(o + 36),
                    aabbMax: cur.readVec3(o + 48),
                    reserved: cur.readUInt32(o + 60)
                )
                if sec.isPanorama { panCount += 1 }
                sections.append(sec)
            }
            let spatialCount = extHeader.map { Int($0.spatialSectionCount) } ?? (sections.count - panCount)
            let panoramaCount = extHeader.map { Int($0.panoramaSectionCount) } ?? panCount

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

            // 6. Upload Packed Vertices (36B), Indices (4B), Materials (32B)
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

            // 7. Pack All World Textures (RGBA8 v1 or BC7+mips v2) into Single Private MTLHeap + Bindless IDs
            let (texHeap, textures, bindlessBuf, texAllocBytes) = try buildBindlessTextureHeap(
                device: device,
                commandQueue: commandQueue,
                records: texRecords,
                base: base,
                fileSize: data.count,
                texDataOffset: header.texDataOffset
            )

            // 8. Build Deduplicated Per-Mesh Hardware RT BLASes in a Single Private MTLHeap
            let (blasHeap, blasList, meshToBlasIndex, blasAllocBytes) = try buildWorldBLASes(
                device: device,
                commandQueue: commandQueue,
                meshes: meshes,
                vertexBuffer: vertexBuffer,
                indexBuffer: indexBuffer
            )

            // 9. Register Texture Heap + BLAS Heap/List into Unified MTLResidencySet (Zero per-frame useResources tax!)
            var residencyObj: AnyObject? = nil
            var hasResidentBLASes = false
            if #available(macOS 15.0, *) {
                let extraTex = textures.filter { $0.heap == nil }
                let extraBLAS = blasList.filter { $0.heap == nil }
                let rDesc = MTLResidencySetDescriptor()
                rDesc.label = "RockportWorldResidencySet"
                rDesc.initialCapacity = 4 + extraTex.count + extraBLAS.count
                if let rSet = try? device.makeResidencySet(descriptor: rDesc) {
                    if let th = texHeap { rSet.addAllocation(th) }
                    for t in extraTex { rSet.addAllocation(t) }
                    if let bh = blasHeap { rSet.addAllocation(bh) }
                    for b in extraBLAS { rSet.addAllocation(b) }
                    rSet.commit()
                    commandQueue.addResidencySet(rSet)
                    residencyObj = rSet
                    hasResidentBLASes = true
                }
            }

            // 10. Parse Optional .rkpt v2 Trailing Tables (RoadNodes, RoadSegments, WorldLights, Landmarks, PVS)
            var roadNodes: [RkptRoadNode] = []
            var roadSegments: [RkptRoadSegment] = []
            var worldLights: [RTWorldLight] = []
            var landmarks: [RkptLandmark] = []
            var pvsZones: [RkptPvsZone] = []
            var pvsVerts: [SIMD2<Float>] = []
            var pvsEntries: [UInt16] = []

            if let ext = extHeader {
                if ext.roadNodeCount > 0 && ext.roadNodesOffset != 0 {
                    let rnBytes = Int(ext.roadNodeCount) * RkptRoadNode.byteSize
                    try validateRange(ext.roadNodesOffset, rnBytes, "roadNodes")
                    roadNodes.reserveCapacity(Int(ext.roadNodeCount))
                    for i in 0..<Int(ext.roadNodeCount) {
                        let o = Int(ext.roadNodesOffset) + i * RkptRoadNode.byteSize
                        roadNodes.append(RkptRoadNode(
                            pos: cur.readVec3(o),
                            nodeIndex: cur.readUInt16(o + 12),
                            flags: cur.readUInt16(o + 14),
                            degree: cur.readUInt8(o + 16),
                            adjSegments: (
                                cur.readUInt16(o + 18),
                                cur.readUInt16(o + 20),
                                cur.readUInt16(o + 22),
                                cur.readUInt16(o + 24),
                                cur.readUInt16(o + 26),
                                cur.readUInt16(o + 28),
                                cur.readUInt16(o + 30)
                            )
                        ))
                    }
                }

                if ext.roadSegmentCount > 0 && ext.roadSegmentsOffset != 0 {
                    let rsBytes = Int(ext.roadSegmentCount) * RkptRoadSegment.byteSize
                    try validateRange(ext.roadSegmentsOffset, rsBytes, "roadSegments")
                    roadSegments.reserveCapacity(Int(ext.roadSegmentCount))
                    for i in 0..<Int(ext.roadSegmentCount) {
                        let o = Int(ext.roadSegmentsOffset) + i * RkptRoadSegment.byteSize
                        roadSegments.append(RkptRoadSegment(
                            node0: cur.readUInt16(o),
                            node1: cur.readUInt16(o + 2),
                            laneFlags: cur.readUInt8(o + 4),
                            dirFlags: cur.readUInt8(o + 5),
                            roadId: cur.readUInt16(o + 6),
                            lengthMeters: cur.readFloat(o + 8),
                            halfWidthMeters: cur.readFloat(o + 12)
                        ))
                    }
                }

                if ext.worldLightCount > 0 && ext.worldLightsOffset != 0 {
                    let wlBytes = Int(ext.worldLightCount) * RTWorldLight.byteSize
                    try validateRange(ext.worldLightsOffset, wlBytes, "worldLights")
                    worldLights.reserveCapacity(Int(ext.worldLightCount))
                    for i in 0..<Int(ext.worldLightCount) {
                        let o = Int(ext.worldLightsOffset) + i * RTWorldLight.byteSize
                        worldLights.append(RTWorldLight(
                            posRadius: cur.readVec4(o),
                            colorCone: cur.readVec4(o + 16)
                        ))
                    }
                }

                if ext.landmarkCount > 0 && ext.landmarksOffset != 0 {
                    let lmBytes = Int(ext.landmarkCount) * RkptLandmark.byteSize
                    try validateRange(ext.landmarksOffset, lmBytes, "landmarks")
                    landmarks.reserveCapacity(Int(ext.landmarkCount))
                    for i in 0..<Int(ext.landmarkCount) {
                        let o = Int(ext.landmarksOffset) + i * RkptLandmark.byteSize
                        landmarks.append(RkptLandmark(
                            name: cur.readASCII(o, length: 32),
                            spawnPos: cur.readVec3(o + 32),
                            spawnYawRad: cur.readFloat(o + 44),
                            regionId: cur.readUInt32(o + 48),
                            flags: cur.readUInt32(o + 52)
                        ))
                    }
                }

                // Optional EA PVS tables (parsed ONLY when version >= 2 && (flags & (1 << 3)) != 0)
                if header.hasPVS && ext.pvsZoneCount > 0 && ext.pvsZonesOffset != 0 {
                    let pzBytes = Int(ext.pvsZoneCount) * RkptPvsZone.byteSize
                    try validateRange(ext.pvsZonesOffset, pzBytes, "pvsZones")
                    pvsZones.reserveCapacity(Int(ext.pvsZoneCount))
                    for i in 0..<Int(ext.pvsZoneCount) {
                        let o = Int(ext.pvsZonesOffset) + i * RkptPvsZone.byteSize
                        pvsZones.append(RkptPvsZone(
                            minX: cur.readFloat(o),
                            minZ: cur.readFloat(o + 4),
                            maxX: cur.readFloat(o + 8),
                            maxZ: cur.readFloat(o + 12),
                            firstVert: cur.readUInt32(o + 16),
                            vertCount: cur.readUInt32(o + 20),
                            firstSecEntry: cur.readUInt32(o + 24),
                            secEntryCount: cur.readUInt32(o + 28)
                        ))
                    }

                    if ext.pvsVertCount > 0 && ext.pvsVertsOffset != 0 {
                        let pvCount = Int(ext.pvsVertCount)
                        try validateRange(ext.pvsVertsOffset, pvCount * 8, "pvsVerts")
                        pvsVerts.reserveCapacity(pvCount)
                        for i in 0..<pvCount {
                            let o = Int(ext.pvsVertsOffset) + i * 8
                            pvsVerts.append(SIMD2<Float>(cur.readFloat(o), cur.readFloat(o + 4)))
                        }
                    }

                    if ext.pvsEntryCount > 0 && ext.pvsEntriesOffset != 0 {
                        let peCount = Int(ext.pvsEntryCount)
                        try validateRange(ext.pvsEntriesOffset, peCount * 2, "pvsEntries")
                        pvsEntries.reserveCapacity(peCount)
                        for i in 0..<peCount {
                            let o = Int(ext.pvsEntriesOffset) + i * 2
                            pvsEntries.append(cur.readUInt16(o))
                        }
                    }

                    // Validate every zone's vertex and section-entry spans stay strictly within bounds
                    for (zIdx, z) in pvsZones.enumerated() {
                        let vEnd = Int(z.firstVert) + Int(z.vertCount)
                        let eEnd = Int(z.firstSecEntry) + Int(z.secEntryCount)
                        if vEnd > pvsVerts.count || eEnd > pvsEntries.count {
                            throw NSError(domain: "RTWorldLoader", code: 15, userInfo: [
                                NSLocalizedDescriptionKey: "RKPT PVS zone #\(zIdx) span out of bounds (vEnd=\(vEnd)/\(pvsVerts.count), eEnd=\(eEnd)/\(pvsEntries.count))"
                            ])
                        }
                    }
                }
            }

            var totalWorldTris = 0
            for inst in instances {
                let mIdx = Int(inst.meshIndex)
                if mIdx >= 0 && mIdx < meshes.count {
                    totalWorldTris += Int(meshes[mIdx].indexCount) / 3
                }
            }

            let estVRAM = vertexBuffer.length + indexBuffer.length + materialBuffer.length
                + bindlessBuf.length + texAllocBytes + blasAllocBytes

            return RTWorldPackage(
                url: url,
                header: header,
                extHeader: extHeader,
                sections: sections,
                meshes: meshes,
                instances: instances,
                materials: materials,
                vertexBuffer: vertexBuffer,
                indexBuffer: indexBuffer,
                materialBuffer: materialBuffer,
                blasHeap: blasHeap,
                blasList: blasList,
                meshToBlasIndex: meshToBlasIndex,
                textureHeap: texHeap,
                textures: textures,
                bindlessTexIDBuffer: bindlessBuf,
                residencySet: residencyObj,
                blasResidencySet: residencyObj,
                hasResidentBLASes: hasResidentBLASes,
                totalTriangles: totalWorldTris,
                roadNodes: roadNodes,
                roadSegments: roadSegments,
                worldLights: worldLights,
                landmarks: landmarks,
                pvsZones: pvsZones,
                pvsVerts: pvsVerts,
                pvsEntries: pvsEntries,
                spatialSectionCount: spatialCount,
                panoramaSectionCount: panoramaCount,
                estimatedVRAMBytes: estVRAM
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
    ) throws -> (MTLHeap?, [MTLTexture], MTLBuffer, Int) {
        if records.isEmpty {
            let td = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: 1, height: 1, mipmapped: false)
            td.usage = [.shaderRead]
            td.storageMode = .shared
            let fallback = device.makeTexture(descriptor: td)!
            var white: [UInt8] = [180, 180, 180, 255]
            fallback.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: &white, bytesPerRow: 4)
            var rid = fallback.gpuResourceID
            let buf = device.makeBuffer(bytes: &rid, length: MemoryLayout<MTLResourceID>.stride, options: .storageModeShared)!
            return (nil, [fallback], buf, 64)
        }

        var descriptors: [MTLTextureDescriptor] = []
        descriptors.reserveCapacity(records.count)
        var totalHeapBytes = 0
        var maxStageBytes = 0

        for rec in records {
            let w = max(1, Int(rec.width))
            let h = max(1, Int(rec.height))
            let td: MTLTextureDescriptor
            if rec.isBC7 {
                let mips = rec.mipCount
                td = MTLTextureDescriptor.texture2DDescriptor(
                    pixelFormat: .bc7_rgbaUnorm_srgb,
                    width: w,
                    height: h,
                    mipmapped: mips > 1
                )
                td.mipmapLevelCount = min(mips, td.mipmapLevelCount)
                maxStageBytes = max(maxStageBytes, max(Int(rec.dataSize), expectedBC7MipBytes(width: w, height: h, mipCount: td.mipmapLevelCount)))
            } else {
                let canMip = (w > 1 || h > 1)
                td = MTLTextureDescriptor.texture2DDescriptor(
                    pixelFormat: .rgba8Unorm_srgb,
                    width: w,
                    height: h,
                    mipmapped: canMip
                )
                maxStageBytes = max(maxStageBytes, w * h * 4)
            }
            td.usage = [.shaderRead]
            td.storageMode = .private
            let sa = device.heapTextureSizeAndAlign(descriptor: td)
            let aligned = ((sa.size + sa.align - 1) / sa.align) * sa.align
            totalHeapBytes += aligned
            descriptors.append(td)
        }

        let heapDesc = MTLHeapDescriptor()
        heapDesc.size = max(totalHeapBytes + 8 * 1024 * 1024, 1024 * 1024)
        heapDesc.storageMode = .private
        heapDesc.type = .automatic
        guard let heap = device.makeHeap(descriptor: heapDesc) else {
            throw NSError(domain: "RTWorldLoader", code: 10, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate RKPT bindless MTLHeap (\(heapDesc.size) bytes)"
            ])
        }

        // Bounded staging buffer (<= 32 MB) for batched blit copies
        let batchCapBytes = max(maxStageBytes + 4096, 24 * 1024 * 1024)
        guard let stagingBuf = device.makeBuffer(length: batchCapBytes, options: .storageModeShared),
              let idBuf = device.makeBuffer(length: records.count * MemoryLayout<MTLResourceID>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTWorldLoader", code: 11, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate RKPT texture staging or bindless ID buffer"
            ])
        }
        let idPtr = idBuf.contents().bindMemory(to: MTLResourceID.self, capacity: records.count)

        var textures: [MTLTexture] = []
        textures.reserveCapacity(records.count)
        var extraAllocBytes = 0

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
                guard let devTex = device.makeTexture(descriptor: td) else {
                    throw NSError(domain: "RTWorldLoader", code: 12, userInfo: [
                        NSLocalizedDescriptionKey: "Failed to allocate RKPT texture #\(i) (\(rec.width)x\(rec.height))"
                    ])
                }
                tex = devTex
                extraAllocBytes += device.heapTextureSizeAndAlign(descriptor: td).size
            }

            let w = max(1, Int(rec.width))
            let h = max(1, Int(rec.height))
            let srcBase = Int(texDataOffset) + Int(rec.dataOffset)

            if rec.isBC7 {
                // Upload every pre-baked BC7 mip level tightly packed in .rkpt v2 (Metal forbids generateMipmaps on BC7)
                let levels = tex.mipmapLevelCount
                var mipSrcRel = 0
                for lvl in 0..<levels {
                    let mw = max(1, w >> lvl)
                    let mh = max(1, h >> lvl)
                    let bx = (mw + 3) / 4
                    let by = (mh + 3) / 4
                    let bpr = bx * 16
                    let mipBytes = bpr * by
                    let alignedBytes = (mipBytes + 255) & ~255
                    if stageOffset + alignedBytes > batchCapBytes {
                        try flushBlitBatch()
                    }
                    let dstPtr = stagingBuf.contents().advanced(by: stageOffset)
                    let srcOff = srcBase + mipSrcRel
                    if srcOff >= 0 && srcOff + mipBytes <= fileSize {
                        memcpy(dstPtr, base.advanced(by: srcOff), mipBytes)
                    } else {
                        memset(dstPtr, 0, mipBytes)
                    }
                    activeBlit?.copy(
                        from: stagingBuf,
                        sourceOffset: stageOffset,
                        sourceBytesPerRow: bpr,
                        sourceBytesPerImage: mipBytes,
                        sourceSize: MTLSize(width: mw, height: mh, depth: 1),
                        to: tex,
                        destinationSlice: 0,
                        destinationLevel: lvl,
                        destinationOrigin: MTLOrigin(x: 0, y: 0, z: 0)
                    )
                    stageOffset += alignedBytes
                    mipSrcRel += mipBytes
                }
            } else {
                let expectedBytes = w * h * 4
                let alignedBytes = (expectedBytes + 255) & ~255
                if stageOffset + alignedBytes > batchCapBytes {
                    try flushBlitBatch()
                }
                let dstPtr = stagingBuf.contents().advanced(by: stageOffset)
                if srcBase >= 0 && srcBase + expectedBytes <= fileSize {
                    memcpy(dstPtr, base.advanced(by: srcBase), expectedBytes)
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
            }

            idPtr[i] = tex.gpuResourceID
            textures.append(tex)
        }

        activeBlit?.endEncoding()
        if let cb = activeCB {
            cb.commit()
            cb.waitUntilCompleted()
            if let err = cb.error { throw err }
        }

        return (heap, textures, idBuf, heap.size + extraAllocBytes)
    }

    private static func buildWorldBLASes(
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        meshes: [RkptMesh],
        vertexBuffer: MTLBuffer,
        indexBuffer: MTLBuffer
    ) throws -> (MTLHeap?, [MTLAccelerationStructure], [Int], Int) {
        var meshToBlas = [Int](repeating: -1, count: meshes.count)
        if meshes.isEmpty {
            return (nil, [], meshToBlas, 0)
        }

        // Precompute descriptors & sizes for all valid meshes so we can allocate ONE private BLAS MTLHeap
        // and ONE shared private scratch buffer (10x faster startup + 1 residency allocation for 10,216 BLASes!)
        struct PendingBLAS {
            let meshIndex: Int
            let descriptor: MTLPrimitiveAccelerationStructureDescriptor
            let asSize: Int
            let scratchSize: Int
        }

        var pending: [PendingBLAS] = []
        pending.reserveCapacity(meshes.count)
        var totalHeapBytes = 0
        var maxSingleScratch = 256

        for (mIdx, m) in meshes.enumerated() {
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
            geomDesc.opaque = !m.hasAlphaCutout

            let blasDesc = MTLPrimitiveAccelerationStructureDescriptor()
            blasDesc.geometryDescriptors = [geomDesc]

            let sizes = device.accelerationStructureSizes(descriptor: blasDesc)
            let sa = device.heapAccelerationStructureSizeAndAlign(size: sizes.accelerationStructureSize)
            let aligned = ((sa.size + sa.align - 1) / sa.align) * sa.align
            totalHeapBytes += aligned
            let scr = max(sizes.buildScratchBufferSize, 256)
            maxSingleScratch = max(maxSingleScratch, scr)
            pending.append(PendingBLAS(meshIndex: mIdx, descriptor: blasDesc, asSize: sizes.accelerationStructureSize, scratchSize: scr))
        }

        if pending.isEmpty {
            return (nil, [], meshToBlas, 0)
        }

        let heapDesc = MTLHeapDescriptor()
        heapDesc.size = max(totalHeapBytes + 4 * 1024 * 1024, 1024 * 1024)
        heapDesc.storageMode = .private
        heapDesc.type = .automatic
        let blasHeap = device.makeHeap(descriptor: heapDesc)

        // Reuse ONE 24 MB private scratch buffer across batched acceleration structure command buffers
        let scratchCap = max(maxSingleScratch + 512, 24 * 1024 * 1024)
        guard let scratchBuf = device.makeBuffer(length: scratchCap, options: .storageModePrivate) else {
            throw NSError(domain: "RTWorldLoader", code: 20, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate RKPT BLAS scratch buffer (\(scratchCap) bytes)"
            ])
        }

        var blasList: [MTLAccelerationStructure] = []
        blasList.reserveCapacity(pending.count)
        var extraAllocBytes = 0

        var activeCB = commandQueue.makeCommandBuffer()
        var activeEnc = activeCB?.makeAccelerationStructureCommandEncoder()
        var scratchOffset = 0
        var batchCount = 0

        func flushBLASBatch() throws {
            activeEnc?.endEncoding()
            if let cb = activeCB {
                cb.commit()
                cb.waitUntilCompleted()
                if let err = cb.error { throw err }
            }
            scratchOffset = 0
            batchCount = 0
            activeCB = commandQueue.makeCommandBuffer()
            activeEnc = activeCB?.makeAccelerationStructureCommandEncoder()
        }

        for item in pending {
            let alignedScr = (item.scratchSize + 255) & ~255
            if scratchOffset + alignedScr > scratchCap || batchCount >= 1024 {
                try flushBLASBatch()
            }

            let blas: MTLAccelerationStructure
            if let heapBlas = blasHeap?.makeAccelerationStructure(size: item.asSize) {
                blas = heapBlas
            } else {
                guard let devBlas = device.makeAccelerationStructure(size: item.asSize) else {
                    throw NSError(domain: "RTWorldLoader", code: 21, userInfo: [
                        NSLocalizedDescriptionKey: "Failed to allocate RKPT BLAS for mesh #\(item.meshIndex)"
                    ])
                }
                blas = devBlas
                extraAllocBytes += item.asSize
            }

            activeEnc?.build(
                accelerationStructure: blas,
                descriptor: item.descriptor,
                scratchBuffer: scratchBuf,
                scratchBufferOffset: scratchOffset
            )
            scratchOffset += alignedScr
            batchCount += 1
            meshToBlas[item.meshIndex] = blasList.count
            blasList.append(blas)
        }

        activeEnc?.endEncoding()
        if let cb = activeCB {
            cb.commit()
            cb.waitUntilCompleted()
            if let err = cb.error { throw err }
        }

        let totalBlasBytes = (blasHeap?.size ?? 0) + extraAllocBytes
        return (blasHeap, blasList, meshToBlas, totalBlasBytes)
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
