import Foundation
import Metal
import simd

// MARK: - Full-Island Rockport XZ AABB Streamer & 128x128x8 Real World Light Grid

/// GPU-ready parameters describing the 128x128x8 XZ spatial light grid.
/// Shader lookup:
///   int2 cell = clamp(int2(floor((worldPos.xz - originXZ) * invCellXZ)), 0, 127);
///   uint base = (uint(cell.y) * 128u + uint(cell.x)) * 8u;
///   for (uint k = 0; k < 8u; ++k) { uint idx = lightGrid[base + k]; if (idx == 0xFFFFu) break; ... }
struct RTLightGridParams {
    var originXZ: SIMD2<Float>
    var invCellXZ: SIMD2<Float>
    var gridDim: UInt32
    var slotsPerCell: UInt32
    var lightCount: UInt32
    var reserved: UInt32

    /// Convenience float4 packing: (originX, originZ, invCellX, invCellZ)
    var originInvCellVec4: SIMD4<Float> {
        SIMD4<Float>(originXZ.x, originXZ.y, invCellXZ.x, invCellXZ.y)
    }
}

final class RTWorldStreamer {
    static let maxWorldSlots: Int = 8192
    static let maxSpatialInstances: Int = 6000
    static let defaultLoadRadius: Float = 350.0
    static let defaultUnloadRadius: Float = 430.0
    static let defaultPanoramaMinDist: Float = 250.0
    static let lightGridDim: Int = 128
    static let lightGridSlotsPerCell: Int = 8
    static let lightGridEmptySentinel: UInt16 = 0xFFFF

    let world: RTWorldPackage
    let loadRadius: Float
    let unloadRadius: Float
    let panoramaMinDist: Float
    let maxWorldSlots: Int
    let maxSpatialInstances: Int

    /// GPU buffer containing `RTWorldLight[]` (32 bytes each, bound at buffer(11) in raytraceKernel).
    let worldLightsBuffer: MTLBuffer
    /// GPU buffer containing `uint16[128 * 128 * 8]` light indices (`0xFFFF` sentinel, bound at buffer(12)).
    let lightGridBuffer: MTLBuffer
    /// World XZ mapping parameters for O(1) shader cell lookup.
    let lightGridParams: RTLightGridParams

    /// Pre-validated instances per section (only instances with a valid BLAS index).
    private let validSectionInstances: [[RkptInstance]]
    private var activeSectionMask: [Bool]
    private var hasInitialized: Bool = false

    private(set) var activeInstances: [RkptInstance] = []
    private(set) var activeSectionCount: Int = 0
    private(set) var activeSpatialSectionCount: Int = 0
    private(set) var activePanoramaCount: Int = 0
    private(set) var effectiveSpatialRadius: Float

    var estimatedWorldVRAMBytes: Int {
        world.estimatedVRAMBytes + worldLightsBuffer.length + lightGridBuffer.length
    }

    var landmarks: [RkptLandmark] {
        if !world.landmarks.isEmpty {
            return world.landmarks
        }
        return [
            RkptLandmark(
                name: world.url.deletingPathExtension().lastPathComponent,
                spawnPos: world.spawnPos,
                spawnYawRad: world.spawnYawRad,
                regionId: 0,
                flags: 0
            )
        ]
    }

