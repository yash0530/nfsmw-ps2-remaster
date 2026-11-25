import CoreGraphics
import Foundation
import ImageIO
import Metal
import UniformTypeIdentifiers
import simd

struct RTVertex {
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
    var materialID: UInt32
}

struct RTInstanceMeta {
    var vertexOffset: UInt32
    var indexOffset: UInt32
    var triOffset: UInt32
    var flags: UInt32
    var modelMatrix: simd_float4x4
    var invTransposeMatrix: simd_float4x4
}

struct RTMaterial {
    var baseColor: SIMD4<Float>
    var secondaryColor: SIMD4<Float>
    var pbrParams: SIMD4<Float>
    var emissive: SIMD4<Float>
}

struct RTLightingPreset {
    let key: String
    let name: String
    let sunDirection: SIMD3<Float>
    let sunColor: SIMD3<Float>
    let sunIntensity: Float
    let sunConeRadius: Float
    let coronaColor: SIMD3<Float>
    let skyZenith: SIMD3<Float>
    let skyHorizon: SIMD3<Float>
    let skyGround: SIMD3<Float>
    let iblIntensity: Float
    let wetness: Float
    let fogDensity: Float
    let exposure: Float
    let bloomThreshold: Float
    let bloomIntensity: Float
    let headlightsOn: Float
    let neonTint: SIMD3<Float>
    let neonIntensity: Float
}

struct RTBLASMesh {
    let name: String
    let vertexOffset: Int
    let vertexCount: Int
    let indexOffset: Int
    let triOffset: Int
    let triangleCount: Int
    let isTransparent: Bool
    let flags: UInt32
    let modelMatrix: simd_float4x4
}

struct RTScenePackage {
    let sourceLabel: String
    let meshes: [RTBLASMesh]
    let vertexBuffer: MTLBuffer
    let indexBuffer: MTLBuffer
    let triMaterialBuffer: MTLBuffer
    let materialBuffer: MTLBuffer
    let asphaltTexture: MTLTexture
    let carbonTexture: MTLTexture
    let presets: [String: RTLightingPreset]
    let cameraTarget: SIMD3<Float>
    let cameraDistance: Float
    let cameraAzimuthDeg: Float
    let cameraElevationDeg: Float
    let cameraFovDeg: Float
    let totalVertices: Int
    let totalTriangles: Int
}

enum RTMaterialID: UInt32 {
    case carPaint = 0
    case chrome = 1
    case brushedAluminum = 2
    case alloyRim = 3
    case matteTrim = 4
    case carbonFiber = 5
    case rubberTire = 6
    case smokedGlass = 7
    case headlightGlass = 8
    case headlightEmit = 9
    case taillightEmit = 10
    case interiorDark = 11
    case brakeRotor = 12
    case brakeCaliper = 13
    case licensePlate = 14
    case wetAsphalt = 15
    case urbanConcrete = 16
    case urbanGlass = 17
    case neonCyan = 18
    case neonAmber = 19

    static func fromName(_ name: String) -> RTMaterialID {
        switch name {
        case "car_paint_hero": return .carPaint
        case "chrome_trim": return .chrome
        case "brushed_aluminum": return .brushedAluminum
        case "alloy_rim": return .alloyRim
        case "matte_trim": return .matteTrim
        case "carbon_fiber": return .carbonFiber
        case "rubber_tire": return .rubberTire
        case "smoked_glass": return .smokedGlass
        case "headlight_glass": return .headlightGlass
        case "headlight_emissive": return .headlightEmit
        case "taillight_emissive": return .taillightEmit
        case "interior_dark": return .interiorDark
        case "brake_rotor": return .brakeRotor
        case "brake_caliper": return .brakeCaliper
        case "license_plate": return .licensePlate
        default: return .carPaint
        }
    }
}

enum RTSceneBuilder {
    static func defaultPresets() -> [String: RTLightingPreset] {
        return [
            "golden_hour": RTLightingPreset(
                key: "golden_hour",
                name: "Rockport 2026 Golden Hour",
                sunDirection: simd_normalize(SIMD3<Float>(0.38, 0.24, 0.89)),
                sunColor: SIMD3<Float>(1.18, 0.82, 0.44),
                sunIntensity: 5.8,
                sunConeRadius: 0.028,
                coronaColor: SIMD3<Float>(1.0, 0.62, 0.26),
                skyZenith: SIMD3<Float>(0.09, 0.22, 0.54),
                skyHorizon: SIMD3<Float>(0.94, 0.48, 0.20),
                skyGround: SIMD3<Float>(0.04, 0.035, 0.045),
                iblIntensity: 1.25,
                wetness: 0.72,
                fogDensity: 0.006,
                exposure: 1.06,
                bloomThreshold: 1.35,
                bloomIntensity: 0.36,
                headlightsOn: 1.0,
                neonTint: SIMD3<Float>(1.0, 0.62, 0.24),
                neonIntensity: 0.48
            ),
            "night_neon": RTLightingPreset(
                key: "night_neon",
                name: "Rockport 2026 Night Neon",
                sunDirection: simd_normalize(SIMD3<Float>(-0.44, 0.36, 0.82)),
                sunColor: SIMD3<Float>(0.52, 0.70, 1.08),
                sunIntensity: 2.3,
                sunConeRadius: 0.042,
                coronaColor: SIMD3<Float>(0.20, 0.48, 0.95),
                skyZenith: SIMD3<Float>(0.010, 0.018, 0.048),
                skyHorizon: SIMD3<Float>(0.065, 0.11, 0.22),
                skyGround: SIMD3<Float>(0.012, 0.016, 0.024),
                iblIntensity: 0.95,
                wetness: 0.94,
                fogDensity: 0.010,
                exposure: 1.24,
                bloomThreshold: 1.0,
                bloomIntensity: 0.68,
                headlightsOn: 1.65,
                neonTint: SIMD3<Float>(0.10, 0.86, 1.0),
                neonIntensity: 1.45
            ),
            "daylight": RTLightingPreset(
                key: "daylight",
                name: "Rockport 2026 Crisp Daylight",
                sunDirection: simd_normalize(SIMD3<Float>(0.45, 0.54, 0.71)),
                sunColor: SIMD3<Float>(1.0, 0.985, 0.95),
                sunIntensity: 5.8,
                sunConeRadius: 0.020,
                coronaColor: SIMD3<Float>(0.68, 0.84, 1.0),
                skyZenith: SIMD3<Float>(0.14, 0.40, 0.88),
                skyHorizon: SIMD3<Float>(0.68, 0.83, 0.97),
                skyGround: SIMD3<Float>(0.07, 0.08, 0.09),
                iblIntensity: 1.45,
                wetness: 0.48,
                fogDensity: 0.004,
                exposure: 1.02,
                bloomThreshold: 1.45,
                bloomIntensity: 0.28,
                headlightsOn: 0.60,
                neonTint: SIMD3<Float>(0.35, 0.80, 1.0),
                neonIntensity: 0.25
            )
        ]
    }

