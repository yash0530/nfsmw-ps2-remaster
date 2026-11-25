import Foundation
import Metal
import simd

struct MetalVertex {
    var px: Float
    var py: Float
    var pz: Float
    var nx: Float
    var ny: Float
    var nz: Float
    var u: Float
    var v: Float
    var mx: Float
    var my: Float
    var mz: Float
    var pad: Float = 0.0
}

struct SubmeshDraw {
    let materialName: String
    let indexOffset: Int
    let indexCount: Int
}

struct LoadedMesh {
    let name: String
    let vertexBuffer: MTLBuffer
    let indexBuffer: MTLBuffer
    let submeshes: [SubmeshDraw]
    let vertexCount: Int
    let triangleCount: Int
}

enum ObjLoader {
    static func vertexDescriptor() -> MTLVertexDescriptor {
        let desc = MTLVertexDescriptor()
        // position (float3)
        desc.attributes[0].format = .float3
        desc.attributes[0].offset = 0
        desc.attributes[0].bufferIndex = 0
        // normal (float3)
        desc.attributes[1].format = .float3
        desc.attributes[1].offset = 12
        desc.attributes[1].bufferIndex = 0
        // uv (float2)
        desc.attributes[2].format = .float2
        desc.attributes[2].offset = 24
        desc.attributes[2].bufferIndex = 0
        // modelPos (float3)
        desc.attributes[3].format = .float3
        desc.attributes[3].offset = 32
        desc.attributes[3].bufferIndex = 0

        desc.layouts[0].stride = MemoryLayout<MetalVertex>.stride
        desc.layouts[0].stepRate = 1
        desc.layouts[0].stepFunction = .perVertex
        return desc
    }