    init(
        world: RTWorldPackage,
        device: MTLDevice,
        loadRadius: Float = RTWorldStreamer.defaultLoadRadius,
        unloadRadius: Float = RTWorldStreamer.defaultUnloadRadius,
        panoramaMinDist: Float = RTWorldStreamer.defaultPanoramaMinDist,
        maxWorldSlots: Int = RTWorldStreamer.maxWorldSlots,
        maxSpatialInstances: Int = RTWorldStreamer.maxSpatialInstances
    ) throws {
        self.world = world
        self.loadRadius = max(50.0, loadRadius)
        self.unloadRadius = max(self.loadRadius + 20.0, unloadRadius)
        self.panoramaMinDist = max(0.0, panoramaMinDist)
        self.maxWorldSlots = max(64, maxWorldSlots)
        self.maxSpatialInstances = min(max(64, maxSpatialInstances), self.maxWorldSlots)
        self.effectiveSpatialRadius = self.loadRadius
        self.activeSectionMask = [Bool](repeating: false, count: world.sections.count)

        // Pre-slice valid instances per section once at load time (O(1) per-section compaction during streaming)
        var perSec: [[RkptInstance]] = []
        perSec.reserveCapacity(world.sections.count)
        let totalInsts = world.instances.count
        let meshCount = world.meshToBlasIndex.count
        for sec in world.sections {
            let sStart = min(Int(sec.instanceStart), totalInsts)
            let sEnd = min(sStart + Int(sec.instanceCount), totalInsts)
            var slice: [RkptInstance] = []
            slice.reserveCapacity(max(0, sEnd - sStart))
            if sStart < sEnd {
                for i in sStart..<sEnd {
                    let inst = world.instances[i]
                    let mIdx = Int(inst.meshIndex)
                    if mIdx >= 0 && mIdx < meshCount && world.meshToBlasIndex[mIdx] >= 0 {
                        slice.append(inst)
                    }
                }
            }
            perSec.append(slice)
        }
        self.validSectionInstances = perSec

        // Build GPU RTWorldLight[] buffer (buffer 11) + 128x128x8 UInt16 LightGrid buffer (buffer 12)
        let (wlBuf, lgBuf, lgParams) = try Self.buildWorldLightGrid(world: world, device: device)
        self.worldLightsBuffer = wlBuf
        self.lightGridBuffer = lgBuf
        self.lightGridParams = lgParams

        self.activeInstances.reserveCapacity(self.maxWorldSlots)
    }

    // MARK: - XZ AABB-Distance Dynamic Streaming + Distant Horizon Panorama Ring

    @inline(__always)
    private static func xzAABBDistance(_ p: SIMD3<Float>, aabbMin: SIMD3<Float>, aabbMax: SIMD3<Float>) -> Float {
        let dx = max(aabbMin.x - p.x, max(0.0, p.x - aabbMax.x))
        let dz = max(aabbMin.z - p.z, max(0.0, p.z - aabbMax.z))
        return hypot(dx, dz)
    }