    static func defaultMaterialTable() -> [RTMaterial] {
        var mats = [RTMaterial](repeating: RTMaterial(
            baseColor: SIMD4<Float>(0.7, 0.7, 0.7, 1.0),
            secondaryColor: SIMD4<Float>(0.1, 0.2, 0.5, 1.5),
            pbrParams: SIMD4<Float>(0.5, 0.3, 0.5, 0.05),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        ), count: 20)

        mats[Int(RTMaterialID.carPaint.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.78, 0.81, 0.86, 1.0),
            secondaryColor: SIMD4<Float>(0.05, 0.22, 0.58, 1.5),
            pbrParams: SIMD4<Float>(0.86, 0.19, 1.0, 0.03),
            emissive: SIMD4<Float>(0, 0, 0, 1)
        )
        mats[Int(RTMaterialID.chrome.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.94, 0.95, 0.97, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(1.0, 0.045, 0.0, 0.04),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.brushedAluminum.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.78, 0.80, 0.83, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.94, 0.20, 0.2, 0.08),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.alloyRim.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.85, 0.87, 0.91, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.96, 0.13, 0.45, 0.045),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.matteTrim.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.055, 0.058, 0.064, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.62, 0.0, 0.2),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.carbonFiber.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.075, 0.08, 0.088, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.08, 0.26, 1.0, 0.04),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.rubberTire.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.032, 0.033, 0.036, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.82, 0.0, 0.5),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.smokedGlass.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.04, 0.05, 0.07, 0.58),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.52),
            pbrParams: SIMD4<Float>(0.04, 0.03, 1.0, 0.02),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.headlightGlass.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.86, 0.91, 0.98, 0.22),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.52),
            pbrParams: SIMD4<Float>(0.02, 0.025, 1.0, 0.02),
            emissive: SIMD4<Float>(0.35, 0.42, 0.55, 0)
        )
        mats[Int(RTMaterialID.headlightEmit.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.95, 0.97, 1.0, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.85, 0.08, 1.0, 0.02),
            emissive: SIMD4<Float>(6.0, 6.8, 8.2, 0)
        )
        mats[Int(RTMaterialID.taillightEmit.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.85, 0.06, 0.03, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.05, 0.12, 1.0, 0.03),
            emissive: SIMD4<Float>(5.6, 0.45, 0.12, 0)
        )
        mats[Int(RTMaterialID.interiorDark.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.06, 0.062, 0.068, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.02, 0.72, 0.0, 0.4),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.brakeRotor.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.62, 0.64, 0.68, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.90, 0.28, 0.0, 0.1),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.brakeCaliper.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.82, 0.12, 0.08, 1.0),
            secondaryColor: SIMD4<Float>(0.82, 0.12, 0.08, 1.5),
            pbrParams: SIMD4<Float>(0.55, 0.24, 0.9, 0.05),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.licensePlate.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.16, 0.19, 0.24, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.15, 0.38, 0.3, 0.1),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.wetAsphalt.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.12, 0.125, 0.135, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.34, 0.0, 0.03),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.urbanConcrete.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.18, 0.19, 0.21, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.78, 0.0, 0.3),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.urbanGlass.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.08, 0.11, 0.15, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.52),
            pbrParams: SIMD4<Float>(0.35, 0.12, 0.85, 0.04),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.neonCyan.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.15, 0.85, 1.0, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.1, 0.2, 0.5, 0.05),
            emissive: SIMD4<Float>(0.3, 1.8, 2.4, 0)
        )
        mats[Int(RTMaterialID.neonAmber.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(1.0, 0.65, 0.22, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.1, 0.2, 0.5, 0.05),
            emissive: SIMD4<Float>(2.2, 1.1, 0.35, 0)
        )
        return mats
    }

    // Smooth vertex normal accumulation when an OBJ lacks explicit normals or needs smoothing
    private static func computeSmoothNormals(positions: [SIMD3<Float>], indices: [UInt32]) -> [SIMD3<Float>] {
        var accum = [SIMD3<Float>](repeating: .zero, count: positions.count)
        var t = 0
        while t + 2 < indices.count {
            let i0 = Int(indices[t])
            let i1 = Int(indices[t + 1])
            let i2 = Int(indices[t + 2])
            if i0 < positions.count && i1 < positions.count && i2 < positions.count {
                let p0 = positions[i0]
                let p1 = positions[i1]
                let p2 = positions[i2]
                let fn = simd_cross(p1 - p0, p2 - p0)
                accum[i0] += fn
                accum[i1] += fn
                accum[i2] += fn
            }
            t += 3
        }
        return accum.map { n in
            let len = simd_length(n)
            return len > 1e-6 ? (n / len) : SIMD3<Float>(0, 1, 0)
        }
    }

    // Parse an OBJ file, optionally converting PS2 Z-up to Y-up meters, and appending into global geometry pools.
    private static func parseOBJIntoPool(
        url: URL,
        name: String,
        ps2ZUpToYUp: Bool,
        isTransparent: Bool,
        liveryEnabled: Bool,
        modelMatrix: simd_float4x4,
        materialMapper: (String, SIMD3<Float>) -> RTMaterialID,
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) throws -> RTBLASMesh {
        let raw = try String(contentsOf: url, encoding: .utf8)
        var positions: [SIMD3<Float>] = []
        var texCoords: [SIMD2<Float>] = []
        var normals: [SIMD3<Float>] = []
        positions.reserveCapacity(4096)
        texCoords.reserveCapacity(4096)
        normals.reserveCapacity(4096)

        var currentGroup = "car_paint_hero"
        var localVerts: [RTVertex] = []
        var localIndices: [UInt32] = []
        var localTriMats: [UInt32] = []
        var vertCache: [SIMD3<Int32>: UInt32] = [:]

        func convertPos(_ p: SIMD3<Float>) -> SIMD3<Float> {
            if !ps2ZUpToYUp { return p }
            return SIMD3<Float>(-p.y, p.z + 0.131551, p.x)
        }

        func convertNorm(_ n: SIMD3<Float>) -> SIMD3<Float> {
            if !ps2ZUpToYUp { return n }
            return SIMD3<Float>(-n.y, n.z, n.x)
        }

        func parseIndex(_ token: Substring, count: Int) -> Int {
            guard !token.isEmpty, let rawIdx = Int(token) else { return -1 }
            return rawIdx > 0 ? (rawIdx - 1) : (count + rawIdx)
        }

        func addVertex(vi: Int, vti: Int, vni: Int, matID: RTMaterialID) -> UInt32 {
            let key = SIMD3<Int32>(Int32(vi), Int32(vti), Int32(vni))
            if let existing = vertCache[key] {
                return existing
            }
            let pRaw = (vi >= 0 && vi < positions.count) ? positions[vi] : .zero
            let nRaw = (vni >= 0 && vni < normals.count) ? normals[vni] : SIMD3<Float>(0, 1, 0)
            let t = (vti >= 0 && vti < texCoords.count) ? texCoords[vti] : .zero
            let p = convertPos(pRaw)
            let n = convertNorm(nRaw)
            let idx = UInt32(localVerts.count)
            localVerts.append(RTVertex(
                px: p.x, py: p.y, pz: p.z,
                nx: n.x, ny: n.y, nz: n.z,
                u: t.x, v: t.y,
                mx: p.x, my: p.y, mz: p.z,
                materialID: matID.rawValue
            ))
            vertCache[key] = idx
            return idx
        }

        for rawLine in raw.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
            let trimmed = rawLine.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            let parts = trimmed.split(whereSeparator: { $0 == " " || $0 == "\t" })
            guard let head = parts.first else { continue }

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
            case "usemtl", "g":
                if parts.count >= 2 {
                    currentGroup = String(parts[1])
                }
            case "f":
                guard parts.count >= 4 else { continue }
                var faceVerts: [UInt32] = []
                var avgPos: SIMD3<Float> = .zero
                for tok in parts.dropFirst() {
                    let comps = tok.split(separator: "/", omittingEmptySubsequences: false)
                    let vi = comps.count > 0 ? parseIndex(comps[0], count: positions.count) : -1
                    let vti = comps.count > 1 ? parseIndex(comps[1], count: texCoords.count) : -1
                    let vni = comps.count > 2 ? parseIndex(comps[2], count: normals.count) : -1
                    if vi >= 0 && vi < positions.count {
                        avgPos += convertPos(positions[vi])
                    }
                    let tempMat = materialMapper(currentGroup, .zero)
                    faceVerts.append(addVertex(vi: vi, vti: vti, vni: vni, matID: tempMat))
                }
                avgPos /= Float(max(faceVerts.count, 1))
                let faceMat = materialMapper(currentGroup, avgPos)
                for i in 1..<(faceVerts.count - 1) {
                    localIndices.append(faceVerts[0])
                    localIndices.append(faceVerts[i])
                    localIndices.append(faceVerts[i + 1])
                    localTriMats.append(faceMat.rawValue)
                }
            default:
                break
            }
        }

        // Compute smooth vertex normals if OBJ did not supply vertex normals
        if normals.isEmpty && !localVerts.isEmpty {
            let posArr = localVerts.map { SIMD3<Float>($0.px, $0.py, $0.pz) }
            let smooth = computeSmoothNormals(positions: posArr, indices: localIndices)
            for i in 0..<localVerts.count {
                localVerts[i].nx = smooth[i].x
                localVerts[i].ny = smooth[i].y
                localVerts[i].nz = smooth[i].z
            }
        }

        let vOff = vertices.count
        let iOff = indices.count
        let tOff = triMaterials.count
        vertices.append(contentsOf: localVerts)
        indices.append(contentsOf: localIndices)
        triMaterials.append(contentsOf: localTriMats)

        return RTBLASMesh(
            name: name,
            vertexOffset: vOff,
            vertexCount: localVerts.count,
            indexOffset: iOff,
            triOffset: tOff,
            triangleCount: localTriMats.count,
            isTransparent: isTransparent,
            flags: (liveryEnabled ? 1 : 0) | (isTransparent ? 2 : 0),
            modelMatrix: modelMatrix
        )
    }

    // Add an axis-aligned box into the geometry pool (for Rockport urban architecture & synthetic car parts).
    private static func appendBox(
        center: SIMD3<Float>,
        halfExtents: SIMD3<Float>,
        matID: RTMaterialID,
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) {
        let cx = center.x, cy = center.y, cz = center.z
        let hx = halfExtents.x, hy = halfExtents.y, hz = halfExtents.z

        let faces: [(SIMD3<Float>, [SIMD3<Float>])] = [
            (SIMD3<Float>(0, 0, 1),  [SIMD3(cx - hx, cy - hy, cz + hz), SIMD3(cx + hx, cy - hy, cz + hz), SIMD3(cx + hx, cy + hy, cz + hz), SIMD3(cx - hx, cy + hy, cz + hz)]),
            (SIMD3<Float>(0, 0, -1), [SIMD3(cx + hx, cy - hy, cz - hz), SIMD3(cx - hx, cy - hy, cz - hz), SIMD3(cx - hx, cy + hy, cz - hz), SIMD3(cx + hx, cy + hy, cz - hz)]),
            (SIMD3<Float>(1, 0, 0),  [SIMD3(cx + hx, cy - hy, cz + hz), SIMD3(cx + hx, cy - hy, cz - hz), SIMD3(cx + hx, cy + hy, cz - hz), SIMD3(cx + hx, cy + hy, cz + hz)]),
            (SIMD3<Float>(-1, 0, 0), [SIMD3(cx - hx, cy - hy, cz - hz), SIMD3(cx - hx, cy - hy, cz + hz), SIMD3(cx - hx, cy + hy, cz + hz), SIMD3(cx - hx, cy + hy, cz - hz)]),
            (SIMD3<Float>(0, 1, 0),  [SIMD3(cx - hx, cy + hy, cz + hz), SIMD3(cx + hx, cy + hy, cz + hz), SIMD3(cx + hx, cy + hy, cz - hz), SIMD3(cx - hx, cy + hy, cz - hz)]),
            (SIMD3<Float>(0, -1, 0), [SIMD3(cx - hx, cy - hy, cz - hz), SIMD3(cx + hx, cy - hy, cz - hz), SIMD3(cx + hx, cy - hy, cz + hz), SIMD3(cx - hx, cy - hy, cz + hz)])
        ]
        let uvs = [SIMD2<Float>(0, 0), SIMD2<Float>(1, 0), SIMD2<Float>(1, 1), SIMD2<Float>(0, 1)]

        for (n, corners) in faces {
            let base = UInt32(vertices.count)
            for (k, p) in corners.enumerated() {
                vertices.append(RTVertex(
                    px: p.x, py: p.y, pz: p.z,
                    nx: n.x, ny: n.y, nz: n.z,
                    u: uvs[k].x, v: uvs[k].y,
                    mx: p.x, my: p.y, mz: p.z,
                    materialID: matID.rawValue
                ))
            }
            indices.append(contentsOf: [base, base + 1, base + 2, base, base + 2, base + 3])
            triMaterials.append(matID.rawValue)
            triMaterials.append(matID.rawValue)
        }
    }

    private static func appendCylinder(
        center: SIMD3<Float>,
        radius: Float,
        halfWidth: Float,
        segments: Int,
        matID: RTMaterialID,
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) {
        let capL = UInt32(vertices.count)
        vertices.append(RTVertex(px: center.x - halfWidth, py: center.y, pz: center.z, nx: -1, ny: 0, nz: 0, u: 0.5, v: 0.5, mx: center.x - halfWidth, my: center.y, mz: center.z, materialID: matID.rawValue))
        let capR = UInt32(vertices.count)
        vertices.append(RTVertex(px: center.x + halfWidth, py: center.y, pz: center.z, nx: 1, ny: 0, nz: 0, u: 0.5, v: 0.5, mx: center.x + halfWidth, my: center.y, mz: center.z, materialID: matID.rawValue))

        for i in 0..<segments {
            let a0 = Float(i) / Float(segments) * 2.0 * .pi
            let a1 = Float(i + 1) / Float(segments) * 2.0 * .pi
            let y0 = cos(a0) * radius, z0 = sin(a0) * radius
            let y1 = cos(a1) * radius, z1 = sin(a1) * radius
            let n0 = simd_normalize(SIMD3<Float>(0, y0, z0))
            let n1 = simd_normalize(SIMD3<Float>(0, y1, z1))

            let base = UInt32(vertices.count)
            vertices.append(RTVertex(px: center.x - halfWidth, py: center.y + y0, pz: center.z + z0, nx: n0.x, ny: n0.y, nz: n0.z, u: 0, v: 0, mx: center.x - halfWidth, my: center.y + y0, mz: center.z + z0, materialID: matID.rawValue))
            vertices.append(RTVertex(px: center.x + halfWidth, py: center.y + y0, pz: center.z + z0, nx: n0.x, ny: n0.y, nz: n0.z, u: 1, v: 0, mx: center.x + halfWidth, my: center.y + y0, mz: center.z + z0, materialID: matID.rawValue))
            vertices.append(RTVertex(px: center.x + halfWidth, py: center.y + y1, pz: center.z + z1, nx: n1.x, ny: n1.y, nz: n1.z, u: 1, v: 1, mx: center.x + halfWidth, my: center.y + y1, mz: center.z + z1, materialID: matID.rawValue))
            vertices.append(RTVertex(px: center.x - halfWidth, py: center.y + y1, pz: center.z + z1, nx: n1.x, ny: n1.y, nz: n1.z, u: 0, v: 1, mx: center.x - halfWidth, my: center.y + y1, mz: center.z + z1, materialID: matID.rawValue))

            indices.append(contentsOf: [base, base + 1, base + 2, base, base + 2, base + 3])
            indices.append(contentsOf: [capL, base + 3, base])
            indices.append(contentsOf: [capR, base + 1, base + 2])
            for _ in 0..<4 { triMaterials.append(matID.rawValue) }
        }
    }

    private static func appendGroundAndRockportArchitecture(
        meshes: inout [RTBLASMesh],
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) {
        // 1. Wet Asphalt Ground Plane BLAS (y = 0, [-65, +65])
        do {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count
            let half: Float = 65.0
            let corners = [
                SIMD3<Float>(-half, 0, -half),
                SIMD3<Float>( half, 0, -half),
                SIMD3<Float>( half, 0,  half),
                SIMD3<Float>(-half, 0,  half)
            ]
            for c in corners {
                vertices.append(RTVertex(
                    px: c.x, py: c.y, pz: c.z,
                    nx: 0, ny: 1, nz: 0,
                    u: c.x * 0.2, v: c.z * 0.2,
                    mx: c.x, my: c.y, mz: c.z,
                    materialID: RTMaterialID.wetAsphalt.rawValue
                ))
            }
            indices.append(contentsOf: [0, 2, 1, 0, 3, 2])
            triMaterials.append(RTMaterialID.wetAsphalt.rawValue)
            triMaterials.append(RTMaterialID.wetAsphalt.rawValue)
            meshes.append(RTBLASMesh(
                name: "Rockport_WetAsphaltGround",
                vertexOffset: vOff, vertexCount: 4,
                indexOffset: iOff, triOffset: tOff, triangleCount: 2,
                isTransparent: false, flags: 4, modelMatrix: matrix_identity_float4x4
            ))
        }

        // 2. Stylized Rockport Urban Bridge Pillars, Arches, Downtown Towers & Neon Light Panels
        do {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count

            // Overpass concrete bridge arch + pillars framing the background (-Z) & flanks
            appendBox(center: SIMD3(-10.5, 3.2, -13.5), halfExtents: SIMD3(0.70, 3.2, 0.75), matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 10.5, 3.2, -13.5), halfExtents: SIMD3(0.70, 3.2, 0.75), matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3(  0.0, 6.2, -13.5), halfExtents: SIMD3(11.8, 0.65, 0.90), matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            // Bridge soffit neon strip (reflects on car roof & hood and wet road!)
            appendBox(center: SIMD3(  0.0, 5.50, -12.8), halfExtents: SIMD3(8.5, 0.08, 0.08), matID: .neonCyan, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)

            // Side concrete Jersey barriers with warm amber reflector strips
            appendBox(center: SIMD3(-8.5, 0.42, 0.0), halfExtents: SIMD3(0.28, 0.42, 16.0), matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 8.5, 0.42, 0.0), halfExtents: SIMD3(0.28, 0.42, 16.0), matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)

            // Streetlight stanchions & glowing warm/cyan overhead light bars (reflected in puddles & car paint!)
            for zPos: Float in [-7.5, 1.5, 10.5] {
                appendBox(center: SIMD3(-8.2, 2.6, zPos), halfExtents: SIMD3(0.10, 2.6, 0.10), matID: .brushedAluminum, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
                appendBox(center: SIMD3(-7.3, 5.1, zPos), halfExtents: SIMD3(0.95, 0.08, 0.10), matID: .brushedAluminum, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
                appendBox(center: SIMD3(-6.7, 4.98, zPos), halfExtents: SIMD3(0.55, 0.05, 0.12), matID: .neonAmber, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)

                appendBox(center: SIMD3( 8.2, 2.6, zPos), halfExtents: SIMD3(0.10, 2.6, 0.10), matID: .brushedAluminum, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
                appendBox(center: SIMD3( 7.3, 5.1, zPos), halfExtents: SIMD3(0.95, 0.08, 0.10), matID: .brushedAluminum, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
                appendBox(center: SIMD3( 6.7, 4.98, zPos), halfExtents: SIMD3(0.55, 0.05, 0.12), matID: .neonCyan, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            }

            // Stylized Rockport downtown towers behind & around the plaza for 3D reflections
            let towers: [(SIMD3<Float>, SIMD3<Float>, RTMaterialID)] = [
                (SIMD3(-15.0, 9.5, -22.0), SIMD3(3.2, 9.5, 3.2), .urbanGlass),
                (SIMD3( -5.5, 13.5, -27.0), SIMD3(3.6, 13.5, 3.6), .urbanGlass),
                (SIMD3(  6.5, 11.5, -25.0), SIMD3(3.4, 11.5, 3.4), .urbanGlass),
                (SIMD3( 16.0, 8.5, -20.0), SIMD3(3.0, 8.5, 3.0), .urbanConcrete),
                (SIMD3(-17.5, 7.5,  11.0), SIMD3(2.8, 7.5, 3.5), .urbanGlass),
                (SIMD3( 18.0, 8.5,  12.0), SIMD3(3.0, 8.5, 3.8), .urbanGlass)
            ]
            for (c, h, m) in towers {
                appendBox(center: c, halfExtents: h, matID: m, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            }

            // Local index array for this BLAS must be 0-based relative to vOff!
            let totalLocalVerts = vertices.count - vOff
            let addedTris = triMaterials.count - tOff
            // Note: appendBox appended global indices; convert indices in [iOff...] to be 0-based relative to vOff!
            for idx in iOff..<indices.count {
                indices[idx] -= UInt32(vOff)
            }

            meshes.append(RTBLASMesh(
                name: "Rockport_UrbanArchitecture",
                vertexOffset: vOff, vertexCount: totalLocalVerts,
                indexOffset: iOff, triOffset: tOff, triangleCount: addedTris,
                isTransparent: false, flags: 4, modelMatrix: matrix_identity_float4x4
            ))
        }
    }

    private static func appendSyntheticCar(
        meshes: inout [RTBLASMesh],
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) {
        // Build a clean synthetic GT coupe + wing + 4 wheels + lights for offline CI (`--synthetic`)
        do {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count
            // Lower body shell
            appendBox(center: SIMD3(0, 0.44, 0.12), halfExtents: SIMD3(0.92, 0.24, 2.22), matID: .carPaint, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            // Cabin roof & hood slope
            appendBox(center: SIMD3(0, 0.84, -0.15), halfExtents: SIMD3(0.72, 0.22, 1.05), matID: .carPaint, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            // Carbon fiber GT wing
            appendBox(center: SIMD3(-0.55, 0.82, -1.88), halfExtents: SIMD3(0.03, 0.18, 0.12), matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.55, 0.82, -1.88), halfExtents: SIMD3(0.03, 0.18, 0.12), matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.0,  1.02, -1.90), halfExtents: SIMD3(0.86, 0.03, 0.18), matID: .carbonFiber, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            // Headlights & taillights
            appendBox(center: SIMD3(-0.62, 0.54, 2.34), halfExtents: SIMD3(0.16, 0.06, 0.03), matID: .headlightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.62, 0.54, 2.34), halfExtents: SIMD3(0.16, 0.06, 0.03), matID: .headlightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3(-0.62, 0.64, -2.10), halfExtents: SIMD3(0.18, 0.06, 0.03), matID: .taillightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.62, 0.64, -2.10), halfExtents: SIMD3(0.18, 0.06, 0.03), matID: .taillightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)

            for idx in iOff..<indices.count { indices[idx] -= UInt32(vOff) }
            meshes.append(RTBLASMesh(
                name: "Synthetic_M3_Body",
                vertexOffset: vOff, vertexCount: vertices.count - vOff,
                indexOffset: iOff, triOffset: tOff, triangleCount: triMaterials.count - tOff,
                isTransparent: false, flags: 1, modelMatrix: matrix_identity_float4x4
            ))
        }

        // 4 wheels
        let wheelPositions: [(String, SIMD3<Float>)] = [
            ("Synthetic_Wheel_FL", SIMD3(-0.88, 0.33,  1.58)),
            ("Synthetic_Wheel_FR", SIMD3( 0.88, 0.33,  1.58)),
            ("Synthetic_Wheel_RL", SIMD3(-0.88, 0.33, -1.14)),
            ("Synthetic_Wheel_RR", SIMD3( 0.88, 0.33, -1.14))
        ]
        for (wName, pos) in wheelPositions {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count
            appendCylinder(center: pos, radius: 0.33, halfWidth: 0.13, segments: 24, matID: .rubberTire, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendCylinder(center: pos, radius: 0.22, halfWidth: 0.136, segments: 18, matID: .alloyRim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            for idx in iOff..<indices.count { indices[idx] -= UInt32(vOff) }
            meshes.append(RTBLASMesh(
                name: wName,
                vertexOffset: vOff, vertexCount: vertices.count - vOff,
                indexOffset: iOff, triOffset: tOff, triangleCount: triMaterials.count - tOff,
                isTransparent: false, flags: 0, modelMatrix: matrix_identity_float4x4
            ))
        }
    }

    static func buildScene(
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        scenePath: String?,
        forceSynthetic: Bool
    ) throws -> RTScenePackage {
        var vertices: [RTVertex] = []
        var indices: [UInt32] = []
        var triMaterials: [UInt32] = []
        var meshes: [RTBLASMesh] = []
        vertices.reserveCapacity(32768)
        indices.reserveCapacity(65536)

        var sourceLabel = "synthetic"
        var camTarget = SIMD3<Float>(0.0, 0.56, 0.14)
        var camDist: Float = 5.25
        var camAz: Float = 38.0
        var camEl: Float = 11.5
        var camFov: Float = 40.0

        var asphaltPath: String? = "build/cc0_baked/asphalt_02_wet_4k.png"
        var carbonPath: String? = "build/cc0_baked/carbon_fiber_4k.png"

        let fm = FileManager.default
        let resolvedScenePath = scenePath ?? "build/metal_wave3/scene/scene.json"

        if !forceSynthetic {
            let sceneURL = URL(fileURLWithPath: resolvedScenePath)
            var jsonURL: URL? = nil
            if resolvedScenePath.hasSuffix(".json") && fm.fileExists(atPath: sceneURL.path) {
                jsonURL = sceneURL
            } else if fm.fileExists(atPath: sceneURL.appendingPathComponent("scene.json").path) {
                jsonURL = sceneURL.appendingPathComponent("scene.json")
            }

            if let jURL = jsonURL,
               let data = try? Data(contentsOf: jURL),
               let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let parts = root["parts"] as? [[String: Any]] {
                sourceLabel = "scene.json (\(jURL.path))"
                let sceneDir = jURL.deletingLastPathComponent()

                if let cam = root["camera"] as? [String: Any] {
                    if let t = cam["target"] as? [Double], t.count == 3 {
                        camTarget = SIMD3<Float>(Float(t[0]), Float(t[1]), Float(t[2]))
                    }
                    if let d = cam["distance"] as? Double { camDist = Float(d) }
                    if let a = cam["azimuth_deg"] as? Double { camAz = Float(a) }
                    if let e = cam["elevation_deg"] as? Double { camEl = Float(e) }
                    if let f = cam["fov_deg"] as? Double { camFov = Float(f) }
                }
                if let textures = root["textures"] as? [String: String] {
                    if let a = textures["asphalt_wet"], fm.fileExists(atPath: a) { asphaltPath = a }
                    if let c = textures["carbon_fiber"], fm.fileExists(atPath: c) { carbonPath = c }
                }

                for part in parts {
                    let visible = (part["visible"] as? Bool) ?? true
                    if !visible { continue }
                    guard let objRel = part["obj"] as? String else { continue }
                    let objURL = sceneDir.appendingPathComponent(objRel)
                    guard fm.fileExists(atPath: objURL.path) else { continue }
                    let pName = (part["name"] as? String) ?? objURL.deletingPathExtension().lastPathComponent
                    let transparent = (part["transparent"] as? Bool) ?? false
                    let mesh = try parseOBJIntoPool(
                        url: objURL,
                        name: pName,
                        ps2ZUpToYUp: false,
                        isTransparent: transparent,
                        liveryEnabled: true,
                        modelMatrix: matrix_identity_float4x4,
                        materialMapper: { matName, _ in RTMaterialID.fromName(matName) },
                        vertices: &vertices,
                        indices: &indices,
                        triMaterials: &triMaterials
                    )
                    if mesh.triangleCount > 0 { meshes.append(mesh) }
                }
            } else if fm.fileExists(atPath: "build/obj_preview/cars_bmwm3gtre46_geometry_bin__BMWM3GTRE46_KIT00_BODY_B.obj") {
                sourceLabel = "build/obj_preview"
                let bodyURL = URL(fileURLWithPath: "build/obj_preview/cars_bmwm3gtre46_geometry_bin__BMWM3GTRE46_KIT00_BODY_B.obj")
                let bodyMesh = try parseOBJIntoPool(
                    url: bodyURL,
                    name: "BMWM3GTRE46_KIT00_BODY_B",
                    ps2ZUpToYUp: true,
                    isTransparent: false,
                    liveryEnabled: true,
                    modelMatrix: matrix_identity_float4x4,
                    materialMapper: { grp, pos in
                        if grp.contains("tex5") { return .chrome }
                        if grp.contains("tex6") || grp.contains("tex3") { return .matteTrim }
                        if grp.contains("tex9") { return pos.z > 0.0 ? .headlightEmit : .taillightEmit }
                        return .carPaint
                    },
                    vertices: &vertices,
                    indices: &indices,
                    triMaterials: &triMaterials
                )
                meshes.append(bodyMesh)

                let wheelURL = URL(fileURLWithPath: "build/obj_preview/cars_wheels_geometry_bin__ENKEI_STYLE01_17_25_B.obj")
                if fm.fileExists(atPath: wheelURL.path) {
                    let wheelSlots: [(String, SIMD3<Float>, Float)] = [
                        ("ENKEI_FL", SIMD3(-0.88, 0.329,  1.595),  1.0),
                        ("ENKEI_FR", SIMD3( 0.88, 0.329,  1.595), -1.0),
                        ("ENKEI_RL", SIMD3(-0.88, 0.329, -1.1295), 1.0),
                        ("ENKEI_RR", SIMD3( 0.88, 0.329, -1.1295),-1.0)
                    ]
                    for (wName, offset, flipX) in wheelSlots {
                        var mat = matrix_identity_float4x4
                        mat.columns.0 = SIMD4<Float>(flipX, 0, 0, 0)
                        mat.columns.3 = SIMD4<Float>(offset.x, offset.y, offset.z, 1)
                        let wMesh = try parseOBJIntoPool(
                            url: wheelURL,
                            name: wName,
                            ps2ZUpToYUp: true,
                            isTransparent: false,
                            liveryEnabled: false,
                            modelMatrix: mat,
                            materialMapper: { grp, _ in
                                if grp.contains("tex3") { return .rubberTire }
                                if grp.contains("tex0") { return .matteTrim }
                                return .alloyRim
                            },
                            vertices: &vertices,
                            indices: &indices,
                            triMaterials: &triMaterials
                        )
                        meshes.append(wMesh)
                    }
                }
            }
        }

        if meshes.isEmpty {
            sourceLabel = "synthetic"
            appendSyntheticCar(meshes: &meshes, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
        }

        // Always append wet asphalt ground plane & stylized Rockport urban architecture BLASes
        appendGroundAndRockportArchitecture(meshes: &meshes, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)

        guard let vBuf = device.makeBuffer(bytes: vertices, length: vertices.count * MemoryLayout<RTVertex>.stride, options: .storageModeShared),
              let iBuf = device.makeBuffer(bytes: indices, length: indices.count * MemoryLayout<UInt32>.stride, options: .storageModeShared),
              let tmBuf = device.makeBuffer(bytes: triMaterials, length: triMaterials.count * MemoryLayout<UInt32>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTSceneBuilder", code: 1, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate Metal geometry buffers"])
        }

        let mats = defaultMaterialTable()
        guard let mBuf = device.makeBuffer(bytes: mats, length: mats.count * MemoryLayout<RTMaterial>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTSceneBuilder", code: 2, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate Metal material buffer"])
        }

        let asphaltTex = asphaltPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, commandQueue: commandQueue) }
            ?? makeProceduralAsphaltTexture(device: device)
        let carbonTex = carbonPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, commandQueue: commandQueue) }
            ?? makeProceduralCarbonTexture(device: device)

        return RTScenePackage(
            sourceLabel: sourceLabel,
            meshes: meshes,
            vertexBuffer: vBuf,
            indexBuffer: iBuf,
            triMaterialBuffer: tmBuf,
            materialBuffer: mBuf,
            asphaltTexture: asphaltTex,
            carbonTexture: carbonTex,
            presets: defaultPresets(),
            cameraTarget: camTarget,
            cameraDistance: camDist,
            cameraAzimuthDeg: camAz,
            cameraElevationDeg: camEl,
            cameraFovDeg: camFov,
            totalVertices: vertices.count,
            totalTriangles: triMaterials.count
        )
    }

    // MARK: - Texture Helpers

    static func loadTexture(at url: URL, device: MTLDevice, commandQueue: MTLCommandQueue, maxDim: Int = 1024) -> MTLTexture? {
        guard FileManager.default.fileExists(atPath: url.path),
              let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let cgImage = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            return nil
        }
        let scale = min(1.0, Double(maxDim) / Double(max(cgImage.width, cgImage.height)))
        let width = max(1, Int((Double(cgImage.width) * scale).rounded()))
        let height = max(1, Int((Double(cgImage.height) * scale).rounded()))
        let bytesPerRow = width * 4
        var raw = [UInt8](repeating: 0, count: bytesPerRow * height)
        guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(
                  data: &raw,
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bytesPerRow: bytesPerRow,
                  space: cs,
                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
              ) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: width, height: height, mipmapped: false)
        desc.usage = [.shaderRead]
        guard let tex = device.makeTexture(descriptor: desc) else { return nil }
        tex.replace(region: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0, withBytes: raw, bytesPerRow: bytesPerRow)
        return tex
    }

    static func makeProceduralAsphaltTexture(device: MTLDevice) -> MTLTexture {
        let size = 128
        var px = [UInt8](repeating: 0, count: size * size * 4)
        for y in 0..<size {
            for x in 0..<size {
                let h = ((x * 73856093) ^ (y * 19349663)) & 255
                let v = UInt8(42 + (h % 26))
                let idx = (y * size + x) * 4
                px[idx] = v
                px[idx + 1] = v
                px[idx + 2] = UInt8(min(255, Int(v) + 2))
                px[idx + 3] = 255
            }
        }
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: size, height: size, mipmapped: false)
        desc.usage = [.shaderRead]
        let tex = device.makeTexture(descriptor: desc)!
        tex.replace(region: MTLRegionMake2D(0, 0, size, size), mipmapLevel: 0, withBytes: px, bytesPerRow: size * 4)
        return tex
    }

    static func makeProceduralCarbonTexture(device: MTLDevice) -> MTLTexture {
        let size = 64
        var px = [UInt8](repeating: 0, count: size * size * 4)
        for y in 0..<size {
            for x in 0..<size {
                let diag = ((x + y) / 4) % 2
                let v: UInt8 = diag == 0 ? 28 : 48
                let idx = (y * size + x) * 4
                px[idx] = v
                px[idx + 1] = v
                px[idx + 2] = UInt8(Int(v) + 4)
                px[idx + 3] = 255
            }
        }
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: size, height: size, mipmapped: false)
        desc.usage = [.shaderRead]
        let tex = device.makeTexture(descriptor: desc)!
        tex.replace(region: MTLRegionMake2D(0, 0, size, size), mipmapLevel: 0, withBytes: px, bytesPerRow: size * 4)
        return tex
    }

    static func writeBGRA8TextureToPNG(texture: MTLTexture, outURL: URL) throws {
        let width = texture.width
        let height = texture.height
        let bytesPerRow = width * 4
        var bgra = [UInt8](repeating: 0, count: bytesPerRow * height)
        texture.getBytes(&bgra, bytesPerRow: bytesPerRow, from: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0)

        var i = 3
        while i < bgra.count {
            bgra[i] = 255
            i += 4
        }

        guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
              let provider = CGDataProvider(data: Data(bgra) as CFData),
              let cgImage = CGImage(
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bitsPerPixel: 32,
                  bytesPerRow: bytesPerRow,
                  space: cs,
                  bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                  provider: provider,
                  decode: nil,
                  shouldInterpolate: false,
                  intent: .defaultIntent
              ) else {
            throw NSError(domain: "RTSceneBuilder", code: 10, userInfo: [NSLocalizedDescriptionKey: "Failed to build CGImage"])
        }

        try FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard let dest = CGImageDestinationCreateWithURL(outURL as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            throw NSError(domain: "RTSceneBuilder", code: 11, userInfo: [NSLocalizedDescriptionKey: "Failed to create PNG destination"])
        }
        CGImageDestinationAddImage(dest, cgImage, nil)
        if !CGImageDestinationFinalize(dest) {
            throw NSError(domain: "RTSceneBuilder", code: 12, userInfo: [NSLocalizedDescriptionKey: "Failed to write PNG"])
        }
    }
}