    static func loadOBJ(at url: URL, device: MTLDevice, name: String) throws -> LoadedMesh {
        let raw = try String(contentsOf: url, encoding: .utf8)
        var positions: [SIMD3<Float>] = []
        var texCoords: [SIMD2<Float>] = []
        var normals: [SIMD3<Float>] = []

        positions.reserveCapacity(4096)
        texCoords.reserveCapacity(4096)
        normals.reserveCapacity(4096)

        var currentMat = "car_paint_hero"
        var groupedIndices: [String: [UInt32]] = [:]
        var materialOrder: [String] = []

        var vertices: [MetalVertex] = []
        vertices.reserveCapacity(8192)
        var vertCache: [SIMD3<Int32>: UInt32] = [:]
        vertCache.reserveCapacity(8192)

        func addVertex(vi: Int, vti: Int, vni: Int) -> UInt32 {
            let key = SIMD3<Int32>(Int32(vi), Int32(vti), Int32(vni))
            if let existing = vertCache[key] {
                return existing
            }
            let p = (vi >= 0 && vi < positions.count) ? positions[vi] : SIMD3<Float>(0, 0, 0)
            let n = (vni >= 0 && vni < normals.count) ? normals[vni] : SIMD3<Float>(0, 1, 0)
            let t = (vti >= 0 && vti < texCoords.count) ? texCoords[vti] : SIMD2<Float>(0, 0)
            let idx = UInt32(vertices.count)
            vertices.append(
                MetalVertex(
                    px: p.x, py: p.y, pz: p.z,
                    nx: n.x, ny: n.y, nz: n.z,
                    u: t.x, v: t.y,
                    mx: p.x, my: p.y, mz: p.z
                )
            )
            vertCache[key] = idx
            return idx
        }

        func parseIndex(_ token: Substring, count: Int) -> Int {
            guard !token.isEmpty, let rawIdx = Int(token) else { return -1 }
            return rawIdx > 0 ? (rawIdx - 1) : (count + rawIdx)
        }

        raw.enumerateLines { line, _ in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { return }
            let parts = trimmed.split(whereSeparator: { $0 == " " || $0 == "\t" })
            guard let head = parts.first else { return }

            switch head {
            case "v":
                if parts.count >= 4,
                   let x = Float(parts[1]), let y = Float(parts[2]), let z = Float(parts[3]) {
                    positions.append(SIMD3<Float>(x, y, z))
                }
            case "vt":
                if parts.count >= 3, let u = Float(parts[1]), let v = Float(parts[2]) {
                    texCoords.append(SIMD2<Float>(u, v))
                }
            case "vn":
                if parts.count >= 4,
                   let x = Float(parts[1]), let y = Float(parts[2]), let z = Float(parts[3]) {
                    normals.append(SIMD3<Float>(x, y, z))
                }
            case "usemtl":
                if parts.count >= 2 {
                    currentMat = String(parts[1])
                }
            case "f":
                guard parts.count >= 4 else { return }
                var faceVerts: [UInt32] = []
                faceVerts.reserveCapacity(parts.count - 1)
                for tok in parts.dropFirst() {
                    let comps = tok.split(separator: "/", omittingEmptySubsequences: false)
                    let vi = comps.count > 0 ? parseIndex(comps[0], count: positions.count) : -1
                    let vti = comps.count > 1 ? parseIndex(comps[1], count: texCoords.count) : -1
                    let vni = comps.count > 2 ? parseIndex(comps[2], count: normals.count) : -1
                    faceVerts.append(addVertex(vi: vi, vti: vti, vni: vni))
                }
                if groupedIndices[currentMat] == nil {
                    groupedIndices[currentMat] = []
                    materialOrder.append(currentMat)
                }
                for i in 1..<(faceVerts.count - 1) {
                    groupedIndices[currentMat]?.append(faceVerts[0])
                    groupedIndices[currentMat]?.append(faceVerts[i])
                    groupedIndices[currentMat]?.append(faceVerts[i + 1])
                }
            default:
                break
            }
        }

        var flatIndices: [UInt32] = []
        var submeshes: [SubmeshDraw] = []
        for mat in materialOrder {
            guard let idxs = groupedIndices[mat], !idxs.isEmpty else { continue }
            let start = flatIndices.count
            flatIndices.append(contentsOf: idxs)
            submeshes.append(SubmeshDraw(materialName: mat, indexOffset: start, indexCount: idxs.count))
        }

        guard !vertices.isEmpty, !flatIndices.isEmpty else {
            throw NSError(domain: "ObjLoader", code: 1, userInfo: [NSLocalizedDescriptionKey: "Empty mesh in \(url.path)"])
        }

        guard let vbuf = device.makeBuffer(
            bytes: vertices,
            length: vertices.count * MemoryLayout<MetalVertex>.stride,
            options: .storageModeShared
        ),
        let ibuf = device.makeBuffer(
            bytes: flatIndices,
            length: flatIndices.count * MemoryLayout<UInt32>.stride,
            options: .storageModeShared
        ) else {
            throw NSError(domain: "ObjLoader", code: 2, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate MTLBuffer for \(name)"])
        }

        return LoadedMesh(
            name: name,
            vertexBuffer: vbuf,
            indexBuffer: ibuf,
            submeshes: submeshes,
            vertexCount: vertices.count,
            triangleCount: flatIndices.count / 3
        )
    }

    static func createGroundPlane(device: MTLDevice, commandQueue: MTLCommandQueue, halfExtent: Float = 70.0) -> LoadedMesh? {
        let verts: [MetalVertex] = [
            MetalVertex(px: -halfExtent, py: 0.0, pz: -halfExtent, nx: 0, ny: 1, nz: 0, u: 0, v: 0, mx: -halfExtent, my: 0, mz: -halfExtent),
            MetalVertex(px:  halfExtent, py: 0.0, pz: -halfExtent, nx: 0, ny: 1, nz: 0, u: 1, v: 0, mx:  halfExtent, my: 0, mz: -halfExtent),
            MetalVertex(px:  halfExtent, py: 0.0, pz:  halfExtent, nx: 0, ny: 1, nz: 0, u: 1, v: 1, mx:  halfExtent, my: 0, mz:  halfExtent),
            MetalVertex(px: -halfExtent, py: 0.0, pz:  halfExtent, nx: 0, ny: 1, nz: 0, u: 0, v: 1, mx: -halfExtent, my: 0, mz:  halfExtent),
        ]
        let indices: [UInt32] = [0, 2, 1, 0, 3, 2]
        guard let vb = device.makeBuffer(bytes: verts, length: verts.count * MemoryLayout<MetalVertex>.stride, options: .storageModeShared),
              let ib = device.makeBuffer(bytes: indices, length: indices.count * MemoryLayout<UInt32>.stride, options: .storageModeShared) else {
            return nil
        }
        return LoadedMesh(
            name: "WetAsphaltGround",
            vertexBuffer: vb,
            indexBuffer: ib,
            submeshes: [SubmeshDraw(materialName: "wet_asphalt", indexOffset: 0, indexCount: 6)],
            vertexCount: 4,
            triangleCount: 2
        )
    }
}