    /// Update active streamed world sections around `playerPos`.
    /// Returns `(dirty: true, ...)` ONLY when the active section set changes (or `forceRebuild == true`),
    /// avoiding unnecessary TLAS world-descriptor rewrites on steady frames.
    @discardableResult
    func update(
        playerPos: SIMD3<Float>,
        forceRebuild: Bool = false
    ) -> (dirty: Bool, activeInstances: [RkptInstance], activeSectionCount: Int, panoramaCount: Int) {
        let secCount = world.sections.count
        if secCount == 0 {
            let dirty = forceRebuild || !hasInitialized
            hasInitialized = true
            return (dirty, [], 0, 0)
        }

        // Fast path for small pilot districts (<= 64 sections, <= maxWorldSlots): keep entire district active
        if world.panoramaSectionCount == 0 && world.instances.count <= maxSpatialInstances && secCount <= 64 {
            if !hasInitialized || forceRebuild {
                activeInstances.removeAll(keepingCapacity: true)
                var actSecs = 0
                for sIdx in 0..<secCount {
                    let sInsts = validSectionInstances[sIdx]
                    if !sInsts.isEmpty {
                        activeInstances.append(contentsOf: sInsts)
                        activeSectionMask[sIdx] = true
                        actSecs += 1
                    }
                }
                activeSectionCount = actSecs
                activeSpatialSectionCount = actSecs
                activePanoramaCount = 0
                effectiveSpatialRadius = loadRadius
                hasInitialized = true
                return (true, activeInstances, activeSectionCount, 0)
            }
            return (false, activeInstances, activeSectionCount, 0)
        }

        var candidateSpatial: [(secIdx: Int, dist: Float, instCount: Int)] = []
        candidateSpatial.reserveCapacity(96)
        var candidatePanorama: [(secIdx: Int, dist: Float, instCount: Int)] = []
        candidatePanorama.reserveCapacity(80)

        var newMask = [Bool](repeating: false, count: secCount)
        let panUnloadDist = max(0.0, panoramaMinDist - 30.0)

        for sIdx in 0..<secCount {
            let instCnt = validSectionInstances[sIdx].count
            if instCnt == 0 { continue }
            let sec = world.sections[sIdx]
            let wasActive = hasInitialized && activeSectionMask[sIdx]

            if sec.isPanorama {
                let cd = hypot(sec.center.x - playerPos.x, sec.center.z - playerPos.z)
                let keep = wasActive ? (cd > panUnloadDist) : (cd > panoramaMinDist)
                if keep {
                    candidatePanorama.append((sIdx, cd, instCnt))
                }
            } else {
                let d = Self.xzAABBDistance(playerPos, aabbMin: sec.aabbMin, aabbMax: sec.aabbMax)
                let thresh = wasActive ? unloadRadius : loadRadius
                if d <= thresh {
                    candidateSpatial.append((sIdx, d, instCnt))
                }
            }
        }

        // Dynamic radius clamp: guarantee active spatial instances NEVER exceed maxSpatialInstances (6,000)
        // by keeping the closest sections first around the player.
        var totalSpatialInsts = candidateSpatial.reduce(0) { $0 + $1.instCount }
        var effRadius = loadRadius
        if totalSpatialInsts > maxSpatialInstances {
            candidateSpatial.sort { $0.dist < $1.dist }
            var kept: [(secIdx: Int, dist: Float, instCount: Int)] = []
            kept.reserveCapacity(candidateSpatial.count)
            var running = 0
            for item in candidateSpatial {
                if running + item.instCount <= maxSpatialInstances || kept.isEmpty {
                    kept.append(item)
                    running += item.instCount
                    effRadius = item.dist
                } else {
                    break
                }
            }
            candidateSpatial = kept
            totalSpatialInsts = running
        }

        for item in candidateSpatial {
            newMask[item.secIdx] = true
        }

        // Fit distant horizon panorama ring into remaining budget up to maxWorldSlots (8,192)
        let remainingBudget = max(0, maxWorldSlots - totalSpatialInsts)
        var panInstTotal = 0
        var keptPan: [(secIdx: Int, dist: Float, instCount: Int)] = []
        keptPan.reserveCapacity(candidatePanorama.count)
        if remainingBudget > 0 {
            // Prioritize closer horizon sections first if budget ever gets tight
            let totalPan = candidatePanorama.reduce(0) { $0 + $1.instCount }
            if totalPan > remainingBudget {
                candidatePanorama.sort { $0.dist < $1.dist }
            }
            for item in candidatePanorama {
                if panInstTotal + item.instCount <= remainingBudget {
                    keptPan.append(item)
                    panInstTotal += item.instCount
                    newMask[item.secIdx] = true
                }
            }
        }

        if hasInitialized && !forceRebuild && newMask == activeSectionMask {
            return (false, activeInstances, activeSectionCount, activePanoramaCount)
        }

        activeSectionMask = newMask
        hasInitialized = true
        effectiveSpatialRadius = effRadius

        activeInstances.removeAll(keepingCapacity: true)
        for item in candidateSpatial {
            activeInstances.append(contentsOf: validSectionInstances[item.secIdx])
        }
        for item in keptPan {
            activeInstances.append(contentsOf: validSectionInstances[item.secIdx])
        }

        activeSpatialSectionCount = candidateSpatial.count
        activePanoramaCount = keptPan.count
        activeSectionCount = activeSpatialSectionCount + activePanoramaCount

        return (true, activeInstances, activeSectionCount, activePanoramaCount)
    }

    // MARK: - Landmark Lookup & Teleport Helpers

    func landmark(namedOrIndex query: String) -> RkptLandmark? {
        let list = landmarks
        if list.isEmpty { return nil }
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return list.first }

        if let idx = Int(trimmed) {
            let wrapped = ((idx % list.count) + list.count) % list.count
            return list[wrapped]
        }

        let needle = trimmed.lowercased().replacingOccurrences(of: "_", with: " ")
        if let exact = list.first(where: { $0.name.lowercased() == needle }) {
            return exact
        }
        // Common shorthand aliases (e.g. "hwy99", "camden", "downtown", "rosewood", "college", "waterfront")
        let aliasNeedle: String
        switch needle {
        case "hwy", "hwy99", "highway99", "99": aliasNeedle = "highway 99"
        case "rw": aliasNeedle = "rosewood"
        case "dt": aliasNeedle = "downtown"
        case "cb", "camden": aliasNeedle = "camden"
        default: aliasNeedle = needle
        }
        if let partial = list.first(where: { $0.name.lowercased().contains(aliasNeedle) }) {
            return partial
        }
        return nil
    }

    func nextLandmark(after index: Int) -> (index: Int, landmark: RkptLandmark) {
        let list = landmarks
        let nextIdx = (index + 1) % max(1, list.count)
        return (nextIdx, list[nextIdx])
    }

    // MARK: - 128x128x8 Real Rockport Spatial Light Grid Builder

    private static func buildWorldLightGrid(
        world: RTWorldPackage,
        device: MTLDevice
    ) throws -> (MTLBuffer, MTLBuffer, RTLightGridParams) {
        let gridDim = lightGridDim
        let slots = lightGridSlotsPerCell
        let cellCount = gridDim * gridDim
        let gridElems = cellCount * slots

        guard let lgBuf = device.makeBuffer(
            length: gridElems * MemoryLayout<UInt16>.stride,
            options: .storageModeShared
        ) else {
            throw NSError(domain: "RTWorldStreamer", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate 128x128x8 world light grid buffer"
            ])
        }
        let gridPtr = lgBuf.contents().bindMemory(to: UInt16.self, capacity: gridElems)
        for i in 0..<gridElems {
            gridPtr[i] = lightGridEmptySentinel
        }

        // Compute tight XZ bounds over spatial sections (so panorama ocean skyboxes don't stretch cell size)
        var minX = Float.greatestFiniteMagnitude
        var minZ = Float.greatestFiniteMagnitude
        var maxX = -Float.greatestFiniteMagnitude
        var maxZ = -Float.greatestFiniteMagnitude
        for sec in world.sections where !sec.isPanorama && sec.instanceCount > 0 {
            minX = min(minX, sec.aabbMin.x)
            minZ = min(minZ, sec.aabbMin.z)
            maxX = max(maxX, sec.aabbMax.x)
            maxZ = max(maxZ, sec.aabbMax.z)
        }
        if minX > maxX {
            minX = world.header.worldBoundsMin.x
            minZ = world.header.worldBoundsMin.z
            maxX = world.header.worldBoundsMax.x
            maxZ = world.header.worldBoundsMax.z
        }
        for l in world.worldLights {
            minX = min(minX, l.posRadius.x)
            minZ = min(minZ, l.posRadius.z)
            maxX = max(maxX, l.posRadius.x)
            maxZ = max(maxZ, l.posRadius.z)
        }
        if minX > maxX {
            minX = -500; maxX = 500; minZ = -500; maxZ = 500
        }
        let pad: Float = 32.0
        minX -= pad; minZ -= pad
        maxX += pad; maxZ += pad
        let spanX = max(128.0, maxX - minX)
        let spanZ = max(128.0, maxZ - minZ)
        let cellSizeX = spanX / Float(gridDim)
        let cellSizeZ = spanZ / Float(gridDim)
        let invCellX = 1.0 / cellSizeX
        let invCellZ = 1.0 / cellSizeZ

        let usableLights = min(world.worldLights.count, 65534)
        let wlBufLen = max(1, usableLights) * MemoryLayout<RTWorldLight>.stride
        guard let wlBuf = device.makeBuffer(length: wlBufLen, options: .storageModeShared) else {
            throw NSError(domain: "RTWorldStreamer", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "Failed to allocate RTWorldLight GPU buffer"
            ])
        }

        if usableLights == 0 {
            memset(wlBuf.contents(), 0, wlBufLen)
            let params = RTLightGridParams(
                originXZ: SIMD2<Float>(minX, minZ),
                invCellXZ: SIMD2<Float>(invCellX, invCellZ),
                gridDim: UInt32(gridDim),
                slotsPerCell: UInt32(slots),
                lightCount: 0,
                reserved: 0
            )
            return (wlBuf, lgBuf, params)
        }

        let wlPtr = wlBuf.contents().bindMemory(to: RTWorldLight.self, capacity: usableLights)
        for i in 0..<usableLights {
            wlPtr[i] = world.worldLights[i]
        }

        // Bin every light into all 128x128 XZ cells intersected by its influence radius (+ margin)
        var buckets = [[UInt16]](repeating: [], count: cellCount)
        for i in 0..<usableLights {
            let l = world.worldLights[i]
            let lx = l.posRadius.x
            let lz = l.posRadius.z
            let reach = max(l.posRadius.w, 16.0) + 14.0
            let cx0 = max(0, min(gridDim - 1, Int(floor((lx - reach - minX) * invCellX))))
            let cx1 = max(0, min(gridDim - 1, Int(floor((lx + reach - minX) * invCellX))))
            let cz0 = max(0, min(gridDim - 1, Int(floor((lz - reach - minZ) * invCellZ))))
            let cz1 = max(0, min(gridDim - 1, Int(floor((lz + reach - minZ) * invCellZ))))
            let idx16 = UInt16(i)
            for cz in cz0...cz1 {
                let rowBase = cz * gridDim
                for cx in cx0...cx1 {
                    buckets[rowBase + cx].append(idx16)
                }
            }
        }

        // Keep top 8 highest-priority closest lights per cell (overhead streetlamps get slight priority over signs)
        for cz in 0..<gridDim {
            let centerZ = minZ + (Float(cz) + 0.5) * cellSizeZ
            let rowBase = cz * gridDim
            for cx in 0..<gridDim {
                let cellIdx = rowBase + cx
                var list = buckets[cellIdx]
                if list.isEmpty { continue }
                if list.count > slots {
                    let centerX = minX + (Float(cx) + 0.5) * cellSizeX
                    list.sort { a, b in
                        let la = world.worldLights[Int(a)]
                        let lb = world.worldLights[Int(b)]
                        let da = hypot(la.posRadius.x - centerX, la.posRadius.z - centerZ) - (la.isDownwardStreetlamp ? 8.0 : 0.0)
                        let db = hypot(lb.posRadius.x - centerX, lb.posRadius.z - centerZ) - (lb.isDownwardStreetlamp ? 8.0 : 0.0)
                        return da < db
                    }
                }
                let outBase = cellIdx * slots
                let n = min(list.count, slots)
                for k in 0..<n {
                    gridPtr[outBase + k] = list[k]
                }
            }
        }

        let params = RTLightGridParams(
            originXZ: SIMD2<Float>(minX, minZ),
            invCellXZ: SIMD2<Float>(invCellX, invCellZ),
            gridDim: UInt32(gridDim),
            slotsPerCell: UInt32(slots),
            lightCount: UInt32(usableLights),
            reserved: 0
        )
        return (wlBuf, lgBuf, params)
    }

    /// CPU query helper mirroring the exact GPU lightGrid lookup (useful for tests & diagnostics).
    func lightsAt(worldXZ: SIMD2<Float>) -> [RTWorldLight] {
        guard lightGridParams.lightCount > 0 else { return [] }
        let dim = Int(lightGridParams.gridDim)
        let slots = Int(lightGridParams.slotsPerCell)
        let cx = max(0, min(dim - 1, Int(floor((worldXZ.x - lightGridParams.originXZ.x) * lightGridParams.invCellXZ.x))))
        let cz = max(0, min(dim - 1, Int(floor((worldXZ.y - lightGridParams.originXZ.y) * lightGridParams.invCellXZ.y))))
        let base = (cz * dim + cx) * slots
        let gridPtr = lightGridBuffer.contents().bindMemory(to: UInt16.self, capacity: dim * dim * slots)
        var out: [RTWorldLight] = []
        out.reserveCapacity(slots)
        for k in 0..<slots {
            let idx = gridPtr[base + k]
            if idx == Self.lightGridEmptySentinel { break }
            let i = Int(idx)
            if i < world.worldLights.count {
                out.append(world.worldLights[i])
            }
        }
        return out
    }
}
